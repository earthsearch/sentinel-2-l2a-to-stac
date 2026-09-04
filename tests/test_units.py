"""Unit tests for individual task components.

Sections
--------
download        read_href + the three-file download block in process()
is_newer        is_newer_than_existing STAC-API gate
update_item     Earth Search override assertions (update_item)
make_cogs       make_cogs_for_item branching + cogify/write_cog pipeline

All tests are local-only: no network, no live S3.  The session-wide
``_stub_stac_api`` fixture in conftest.py intercepts every STAC-API request;
read_href / stac_asset.blocking are monkeypatched where needed.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import rasterio
import requests_mock as requests_mock_module
import stac_asset.blocking
from botocore.exceptions import ClientError
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from rasterio.transform import from_bounds
from returns.result import Failure, Success
from stactask.exceptions import InvalidInput

import sentinel_2_l2a_to_stac.task as task_module
from sentinel_2_l2a_to_stac.task import Sentinel2ToStac, _set_asset_owners, cogify


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _minimal_task(payload_id: str = "test-unit") -> Sentinel2ToStac:
    """Minimal task instance — only needs metadata_href to pass validate()."""
    return Sentinel2ToStac(
        {
            "id": payload_id,
            "metadata_href": "s3://sentinel-s2-l2a/tiles/19/T/DJ/2023/4/19/0/metadata.xml",
        },
        upload=False,
    )


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

_S3_DIR = "s3://sentinel-s2-l2a/tiles/35/M/PP/2023/5/27/0"
_METADATA_HREF = f"{_S3_DIR}/metadata.xml"
_PRODUCT_PATH = "products/2023/5/27/S2A_MSIL2A_EXAMPLE"

_TILEINFO_BYTES = json.dumps({"productPath": _PRODUCT_PATH}).encode()
_GRANULE_BYTES = b"<granule-metadata/>"
_PRODUCT_BYTES = b"<product-metadata/>"

_EXPECTED_HREFS = {
    f"{_S3_DIR}/tileInfo.json": _TILEINFO_BYTES,
    f"s3://sentinel-s2-l2a/{_PRODUCT_PATH}/metadata.xml": _PRODUCT_BYTES,
    f"{_S3_DIR}/metadata.xml": _GRANULE_BYTES,
}


def _make_download_task(tmp_path: Path, metadata_href: str = _METADATA_HREF) -> Sentinel2ToStac:
    payload = {
        "metadata_href": metadata_href,
        "create_cogs": False,
        "process": [
            {
                "id": "collection-0/workflow-workflow-1/item-1",
                "workflow": "workflow-1",
                "output_options": {"collections": {"collection-1": ".*"}},
                "tasks": {"sentinel-2-l2a-to-stac": {}},
            }
        ],
    }
    return Sentinel2ToStac(payload, workdir=tmp_path, upload=False)


def _fake_read_href_factory(calls: list[str]) -> Any:
    def _fake(self: Sentinel2ToStac, href: str) -> bytes:
        calls.append(href)
        return _EXPECTED_HREFS[href]
    return _fake


class _StubItem:
    """Minimal stand-in so process() can pass the is_newer and fileinfo stages.

    An empty `assets` dict lets add_fileinfo_to_local_assets iterate to a no-op.
    """

    id = "stub-item"
    collection_id = "stub-collection"
    assets: ClassVar[dict[str, Any]] = {}
    stac_version = "1.1.0"
    stac_extensions = "mock"

    def to_dict(self) -> dict[str, str]:
        return {"id": "stub-item"}


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize create_item + update_item + add_storage_schemes so download tests stay focused.

    The canned bytes above are not real Sentinel-2 metadata; those stages are
    covered by the update_item / storage tests below against genuine local metadata.
    """
    monkeypatch.setattr(task_module, "create_item", lambda _workdir: _StubItem())
    monkeypatch.setattr(
        Sentinel2ToStac, "update_item", lambda self, item, s3_path: item
    )
    monkeypatch.setattr(
        Sentinel2ToStac, "add_storage_schemes", lambda self, item: item
    )


def test_download_fetches_all_three_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fake_read_href_factory(calls))
    _stub_pipeline(monkeypatch)
    task = _make_download_task(tmp_path)

    task.process()

    assert task.tileinfo_path.read_bytes() == _TILEINFO_BYTES
    assert task.granule_metadata_xml_path.read_bytes() == _GRANULE_BYTES
    assert task.product_metadata_xml_path.read_bytes() == _PRODUCT_BYTES
    # Product metadata is fetched from the productPath-derived href, not next
    # to the granule metadata — the non-obvious behavior this port preserves.
    assert f"s3://sentinel-s2-l2a/{_PRODUCT_PATH}/metadata.xml" in calls
    assert set(calls) == set(_EXPECTED_HREFS)


