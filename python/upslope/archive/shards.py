"""Reading one grid cell of an archived run's array straight from its shard files, without zarr.

A point forecast reads one cell of a dozen arrays per model. Through zarr-python each read costs far more than the
bytes it decodes: every inner chunk goes through zarr's async codec pipeline, so a point took 45-90 ms per model.
Reading the shard files directly (the index once at open, then one `pread` and one Blosc decode per inner chunk)
returns the same values about 3-10x faster.

Only the exact layout `upslope.archive.writer.RunWriter` writes is accepted: int16 arrays of (y, x) or
(lead_time, y, x), one `sharding_indexed` codec with inner chunks encoded `bytes` (little-endian) then `blosc`, the
index at the end of the shard encoded `bytes` (little-endian) then `crc32c`, and the default chunk key encoding
with "/". Shapes and Blosc's parameters may vary; anything else raises `UnsupportedLayout`, and the caller reads
that array through zarr instead.

A shard file is `<array>/c/<i>/<j>[/<k>]` and ends with its index: per inner chunk in C order, (offset, nbytes) as
little-endian uint64 (both 2^64-1 for a chunk never written, which reads as the fill value), followed by the
index's crc32c. A shard with no chunks written has no file.
"""

import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import google_crc32c
import numpy as np
from attrs import define
from numcodecs import Blosc

# Offset and size of an inner chunk that was never written
_MISSING = np.uint64(2**64 - 1)
_CRC_BYTES = 4
_BLOSC = Blosc()


class UnsupportedLayout(ValueError):
    """An array isn't stored in the one layout `ShardedArray` reads."""


