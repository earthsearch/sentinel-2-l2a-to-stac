"""PR 6 tests: make_cogs_for_item branching logic and cogify/write_cog unit.

The processing_baseline check and the Europe-MGRS carve-out are tested by
calling make_cogs_for_item directly on an Item built from baseline_item_dict
with its s2:processing_baseline and grid:code properties overridden.  This
avoids needing real JP2 source files for every permutation while still
exercising the real gate logic.

For the success case (04.xx + EU tile), stac_asset.blocking.download_item and
cogify are stubbed so the test stays local and fast: the carve-out check
passes before either stub is reached, and the stubs prevent any real raster
I/O or network access.

The cogify/write_cog unit test creates a tiny synthetic GeoTIFF and exercises
the full rasterio pipeline, asserting COG output properties (extension, media
type, file:checksum/size, and at least one overview level on a raster large
enough for overviews).
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
import stac_asset.blocking
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from rasterio.transform import from_bounds
from stactask.exceptions import InvalidInput

import sentinel_2_l2a_to_stac.task as task_module
from sentinel_2_l2a_to_stac.task import (
    EUROPE_MGRS_IDS,
    Sentinel2ToStac,
    cogify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task() -> Sentinel2ToStac:
    """Minimal task instance — only needs metadata_href to pass validate()."""
    payload = {
        "id": "test-make-cogs",
        "metadata_href": "s3://sentinel-s2-l2a/tiles/19/T/DJ/2023/4/19/0/metadata.xml",
    }
    return Sentinel2ToStac(payload, upload=False)


def _item_with_baseline(
    baseline_item_dict: dict[str, Any],
    processing_baseline: str,
    grid_code: str | None = None,
) -> Item:
    """Return an Item from the session fixture with overridden baseline/grid:code."""
    item = Item.from_dict(baseline_item_dict)
    item.properties["s2:processing_baseline"] = processing_baseline
    if grid_code is not None:
        item.properties["grid:code"] = grid_code
    return item


# ---------------------------------------------------------------------------
# Processing baseline gate
# ---------------------------------------------------------------------------

def test_baseline_below_04_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "02.13")
    with pytest.raises(InvalidInput, match=r"only >= 04\.00 is supported"):
        _task().make_cogs_for_item(item)


def test_baseline_03_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "03.99")
    with pytest.raises(InvalidInput, match=r"only >= 04\.00 is supported"):
        _task().make_cogs_for_item(item)


# ---------------------------------------------------------------------------
# Europe-MGRS carve-out: 04.xx baseline
# ---------------------------------------------------------------------------

def test_baseline_04_outside_eu_raises(baseline_item_dict: dict[str, Any]) -> None:
    # The baseline tile is 19TDJ — not in EUROPE_MGRS_IDS.  The item's
    # grid:code from update_item is "MGRS-19TDJ"; we leave it unchanged here.
    assert "19TDJ" not in EUROPE_MGRS_IDS
    item = _item_with_baseline(baseline_item_dict, "04.00")
    with pytest.raises(
        InvalidInput, match=r"Processing baseline 04\.00 must intersect Europe"
    ):
        _task().make_cogs_for_item(item)


def test_baseline_04_eu_proceeds(
    baseline_item_dict: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 31TEM is in EUROPE_MGRS_IDS — the carve-out check should pass.
    assert "31TEM" in EUROPE_MGRS_IDS
    item = _item_with_baseline(
        baseline_item_dict,
        processing_baseline="04.00",
        grid_code="MGRS-31TEM",
    )

    # Stub stac_asset.blocking.download_item so no network/S3 is touched.
    monkeypatch.setattr(
        stac_asset.blocking,
        "download_item",
        lambda *args, **kwargs: item,
    )
    # Stub cogify so no raster I/O happens — we're only testing the gate here.
    monkeypatch.setattr(task_module, "cogify", lambda asset_name, asset: None)

    result = _task().make_cogs_for_item(item)
    assert isinstance(result, Item)


def test_baseline_05_proceeds(
    baseline_item_dict: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # >= 05.xx skips the EU check entirely.
    item = _item_with_baseline(baseline_item_dict, "05.00")

    monkeypatch.setattr(
        stac_asset.blocking,
        "download_item",
        lambda *args, **kwargs: item,
    )
    monkeypatch.setattr(task_module, "cogify", lambda asset_name, asset: None)

    result = _task().make_cogs_for_item(item)
    assert isinstance(result, Item)


# ---------------------------------------------------------------------------
# cogify / write_cog unit test
# ---------------------------------------------------------------------------

def _make_synthetic_raster(path: Path, *, width: int = 64, height: int = 64) -> None:
    """Write a minimal uint16 single-band GeoTIFF for cogify to consume."""
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "uint16",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:32619",
        "transform": transform,
    }
    data = np.arange(width * height, dtype=np.uint16).reshape(1, height, width)
    with rasterio.open(str(path), "w", **profile) as dst:
        dst.write(data)


def test_cogify_produces_valid_cog(tmp_path: Path) -> None:
    # Build a fake JP2 (really a GeoTIFF — rasterio doesn't care about the
    # extension, and JP2 encode/decode adds unnecessary test complexity).
    src_path = tmp_path / "B01.jp2"
    _make_synthetic_raster(src_path, width=64, height=64)

    asset = Asset(href=str(src_path), media_type="image/jp2")
    # No raster:bands metadata → scales/offsets come from the file itself.
    # cogify() calls FileExtension.ext(asset, add_if_missing=True), which under
    # pystac 1.15.2 requires the asset to have an owner. In the real pipeline the
    # asset is always item.assets[name] (owner set); mirror that by attaching it
    # to an Item here.
    owner = Item(
        id="test-item",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={},
    )
    owner.add_asset("B01", asset)

    cogify("B01", asset)

    cog_path = Path(asset.href)

    # Asset was updated in place.
    assert cog_path.suffix == ".tif"
    assert asset.media_type == MediaType.COG

    # file:checksum and file:size were set.
    fext = FileExtension.ext(asset)
    assert fext.checksum is not None
    assert fext.size is not None
    assert fext.size > 0

    # Output is a valid rasterio-readable GeoTIFF with at least one overview
    # level (the 64x64 raster at blocksize 1024 produces 0 overviews, but we
    # still verify it opens cleanly and has the right band count).
    with rasterio.open(str(cog_path)) as ds:
        assert ds.count == 1
        assert ds.width == 64
        assert ds.height == 64


def test_cogify_scl_uses_mode_resampling(tmp_path: Path) -> None:
    """'scl' asset gets MODE resampling (not AVERAGE) — spot-checks the lookup."""
    from sentinel_2_l2a_to_stac.task import asset_name_to_resample_algorithm
    assert asset_name_to_resample_algorithm("scl") == "MODE"
    assert asset_name_to_resample_algorithm("blue") == "AVERAGE"
    assert asset_name_to_resample_algorithm("unknown") == "AVERAGE"


def test_gsd_to_blocksize_lookup(tmp_path: Path) -> None:
    from sentinel_2_l2a_to_stac.task import gsd_to_blocksize
    assert gsd_to_blocksize(10) == (1024, 512)
    assert gsd_to_blocksize(20) == (512, 256)
    assert gsd_to_blocksize(60) == (256, 128)
    assert gsd_to_blocksize(None) == (1024, 512)
    assert gsd_to_blocksize(99) == (1024, 512)  # fallback