def test_existing_files_are_not_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tileInfo.json").write_bytes(_TILEINFO_BYTES)
    (tmp_path / "metadata.xml").write_bytes(_GRANULE_BYTES)
    (tmp_path / "product_metadata.xml").write_bytes(_PRODUCT_BYTES)

    calls: list[str] = []
    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fake_read_href_factory(calls))
    _stub_pipeline(monkeypatch)
    task = _make_download_task(tmp_path)

    task.process()

    assert calls == []


def test_corrupted_tileinfo_raises_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Sentinel2ToStac, "read_href", lambda self, href: b"not valid json{"
    )
    with pytest.raises(InvalidInput, match=r"Corrupted tileInfo\.json"):
        _make_download_task(tmp_path).process()


def test_read_href_translates_nosuchkey_to_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(href: str, config: Any = None, clients: Any = None) -> bytes:
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
        )

    monkeypatch.setattr(stac_asset.blocking, "read_href", _raise)
    with pytest.raises(InvalidInput, match="Failed fetching href"):
        _make_download_task(tmp_path).read_href("s3://sentinel-s2-l2a/does/not/exist.json")


def test_read_href_reraises_other_client_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(href: str, config: Any = None, clients: Any = None) -> bytes:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetObject"
        )

    monkeypatch.setattr(stac_asset.blocking, "read_href", _raise)
    with pytest.raises(ClientError):
        _make_download_task(tmp_path).read_href("s3://sentinel-s2-l2a/denied.json")


# ---------------------------------------------------------------------------
# is_newer_than_existing
# ---------------------------------------------------------------------------

_ITEM_URL = (
    "https://earth-search.aws.element84.com/v1"
    "/collections/sentinel-2-c1-l2a/items/S2A_T19TDJ_20230419T153818_L2A"
)
_BASELINE_GEN_TIME = "2023-04-19T22:08:59.000000Z"


def _check_is_newer(
    item: Item,
    *,
    status_code: int = 200,
    body: dict[str, Any] | None = None,
) -> Any:
    with requests_mock_module.Mocker() as m:
        m.get(_ITEM_URL, status_code=status_code, text=json.dumps(body))
        return _minimal_task("regression-is-newer").is_newer_than_existing(item)


def test_server_error_is_failure(baseline_item_dict: dict[str, Any]) -> None:
    item = Item.from_dict(baseline_item_dict)
    result = _check_is_newer(item, status_code=500)
    assert isinstance(result, Failure)
    assert isinstance(result.failure(), Exception)


def test_not_found_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    item = Item.from_dict(baseline_item_dict)
    assert _check_is_newer(item, status_code=404) == Success(True)


def test_empty_body_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    # 200 with no generation_time defaults existing to "" <= created → proceed.
    item = Item.from_dict(baseline_item_dict)
    assert _check_is_newer(item, body={}) == Success(True)


def test_existing_older_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": "2020-03-27T06:15:34Z"}}
    assert _check_is_newer(item, body=body) == Success(True)


def test_existing_same_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    # Equal generation_time still proceeds (<=), matching legacy behavior.
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": _BASELINE_GEN_TIME}}
    assert _check_is_newer(item, body=body) == Success(True)


def test_existing_newer_skips(baseline_item_dict: dict[str, Any]) -> None:
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": "2026-03-27T06:15:34Z"}}
    assert _check_is_newer(item, body=body) == Success(False)


# ---------------------------------------------------------------------------
# update_item
# ---------------------------------------------------------------------------

