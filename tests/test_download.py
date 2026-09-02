"""Unit tests for PR 2's source-metadata download plumbing.

These exercise ``read_href``, the three ``*_path`` properties, and the download
block in ``process()`` entirely locally — no network, no live S3. ``read_href``
is monkeypatched (or, for the ClientError translation, the underlying
``stac_asset.blocking.read_href``) so nothing ever reaches AWS.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import stac_asset.blocking
from botocore.exceptions import ClientError
from stactask.exceptions import InvalidInput

import sentinel_2_l2a_to_stac.task as task_module
from sentinel_2_l2a_to_stac.task import Sentinel2ToStac

S3_DIR = "s3://sentinel-s2-l2a/tiles/35/M/PP/2023/5/27/0"
METADATA_HREF = f"{S3_DIR}/metadata.xml"
PRODUCT_PATH = "products/2023/5/27/S2A_MSIL2A_EXAMPLE"

TILEINFO_BYTES = json.dumps({"productPath": PRODUCT_PATH}).encode()
GRANULE_BYTES = b"<granule-metadata/>"
PRODUCT_BYTES = b"<product-metadata/>"

# The href each source file is expected to be fetched from, given METADATA_HREF.
EXPECTED_HREFS = {
    f"{S3_DIR}/tileInfo.json": TILEINFO_BYTES,
    f"s3://sentinel-s2-l2a/{PRODUCT_PATH}/metadata.xml": PRODUCT_BYTES,
    f"{S3_DIR}/metadata.xml": GRANULE_BYTES,
}


def make_task(tmp_path: Path, metadata_href: str = METADATA_HREF) -> Sentinel2ToStac:
    """Construct a task against a real tmp workdir, no upload, no network."""
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


def fake_read_href_factory(calls: list[str]) -> Any:
    """Return a read_href replacement that records calls and serves canned bytes."""

    def _fake(self: Sentinel2ToStac, href: str) -> bytes:
        calls.append(href)
        return EXPECTED_HREFS[href]

    return _fake


class _StubItem:
    """Minimal stand-in for the pystac Item create_item returns.

    Carries id/collection_id so process()'s is_newer_than_existing gate (PR 5)
    can build its lookup URL; the session-wide autouse requests_mock stub
    answers that GET with 404 (item not yet ingested → proceed), keeping these
    tests focused on the download block with no network.
    """

    id = "stub-item"
    collection_id = "stub-collection"

    def to_dict(self) -> dict[str, str]:
        return {"id": "stub-item"}


def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize create_item + update_item so these tests exercise only the
    download block.

    The fake bytes served above are not real Sentinel-2 metadata, so the real
    create_item/update_item would fail on them; those stages are covered by
    test_create_item.py / test_update_item.py against genuine local metadata.
    """
    monkeypatch.setattr(task_module, "create_item", lambda _workdir: _StubItem())
    monkeypatch.setattr(
        Sentinel2ToStac, "update_item", lambda self, item, s3_path: item
    )


def test_download_fetches_all_three_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        Sentinel2ToStac, "read_href", fake_read_href_factory(calls)
    )
    stub_pipeline(monkeypatch)
    task = make_task(tmp_path)

    task.process()

    # All three files landed in the workdir with the expected bytes.
    assert task.tileinfo_path.read_bytes() == TILEINFO_BYTES
    assert task.granule_metadata_xml_path.read_bytes() == GRANULE_BYTES
    assert task.product_metadata_xml_path.read_bytes() == PRODUCT_BYTES
    # Product metadata was fetched from the productPath-derived href, not next
    # to the granule metadata — the non-obvious behavior this port preserves.
    assert f"s3://sentinel-s2-l2a/{PRODUCT_PATH}/metadata.xml" in calls
    assert set(calls) == set(EXPECTED_HREFS)


def test_existing_files_are_not_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-populate the workdir; the .exists() guards should skip every fetch.
    (tmp_path / "tileInfo.json").write_bytes(TILEINFO_BYTES)
    (tmp_path / "metadata.xml").write_bytes(GRANULE_BYTES)
    (tmp_path / "product_metadata.xml").write_bytes(PRODUCT_BYTES)

    calls: list[str] = []
    monkeypatch.setattr(
        Sentinel2ToStac, "read_href", fake_read_href_factory(calls)
    )
    stub_pipeline(monkeypatch)
    task = make_task(tmp_path)

    task.process()

    assert calls == []


def test_corrupted_tileinfo_raises_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake(self: Sentinel2ToStac, href: str) -> bytes:
        return b"not valid json{"

    monkeypatch.setattr(Sentinel2ToStac, "read_href", _fake)
    task = make_task(tmp_path)

    with pytest.raises(InvalidInput, match=r"Corrupted tileInfo\.json"):
        task.process()


def test_read_href_translates_nosuchkey_to_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(href: str, config: Any = None, clients: Any = None) -> bytes:
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
            "GetObject",
        )

    monkeypatch.setattr(stac_asset.blocking, "read_href", _raise)
    task = make_task(tmp_path)

    with pytest.raises(InvalidInput, match="Failed fetching href"):
        task.read_href("s3://sentinel-s2-l2a/does/not/exist.json")


def test_read_href_reraises_other_client_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(href: str, config: Any = None, clients: Any = None) -> bytes:
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "nope"}},
            "GetObject",
        )

    monkeypatch.setattr(stac_asset.blocking, "read_href", _raise)
    task = make_task(tmp_path)

    with pytest.raises(ClientError):
        task.read_href("s3://sentinel-s2-l2a/denied.json")
