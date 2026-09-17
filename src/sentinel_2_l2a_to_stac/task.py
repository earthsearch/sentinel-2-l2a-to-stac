import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import rasterio
import requests
import stac_asset.blocking
from boto3utils import s3
from botocore.exceptions import ClientError
from multiformats import multihash
from PIL import Image
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from pystac.extensions.storage import StorageExtension, StorageScheme
from rasterio.enums import ColorInterp, Resampling
from rasterio.errors import CRSError
from rasterio.rio.overview import get_maximum_overview_level
from returns.result import Failure, ResultE, Success
from stac_asset import Config
from stactask import Task
from stactask.exceptions import InvalidInput
from stactask.utils import stac_jsonpath_match

# SHIM(pystac-2.0): stac-asset reads asset.media_type when downloading but pystac
# 2.0 renamed the field to Asset.type. Remove once stac-asset is updated.
if not hasattr(Asset, "media_type"):
    Asset.media_type = property(lambda self: self.type)  # type: ignore[attr-defined]

from sentinel_2_l2a_to_stac.constants import (
    RASTER_NODATA_KEY,
    RASTER_OFFSET_KEY,
    RASTER_SCALE_KEY,
    RASTER_SPATIAL_RESOLUTION_KEY,
)
from sentinel_2_l2a_to_stac.stac import create_item

# stactask 0.7.0 already prefixes lines with payload id, so the legacy
# logging change was deliberately left off.
logging.getLogger().setLevel(os.getenv("CIRRUS_LOG_LEVEL", "WARN"))
for _noisy_logger in ("botocore", "rasterio"):
    logging.getLogger(_noisy_logger).propagate = False

s3_client = s3(requester_pays=False)

# Storage extension (pystac 1.15.2, schemes/refs model). Two aws-s3 schemes:
# "roda" for source metadata assets remaining on the RODA public bucket, and
# "earthsearch" for assets uploaded to the Earth Search output bucket. A
# "local" placeholder scheme is used in --local/test runs where no upload
# occurs. In v2 `platform` is the access-endpoint URI/template (provider
# identity moved to `type`), unlike v1's literal "AWS". `bucket` is an
# additional property required by the aws-s3 best-practices template.
RODA_SCHEME_KEY = "roda"
RODA_BUCKET = "sentinel-s2-l2a"
EARTHSEARCH_SCHEME_KEY = "earthsearch"
LOCAL_SCHEME_KEY = "local"
LOCAL_BUCKET = "local"
STORAGE_PLATFORM = "https://{bucket}.s3.{region}.amazonaws.com"
STORAGE_REGION = "us-west-2"

THUMBNAIL_ASSET_NAME = "thumbnail"
THUMBNAIL_SOURCE_ASSET_NAME = "preview"

# Source-metadata assets stay on local workdir paths through update_item (they're
# uploaded to earthsearch later); every other asset is rewritten to an S3 href.
_METADATA_ASSET_KEYS = ("tileinfo_metadata", "granule_metadata", "product_metadata")

EXPECTED_COGIFIED_COUNT = 19

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


