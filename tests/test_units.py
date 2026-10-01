"""Unit tests for individual task components.

Sections
--------
download        read_href + the metadata download block in resolve_source()
bucket/doc      bucket listing, existing-doc discovery, metadata resolution
reference path  create_cogs=False reference/update path
is_newer        is_newer_than_existing STAC-API gate
update_item     Earth Search override assertions (update_item)
safe            SAFE archive layout resolution
cogify          up-front COG creation + the cogify/write_cog pipeline

All tests are local-only: no network, no live S3.  The session-wide
``_stub_stac_api`` fixture in conftest.py intercepts every STAC-API request;
``read_href`` and the module-level boto3 S3 clients are monkeypatched where
needed.
"""

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest
import rasterio
import requests_mock as requests_mock_module
from botocore.exceptions import ClientError
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from rasterio.transform import from_bounds
from returns.result import Failure, Success
from stactask.exceptions import InvalidInput

import sentinel_2_l2a_to_stac.task as task_module
from sentinel_2_l2a_to_stac.cogify import CogFile, FileInfo, cogify
from sentinel_2_l2a_to_stac.constants import (
    CANONICAL_L2A_IMAGE_PATHS,
)
from sentinel_2_l2a_to_stac.metadata import parse_metadata
from sentinel_2_l2a_to_stac.safe import resolve_safe_layout
from sentinel_2_l2a_to_stac.task import (
    ASSET_FILENAMES,
    THUMBNAIL_ASSET_NAME,
    Sentinel2ToStac,
    _set_asset_owners,
    _validate_processing_baseline,
    find_existing_stac_doc_filename,
)

_BASELINE_SOURCE_METADATA = (
    Path(__file__).parent / "fixtures" / "source-metadata" / "tiles-19-T-DJ-2023-4-19-0"
)

# The Earth Search-style bucket the baseline_item_dict fixture (conftest.py)
# resolves its metadata_href against -- must match the literal there.
_BASELINE_BUCKET = "es-test-bucket"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _minimal_task(payload_id: str = "test-unit") -> Sentinel2ToStac:
    """Minimal task instance — only needs metadata_href to pass validate()."""
    return Sentinel2ToStac(
        {
            "id": payload_id,
            "metadata_href": "s3://example-bucket/prefix/metadata.xml",
        },
        upload=False,
    )


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------

_S3_DIR = "s3://example-bucket/prefix"
_METADATA_HREF = f"{_S3_DIR}/metadata.xml"

_GRANULE_BYTES = b"<granule-metadata/>"
_PRODUCT_BYTES = b"<product-metadata/>"

_EXPECTED_HREFS = {
    f"{_S3_DIR}/product_metadata.xml": _PRODUCT_BYTES,
    f"{_S3_DIR}/metadata.xml": _GRANULE_BYTES,
}


