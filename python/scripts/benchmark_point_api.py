"""Benchmark the point forecast API (`GET /fx/point`) and compare its forecasts with Open-Meteo's.

For each point it measures `/fx/point` latency and response size (a first request, then repeats, with and without
`interpolate`, then every point at once), and compares each model's interpolated series with Open-Meteo's forecast
for the same model, asked at the same elevation so both apply a lapse-rate adjustment to the same height. Only
reads: production's public API and Open-Meteo's free API (a few dozen requests in all).

Run from `python/`:

    uv run scripts/benchmark_point_api.py
    uv run scripts/benchmark_point_api.py --base-url http://localhost:8000 --repeats 10 --hours 48 --json out.json

Differences are ours minus Open-Meteo's, over the hours both forecast from the later of the two runs' start
(`--hours`). The runs may differ (Open-Meteo picks up new runs sooner, and HRRR and NBM run hourly), so the run
table shows each side's initialization time.
"""

import asyncio
import json
import math
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import httpx
from attrs import asdict, define

BASE_URL = "https://upslope.fly.dev"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_META_URL = "https://api.open-meteo.com/data/{domain}/static/meta.json"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


@define(frozen=True)
class Point:
    """A place to forecast. Coordinates were checked against the elevation model (`elevation` is its height there).

    Attributes:
        name: Display name.
        lat, lon: WGS 84 degrees.
        note: What exactly the coordinates mark.
    """

    name: str
    lat: float
    lon: float
    note: str


POINTS = [
    Point("Camp Muir", 46.83556, -121.73139, "the shelter (DEM 3095 m)"),
    Point("Cascade Pass", 48.4683, -121.0600, "the pass (DEM 1624 m; published 1643 m)"),
    Point("Hurricane Ridge", 47.9696, -123.4985, "the visitor centre (DEM 1602 m)"),
    # No gazetteer lists Bullion Peak; SummitPost gives 6,479 ft. This is the 1973 m (6,473 ft) summit NE of Bullion
    # Basin, the closest DEM summit to that height. Unconfirmed.
    Point("Bullion Peak", 46.96042, -121.44542, "summit NE of Bullion Basin (DEM 1973 m; unconfirmed)"),
    Point("Chair Peak", 47.45958, -121.46708, "the summit (DEM 1882 m; published 1901 m)"),
    Point("Table Mountain", 48.84795, -121.71333, "the summit near Mt Baker (DEM 1749 m)"),
]


@define(frozen=True)
class OpenMeteoModel:
    """Open-Meteo's equivalent of an archived model.

    Attributes:
        model: The forecast API's `models` value.
        domain: The domain whose `meta.json` gives the newest run.
    """

    model: str
    domain: str


# Open-Meteo's `ncep_gfs025` has no surface fields in the forecast API; `gfs_global` is its GFS 0.11°/0.25° blend
MODELS = {
    "hrrr_3km": OpenMeteoModel("ncep_hrrr_conus", "ncep_hrrr_conus"),
    "rrfs_3km": OpenMeteoModel("ncep_rrfs_conus", "ncep_rrfs_conus"),
    "gfs_0.25": OpenMeteoModel("gfs_global", "ncep_gfs013"),
    "aifs_0.25": OpenMeteoModel("ecmwf_aifs025_single", "ecmwf_aifs025_single"),
    "hrdps_2.5km": OpenMeteoModel("gem_hrdps_continental", "cmc_gem_hrdps"),
    "ifs_0.25": OpenMeteoModel("ecmwf_ifs025", "ecmwf_ifs025"),
    "nbm_2.5km": OpenMeteoModel("ncep_nbm_conus", "ncep_nbm_conus"),
}

# Our variable id -> Open-Meteo's hourly variable, in the same units (wind in m/s). Snowfall is left out: Open-Meteo
# gives it as snow depth (cm), ours is water equivalent.
VARIABLES = {
    "temperature_2m": "temperature_2m",
    "dew_point_2m": "dew_point_2m",
    "relative_humidity_2m": "relative_humidity_2m",
    "wind_speed_10m": "wind_speed_10m",
    "wind_direction_10m": "wind_direction_10m",
    "wind_gust_10m": "wind_gusts_10m",
    "precipitation": "precipitation",
    "snow_depth": "snow_depth",
    "freezing_level": "freezing_level_height",
    "cloud_cover": "cloud_cover",
    "shortwave_radiation": "shortwave_radiation",
    "pressure_msl": "pressure_msl",
}
CIRCULAR = {"wind_direction_10m"}
SUMS = {"precipitation"}


