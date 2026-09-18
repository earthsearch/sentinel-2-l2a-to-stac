import hashlib
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import rasterio
from multiformats import multihash
from PIL import Image
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from rasterio.enums import ColorInterp, Resampling
from rasterio.rio.overview import get_maximum_overview_level
from stactask.exceptions import InvalidInput

from sentinel_2_l2a_to_stac.constants import (
    RASTER_NODATA_KEY,
    RASTER_OFFSET_KEY,
    RASTER_SCALE_KEY,
    RASTER_SPATIAL_RESOLUTION_KEY,
)

# SHIM(pystac-2.0): stac-asset reads asset.media_type when downloading but pystac
# 2.0 renamed the field to Asset.type. Remove once stac-asset is updated.
if not hasattr(Asset, "media_type"):
    Asset.media_type = property(lambda self: self.type)  # type: ignore[attr-defined]


THUMBNAIL_ASSET_NAME = "thumbnail"
THUMBNAIL_SOURCE_ASSET_NAME = "preview"

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


def sha256sum_multihash(filename: str) -> str:
    with open(filename, "rb") as f:
        return str(
            multihash.wrap(hashlib.file_digest(f, "sha256").digest(), "sha2-256").hex()
        )


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
        title="Thumbnail of preview image",
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
            dst.stats()


def cogify(asset_name: str, asset: Asset) -> None:
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

    # Reading the file into a MemoryFile is slightly more performant
    with rasterio.MemoryFile() as mem_src:
        mem_src.write(infile.read_bytes())

        with mem_src.open(mode="r") as src:
            array = src.read()
            if scales is None:
                scales = src.scales
            if offsets is None:
                offsets = src.offsets
            src_colorinterp = src.colorinterp

    blocksize, overview_blocksize = gsd_to_blocksize(gsd)
    overview_resampling = asset_name_to_resample_algorithm(asset_name)

    shasum = hashlib.sha256()
    # Use a MemoryFile for the output so we can hash while writing to disk
    # instead of opening and re-reading the file afterward.
    with rasterio.MemoryFile() as mem_dst:
        write_cog(
            array,
            mem_dst.name,
            src.crs,
            src.transform,
            src.width,
            src.height,
            src.count,
            blocksize,
            overview_blocksize,
            overview_resampling,
            nodata,
            scales=scales,
            offsets=offsets,
            colorinterp=src_colorinterp,
        )
        cogfile_tmp = cogfile.with_name("." + cogfile.name + ".tmp")
        output = mem_dst.read()
        shasum.update(output)
        cogfile_tmp.write_bytes(output)
        cogfile_tmp.rename(cogfile)

    # pystac 2.0: add_if_missing=True. Both callers
    # give the asset an owner first, so add_if_missing=True
    # can register the file extension URI on the owning item here.
    FileExtension.ext(
        asset,
        add_if_missing=True,
    ).apply(
        checksum=str(multihash.wrap(shasum.digest(), "sha2-256").hex()),
        size=cogfile.stat().st_size,
    )

    asset.href = str(cogfile)
    # pystac 2.0 renamed Asset.media_type → Asset.type
    asset.type = MediaType.COG


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
        # Multi-band: fields live in each Band's extra_fields.
        scales, offsets, nodatas, resolutions = [], [], [], []
        for band in bands:
            ef = band.extra_fields
            scales.append(ef.get(RASTER_SCALE_KEY) or 1)
            offsets.append(ef.get(RASTER_OFFSET_KEY) or 0)
            nodata = ef.get(RASTER_NODATA_KEY)
            nodatas.append(float(nodata) if nodata is not None else None)
            resolutions.append(ef.get(RASTER_SPATIAL_RESOLUTION_KEY))
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
