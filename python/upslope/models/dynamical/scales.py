"""Color scales for gridded forecast fields: display units, colors, and the palette images are drawn with.

Layers name a scale in `config/models.yaml`. Values are converted from the dataset's units to the scale's
before coloring, so one scale serves every model.
"""

import math
from collections.abc import Callable, Sequence

import numpy as np
from attrs import define

from upslope.models.manifest import Legend, LegendStop

# Palette index for pixels with no color: outside the data, or under a scale's first stop when that's see-through
NODATA_INDEX = 255


@define(frozen=True)
class Scale:
    """How to color one kind of field.

    Attributes:
        units: Display units, e.g. `°F`.
        stops: (value, CSS hex color) pairs in increasing value order; each color applies from its value up to the
            next stop's, and the last color to everything above it.
        decimals: Decimal places to show values with.
        transparent_below: Leave values under the first stop uncolored (e.g. no precipitation, clear sky);
            otherwise they get the first color.
        ticks: For a ramp sampled into fine stops (see `oklch_ramp`), the values its legend labels; it's then drawn
            as a gradient. None for a scale of distinct steps, whose legend labels every stop.
    """

    units: str
    stops: tuple[tuple[float, str], ...]
    decimals: int = 0
    transparent_below: bool = False
    ticks: tuple[float, ...] | None = None

    def __attrs_post_init__(self) -> None:
        values = [value for value, _ in self.stops]
        if not self.stops or len(self.stops) >= NODATA_INDEX or values != sorted(set(values)):
            raise ValueError(f"scale stops must be 1-{NODATA_INDEX - 1} increasing values")

    def legend(self) -> Legend:
        """The scale as the map's legend shows it."""
        stops = [LegendStop(value, color) for value, color in self.stops]
        if self.ticks is None:
            return Legend(self.units, stops, self.decimals)
        return Legend(self.units, stops, self.decimals, continuous=True, ticks=list(self.ticks))

    def colormap(self) -> dict[int, tuple[int, int, int, int]]:
        """Palette index -> RGBA for `colorize`'s indices."""
        return {i: (*_rgb(color), 255) for i, (_, color) in enumerate(self.stops)} | {NODATA_INDEX: (0, 0, 0, 0)}

    def colorize(self, values: np.ndarray) -> np.ndarray:
        """Palette indices (see `colormap`) for values in the scale's units; NaN gets `NODATA_INDEX`."""
        bounds = np.array([value for value, _ in self.stops])
        index = np.searchsorted(bounds, np.nan_to_num(values, nan=-np.inf), side="right") - 1
        below = index < 0
        index = np.clip(index, 0, len(bounds) - 1).astype(np.uint8)
        index[np.isnan(values) | (below & self.transparent_below)] = NODATA_INDEX
        return index

    def fingerprint_payload(self) -> dict:
        """What the scale contributes to a layer's fingerprint: anything that changes its frames."""
        return {"units": self.units, "stops": [list(stop) for stop in self.stops], "transparent_below": self.transparent_below}


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def oklch(lightness: float, chroma: float, hue: float) -> str:
    """An OKLCH color (the webapp's design tokens are OKLCH) as sRGB hex, clipped to the sRGB gamut."""
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )

    def encode(channel: float) -> int:
        channel = min(max(channel, 0.0), 1.0)
        srgb = 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055
        return round(srgb * 255)

    return "#" + "".join(f"{encode(channel):02x}" for channel in linear)


def oklch_ramp(
    anchors: Sequence[tuple[float, tuple[float, float, float]]], step: float
) -> tuple[tuple[float, str], ...]:
    """Stops every `step` along a ramp defined by (value, OKLCH) anchors, interpolated in OKLCH so the ramp's
    lightness changes evenly; the frames then look continuous. Hues interpolate the short way round."""
    stops = []
    value = anchors[0][0]
    while value <= anchors[-1][0] + 1e-9:
        upper = next(i for i, (at, _) in enumerate(anchors) if at >= value - 1e-9)
        lower = max(upper - 1, 0)
        (v0, (l0, c0, h0)), (v1, (l1, c1, h1)) = anchors[lower], anchors[upper]
        t = 0.0 if v1 == v0 else (value - v0) / (v1 - v0)
        dh = (h1 - h0 + 180) % 360 - 180
        stops.append((round(value, 6), oklch(l0 + t * (l1 - l0), c0 + t * (c1 - c0), (h0 + t * dh) % 360)))
        value += step
    return tuple(stops)


