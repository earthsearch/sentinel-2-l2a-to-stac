"""Unit tests for individual task components.

Sections
--------
download        read_href + the three-file download block in process()
bucket/doc      bucket listing, existing-doc discovery, metadata resolution
reference path  create_cogs=False reference/update path
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
from sentinel_2_l2a_to_stac.cogify import cogify
from sentinel_2_l2a_to_stac.task import (
    ASSET_FILENAMES,
    THUMBNAIL_ASSET_NAME,
    Sentinel2ToStac,
    _prune_to_canonical_assets,
    _resolve_product_metadata_href,
    _set_asset_owners,
    find_existing_stac_doc_filename,
)

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


def _make_download_task(
    tmp_path: Path, metadata_href: str = _METADATA_HREF
) -> Sentinel2ToStac:
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
    """Neutralize create_item + update_item + add_storage_schemes so download tests stay
    focused.

    The canned bytes above are not real Sentinel-2 metadata; those stages are
    covered by the update_item / storage tests below against genuine local metadata.
    """
    monkeypatch.setattr(task_module, "create_item", lambda _workdir: _StubItem())
    monkeypatch.setattr(
        Sentinel2ToStac, "update_item", lambda self, item, s3_path: item
    )
    monkeypatch.setattr(Sentinel2ToStac, "add_storage_schemes", lambda self, item: item)
    monkeypatch.setattr(
        Sentinel2ToStac, "list_bucket_filenames", lambda self, s3_path: set()
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


def test_corrupted_tileinfo_raises_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        Sentinel2ToStac, "read_href", lambda self, href: b"not valid json{"
    )
    monkeypatch.setattr(
        Sentinel2ToStac, "list_bucket_filenames", lambda self, s3_path: set()
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
        _make_download_task(tmp_path).read_href(
            "s3://sentinel-s2-l2a/does/not/exist.json"
        )


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
# bucket listing / existing-doc discovery / product-metadata resolution
# ---------------------------------------------------------------------------


def _fake_s3_find(urls: list[str]) -> Any:
    def _fake(url: str, suffix: str = "") -> Any:
        return iter(urls)

    return _fake


def test_list_bucket_filenames_returns_basenames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_module.s3_client,
        "find",
        _fake_s3_find(
            [
                "s3://bucket/prefix/B02.tif",
                "s3://bucket/prefix/tileInfo.json",
                "s3://bucket/prefix/S2A_T19TDJ_L2A.json",
            ]
        ),
    )
    task = _minimal_task("test-list-bucket")
    assert task.list_bucket_filenames("s3://bucket/prefix") == {
        "B02.tif",
        "tileInfo.json",
        "S2A_T19TDJ_L2A.json",
    }


def test_find_existing_stac_doc_filename_ignores_roda_product_info() -> None:
    # productInfo.json is Sinergise's own RODA product-info file, present at
    # every RODA granule prefix -- it must never be mistaken for the doc.
    assert (
        find_existing_stac_doc_filename(
            {"tileInfo.json", "productInfo.json"}, "S2A_T19TDJ_L2A"
        )
        is None
    )


def test_load_existing_stac_doc_reads_via_read_href(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _fake_read_href(self: Sentinel2ToStac, href: str) -> bytes:
        calls.append(href)
        return json.dumps({"id": "S2A_T19TDJ_L2A"}).encode()

    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fake_read_href)
    task = _minimal_task("test-load-doc")
    doc = task.load_existing_stac_doc("s3://bucket/prefix", "S2A_T19TDJ_L2A.json")
    assert doc == {"id": "S2A_T19TDJ_L2A"}
    assert calls == ["s3://bucket/prefix/S2A_T19TDJ_L2A.json"]


def test_resolve_product_metadata_href_prefers_flat_layout() -> None:
    assert (
        _resolve_product_metadata_href(
            "s3://bucket/prefix",
            {"product_metadata.xml", "tileInfo.json"},
            "bucket",
            "products/2023/5/27/S2A_MSIL2A_EXAMPLE",
        )
        == "s3://bucket/prefix/product_metadata.xml"
    )


def test_resolve_product_metadata_href_falls_back_to_roda() -> None:
    assert (
        _resolve_product_metadata_href(
            "s3://bucket/prefix",
            {"tileInfo.json", "metadata.xml"},
            "sentinel-s2-l2a",
            "products/2023/5/27/S2A_MSIL2A_EXAMPLE",
        )
        == "s3://sentinel-s2-l2a/products/2023/5/27/S2A_MSIL2A_EXAMPLE/metadata.xml"
    )


def test_prune_to_canonical_assets_drops_m_suffixed_and_thumbnail(
    baseline_item_dict: dict[str, Any],
) -> None:
    item = Item.from_dict(baseline_item_dict)
    # Simulate create_item's raw, unpruned output plus a thumbnail asset.
    item.assets["red_20m"] = Asset(href="x", type="image/jp2")
    item.assets["visual_60m"] = Asset(href="x", type="image/jp2")
    item.assets["thumbnail"] = Asset(href="x", type="image/jpeg")
    _set_asset_owners(item)

    before_canonical_keys = {
        k for k in item.assets if not k.endswith("m") and k != "thumbnail"
    }

    result = _prune_to_canonical_assets(item)

    assert "red_20m" not in result.assets
    assert "visual_60m" not in result.assets
    assert "thumbnail" not in result.assets
    assert set(result.assets.keys()) == before_canonical_keys


def test_asset_filenames_map_matches_create_item_keys(
    baseline_item_dict: dict[str, Any],
) -> None:
    # ASSET_FILENAMES must cover exactly the 23 canonical keys create_item
    # produces after pruning (thumbnail is added manually in the reference
    # path, not produced by create_item, so it's expected here too).
    item = Item.from_dict(baseline_item_dict)
    canonical_keys = {k for k in item.assets if not k.endswith("m")}
    assert set(ASSET_FILENAMES.keys()) == canonical_keys | {"thumbnail"}


# ---------------------------------------------------------------------------
# reference path (create_cogs=False, existing doc present)
# ---------------------------------------------------------------------------


def _synthetic_item(keys: list[str]) -> Item:
    item = Item(
        id="test-item",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={},
    )
    for key in keys:
        item.assets[key] = Asset(href=f"placeholder-{key}", type="image/tiff")
    _set_asset_owners(item)
    return item


def test_apply_earthsearch_hrefs_rewrites_every_asset() -> None:
    item = _synthetic_item(["blue", "scl", "thumbnail"])
    result = _minimal_task("test-apply-hrefs").apply_earthsearch_hrefs(
        item, "s3://bucket/prefix"
    )
    assert result.assets["blue"].href == "s3://bucket/prefix/B02.tif"
    assert result.assets["scl"].href == "s3://bucket/prefix/SCL.tif"
    assert result.assets["thumbnail"].href == "s3://bucket/prefix/L2A_PVI.jpg"


def test_add_thumbnail_asset() -> None:
    item = _synthetic_item(["preview"])
    result = _minimal_task("test-add-thumbnail").add_thumbnail_asset(
        item, "s3://bucket/prefix"
    )
    thumb = result.assets["thumbnail"]
    assert thumb.href == "s3://bucket/prefix/L2A_PVI.jpg"
    assert thumb.roles == ["thumbnail"]
    assert thumb.type == MediaType.JPEG


def test_apply_reference_file_info_reuses_from_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"

    def _fail_if_called(self: Sentinel2ToStac, href: str) -> bytes:
        raise AssertionError(f"should not download when reusing: {href}")

    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fail_if_called)
    task = Sentinel2ToStac(
        {"id": "test-reuse", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    doc = {"assets": {"blue": {"file:size": 123, "file:checksum": "abc"}}}
    result = task.apply_reference_file_info(item, {"B02.tif"}, doc)

    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 123
    assert fext.checksum == "abc"


def test_apply_reference_file_info_downloads_when_missing_from_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    monkeypatch.setattr(
        Sentinel2ToStac, "read_href", lambda self, href: b"fake-cog-bytes"
    )
    task = Sentinel2ToStac(
        {"id": "test-download", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    result = task.apply_reference_file_info(item, {"B02.tif"}, {"assets": {}})

    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == len(b"fake-cog-bytes")
    assert fext.checksum is not None
    # The downloaded file is only needed to compute size/checksum.
    assert not (tmp_path / "B02.tif").exists()


def test_apply_reference_file_info_ignores_extra_doc_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    monkeypatch.setattr(Sentinel2ToStac, "read_href", lambda self, href: b"x")
    task = Sentinel2ToStac(
        {"id": "test-extra", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    doc = {
        "assets": {
            "blue": {"file:size": 1, "file:checksum": "x"},
            # "red" isn't one of item's own assets -- it must be ignored,
            # never consulted or otherwise acted on.
            "red": {"file:size": 999, "file:checksum": "should-not-be-used"},
        }
    }
    result = task.apply_reference_file_info(item, {"B02.tif"}, doc)

    assert "red" not in result.assets
    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 1
    assert fext.checksum == "x"


def test_apply_reference_file_info_raises_when_missing_from_bucket() -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    with pytest.raises(InvalidInput, match="not found in the bucket"):
        _minimal_task("test-missing-from-bucket").apply_reference_file_info(
            item, set(), {"assets": {}}
        )


_FULL_ITEM_ID = "S2A_T19TDJ_20230419T153818_L2A"
_FULL_ITEM_DOC_FILENAME = f"{_FULL_ITEM_ID}.json"


def _synthetic_full_item() -> Item:
    """An item shaped like update_item's output: every canonical asset key,
    plus what the freshness gate and update_item's collection lookup need."""
    item = Item(
        id=_FULL_ITEM_ID,
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 4, 19, tzinfo=timezone.utc),
        properties={"s2:generation_time": "2023-04-19T22:08:59.000000Z"},
    )
    item.set_collection("sentinel-2-c1-l2a")
    # create_item never produces a "thumbnail" asset -- only the reference
    # path (via add_thumbnail_asset) or the cog path (via make_thumbnail) do.
    for key, filename in ASSET_FILENAMES.items():
        if key == THUMBNAIL_ASSET_NAME:
            continue
        item.assets[key] = Asset(href=f"local-{filename}", type="image/tiff")
    _set_asset_owners(item)
    return item


