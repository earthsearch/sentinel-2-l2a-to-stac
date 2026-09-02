import hashlib
import json
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
from pystac import Asset, Item, Link, MediaType, RelType
from pystac.extensions.file import FileExtension
from pystac.extensions.grid import GridExtension
from pystac.extensions.raster import AssetRasterExtension
from pystac.extensions.storage import StorageExtension, StorageScheme
from rasterio.enums import ColorInterp, Resampling
from rasterio.errors import CRSError, StatisticsError
from rasterio.rio.overview import get_maximum_overview_level
from returns.result import Failure, ResultE, Success
from stac_asset import Config
from stactask import Task
from stactask.exceptions import InvalidInput
from stactask.utils import stac_jsonpath_match
from stactools.sentinel2.stac import create_item

# RODA hosts the raw Sentinel-2 tiles in a public bucket with no STAC catalog,
# only the source metadata files. We reconstruct the product-level metadata
# href from tileInfo.json's `productPath`, which requires recovering the bucket
# from the granule href — that's the only use of this client.
s3_client = s3(requester_pays=False)

# Storage extension (pystac 1.15.2, schemes/refs model). One aws-s3 scheme,
# referenced from every asset. In v2 `platform` is the access-endpoint URI/
# template (provider identity moved to `type`), unlike v1's literal "AWS".
STORAGE_SCHEME_KEY = "aws"
STORAGE_PLATFORM = "https://{bucket}.s3.{region}.amazonaws.com"
STORAGE_REGION = "us-west-2"

# make_thumbnail re-encodes the `preview` asset into a JPEG registered under the
# `thumbnail` asset key.
THUMBNAIL_ASSET_NAME = "thumbnail"
THUMBNAIL_SOURCE_ASSET_NAME = "preview"