# Upslope's map ramps (the design palette; see webapp/STYLE.md), shared by both themes since they sit on imagery.
# Surface temperature, °F: a continuous ramp, freezing at the yellow.
TEMPERATURE_RAMP_F = (
    (-20, (0.38, 0.14, 300)),
    (-10, (0.5, 0.16, 265)),
    (0, (0.65, 0.13, 230)),
    (10, (0.76, 0.12, 190)),
    (20, (0.8, 0.15, 145)),
    (32, (0.88, 0.15, 105)),
    (50, (0.78, 0.15, 65)),
    (60, (0.64, 0.19, 35)),
    (70, (0.55, 0.2, 15)),
)
# Snowfall, inches: discrete bins. The design's six end at 18+; PNW storm totals go well past that, so three more
# bins (24, 36, 48+) carry on through magenta and crimson to a dark maroon.
SNOW_BINS_IN = (
    (0.1, (0.96, 0.015, 240)),
    (1, (0.85, 0.06, 235)),
    (3, (0.7, 0.12, 245)),
    (6, (0.56, 0.16, 265)),
    (12, (0.46, 0.17, 295)),
    (18, (0.4, 0.15, 325)),
    (24, (0.52, 0.2, 350)),
    (36, (0.42, 0.18, 5)),
    (48, (0.3, 0.12, 15)),
)


SCALES: dict[str, Scale] = {
    # every 2 °F along the ramp, so frames read as continuous; the legend labels the ramp's anchors
    "temperature_f": Scale(
        "°F", oklch_ramp(TEMPERATURE_RAMP_F, 2), ticks=tuple(value for value, _ in TEMPERATURE_RAMP_F)
    ),
    "wind_mph": Scale(
        "mph",
        (
            (0, "#f7fbff"), (5, "#c6dbef"), (10, "#9ecae1"), (15, "#6baed6"), (20, "#4292c6"), (25, "#2171b5"),
            (30, "#08519c"), (40, "#54278f"), (50, "#980043"), (60, "#dd1c77"), (75, "#f768a1"),
        ),
    ),
    "precip_in": Scale(
        "in",
        (
            (0.01, "#c7e9c0"), (0.05, "#a1d99b"), (0.1, "#74c476"), (0.25, "#31a354"), (0.5, "#006d2c"),
            (0.75, "#fed976"), (1, "#feb24c"), (1.5, "#fd8d3c"), (2, "#f03b20"), (3, "#bd0026"),
        ),
        decimals=2,
        transparent_below=True,
    ),
    "snow_in": Scale(
        "in",
        tuple((value, oklch(*color)) for value, color in SNOW_BINS_IN),
        decimals=1,
        transparent_below=True,
    ),
    "height_ft": Scale(
        "ft",
        (
            (0, "#440154"), (1000, "#482173"), (2000, "#433e85"), (3000, "#38588c"), (4000, "#2d708e"),
            (5000, "#25858e"), (6000, "#1e9b8a"), (7000, "#2ab07f"), (8000, "#52c569"), (9000, "#86d549"),
            (10000, "#c2df23"), (12000, "#fde725"),
        ),
    ),
    "cloud_pct": Scale(
        "%",
        ((10, "#525252"), (30, "#737373"), (50, "#969696"), (70, "#bdbdbd"), (90, "#f0f0f0")),
        transparent_below=True,
    ),
    "reflectivity_dbz": Scale(
        "dBZ",
        (
            (5, "#04e9e7"), (10, "#019ff4"), (15, "#0300f4"), (20, "#02fd02"), (25, "#01c501"), (30, "#008e00"),
            (35, "#fdf802"), (40, "#e5bc00"), (45, "#fd9500"), (50, "#fd0000"), (55, "#d40000"), (60, "#bc0000"),
            (65, "#f800fd"), (70, "#9854c6"), (75, "#fdfdfd"),
        ),
        transparent_below=True,
    ),
}

# Dataset units (CF `units` attribute) -> display units -> conversion
CONVERSIONS: dict[tuple[str, str], Callable[[np.ndarray], np.ndarray]] = {
    ("degree_Celsius", "°F"): lambda v: v * 9 / 5 + 32,
    ("m s-1", "mph"): lambda v: v * 2.2369363,
    ("kg m-2", "in"): lambda v: v / 25.4,  # 1 kg m-2 of water is 1 mm deep
    ("m", "in"): lambda v: v * 39.370079,
    ("m", "ft"): lambda v: v * 3.2808399,
    ("percent", "%"): lambda v: v,
    ("dBZ", "dBZ"): lambda v: v,
}


def convert(values: np.ndarray, units: str, scale: Scale) -> np.ndarray:
    """Convert values from a dataset's units to a scale's. Raises ValueError for an unknown pair."""
    try:
        return CONVERSIONS[(units, scale.units)](values)
    except KeyError:
        raise ValueError(f"can't convert {units!r} to {scale.units!r}") from None
