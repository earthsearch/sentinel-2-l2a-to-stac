import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Sequence

import numpy as np
import rasterio
from multiformats import multihash
from PIL import Image
from pystac import Asset, Item, MediaType
from rasterio.enums import ColorInterp, Resampling
from rasterio.rio.overview import get_maximum_overview_level
from stactask.exceptions import InvalidInput

from sentinel_2_l2a_to_stac.constants import (
    RASTER_NODATA_KEY,
    RASTER_OFFSET_KEY,
    RASTER_SCALE_KEY,
    RASTER_SPATIAL_RESOLUTION_KEY,
    STATISTICS_ASSET_KEYS,
)

THUMBNAIL_ASSET_NAME = "thumbnail"
THUMBNAIL_SOURCE_ASSET_NAME = "preview"
THUMBNAIL_TITLE = "Thumbnail of preview image"

# Decimal places kept on `statistics` values
STATISTICS_ROUNDING = 4

# Band tags that together make a complete stored `statistics` object.
STORED_STATISTICS_TAGS = frozenset(
    {
        "STATISTICS_MINIMUM",
        "STATISTICS_MAXIMUM",
        "STATISTICS_MEAN",
        "STATISTICS_STDDEV",
        "STATISTICS_VALID_PERCENT",
    }
)

ASSET_TO_RESAMPLE_ALGORITHM: dict[str | None, str] = {
    None: "AVERAGE",  # default case
    "scl": "MODE",
}

GSD_TO_BLOCKSIZE: dict[int | None, tuple[int, int]] = {
    None: (1024, 512),  # default case
    10: (1024, 512),
    20: (512, 256),
    60: (256, 128),
}


@dataclass(frozen=True)
class CogFile:
    """A COG written by :func:`cogify`, with its file info already computed."""

    path: Path
    size: int
    checksum: str
    # None for a non-statistics asset, or one with no valid pixels.
    statistics: dict[str, float] | None

    @property
    def filename(self) -> str:
        return self.path.name


class FileInfo(NamedTuple):
    """The ``file:size``/``file:checksum`` pair for one object."""

    size: int
    checksum: str


def sha256sum_multihash(filename: str) -> str:
    with open(filename, "rb") as f:
        return str(
            multihash.wrap(hashlib.file_digest(f, "sha256").digest(), "sha2-256").hex()
        )


def stream_file_info(chunks: Iterable[bytes]) -> FileInfo:
    """Measure a byte stream without ever holding the whole of it in memory."""
    digest = hashlib.sha256()
    size = 0
    for chunk in chunks:
        size += len(chunk)
        digest.update(chunk)
    return FileInfo(size, str(multihash.wrap(digest.digest(), "sha2-256").hex()))


def read_statistics(path: str | Path) -> dict[str, float] | None:
    """Exact band-1 statistics of a local or remote raster, in raw (unscaled) values.

    COGs written by :func:`write_cog` (and the legacy task) already store
    exact ``STATISTICS_*`` tags, which are used as-is (a header read). A file
    whose tags are missing, partial (e.g. no ``STATISTICS_VALID_PERCENT``,
    which GDAL < 3.2 never wrote) or approximate is instead computed exactly
    from a full-resolution read. Returns None for a band with no valid pixels;
    a failed read raises rasterio's own error rather than being guessed at.
    """
    with rasterio.open(path) as src:
        tags = src.tags(1)
        if tags.get("STATISTICS_APPROXIMATE", "").upper() != "YES":
            # GDAL stores only VALID_PERCENT=0 for an all-nodata band.
            if float(tags.get("STATISTICS_VALID_PERCENT", "nan")) == 0:
                return None
            if STORED_STATISTICS_TAGS <= tags.keys():
                return _rounded_statistics(
                    float(tags["STATISTICS_MINIMUM"]),
                    float(tags["STATISTICS_MAXIMUM"]),
                    float(tags["STATISTICS_MEAN"]),
                    float(tags["STATISTICS_STDDEV"]),
                    float(tags["STATISTICS_VALID_PERCENT"]),
                )
        return array_statistics(src.read(1, masked=True))


def array_statistics(band: np.ma.MaskedArray) -> dict[str, float] | None:
    """Exact `statistics` of one masked band; None when every pixel is masked."""
    valid = int(band.count())
    if valid == 0:
        return None
    return _rounded_statistics(
        float(band.min()),
        float(band.max()),
        float(band.mean(dtype=np.float64)),
        # Population stddev, as GDAL computes it.
        float(band.std(dtype=np.float64)),
        100.0 * valid / band.size,
    )


