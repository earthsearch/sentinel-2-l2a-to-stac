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

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from re import Pattern
from statistics import mean
from typing import Any, Final, Optional

import antimeridian
import pystac
import rasterio.transform
from pyproj import Transformer
from pystac.extensions.eo import Band as EOBand
from pystac.extensions.eo import EOExtension
from pystac.extensions.grid import GridExtension
from pystac.extensions.mgrs import MgrsExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import RasterExtension
from pystac.extensions.sat import OrbitState, SatExtension
from pystac.extensions.view import SCHEMA_URI as VIEW_EXT_URI
from pystac.extensions.view import ViewExtension
from pystac.provider import ProviderRole
from pystac.utils import map_opt, now_to_rfc3339_str, str_to_datetime
from shapely import remove_repeated_points
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape as shapely_shape
from shapely.ops import transform as shapely_transform
from shapely.validation import make_valid
from src.sentinel_2_l2a_to_stac.utils import XmlElement, ViewingAngle

logger = logging.getLogger(__name__)

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
TILEINFO_METADATA_ASSET_KEY: Final[str] = "tileinfo_metadata"

DEFAULT_TOLERANCE: Final[float] = 0.01
COORD_ROUNDING: Final[int] = 6

SENTINEL_BANDS: Final[dict[str, EOBand]] = {
    "coastal": EOBand.create(
        name="B01",
        common_name="coastal",
        center_wavelength=0.443,
        full_width_half_max=0.027,
    ),
    "blue": EOBand.create(
        name="B02",
        common_name="blue",
        center_wavelength=0.490,
        full_width_half_max=0.098,
    ),
    "green": EOBand.create(
        name="B03",
        common_name="green",
        center_wavelength=0.560,
        full_width_half_max=0.045,
    ),
    "red": EOBand.create(
        name="B04",
        common_name="red",
        center_wavelength=0.665,
        full_width_half_max=0.038,
    ),
    "rededge1": EOBand.create(
        name="B05",
        common_name="rededge",
        center_wavelength=0.704,
        full_width_half_max=0.019,
    ),
    "rededge2": EOBand.create(
        name="B06",
        common_name="rededge",
        center_wavelength=0.740,
        full_width_half_max=0.018,
    ),
    "rededge3": EOBand.create(
        name="B07",
        common_name="rededge",
        center_wavelength=0.783,
        full_width_half_max=0.028,
    ),
    "nir": EOBand.create(
        name="B08",
        common_name="nir",
        center_wavelength=0.842,
        full_width_half_max=0.145,
    ),
    "nir08": EOBand.create(
        name="B8A",
        common_name="nir08",
        center_wavelength=0.865,
        full_width_half_max=0.033,
    ),
    "nir09": EOBand.create(
        name="B09",
        common_name="nir09",
        center_wavelength=0.945,
        full_width_half_max=0.026,
    ),
    "cirrus": EOBand.create(
        name="B10",
        common_name="cirrus",
        center_wavelength=1.3735,
        full_width_half_max=0.075,
    ),
    "swir16": EOBand.create(
        name="B11",
        common_name="swir16",
        center_wavelength=1.610,
        full_width_half_max=0.143,
    ),
    "swir22": EOBand.create(
        name="B12",
        common_name="swir22",
        center_wavelength=2.190,
        full_width_half_max=0.242,
    ),
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
L2A_IMAGE_PATHS: Final[list[str]] = [
    "R10m/B04.jp2",
    "R10m/B03.jp2",
    "R10m/B02.jp2",
    "R10m/WVP.jp2",
    "R10m/AOT.jp2",
    "R10m/TCI.jp2",
    "R10m/B08.jp2",
    "R20m/B12.jp2",
    "R20m/B06.jp2",
    "R20m/B07.jp2",
    "R20m/B05.jp2",
    "R20m/B11.jp2",
    "R20m/B04.jp2",
    "R20m/B03.jp2",
    "R20m/B02.jp2",
    "R20m/WVP.jp2",
    "R20m/B8A.jp2",
    "R20m/SCL.jp2",
    "R20m/AOT.jp2",
    "R20m/TCI.jp2",
    "R20m/B08.jp2",
    "R60m/B12.jp2",
    "R60m/B06.jp2",
    "R60m/B07.jp2",
    "R60m/B05.jp2",
    "R60m/B11.jp2",
    "R60m/B04.jp2",
    "R60m/B01.jp2",
    "R60m/B03.jp2",
    "R60m/B02.jp2",
    "R60m/WVP.jp2",
    "R60m/B8A.jp2",
    "R60m/SCL.jp2",
    "R60m/AOT.jp2",
    "R60m/B09.jp2",
    "R60m/TCI.jp2",
    "R60m/B08.jp2",
    "qi/CLD_20m.jp2",
    "qi/SNW_20m.jp2",
    "qi/L2A_PVI.jp2",
]

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
_EO_EXT_V2: Final[str] = "https://stac-extensions.github.io/eo/v2.0.0/schema.json"
_RASTER_EXT_V2: Final[str] = (
    "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
)

# Rename maps: old eo/raster band dict keys → STAC 1.1.0 merged `bands` keys.
# EOBand.to_dict() uses the old unprefixed names; pystac.Band.from_dict() expects
# the 1.1 `eo:`/`raster:` prefixed names.
_EO_BAND_RENAME: Final[dict[str, str]] = {
    "name": "name",
    "common_name": "eo:common_name",
    "center_wavelength": "eo:center_wavelength",
    "full_width_half_max": "eo:full_width_half_max",
    "solar_illumination": "eo:solar_illumination",
}
# Keys `bands[*]` are addressed by once merged onto a `pystac.Band` (via
# _RASTER_BAND_RENAME below). task.py reads these back out of
# Band.extra_fields, so they're named here rather than duplicated as string
# literals in both files.
RASTER_NODATA_KEY: Final[str] = "nodata"
RASTER_SCALE_KEY: Final[str] = "raster:scale"
RASTER_OFFSET_KEY: Final[str] = "raster:offset"
RASTER_SPATIAL_RESOLUTION_KEY: Final[str] = "raster:spatial_resolution"

_RASTER_BAND_RENAME: Final[dict[str, str]] = {
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


def _native_band(
    eo: dict[str, Any] | None,
    raster: dict[str, Any] | None,
) -> pystac.Band:
    """Merge an EOBand dict + raster-fields dict into a STAC 1.1 pystac.Band."""
    merged: dict[str, Any] = {}
    for src, rename in ((eo, _EO_BAND_RENAME), (raster, _RASTER_BAND_RENAME)):
        if src:
            for k, v in src.items():
                merged[rename.get(k, k)] = v
    return pystac.Band.from_dict(merged)


def _apply_bands(asset: pystac.Asset, bands: list[pystac.Band]) -> None:
    """Apply band metadata to an asset.

    Single-band assets get fields merged directly into extra_fields (STAC 1.1.0
    canonical form for single-band). Multi-band assets use asset.bands so each
    band's position is unambiguous.
    """
    if len(bands) == 1:
        asset.extra_fields.update(bands[0].to_dict())
    else:
        asset.bands = bands


def _bump_band_extension_versions(item: pystac.Item) -> None:
    """Replace eo/raster extension URIs with the v2.0.0 schemas that define the
    STAC 1.1.0 `bands` field shape."""
    item.stac_extensions = [
        _EO_EXT_V2
        if "/eo/" in ext
        else _RASTER_EXT_V2
        if "/raster/" in ext
        else ext
        for ext in (item.stac_extensions or [])
    ]


# ---------------------------------------------------------------------------
# From stactools.core.projection — only transform_from_bbox is needed
# ---------------------------------------------------------------------------


def _transform_from_bbox(bbox: list[float], shape: list[int]) -> list[float]:
    return list(
        rasterio.transform.from_bounds(
            bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0]
        )
    )[:6]


# ---------------------------------------------------------------------------
# Inline: stactools-sentinel2 utils (extract_gsd, fix_z_values)
# ---------------------------------------------------------------------------

_GSD_PATTERN: Final[Pattern[str]] = re.compile(r"[_R](\d0)m")


def _extract_gsd(image_path: str) -> Optional[int]:
    match = _GSD_PATTERN.search(image_path)
    return int(match.group(1)) if match else None


def _fix_z_values(coord_values: list[str]) -> list[float]:
    if len(coord_values) % 3 == 0:
        third_position_is_zero = [
            x == "0" for i, x in enumerate(coord_values) if i % 3 == 2 and x
        ]
        if all(third_position_is_zero):
            return [float(c) for i, c in enumerate(coord_values) if i % 3 != 2]
    return [float(c) for c in coord_values if c]


# ---------------------------------------------------------------------------
# GranuleMetadata (from stactools-sentinel2 granule_metadata.py)
# ---------------------------------------------------------------------------

_BASELINE_PROCESSING: Final[Pattern[str]] = re.compile(r"_N(\d\d\.\d\d)")


class GranuleMetadataError(Exception):
    pass

class GranuleMetadata:
    def __init__(self, href: str) -> None:
        self.href = href
        self._root = XmlElement.from_file(href)

        self.tile_id = self._root.find_text_or_throw(
            "n1:General_Info/TILE_ID",
            lambda _: GranuleMetadataError(
                f"Cannot find granule tile_id granule metadata at {self.href}"
            ),
        )

        self._geocoding_node = self._root.find_or_throw(
            "n1:Geometric_Info/Tile_Geocoding",
            lambda _: GranuleMetadataError(
                f"Cannot find geocoding node in {self.href}"
            ),
        )

        self._tile_angles_node = self._root.find_or_throw(
            "n1:Geometric_Info/Tile_Angles",
            lambda _: GranuleMetadataError(
                f"Cannot find tile angles node in {self.href}"
            ),
        )

        self.viewing_angles = ViewingAngle.from_nodes(
            self._tile_angles_node.findall(
                "Mean_Viewing_Incidence_Angle_List/Mean_Viewing_Incidence_Angle"
            )
        )

        self._image_content_node = self._root.find_or_throw(
            "n1:Quality_Indicators_Info/Image_Content_QI",
            lambda _: GranuleMetadataError(
                f"Cannot find Image_Content_QI node in {self.href}"
            ),
        )

        self.resolution_to_shape: dict[int, tuple[int, int]] = {}
        for size_node in self._geocoding_node.findall("Size"):
            res = size_node.get_attr("resolution")
            if res is None:
                raise GranuleMetadataError("Size element does not have resolution.")
            nrows = map_opt(int, size_node.find_text("NROWS"))
            if nrows is None:
                raise GranuleMetadataError(
                    f"Could not get rows from size for resolution {res}"
                )
            ncols = map_opt(int, size_node.find_text("NCOLS"))
            if ncols is None:
                raise GranuleMetadataError(
                    f"Could not get columns from size for resolution {res}"
                )
            self.resolution_to_shape[int(res)] = (nrows, ncols)

        self.resolution_to_shape[320] = (
            int(self.resolution_to_shape[10][0] * 10 / 320),
            int(self.resolution_to_shape[10][1] * 10 / 320),
        )

    @property
    def epsg(self) -> Optional[int]:
        epsg_str = self._geocoding_node.find_text("HORIZONTAL_CS_CODE")
        return None if epsg_str is None else int(epsg_str.split(":")[1])

    @property
    def proj_bbox(self) -> list[float]:
        nrows, ncols = self.resolution_to_shape[10]
        geoposition = self._geocoding_node.find("Geoposition")
        if geoposition is None:
            raise GranuleMetadataError(f"Cannot find geoposition node in {self.href}")
        ulx = map_opt(float, geoposition.find_text("ULX"))
        if ulx is None:
            raise GranuleMetadataError("Could not get upper left X coordinate")
        uly = map_opt(float, geoposition.find_text("ULY"))
        if uly is None:
            raise GranuleMetadataError("Could not get upper left Y coordinate")
        return [ulx, uly - (10 * nrows), ulx + (10 * ncols), uly]

    @property
    def cloudiness_percentage(self) -> Optional[float]:
        return map_opt(
            float,
            self._image_content_node.find_text("CLOUDY_PIXEL_PERCENTAGE"),
        )

    @property
    def snow_ice_percentage(self) -> Optional[float]:
        return map_opt(
            float,
            self._image_content_node.find_text("SNOW_ICE_PERCENTAGE"),
        )

    @property
    def mean_solar_zenith(self) -> Optional[float]:
        return map_opt(
            float,
            self._tile_angles_node.find_text("Mean_Sun_Angle/ZENITH_ANGLE"),
        )

    @property
    def mean_solar_azimuth(self) -> Optional[float]:
        return map_opt(
            float,
            self._tile_angles_node.find_text("Mean_Sun_Angle/AZIMUTH_ANGLE"),
        )

    @property
    def metadata_dict(self) -> dict[str, Any]:
        icn = self._image_content_node
        properties: dict[str, Any] = {
            f"{s2_prefix}:tile_id": self.tile_id,
            f"{s2_prefix}:product_type": map_opt(
                float, icn.find_text("PRODUCT_TYPE")
            ),
            f"{s2_prefix}:orbit_state": map_opt(
                float, icn.find_text("SENSING_ORBIT_DIRECTION")
            ),
            f"{s2_prefix}:datatake_type": map_opt(
                float, icn.find_text("DATATAKE_TYPE")
            ),
            f"{s2_prefix}:generation_time": map_opt(
                float, icn.find_text("GENERATION_TIME")
            ),
            f"{s2_prefix}:relative_orbit": map_opt(
                float, icn.find_text("SENSING_ORBIT_NUMBER")
            ),
            f"{s2_prefix}:reflectance_conversion_factor": map_opt(
                float,
                icn.find_text(
                    "BOA_ADD_OFFSET_VALUES_LIST/Reflectance_Conversion/U"
                ),
            ),
            f"{s2_prefix}:degraded_msi_data_percentage": map_opt(
                float,
                icn.find_text("DEGRADED_MSI_DATA_PERCENTAGE"),
            ),
            f"{s2_prefix}:nodata_pixel_percentage": map_opt(
                float, icn.find_text("NODATA_PIXEL_PERCENTAGE")
            ),
            f"{s2_prefix}:saturated_defective_pixel_percentage": map_opt(
                float,
                icn.find_text("SATURATED_DEFECTIVE_PIXEL_PERCENTAGE"),
            ),
            f"{s2_prefix}:dark_features_percentage": map_opt(
                float, icn.find_text("DARK_FEATURES_PERCENTAGE")
            ),
            f"{s2_prefix}:cloud_shadow_percentage": map_opt(
                float, icn.find_text("CLOUD_SHADOW_PERCENTAGE")
            ),
            f"{s2_prefix}:vegetation_percentage": map_opt(
                float, icn.find_text("VEGETATION_PERCENTAGE")
            ),
            f"{s2_prefix}:not_vegetated_percentage": map_opt(
                float, icn.find_text("NOT_VEGETATED_PERCENTAGE")
            ),
            f"{s2_prefix}:water_percentage": map_opt(
                float, icn.find_text("WATER_PERCENTAGE")
            ),
            f"{s2_prefix}:unclassified_percentage": map_opt(
                float, icn.find_text("UNCLASSIFIED_PERCENTAGE")
            ),
            f"{s2_prefix}:medium_proba_clouds_percentage": map_opt(
                float,
                icn.find_text("MEDIUM_PROBA_CLOUDS_PERCENTAGE"),
            ),
            f"{s2_prefix}:high_proba_clouds_percentage": map_opt(
                float,
                icn.find_text("HIGH_PROBA_CLOUDS_PERCENTAGE"),
            ),
            f"{s2_prefix}:thin_cirrus_percentage": map_opt(
                float, icn.find_text("THIN_CIRRUS_PERCENTAGE")
            ),
            f"{s2_prefix}:snow_ice_percentage": map_opt(
                float, icn.find_text("SNOW_ICE_PERCENTAGE")
            ),
        }
        return {k: v for k, v in properties.items() if v is not None}

    @property
    def product_id(self) -> str:
        return self.tile_id

    @property
    def scene_id(self) -> str:
        id_parts = self.product_id.split("_")
        id_parts = [part for part in id_parts if not part.startswith("N")]
        return "_".join(id_parts)

    @property
    def platform(self) -> Optional[str]:
        if self.tile_id.startswith("S2A"):
            return "sentinel-2a"
        elif self.tile_id.startswith("S2B"):
            return "sentinel-2b"
        elif self.tile_id.startswith("S2C"):
            return "sentinel-2c"
        elif self.tile_id.startswith("S2D"):
            return "sentinel-2d"
        else:
            return None

    @property
    def processing_baseline(self) -> Optional[str]:
        mgrs_match = _BASELINE_PROCESSING.search(self.product_id)
        return mgrs_match.group(1) if mgrs_match else None

    @property
    def pvi_filename(self) -> Optional[str]:
        return self._root.find_text("n1:Quality_Indicators_Info/PVI_FILENAME")

    def create_asset(self) -> tuple[str, pystac.Asset]:
        asset = pystac.Asset(
            href=self.href, type=pystac.MediaType.XML, roles=["metadata"]
        )
        return GRANULE_METADATA_ASSET_KEY, asset


# ---------------------------------------------------------------------------
# ProductMetadata (from stactools-sentinel2 product_metadata.py)
# ---------------------------------------------------------------------------


class ProductMetadataError(Exception):
    pass


class ProductMetadata:
    def __init__(self, href: str) -> None:
        self.href = href
        self._root = XmlElement.from_file(href)

        self.product_info_node = self._root.find_or_throw(
            "n1:General_Info/Product_Info",
            lambda _: ProductMetadataError(
                f"Cannot find product info node for product metadata at {self.href}"
            ),
        )

        self.datatake_node = self.product_info_node.find_or_throw(
            "Datatake",
            lambda _: ProductMetadataError(
                f"Cannot find Datatake node in product metadata at {self.href}"
            ),
        )

        self.granule_node = self.product_info_node.find_or_throw(
            "Product_Organisation/Granule_List/Granule",
            lambda _: ProductMetadataError(
                f"Cannot find granule node in product metadata at {self.href}"
            ),
        )

        self.reflectance_conversion_node = self._root.find_or_throw(
            "n1:General_Info/Product_Image_Characteristics/Reflectance_Conversion",
            lambda _: ProductMetadataError(
                "Could not find reflectance conversion node in product metadata at "
                f"{self.href}"
            ),
        )

        self.qa_node = self._root.find_or_throw(
            "n1:Quality_Indicators_Info",
            lambda _: ProductMetadataError(
                f"Could not find QA node in product metadata at {self.href}"
            ),
        )

        self.boa_add_offset_values_list_node = self._root.find(
            "n1:General_Info/Product_Image_Characteristics/BOA_ADD_OFFSET_VALUES_LIST"
        )

        def _get_geometries() -> tuple[Any, Any]:
            geometric_info = self._root.find_or_throw(
                "n1:Geometric_Info",
                lambda _: ProductMetadataError(
                    f"Cannot find geometric info in product metadata at {self.href}"
                ),
            )
            footprint_text = geometric_info.find_text_or_throw(
                "Product_Footprint/Product_Footprint/Global_Footprint/EXT_POS_LIST",
                lambda _: ProductMetadataError(
                    f"Cannot parse footprint from product metadata at {self.href}"
                ),
            )
            footprint_coords = _fix_z_values(footprint_text.split(" "))
            footprint_points = [
                p[::-1]
                for p in list(
                    zip(
                        *[
                            iter(
                                round(coord, COORD_ROUNDING)
                                for coord in footprint_coords
                            )
                        ]
                        * 2
                    )
                )
            ]
            footprint_polygon = Polygon(footprint_points)
            geometry = shapely_mapping(footprint_polygon)
            bbox = footprint_polygon.bounds
            return bbox, geometry

        self.bbox, self.geometry = _get_geometries()

    @property
    def scene_id(self) -> str:
        product_id = self.product_id
        if not product_id.endswith(".SAFE"):
            raise ValueError(
                "Unexpected value found at "
                f"General_Info/Product_Info: {product_id}. "
                "This was expected to follow the sentinel 2 "
                "naming convention, including ending in .SAFE"
            )
        id_parts = self.product_id.split("_")
        sensor_id = id_parts[0]
        tile_id = id_parts[5].lstrip("T")

        datastrip_id = self.metadata_dict["s2:datastrip_id"].split("_")
        dt = datastrip_id[-2].lstrip("S")
        processing_level = datastrip_id[3]

        return f"{sensor_id}_T{tile_id}_{dt}_{processing_level}"

    @property
    def product_id(self) -> str:
        return self.product_info_node.find_text_or_throw(
            "PRODUCT_URI",
            lambda _: ValueError(
                f"Cannot determine product ID using product metadata at {self.href}"
            ),
        )

    @property
    def datetime(self) -> datetime:
        return str_to_datetime(
            self.product_info_node.find_text_or_throw(
                "PRODUCT_START_TIME",
                lambda _: ValueError(
                    "Cannot determine product start time using product metadata "
                    f"at {self.href}"
                ),
            )
        )

    @property
    def image_media_type(self) -> str:
        if self.granule_node.get_attr("imageFormat") == "GeoTIFF":
            return pystac.MediaType.COG
        else:
            return pystac.MediaType.JPEG2000

    @property
    def image_paths(self) -> list[str]:
        extension = ".tif" if self.image_media_type == pystac.MediaType.COG else ".jp2"
        return [f"{x.text}{extension}" for x in self.granule_node.findall("IMAGE_FILE")]

    @property
    def relative_orbit(self) -> Optional[int]:
        return map_opt(int, self.datatake_node.find_text("SENSING_ORBIT_NUMBER"))

    @property
    def orbit_state(self) -> Optional[str]:
        return self.datatake_node.find_text("SENSING_ORBIT_DIRECTION")

    @property
    def platform(self) -> Optional[str]:
        return self.datatake_node.find_text("SPACECRAFT_NAME")

    @property
    def metadata_dict(self) -> dict[str, Any]:
        result = {
            f"{s2_prefix}:product_uri": self.product_id,
            f"{s2_prefix}:generation_time": self.product_info_node.find_text(
                "GENERATION_TIME"
            ),
            f"{s2_prefix}:processing_baseline": self.product_info_node.find_text(
                "PROCESSING_BASELINE"
            ),
            f"{s2_prefix}:product_type": self.product_info_node.find_text(
                "PRODUCT_TYPE"
            ),
            f"{s2_prefix}:datatake_id": self.datatake_node.get_attr(
                "datatakeIdentifier"
            ),
            f"{s2_prefix}:datatake_type": self.datatake_node.find_text("DATATAKE_TYPE"),
            f"{s2_prefix}:datastrip_id": self.granule_node.get_attr(
                "datastripIdentifier"
            ),
            f"{s2_prefix}:tile_id": self.granule_node.get_attr("granuleIdentifier"),
            f"{s2_prefix}:reflectance_conversion_factor": map_opt(
                float, self.reflectance_conversion_node.find_text("U")
            ),
        }
        return {k: v for k, v in result.items() if v is not None}

    @property
    def boa_add_offsets(self) -> dict[str, int]:
        if self.boa_add_offset_values_list_node is not None:
            xs = {
                x.get_attr("band_id"): int(x.text)  # type: ignore[arg-type]
                for x in self.boa_add_offset_values_list_node.findall("BOA_ADD_OFFSET")
            }
            return {
                "B01": xs["0"],
                "B02": xs["1"],
                "B03": xs["2"],
                "B04": xs["3"],
                "B05": xs["4"],
                "B06": xs["5"],
                "B07": xs["6"],
                "B08": xs["7"],
                "B8A": xs["8"],
                "B09": xs["9"],
                "B10": xs["10"],
                "B11": xs["11"],
                "B12": xs["12"],
            }
        else:
            return {}

    def create_asset(self) -> tuple[str, pystac.Asset]:
        asset = pystac.Asset(
            href=self.href, type=pystac.MediaType.XML, roles=["metadata"]
        )
        return PRODUCT_METADATA_ASSET_KEY, asset


# ---------------------------------------------------------------------------
# TileInfoMetadata (from stactools-sentinel2 tileinfo_metadata.py)
# ---------------------------------------------------------------------------


class TileInfoMetadata:
    def __init__(self, href: str) -> None:
        self.href = href
        self.tileinfo: dict[str, Any] = json.loads(Path(href).read_text())

        self._datetime = str_to_datetime(self.tileinfo["timestamp"])
        self._geometry: Optional[dict[str, Any]] = self.tileinfo.get("tileDataGeometry")
        self._bbox: Optional[tuple[float, float, float, float]] = (
            shapely_shape(self._geometry).bounds if self._geometry else None
        )
        self._product_path: str = self.tileinfo["productPath"]

    @property
    def product_path(self) -> str:
        return self._product_path

    @property
    def geometry(self) -> Optional[dict[str, Any]]:
        return self._geometry

    @property
    def bbox(self) -> Optional[tuple[float, float, float, float]]:
        return self._bbox

    @property
    def datetime(self) -> datetime:
        return self._datetime

    @property
    def metadata_dict(self) -> dict[str, Any]:
        product_type = None
        product_name = self.tileinfo.get("productName")
        if product_name and "_MSIL2A_" in product_name:
            product_type = "S2MSI2A"
        elif product_name and "_MSIL1C_" in product_name:
            product_type = "S2MSI1C"
        result = {f"{s2_prefix}:product_type": product_type}
        return {k: v for k, v in result.items() if v is not None}

    def create_asset(self) -> tuple[str, pystac.Asset]:
        asset = pystac.Asset(
            href=self.href, type=pystac.MediaType.JSON, roles=["metadata"]
        )
        return TILEINFO_METADATA_ASSET_KEY, asset


# ---------------------------------------------------------------------------
# Metadata dataclass + create_item (from stactools-sentinel2 stac.py,
# granule/S3 path only)
# ---------------------------------------------------------------------------

_MGRS_PATTERN: Final[Pattern[str]] = re.compile(
    r"_T(\d{1,2})([CDEFGHJKLMNPQRSTUVWX])([ABCDEFGHJKLMNPQRSTUVWXYZ][ABCDEFGHJKLMNPQRSTUV])"
)

_TCI_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]TCI[_.]")
_AOT_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]AOT[_.]")
_WVP_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]WVP[_.]")
_SCL_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]SCL[_.]")
_CLD_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]CLD[_.]")
_SNW_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]SNW[_.]")

