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
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from re import Pattern
from typing import Any, Final, Optional

import pystac
from pyproj import Transformer
from pystac.utils import map_opt, str_to_datetime
from shapely.geometry import Polygon
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape as shapely_shape
from shapely.ops import transform as shapely_transform

from sentinel_2_l2a_to_stac.constants import (
    COORD_ROUNDING,
    GRANULE_METADATA_ASSET_KEY,
    L1C_IMAGE_PATHS,
    L2A_IMAGE_PATHS,
    PRODUCT_METADATA_ASSET_KEY,
    TILEINFO_METADATA_ASSET_KEY,
    s2_prefix,
)
from sentinel_2_l2a_to_stac.utils import ViewingAngle, XmlElement


def _fix_z_values(coord_values: list[str]) -> list[float]:
    if len(coord_values) % 3 == 0:
        third_position_is_zero = [
            x == "0" for i, x in enumerate(coord_values) if i % 3 == 2 and x
        ]
        if all(third_position_is_zero):
            return [float(c) for i, c in enumerate(coord_values) if i % 3 != 2]
    return [float(c) for c in coord_values if c]


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
            f"{s2_prefix}:product_type": map_opt(float, icn.find_text("PRODUCT_TYPE")),
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
                icn.find_text("BOA_ADD_OFFSET_VALUES_LIST/Reflectance_Conversion/U"),
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

    def create_asset(self) -> tuple[str, pystac.Asset]:
        asset = pystac.Asset(
            href=self.href, type=pystac.MediaType.XML, roles=["metadata"]
        )
        return GRANULE_METADATA_ASSET_KEY, asset


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


def parse_metadata(
    granule_href: str,
    tolerance: float,
    allow_fallback_geometry: bool,
) -> Metadata:
    granule_metadata = GranuleMetadata(os.path.join(granule_href, "metadata.xml"))
    tileinfo_metadata = TileInfoMetadata(os.path.join(granule_href, "tileInfo.json"))

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
        image_media_type=product_metadata.image_media_type
        if product_metadata is not None
        else pystac.MediaType.JPEG2000,
        image_paths=image_paths,
        sun_zenith=granule_metadata.mean_solar_zenith,
        sun_azimuth=granule_metadata.mean_solar_azimuth,
        processing_baseline=granule_metadata.processing_baseline,  # type: ignore[arg-type]
        boa_add_offsets=product_metadata.boa_add_offsets if product_metadata else None,
        viewing_angles=granule_metadata.viewing_angles,
    )
