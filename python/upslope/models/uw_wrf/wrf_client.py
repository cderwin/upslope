import hashlib
import json
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import ClassVar
from attrs import define, field
import httpx
from cattrs.preconf.pyyaml import make_converter

from upslope.logging import get_logger

logger = get_logger(__name__)

# UW runs at 00Z and 12Z. How many recent runs to consider when looking for the newest complete one;
# a run takes under 12 hours, so two is normally enough, and the rest cover a late or missing run.
RUN_LOOKBACK = 4


def latest_initializations(now: datetime, count: int = RUN_LOOKBACK) -> list[datetime]:
    """The most recent 00Z/12Z initialization times at or before `now`, newest first.

    Args:
        now: Current time (timezone-aware).
        count: How many times to return.

    Returns:
        `count` initialization times in UTC.
    """
    now = now.astimezone(UTC)
    latest = now.replace(hour=now.hour - now.hour % 12, minute=0, second=0, microsecond=0)
    return [latest - timedelta(hours=12 * i) for i in range(count)]


@define
class CodeConfig:
    name: str
    code: str


@define
class ForecastScheduleConfig:
    name: str
    hours: list[int]


@define
class LayerConfig:
    """One forecast layer to download and process.

    Attributes:
        domain: Domain name, e.g. the model grid region.
        variable: Forecast variable name.
        forecast_schedule: Name of the schedule giving the forecast hours.
        maptype: Map type name.
    """

    domain: str
    variable: str
    forecast_schedule: str
    maptype: str


@define
class WrfConfig:
    """UW WRF settings: name-to-URL-code mappings, forecast schedules, and layers to fetch.

    Attributes:
        domain_codes: Domain name to URL code mappings.
        maptype_codes: Map type name to URL code mappings.
        variable_codes: Variable name to URL code mappings.
        forecast_schedules: Named lists of forecast hours.
        layers: Layers to process.
    """

    domain_codes: list[CodeConfig]
    maptype_codes: list[CodeConfig]
    variable_codes: list[CodeConfig]
    forecast_schedules: list[ForecastScheduleConfig]
    layers: list[LayerConfig]

    _domains: dict[str, str] = field(init=False, factory=dict)
    _maptypes: dict[str, str] = field(init=False, factory=dict)
    _variables: dict[str, str] = field(init=False, factory=dict)
    _schedules: dict[str, list[int]] = field(init=False, factory=dict)


    def get_domain_code(self, domain_name: str) -> str:
        if not self._domains:
            self._domains = {conf.name: conf.code for conf in self.domain_codes}

        code = self._domains.get(domain_name)
        if code is None:
            raise ValueError(f"{domain_name} was not found")
        return code

    def get_maptype_code(self, maptype_name: str) -> str:
        if not self._maptypes:
            self._maptypes = {conf.name: conf.code for conf in self.maptype_codes}

        code = self._maptypes.get(maptype_name)
        if code is None:
            raise ValueError(f"{maptype_name} was not found")
        return code

    def get_variable_code(self, variable_name: str) -> str:
        if not self._variables:
            self._variables = {conf.name: conf.code for conf in self.variable_codes}

        code = self._variables.get(variable_name)
        if code is None:
            raise ValueError(f"{variable_name} was not found")
        return code

    def get_forecast_hours(self, schedule_name: str) -> list[int]:
        """Look up the forecast hours for a named schedule.

        Args:
            schedule_name: Name of a configured forecast schedule.

        Returns:
            The schedule's forecast hours. Raises ValueError if the name is unknown.
        """
        if not self._schedules:
            self._schedules = {
                conf.name: conf.hours for conf in self.forecast_schedules
            }

        code = self._schedules.get(schedule_name)
        if code is None:
            raise ValueError(f"{schedule_name} was not found")
        return code


    def fingerprint(self) -> str:
        """Identify what the export produces under this config: each layer and the URLs and hours it uses.

        Codes and schedules that no layer uses don't affect it.

        Returns:
            A short hex digest; equal configs give equal digests.
        """
        layers = [
            {
                "domain": [layer.domain, self.get_domain_code(layer.domain)],
                "variable": [layer.variable, self.get_variable_code(layer.variable)],
                "maptype": [layer.maptype, self.get_maptype_code(layer.maptype)],
                "hours": self.get_forecast_hours(layer.forecast_schedule),
            }
            for layer in self.layers
        ]
        return hashlib.sha256(json.dumps(layers, sort_keys=True).encode()).hexdigest()[:16]

    @classmethod
    def load(cls, wrf_config: Path) -> "WrfConfig":
        """Load the config from a YAML file.

        Args:
            wrf_config: Path to the YAML config file.

        Returns:
            The parsed WrfConfig.
        """
        converter = make_converter()
        return converter.loads(wrf_config.read_text(), cls)


