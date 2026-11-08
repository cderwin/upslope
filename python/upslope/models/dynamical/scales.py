"""Color scales for gridded forecast fields: display units, colors, and the palette images are drawn with.

Layers name a scale in `config/models.yaml`. Values are converted from the dataset's units to the scale's
before coloring, so one scale serves every model.
"""

from collections.abc import Callable

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
    """

    units: str
    stops: tuple[tuple[float, str], ...]
    decimals: int = 0
    transparent_below: bool = False

    def __attrs_post_init__(self) -> None:
        values = [value for value, _ in self.stops]
        if not self.stops or len(self.stops) >= NODATA_INDEX or values != sorted(set(values)):
            raise ValueError(f"scale stops must be 1-{NODATA_INDEX - 1} increasing values")

    def legend(self) -> Legend:
        """The scale as the map's legend shows it."""
        return Legend(self.units, [LegendStop(value, color) for value, color in self.stops], self.decimals)

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


SCALES: dict[str, Scale] = {
    "temperature_f": Scale(
        "°F",
        (
            (-20, "#3b0f70"), (-10, "#5a2a9c"), (0, "#4060c8"), (10, "#2f8ad8"), (20, "#45b0e0"), (28, "#9bd4ee"),
            (32, "#d9f0d3"), (40, "#a6dba0"), (50, "#5aae61"), (60, "#d9ef8b"), (70, "#fee08b"), (80, "#fdae61"),
            (90, "#f46d43"), (100, "#d73027"), (110, "#a50026"),
        ),
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
        (
            (0.1, "#dbe9f6"), (1, "#bad6eb"), (2, "#89bedc"), (4, "#539ecd"), (6, "#2b7bba"), (8, "#0b559f"),
            (12, "#3f007d"), (18, "#6a51a3"), (24, "#9e9ac8"), (36, "#df65b0"), (48, "#e7298a"), (72, "#ce1256"),
            (96, "#91003f"),
        ),
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