class CorruptShard(ValueError):
    """A shard file doesn't match its own index (wrong checksum, chunks past its end, or a bad chunk)."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise UnsupportedLayout(message)


def _ints(value: Any, ndim: int, what: str) -> tuple[int, ...]:
    _expect(
        isinstance(value, list) and len(value) == ndim and all(isinstance(v, int) and v > 0 for v in value),
        f"{what} isn't {ndim} positive integers: {value!r}",
    )
    return tuple(value)


def _little_endian_bytes(codec: Any) -> bool:
    return codec == {"name": "bytes", "configuration": {"endian": "little"}}


@define(frozen=True)
class Layout:
    """An array's shape and chunking, from its Zarr metadata.

    Attributes:
        shape: The array's shape: (y, x) or (lead_time, y, x).
        shard_shape: Each shard's shape (the chunk grid); the last shards may extend past the array's edge.
        chunk_shape: Each inner chunk's shape; divides `shard_shape`.
        fill_value: The value of cells never written.
    """

    shape: tuple[int, ...]
    shard_shape: tuple[int, ...]
    chunk_shape: tuple[int, ...]
    fill_value: int

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> Self:
        """Validate an array's metadata (its `zarr.json`) against the layout this module reads.

        Raises:
            UnsupportedLayout: It's anything else.
        """
        _expect(metadata.get("zarr_format") == 3 and metadata.get("node_type") == "array", "not a Zarr v3 array")
        _expect(metadata.get("data_type") == "int16", f"data type {metadata.get('data_type')!r}, not int16")
        shape = metadata.get("shape")
        if not isinstance(shape, list) or len(shape) not in (2, 3):
            raise UnsupportedLayout(f"shape {shape!r} isn't 2-D or 3-D")
        ndim = len(shape)
        shape = _ints(shape, ndim, "the shape")
        _expect(not metadata.get("storage_transformers"), "storage transformers")
        grid = metadata.get("chunk_grid") or {}
        _expect(grid.get("name") == "regular", f"chunk grid {grid.get('name')!r}, not regular")
        shard_shape = _ints((grid.get("configuration") or {}).get("chunk_shape"), ndim, "the shard shape")
        encoding = metadata.get("chunk_key_encoding") or {}
        _expect(
            encoding.get("name") == "default" and (encoding.get("configuration") or {}).get("separator", "/") == "/",
            f"chunk key encoding {encoding!r}",
        )
        fill_value = metadata.get("fill_value")
        if not isinstance(fill_value, int) or isinstance(fill_value, bool):
            raise UnsupportedLayout(f"fill value {fill_value!r}")

        codecs = metadata.get("codecs")
        if not (
            isinstance(codecs, list)
            and len(codecs) == 1
            and isinstance(codecs[0], dict)
            and codecs[0].get("name") == "sharding_indexed"
        ):
            raise UnsupportedLayout("not a single sharding_indexed codec")
        sharding = codecs[0].get("configuration") or {}
        chunk_shape = _ints(sharding.get("chunk_shape"), ndim, "the inner chunk shape")
        _expect(
            all(s % c == 0 for s, c in zip(shard_shape, chunk_shape, strict=True)),
            f"inner chunks {chunk_shape} don't divide shards {shard_shape}",
        )
        inner = sharding.get("codecs")
        _expect(
            isinstance(inner, list)
            and len(inner) == 2
            and _little_endian_bytes(inner[0])
            and isinstance(inner[1], dict)
            and inner[1].get("name") == "blosc",
            f"inner codecs {inner!r}, not little-endian bytes then blosc",
        )
        index = sharding.get("index_codecs")
        _expect(
            isinstance(index, list)
            and len(index) == 2
            and _little_endian_bytes(index[0])
            and index[1] in ({"name": "crc32c"}, {"name": "crc32c", "configuration": {}}),
            f"index codecs {index!r}, not little-endian bytes then crc32c",
        )
        _expect(sharding.get("index_location", "end") == "end", "the shard index isn't at the end")
        return cls(shape, shard_shape, chunk_shape, fill_value)


def _read_index(path: Path, chunks_per_shard: tuple[int, ...]) -> np.ndarray | None:
    """A shard's index, shaped (*chunks_per_shard, 2) of (offset, nbytes); None if the shard has no file.

    Raises:
        CorruptShard: The index's checksum doesn't match, or it points past the data.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        size = os.fstat(fd).st_size
        index_bytes = 16 * math.prod(chunks_per_shard)
        if size < index_bytes + _CRC_BYTES:
            raise CorruptShard(f"{path} is {size} bytes, too small for its index")
        tail = os.pread(fd, index_bytes + _CRC_BYTES, size - index_bytes - _CRC_BYTES)
    finally:
        os.close(fd)
    body, stored = tail[:index_bytes], tail[index_bytes:]
    if google_crc32c.value(body).to_bytes(4, "little") != stored:
        raise CorruptShard(f"{path}'s index fails its checksum")
    index = np.frombuffer(body, dtype="<u8").reshape(*chunks_per_shard, 2)
    offsets, nbytes = index[..., 0], index[..., 1]
    missing = offsets == _MISSING
    if not np.array_equal(missing, nbytes == _MISSING):
        raise CorruptShard(f"{path}'s index has a chunk with only one of its offset and size missing")
    # compared as ints: offset + nbytes can't overflow uint64 for real chunks, but an unchecked sum could wrap
    ends = [int(o) + int(n) for o, n in zip(offsets[~missing], nbytes[~missing], strict=True)]
    if ends and max(ends) > size - index_bytes - _CRC_BYTES:
        raise CorruptShard(f"{path}'s index points past its data")
    return index