def _stub_pipeline_with_item(monkeypatch: pytest.MonkeyPatch, item: Item) -> None:
    monkeypatch.setattr(task_module, "create_item", lambda _workdir: item)
    monkeypatch.setattr(
        Sentinel2ToStac, "update_item", lambda self, item, s3_path: item
    )
    monkeypatch.setattr(Sentinel2ToStac, "add_storage_schemes", lambda self, item: item)


def _stub_metadata_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        Sentinel2ToStac,
        "read_href",
        lambda self, href: (
            b'{"productPath": "p"}' if href.endswith("tileInfo.json") else b"<x/>"
        ),
    )


def test_process_takes_reference_path_when_doc_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: set(ASSET_FILENAMES.values()) | {_FULL_ITEM_DOC_FILENAME},
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "load_existing_stac_doc",
        lambda self, s3_path, filename: {"assets": {}},
    )

    result = _make_download_task(tmp_path).process()

    assert len(result) == 1
    out_item = result[0]
    assert out_item["assets"]["blue"]["href"].endswith("/B02.tif")
    assert "thumbnail" in out_item["assets"]
    assert out_item["assets"]["thumbnail"]["href"].endswith("/L2A_PVI.jpg")
    # No existing doc entries -> every asset is "missing from doc" -> downloaded.
    assert out_item["assets"]["blue"]["file:size"] == len(b"<x/>")


