"""Locate the files of interest inside a Sentinel-2 L2A SAFE archive.

A SAFE archive nests its metadata and imagery under a granule directory and
names every image after the tile and sensing time, unlike the flat, fixed
filenames of the canonical Earth Search COG layout. This module maps a SAFE
listing onto the canonical asset keys the rest of the task works in; nothing
here reads file contents, so it is pure path logic.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Final

from stactask.exceptions import InvalidInput

SAFE_SUFFIX: Final[str] = ".SAFE"

PRODUCT_METADATA_PATTERN: Final[str] = "MTD_MSIL2A.xml"
GRANULE_METADATA_PATTERN: Final[str] = "GRANULE/*/MTD_TL.xml"

# Canonical asset key -> glob of the granule-relative path inside the SAFE.
# The PVI glob covers both the `L2A_PVI.jp2` and `<tile>_<time>_PVI.jp2`
# spellings used across processing baselines.
IMAGE_PATTERNS: Final[dict[str, str]] = {
    "coastal": "IMG_DATA/R60m/*_B01_60m.jp2",
    "blue": "IMG_DATA/R10m/*_B02_10m.jp2",
    "green": "IMG_DATA/R10m/*_B03_10m.jp2",
    "red": "IMG_DATA/R10m/*_B04_10m.jp2",
    "rededge1": "IMG_DATA/R20m/*_B05_20m.jp2",
    "rededge2": "IMG_DATA/R20m/*_B06_20m.jp2",
    "rededge3": "IMG_DATA/R20m/*_B07_20m.jp2",
    "nir": "IMG_DATA/R10m/*_B08_10m.jp2",
    "nir08": "IMG_DATA/R20m/*_B8A_20m.jp2",
    "nir09": "IMG_DATA/R60m/*_B09_60m.jp2",
    "swir16": "IMG_DATA/R20m/*_B11_20m.jp2",
    "swir22": "IMG_DATA/R20m/*_B12_20m.jp2",
    "aot": "IMG_DATA/R20m/*_AOT_20m.jp2",
    "wvp": "IMG_DATA/R20m/*_WVP_20m.jp2",
    "scl": "IMG_DATA/R20m/*_SCL_20m.jp2",
    "visual": "IMG_DATA/R10m/*_TCI_10m.jp2",
    "cloud": "QI_DATA/MSK_CLDPRB_20m.jp2",
    "snow": "QI_DATA/MSK_SNWPRB_20m.jp2",
    "preview": "QI_DATA/*PVI.jp2",
}


@dataclass(frozen=True)
class SafeLayout:
    product_metadata_href: str
    granule_metadata_href: str
    image_hrefs: dict[str, str]


def resolve_safe_layout(safe_href: str, file_hrefs: list[str]) -> SafeLayout:
    """Map a recursive listing of a SAFE archive onto canonical asset keys.

    Args:
        safe_href: The SAFE directory, local or ``s3://``, without a trailing
            slash.
        file_hrefs: Every file href underneath ``safe_href``.
    """
    root = safe_href.rstrip("/")
    relative = {
        href[len(root) + 1 :]: href
        for href in file_hrefs
        if href.startswith(f"{root}/")
    }

    def find_one(pattern: str) -> str:
        matches = sorted(
            href for rel, href in relative.items() if fnmatchcase(rel, pattern)
        )
        if not matches:
            raise InvalidInput(f"No file matching '{pattern}' in SAFE archive {root}")
        return matches[0]

    granule_metadata_href = find_one(GRANULE_METADATA_PATTERN)
    granule_prefix = posixpath.dirname(granule_metadata_href)[len(root) + 1 :]

    return SafeLayout(
        product_metadata_href=find_one(PRODUCT_METADATA_PATTERN),
        granule_metadata_href=granule_metadata_href,
        image_hrefs={
            key: find_one(f"{granule_prefix}/{pattern}")
            for key, pattern in IMAGE_PATTERNS.items()
        },
    )