_BAND_PATTERN: Final[Pattern[str]] = re.compile(r"[_/](B\w{2})")
_IS_TCI_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]TCI")
_IS_PVI_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]PVI")
_BAND_ID_PATTERN: Final[Pattern[str]] = re.compile(r"[_/](B\d[A\d])")

_RGB_BANDS: Final[list[EOBand]] = [
    SENTINEL_BANDS["red"],
    SENTINEL_BANDS["green"],
    SENTINEL_BANDS["blue"],
]


@dataclass(frozen=True)
class Metadata:
    scene_id: str
    cloudiness_percentage: Optional[float]
    snow_ice_percentage: Optional[float]
    extra_assets: dict[str, pystac.Asset]
    geometry: dict[str, Any]
    datetime: datetime
    platform: str
    metadata_dict: dict[str, Any]
    image_media_type: str
    image_paths: list[str]
    epsg: int
    proj_bbox: list[float]
    resolution_to_shape: dict[int, tuple[int, int]]
    processing_baseline: str
    viewing_angles: dict[str, ViewingAngle]
    orbit_state: Optional[str] = None
    relative_orbit: Optional[int] = None
    sun_azimuth: Optional[float] = None
    sun_zenith: Optional[float] = None
    boa_add_offsets: Optional[dict[str, int]] = None