def _rounded_statistics(
    minimum: float, maximum: float, mean: float, stddev: float, valid_percent: float
) -> dict[str, float]:
    return {
        "minimum": round(minimum, STATISTICS_ROUNDING),
        "maximum": round(maximum, STATISTICS_ROUNDING),
        "mean": round(mean, STATISTICS_ROUNDING),
        "stddev": round(stddev, STATISTICS_ROUNDING),
        "valid_percent": round(valid_percent, STATISTICS_ROUNDING),
    }


def make_thumbnail(item: Item) -> Item:
    asset = item.assets[THUMBNAIL_SOURCE_ASSET_NAME]

    # remove existing thumbnail asset linking to preview.jpg/jp2
    if THUMBNAIL_ASSET_NAME in item.assets:
        del item.assets[THUMBNAIL_ASSET_NAME]

    # pystac 2.0 renamed `media_type` kwarg to `type`
    tn_asset = Asset(
        href=os.path.splitext(asset.href)[0] + ".jpg",
        type=MediaType.JPEG,
        roles=["thumbnail"],
        title=THUMBNAIL_TITLE,
    )
    item.assets[THUMBNAIL_ASSET_NAME] = tn_asset

    if not Path(tn_asset.href).exists():
        with Image.open(asset.href) as im:
            im.save(tn_asset.href, "JPEG", quality=75)

    return item


def write_cog(
    array: np.ndarray,
    fout: str | Path,
    crs: rasterio.CRS,
    transform: rasterio.Affine,
    width: int,
    height: int,
    count: int,
    blocksize: int,
    overview_blocksize: int,
    overview_resampling: str,
    nodata: int | float | None = None,
    scales: Sequence[float] | None = None,
    offsets: Sequence[float | int] | None = None,
    colorinterp: Sequence[ColorInterp] | None = None,
) -> None:
    """Write ``array`` as a COG with exact statistics stored for every band."""
    profile = {
        "driver": "COG",
        "dtype": array.dtype,
        "interleave": "pixel",
        "tiled": True,
        "crs": crs,
        "transform": transform,
        "width": width,
        "height": height,
        "count": count,
        "nodata": nodata,
        "BLOCKSIZE": blocksize,
        "COMPRESS": "DEFLATE",
        "BIGTIFF": os.getenv("BIGTIFF", "IF_SAFER"),
        "PREDICTOR": 2,
        # Build overviews with rasterio to match rio-cogeo's cog_translate() sizes.
        "OVERVIEWS": "FORCE_USE_EXISTING",
        "STATISTICS": True,
        # Max compression at the expense of one-time compute.
        "LEVEL": 12,
        "NUM_THREADS": "ALL_CPUS",
    }

    config = {
        "GDAL_TIFF_INTERNAL_MASK": os.getenv("GDAL_TIFF_INTERNAL_MASK", True),
        "GDAL_TIFF_OVR_BLOCKSIZE": str(overview_blocksize),
        "NUM_THREADS": "ALL_CPUS",
    }

    tags = {
        "OVR_RESAMPLING_ALG": overview_resampling,
    }

    overview_level = get_maximum_overview_level(width, height, minsize=blocksize)
    overviews: list[Any] = [2 ** (j + 1) for j in range(overview_level)]

    with rasterio.Env(**config):
        with rasterio.open(fout, mode="w", **profile) as dst:
            dst.scales = scales
            dst.offsets = offsets
            dst.colorinterp = colorinterp
            dst.update_tags(**tags)
            dst.write(array)
            dst.build_overviews(overviews, Resampling[overview_resampling.lower()])
            # Stores STATISTICS_* tags; a failure here only means a COG
            # without them, which read_statistics recomputes if it needs to.
            dst.stats()


