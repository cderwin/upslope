"""Reading cells straight from shard files (`upslope.archive.shards`), checked against zarr's reads of the same
stores."""

import copy
import json
import logging
from pathlib import Path

import attrs
import numpy as np
import pytest

from tests.test_point import RUN, VARIABLES, grid, model
from upslope.archive.config import ChunkShape, ShardShape
from upslope.archive.point import OpenRun, read_run_point
from upslope.archive.shards import CorruptShard, ShardedArray, UnsupportedLayout
from upslope.archive.terrain import TERRAIN_ARRAY
from upslope.archive.writer import RunWriter

# 7 leads in chunks of 2 (the last chunk half past the end), and a 10 x 20 grid in 8 x 8 shards (2 x 3 of them)
LEADS = [0, 3, 6, 9, 12, 15, 18]
NAMES = ["temperature_2m", "precipitation"]


def write_run(folder: Path) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """A run with gaps: a chunk never written for some leads, a shard with no file, and missing terrain cells.

    Returns:
        Each variable's values and the terrain, as written.
    """
    rng = np.random.default_rng(1)
    temperature = rng.uniform(-20, 30, (len(LEADS), 10, 20)).astype(np.float32)
    temperature[2:4, 0:4, 0:4] = np.nan  # one inner chunk (leads 2-3, rows 0-3, cols 0-3) never written
    temperature[:, 8:10, 16:20] = np.nan  # the whole of shard (1, 2): no file
    temperature[5, 6, 7] = np.nan  # one cell missing in a chunk that's written
    precipitation = rng.uniform(0, 5, (len(LEADS), 10, 20)).astype(np.float32)
    precipitation[0] = np.nan
    terrain = rng.uniform(0, 3000, (10, 20)).astype(np.float32)
    terrain[0:4, 0:4] = np.nan
    writer = RunWriter(folder, grid(), LEADS, ChunkShape(2, 4, 4), ShardShape(8, 8), RUN, {"model": "fine"})
    writer.write(VARIABLES[0], temperature, "t")
    writer.write(VARIABLES[5], precipitation, "p")
    writer.write_terrain(terrain, "test")
    writer.finish()
    return {"temperature_2m": temperature, "precipitation": precipitation}, terrain


def metadata(folder: Path) -> dict:
    return json.loads((folder / "zarr.json").read_bytes())["consolidated_metadata"]["metadata"]


def open_zarr(folder: Path, name: str):
    import zarr

    return zarr.open_array(folder / name, mode="r")


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    write_run(tmp_path / "run.zarr")
    return tmp_path / "run.zarr"


def test_reads_every_cell_as_zarr_does(run_folder: Path) -> None:
    assert not (run_folder / "temperature_2m" / "c" / "0" / "1" / "2").exists()  # the empty shard has no file
    stored = metadata(run_folder)
    for name in NAMES:
        reader = ShardedArray.from_metadata(run_folder / name, stored[name])
        expected = np.asarray(open_zarr(run_folder, name)[:])
        assert len(reader.indexes) == 6
        for row in range(10):
            for col in range(20):
                np.testing.assert_array_equal(reader.series(row, col), expected[:, row, col], f"{name} {row} {col}")
    terrain = ShardedArray.from_metadata(run_folder / TERRAIN_ARRAY, stored[TERRAIN_ARRAY])
    expected = np.asarray(open_zarr(run_folder, TERRAIN_ARRAY)[:])
    assert [terrain.value(row, col) for row in range(10) for col in range(20)] == expected.ravel().tolist()
    with pytest.raises(IndexError):
        terrain.value(10, 0)


def test_reads_shards_split_along_the_lead_axis(tmp_path: Path) -> None:
    # RunWriter puts every lead in one shard; the reader doesn't rely on it
    import zarr
    from zarr.codecs import BloscCodec

    values = np.random.default_rng(2).integers(-1000, 1000, (7, 10, 20), dtype=np.int16)
    values[4:6, 0:4, 0:4] = -32768
    array = zarr.create_array(
        tmp_path / "a", shape=values.shape, dtype="int16", chunks=(2, 4, 4), shards=(4, 8, 8), fill_value=-32768,
        compressors=BloscCodec(cname="lz4", clevel=1, shuffle="bitshuffle"), dimension_names=["lead_time", "y", "x"],
    )  # fmt: skip
    array[:] = values
    reader = ShardedArray.from_metadata(tmp_path / "a", json.loads((tmp_path / "a" / "zarr.json").read_bytes()))
    assert len(reader.indexes) == 2 * 2 * 3
    for row in range(10):
        for col in range(20):
            np.testing.assert_array_equal(reader.series(row, col), values[:, row, col])