class Sentinel2ToStac(Task):
    name = "sentinel-2-l2a-to-stac"
    description = "Sentinel-2 L2A to STAC Cirrus task"
    version = "v2026.09.03"

    def validate(self) -> bool:
        # Rewritten for stactask 0.6.1 (requires self._payload instead of
        # payload arg)
        if "metadata_href" not in self._payload:
            raise InvalidInput("metadata_href required")
        return True

    @property
    def tileinfo_path(self) -> Path:
        return Path(self._workdir.joinpath("tileInfo.json"))

    @property
    def granule_metadata_xml_path(self) -> Path:
        return Path(self._workdir.joinpath("metadata.xml"))

    @property
    def product_metadata_xml_path(self) -> Path:
        return Path(self._workdir.joinpath("product_metadata.xml"))

    def update_item(self, item: Item, s3_path: str) -> Item:
        """Apply Earth Search-specific overrides to the item from create_item."""
        item_dict = item.to_dict()
        if not (
            collection := next(
                (
                    c
                    for c, expr in self.upload_options.get("collections", {}).items()
                    if stac_jsonpath_match(item_dict, expr)
                ),
                None,
            )
        ):
            raise Exception("No collection defined for item")

        # pystac 2.0 made Item.collection_id read-only; set via set_collection()
        item.set_collection(collection)

        item.properties["earthsearch:payload_id"] = self._payload["id"]

        if item.datetime is None:
            raise ValueError("Item datetime property cannot be None")

        # create_item builds every asset with a workdir-relative local href.
        # Metadata assets keep those local paths (uploaded to earthsearch later by
        # upload_item_assets_to_s3); all other assets get rewritten to RODA S3
        # hrefs. When create_cogs=True those RODA hrefs are transitional — they're
        # overwritten again as each JP2 is downloaded, cogified, and uploaded to
        # earthsearch. With create_cogs=False they are the final output hrefs.
        for asset_name, asset in item.assets.items():
            if asset_name not in _METADATA_ASSET_KEYS:
                asset.href = f"{s3_path}{asset.href.removeprefix(str(self._workdir))}"

        return item

    def is_newer_than_existing(self, item: Item) -> ResultE[bool]:
        stac_api_url = os.getenv(
            "STAC_API_URL", "https://earth-search.aws.element84.com/v2"
        )
        existing_item_r = requests.get(
            f"{stac_api_url}/collections/{item.collection_id}/items/{item.id}",
            timeout=30,
        )

        if existing_item_r.status_code == 404:
            return Success(True)
        elif existing_item_r.status_code == 200:
            if (
                existing_item_pg := existing_item_r.json()
                .get("properties", {})
                .get("s2:generation_time", "")
            ) <= (created_item_pg := item.properties.get("s2:generation_time", "")):
                return Success(True)
            else:
                self.logger.info(
                    f"Item {item.id} exists with s2:generation_time "
                    f"'{existing_item_pg}', ignoring ingest with '{created_item_pg}'"
                )
                return Success(False)
        else:
            return Failure(
                Exception(
                    f"Failure attempting to check for existing item: "
                    f"{existing_item_r.status_code} {existing_item_r.text}"
                )
            )

    def make_cogs_for_item(self, item: Item) -> Item:
        try:
            processing_baseline = item.properties.get("s2:processing_baseline", "0")
            if processing_baseline < "05.00" or processing_baseline == "05.09":
                raise InvalidInput(
                    f"Processing baseline is {processing_baseline}, "
                    "only >= 05.00 (not including 5.09) is supported."
                )

            assets_to_cogify = list()
            for key, asset in item.assets.copy().items():
                if key.endswith("m") or key == "thumbnail":
                    del item.assets[key]
                # pystac 2.0 renamed Asset.media_type → Asset.type
                elif asset.type == "image/jp2":
                    assets_to_cogify.append(key)

            self.logger.info(f"Downloading assets to cogify: {assets_to_cogify}")

            item = stac_asset.blocking.download_item(
                item,
                self._workdir,
                infer_file_name=False,
                keep_non_downloaded=True,
                config=Config(s3_requester_pays=False, include=assets_to_cogify),
            )

            # pystac 2.0 change (same reason _set_asset_owners is called at the top of
            # update_item). Idempotent — safe to call again here.
            _set_asset_owners(item)

            for asset_name in assets_to_cogify:
                asset = item.assets[asset_name]
                self.logger.info(f"Converting {asset_name} {asset.href} to COG")
                cogify(asset_name, asset)

        except InvalidInput:
            raise
        except CRSError as err:
            msg = f"Invalid CRS ({err})"
            self.logger.exception(msg)
            raise InvalidInput(msg)
        except ClientError as err:
            if err.response["Error"]["Code"] == "NoSuchKey":
                msg = "Failed creating COGs: one or more assets not found"
                self.logger.exception(msg)
                raise InvalidInput(msg)
            else:
                raise
        except Exception:
            self.logger.exception("Failed creating COGs")
            raise

        cogified_count = len(assets_to_cogify)
        self.logger.info(f"Cogified {cogified_count} assets.")
        if cogified_count != EXPECTED_COGIFIED_COUNT:
            self.logger.error(
                f"Cogified {cogified_count} assets, expected {EXPECTED_COGIFIED_COUNT}"
            )

        return item

    def is_local_asset(self, asset: Asset) -> bool:
        return bool(asset.href.startswith(str(self._workdir)))

    def get_local_asset_keys(self, item: Item) -> list[str]:
        return [key for key, asset in item.assets.items() if self.is_local_asset(asset)]

    def add_storage_schemes(self, item: Item) -> Item:
        """Add per-bucket storage schemes and asset refs after upload.

        Classifies each asset by its final href: Earth Search bucket (uploaded
        COGs, thumbnail, and the source metadata files, which are re-uploaded
        rather than referenced in place), RODA bucket (non-cogified JP2 data
        assets left on the public bucket, only in the create_cogs=False path),
        or local path (--local/test runs). Each group gets its own named scheme
        so the `bucket` template variable is always defined.
        """
        roda_keys: list[str] = []
        earthsearch_keys: list[str] = []
        local_keys: list[str] = []

        for key, asset in item.assets.items():
            if asset.href.startswith("s3://"):
                bucket = s3_client.urlparse(asset.href)["bucket"]
                (roda_keys if bucket == RODA_BUCKET else earthsearch_keys).append(key)
            else:
                local_keys.append(key)

        storage = StorageExtension.ext(item, add_if_missing=True)

        def _add(scheme_key: str, bucket: str, asset_keys: list[str]) -> None:
            storage.add_scheme(
                scheme_key,
                StorageScheme.create(
                    type="aws-s3",
                    platform=STORAGE_PLATFORM,
                    region=STORAGE_REGION,
                    requester_pays=False,
                    bucket=bucket,
                ),
            )
            for ak in asset_keys:
                StorageExtension.ext(item.assets[ak], add_if_missing=True).add_ref(
                    scheme_key
                )

        if roda_keys:
            _add(RODA_SCHEME_KEY, RODA_BUCKET, roda_keys)
        if earthsearch_keys:
            es_bucket = s3_client.urlparse(item.assets[earthsearch_keys[0]].href)[
                "bucket"
            ]
            _add(EARTHSEARCH_SCHEME_KEY, es_bucket, earthsearch_keys)
        if local_keys:
            _add(LOCAL_SCHEME_KEY, LOCAL_BUCKET, local_keys)

        return item

    def add_fileinfo_to_local_assets(self, item: Item) -> Item:
        # pystac 2.0: re-parent first. Idempotent.
        _set_asset_owners(item)
        for asset in item.assets.values():
            if not self.is_local_asset(asset):
                continue
            fext = FileExtension.ext(
                asset,
                add_if_missing=True,
            )

            if fext.checksum is None:
                fext.checksum = sha256sum_multihash(asset.href)

            if fext.size is None:
                fext.size = os.path.getsize(asset.href)

        return item

    def read_href(self, href: str) -> bytes:
        try:
            return bytes(
                stac_asset.blocking.read_href(
                    href, config=Config(s3_requester_pays=False)
                )
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == "NoSuchKey":
                msg = f"Failed fetching href '{href}' ({err})"
                self.logger.error(msg, exc_info=True)
                raise InvalidInput(msg)
            else:
                raise

    def process(self, **kwargs: Any) -> list[dict[str, Any]]:
        metadata_href = self._payload["metadata_href"]
        create_cogs = self._payload.get("create_cogs", True)
        s3_path = os.path.dirname(metadata_href)

        # Download the three source metadata files into the workdir.
        # Each download is guarded by .exists() so a saved workdir is reused
        # without re-fetching.

        # tileInfo metadata, e.g.
        # s3://sentinel-s2-l2a/tiles/35/M/PP/2023/5/27/0/tileInfo.json
        if not self.tileinfo_path.exists():
            self.tileinfo_path.write_bytes(self.read_href(f"{s3_path}/tileInfo.json"))

        try:
            l2a_tileinfo = json.loads(self.tileinfo_path.read_text())
        except json.JSONDecodeError:
            raise InvalidInput("Corrupted tileInfo.json")

        if not self.product_metadata_xml_path.exists():
            parts = s3_client.urlparse(s3_path)
            self.product_metadata_xml_path.write_bytes(
                self.read_href(
                    f"s3://{parts['bucket']}/{l2a_tileinfo['productPath']}/metadata.xml"
                )
            )

        if not self.granule_metadata_xml_path.exists():
            self.granule_metadata_xml_path.write_bytes(
                self.read_href(f"{s3_path}/metadata.xml")
            )

        # Build the STAC Item from the downloaded metadata. stactools-sentinel2
        # reads all three files out of the workdir.
        try:
            item = create_item(str(self._workdir))
        except ValueError as ex:
            self.logger.error(ex)
            raise InvalidInput(f"Invalid input metadata: {ex}")
        except AssertionError as ex:
            self.logger.error(ex)
            raise InvalidInput(
                f"Assertion failed, likely because of an invalid geometry: {ex}"
            )
        except Exception as ex:
            self.logger.error(ex)
            if "Cannot find granule tile_id granule metadata" in str(ex):
                raise InvalidInput("Unable to parse older metadata file format")
            else:
                raise Exception(f"Unable to create item: {ex}") from ex

        # Apply Earth Search-specific overrides
        try:
            item = self.update_item(item, s3_path)
        except Exception as ex:
            self.logger.error(ex)
            raise Exception(f"Unable to update item: {ex}")

        # id and collection are set, so look up in the live STAC API to avoid
        # regressing an already-ingested item.
        match self.is_newer_than_existing(item):
            case Failure(e):
                raise e
            case Success(False):
                return []
            case _:
                pass

        if create_cogs:
            self.logger.info("Making COGs for item assets")
            item = self.make_cogs_for_item(item)

            self.logger.info("Making preview thumbnail")
            try:
                item = make_thumbnail(item)
            except Exception:
                self.logger.exception("Cannot create JPEG thumbnail")
                raise

        self.logger.info("Adding fileinfo to assets")
        item = self.add_fileinfo_to_local_assets(item)

        self.logger.info("Uploading assets")
        item = self.upload_item_assets_to_s3(item, self.get_local_asset_keys(item))

        self.logger.info("Adding storage schemes")
        item = self.add_storage_schemes(item)

        return [self.add_software_version_to_item(item.to_dict())]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


def _set_asset_owners(item: Item) -> None:
    for asset in item.assets.values():
        asset.set_owner(item)


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


if __name__ == "__main__":
    Sentinel2ToStac.cli()
