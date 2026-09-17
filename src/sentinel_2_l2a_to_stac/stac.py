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

import logging
import math
import os
import re
from itertools import chain
from re import Pattern
from statistics import mean
from typing import Any, Final, Optional

import antimeridian
import pystac
import rasterio.transform
from pystac.extensions.eo import Band as EOBand
from pystac.extensions.eo import EOExtension
from pystac.extensions.grid import GridExtension
from pystac.extensions.mgrs import MgrsExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import RasterExtension
from pystac.extensions.sat import OrbitState, SatExtension
from pystac.extensions.view import SCHEMA_URI as VIEW_EXT_URI
from pystac.extensions.view import ViewExtension
from pystac.utils import now_to_rfc3339_str
from shapely import remove_repeated_points
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape as shapely_shape
from shapely.validation import make_valid

from sentinel_2_l2a_to_stac.constants import (
    ASSET_TO_TITLE,
    BANDS_TO_ASSET_NAME,
    COORD_ROUNDING,
    DEFAULT_SCALE,
    DEFAULT_TOLERANCE,
    EO_BAND_RENAME,
    EO_EXT_V2,
    RASTER_BAND_RENAME,
    RASTER_EXT_V2,
    SENTINEL2_EXTENSION_SCHEMA,
    SENTINEL_BANDS,
    SENTINEL_CONSTELLATION,
    SENTINEL_INSTRUMENTS,
    UNSUFFIXED_BAND_RESOLUTION,
)
from sentinel_2_l2a_to_stac.metadata import parse_metadata

logger = logging.getLogger(__name__)


def _transform_from_bbox(bbox: list[float], shape: list[int]) -> list[float]:
    return list(
        rasterio.transform.from_bounds(
            bbox[0], bbox[1], bbox[2], bbox[3], shape[1], shape[0]
        )
    )[:6]


_GSD_PATTERN: Final[Pattern[str]] = re.compile(r"[_R](\d0)m")


def _extract_gsd(image_path: str) -> Optional[int]:
    match = _GSD_PATTERN.search(image_path)
    return int(match.group(1)) if match else None


def _native_band(
    eo: dict[str, Any] | None,
    raster: dict[str, Any] | None,
) -> pystac.Band:
    """Merge an EOBand dict + raster-fields dict into a STAC 1.1 pystac.Band."""
    merged: dict[str, Any] = {}
    for src, rename in ((eo, EO_BAND_RENAME), (raster, RASTER_BAND_RENAME)):
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
        EO_EXT_V2 if "/eo/" in ext else RASTER_EXT_V2 if "/raster/" in ext else ext
        for ext in (item.stac_extensions or [])
    ]


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
    asset_projection.transform = _transform_from_bbox(bbox, list(shape))
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
        "nodata": 0,
        "spatial_resolution": resolution,
        "data_type": "uint8",
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
        _apply_bands(
            asset, [_native_band(b.to_dict(), _raster_uint8) for b in _RGB_BANDS]
        )
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
    metadata = parse_metadata(granule_href, tolerance, allow_fallback_geometry)

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