def test_runs_read_the_same_points_directly_and_through_zarr(run_folder: Path) -> None:
    m = model(variables=NAMES)
    fast = OpenRun.open(m, run_folder)
    assert set(fast.readers) == {*NAMES, TERRAIN_ARRAY}
    slow = attrs.evolve(fast, readers={})
    nan_cells = 0
    for row in range(10):
        for col in range(20):
            lon, lat = fast.grid.cell_centre(row, col)
            a, b = read_run_point(slow, [*NAMES, "snow_depth"], lat, lon), read_run_point(fast, NAMES, lat, lon)
            assert a is not None and b is not None and a.values.keys() == b.values.keys() == set(NAMES)
            for name in NAMES:
                np.testing.assert_array_equal(a.values[name], b.values[name])
                nan_cells += int(np.isnan(b.values[name]).any())
            assert a.terrain == b.terrain
    assert nan_cells > 0
    gap = read_run_point(fast, NAMES, *reversed(fast.grid.cell_centre(1, 1)))
    assert gap is not None and gap.terrain is None
    assert np.isnan(gap.values["temperature_2m"][2:4]).all() and np.isfinite(gap.values["temperature_2m"][4:]).all()


def test_a_damaged_index_fails_its_checksum(run_folder: Path) -> None:
    shard = run_folder / "temperature_2m" / "c" / "0" / "0" / "0"
    data = bytearray(shard.read_bytes())
    data[-10] ^= 0xFF
    shard.write_bytes(bytes(data))
    with pytest.raises(CorruptShard, match="checksum"):
        ShardedArray.from_metadata(run_folder / "temperature_2m", metadata(run_folder)["temperature_2m"])


def _edit(path: str, value):
    def edit(array: dict) -> None:
        *parents, last = path.split(".")
        target = array
        for key in parents:
            target = target[int(key)] if isinstance(target, list) else target[key]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return edit


@pytest.mark.parametrize(
    "edit",
    [
        _edit("data_type", "int32"),
        _edit("shape", [7, 10, 20, 1]),
        _edit("chunk_grid.name", "rectilinear"),
        _edit("chunk_key_encoding.configuration.separator", "."),
        _edit("chunk_key_encoding.name", "v2"),
        _edit("fill_value", "NaN"),
        _edit("storage_transformers", [{"name": "x"}]),
        _edit("codecs", [{"name": "bytes", "configuration": {"endian": "little"}}]),
        _edit("codecs.0.configuration.chunk_shape", [3, 4, 4]),
        _edit("codecs.0.configuration.codecs.1.name", "zstd"),
        _edit("codecs.0.configuration.codecs.0.configuration.endian", "big"),
        _edit("codecs.0.configuration.index_codecs", [{"name": "bytes", "configuration": {"endian": "little"}}]),
        _edit("codecs.0.configuration.index_location", "start"),
    ],
)
def test_only_the_written_layout_is_read(run_folder: Path, edit) -> None:
    stored = copy.deepcopy(metadata(run_folder)["temperature_2m"])
    ShardedArray.from_metadata(run_folder / "temperature_2m", stored)  # as written: fine
    edit(stored)
    with pytest.raises(UnsupportedLayout):
        ShardedArray.from_metadata(run_folder / "temperature_2m", stored)


def test_other_layouts_are_read_through_zarr(run_folder: Path, caplog: pytest.LogCaptureFixture) -> None:
    import zarr
    import zarr.storage
    from zarr.codecs import ZstdCodec

    # a variable compressed with zstd alone, not blosc
    dew_point = np.random.default_rng(3).integers(-200, 200, (len(LEADS), 10, 20), dtype=np.int16)
    group = zarr.open_group(zarr.storage.LocalStore(run_folder), mode="r+", zarr_format=3)
    array = group.create_array(
        "dew_point_2m", shape=dew_point.shape, dtype="int16", chunks=(2, 4, 4), shards=(8, 8, 8), fill_value=-32768,
        compressors=ZstdCodec(), dimension_names=["lead_time", "y", "x"],
        attributes={"scale_factor": 0.05, "add_offset": 0.0, "_FillValue": -32768},
    )  # fmt: skip
    array[:] = dew_point
    zarr.consolidate_metadata(group.store)

    with caplog.at_level(logging.WARNING, logger="upslope.archive.point"):
        run = OpenRun.open(model(variables=[*NAMES, "dew_point_2m"]), run_folder)
    assert "dew_point_2m" not in run.readers and set(NAMES) <= set(run.readers)
    assert "reading dew_point_2m through zarr" in caplog.text
    lon, lat = run.grid.cell_centre(5, 9)
    got = read_run_point(run, [*NAMES, "dew_point_2m"], lat, lon)
    assert got is not None
    np.testing.assert_allclose(got.values["dew_point_2m"], dew_point[:, 5, 9] * 0.05)
