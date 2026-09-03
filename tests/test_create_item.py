"""PR 3 assertions: what create_item produces for the baseline tile.

Uses the session-scoped ``baseline_item_dict`` fixture (see conftest.py), which
runs the full local ``process()`` over the checked-in source metadata — no
network, no live S3. These assertions cover create_item-level facts that survive
update_item; the override-specific behavior is in test_update_item.py.
"""

from typing import Any


def test_create_item_identity(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert item["id"] == "S2A_T19TDJ_20230419T153818_L2A"
    assert item["properties"]["s2:processing_baseline"] == "05.09"
    assert item["geometry"]["type"] == "Polygon"
    assert item["properties"]["datetime"].startswith("2023-04-19T")


def test_create_item_stac_version_and_band_shape(
    baseline_item_dict: dict[str, Any]
) -> None:
    item = baseline_item_dict
    # stactools-sentinel2 0.8.0 under pystac 1.15.2 stamps stac_version 1.1.0
    # (a pystac default) but still emits the OLD per-asset band shape
    # (eo:bands + raster:bands, no consolidated `bands`). Consolidating those
    # into the 1.1.0 `bands` field is PR 9's job.
    assert item["stac_version"] == "1.1.0"
    sample_asset = item["assets"]["blue"]
    assert "bands" in sample_asset
    assert "eo:bands" not in sample_asset
    assert "raster:bands" not in sample_asset
    assert "item_assets" not in item
