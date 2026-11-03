# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Portions of this file are derived from:
#
#   stactools (https://github.com/stac-utils/stactools)
#   Copyright 2020 Azavea (http://www.azavea.com)
#   Apache License, Version 2.0
#
#   stactools-sentinel2 (https://github.com/stactools-sentinel2/stactools-sentinel2)
#   Copyright stac-utils <stac@radiant.earth>
#   Apache License, Version 2.0
#
# Significantly modified from the originals:
# - Granule/S3 path only; SAFE archive path removed entirely
# - stactools.core I/O (StacIO, ReadHrefModifier) replaced with direct local
#   file reads; all hrefs in this task are local workdir paths
# - L1C image paths removed; L2A-only constants kept
# - stactools.core.projection.transform_from_bbox inlined as a few lines
# - pystac 2.0 native `bands` used (asset.bands = [pystac.Band.from_dict(...)]);
#   per-asset eo:bands / raster:bands arrays eliminated

from __future__ import annotations

from typing import Final

import pystac
from pystac.extensions.eo import Band as EOBand
from pystac.provider import ProviderRole

# ---------------------------------------------------------------------------
# Constants (from stactools-sentinel2 constants.py, L2A subset)
# ---------------------------------------------------------------------------

SENTINEL2_PROPERTY_PREFIX: Final[str] = "s2"
s2_prefix = SENTINEL2_PROPERTY_PREFIX

SENTINEL2_EXTENSION_SCHEMA: Final[str] = (
    "https://stac-extensions.github.io/sentinel-2/v1.0.0/schema.json"
)

SENTINEL_INSTRUMENTS: Final[list[str]] = ["msi"]
SENTINEL_CONSTELLATION: Final[str] = "sentinel-2"

SENTINEL_PROVIDER: Final[pystac.Provider] = pystac.Provider(
    name="ESA",
    roles=[ProviderRole.PRODUCER, ProviderRole.PROCESSOR, ProviderRole.LICENSOR],
    url="https://earth.esa.int/web/guest/home",
)

PRODUCT_METADATA_ASSET_KEY: Final[str] = "product_metadata"
GRANULE_METADATA_ASSET_KEY: Final[str] = "granule_metadata"

DEFAULT_TOLERANCE: Final[float] = 0.01
COORD_ROUNDING: Final[int] = 6

SENTINEL_BANDS: Final[dict[str, EOBand]] = {
    "coastal": EOBand.create(
        name="B01",
        common_name="coastal",
    ),
    "blue": EOBand.create(
        name="B02",
        common_name="blue",
    ),
    "green": EOBand.create(
        name="B03",
        common_name="green",
    ),
    "red": EOBand.create(
        name="B04",
        common_name="red",
    ),
    "rededge1": EOBand.create(
        name="B05",
        common_name="rededge",
    ),
    "rededge2": EOBand.create(
        name="B06",
        common_name="rededge",
    ),
    "rededge3": EOBand.create(
        name="B07",
        common_name="rededge",
    ),
    "nir": EOBand.create(
        name="B08",
        common_name="nir",
    ),
    "nir08": EOBand.create(
        name="B8A",
        common_name="nir08",
    ),
    "nir09": EOBand.create(
        name="B09",
        common_name="nir09",
    ),
    "cirrus": EOBand.create(
        name="B10",
        common_name="cirrus",
    ),
    "swir16": EOBand.create(
        name="B11",
        common_name="swir16",
    ),
    "swir22": EOBand.create(
        name="B12",
        common_name="swir22",
    ),
}

# SentiWiki "Spectral Resolution" tables 3-4: (equivalent wavelength, bandwidth) in nm.
SENTINEL_BAND_WAVELENGTHS_NM: Final[dict[str, dict[str, tuple[float, float]]]] = {
    "sentinel-2a": {
        "coastal": (442.7, 21),
        "blue": (492.7, 65),
        "green": (559.9, 35),
        "red": (664.6, 31),
        "rededge1": (704.1, 15),
        "rededge2": (740.5, 13),
        "rededge3": (782.8, 19),
        "nir": (832.8, 118),
        "nir08": (864.7, 20),
        "nir09": (945.1, 20),
        "cirrus": (1373.5, 29),
        "swir16": (1613.7, 89),
        "swir22": (2202.4, 180),
    },
    "sentinel-2b": {
        "coastal": (442.3, 20),
        "blue": (492.3, 65),
        "green": (559.0, 35),
        "red": (664.9, 31),
        "rededge1": (703.8, 14),
        "rededge2": (739.1, 14),
        "rededge3": (779.7, 20),
        "nir": (832.9, 115),
        "nir08": (864.0, 21),
        "nir09": (943.2, 19),
        "cirrus": (1376.9, 29),
        "swir16": (1610.4, 93),
        "swir22": (2185.7, 181),
    },
    "sentinel-2c": {
        "coastal": (444.2, 20),
        "blue": (489.1, 65),
        "green": (560.6, 35),
        "red": (666.5, 31),
        "rededge1": (707.1, 16),
        "rededge2": (741.1, 16),
        "rededge3": (784.7, 20),
        "nir": (834.6, 114),
        "nir08": (865.6, 20),
        "nir09": (947.2, 20),
        "cirrus": (1372.2, 33),
        "swir16": (1612.0, 90),
        "swir22": (2191.3, 181),
    },
}