def test_process_reference_path_never_reuploads_existing_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: set(ASSET_FILENAMES.values()) | {_FULL_ITEM_DOC_FILENAME},
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "load_existing_stac_doc",
        lambda self, s3_path, filename: {"assets": {}},
    )

    upload_calls: list[list[str]] = []
    original_upload = Sentinel2ToStac.upload_item_assets_to_s3

    def _spy_upload(
        self: Sentinel2ToStac, item: Item, assets: list[str] | None = None
    ) -> Item:
        upload_calls.append(list(assets or []))
        return original_upload(self, item, assets)

    monkeypatch.setattr(Sentinel2ToStac, "upload_item_assets_to_s3", _spy_upload)

    result = _make_download_task(tmp_path).process()

    assert len(result) == 1
    assert upload_calls == [[]]


def test_process_takes_legacy_path_when_no_doc_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(
        Sentinel2ToStac, "list_bucket_filenames", lambda self, s3_path: set()
    )

    result = _make_download_task(tmp_path).process()

    assert len(result) == 1
    out_item = result[0]
    # Legacy pass-through: hrefs untouched by the reference-path map, no
    # thumbnail added, no forced file info.
    assert out_item["assets"]["blue"]["href"] == "local-B02.tif"
    assert "thumbnail" not in out_item["assets"]
    assert "file:size" not in out_item["assets"]["blue"]