def _make_download_task(
    tmp_path: Path, metadata_href: str = _METADATA_HREF
) -> Sentinel2ToStac:
    payload = {
        "metadata_href": metadata_href,
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


@dataclass(frozen=True)
class _StubMetadata:
    """Just enough of Metadata for process()'s processing-baseline gate and,
    for a SAFE (create_cogs=True) input, the post-cogify replace() rebuild.
    """

    metadata_dict: dict[str, str] = field(
        default_factory=lambda: {"s2:processing_baseline": "05.00"}
    )
    image_paths: list[str] = field(default_factory=list)
    image_media_type: str = "image/tiff"


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize metadata parsing + create_item + update_item +
    add_storage_schemes so download tests stay focused.

    The canned bytes above are not real Sentinel-2 metadata; those stages are
    covered by the update_item / storage tests below against genuine local metadata.
    """
    monkeypatch.setattr(task_module, "parse_metadata", lambda *a, **k: _StubMetadata())
    monkeypatch.setattr(
        task_module, "create_item", lambda _workdir, metadata=None: _StubItem()
    )
    monkeypatch.setattr(Sentinel2ToStac, "data_geometry", lambda self, source: None)
    monkeypatch.setattr(
        Sentinel2ToStac, "fetch_source_images", lambda self, image_hrefs: None
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "measure_reference_images",
        lambda self, image_hrefs: (None, {}),
    )
    monkeypatch.setattr(Sentinel2ToStac, "update_item", lambda self, item: item)
    monkeypatch.setattr(Sentinel2ToStac, "add_storage_schemes", lambda self, item: item)
    # _StubItem is a bare stand-in, not a real pystac Item -- the reference-path
    # asset-rewriting helpers are exercised for real in the dedicated process()
    # tests below (against _synthetic_full_item), so no-op them here.
    monkeypatch.setattr(
        Sentinel2ToStac, "apply_earthsearch_hrefs", lambda self, item, s3_path: item
    )
    monkeypatch.setattr(
        Sentinel2ToStac, "add_thumbnail_asset", lambda self, item, s3_path: item
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "apply_reference_file_info",
        lambda self, item, bucket_filenames, doc, measured: item,
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: set(ASSET_FILENAMES.values()),
    )


def test_download_fetches_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fake_read_href_factory(calls))
    _stub_pipeline(monkeypatch)
    task = _make_download_task(tmp_path)

    task.process()

    assert task.granule_metadata_xml_path.read_bytes() == _GRANULE_BYTES
    assert task.product_metadata_xml_path.read_bytes() == _PRODUCT_BYTES
    # Fetched directly from the granule prefix -- never a fallback elsewhere.
    assert set(calls) == set(_EXPECTED_HREFS)


def test_read_href_translates_nosuchkey_to_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(Bucket: str, Key: str, **kwargs: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
        )

    monkeypatch.setattr(task_module._s3_client, "get_object", _raise)
    with pytest.raises(InvalidInput, match="Failed fetching href"):
        _make_download_task(tmp_path).read_href(
            "s3://example-bucket/does/not/exist.json"
        )


def test_read_href_reraises_other_client_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(Bucket: str, Key: str, **kwargs: Any) -> Any:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetObject"
        )

    monkeypatch.setattr(task_module._s3_client, "get_object", _raise)
    with pytest.raises(ClientError):
        _make_download_task(tmp_path).read_href("s3://example-bucket/denied.json")


# ---------------------------------------------------------------------------
# bucket listing / existing-doc discovery / product-metadata resolution
# ---------------------------------------------------------------------------


def _fake_s3_client(keys: list[str]) -> Any:
    class _FakePaginator:
        def paginate(self, **kwargs: Any) -> Any:
            return [{"Contents": [{"Key": k} for k in keys]}]

    class _FakeClient:
        def get_paginator(self, name: str) -> Any:
            assert name == "list_objects_v2"
            return _FakePaginator()

    return _FakeClient()


def test_list_bucket_filenames_returns_basenames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_module,
        "_s3_client",
        _fake_s3_client(
            [
                "prefix/B02.tif",
                "prefix/tileInfo.json",
                "prefix/S2A_T19TDJ_L2A.json",
            ]
        ),
    )
    task = _minimal_task("test-list-bucket")
    assert task.list_bucket_filenames("s3://bucket/prefix") == {
        "B02.tif",
        "tileInfo.json",
        "S2A_T19TDJ_L2A.json",
    }


def test_find_existing_stac_doc_filename_ignores_non_doc_files() -> None:
    # Only an exact `{item_id}.json` counts as the doc; any other JSON file
    # sitting alongside the COGs must never be mistaken for it.
    assert (
        find_existing_stac_doc_filename(
            {"metadata.xml", "some-other-file.json"}, "S2A_T19TDJ_L2A"
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


def test_resolve_image_hrefs_raises_when_cog_missing() -> None:
    bucket_filenames = set(ASSET_FILENAMES.values()) - {"B02.tif"}
    with pytest.raises(InvalidInput, match="Expected COG\\(s\\) not found"):
        Sentinel2ToStac.resolve_image_hrefs("s3://bucket/prefix", bucket_filenames)


def test_resolve_image_hrefs_returns_flat_earthsearch_layout() -> None:
    image_hrefs = Sentinel2ToStac.resolve_image_hrefs(
        "s3://bucket/prefix", set(ASSET_FILENAMES.values())
    )
    assert image_hrefs["blue"] == "s3://bucket/prefix/B02.tif"
    assert set(image_hrefs) == set(CANONICAL_L2A_IMAGE_PATHS)


def test_asset_filenames_map_matches_create_item_keys(
    baseline_item_dict: dict[str, Any],
) -> None:
    # ASSET_FILENAMES must cover exactly the canonical keys create_item produces
    # (thumbnail is added manually in the reference path, not by create_item).
    item = Item.from_dict(baseline_item_dict)
    assert set(ASSET_FILENAMES.keys()) == set(item.assets.keys()) | {"thumbnail"}


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
    assert thumb.title == "Thumbnail of preview image"


def test_apply_reference_file_info_reuses_from_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"

    def _fail_if_called(self: Sentinel2ToStac, href: str) -> FileInfo:
        raise AssertionError(f"should not download when reusing: {href}")

    monkeypatch.setattr(Sentinel2ToStac, "remote_file_info", _fail_if_called)
    task = Sentinel2ToStac(
        {"id": "test-reuse", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    doc = {"assets": {"blue": {"file:size": 123, "file:checksum": "abc"}}}
    result = task.apply_reference_file_info(item, {"B02.tif"}, doc, {})

    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 123
    assert fext.checksum == "abc"


def test_apply_reference_file_info_reuses_measured_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The footprint pass already hashed this raster; it must not be fetched again.
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"

    def _fail_if_called(self: Sentinel2ToStac, href: str) -> FileInfo:
        raise AssertionError(f"should not download when measured: {href}")

    monkeypatch.setattr(Sentinel2ToStac, "remote_file_info", _fail_if_called)
    task = Sentinel2ToStac(
        {"id": "test-measured", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    measured = {"blue": FileInfo(size=42, checksum="measured")}
    result = task.apply_reference_file_info(item, {"B02.tif"}, {"assets": {}}, measured)

    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 42
    assert fext.checksum == "measured"


def test_apply_reference_file_info_downloads_when_missing_from_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    monkeypatch.setattr(
        Sentinel2ToStac,
        "remote_file_info",
        lambda self, href: FileInfo(size=14, checksum="fresh"),
    )
    task = Sentinel2ToStac(
        {"id": "test-download", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    result = task.apply_reference_file_info(item, {"B02.tif"}, {"assets": {}}, {})

    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 14
    assert fext.checksum == "fresh"
    # Size/checksum are streamed; nothing is written to the workdir.
    assert list(tmp_path.iterdir()) == []


def test_apply_reference_file_info_preserves_cached_metadata_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A metadata asset with no file info in the doc must not clobber or delete
    # the cached source file of the same name in the workdir.
    item = _synthetic_item(["granule_metadata"])
    item.assets["granule_metadata"].href = "s3://bucket/prefix/metadata.xml"
    cached = tmp_path / "metadata.xml"
    cached.write_bytes(b"cached-original")
    monkeypatch.setattr(
        Sentinel2ToStac,
        "remote_file_info",
        lambda self, href: FileInfo(size=7, checksum="from-s3"),
    )
    task = Sentinel2ToStac(
        {"id": "test-cache", "metadata_href": "s3://bucket/prefix/x"},
        workdir=tmp_path,
        upload=False,
    )

    result = task.apply_reference_file_info(item, {"metadata.xml"}, {"assets": {}}, {})

    assert cached.read_bytes() == b"cached-original"
    assert FileExtension.ext(result.assets["granule_metadata"]).size == 7


def test_apply_reference_file_info_ignores_extra_doc_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    monkeypatch.setattr(
        Sentinel2ToStac,
        "remote_file_info",
        lambda self, href: FileInfo(size=1, checksum="x"),
    )
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
    result = task.apply_reference_file_info(item, {"B02.tif"}, doc, {})

    assert "red" not in result.assets
    fext = FileExtension.ext(result.assets["blue"])
    assert fext.size == 1
    assert fext.checksum == "x"


def test_apply_reference_file_info_raises_when_missing_from_bucket() -> None:
    item = _synthetic_item(["blue"])
    item.assets["blue"].href = "s3://bucket/prefix/B02.tif"
    with pytest.raises(InvalidInput, match="not found in the bucket"):
        _minimal_task("test-missing-from-bucket").apply_reference_file_info(
            item, set(), {"assets": {}}, {}
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
    item.set_collection("sentinel-2-l2a")
    # create_item never produces a "thumbnail" asset -- only the reference
    # path (via add_thumbnail_asset) or the cog path (via make_thumbnail) do.
    for key, filename in ASSET_FILENAMES.items():
        if key == THUMBNAIL_ASSET_NAME:
            continue
        item.assets[key] = Asset(href=f"local-{filename}", type="image/tiff")
    _set_asset_owners(item)
    return item


def _stub_pipeline_with_item(monkeypatch: pytest.MonkeyPatch, item: Item) -> None:
    monkeypatch.setattr(task_module, "parse_metadata", lambda *a, **k: _StubMetadata())
    monkeypatch.setattr(
        task_module, "create_item", lambda _workdir, metadata=None: item
    )
    monkeypatch.setattr(Sentinel2ToStac, "data_geometry", lambda self, source: None)
    monkeypatch.setattr(
        Sentinel2ToStac, "fetch_source_images", lambda self, image_hrefs: None
    )
    monkeypatch.setattr(
        Sentinel2ToStac,
        "measure_reference_images",
        lambda self, image_hrefs: (None, {}),
    )
    monkeypatch.setattr(Sentinel2ToStac, "update_item", lambda self, item: item)
    monkeypatch.setattr(Sentinel2ToStac, "add_storage_schemes", lambda self, item: item)


_STUB_FILE_INFO = FileInfo(size=4, checksum="stub-checksum")


def _stub_metadata_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Sentinel2ToStac, "read_href", lambda self, href: b"<x/>")
    monkeypatch.setattr(
        Sentinel2ToStac, "remote_file_info", lambda self, href: _STUB_FILE_INFO
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
    # No existing doc entries -> every asset is "missing from doc" -> measured.
    assert out_item["assets"]["blue"]["file:size"] == _STUB_FILE_INFO.size


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


def test_process_raises_when_granule_missing_cogs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A granule metadata_href is always the "existing COGs" flow -- there is
    # no create-COGs fallback, so a bucket without the full flat
    # COG layout is a hard failure, not a legacy pass-through.
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(
        Sentinel2ToStac, "list_bucket_filenames", lambda self, s3_path: set()
    )

    with pytest.raises(InvalidInput, match=r"Expected COG\(s\) not found"):
        _make_download_task(tmp_path).process()


def test_process_raises_when_thumbnail_missing_from_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    bucket_filenames = set(ASSET_FILENAMES.values()) - {"L2A_PVI.jpg"}
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


def test_process_ignores_create_cogs_payload_for_granule_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stray create_cogs=true from a legacy caller must not change anything
    # for a granule metadata_href: it's still the existing-COGs flow, and a
    # bucket without the full flat layout still fails.
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    _stub_metadata_reads(monkeypatch)
    monkeypatch.setattr(
        Sentinel2ToStac,
        "list_bucket_filenames",
        lambda self, s3_path: {_FULL_ITEM_DOC_FILENAME},
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

    with pytest.raises(InvalidInput, match=r"Expected COG\(s\) not found"):
        task.process()


def test_process_safe_input_always_creates_cogs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A SAFE archive is never checked against a bucket listing at all -- COG
    # creation is unconditional, regardless of what (if anything) exists there.
    _stub_pipeline_with_item(monkeypatch, _synthetic_full_item())
    monkeypatch.setattr(
        Sentinel2ToStac,
        "resolve_source",
        lambda self: task_module.SourceProduct(
            prefix="/safe/root",
            image_hrefs={"blue": "/safe/root/B02.jp2"},
            bucket_filenames=set(),
            create_cogs=True,
        ),
    )
    cogify_calls: list[dict[str, str]] = []

    def _fake_cogify_source_images(
        self: Sentinel2ToStac, metadata: Any, image_hrefs: dict[str, str]
    ) -> dict[str, CogFile]:
        cogify_calls.append(image_hrefs)
        return {}

    monkeypatch.setattr(
        Sentinel2ToStac, "cogify_source_images", _fake_cogify_source_images
    )
    monkeypatch.setattr(task_module, "make_thumbnail", lambda item: item)

    task = Sentinel2ToStac(
        {"id": "test-safe-cogs", "safe_href": "/safe/root"},
        workdir=tmp_path,
        upload=False,
    )

    result = task.process()

    assert len(result) == 1
    assert cogify_calls == [{"blue": "/safe/root/B02.jp2"}]


# ---------------------------------------------------------------------------
# is_newer_than_existing
# ---------------------------------------------------------------------------

_ITEM_URL = (
    "https://earth-search.aws.element84.com/v2"
    "/collections/sentinel-2-l2a/items/S2A_T19TDJ_20230419T153818_L2A"
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
    # The baseline fixture simulates the existing-COGs flow: every asset is
    # rewritten to the Earth Search prefix, so only the "earthsearch" scheme
    # is ever produced -- this task never references any other bucket.
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
        "earthsearch": {**_S3_SCHEME, "bucket": _BASELINE_BUCKET},
    }
    assert "storage:platform" not in item["properties"]
    for name, asset in item["assets"].items():
        assert asset.get("storage:refs") == ["earthsearch"], name


def test_add_storage_schemes_classification() -> None:
    """add_storage_schemes assigns refs by href: Earth Search or local."""
    item = Item(
        id="test",
        geometry=None,
        bbox=None,
        datetime=datetime(2023, 1, 1, tzinfo=timezone.utc),
        properties={},
    )
    item.assets["es_asset"] = Asset(href="s3://earth-search-output/collection/x.tif")
    item.assets["es_asset_2"] = Asset(href="s3://earth-search-output/collection/y.tif")
    item.assets["local_asset"] = Asset(href="/tmp/workdir/metadata.xml")
    _set_asset_owners(item)

    result = _minimal_task("test-storage").add_storage_schemes(item)
    result_dict = result.to_dict()

    schemes = result_dict["properties"]["storage:schemes"]
    assert set(schemes.keys()) == {"earthsearch", "local"}
    assert schemes["earthsearch"]["bucket"] == "earth-search-output"
    assert schemes["local"]["bucket"] == "local"
    assert all(s["type"] == "aws-s3" for s in schemes.values())

    assets = result_dict["assets"]
    assert assets["es_asset"]["storage:refs"] == ["earthsearch"]
    assert assets["es_asset_2"]["storage:refs"] == ["earthsearch"]
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
            item.assets[key].href = f"s3://{ES_BUCKET}/sentinel-2-l2a/S2A_TEST/{fname}"
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
    item.set_collection("sentinel-2-l2a")
    item.assets["blue"] = Asset(href=str(local_file), type=MediaType.COG)
    _set_asset_owners(item)

    task = Sentinel2ToStac(
        {"id": "test-es-scheme", "metadata_href": "s3://example-bucket/x"},
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
    assert item["assets"]["blue"]["href"] == f"s3://{_BASELINE_BUCKET}/prefix/B02.tif"
    # apply_earthsearch_hrefs rewrites every asset, including metadata files --
    # this task never leaves an asset pointing at a source bucket in place.
    assert (
        item["assets"]["granule_metadata"]["href"]
        == f"s3://{_BASELINE_BUCKET}/prefix/metadata.xml"
    )
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
        {"id": "test-local-keys", "metadata_href": "s3://example-bucket/x"},
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
    item.assets["remote"] = Asset(href="s3://example-bucket/prefix/B02.tif")
    item.assets["outside"] = Asset(href="/some/other/dir/B01.tif")

    assert task.get_local_asset_keys(item) == ["local_meta", "local_cog"]


# ---------------------------------------------------------------------------
# safe
# ---------------------------------------------------------------------------

_SAFE_ROOT = "/data/S2B_MSIL2A_20190704T103029_N0500_R108_T31SGV_20230624T153438.SAFE"
_SAFE_GRANULE = f"{_SAFE_ROOT}/GRANULE/L2A_T31SGV_A012146_20190704T103317"
_SAFE_LISTING = [
    f"{_SAFE_ROOT}/MTD_MSIL2A.xml",
    f"{_SAFE_ROOT}/INSPIRE.xml",
    f"{_SAFE_GRANULE}/MTD_TL.xml",
    f"{_SAFE_GRANULE}/QI_DATA/MSK_CLDPRB_20m.jp2",
    f"{_SAFE_GRANULE}/QI_DATA/MSK_CLDPRB_60m.jp2",
    f"{_SAFE_GRANULE}/QI_DATA/MSK_SNWPRB_20m.jp2",
    f"{_SAFE_GRANULE}/QI_DATA/MSK_DETFOO_B02.jp2",
    f"{_SAFE_GRANULE}/QI_DATA/T31SGV_20190704T103029_PVI.jp2",
] + [
    f"{_SAFE_GRANULE}/IMG_DATA/R{res}m/T31SGV_20190704T103029_{name}_{res}m.jp2"
    for res, names in (
        (10, ["AOT", "B02", "B03", "B04", "B08", "TCI", "WVP"]),
        (
            20,
            [
                "AOT",
                "B01",
                "B02",
                "B03",
                "B04",
                "B05",
                "B06",
                "B07",
                "B8A",
                "B11",
                "B12",
                "SCL",
                "TCI",
                "WVP",
            ],
        ),
        (60, ["AOT", "B01", "B09", "SCL", "TCI", "WVP"]),
    )
    for name in names
]


def test_resolve_safe_layout_maps_every_canonical_asset() -> None:
    layout = resolve_safe_layout(_SAFE_ROOT, _SAFE_LISTING)

    assert layout.product_metadata_href == f"{_SAFE_ROOT}/MTD_MSIL2A.xml"
    assert layout.granule_metadata_href == f"{_SAFE_GRANULE}/MTD_TL.xml"
    assert set(layout.image_hrefs) == set(CANONICAL_L2A_IMAGE_PATHS)
    # Native resolution wins where a band exists at several: B01 is 60m and
    # B02 is 10m, and TCI/SCL come from their published resolution.
    assert layout.image_hrefs["coastal"].endswith(
        "R60m/T31SGV_20190704T103029_B01_60m.jp2"
    )
    assert layout.image_hrefs["blue"].endswith(
        "R10m/T31SGV_20190704T103029_B02_10m.jp2"
    )
    assert layout.image_hrefs["visual"].endswith("_TCI_10m.jp2")
    assert layout.image_hrefs["scl"].endswith("_SCL_20m.jp2")
    assert layout.image_hrefs["cloud"].endswith("QI_DATA/MSK_CLDPRB_20m.jp2")
    assert layout.image_hrefs["snow"].endswith("QI_DATA/MSK_SNWPRB_20m.jp2")
    assert layout.image_hrefs["preview"].endswith("_PVI.jp2")


def test_resolve_safe_layout_raises_on_missing_file() -> None:
    listing = [h for h in _SAFE_LISTING if "MSK_SNWPRB_20m" not in h]
    with pytest.raises(InvalidInput, match="No file matching"):
        resolve_safe_layout(_SAFE_ROOT, listing)


# ---------------------------------------------------------------------------
# cogify_source_images / cogify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("processing_baseline", ["02.13", "04.99", "05.09", "06.00"])
def test_unsupported_baseline_raises(processing_baseline: str) -> None:
    with pytest.raises(
        InvalidInput,
        match=rf"Invalid processing baseline \({processing_baseline}\)",
    ):
        _validate_processing_baseline(processing_baseline)


@pytest.mark.parametrize("processing_baseline", ["05.00", "05.08", "05.10", "05.99"])
def test_supported_baseline_does_not_raise(processing_baseline: str) -> None:
    _validate_processing_baseline(processing_baseline)


def test_cogify_source_images_covers_the_canonical_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = parse_metadata(str(_BASELINE_SOURCE_METADATA))
    metadata.metadata_dict["s2:processing_baseline"] = "05.00"

    monkeypatch.setattr(
        Sentinel2ToStac, "fetch_source_images", lambda self, image_hrefs: None
    )
    monkeypatch.setattr(
        task_module,
        "cogify",
        lambda asset_name, asset: CogFile(Path(asset.href).with_suffix(".tif"), 1, "a"),
    )

    cogs = _minimal_task("test-cogify").cogify_source_images(
        metadata, {k: f"s3://bucket/{v}" for k, v in CANONICAL_L2A_IMAGE_PATHS.items()}
    )

    assert set(cogs) == set(CANONICAL_L2A_IMAGE_PATHS)
    assert cogs["blue"].filename == "B02.tif"
    assert cogs["cloud"].filename == "CLD_20m.tif"


def test_fetch_source_images_renames_to_canonical_filenames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A SAFE archive names its images after the tile and sensing time; they
    # must land in the workdir under the canonical flat filenames.
    safe_dir = tmp_path / "safe"
    safe_dir.mkdir()
    (safe_dir / "T31SGV_20190704T103029_B02_10m.jp2").write_bytes(b"blue")
    (safe_dir / "MSK_CLDPRB_20m.jp2").write_bytes(b"cloud")

    workdir = tmp_path / "work"
    workdir.mkdir()
    task = Sentinel2ToStac(
        {"id": "test-fetch", "safe_href": str(safe_dir)},
        workdir=workdir,
        upload=False,
    )

    task.fetch_source_images(
        {
            "blue": str(safe_dir / "T31SGV_20190704T103029_B02_10m.jp2"),
            "cloud": str(safe_dir / "MSK_CLDPRB_20m.jp2"),
        }
    )

    assert (workdir / "B02.jp2").read_bytes() == b"blue"
    assert (workdir / "CLD_20m.jp2").read_bytes() == b"cloud"


def test_measure_reference_images_discards_each_raster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reference path must not accumulate imagery in the workdir: each
    # raster is hashed and deleted before the next one is fetched.
    source = tmp_path / "source"
    source.mkdir()
    _make_synthetic_raster(source / "B02.tif")
    workdir = tmp_path / "work"
    workdir.mkdir()

    held: list[int] = []

    def _fake_fetch(self: Sentinel2ToStac, image_hrefs: dict[str, str]) -> None:
        for key, href in image_hrefs.items():
            shutil.copy(href, workdir / task_module.CANONICAL_IMAGE_FILENAMES[key])
        held.append(len(list(workdir.iterdir())))

    monkeypatch.setattr(Sentinel2ToStac, "fetch_source_images", _fake_fetch)
    task = Sentinel2ToStac(
        {"id": "test-measure", "metadata_href": _METADATA_HREF},
        workdir=workdir,
        upload=False,
    )

    _, file_info = task.measure_reference_images(
        {
            "blue": str(source / "B02.tif"),
            # Not a reflectance band: never fetched, left for the bucket.
            "aot": str(source / "AOT.tif"),
        }
    )

    assert set(file_info) == {"blue"}
    assert file_info["blue"].size == (source / "B02.tif").stat().st_size
    assert file_info["blue"].checksum
    assert held == [1]
    assert list(workdir.iterdir()) == []


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

    cog = cogify("B01", Asset(href=str(src_path), media_type="image/jp2"))

    assert cog.path.suffix == ".tif"
    assert cog.filename == "B01.tif"
    assert cog.checksum
    assert cog.size > 0

    with rasterio.open(str(cog.path)) as ds:
        assert ds.count == 1
        assert ds.width == 64
        assert ds.height == 64


def test_logger_prefixes_payload_id(caplog: pytest.LogCaptureFixture) -> None:
    # stactask 0.7.0 installs a TaskLoggerAdapter in Task.__init__ that
    # prefixes every log line with the payload id. Guard against a future
    # stactask change silently dropping the prefix from production logs.
    task = Sentinel2ToStac(
        {"id": "test-payload-123", "metadata_href": "x"}, upload=False
    )
    with caplog.at_level(logging.INFO):
        task.logger.info("processing tile")
    assert "[test-payload-123] processing tile" in caplog.text