UNSUFFIXED_BAND_RESOLUTION: Final[dict[str, int]] = {
    "coastal": 60,
    "blue": 10,
    "green": 10,
    "red": 10,
    "rededge1": 20,
    "rededge2": 20,
    "rededge3": 20,
    "nir": 10,
    "nir08": 20,
    "nir09": 60,
    "cirrus": 60,
    "swir16": 20,
    "swir22": 20,
    "cloud": 20,
    "snow": 20,
}

BANDS_TO_ASSET_NAME: Final[dict[str, str]] = {
    "B01": "coastal",
    "B02": "blue",
    "B03": "green",
    "B04": "red",
    "B05": "rededge1",
    "B06": "rededge2",
    "B07": "rededge3",
    "B08": "nir",
    "B8A": "nir08",
    "B09": "nir09",
    "B10": "cirrus",
    "B11": "swir16",
    "B12": "swir22",
}

ASSET_TO_TITLE: Final[dict[str, str]] = {
    "coastal": "Coastal",
    "blue": "Blue",
    "green": "Green",
    "red": "Red",
    "rededge1": "Red Edge 1",
    "rededge2": "Red Edge 2",
    "rededge3": "Red Edge 3",
    "nir": "NIR 1",
    "nir08": "NIR 2",
    "nir09": "NIR 3",
    "cirrus": "Cirrus",
    "swir16": "SWIR 1.6μm",
    "swir22": "SWIR 2.2μm",
}

# L2A image path list (granule-relative, Sinergise S3 layout)
# The canonical, native-resolution L2A images: asset key -> granule-relative
# source path. These are the only images that get built into assets, COGified
# and published; ESA's other resolution variants are never considered. The
# basename of each path is also the filename used in the (flat) workdir and
# output prefix.
CANONICAL_L2A_IMAGE_PATHS: Final[dict[str, str]] = {
    "coastal": "R60m/B01.jp2",
    "blue": "R10m/B02.jp2",
    "green": "R10m/B03.jp2",
    "red": "R10m/B04.jp2",
    "rededge1": "R20m/B05.jp2",
    "rededge2": "R20m/B06.jp2",
    "rededge3": "R20m/B07.jp2",
    "nir": "R10m/B08.jp2",
    "nir08": "R20m/B8A.jp2",
    "nir09": "R60m/B09.jp2",
    "swir16": "R20m/B11.jp2",
    "swir22": "R20m/B12.jp2",
    "aot": "R20m/AOT.jp2",
    "wvp": "R20m/WVP.jp2",
    "scl": "R20m/SCL.jp2",
    "visual": "R10m/TCI.jp2",
    "cloud": "qi/CLD_20m.jp2",
    "snow": "qi/SNW_20m.jp2",
    "preview": "qi/L2A_PVI.jp2",
}

L1C_IMAGE_PATHS: Final[list[str]] = [
    "B01.jp2",
    "B02.jp2",
    "B03.jp2",
    "B04.jp2",
    "B05.jp2",
    "B06.jp2",
    "B07.jp2",
    "B08.jp2",
    "B8A.jp2",
    "B09.jp2",
    "B10.jp2",
    "B11.jp2",
    "B12.jp2",
    "TCI.jp2",
]

DEFAULT_SCALE: Final[float] = 0.0001

# EO and raster extension URIs bumped to v2.0.0 to match STAC 1.1.0 `bands` shape.
EO_EXT_V2: Final[str] = "https://stac-extensions.github.io/eo/v2.0.0/schema.json"
RASTER_EXT_V2: Final[str] = (
    "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
)

# Asset hrefs are published as public https URLs; the s3:// URL is kept as an
# alternate. Storage refs stay on the asset only.
ALTERNATE_ASSETS_EXT: Final[str] = (
    "https://stac-extensions.github.io/alternate-assets/v1.2.0/schema.json"
)
ALTERNATE_S3_KEY: Final[str] = "s3"
# alternate:name labels the access method on both the asset and its alternate.
ALTERNATE_HTTPS_NAME: Final[str] = "HTTPS"
ALTERNATE_S3_NAME: Final[str] = "S3"

# Rename maps: old eo/raster band dict keys → STAC 1.1.0 merged `bands` keys.
# EOBand.to_dict() uses the old unprefixed names; pystac.Band.from_dict() expects
# the 1.1 `eo:`/`raster:` prefixed names.
EO_BAND_RENAME: Final[dict[str, str]] = {
    "name": "name",
    "common_name": "eo:common_name",
    "center_wavelength": "eo:center_wavelength",
    "full_width_half_max": "eo:full_width_half_max",
    "solar_illumination": "eo:solar_illumination",
}
# Keys `bands[*]` are addressed by once merged onto a `pystac.Band` (via
# RASTER_BAND_RENAME below). task.py reads these back out of
# Band.extra_fields, so they're named here rather than duplicated as string
# literals in both files.
RASTER_NODATA_KEY: Final[str] = "nodata"
RASTER_SCALE_KEY: Final[str] = "raster:scale"
RASTER_OFFSET_KEY: Final[str] = "raster:offset"
RASTER_SPATIAL_RESOLUTION_KEY: Final[str] = "raster:spatial_resolution"

RASTER_BAND_RENAME: Final[dict[str, str]] = {
    "data_type": "data_type",
    "nodata": RASTER_NODATA_KEY,
    "unit": "unit",
    "statistics": "statistics",
    "spatial_resolution": RASTER_SPATIAL_RESOLUTION_KEY,
    "scale": RASTER_SCALE_KEY,
    "offset": RASTER_OFFSET_KEY,
    "sampling": "raster:sampling",
    "bits_per_sample": "raster:bits_per_sample",
}