# --- requests


@define
class Timed:
    """A response's JSON body, how long it took, and its size on the wire."""

    body: dict[str, Any]
    seconds: float
    wire_bytes: int
    status: int


async def get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Timed:
    start = time.perf_counter()
    response = await client.get(url, params=params)
    body = response.json() if response.content else {}
    seconds = time.perf_counter() - start
    return Timed(body, seconds, response.num_bytes_downloaded, response.status_code)


def point_params(point: Point, interpolate: bool) -> dict[str, Any]:
    return {"lat": point.lat, "lon": point.lon} | ({"interpolate": "true"} if interpolate else {})


def open_meteo_params(point: Point, elevation: float | None, variables: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "latitude": point.lat,
        "longitude": point.lon,
        "hourly": ",".join(variables),
        "models": ",".join(m.model for m in MODELS.values()),
        "timeformat": "unixtime",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
        "past_days": 1,
        "forecast_days": 16,
    }
    if elevation is not None:
        params["elevation"] = elevation
    return params


# --- latency


@define
class Latency:
    """`/fx/point` timings for one point."""

    first: float
    repeats: list[float]
    interpolated: list[float]
    wire_bytes: int
    interpolated_wire_bytes: int


async def measure(client: httpx.AsyncClient, url: str, point: Point, repeats: int) -> tuple[Latency, Timed]:
    """Time `/fx/point` at a point: a first request, then `repeats` more without and with `interpolate`.

    Returns:
        The timings, and the last interpolated response (for the comparison).
    """
    first = await get(client, url, point_params(point, False))
    if first.status != 200:
        raise click.ClickException(f"{point.name}: /fx/point answered {first.status}: {first.body}")
    plain = [await get(client, url, point_params(point, False)) for _ in range(repeats)]
    interpolated = [await get(client, url, point_params(point, True)) for _ in range(repeats)]
    latency = Latency(
        first.seconds,
        [t.seconds for t in plain],
        [t.seconds for t in interpolated],
        first.wire_bytes,
        interpolated[-1].wire_bytes,
    )
    return latency, interpolated[-1]


# --- comparison


@define
class Comparison:
    """One model's variable at one point, ours against Open-Meteo's.

    Attributes:
        hours: Hours both have a value for.
        bias: Mean of ours minus Open-Meteo's.
        mae: Mean absolute difference.
        ours_mean, theirs_mean: Each side's mean over those hours (for sums, their total).
    """

    hours: int
    bias: float
    mae: float
    ours_mean: float
    theirs_mean: float


def compare(variable: str, ours: list[float | None], theirs: list[float | None]) -> Comparison | None:
    pairs = [(a, b) for a, b in zip(ours, theirs, strict=True) if a is not None and b is not None]
    if not pairs:
        return None
    if variable in CIRCULAR:
        differences = [(a - b + 180) % 360 - 180 for a, b in pairs]
        ours_mean, theirs_mean = (_circular_mean(side) for side in zip(*pairs, strict=True))
        return Comparison(
            len(pairs),
            statistics.fmean(differences),
            statistics.fmean(abs(d) for d in differences),
            ours_mean,
            theirs_mean,
        )
    differences = [a - b for a, b in pairs]
    total = sum if variable in SUMS else statistics.fmean
    return Comparison(
        len(pairs),
        statistics.fmean(differences),
        statistics.fmean(abs(d) for d in differences),
        total(a for a, _ in pairs),
        total(b for _, b in pairs),
    )


def _circular_mean(degrees: tuple[float, ...]) -> float:
    x = sum(math.sin(math.radians(d)) for d in degrees)
    y = sum(math.cos(math.radians(d)) for d in degrees)
    return math.degrees(math.atan2(x, y)) % 360


