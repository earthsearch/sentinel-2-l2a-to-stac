"""Valid-data footprint derived from the product's own rasters.

Replaces the Sinergise ``tileInfo.json`` ``tileDataGeometry``, which this task
no longer has access to. Every raster's valid (non-nodata) pixels are resampled
onto one common grid and stacked into a single mask that is 1 only where *all*
rasters have data; that mask is polygonized once and only its largest part
is kept.

The polygon is simplified in the projected CRS. Densification and reprojection
to WGS84 happen once, at the end, so the antimeridian is crossed exactly once
(by ``antimeridian.fix_shape`` downstream).
"""

from __future__ import annotations

import logging
from typing import Any, Final, Iterable, Optional

import numpy as np
import numpy.typing as npt
import rasterio
from affine import Affine
from raster_footprint import densify_geometry, reproject_geometry
from raster_footprint.mask import get_mask_geometry
from rasterio.coords import BoundingBox
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.windows import Window
from shapely.geometry import MultiPolygon
from shapely.geometry import mapping as shapely_mapping

from sentinel_2_l2a_to_stac.constants import COORD_ROUNDING

logger = logging.getLogger(__name__)

WGS84: Final[CRS] = CRS.from_epsg(4326)

# Maximum displacement, in projected CRS units (metres), of the simplified
# footprint. Matches the Sinergise extractor's value.
SIMPLIFY_TOLERANCE: Final[float] = 100.0

# Vertex spacing, in metres, inserted before reprojection so that straight UTM
# edges reproject as curves rather than chords.
DENSIFY_DISTANCE: Final[float] = 10_000.0

# Pixel size, in metres, of the stacked mask: the finest Sentinel-2 resolution.
STACK_RESOLUTION: Final[float] = 10.0

# Output rows read per pass; a multiple of 6 so 20 m and 60 m bands split on
# whole source pixels.
STACK_BAND_ROWS: Final[int] = 1536

# Sentinel-2 rasters use 0 for no-data; the source JP2s do not declare it.
DEFAULT_NODATA: Final[int] = 0


def data_footprint(hrefs: Iterable[str]) -> Optional[dict[str, Any]]:
    """Polygonize the pixels that hold valid data in every raster of `hrefs`.

    Rasters are consumed one at a time. Returns None when no pixel is valid in
    every raster, which leaves the caller free to fall back to the product
    metadata footprint.
    """
    stack: Optional[npt.NDArray[np.bool_]] = None
    transform: Optional[Affine] = None
    bounds: Optional[BoundingBox] = None
    source_crs: Optional[CRS] = None

    for href in hrefs:
        with rasterio.Env(NUM_THREADS="ALL_CPUS"), rasterio.open(href) as src:
            if stack is None:
                bounds, source_crs = src.bounds, src.crs
                width = round((bounds.right - bounds.left) / STACK_RESOLUTION)
                height = round((bounds.top - bounds.bottom) / STACK_RESOLUTION)
                transform = from_bounds(*bounds, width=width, height=height)
                stack = np.ones((height, width), dtype=bool)
            elif src.crs != source_crs or src.bounds != bounds:
                raise ValueError(
                    f"Raster {href} covers {src.bounds} in {src.crs}, but the "
                    f"footprint is being built over {bounds} in {source_crs}"
                )
            nodata = DEFAULT_NODATA if src.nodata is None else src.nodata
            scale = src.height / stack.shape[0]
            # Row bands keep the transient read to a fraction of the tile;
            # nearest-neighbour keeps 20 m and 60 m pixels as whole blocks.
            for row in range(0, stack.shape[0], STACK_BAND_ROWS):
                rows = min(STACK_BAND_ROWS, stack.shape[0] - row)
                window = Window(0, round(row * scale), src.width, round(rows * scale))
                band = src.read(
                    1,
                    window=window,
                    out_shape=(rows, stack.shape[1]),
                    resampling=Resampling.nearest,
                )
                stack[row : row + rows] &= band != nodata

    if stack is None or transform is None or source_crs is None or not stack.any():
        return None

    mask = stack.view(np.uint8)
    mask *= 255  # in place: a second tile-sized copy is avoided
    geometry = get_mask_geometry(mask, transform=transform, holes=False)
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, MultiPolygon):
        geometry = max(geometry.geoms, key=lambda part: part.area)

    simplified = geometry.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
    densified = densify_geometry(simplified, distance=DENSIFY_DISTANCE)
    reprojected = reproject_geometry(
        densified, source_crs, WGS84, precision=COORD_ROUNDING
    )
    simplified = reprojected.simplify(0.001, preserve_topology=True)
    return dict(shapely_mapping(simplified))
