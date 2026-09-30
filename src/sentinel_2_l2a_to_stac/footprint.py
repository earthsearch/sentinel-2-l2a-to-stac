"""Valid-data footprint derived from the product's own rasters.

Replaces the Sinergise ``tileInfo.json`` ``tileDataGeometry``, which this task
no longer has access to. Each raster contributes the polygon surrounding its
valid (non-nodata) pixels and the product footprint is the union of them all.

Polygonize the data mask, simplify it in the projected CRS. Union, densification
and reprojection to WGS84 happen once, at the end, so the antimeridian is crossed
exactly once (by ``antimeridian.fix_shape`` downstream) rather than per raster.
"""

from __future__ import annotations

import logging
from typing import Any, Final, Iterable, Optional

import rasterio
import shapely
from raster_footprint import densify_geometry, footprint_from_data, reproject_geometry
from rasterio.crs import CRS
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry

from sentinel_2_l2a_to_stac.constants import COORD_ROUNDING

logger = logging.getLogger(__name__)

WGS84: Final[CRS] = CRS.from_epsg(4326)

# Maximum displacement, in projected CRS units (metres), of the simplified
# per-raster footprint. Matches the Sinergise extractor's value.
SIMPLIFY_TOLERANCE: Final[float] = 100.0

# Vertex spacing, in metres, inserted before reprojection so that straight UTM
# edges reproject as curves rather than chords.
DENSIFY_DISTANCE: Final[float] = 10_000.0

# Sentinel-2 rasters use 0 for no-data; the source JP2s do not declare it.
DEFAULT_NODATA: Final[int] = 0


def raster_footprint(href: str) -> tuple[Optional[BaseGeometry], Optional[CRS]]:
    """Extract one raster's valid-data footprint, in that raster's own CRS."""
    with (
        rasterio.Env(NUM_THREADS="ALL_CPUS"),
        rasterio.open(href) as src,
    ):
        data = src.read(1)
        footprint = footprint_from_data(
            data,
            src.transform,
            src.crs,
            destination_crs=src.crs,
            nodata=DEFAULT_NODATA if src.nodata is None else src.nodata,
            precision=COORD_ROUNDING,
            simplify_tolerance=SIMPLIFY_TOLERANCE,
        )
        crs = src.crs
    if footprint is None:
        return None, crs
    return shapely_shape(footprint), crs


def data_footprint(hrefs: Iterable[str]) -> Optional[dict[str, Any]]:
    """Union the valid-data footprints of `hrefs` into one WGS84 geometry.

    Returns None when none of the rasters contain valid data, which leaves the
    caller free to fall back to the product metadata footprint.
    """
    geometries: list[BaseGeometry] = []
    source_crs: Optional[CRS] = None

    for href in hrefs:
        geometry, crs = raster_footprint(href)
        if geometry is None or geometry.is_empty:
            logger.info(f"No valid data found in {href}, skipping")
            continue
        if source_crs is None:
            source_crs = crs
        elif crs != source_crs:
            raise ValueError(
                f"Raster {href} is in {crs}, but the footprint is being built "
                f"in {source_crs}"
            )
        geometries.append(geometry)

    if not geometries or source_crs is None:
        return None

    union = shapely.union_all(geometries)
    densified = densify_geometry(union, distance=DENSIFY_DISTANCE)
    reprojected = reproject_geometry(
        densified, source_crs, WGS84, precision=COORD_ROUNDING
    )
    return dict(shapely_mapping(reprojected))
