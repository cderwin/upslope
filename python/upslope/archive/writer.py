"""Writing one archived run as a Zarr v3 store on local disk, ready to upload.

```
<YYYYMMDDTHH>.zarr/
  zarr.json            group attributes (model, init time, source, config fingerprint) and consolidated metadata
  lead_time            hours since the run started (int32)
  init_time            the run's start (int64 seconds since 1970, a CF time)
  y, x                 cell-centre coordinates in the grid's CRS (y north to south)
  spatial_ref          the grid's CRS and transform (CF grid mapping, GDAL's GeoTransform)
  <variable>           (lead_time, y, x) int16 steps, CF scale_factor/add_offset, -32768 for no data
```

Variables are chunked lead times x rows x cols (the config's chunk shape), and the chunks are grouped into
shards (one stored object each) covering every lead time and up to the config's shard size in rows and cols. A
point's series and a map of one lead time each read a few chunks of one shard.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from attrs import define, field

from upslope.archive.config import ArchiveVariable, ChunkShape, ShardShape
from upslope.archive.grid import RegionGrid
from upslope.raster import VALUES_NODATA, quantize

if TYPE_CHECKING:
    import zarr
    from zarr.core.common import JSON

# What each kind of variable says about its time axis, as a CF cell_methods
_CELL_METHODS = {"sum": "lead_time: sum", "mean": "lead_time: mean"}
# Format of the layout this module writes, stored with each run so readers can tell layouts apart
LAYOUT_VERSION = 1


def _axis_attributes(crs: str, axis: str) -> dict:
    """CF attributes of a grid coordinate: latitude and longitude on a plain geographic grid, else projected
    coordinates (metres, or degrees on a rotated-pole grid)."""
    from pyproj import CRS

    parsed = CRS.from_wkt(crs)
    if parsed.is_geographic and not parsed.is_derived:
        return {"standard_name": "longitude", "units": "degrees_east"} if axis == "x" else {"standard_name": "latitude", "units": "degrees_north"}
    return {"standard_name": f"projection_{axis}_coordinate", "units": "degrees" if parsed.is_geographic else "m"}


def _round_up(size: int, multiple: int) -> int:
    return -(-size // multiple) * multiple


@define
class RunWriter:
    """Builds one run's store variable by variable, so only one variable is held in memory at a time.

    Attributes:
        path: Folder of the store (`<init>.zarr`); created, and replaced if it exists.
        grid: The region's grid.
        leads: The run's lead times, in hours.
        chunks: Inner chunk shape.
        shards: Shard size in grid cells.
        attributes: Group attributes (the model, its source, the config fingerprint, ...).
        initialized: When the run started.
    """

    path: Path
    grid: RegionGrid
    leads: list[int]
    chunks: ChunkShape
    shards: ShardShape
    initialized: datetime
    attributes: dict = field(factory=dict)
    _group: "zarr.Group" = field(init=False)

    def __attrs_post_init__(self) -> None:
        import zarr
        import zarr.storage

        initialized = self.initialized.astimezone(UTC)
        self._group = zarr.open_group(
            zarr.storage.LocalStore(self.path), mode="w", zarr_format=3,
            attributes={
                **self.attributes,
                "init_time": initialized.isoformat().replace("+00:00", "Z"),
                "layout_version": LAYOUT_VERSION,
                "Conventions": "CF-1.10",
            },
        )
        group = self._group
        group.create_array("lead_time", data=np.asarray(self.leads, dtype=np.int32), dimension_names=["lead_time"],
                           attributes={"units": "hours", "long_name": "time since the run started"})
        group.create_array("init_time", data=np.array(int(initialized.timestamp()), dtype=np.int64), dimension_names=[],
                           attributes={"units": "seconds since 1970-01-01T00:00:00Z", "calendar": "proleptic_gregorian",
                                       "long_name": "run initialization time"})
        group.create_array("y", data=self.grid.y, dimension_names=["y"], attributes=_axis_attributes(self.grid.crs, "y"))
        group.create_array("x", data=self.grid.x, dimension_names=["x"], attributes=_axis_attributes(self.grid.crs, "x"))
        group.create_array("spatial_ref", data=np.array(0, dtype=np.int32), dimension_names=[],
                           attributes={"crs_wkt": self.grid.crs, "spatial_ref": self.grid.crs, "GeoTransform": self.grid.geo_transform})

    def shape(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """(inner chunk, shard) shapes of every variable."""
        rows, cols = self.grid.shape
        chunk = (self.chunks.leads or 1, self.chunks.rows or 1, self.chunks.cols or 1)
        shard = (
            _round_up(len(self.leads), chunk[0]),
            min(self.shards.rows or 1, _round_up(rows, chunk[1])),
            min(self.shards.cols or 1, _round_up(cols, chunk[2])),
        )
        return chunk, shard

    def write(self, variable: ArchiveVariable, values: np.ndarray, source: str) -> None:
        """Store a standard variable.

        Args:
            variable: The variable.
            values: Shaped (lead_time, y, x), in the variable's units, NaN where there's no data. Values beyond the
                variable's `values` range are stored as its min or max.
            source: Where it came from (upstream fields and derivation), for the array's attributes.
        """
        from zarr.codecs import BloscCodec

        expected = (len(self.leads), *self.grid.shape)
        if values.shape != expected:
            raise ValueError(f"{variable.id} is shaped {values.shape}, not {expected}")
        quantization = variable.values
        chunk, shard = self.shape()
        attributes: dict[str, JSON] = {
            "long_name": variable.name,
            "units": variable.units,
            "scale_factor": quantization.step,
            "add_offset": quantization.offset,
            "_FillValue": VALUES_NODATA,
            "grid_mapping": "spatial_ref",
            "coordinates": "init_time spatial_ref",
            "source": source,
        }
        if variable.kind in _CELL_METHODS:
            attributes["cell_methods"] = _CELL_METHODS[variable.kind]
        array = self._group.create_array(
            variable.id, shape=expected, dtype="int16", chunks=chunk, shards=shard, fill_value=VALUES_NODATA,
            compressors=BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),
            dimension_names=["lead_time", "y", "x"], attributes=attributes,
        )
        # values beyond the variable's range are clamped to it, e.g. an upstream's small negative precipitation
        clamped = np.clip(values, quantization.min, quantization.max)
        array[:] = quantize(clamped, quantization.step, quantization.offset)

    def finish(self) -> None:
        """Consolidate the metadata into the root `zarr.json`, so a reader opens the run with one request."""
        import zarr

        zarr.consolidate_metadata(self._group.store)