def _offset_for_pb(processing_baseline: str) -> float:
    return 0 if processing_baseline < "04.00" else -0.1


def _raster_band_fields(
    boa_add_offsets: Optional[dict[str, int]],
    processing_baseline: str,
    band_id: str,
    resolution: float,
) -> dict[str, Any]:
    offset = (
        round(boa_add_offsets[band_id] * DEFAULT_SCALE, 6)
        if boa_add_offsets
        else _offset_for_pb(processing_baseline)
    )
    return {
        "nodata": 0,
        "spatial_resolution": resolution,
        "data_type": "uint16",
        "scale": DEFAULT_SCALE,
        "offset": offset,
    }


def _highest_asset_res(band_id: str) -> int:
    return UNSUFFIXED_BAND_RESOLUTION[BANDS_TO_ASSET_NAME[band_id]]


def _band_from_band_id(band_id: str) -> EOBand:
    return SENTINEL_BANDS[BANDS_TO_ASSET_NAME[band_id]]


def _mk_asset_id(maybe_res: Optional[int], name: str) -> str:
    return f"{name.lower()}_{maybe_res}m" if maybe_res and maybe_res != 20 else name


def _set_asset_properties(
    asset: pystac.Asset,
    resolution: int,
    shape: tuple[int, int],
    proj_bbox_10m: list[float],
    gsd: Optional[int] = None,
) -> pystac.Asset:
    if gsd:
        pystac.CommonMetadata(asset).gsd = gsd
    asset_projection = ProjectionExtension.ext(asset)
    asset_projection.shape = list(shape)
    bbox = [
        proj_bbox_10m[0],
        proj_bbox_10m[3] - resolution * shape[1],
        proj_bbox_10m[0] + resolution * shape[0],
        proj_bbox_10m[3],
    ]
    asset_projection.transform = _transform_from_bbox(
        bbox, list(shape)
    )
    return asset