def test_process_raises_when_reference_asset_missing_from_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    bucket_filenames = set(ASSET_FILENAMES.values()) - {"B02.tif"}
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: bucket_filenames | {_FULL_ITEM_DOC_FILENAME},
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "load_existing_stac_doc",
        lambda self, s3_path, filename: {"assets": {}},
    )

    with pytest.raises(InvalidInput, match="not found in the bucket"):
        _make_download_task(tmp_path).process()


def test_process_create_cogs_true_noops_with_stac_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # create_cogs=True must never consult the bucket-presence guard -- an
    # empty bucket (or one missing every expected COG) is exactly the normal
    # case for this path, since it's the one generating those COGs.
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(Sentinel2ToStac, "make_cogs_for_item", lambda self, item: item)
    monkeypatch.setattr(task_module, "make_thumbnail", lambda item: item)
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: {_FULL_ITEM_DOC_FILENAME},
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "load_existing_stac_doc",
        lambda self, s3_path, filename: {"assets": {}},
    )

    payload = {
        "metadata_href": _METADATA_HREF,
        "create_cogs": True,
        "process": [
            {
                "id": "collection-0/workflow-workflow-1/item-1",
                "workflow": "workflow-1",
                "output_options": {"collections": {"collection-1": ".*"}},
                "tasks": {"sentinel-2-l2a-to-stac": {}},
            }
        ],
    }
    task = Sentinel2ToStac(payload, workdir=tmp_path, upload=False)

    with pytest.raises(InvalidInput):
        task.process()


# ---------------------------------------------------------------------------
# is_newer_than_existing
# ---------------------------------------------------------------------------

_ITEM_URL = (
    "https://earth-search.aws.element84.com/v2"
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


@pytest.mark.parametrize(
    "status_code, body, expected",
    [
        (404, None, Success(True)),
        (500, None, "Failure"),
        (200, {}, Success(True)),
        (
            200,
            {"properties": {"s2:generation_time": "2020-03-27T06:15:34Z"}},
            Success(True),
        ),
        (
            200,
            {"properties": {"s2:generation_time": _BASELINE_GEN_TIME}},
            Success(True),
        ),
        (
            200,
            {"properties": {"s2:generation_time": "2026-03-27T06:15:34Z"}},
            Success(False),
        ),
    ],
    ids=["not-found", "server-error", "empty-body", "older", "same", "newer"],
)
def test_is_newer_than_existing(
    baseline_item_dict: dict[str, Any],
    status_code: int,
    body: dict[str, Any] | None,
    expected: Any,
) -> None:
    item = Item.from_dict(baseline_item_dict)
    result = _check_is_newer(item, status_code=status_code, body=body)
    if expected == "Failure":
        assert isinstance(result, Failure)
        assert isinstance(result.failure(), Exception)
    else:
        assert result == expected


# ---------------------------------------------------------------------------
# update_item
# ---------------------------------------------------------------------------


def test_providers_and_license_dropped(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert "providers" not in item["properties"]
    assert [link for link in item["links"] if link["rel"] == "license"] == []


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
            item.assets[
                key
            ].href = f"s3://{ES_BUCKET}/sentinel-2-c1-l2a/S2A_TEST/{fname}"
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
    assert "classification:classes" not in item["assets"]["scl"]
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
    with pytest.raises(
        InvalidInput, match=r"only >= 05.00 \(not including 5.09\) is supported"
    ):
        _minimal_task("test-make-cogs").make_cogs_for_item(item)


def test_baseline_0509_raises(baseline_item_dict: dict[str, Any]) -> None:
    item = _item_with_baseline(baseline_item_dict, "05.09")
    with pytest.raises(
        InvalidInput, match=r"only >= 05.00 \(not including 5.09\) is supported"
    ):
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
    owner.assets["B01"] = asset
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