@define
class WrfClient:
    """HTTP client for downloading UW WRF forecast images from a specific model run.

    UW also serves the newest images under `timeindep/`, but overwrites them in place while a run is in
    progress, so they mix two runs for hours at a time. Each run's own directory (`<YYYYMMDDHH>/`) is
    never mixed, and UW keeps them for at least two weeks.

    Attributes:
        config: Name-to-URL-code mappings and forecast schedules used to build image URLs.
    """

    config: WrfConfig
    _client: httpx.Client = field(factory=lambda: httpx.Client(timeout=60.0))

    base_url: ClassVar[str] = "https://a.atmos.washington.edu/wrfrt/data"

    def build_url(
        self, initialized: datetime, domain: str, variable: str, hour: int, maptype: str | None = None
    ) -> str:
        """Build the URL of one forecast image.

        Args:
            initialized: Initialization time of the model run.
            domain: Domain name.
            variable: Variable name.
            hour: Forecast hour.
            maptype: Map type name; None for the domain's default map.

        Returns:
            The image's URL in the run's directory. Raises ValueError if a name isn't configured.
        """
        domain_code = self.config.get_domain_code(domain)
        maptype_code = self.config.get_maptype_code(maptype) if maptype is not None else ""
        variable_code = self.config.get_variable_code(variable)
        run = f"{initialized.astimezone(UTC):%Y%m%d%H}"
        return f"{self.base_url}/{run}/images_{domain_code}/{maptype_code}{variable_code}.{hour:02d}.0000.gif"

    def download_image(self, layer: LayerConfig, hour: int, initialized: datetime) -> bytes:
        """Download one forecast hour of a layer.

        Args:
            layer: Layer to download.
            hour: Forecast hour.
            initialized: Initialization time of the model run to download from.

        Returns:
            The GIF's bytes. Raises httpx.HTTPError if the download fails.
        """
        response = self._client.get(self.build_url(initialized, layer.domain, layer.variable, hour, layer.maptype))
        _ = response.raise_for_status()
        return response.content

    def download_layer(self, layer: LayerConfig, output_path: Path, initialized: datetime) -> None:
        """Download a layer's GIF for each forecast hour, skipping files already on disk.

        Args:
            layer: Layer to download.
            output_path: Root directory; files go to `<domain>/<variable>/hourNN.gif`.
            initialized: Initialization time of the model run to download from.
        """
        for fx_hour in self.config.get_forecast_hours(layer.forecast_schedule):
            gif_output_path = (
                output_path / layer.domain / layer.variable / f"hour{fx_hour:02d}.gif"
            )
            if gif_output_path.exists():
                logger.debug("skipping %s, already downloaded", gif_output_path)
                continue

            gif_output_path.parent.mkdir(exist_ok=True, parents=True)
            image_url = self.build_url(initialized, layer.domain, layer.variable, fx_hour, layer.maptype)
            logger.debug("downloading %s", image_url)
            r = self._client.get(image_url)
            _ = r.raise_for_status()
            _ = gif_output_path.write_bytes(r.content)

    def run_complete(self, initialized: datetime) -> bool:
        """Check whether UW has published every configured layer of a model run.

        UW publishes a run's forecast hours in order, so a run is complete once each layer's last
        scheduled hour exists.

        Args:
            initialized: Initialization time of the run.

        Returns:
            True if every layer's last hour exists. Raises httpx.HTTPError on errors other than 404.
        """
        for layer in self.config.layers:
            last_hour = max(self.config.get_forecast_hours(layer.forecast_schedule))
            url = self.build_url(initialized, layer.domain, layer.variable, last_hour, layer.maptype)
            response = self._client.head(url)
            if response.status_code == 404:
                logger.debug("run %s is incomplete: %s is missing", f"{initialized:%Y-%m-%d %HZ}", url)
                return False
            _ = response.raise_for_status()
        return True

    def latest_complete_run(self, now: datetime | None = None) -> datetime | None:
        """Find the newest model run that UW has finished publishing.

        Args:
            now: Current time; defaults to now. Only runs initialized at or before it are considered.

        Returns:
            The run's initialization time (00Z or 12Z, UTC), or None if none of the last
            RUN_LOOKBACK runs is complete.
        """
        for initialized in latest_initializations(now or datetime.now(UTC)):
            if self.run_complete(initialized):
                return initialized
        return None