def comparisons(
    ours: dict[str, Any], theirs: dict[str, Any], runs: dict[str, datetime | None], hours: int
) -> dict[str, dict[str, Comparison]]:
    """Every model's variables at a point, over `hours` from the later of the two runs' start.

    Returns:
        {model: {variable: comparison}}, leaving out pairs with nothing to compare.
    """
    their_times = theirs["hourly"]["time"]
    result: dict[str, dict[str, Comparison]] = {}
    for model_id, model in ours["models"].items():
        if model_id not in MODELS:
            continue
        start = datetime.fromisoformat(model["init_time"])
        if (their_run := runs.get(model_id)) is not None:
            start = max(start, their_run)
        window = [t for t in ours["times"] if start.timestamp() <= t < start.timestamp() + hours * 3600]
        for variable, their_variable in VARIABLES.items():
            series = ours["variables"].get(variable, {}).get("series", {}).get(model_id)
            their_series = theirs["hourly"].get(f"{their_variable}_{MODELS[model_id].model}")
            if series is None or their_series is None:
                continue
            ours_by_time = dict(zip(ours["times"], series, strict=True))
            theirs_by_time = dict(zip(their_times, their_series, strict=True))
            got = compare(variable, [ours_by_time.get(t) for t in window], [theirs_by_time.get(t) for t in window])
            if got is not None:
                result.setdefault(model_id, {})[variable] = got
    return result


async def open_meteo_runs(client: httpx.AsyncClient) -> dict[str, datetime | None]:
    """Each model's newest run on Open-Meteo, from its domain's `meta.json` (None if unreadable)."""

    async def one(model: OpenMeteoModel) -> datetime | None:
        try:
            response = await client.get(OPEN_METEO_META_URL.format(domain=model.domain))
            response.raise_for_status()
            return datetime.fromtimestamp(response.json()["last_run_initialisation_time"], UTC)
        except (httpx.HTTPError, KeyError, ValueError):
            return None

    runs = await asyncio.gather(*(one(m) for m in MODELS.values()))
    return dict(zip(MODELS, runs, strict=True))


# --- output


def table(header: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(w) if i == 0 else cell.rjust(w) for i, (cell, w) in enumerate(zip(row, widths)))
        for row in [header, *rows]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def ms(seconds: list[float]) -> str:
    return f"{statistics.median(seconds) * 1000:.0f}/{max(seconds) * 1000:.0f}"


def number(value: float, digits: int) -> str:
    return f"{value:+.{digits}f}"


DIGITS = {"pressure_msl": 1, "freezing_level": 0, "shortwave_radiation": 0, "cloud_cover": 0, "relative_humidity_2m": 0}
DIGITS |= {"wind_direction_10m": 0, "snow_depth": 2}


@click.command()
@click.option("--base-url", default=BASE_URL, show_default=True, help="The upslope server.")
@click.option("--repeats", default=5, show_default=True, help="Requests per point after the first, per mode.")
@click.option("--hours", default=72, show_default=True, help="Hours compared, from the later run's start.")
@click.option(
    "--json", "json_path", type=click.Path(dir_okay=False, path_type=Path), help="Also write the results here."
)
def main(base_url: str, repeats: int, hours: int, json_path: Path | None) -> None:
    """Benchmark /fx/point at a few Cascade and Olympic points and compare it with Open-Meteo."""
    asyncio.run(run(base_url.rstrip("/"), repeats, hours, json_path))