def cogify(asset_name: str, asset: Asset) -> CogFile:
    """COGify the JP2 at ``asset.href``, writing a sibling ``.tif``.

    ``asset`` is only read from: it supplies the source href and the band
    metadata that determines scale/offset/nodata/blocksize, so callers can pass
    a throwaway asset built straight from the granule metadata.
    """
    infile = Path(asset.href)
    cogfile = infile.with_suffix(".tif")

    scales, offsets, nodatas, resolutions = get_band_scales_offsets_nodatas_resolutions(
        asset
    )

    if nodatas is None:
        nodata = None
    else:
        nodatas = list(set(nodatas))
        if len(nodatas) > 1:
            raise InvalidInput(
                "Differing nodata values per asset band is not supported"
            )
        nodata = nodatas[0]

    if resolutions is None:
        gsd = None
    else:
        resolutions = list(set(resolutions))
        if len(resolutions) > 1:
            raise InvalidInput(
                "Differing spatial resolutions per asset band is not supported"
            )
        gsd = resolutions[0]

    # The source is already on local disk, so read it in place rather than
    # copying the whole encoded file through a MemoryFile first.
    with rasterio.open(infile) as src:
        array = src.read()
        if scales is None:
            scales = src.scales
        if offsets is None:
            offsets = src.offsets
        src_crs = src.crs
        src_transform = src.transform
        src_width, src_height, src_count = src.width, src.height, src.count
        src_colorinterp = src.colorinterp

    blocksize, overview_blocksize = gsd_to_blocksize(gsd)
    overview_resampling = asset_name_to_resample_algorithm(asset_name)

    # Written straight to disk and hashed by streaming it back, so no copy of
    # the encoded COG is ever held in memory.
    cogfile_tmp = cogfile.with_name("." + cogfile.name + ".tmp")
    write_cog(
        array,
        cogfile_tmp,
        src_crs,
        src_transform,
        src_width,
        src_height,
        src_count,
        blocksize,
        overview_blocksize,
        overview_resampling,
        nodata,
        scales=scales,
        offsets=offsets,
        colorinterp=src_colorinterp,
    )
    cogfile_tmp.rename(cogfile)

    # Only statistics assets are measured, so a stats problem on e.g. SCL or
    # the visual COG can never fail the task.
    statistics = (
        read_statistics(cogfile) if asset_name in STATISTICS_ASSET_KEYS else None
    )

    return CogFile(
        path=cogfile,
        size=cogfile.stat().st_size,
        checksum=sha256sum_multihash(str(cogfile)),
        statistics=statistics,
    )


def get_band_scales_offsets_nodatas_resolutions(
    asset: Asset,
) -> tuple[
    list[int | float] | None,
    list[int | float] | None,
    list[float | None] | None,
    list[int | float | None] | None,
]:
    # SHIM(pystac-2.0): reads raster fields via band.extra_fields because pystac
    # 2.0-dev Band has no typed raster accessors yet. Replace with proper
    # attribute access once pystac stabilises the Band field API.
    bands = asset.bands
    if bands:
        # Multi-band: fields live in each Band's extra_fields, or hoisted to
        # asset.extra_fields when identical across all bands.
        asset_fields = asset.extra_fields
        scales, offsets, nodatas, resolutions = [], [], [], []
        for band in bands:
            bf = band.extra_fields
            af = asset_fields
            scale = (
                bf[RASTER_SCALE_KEY]
                if RASTER_SCALE_KEY in bf
                else af.get(RASTER_SCALE_KEY)
            )
            offset = (
                bf[RASTER_OFFSET_KEY]
                if RASTER_OFFSET_KEY in bf
                else af.get(RASTER_OFFSET_KEY)
            )
            nodata_raw = (
                bf[RASTER_NODATA_KEY]
                if RASTER_NODATA_KEY in bf
                else af.get(RASTER_NODATA_KEY)
            )
            resolution = (
                bf[RASTER_SPATIAL_RESOLUTION_KEY]
                if RASTER_SPATIAL_RESOLUTION_KEY in bf
                else af.get(RASTER_SPATIAL_RESOLUTION_KEY)
            )
            scales.append(scale or 1)
            offsets.append(offset or 0)
            nodatas.append(float(nodata_raw) if nodata_raw is not None else None)
            resolutions.append(resolution)
        return scales, offsets, nodatas, resolutions

    # Single-band: fields are merged directly onto asset.extra_fields.
    # Assets with no band info at all (metadata XML, thumbnail) won't have
    # any of these keys, so we return None in that case.
    ef = asset.extra_fields
    if RASTER_SPATIAL_RESOLUTION_KEY not in ef and RASTER_SCALE_KEY not in ef:
        return None, None, None, None

    nodata = ef.get(RASTER_NODATA_KEY)
    return (
        [ef.get(RASTER_SCALE_KEY) or 1],
        [ef.get(RASTER_OFFSET_KEY) or 0],
        [float(nodata) if nodata is not None else None],
        [ef.get(RASTER_SPATIAL_RESOLUTION_KEY)],
    )


def asset_name_to_resample_algorithm(asset_name: str) -> str:
    return ASSET_TO_RESAMPLE_ALGORITHM.get(
        asset_name,
        ASSET_TO_RESAMPLE_ALGORITHM[None],
    )


def gsd_to_blocksize(gsd: int | float | None) -> tuple[int, int]:
    return GSD_TO_BLOCKSIZE.get(
        None if gsd is None else int(gsd),
        GSD_TO_BLOCKSIZE[None],
    )
