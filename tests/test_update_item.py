"""PR 4 assertions: the Earth Search overrides update_item applies.

Consumes the same session-scoped ``baseline_item_dict`` (full local process()
over the baseline tile) and checks each piece update_item is responsible for:
collection assignment, payload id, dropped providers/license, the `via` link,
the pystac 1.15.2 storage schemes/refs block, the scrub block, and asset href
rewriting / proj:bbox stripping.
"""

from typing import Any


def test_collection_and_payload_id(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    # Matched via stac_jsonpath_match against upload_options.collections.
    assert item["collection"] == "sentinel-2-c1-l2a"
    assert item["properties"]["earthsearch:payload_id"] == (
        "roda-sentinel-2-l2a/workflow-sentinel-2-l2a-to-stac/"
        "tiles-19-T-DJ-2023-4-19-0"
    )


def test_providers_and_license_dropped(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert "providers" not in item["properties"]
    assert [link for link in item["links"] if link["rel"] == "license"] == []


def test_via_link_added(baseline_item_dict: dict[str, Any]) -> None:
    via = [link for link in baseline_item_dict["links"] if link["rel"] == "via"]
    assert len(via) == 1
    assert via[0]["href"] == (
        "s3://sentinel-s2-l2a/tiles/19/T/DJ/2023/4/19/0/metadata.xml"
    )
    assert via[0]["type"] == "application/xml"
    assert via[0]["title"] == "Granule Metadata in Sinergize RODA Archive"


def test_storage_schemes_and_refs(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    # pystac 1.15.2 storage v2: schemes/refs, NOT the flat v1
    # storage:platform/region/requester_pays. Provider identity is in `type`;
    # `platform` is the access-endpoint URI template.
    assert (
        "https://stac-extensions.github.io/storage/v2.0.0/schema.json"
        in item["stac_extensions"]
    )
    assert item["properties"]["storage:schemes"] == {
        "aws": {
            "type": "aws-s3",
            "platform": "https://{bucket}.s3.{region}.amazonaws.com",
            "region": "us-west-2",
            "requester_pays": False,
        }
    }
    # No flat v1 fields.
    assert "storage:platform" not in item["properties"]
    # Every asset references the single scheme.
    for name, asset in item["assets"].items():
        assert asset.get("storage:refs") == ["aws"], name


def test_scrub_block(baseline_item_dict: dict[str, Any]) -> None:
    item = baseline_item_dict
    assert "classification:classes" not in item["assets"]["scl"]["raster:bands"][0]
    assert "eo:snow_cover" not in item["properties"]
    assert not any("classification" in ext for ext in item["stac_extensions"])


def test_asset_href_rewriting_and_proj_bbox(
    baseline_item_dict: dict[str, Any]
) -> None:
    item = baseline_item_dict
    # Imagery assets point back at the RODA source bucket, preserving the
    # resolution subpath create_item produced.
    assert item["assets"]["blue"]["href"] == (
        "s3://sentinel-s2-l2a/tiles/19/T/DJ/2023/4/19/0/R10m/B02.jp2"
    )
    # Metadata assets point at the local downloaded files.
    assert item["assets"]["granule_metadata"]["href"].endswith("/metadata.xml")
    assert not item["assets"]["granule_metadata"]["href"].startswith("s3://")
    # proj:bbox stripped from assets.
    assert "proj:bbox" not in item["assets"]["blue"]