async def run(base_url: str, repeats: int, hours: int, json_path: Path | None) -> None:
    url = f"{base_url}/fx/point"
    headers = {"Accept-Encoding": "gzip", "User-Agent": "upslope-point-benchmark"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        latencies: dict[str, Latency] = {}
        forecasts: dict[str, dict[str, Any]] = {}
        for point in POINTS:
            latencies[point.name], forecast = await measure(client, url, point, repeats)
            forecasts[point.name] = forecast.body

        burst_start = time.perf_counter()
        burst = await asyncio.gather(*(get(client, url, point_params(p, True)) for p in POINTS))
        burst_seconds = time.perf_counter() - burst_start

        runs = await open_meteo_runs(client)
        theirs: dict[str, Timed] = {}
        their_heights: dict[str, float | None] = {}
        for point in POINTS:
            ours = forecasts[point.name]
            theirs[point.name] = await get(
                client, OPEN_METEO_URL, open_meteo_params(point, ours["point"]["elevation_m"], list(VARIABLES.values()))
            )
            if theirs[point.name].status != 200:
                raise click.ClickException(f"{point.name}: Open-Meteo answered {theirs[point.name].body}")
            # Open-Meteo's own height for the point (its 90 m DEM), for the elevation table
            own = await get(
                client, OPEN_METEO_URL, open_meteo_params(point, None, ["temperature_2m"]) | {"forecast_days": 1}
            )
            their_heights[point.name] = own.body.get("elevation")

    click.echo(f"/fx/point at {base_url}: milliseconds, median/max of {repeats}; sizes on the wire (gzip)\n")
    click.echo(
        table(
            ["point", "first", "plain", "interpolated", "plain KB", "interp. KB", "Open-Meteo"],
            [
                [
                    name,
                    f"{lat.first * 1000:.0f}",
                    ms(lat.repeats),
                    ms(lat.interpolated),
                    f"{lat.wire_bytes / 1000:.0f}",
                    f"{lat.interpolated_wire_bytes / 1000:.0f}",
                    f"{theirs[name].seconds * 1000:.0f}",
                ]
                for name, lat in latencies.items()
            ],
        )
    )
    slowest = max(t.seconds for t in burst)
    click.echo(
        f"\nAll {len(POINTS)} points at once (interpolated): {burst_seconds * 1000:.0f} ms in all, "
        f"slowest {slowest * 1000:.0f} ms; statuses {sorted({t.status for t in burst})}"
    )

    click.echo("\nElevations (m): the point, each side's DEM, and the model cells ours adjusts from\n")
    model_ids = [m for m in MODELS if any(m in f["models"] for f in forecasts.values())]
    click.echo(
        table(
            ["point", "ours", "source", "Open-Meteo", *model_ids],
            [
                [
                    point.name,
                    f"{forecasts[point.name]['point']['elevation_m']:.0f}",
                    str(forecasts[point.name]["point"]["elevation_source"]),
                    f"{their_heights[point.name]:.0f}" if their_heights[point.name] is not None else "-",
                    *(_cell(forecasts[point.name], m) for m in model_ids),
                ]
                for point in POINTS
            ],
        )
    )
    missing = {name: f["missing"] for name, f in forecasts.items() if f["missing"]}
    if missing:
        click.echo(f"\nMissing models: {missing}")

    click.echo("\nRuns (UTC): ours (the newest archived) and Open-Meteo's newest\n")
    any_forecast = forecasts[POINTS[0].name]
    click.echo(
        table(
            ["model", "ours", "Open-Meteo"],
            [
                [
                    m,
                    _hour(any_forecast["models"].get(m, {}).get("init_time")),
                    _hour(runs[m]),
                ]
                for m in model_ids
            ],
        )
    )

    results = {p.name: comparisons(forecasts[p.name], theirs[p.name].body, runs, hours) for p in POINTS}
    click.echo(
        f"\nOurs minus Open-Meteo, at our DEM's elevation, over {hours} h from the later run's start: "
        "bias / mean absolute difference (precipitation: our total / theirs, mm)"
    )
    for variable in VARIABLES:
        rows = []
        for model in model_ids:
            cells = [_comparison(variable, results[p.name].get(model, {}).get(variable)) for p in POINTS]
            if any(c != "-" for c in cells):
                rows.append([model, *cells])
        if rows:
            units = next(
                (f["variables"][variable]["units"] for f in forecasts.values() if variable in f["variables"]), ""
            )
            click.echo(f"\n{variable} ({units})\n")
            click.echo(table(["model", *(p.name for p in POINTS)], rows))

    if json_path is not None:
        json_path.write_text(
            json.dumps(
                {
                    "base_url": base_url,
                    "measured_at": datetime.now(UTC).isoformat(),
                    "points": [{"name": p.name, "lat": p.lat, "lon": p.lon, "note": p.note} for p in POINTS],
                    "latency_s": {name: asdict(lat) for name, lat in latencies.items()},
                    "open_meteo_runs": {m: r.isoformat() if r else None for m, r in runs.items()},
                    "comparisons": {
                        name: {m: {v: asdict(c) for v, c in got.items()} for m, got in result.items()}
                        for name, result in results.items()
                    },
                },
                indent=2,
            )
        )
        click.echo(f"\nWrote {json_path}")


def _cell(forecast: dict[str, Any], model: str) -> str:
    info = forecast["models"].get(model)
    if info is None:
        return "-"
    height = info["cell"]["elevation_m"]
    return "?" if height is None else f"{height:.0f}"


def _hour(when: str | datetime | None) -> str:
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    return when.strftime("%m-%d %HZ") if when else "-"


def _comparison(variable: str, got: Comparison | None) -> str:
    if got is None:
        return "-"
    if variable in SUMS:
        return f"{got.ours_mean:.1f} / {got.theirs_mean:.1f}"
    digits = DIGITS.get(variable, 1)
    return f"{number(got.bias, digits)} / {got.mae:.{digits}f}"


if __name__ == "__main__":
    main()
