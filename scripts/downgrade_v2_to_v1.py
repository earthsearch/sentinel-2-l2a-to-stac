"""POC: downgrade a v2 (STAC 1.1) Sentinel-2 L2A item dict to the v1 (STAC 1.0) shape.

Proof of concept for review, not wired into the task. Each numbered step below
is independent so it can be reimplemented (or dropped) on its own later.

Deliberately NOT restored (output still differs from v1 here):
  - `via` link: removed upstream on purpose.
  - `s2:dark_features_percentage`: absent from the source data, not a code change.
  - Workflow/task identity (`processing:software` key, `earthsearch:payload_id`,
    hrefs): these follow the task rename, not the STAC format.
  - Key order within dicts: JSON-irrelevant.

Interface is dict -> dict. For a pystac Item, the wrapper is one line:
`downgrade_item(item.to_dict())`. Keep the result as a dict, because pystac
re-serialization stamps `stac_version: 1.1.0` back on.

Usage:
    uv run python scripts/downgrade_v2_to_v1.py <v2.json> [out.json]
Accepts a single Item or a payload/FeatureCollection with `features`.
"""

import copy
import json
import sys
from typing import Any

from sentinel_2_l2a_to_stac.constants import EO_BAND_RENAME, RASTER_BAND_RENAME

Json = dict[str, Any]


# ── Step 1: STAC version + extension schema URLs ────────────────────────────

V1_STAC_VERSION = "1.0.0"
EXT = "https://stac-extensions.github.io"
EXTENSION_DOWNGRADE: dict[str, str] = {
    f"{EXT}/eo/v2.0.0/schema.json": f"{EXT}/eo/v1.1.0/schema.json",
    f"{EXT}/projection/v2.0.0/schema.json": f"{EXT}/projection/v1.1.0/schema.json",
    f"{EXT}/raster/v2.0.0/schema.json": f"{EXT}/raster/v1.1.0/schema.json",
    f"{EXT}/storage/v2.0.0/schema.json": f"{EXT}/storage/v1.0.0/schema.json",
}


def downgrade_version(item: Json) -> None:
    item["stac_version"] = V1_STAC_VERSION
    item["stac_extensions"] = [
        EXTENSION_DOWNGRADE.get(url, url) for url in item.get("stac_extensions", [])
    ]


# ── Step 2: projection `proj:code` -> `proj:epsg` ───────────────────────────


def _proj_code_to_epsg(fields: Json) -> None:
    if "proj:code" not in fields:
        return
    code = fields.pop("proj:code")
    if code is None:
        fields["proj:epsg"] = None
        return
    authority, _, number = str(code).partition(":")
    if authority.upper() != "EPSG" or not number.isdigit():
        raise ValueError(f"proj:code {code!r} has no proj:epsg equivalent")
    fields["proj:epsg"] = int(number)


def downgrade_projection(item: Json) -> None:
    _proj_code_to_epsg(item["properties"])
    for asset in item["assets"].values():
        _proj_code_to_epsg(asset)


# ── Step 3: storage schemes/refs -> item-level platform/region/requester_pays ─

V1_PLATFORM_BY_TYPE: dict[str, str] = {"aws-s3": "AWS"}


def downgrade_storage(item: Json) -> None:
    for asset in item["assets"].values():
        asset.pop("storage:refs", None)

    schemes: Json | None = item["properties"].pop("storage:schemes", None)
    if not schemes:
        return

    # v1 only has one item-level storage location, so every scheme must agree.
    # Bucket names are v2-only and are dropped.
    distinct = {
        (s["type"], s.get("region"), s.get("requester_pays")) for s in schemes.values()
    }
    if len(distinct) != 1:
        raise ValueError(f"storage schemes disagree, cannot collapse: {distinct}")
    scheme_type, region, requester_pays = distinct.pop()
    if scheme_type not in V1_PLATFORM_BY_TYPE:
        raise ValueError(f"no v1 storage:platform for scheme type {scheme_type!r}")

    props = item["properties"]
    props["storage:platform"] = V1_PLATFORM_BY_TYPE[scheme_type]
    if region is not None:
        props["storage:region"] = region
    if requester_pays is not None:
        props["storage:requester_pays"] = requester_pays


# ── Step 4: STAC 1.1 `bands` / flattened fields -> `eo:bands` + `raster:bands`
#
# v2 hoists band fields to the asset when there is one band, or when every band
# shares the value (e.g. visual's nodata/data_type). Undo that by merging
# asset-level band fields into each band, then splitting by extension.

EO_V1_KEY: dict[str, str] = {v2: v1 for v1, v2 in EO_BAND_RENAME.items()}
RASTER_V1_KEY: dict[str, str] = {v2: v1 for v1, v2 in RASTER_BAND_RENAME.items()}
BAND_KEYS = EO_V1_KEY.keys() | RASTER_V1_KEY.keys()


def _split_band(band: Json) -> tuple[Json, Json]:
    unknown = band.keys() - BAND_KEYS
    if unknown:
        raise ValueError(f"band fields with no v1 mapping: {sorted(unknown)}")
    eo = {EO_V1_KEY[k]: v for k, v in band.items() if k in EO_V1_KEY}
    raster = {RASTER_V1_KEY[k]: v for k, v in band.items() if k in RASTER_V1_KEY}
    return eo, raster


def _downgrade_asset_bands(asset: Json) -> None:
    shared = {k: asset.pop(k) for k in list(asset) if k in BAND_KEYS}
    bands: list[Json] = asset.pop("bands", None) or ([{}] if shared else [])

    split = [_split_band({**shared, **band}) for band in bands]
    eo_bands = [eo for eo, _ in split]
    raster_bands = [raster for _, raster in split]

    if any(eo_bands):
        asset["eo:bands"] = eo_bands
    if any(raster_bands):
        asset["raster:bands"] = raster_bands


def downgrade_bands(item: Json) -> None:
    for asset in item["assets"].values():
        _downgrade_asset_bands(asset)


# ── Entry points ────────────────────────────────────────────────────────────


def downgrade_item(item: Json) -> Json:
    """Return a v1-shaped copy of a v2 item dict. The input is not mutated."""
    out = copy.deepcopy(item)
    downgrade_version(out)
    downgrade_projection(out)
    downgrade_storage(out)
    downgrade_bands(out)
    return out


def downgrade_document(doc: Json) -> Json:
    if "features" in doc:
        return {**doc, "features": [downgrade_item(f) for f in doc["features"]]}
    return downgrade_item(doc)


if __name__ == "__main__":
    with open(sys.argv[1]) as f:
        result = json.dumps(downgrade_document(json.load(f)), indent=2)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f:
            f.write(result + "\n")
    else:
        print(result)