def test_collection_and_payload_id(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert item["collection"] == "sentinel-2-c1-l2a"
    assert item["properties"]["earthsearch:payload_id"] == (
        "roda-sentinel-2-l2a/workflow-sentinel-2-l2a-to-stac/"
        "tiles-19-T-DJ-2026-8-23-0"
    )


def test_providers_and_license_dropped(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert "providers" not in item["properties"]
    assert [link for link in item["links"] if link["rel"] == "license"] == []


def test_via_link_added(baseline_item_dict: dict[str, Any]) -> None:
    via = [link for link in baseline_item_dict["links"] if link["rel"] == "via"]
    assert len(via) == 1
    assert via[0]["href"] == (
        "s3://sentinel-s2-l2a/tiles/19/T/DJ/2026/8/23/0/metadata.xml"
    )
    assert via[0]["type"] == "application/xml"
    assert via[0]["title"] == "Granule Metadata in Sinergize RODA Archive"


def test_storage_schemes_and_refs(baseline_item_dict: dict[str, Any]) -> None:
    # baseline runs with create_cogs=False, upload=False:
    #   - data assets keep their RODA S3 hrefs → "roda" scheme
    #   - metadata assets keep local workdir paths → "local" placeholder scheme
    #   - no "earthsearch" scheme because nothing was uploaded
    item = baseline_item_dict
    assert (
        "https://stac-extensions.github.io/storage/v2.0.0/schema.json"
        in item["stac_extensions"]
    )
    _S3_SCHEME = {
        "type": "aws-s3",
        "platform": "https://{bucket}.s3.{region}.amazonaws.com",
        "region": "us-west-2",
        "requester_pays": False,
    }
    assert item["properties"]["storage:schemes"] == {
        "roda": {**_S3_SCHEME, "bucket": "sentinel-s2-l2a"},
        "local": {**_S3_SCHEME, "bucket": "local"},
    }
    assert "storage:platform" not in item["properties"]
    assert "earthsearch" not in item["properties"]["storage:schemes"]
    for name, asset in item["assets"].items():
        expected_ref = (
            "roda" if asset["href"].startswith("s3://sentinel-s2-l2a/") else "local"
        )
        assert asset.get("storage:refs") == [expected_ref], name


def test_add_storage_schemes_classification() -> None:
    """add_storage_schemes assigns refs by href: RODA, Earth Search, or local."""
    from datetime import datetime, timezone

    from pystac import Asset, Item

    item = Item(
        id="test",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 1, 1, tzinfo=timezone.utc),
        properties={},
    )
    item.assets["roda_asset"] = Asset(href="s3://sentinel-s2-l2a/tiles/x/y.jp2")
    item.assets["es_asset"] = Asset(href="s3://earth-search-output/collection/x.tif")
    item.assets["local_asset"] = Asset(href="/tmp/workdir/metadata.xml")
    _set_asset_owners(item)

    result = _minimal_task("test-storage").add_storage_schemes(item)
    result_dict = result.to_dict()

    schemes = result_dict["properties"]["storage:schemes"]
    assert set(schemes.keys()) == {"roda", "earthsearch", "local"}
    assert schemes["roda"]["bucket"] == "sentinel-s2-l2a"
    assert schemes["earthsearch"]["bucket"] == "earth-search-output"
    assert schemes["local"]["bucket"] == "local"
    assert all(s["type"] == "aws-s3" for s in schemes.values())

    assets = result_dict["assets"]
    assert assets["roda_asset"]["storage:refs"] == ["roda"]
    assert assets["es_asset"]["storage:refs"] == ["earthsearch"]
    assert assets["local_asset"]["storage:refs"] == ["local"]

    assert any("storage" in ext for ext in result_dict["stac_extensions"])


def test_earthsearch_storage_scheme_after_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verify that assets uploaded to the Earth Search bucket get the "earthsearch"
    # scheme and ref — a path the baseline_item_dict fixture can't reach because
    # it runs with upload=False.
    ES_BUCKET = "earth-search-output"

    def _fake_upload(self: Sentinel2ToStac, item: Item, asset_keys: list[str]) -> Item:
        for key in asset_keys:
            fname = Path(item.assets[key].href).name
            item.assets[key].href = (
                f"s3://{ES_BUCKET}/sentinel-2-c1-l2a/S2A_TEST/{fname}"
            )
        return item

    monkeypatch.setattr(Sentinel2ToStac, "upload_item_assets_to_s3", _fake_upload)

    local_file = tmp_path / "B01.tif"
    local_file.write_bytes(b"fake")
    item = Item(
        id="S2A_TEST",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={},
    )
    item.set_collection("sentinel-2-c1-l2a")
    item.assets["blue"] = Asset(href=str(local_file), type=MediaType.COG)
    _set_asset_owners(item)

    task = Sentinel2ToStac(
        {"id": "test-es-scheme", "metadata_href": "s3://sentinel-s2-l2a/x"},
        workdir=tmp_path,
        upload=False,
    )
    local_keys = task.get_local_asset_keys(item)
    item = task.upload_item_assets_to_s3(item, local_keys)
    item = task.add_storage_schemes(item)

    result = item.to_dict()
    schemes = result["properties"]["storage:schemes"]

    assert set(schemes.keys()) == {"earthsearch"}
    assert schemes["earthsearch"]["bucket"] == ES_BUCKET
    assert schemes["earthsearch"]["type"] == "aws-s3"
    assert result["assets"]["blue"]["storage:refs"] == ["earthsearch"]


def test_scrub_block(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert "classification:classes" not in item["assets"]["scl"]["bands"][0]
    assert "eo:snow_cover" not in item["properties"]
    assert not any("classification" in ext for ext in item["stac_extensions"])


def test_asset_href_rewriting_and_proj_bbox(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert item["assets"]["blue"]["href"] == (
        "s3://sentinel-s2-l2a/tiles/19/T/DJ/2026/8/23/0/R10m/B02.jp2"
    )
    assert item["assets"]["granule_metadata"]["href"].endswith("/metadata.xml")
    assert not item["assets"]["granule_metadata"]["href"].startswith("s3://")
    assert "proj:bbox" not in item["assets"]["blue"]


# ---------------------------------------------------------------------------
# get_local_asset_keys
# ---------------------------------------------------------------------------

def test_get_local_asset_keys_selects_only_workdir_hrefs(tmp_path: Path) -> None:
    # Only assets whose href lives under the task workdir count as local. S3
    # hrefs and local paths *outside* the workdir are excluded. This is exactly
    # the key list handed to upload_item_assets_to_s3, so a misclassification
    # would upload (or skip) the wrong assets.
    task = Sentinel2ToStac(
        {"id": "test-local-keys", "metadata_href": "s3://sentinel-s2-l2a/x"},
        workdir=tmp_path,
        upload=False,
    )
    item = Item(
        id="test",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={},
    )
    item.assets["local_meta"] = Asset(href=str(tmp_path / "metadata.xml"))
    item.assets["local_cog"] = Asset(href=str(tmp_path / "R10m" / "B02.tif"))
    item.assets["roda"] = Asset(href="s3://sentinel-s2-l2a/tiles/x/B02.jp2")
    item.assets["outside"] = Asset(href="/some/other/dir/B01.tif")

    assert task.get_local_asset_keys(item) == ["local_meta", "local_cog"]


# ---------------------------------------------------------------------------
# make_cogs / cogify
# ---------------------------------------------------------------------------

def _item_with_baseline(
    baseline_item_dict: dict[str, Any],
    processing_baseline: str,
    grid_code: str | None = None,
) -> Item:
    item = Item.from_dict(baseline_item_dict)
    item.properties["s2:processing_baseline"] = processing_baseline
    if grid_code is not None:
        item.properties["grid:code"] = grid_code
    return item


def test_baseline_below_04_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "02.13")
    with pytest.raises(InvalidInput, match=r"only >= 05.00 \(not including 5.09\) is supported"):
        _minimal_task("test-make-cogs").make_cogs_for_item(item)


def test_baseline_03_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "03.99")
    with pytest.raises(InvalidInput, match=r"only >= 05.00 \(not including 5.09\) is supported"):
        _minimal_task("test-make-cogs").make_cogs_for_item(item)


def test_baseline_0509_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "05.09")
    with pytest.raises(InvalidInput, match=r"only >= 05.00 \(not including 5.09\) is supported"):
        _minimal_task("test-make-cogs").make_cogs_for_item(item)


