"""Semantic parity diff between a destination actual.json and a legacy out.json.

Dev tool (not part of the test suite) for reviewing legacy↔destination parity
during the migration. Plain `diff` is useless here because pystac serializes
keys in a different order; this does a structural, key-order-insensitive compare
and strips the fields we've established as *expected* drift so only meaningful
STAC differences surface:
  - properties.created (timestamp)
  - asset href / file:checksum / file:size (paths; GDAL/JPEG byte drift; id rename)
  - geometry / proj:centroid (platform float wobble — the real test handles these
    tolerantly)
  - stac_extensions element order

Usage:
    uv run python scripts/compare_fixture.py <actual.json> <expected/out.json>
    uv run python scripts/compare_fixture.py --keep-href <actual> <expected>

DeepDiff runs with ignore_order=True, so reordered lists (stac_extensions, band
arrays) don't show — only genuine added/removed/changed values do.
"""

import json
import sys

from deepdiff import DeepDiff

KEEP_HREF = "--keep-href" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
actual_path, expected_path = args[0], args[1]

# Per-asset keys expected to differ (bytes/paths), not semantic parity.
ASSET_NOISE = {"file:checksum", "file:size"}
if not KEEP_HREF:
    ASSET_NOISE.add("href")


def scrub(doc: dict) -> dict:
    doc = json.loads(json.dumps(doc))  # deep copy + type-normalize
    for feat in doc.get("features", [doc]):
        props = feat.get("properties", {})
        props.pop("created", None)
        props.pop("proj:centroid", None)
        feat.pop("geometry", None)
        if isinstance(feat.get("stac_extensions"), list):
            feat["stac_extensions"].sort()
        for asset in feat.get("assets", {}).values():
            for k in ASSET_NOISE:
                asset.pop(k, None)
    return doc


expected = scrub(json.load(open(expected_path)))
actual = scrub(json.load(open(actual_path)))

diff = DeepDiff(expected, actual, ignore_order=True, math_epsilon=1e-5)
if not diff:
    print("No semantic differences (after stripping expected-drift fields).")
else:
    print(diff.to_json(indent=2))