def _image_asset_from_href(
    asset_href: str,
    resolution_to_shape: dict[int, tuple[int, int]],
    proj_bbox: list[float],
    media_type: Optional[str],
    processing_baseline: str,
    boa_add_offsets: Optional[dict[str, int]] = None,
) -> tuple[str, pystac.Asset]:
    logger.debug(f"Creating asset for image {asset_href}")

    _, ext = os.path.splitext(asset_href)
    if media_type is not None:
        asset_media_type = media_type
    else:
        if ext.lower() == ".jp2":
            asset_media_type = pystac.MediaType.JPEG2000
        elif ext.lower() in [".tiff", ".tif"]:
            asset_media_type = pystac.MediaType.GEOTIFF
        else:
            raise Exception(f"Must supply a media type for asset : {asset_href}")

    maybe_res = _extract_gsd(asset_href)
    resolution = maybe_res
    if resolution is None:
        band_id_search = _BAND_PATTERN.search(asset_href)
        if band_id_search:
            resolution = _highest_asset_res(band_id_search.group(1))
        elif _IS_TCI_PATTERN.search(asset_href):
            resolution = 10
        elif _IS_PVI_PATTERN.search(asset_href):
            resolution = 320
        else:
            raise ValueError(f"Could not determine resolution for {asset_href}")

    shape = resolution_to_shape[int(resolution)]

    _raster_uint8 = {
        "nodata": 0, "spatial_resolution": resolution, "data_type": "uint8"
    }

    if "_PVI" in asset_href:
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="True color preview",
            roles=["overview"],
        )
        _set_asset_properties(asset, resolution, shape, proj_bbox, resolution)
        _apply_bands(asset, [_native_band(b.to_dict(), None) for b in _RGB_BANDS])
        return "preview", asset

    band_id_search = _BAND_ID_PATTERN.search(asset_href)
    if band_id_search:
        try:
            band_id = band_id_search.group(1)
            asset_res = resolution
        except KeyError:
            band_id = os.path.splitext(asset_href)[0].split("_")[-1]
            asset_res = _highest_asset_res(band_id_search.group(1))

        band_gsd: Optional[int] = None
        if asset_res == _highest_asset_res(band_id):
            asset_id = BANDS_TO_ASSET_NAME[band_id]
            band_gsd = asset_res
        else:
            asset_id = f"{BANDS_TO_ASSET_NAME[band_id]}_{int(asset_res)}m"

        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title=f"{ASSET_TO_TITLE[asset_id.split('_')[0]]} - {asset_res}m",
            roles=["data", "reflectance"],
        )
        _apply_bands(
            asset,
            [
                _native_band(
                    _band_from_band_id(band_id).to_dict(),
                    _raster_band_fields(
                        boa_add_offsets, processing_baseline, band_id, resolution
                    ),
                )
            ],
        )
        _set_asset_properties(asset, resolution, shape, proj_bbox, band_gsd)

    elif _TCI_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="True color image",
            roles=["visual"],
        )
        _apply_bands(asset, [_native_band(b.to_dict(), _raster_uint8) for b in _RGB_BANDS])
        asset_id = f"visual_{maybe_res}m" if maybe_res and maybe_res != 10 else "visual"
        _set_asset_properties(asset, resolution, shape, proj_bbox, maybe_res)

    elif _AOT_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="Aerosol optical thickness (AOT)",
            roles=["data"],
        )
        asset_id = _mk_asset_id(maybe_res, "aot")
        _set_asset_properties(asset, resolution, shape, proj_bbox, maybe_res)
        _apply_bands(
            asset,
            [
                _native_band(
                    None,
                    {
                        "nodata": 0,
                        "spatial_resolution": resolution,
                        "data_type": "uint16",
                        "scale": 0.001,
                        "offset": 0,
                    },
                )
            ],
        )

    elif _WVP_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="Water Vapour (WVP)",
            roles=["data"],
        )
        asset_id = _mk_asset_id(maybe_res, "wvp")
        _set_asset_properties(asset, resolution, shape, proj_bbox, maybe_res)
        _apply_bands(
            asset,
            [
                _native_band(
                    None,
                    {
                        "nodata": 0,
                        "spatial_resolution": resolution,
                        "data_type": "uint16",
                        "unit": "cm",
                        "scale": 0.001,
                        "offset": 0,
                    },
                )
            ],
        )

    elif _SCL_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="Scene classification map (SCL)",
            roles=["data"],
        )
        asset_id = _mk_asset_id(maybe_res, "scl")
        _set_asset_properties(asset, resolution, shape, proj_bbox, maybe_res)
        _apply_bands(asset, [_native_band(None, _raster_uint8)])

    elif _CLD_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="Cloud Probabilities",
            roles=["data", "cloud"],
        )
        asset_id = _mk_asset_id(maybe_res, "cloud")
        _set_asset_properties(asset, resolution, shape, proj_bbox, maybe_res)
        _apply_bands(asset, [_native_band(None, _raster_uint8)])

    elif _SNW_PATTERN.search(asset_href):
        asset = pystac.Asset(
            href=asset_href,
            type=asset_media_type,
            title="Snow Probabilities",
            roles=["data", "snow-ice"],
        )
        asset_id = _mk_asset_id(maybe_res, "snow")
        _set_asset_properties(asset, resolution, shape, proj_bbox)
        _apply_bands(asset, [_native_band(None, _raster_uint8)])

    else:
        raise ValueError(f"Unexpected asset: {asset_href}")

    return asset_id, asset


