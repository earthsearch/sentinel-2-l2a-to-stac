"""Unit test for PR 3's create_item wiring.

Runs the full ``process()`` path against the ``create-item-baseline`` payload,
but pre-populates the workdir with the three source metadata files checked in
under ``tests/fixtures/source-metadata/`` so the PR 2 download block's
``.exists()`` guards skip every fetch — no network, no live S3.

Assertions are deliberately narrow (id / geometry / processing baseline /
stac_version), not a full ``out.json`` diff: ``update_item``, COGs, thumbnail,
and the STAC 1.1.0 field upgrade land in later PRs, so a full-output comparison
is not meaningful until PR 8/9.
"""

import json
import shutil
from pathlib import Path

from sentinel_2_l2a_to_stac.task import Sentinel2ToStac

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOAD = FIXTURES / "payloads" / "success" / "create-item-baseline" / "in.json"
SOURCE_METADATA = FIXTURES / "source-metadata" / "tiles-19-T-DJ-2023-4-19-0"
SOURCE_FILES = ("tileInfo.json", "metadata.xml", "product_metadata.xml")


def test_create_item_baseline(tmp_path: Path) -> None:
    # Seed the workdir so the download block is a no-op (fully local).
    for name in SOURCE_FILES:
        shutil.copy(SOURCE_METADATA / name, tmp_path / name)

    payload = json.loads(PAYLOAD.read_text())
    task = Sentinel2ToStac(payload, workdir=tmp_path, upload=False)

    result = task.process()

    assert len(result) == 1
    item = result[0]

    assert item["id"] == "S2A_T19TDJ_20230419T153818_L2A"
    assert item["properties"]["s2:processing_baseline"] == "05.09"
    assert item["geometry"]["type"] == "Polygon"
    assert item["properties"]["datetime"].startswith("2023-04-19T")

    # stactools-sentinel2 0.8.0 under pystac 1.15.2 stamps stac_version 1.1.0
    # (a pystac default) but still emits the OLD per-asset band shape
    # (eo:bands + raster:bands, no consolidated `bands`). Consolidating those
    # into the 1.1.0 `bands` field is PR 9's job; here we only assert what
    # create_item produces today.
    assert item["stac_version"] == "1.1.0"
    sample_asset = item["assets"]["blue"]
    assert "eo:bands" in sample_asset
    assert "raster:bands" in sample_asset
    assert "item_assets" not in item
