import re
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import ClassVar
from attrs import define, field
import httpx
from cattrs.preconf.pyyaml import make_converter

from upslope.logging import get_logger

logger = get_logger(__name__)

# Dates the run being served. The 12km domain is published first, so its hour-0 image is the
# first to change when a new run starts.
RUN_REFERENCE_IMAGE = "timeindep/images_d2/tsfc.00.0000.gif"


class RunStatusError(Exception):
    """UW's response doesn't say which model run it is serving."""


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
    """HTTP client for downloading UW WRF forecast images and checking run status.

    Attributes:
        config: Name-to-URL-code mappings and forecast schedules used to build image URLs.
    """

    config: WrfConfig
    _client: httpx.Client = field(factory=lambda: httpx.Client(timeout=60.0))

    base_url: ClassVar[str] = "https://a.atmos.washington.edu/wrfrt/data"

    def build_url(
        self, domain: str, variable: str, hour: int, maptype: str | None = None
    ) -> str:
        domain_code = self.config.get_domain_code(domain)
        maptype_code = self.config.get_maptype_code(maptype) if maptype is not None else ""
        variable_code = self.config.get_variable_code(variable)
        return f"{self.base_url}/timeindep/images_{domain_code}/{maptype_code}{variable_code}.{hour:02d}.0000.gif"

    def download_image(self, layer: LayerConfig, hour: int) -> bytes:
        """Download one forecast hour of a layer from the latest run.

        Args:
            layer: Layer to download.
            hour: Forecast hour.

        Returns:
            The GIF's bytes. Raises httpx.HTTPError if the download fails.
        """
        response = self._client.get(self.build_url(layer.domain, layer.variable, hour, layer.maptype))
        _ = response.raise_for_status()
        return response.content

    def download_layer(self, layer: LayerConfig, output_path: Path) -> None:
        """Download a layer's GIF for each forecast hour, skipping files already on disk.

        Args:
            layer: Layer to download.
            output_path: Root directory; files go to `<domain>/<variable>/hourNN.gif`.
        """
        for fx_hour in self.config.get_forecast_hours(layer.forecast_schedule):
            gif_output_path = (
                output_path / layer.domain / layer.variable / f"hour{fx_hour:02d}.gif"
            )
            if gif_output_path.exists():
                logger.debug("skipping %s, already downloaded", gif_output_path)
                continue

            gif_output_path.parent.mkdir(exist_ok=True, parents=True)
            image_url = self.build_url(
                layer.domain, layer.variable, fx_hour, layer.maptype
            )
            logger.debug("downloading %s", image_url)
            r = self._client.get(image_url)
            _ = r.raise_for_status()
            _ = gif_output_path.write_bytes(r.content)

    def run_in_progress(self) -> bool:
        """Check whether UW is partway through publishing a model run.

        UW overwrites the images in place as a run progresses, so during a run the early forecast hours
        come from the new run and the later ones from the previous run.

        Returns:
            False if the status page says the latest run is done or the next one hasn't begun; True
            otherwise, including for a status this code doesn't recognize.
        """
        response = self._client.get(f"{self.base_url}/run_status.html")
        _ = response.raise_for_status()
        match = re.search(r"<td class=['\"]?(\w+)", response.text)
        if match is not None and match.group(1) in ("wait", "done"):
            return False
        return "complete" not in response.text

    def current_run_initialization(self) -> datetime:
        """Identify the model run whose images UW is currently serving.

        The status page doesn't name the run, and the clock alone mislabels it: at 00Z and 12Z UW is
        still serving the previous run. Instead, read when the 12km domain's hour-0 image was
        published (about 4 hours after initialization) and round down to the run before it.

        Returns:
            The run's initialization time (00Z or 12Z, UTC). Raises RunStatusError if UW doesn't say
            when the image was published.
        """
        response = self._client.head(f"{self.base_url}/{RUN_REFERENCE_IMAGE}")
        _ = response.raise_for_status()
        last_modified = response.headers.get("last-modified")
        if last_modified is None:
            raise RunStatusError(f"UW did not send Last-Modified for {RUN_REFERENCE_IMAGE}")
        try:
            published = parsedate_to_datetime(last_modified).astimezone(UTC)
        except (TypeError, ValueError) as e:
            raise RunStatusError(f"UW sent an unreadable Last-Modified for {RUN_REFERENCE_IMAGE}: {last_modified!r}") from e
        return published.replace(hour=published.hour - published.hour % 12, minute=0, second=0, microsecond=0)