def _make_valid_geometry(input_geometry: dict[str, Any]) -> Polygon | MultiPolygon:
    shapely_geometry = shapely_shape(antimeridian.fix_shape(input_geometry))
    geometry = make_valid(shapely_geometry)

    if geometry.geom_type == "GeometryCollection":
        geometry = next(
            filter(lambda x: x.geom_type in ["Polygon", "MultiPolygon"], geometry.geoms)
        )

    geometry = remove_repeated_points(geometry)

    if (ga := geometry.area) > 100:
        raise Exception(f"Area of geometry is {ga}, which is too large to be correct.")

    return geometry


def _metadata_from_granule_metadata(
    granule_href: str,
    tolerance: float,
    allow_fallback_geometry: bool,
) -> Metadata:
    granule_metadata = GranuleMetadata(
        os.path.join(granule_href, "metadata.xml")
    )
    tileinfo_metadata = TileInfoMetadata(
        os.path.join(granule_href, "tileInfo.json")
    )

    product_metadata: Optional[ProductMetadata] = None
    f = os.path.join(granule_href, "product_metadata.xml")
    if os.path.exists(f):
        product_metadata = ProductMetadata(f)

    if tileinfo_metadata.geometry and (
        (cs := tileinfo_metadata.geometry.get("coordinates")) and (all(cs))
    ):
        transformer = Transformer.from_crs(
            granule_metadata.epsg, 4326, force_over=True, always_xy=True
        )
        geometry: dict[str, Any] = shapely_mapping(
            shapely_transform(
                transformer.transform, shapely_shape(tileinfo_metadata.geometry)
            ).simplify(tolerance)
        )
    elif allow_fallback_geometry and product_metadata:
        geometry = product_metadata.geometry
    else:
        raise ValueError(
            f"Metadata does not contain geometry for {granule_href}. "
            "Perhaps there is no data in the scene?"
        )

    extra_assets: dict[str, pystac.Asset] = dict(
        [
            granule_metadata.create_asset(),
            tileinfo_metadata.create_asset(),
        ]
    )
    if product_metadata:
        key, asset = product_metadata.create_asset()
        extra_assets[key] = asset

    image_paths = (
        L2A_IMAGE_PATHS if "_L2A_" in granule_metadata.scene_id else L1C_IMAGE_PATHS
    )

    metadata_dict: dict[str, Any] = {
        **granule_metadata.metadata_dict,
        **tileinfo_metadata.metadata_dict,
        f"{s2_prefix}:processing_baseline": granule_metadata.processing_baseline,
    }
    if product_metadata is not None:
        metadata_dict.update(**product_metadata.metadata_dict)

    return Metadata(
        scene_id=(
            granule_metadata.scene_id
            if product_metadata is None
            else product_metadata.scene_id
        ),
        extra_assets=extra_assets,
        metadata_dict=metadata_dict,
        cloudiness_percentage=granule_metadata.cloudiness_percentage,
        snow_ice_percentage=granule_metadata.snow_ice_percentage,
        epsg=granule_metadata.epsg,  # type: ignore[arg-type]
        proj_bbox=granule_metadata.proj_bbox,
        resolution_to_shape=granule_metadata.resolution_to_shape,
        geometry=geometry,
        datetime=tileinfo_metadata.datetime,
        platform=granule_metadata.platform,  # type: ignore[arg-type]
        image_media_type=pystac.MediaType.JPEG2000,
        image_paths=image_paths,
        sun_zenith=granule_metadata.mean_solar_zenith,
        sun_azimuth=granule_metadata.mean_solar_azimuth,
        processing_baseline=granule_metadata.processing_baseline,  # type: ignore[arg-type]
        boa_add_offsets=product_metadata.boa_add_offsets if product_metadata else None,
        viewing_angles=granule_metadata.viewing_angles,
    )