def test_baseline_05_proceeds(
    baseline_item_dict: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item_with_baseline(baseline_item_dict, "05.00")
    monkeypatch.setattr(
        stac_asset.blocking, "download_item", lambda *args, **kwargs: item
    )
    monkeypatch.setattr(task_module, "cogify", lambda asset_name, asset: None)
    result = _minimal_task("test-make-cogs").make_cogs_for_item(item)
    assert isinstance(result, Item)


def _make_synthetic_raster(path: Path, *, width: int = 64, height: int = 64) -> None:
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
    # Use a GeoTIFF with a .jp2 extension — rasterio doesn't validate the
    # extension, and JP2 encode/decode adds unnecessary test complexity.
    src_path = tmp_path / "B01.jp2"
    _make_synthetic_raster(src_path, width=64, height=64)

    asset = Asset(href=str(src_path), media_type="image/jp2")
    # cogify() calls FileExtension.ext(asset, add_if_missing=True), which under
    # pystac 1.15.2 requires the asset to have an owner.
    owner = Item(
        id="test-item",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={},
    )
    owner.assets["B01"] =  asset
    asset.set_owner(owner)

    cogify("B01", asset)

    cog_path = Path(asset.href)
    assert cog_path.suffix == ".tif"
    assert asset.type == MediaType.COG

    fext = FileExtension.ext(asset)
    assert fext.checksum is not None
    assert fext.size is not None
    assert fext.size > 0

    with rasterio.open(str(cog_path)) as ds:
        assert ds.count == 1
        assert ds.width == 64
        assert ds.height == 64


def test_logger_prefixes_payload_id(caplog: pytest.LogCaptureFixture) -> None:
    # stactask 0.7.0 installs a TaskLoggerAdapter in Task.__init__ that
    # prefixes every log line with the payload id. Guard against a future
    # stactask change silently dropping the prefix from production logs.
    task = Sentinel2ToStac(
        {"id": "roda-payload-123", "metadata_href": "x"}, upload=False
    )
    with caplog.at_level(logging.INFO):
        task.logger.info("processing tile")
    assert "[roda-payload-123] processing tile" in caplog.text
