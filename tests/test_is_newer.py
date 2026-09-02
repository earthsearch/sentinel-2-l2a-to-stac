"""PR 5 regression tests: the is_newer_than_existing STAC-API gate.

Ported from legacy ``test_only_process_newer_product_generated_timestamps``.
The gate GETs the item from the live STAC API and, comparing
``s2:generation_time``, decides whether the just-built item is newer than (or
as new as) what is already ingested — the guard that stops the task from
regressing an already-published item.

Rather than re-run the full ``process()`` five times, each case builds an Item
from the session-scoped ``baseline_item_dict`` and calls
``is_newer_than_existing`` directly under a per-test ``requests_mock`` override
of the exact item URL. The session-wide autouse stub in ``conftest.py`` returns
404 for everything by default; requests_mock's nested Mockers (LIFO) let these
tests override that with the specific response each case needs. Nothing here
touches the network — an un-stubbed URL raises ``NoMockAddress``.
"""

import json
from typing import Any

import requests_mock as requests_mock_module
from pystac import Item
from returns.result import Failure, Success

from sentinel_2_l2a_to_stac.task import Sentinel2ToStac

# Matches the baseline tile's create_item output (collection assigned by
# update_item, id from the granule metadata) — the URL the gate will GET.
ITEM_URL = (
    "https://earth-search.aws.element84.com/v1"
    "/collections/sentinel-2-c1-l2a/items/S2A_T19TDJ_20230419T153818_L2A"
)
# The baseline item's own s2:generation_time.
BASELINE_GEN_TIME = "2023-04-19T22:08:59.000000Z"


def _task() -> Sentinel2ToStac:
    # is_newer_than_existing only needs a logger (for the "ignoring ingest"
    # branch); a minimal payload with a metadata_href passes validate().
    payload = {
        "id": "regression-is-newer",
        "metadata_href": "s3://sentinel-s2-l2a/tiles/19/T/DJ/2023/4/19/0/metadata.xml",
    }
    return Sentinel2ToStac(payload, upload=False)


def _check(
    item: Item,
    *,
    status_code: int = 200,
    body: dict[str, Any] | None = None,
) -> Any:
    with requests_mock_module.Mocker() as m:
        m.get(ITEM_URL, status_code=status_code, text=json.dumps(body))
        return _task().is_newer_than_existing(item)


def test_server_error_is_failure(baseline_item_dict: dict[str, Any]) -> None:
    # Any non-200/404 status is an internal failure the caller re-raises.
    item = Item.from_dict(baseline_item_dict)
    result = _check(item, status_code=500)
    assert isinstance(result, Failure)
    assert isinstance(result.failure(), Exception)


def test_not_found_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    # 404 → item not yet ingested → proceed.
    item = Item.from_dict(baseline_item_dict)
    assert _check(item, status_code=404) == Success(True)


def test_empty_body_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    # 200 with no generation_time defaults existing to "" <= created → proceed.
    item = Item.from_dict(baseline_item_dict)
    assert _check(item, body={}) == Success(True)


def test_existing_older_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": "2020-03-27T06:15:34Z"}}
    assert _check(item, body=body) == Success(True)


def test_existing_same_proceeds(baseline_item_dict: dict[str, Any]) -> None:
    # Equal generation_time still proceeds (<=), matching legacy behavior.
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": BASELINE_GEN_TIME}}
    assert _check(item, body=body) == Success(True)


def test_existing_newer_skips(baseline_item_dict: dict[str, Any]) -> None:
    # Existing item is newer → skip the ingest (process() returns []).
    item = Item.from_dict(baseline_item_dict)
    body = {"properties": {"s2:generation_time": "2026-03-27T06:15:34Z"}}
    assert _check(item, body=body) == Success(False)