def create_item(
    granule_href: str,
    tolerance: float = DEFAULT_TOLERANCE,
    allow_fallback_geometry: bool = True,
) -> pystac.Item:
    """Create a STAC Item from a Sentinel-2 L2A granule (Sinergise S3 layout).

    Args:
        granule_href: Local path to the directory containing metadata.xml,
            tileInfo.json, and product_metadata.xml.
        tolerance: Geometry simplification tolerance.
        allow_fallback_geometry: Use product_metadata.xml footprint when
            tileInfo.json has no data geometry.

    Returns:
        A pystac.Item representing the Sentinel-2 scene.
    """
    metadata = _metadata_from_granule_metadata(
        granule_href, tolerance, allow_fallback_geometry
    )

    geometry = _make_valid_geometry(metadata.geometry)

    bbox = [round(v, COORD_ROUNDING) for v in antimeridian.bbox(geometry)]

    item = pystac.Item(
        id=metadata.scene_id,
        geometry=shapely_mapping(geometry),
        bbox=bbox,
        datetime=metadata.datetime,
        properties={"created": now_to_rfc3339_str()},
    )

    item.properties["platform"] = metadata.platform.lower()
    item.properties["constellation"] = SENTINEL_CONSTELLATION
    item.properties["instruments"] = SENTINEL_INSTRUMENTS

    eo = EOExtension.ext(item, add_if_missing=True)
    eo.cloud_cover = metadata.cloudiness_percentage
    RasterExtension.add_to(item)

    if metadata.orbit_state or metadata.relative_orbit:
        sat = SatExtension.ext(item, add_if_missing=True)
        sat.orbit_state = (
            OrbitState(metadata.orbit_state.lower()) if metadata.orbit_state else None
        )
        sat.relative_orbit = metadata.relative_orbit

    projection = ProjectionExtension.ext(item, add_if_missing=True)
    projection.epsg = metadata.epsg
    if projection.epsg is None:
        raise ValueError(
            f"Could not determine EPSG code for {granule_href}; which is required."
        )

    assert item.geometry is not None
    centroid = antimeridian.centroid(item.geometry)
    projection.centroid = {"lat": round(centroid.y, 5), "lon": round(centroid.x, 5)}

    mgrs_match = _MGRS_PATTERN.search(metadata.scene_id)
    if mgrs_match and len(mgrs_groups := mgrs_match.groups()) == 3:
        mgrs = MgrsExtension.ext(item, add_if_missing=True)
        mgrs.apply(
            utm_zone=int(mgrs_groups[0]),
            latitude_band=mgrs_groups[1],
            grid_square=mgrs_groups[2],
        )
        grid = GridExtension.ext(item, add_if_missing=True)
        grid.code = f"MGRS-{mgrs.utm_zone:02}{mgrs.latitude_band}{mgrs.grid_square}"
    else:
        logger.error(
            "Error populating MGRS and Grid Extensions fields from ID: "
            f"{metadata.scene_id}"
        )

    view = ViewExtension.ext(item, add_if_missing=True)

    if all(not math.isnan(v.azimuth) for v in metadata.viewing_angles.values()):
        view.azimuth = mean([v.azimuth for v in metadata.viewing_angles.values()])

    if all(not math.isnan(v.zenith) for v in metadata.viewing_angles.values()):
        view.incidence_angle = mean(
            [v.zenith for v in metadata.viewing_angles.values()]
        )

    if (msa := metadata.sun_azimuth) and not math.isnan(msa):
        view.sun_azimuth = msa

    if (msz := metadata.sun_zenith) and not math.isnan(msz):
        view.sun_elevation = 90 - msz

    if all(
        x is None
        for x in [
            view.azimuth,
            view.incidence_angle,
            view.sun_azimuth,
            view.sun_elevation,
        ]
    ):
        if item.stac_extensions is not None:
            item.stac_extensions.remove(VIEW_EXT_URI)

    if item.stac_extensions is not None:
        item.stac_extensions.append(SENTINEL2_EXTENSION_SCHEMA)
    item.properties.update(metadata.metadata_dict)

    image_assets = dict(
        [
            _image_asset_from_href(
                asset_href=os.path.join(granule_href, image_path),
                resolution_to_shape=metadata.resolution_to_shape,
                proj_bbox=metadata.proj_bbox,
                media_type=metadata.image_media_type,
                processing_baseline=metadata.processing_baseline,
                boa_add_offsets=metadata.boa_add_offsets,
            )
            for image_path in metadata.image_paths
        ]
    )

    for key, asset in chain(image_assets.items(), metadata.extra_assets.items()):
        assert key not in item.assets
        item.assets[key] = asset
        asset.set_owner(item)

    _bump_band_extension_versions(item)

    return item