@define(frozen=True)
class ShardedArray:
    """One array of a run on local disk, read a cell at a time straight from its shard files.

    Every shard's index is read (and its checksum verified) when built, so a read is one `pread` and one Blosc
    decode per inner chunk the cell falls in. Reads open the shard file each time and hold no state, so they're
    safe from several threads at once, and a run's folder can be deleted once no read is in progress.

    Attributes:
        folder: The array's folder in the store.
        layout: Its shape and chunking.
        attributes: Its attributes (CF `scale_factor`, `add_offset`, `_FillValue`, ...).
        indexes: Shard grid position -> its index, shaped (*inner chunks per shard, 2) of (offset, nbytes), or None
            for a shard with no file (every cell the fill value).
    """

    folder: Path
    layout: Layout
    attributes: Mapping[str, Any]
    indexes: dict[tuple[int, ...], np.ndarray | None]

    @classmethod
    def from_metadata(cls, folder: Path, metadata: Mapping[str, Any]) -> Self:
        """Build a reader for the array stored in `folder`, reading every shard's index (blocking).

        Args:
            folder: The array's folder (`<run>.zarr/<name>`).
            metadata: The array's metadata, as in its `zarr.json` or the run's consolidated metadata.

        Raises:
            UnsupportedLayout: The array isn't stored in the layout this module reads.
            CorruptShard: A shard's index is damaged.
        """
        layout = Layout.from_metadata(metadata)
        shards = tuple(-(-n // s) for n, s in zip(layout.shape, layout.shard_shape, strict=True))
        per_shard = tuple(s // c for s, c in zip(layout.shard_shape, layout.chunk_shape, strict=True))
        indexes = {
            position: _read_index(folder.joinpath("c", *map(str, position)), per_shard)
            for position in np.ndindex(*shards)
        }
        return cls(folder, layout, dict(metadata.get("attributes") or {}), indexes)

    def series(self, row: int, col: int) -> np.ndarray:
        """A cell's raw int16 values over the first axis (lead time) of a 3-D array (blocking)."""
        if len(self.layout.shape) != 3:
            raise ValueError(f"{self.folder.name} isn't 3-D")
        return self._cell(row, col)

    def value(self, row: int, col: int) -> int:
        """A cell's raw int16 value in a 2-D array (blocking)."""
        if len(self.layout.shape) != 2:
            raise ValueError(f"{self.folder.name} isn't 2-D")
        return int(self._cell(row, col)[0])

    def _cell(self, row: int, col: int) -> np.ndarray:
        layout = self.layout
        rows, cols = layout.shape[-2:]
        if not (0 <= row < rows and 0 <= col < cols):
            raise IndexError(f"cell ({row}, {col}) is outside {self.folder.name}'s {rows} x {cols} grid")
        # a 2-D array reads as a 3-D one with a single lead, so both share one loop
        leading = layout.shape[0] if len(layout.shape) == 3 else 1
        shard_leads = layout.shard_shape[0] if len(layout.shape) == 3 else 1
        chunk = layout.chunk_shape if len(layout.shape) == 3 else (1, *layout.chunk_shape)
        shard_rows, shard_cols = layout.shard_shape[-2:]
        in_rows, in_cols = row % shard_rows, col % shard_cols
        chunk_row, chunk_col = in_rows // chunk[1], in_cols // chunk[2]
        at = (in_rows % chunk[1], in_cols % chunk[2])
        out = np.full(leading, layout.fill_value, dtype=np.int16)
        for shard_lead in range(-(-leading // shard_leads)):
            spatial = (row // shard_rows, col // shard_cols)
            position = (shard_lead, *spatial) if len(layout.shape) == 3 else spatial
            index = self.indexes[position]
            if index is None:
                continue
            index = index.reshape(-1, *index.shape[-3:])
            path = self.folder.joinpath("c", *map(str, position))
            fd = os.open(path, os.O_RDONLY)
            try:
                for k in range(index.shape[0]):
                    start = shard_lead * shard_leads + k * chunk[0]
                    if start >= leading:
                        break
                    offset, nbytes = index[k, chunk_row, chunk_col]
                    if offset == _MISSING:
                        continue
                    values = self._decode(os.pread(fd, int(nbytes), int(offset)), chunk, path)
                    stop = min(start + chunk[0], leading)
                    out[start:stop] = values[: stop - start, at[0], at[1]]
            finally:
                os.close(fd)
        return out

    @staticmethod
    def _decode(data: bytes, chunk: tuple[int, ...], path: Path) -> np.ndarray:
        decoded = _BLOSC.decode(data)
        values = np.frombuffer(decoded, dtype="<i2")
        if values.size != math.prod(chunk):
            raise CorruptShard(f"a chunk of {path} decodes to {values.size} values, not {math.prod(chunk)}")
        return values.reshape(chunk)