# After processing_baseline check passes for 04.xx tiles, the tile must
# additionally intersect Europe. This carve-out was introduced for the Tapir
# project; the inline comment "change this back to 05.00 after Tapir is
# complete" remains in the legacy code. Preserved verbatim per plan — whether
# to remove it is an open question for the user (see CLAUDE.md open questions).
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

    def validate(self) -> bool:
        # The task is driven entirely by a `metadata_href` pointing at the
        # Sentinel-2 source metadata on RODA/S3; without it there is nothing to
        # process, so reject the payload as the input's fault (InvalidInput),
        # not an internal error. Ported from the legacy classmethod validate,
        # rewritten for stactask 0.6.1's instance-method signature (reads
        # self._payload instead of a `payload` arg).
        if "metadata_href" not in self._payload:
            raise InvalidInput("metadata_href required")
        return True

    # The three source files stactools' create_item reads, all under the
    # workdir. Path() re-wraps because mypy infers Any from Path.joinpath here.
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
        """Update metadata from stactools-sentinel2 with Earth Search specifics."""
        # Assign the collection by matching the item against each configured
        # JSONPath expression, first match wins. Fail-fast if none match. (The
        # base Task.handler also runs assign_collections() after process(), but
        # we keep this manual assignment for its fail-fast behavior — the two
        # overlap intentionally; see MIGRATION_PLAN.md PR 4.)
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

        item.collection_id = collection

        item.properties["earthsearch:payload_id"] = self._payload["id"]

        if item.datetime is None:
            raise ValueError("Item datetime property cannot be None")

        # ESA-provided providers/license don't apply to the Earth Search
        # republish; drop them.
        item.properties.pop("providers", None)
        item.remove_links("license")

        # Link back to the source granule metadata on RODA.
        item.add_link(
            Link(
                rel=RelType.VIA,
                target=f"{s3_path}/metadata.xml",
                media_type=MediaType.XML,
                title="Granule Metadata in Sinergize RODA Archive",
            )
        )

        # Storage extension, rewritten for pystac 1.15.2's schemes/refs model
        # (legacy's CloudPlatform + .apply(platform=,region=,requester_pays=) API
        # was removed). Define one aws-s3 scheme here; each asset references it
        # via storage:refs in the loop below.
        storage = StorageExtension.ext(item, add_if_missing=True)
        storage.add_scheme(
            STORAGE_SCHEME_KEY,
            StorageScheme.create(
                type="aws-s3",
                platform=STORAGE_PLATFORM,
                region=STORAGE_REGION,
                requester_pays=False,
            ),
        )

        ########################################
        # scrub extra metadata added after v0.7.1
        del item.assets["scl"].extra_fields["raster:bands"][0]["classification:classes"]
        del item.properties["eo:snow_cover"]
        item.stac_extensions = [
            ext for ext in item.stac_extensions if "classification" not in ext
        ]
        # end_scrub
        ########################################

        # Rewrite asset URLs to reference the RODA S3 bucket instead of the local
        # workdir, except for the metadata assets, which point to the local files
        # already downloaded for create_item(). Also strip proj:bbox and attach
        # the storage ref to every asset.
        for asset_name, asset in item.assets.items():
            if asset_name == "tileinfo_metadata":
                asset.href = str(self.tileinfo_path)
            elif asset_name == "granule_metadata":
                asset.href = str(self.granule_metadata_xml_path)
            elif asset_name == "product_metadata":
                asset.href = str(self.product_metadata_xml_path)
            else:
                asset.href = f"{s3_path}{asset.href.removeprefix(str(self._workdir))}"

            # Remove unnecessary fields
            asset.extra_fields.pop("proj:bbox", None)

            StorageExtension.ext(asset, add_if_missing=True).add_ref(
                STORAGE_SCHEME_KEY
            )

        return item

    def is_newer_than_existing(self, item: Item) -> ResultE[bool]:
        stac_api_url = os.getenv(
            "STAC_API_URL", "https://earth-search.aws.element84.com/v1"
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
            # Temporarily allow processing baseline >= 04.00, but change this
            # back to 05.00 after Tapir is complete (see EUROPE_MGRS_IDS comment).
            processing_baseline = item.properties.get("s2:processing_baseline", "0")
            if processing_baseline < "04.00":
                raise InvalidInput(
                    f"Processing baseline is {processing_baseline}, only >= 04.00 is "
                    "supported."
                )
            elif processing_baseline.startswith("04."):
                # grid:code is prefixed with 'MGRS-', so remove that for compare.
                # Under pystac 1.15.2 GridExtension.code is typed str | None
                # (was effectively str in legacy's pystac 1.9); a real Sentinel-2
                # item always carries grid:code, so a missing one is treated as
                # "not in Europe" and rejected, keeping the check type-safe.
                grid_code = GridExtension.ext(item).code
                if grid_code is None or grid_code[5:] not in EUROPE_MGRS_IDS:
                    raise InvalidInput(
                        "Processing baseline 04.00 must intersect Europe"
                    )

            assets_to_cogify = list()
            for key, asset in item.assets.copy().items():
                if key.endswith("m") or key == "thumbnail":
                    del item.assets[key]
                elif asset.media_type == "image/jp2":
                    assets_to_cogify.append(key)

            self.logger.info(f"Downloading assets to cogify: {assets_to_cogify}")

            item = stac_asset.blocking.download_item(
                item,
                self._workdir,
                infer_file_name=False,
                keep_non_downloaded=True,
                config=Config(s3_requester_pays=False, include=assets_to_cogify),
            )

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

    def add_fileinfo_to_local_assets(self, item: Item) -> Item:
        # Stamp file:checksum (multihash-wrapped sha256) and file:size on every
        # asset still living in the workdir. Runs unconditionally (even when COGs
        # are off) so the local metadata assets get file info too; s3:// assets
        # are skipped. `op.getsize` in legacy → `os.path.getsize` here (the
        # `os.path as op` alias was dropped in PR 6).
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
        # Fetch a single href's bytes. A missing object (NoSuchKey) is the
        # input payload's fault (a bad/removed source tile), so translate it to
        # InvalidInput per the project's InvalidInput-vs-Exception convention;
        # any other ClientError is an internal/transient failure and re-raises.
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

        # Download the three source metadata files into the workdir, where
        # create_item (PR 3) will read them. Each download is guarded by
        # .exists() so a saved workdir is reused without re-fetching.

        # tileInfo metadata, e.g.
        # s3://sentinel-s2-l2a/tiles/35/M/PP/2023/5/27/0/tileInfo.json
        if not self.tileinfo_path.exists():
            self.tileinfo_path.write_bytes(
                self.read_href(f"{s3_path}/tileInfo.json")
            )

        try:
            l2a_tileinfo = json.loads(self.tileinfo_path.read_text())
        except json.JSONDecodeError:
            raise InvalidInput("Corrupted tileInfo.json")

        # Product-level metadata lives under `productPath` (from tileInfo.json),
        # not next to the granule metadata, so recover the bucket from the
        # granule path and rebuild the product metadata href.
        if not self.product_metadata_xml_path.exists():
            parts = s3_client.urlparse(s3_path)
            self.product_metadata_xml_path.write_bytes(
                self.read_href(
                    f"s3://{parts['bucket']}/{l2a_tileinfo['productPath']}/metadata.xml"
                )
            )

        # granule metadata.xml (the metadata_href itself)
        if not self.granule_metadata_xml_path.exists():
            self.granule_metadata_xml_path.write_bytes(
                self.read_href(f"{s3_path}/metadata.xml")
            )

        # Build the STAC Item from the downloaded metadata. stactools-sentinel2
        # reads all three files out of the workdir. The exception translation is
        # ported verbatim and preserves the InvalidInput-vs-Exception convention:
        # a ValueError/AssertionError (and the specific "older metadata format"
        # message) is the input's fault; anything else is an internal failure.
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

        # Apply Earth Search-specific overrides (collection, storage, scrub,
        # asset href rewriting, ...). Wrap failures as internal Exceptions.
        try:
            item = self.update_item(item, s3_path)
        except Exception as ex:
            self.logger.error(ex)
            raise Exception(f"Unable to update item: {ex}")

        # id and collection are set, so look up in the live STAC API to avoid
        # regressing an already-ingested item.
        # (Legacy needed `# type: ignore` on these cases for returns ~0.22;
        # returns 0.29 + mypy 2.3.1 type the pattern matching cleanly.)
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

        # Upload is ported in PR 8.
        return [item.to_dict()]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


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

    # Reading the file into a MemoryFile is slightly more performant — avoids
    # seek-back during the COG overview build step on some filesystems.
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

    FileExtension.ext(
        asset,
        add_if_missing=True,
    ).apply(
        checksum=str(multihash.wrap(shasum.digest(), "sha2-256").hex()),
        size=cogfile.stat().st_size,
    )

    asset.href = str(cogfile)
    asset.media_type = MediaType.COG


def sha256sum_multihash(filename: str) -> str:
    # Wrap a streamed sha256 digest in a multihash envelope (sha2-256 code +
    # length prefix), hex-encoded — the checksum format the file extension wants.
    with open(filename, "rb") as f:
        return str(
            multihash.wrap(hashlib.file_digest(f, "sha256").digest(), "sha2-256").hex()
        )


def make_thumbnail(item: Item) -> Item:
    asset = item.assets[THUMBNAIL_SOURCE_ASSET_NAME]

    # remove existing thumbnail asset linking to preview.jpg/jp2
    if THUMBNAIL_ASSET_NAME in item.assets:
        item.delete_asset(THUMBNAIL_ASSET_NAME)

    tn_asset = Asset(
        href=os.path.splitext(asset.href)[0] + ".jpg",
        media_type=MediaType.JPEG,
        roles=["thumbnail"],
        title="Thumbnail of preview image",
    )
    item.add_asset(THUMBNAIL_ASSET_NAME, tn_asset)

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
            for band in range(dst.count):
                try:
                    dst.statistics(band + 1)
                except StatisticsError:
                    # stats fail if a raster is all nodata
                    pass


def get_band_scales_offsets_nodatas_resolutions(
    asset: Asset,
) -> tuple[
    list[int | float] | None,
    list[int | float] | None,
    list[float | None] | None,
    list[int | float | None] | None,
]:
    # Reads the pre-1.1.0 raster:bands shape by design. PR 9 consolidates
    # raster:bands + eo:bands into the STAC 1.1.0 `bands` field; this function
    # intentionally runs before that upgrade.
    bands = AssetRasterExtension(asset).bands

    if bands is None:
        return None, None, None, None

    scales, offsets, nodatas, resolutions = [], [], [], []
    for band in bands:
        scales.append(band.scale or 1)
        offsets.append(band.offset or 0)
        nodatas.append(float(band.nodata) if band.nodata is not None else None)
        resolutions.append(band.spatial_resolution)

    return scales, offsets, nodatas, resolutions


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



EUROPE_MGRS_IDS = [
    "21NYC",
    "21NYD",
    "21NYE",
    "21NYF",
    "21NYG",
    "21NZC",
    "21NZD",
    "21NZE",
    "21NZF",
    "21NZG",
    "22NBH",
    "22NBJ",
    "22NBK",
    "22NBL",
    "22NBM",
    "22NCH",
    "22NCJ",
    "22NCK",
    "22NCL",
    "22NCM",
    "22NDK",
    "22NDL",
    "25SFD",
    "25TFE",
    "26SLH",
    "26SLJ",
    "26SMH",
    "26SMJ",
    "26SNG",
    "26SNH",
    "26SNJ",
    "26SPF",
    "26SPG",
    "26SPH",
    "26VPR",
    "26WNT",
    "26WPS",
    "26WPT",
    "26WPU",
    "27RYL",
    "27RYM",
    "27RYN",
    "27VUL",
    "27VVL",
    "27VWL",
    "27VXL",
    "27WVM",
    "27WVN",
    "27WVP",
    "27WWM",
    "27WWN",
    "27WWP",
    "27WXM",
    "27WXN",
    "27WXP",
    "27WXQ",
    "28RBR",
    "28RBS",
    "28RBT",
    "28RCR",
    "28RCS",
    "28RCU",
    "28RDR",
    "28RDS",
    "28RDU",
    "28RER",
    "28RES",
    "28RET",
    "28RFS",
    "28RFT",
    "28SBB",
    "28SCA",
    "28SCB",
    "28UGC",
    "28UGD",
    "28UGE",
    "28VCR",
    "28VDR",
    "28WDS",
    "28WDT",
    "28WDU",
    "28WDV",
    "28WES",
    "28WET",
    "28WEU",
    "29SMA",
    "29SMB",
    "29SMC",
    "29SMD",
    "29SNA",
    "29SNB",
    "29SNC",
    "29SND",
    "29SPA",
    "29SPB",
    "29SPC",
    "29SPD",
    "29SQA",
    "29SQB",
    "29SQC",
    "29SQD",
    "29SQV",
    "29TME",
    "29TMF",
    "29TMG",
    "29TMH",
    "29TMJ",
    "29TNE",
    "29TNF",
    "29TNG",
    "29TNH",
    "29TNJ",
    "29TPE",
    "29TPF",
    "29TPG",
    "29TPH",
    "29TPJ",
    "29TQE",
    "29TQF",
    "29TQG",
    "29TQH",
    "29TQJ",
    "29ULA",
    "29ULT",
    "29ULU",
    "29ULV",
    "29UMA",
    "29UMS",
    "29UMT",
    "29UMU",
    "29UMV",
    "29UNA",
    "29UNB",
    "29UNS",
    "29UNT",
    "29UNU",
    "29UNV",
    "29UPA",
    "29UPB",
    "29UPR",
    "29UPT",
    "29UPU",
    "29UPV",
    "29UQP",
    "29UQQ",
    "29UQR",
    "29UQS",
    "29UQT",
    "29UQU",
    "29UQV",
    "29VNC",
    "29VND",
    "29VNE",
    "29VNF",
    "29VPC",
    "29VPD",
    "29VPE",
    "29VPF",
    "30STE",
    "30STF",
    "30STG",
    "30STH",
    "30STJ",
    "30SUD",
    "30SUE",
    "30SUF",
    "30SUG",
    "30SUH",
    "30SUJ",
    "30SVD",
    "30SVE",
    "30SVF",
    "30SVG",
    "30SVH",
    "30SVJ",
    "30SWD",
    "30SWE",
    "30SWF",
    "30SWG",
    "30SWH",
    "30SWJ",
    "30SXF",
    "30SXG",
    "30SXH",
    "30SXJ",
    "30SYG",
    "30SYH",
    "30SYJ",
    "30TTK",
    "30TTL",
    "30TTM",
    "30TUK",
    "30TUL",
    "30TUM",
    "30TUN",
    "30TUP",
    "30TUT",
    "30TVK",
    "30TVL",
    "30TVM",
    "30TVN",
    "30TVP",
    "30TVT",
    "30TWK",
    "30TWL",
    "30TWM",
    "30TWN",
    "30TWP",
    "30TWS",
    "30TWT",
    "30TXK",
    "30TXL",
    "30TXM",
    "30TXN",
    "30TXP",
    "30TXQ",
    "30TXR",
    "30TXS",
    "30TXT",
    "30TYK",
    "30TYL",
    "30TYM",
    "30TYN",
    "30TYP",
    "30TYQ",
    "30TYR",
    "30TYS",
    "30TYT",
    "30UUA",
    "30UUB",
    "30UUC",
    "30UUD",
    "30UUE",
    "30UUF",
    "30UUG",
    "30UUU",
    "30UUV",
    "30UVA",
    "30UVB",
    "30UVC",
    "30UVD",
    "30UVE",
    "30UVF",
    "30UVG",
    "30UVU",
    "30UVV",
    "30UWA",
    "30UWB",
    "30UWC",
    "30UWD",
    "30UWE",
    "30UWF",
    "30UWG",
    "30UWU",
    "30UWV",
    "30UXA",
    "30UXB",
    "30UXC",
    "30UXD",
    "30UXE",
    "30UXF",
    "30UXG",
    "30UXU",
    "30UXV",
    "30UYA",
    "30UYB",
    "30UYC",
    "30UYD",
    "30UYE",
    "30UYU",
    "30UYV",
    "30VUH",
    "30VUJ",
    "30VUK",
    "30VUL",
    "30VVH",
    "30VVJ",
    "30VVK",
    "30VVL",
    "30VVM",
    "30VWH",
    "30VWJ",
    "30VWK",
    "30VWL",
    "30VWM",
    "30VWN",
    "30VXM",
    "30VXN",
    "31SBC",
    "31SBD",
    "31SCC",
    "31SCD",
    "31SDD",
    "31SED",
    "31SFD",
    "31TBE",
    "31TBF",
    "31TBG",
    "31TCE",
    "31TCF",
    "31TCG",
    "31TCH",
    "31TCJ",
    "31TCK",
    "31TCL",
    "31TCM",
    "31TCN",
    "31TDE",
    "31TDF",
    "31TDG",
    "31TDH",
    "31TDJ",
    "31TDK",
    "31TDL",
    "31TDM",
    "31TDN",
    "31TEE",
    "31TEG",
    "31TEH",
    "31TEJ",
    "31TEK",
    "31TEL",
    "31TEM",
    "31TEN",
    "31TFE",
    "31TFH",
    "31TFJ",
    "31TFK",
    "31TFL",
    "31TFM",
    "31TFN",
    "31TGH",
    "31TGJ",
    "31TGK",
    "31TGL",
    "31TGM",
    "31TGN",
    "31UCA",
    "31UCP",
    "31UCQ",
    "31UCR",
    "31UCS",
    "31UCT",
    "31UCU",
    "31UCV",
    "31UDP",
    "31UDQ",
    "31UDR",
    "31UDS",
    "31UDT",
    "31UDU",
    "31UEP",
    "31UEQ",
    "31UER",
    "31UES",
    "31UET",
    "31UEU",
    "31UEV",
    "31UFP",
    "31UFQ",
    "31UFR",
    "31UFS",
    "31UFT",
    "31UFU",
    "31UFV",
    "31UGP",
    "31UGQ",
    "31UGR",
    "31UGS",
    "31UGT",
    "31UGU",
    "31UGV",
    "31VCG",
    "31VCH",
    "31VEF",
    "31VEG",
    "31VEH",
    "31VEJ",
    "31VEK",
    "31VFL",
    "32SMH",
    "32SMJ",
    "32SNJ",
    "32SQE",
    "32SQF",
    "32SQG",
    "32SQH",
    "32TLN",
    "32TLP",
    "32TLQ",
    "32TLR",
    "32TLS",
    "32TLT",
    "32TMK",
    "32TML",
    "32TMM",
    "32TMN",
    "32TMP",
    "32TMQ",
    "32TMR",
    "32TMS",
    "32TMT",
    "32TNK",
    "32TNL",
    "32TNM",
    "32TNN",
    "32TNP",
    "32TNQ",
    "32TNR",
    "32TNS",
    "32TNT",
    "32TPM",
    "32TPN",
    "32TPP",
    "32TPQ",
    "32TPR",
    "32TPS",
    "32TPT",
    "32TQL",
    "32TQM",
    "32TQN",
    "32TQP",
    "32TQQ",
    "32TQR",
    "32TQS",
    "32TQT",
    "32ULA",
    "32ULB",
    "32ULC",
    "32ULD",
    "32ULE",
    "32ULU",
    "32ULV",
    "32UMA",
    "32UMB",
    "32UMC",
    "32UMD",
    "32UME",
    "32UMF",
    "32UMG",
    "32UMU",
    "32UMV",
    "32UNA",
    "32UNB",
    "32UNC",
    "32UND",
    "32UNE",
    "32UNF",
    "32UNG",
    "32UNU",
    "32UNV",
    "32UPA",
    "32UPB",
    "32UPC",
    "32UPD",
    "32UPE",
    "32UPF",
    "32UPG",
    "32UPU",
    "32UPV",
    "32UQA",
    "32UQB",
    "32UQC",
    "32UQD",
    "32UQE",
    "32UQU",
    "32UQV",
    "32VKK",
    "32VKL",
    "32VKM",
    "32VKN",
    "32VKP",
    "32VKQ",
    "32VLK",
    "32VLL",
    "32VLM",
    "32VLN",
    "32VLP",
    "32VLQ",
    "32VLR",
    "32VMH",
    "32VMJ",
    "32VMK",
    "32VML",
    "32VMM",
    "32VMN",
    "32VMP",
    "32VMQ",
    "32VMR",
    "32VNH",
    "32VNJ",
    "32VNK",
    "32VNL",
    "32VNM",
    "32VNN",
    "32VNP",
    "32VNQ",
    "32VNR",
    "32VPH",
    "32VPJ",
    "32VPK",
    "32VPL",
    "32VPM",
    "32VPN",
    "32VPP",
    "32VPQ",
    "32VPR",
    "32WMS",
    "32WNS",
    "32WNT",
    "32WNU",
    "32WPA",
    "32WPB",
    "32WPS",
    "32WPT",
    "32WPU",
    "32WPV",
    "33STA",
    "33STB",
    "33STC",
    "33STV",
    "33SUA",
    "33SUB",
    "33SUC",
    "33SUD",
    "33SUV",
    "33SVA",
    "33SVB",
    "33SVC",
    "33SVD",
    "33SVV",
    "33SWA",
    "33SWB",
    "33SWC",
    "33SWD",
    "33SXB",
    "33SXC",
    "33SXD",
    "33SYD",
    "33TTF",
    "33TTG",
    "33TUE",
    "33TUF",
    "33TUG",
    "33TUH",
    "33TUJ",
    "33TUK",
    "33TUL",
    "33TUM",
    "33TUN",
    "33TVE",
    "33TVF",
    "33TVG",
    "33TVH",
    "33TVJ",
    "33TVK",
    "33TVL",
    "33TVM",
    "33TVN",
    "33TWE",
    "33TWF",
    "33TWG",
    "33TWH",
    "33TWJ",
    "33TWK",
    "33TWL",
    "33TWM",
    "33TWN",
    "33TXE",
    "33TXF",
    "33TXG",
    "33TXH",
    "33TXJ",
    "33TXK",
    "33TXL",
    "33TXM",
    "33TXN",
    "33TYE",
    "33TYF",
    "33TYG",
    "33TYH",
    "33TYJ",
    "33TYK",
    "33TYL",
    "33TYM",
    "33TYN",
    "33UUA",
    "33UUB",
    "33UUP",
    "33UUQ",
    "33UUR",
    "33UUS",
    "33UUT",
    "33UUU",
    "33UUV",
    "33UVA",
    "33UVB",
    "33UVP",
    "33UVQ",
    "33UVR",
    "33UVS",
    "33UVT",
    "33UVU",
    "33UVV",
    "33UWA",
    "33UWB",
    "33UWP",
    "33UWQ",
    "33UWR",
    "33UWS",
    "33UWT",
    "33UWU",
    "33UWV",
    "33UXA",
    "33UXB",
    "33UXP",
    "33UXQ",
    "33UXR",
    "33UXS",
    "33UXT",
    "33UXU",
    "33UXV",
    "33UYP",
    "33UYQ",
    "33UYR",
    "33UYS",
    "33UYT",
    "33UYU",
    "33UYV",
    "33VUC",
    "33VUD",
    "33VUE",
    "33VUF",
    "33VUG",
    "33VUH",
    "33VUJ",
    "33VUK",
    "33VUL",
    "33VVC",
    "33VVD",
    "33VVE",
    "33VVF",
    "33VVG",
    "33VVH",
    "33VVJ",
    "33VVK",
    "33VVL",
    "33VWC",
    "33VWD",
    "33VWE",
    "33VWF",
    "33VWG",
    "33VWH",
    "33VWJ",
    "33VWK",
    "33VWL",
    "33VXC",
    "33VXD",
    "33VXE",
    "33VXF",
    "33VXG",
    "33VXH",
    "33VXJ",
    "33VXK",
    "33VXL",
    "33WVM",
    "33WVN",
    "33WVP",
    "33WVQ",
    "33WVR",
    "33WVS",
    "33WWM",
    "33WWN",
    "33WWP",
    "33WWQ",
    "33WWR",
    "33WWS",
    "33WWT",
    "33WXM",
    "33WXN",
    "33WXP",
    "33WXQ",
    "33WXR",
    "33WXS",
    "33WXT",
    "33WXU",
    "33WYV",
    "34SBJ",
    "34SCJ",
    "34SDG",
    "34SDH",
    "34SDJ",
    "34SEF",
    "34SEG",
    "34SEH",
    "34SEJ",
    "34SFE",
    "34SFF",
    "34SFG",
    "34SFH",
    "34SFJ",
    "34SGD",
    "34SGE",
    "34SGF",
    "34SGG",
    "34SGH",
    "34SGJ",
    "34TBK",
    "34TBL",
    "34TBM",
    "34TCK",
    "34TCL",
    "34TCM",
    "34TCN",
    "34TCP",
    "34TCQ",
    "34TCR",
    "34TCS",
    "34TCT",
    "34TDK",
    "34TDL",
    "34TDM",
    "34TDN",
    "34TDP",
    "34TDQ",
    "34TDR",
    "34TDS",
    "34TDT",
    "34TEK",
    "34TEL",
    "34TEM",
    "34TEN",
    "34TEP",
    "34TEQ",
    "34TER",
    "34TES",
    "34TET",
    "34TFK",
    "34TFL",
    "34TFM",
    "34TFN",
    "34TFP",
    "34TFQ",
    "34TFR",
    "34TFS",
    "34TFT",
    "34TGK",
    "34TGL",
    "34TGM",
    "34TGN",
    "34TGP",
    "34TGQ",
    "34TGR",
    "34TGS",
    "34TGT",
    "34UCA",
    "34UCB",
    "34UCC",
    "34UCD",
    "34UCE",
    "34UCF",
    "34UCG",
    "34UCU",
    "34UCV",
    "34UDA",
    "34UDB",
    "34UDC",
    "34UDD",
    "34UDE",
    "34UDF",
    "34UDG",
    "34UDU",
    "34UDV",
    "34UEA",
    "34UEB",
    "34UEC",
    "34UED",
    "34UEE",
    "34UEF",
    "34UEG",
    "34UEU",
    "34UEV",
    "34UFA",
    "34UFB",
    "34UFC",
    "34UFD",
    "34UFE",
    "34UFF",
    "34UFG",
    "34UFU",
    "34UFV",
    "34UGA",
    "34UGB",
    "34UGD",
    "34UGE",
    "34UGU",
    "34VCJ",
    "34VCK",
    "34VCL",
    "34VCM",
    "34VCN",
    "34VCP",
    "34VCQ",
    "34VCR",
    "34VDH",
    "34VDJ",
    "34VDK",
    "34VDL",
    "34VDM",
    "34VDN",
    "34VDP",
    "34VDQ",
    "34VDR",
    "34VEH",
    "34VEJ",
    "34VEK",
    "34VEL",
    "34VEM",
    "34VEN",
    "34VEP",
    "34VEQ",
    "34VER",
    "34VFH",
    "34VFJ",
    "34VFK",
    "34VFL",
    "34VFM",
    "34VFN",
    "34VFP",
    "34VFQ",
    "34VFR",
    "34WDA",
    "34WDB",
    "34WDC",
    "34WDD",
    "34WDS",
    "34WDT",
    "34WDU",
    "34WDV",
    "34WEA",
    "34WEB",
    "34WEC",
    "34WED",
    "34WEE",
    "34WES",
    "34WET",
    "34WEU",
    "34WEV",
    "34WFA",
    "34WFB",
    "34WFC",
    "34WFD",
    "34WFE",
    "34WFS",
    "34WFT",
    "34WFU",
    "34WFV",
    "35SKA",
    "35SKB",
    "35SKC",
    "35SKD",
    "35SKU",
    "35SKV",
    "35SLA",
    "35SLB",
    "35SLC",
    "35SLD",
    "35SLU",
    "35SLV",
    "35SMA",
    "35SMB",
    "35SMC",
    "35SMD",
    "35SMU",
    "35SMV",
    "35SNA",
    "35SNB",
    "35SNC",
    "35SND",
    "35SNV",
    "35SPA",
    "35SPB",
    "35SPC",
    "35SPD",
    "35SPV",
    "35SQA",
    "35SQB",
    "35SQC",
    "35SQD",
    "35SQV",
    "35TKE",
    "35TKF",
    "35TKG",
    "35TLE",
    "35TLF",
    "35TLG",
    "35TLH",
    "35TLJ",
    "35TLK",
    "35TLL",
    "35TLM",
    "35TLN",
    "35TME",
    "35TMF",
    "35TMG",
    "35TMH",
    "35TMJ",
    "35TMK",
    "35TML",
    "35TMM",
    "35TMN",
    "35TNE",
    "35TNF",
    "35TNG",
    "35TNH",
    "35TNJ",
    "35TNK",
    "35TNL",
    "35TNM",
    "35TNN",
    "35TPE",
    "35TPF",
    "35TPG",
    "35TPH",
    "35TPJ",
    "35TPK",
    "35TPL",
    "35TPM",
    "35TQE",
    "35TQF",
    "35TQK",
    "35TQL",
    "35ULA",
    "35ULB",
    "35ULP",
    "35ULR",
    "35ULS",
    "35ULU",
    "35ULV",
    "35UMA",
    "35UMB",
    "35UMP",
    "35UMV",
    "35UNB",
    "35UNP",
    "35VLC",
    "35VLD",
    "35VLE",
    "35VLF",
    "35VLG",
    "35VLH",
    "35VLJ",
    "35VLK",
    "35VLL",
    "35VMC",
    "35VMD",
    "35VME",
    "35VMF",
    "35VMG",
    "35VMH",
    "35VMJ",
    "35VMK",
    "35VML",
    "35VNC",
    "35VND",
    "35VNE",
    "35VNF",
    "35VNG",
    "35VNH",
    "35VNJ",
    "35VNK",
    "35VNL",
    "35VPH",
    "35VPJ",
    "35VPK",
    "35VPL",
    "35WLV",
    "35WMM",
    "35WMN",
    "35WMP",
    "35WMQ",
    "35WMR",
    "35WMS",
    "35WMT",
    "35WMU",
    "35WMV",
    "35WNM",
    "35WNN",
    "35WNP",
    "35WNQ",
    "35WNR",
    "35WNS",
    "35WNT",
    "35WNU",
    "35WNV",
    "35WPM",
    "35WPN",
    "35WPP",
    "35WPQ",
    "35WPR",
    "35WPS",
    "35WPT",
    "35WPU",
    "36STE",
    "36STF",
    "36STG",
    "36STH",
    "36STJ",
    "36SUF",
    "36SUG",
    "36SUH",
    "36SUJ",
    "36SVD",
    "36SVE",
    "36SVF",
    "36SVG",
    "36SVH",
    "36SVJ",
    "36SWD",
    "36SWE",
    "36SWF",
    "36SWG",
    "36SWH",
    "36SWJ",
    "36SXD",
    "36SXE",
    "36SXF",
    "36SXG",
    "36SXH",
    "36SXJ",
    "36SYE",
    "36SYF",
    "36SYG",
    "36SYH",
    "36SYJ",
    "36TTK",
    "36TTL",
    "36TUK",
    "36TUL",
    "36TUM",
    "36TVK",
    "36TVL",
    "36TVM",
    "36TWK",
    "36TWL",
    "36TWM",
    "36TXK",
    "36TXL",
    "36TXM",
    "36TYK",
    "36TYL",
    "36TYM",
    "36VUN",
    "36VUP",
    "36VUQ",
    "36VUR",
    "36VVQ",
    "36VVR",
    "36WVC",
    "36WVD",
    "37SBA",
    "37SBB",
    "37SBC",
    "37SBD",
    "37SBV",
    "37SCA",
    "37SCB",
    "37SCC",
    "37SCD",
    "37SDA",
    "37SDB",
    "37SDC",
    "37SDD",
    "37SEA",
    "37SEB",
    "37SEC",
    "37SED",
    "37SFA",
    "37SFB",
    "37SFC",
    "37SFD",
    "37SGA",
    "37SGB",
    "37SGC",
    "37SGD",
    "37TBE",
    "37TBF",
    "37TBG",
    "37TCE",
    "37TCF",
    "37TCG",
    "37TDE",
    "37TDF",
    "37TEE",
    "37TEF",
    "37TFE",
    "37TFF",
    "37TFG",
    "37TGE",
    "37TGF",
    "37TGG",
    "38SKF",
    "38SKG",
    "38SKH",
    "38SKJ",
    "38SLG",
    "38SLH",
    "38SLJ",
    "38SMF",
    "38SMG",
    "38SMH",
    "38SMJ",
    "38TKK",
    "38TKL",
    "38TKM",
    "38TLK",
    "38TLL",
    "38TLM",
    "38TMK",
    "38TML",
    "20PPA",
    "20PPB",
    "20PPC",
    "20PQA",
    "20PQB",
    "20PQC",
    "20QPD",
    "20QQD",
    "38LML",
    "38LMM",
    "38LNL",
    "38LNM",
    "40KCB",
    "40KCC",
    "29VNJ",
    "29VNK",
    "29VPJ",
    "29VPK",
    "30VUP",
    "30VUQ",
]

if __name__ == "__main__":
    Sentinel2ToStac.cli()
