import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import boto3  # type: ignore[import-untyped]
import requests
import stac_asset.blocking
from boto3utils import s3
from botocore import UNSIGNED
from botocore.config import Config as BotoClientConfig
from botocore.exceptions import ClientError
from pystac import Asset, Item, MediaType
from pystac.extensions.file import FileExtension
from pystac.extensions.storage import StorageExtension, StorageScheme
from rasterio.errors import CRSError
from returns.result import Failure, ResultE, Success
from stac_asset import Config
from stactask import Task
from stactask.exceptions import InvalidInput
from stactask.utils import stac_jsonpath_match

from sentinel_2_l2a_to_stac.cogify import cogify, make_thumbnail, sha256sum_multihash

# SHIM(pystac-2.0): stac-asset reads asset.media_type when downloading but pystac
# 2.0 renamed the field to Asset.type. Remove once stac-asset is updated.
if not hasattr(Asset, "media_type"):
    Asset.media_type = property(lambda self: self.type)  # type: ignore[attr-defined]

from sentinel_2_l2a_to_stac.stac import create_item

# stactask 0.7.0 already prefixes lines with payload id, so the legacy
# logging change was deliberately left off.
logging.getLogger().setLevel(os.getenv("CIRRUS_LOG_LEVEL", "WARN"))
for _noisy_logger in ("botocore", "rasterio"):
    logging.getLogger(_noisy_logger).propagate = False

s3_client = s3(requester_pays=False)
# list_bucket_names is the only call made through the s3 client,
# and should only ever hit buckets allowing anonymous access
s3_client.s3 = boto3.client("s3", config=BotoClientConfig(signature_version=UNSIGNED))

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

# Filename <-> asset key map for the earthsearch flat product-prefix layout. 
# Every entry is verified against create_item's actual output keys.
ASSET_FILENAMES: dict[str, str] = {
    "aot": "AOT.tif",
    "coastal": "B01.tif",
    "blue": "B02.tif",
    "green": "B03.tif",
    "red": "B04.tif",
    "rededge1": "B05.tif",
    "rededge2": "B06.tif",
    "rededge3": "B07.tif",
    "nir": "B08.tif",
    "nir09": "B09.tif",
    "swir16": "B11.tif",
    "swir22": "B12.tif",
    "nir08": "B8A.tif",
    "cloud": "CLD_20m.tif",
    "snow": "SNW_20m.tif",
    "visual": "TCI.tif",
    "wvp": "WVP.tif",
    "scl": "SCL.tif",
    "preview": "L2A_PVI.tif",
    THUMBNAIL_ASSET_NAME: "L2A_PVI.jpg",
    "granule_metadata": "metadata.xml",
    "product_metadata": "product_metadata.xml",
    "tileinfo_metadata": "tileInfo.json",
}


def _prune_to_canonical_assets(item: Item) -> Item:
    """Drop create_item's non-native-resolution "*m" variants and the thumbnail.

    create_item emits ~40 asset keys, including *m-suffixed duplicates for
    every resolution other than a band's native one (e.g. red_20m,
    visual_60m). None of the 23 canonical asset keys end in "m", so filtering
    on that suffix is safe.
    """
    for key in list(item.assets.keys()):
        if key.endswith("m") or key == THUMBNAIL_ASSET_NAME:
            del item.assets[key]
    return item


def _resolve_product_metadata_href(
    s3_path: str, bucket_filenames: set[str], bucket: str, product_path: str
) -> str:
    """Resolve product_metadata.xml: flat earthsearch layout first, RODA fallback.
    """
    if "product_metadata.xml" in bucket_filenames:
        return f"{s3_path}/product_metadata.xml"
    return f"s3://{bucket}/{product_path}/metadata.xml"


def find_existing_stac_doc_filename(
    bucket_filenames: set[str], item_id: str
) -> Optional[str]:
    """Locate the existing product STAC doc's filename in a prefix listing.

    The doc is named after the product. Requires
    item_id, so this can only run after create_item builds the item.
    """
    filename = f"{item_id}.json"
    return filename if filename in bucket_filenames else None


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

    def list_bucket_filenames(self, s3_path: str) -> set[str]:
        """List the basenames of every object directly under an S3 prefix."""
        prefix = s3_path if s3_path.endswith("/") else f"{s3_path}/"
        return {href.rsplit("/", 1)[-1] for href in s3_client.find(prefix)}

    def load_existing_stac_doc(self, s3_path: str, filename: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.read_href(f"{s3_path}/{filename}"))
        return result

    def apply_earthsearch_hrefs(self, item: Item, s3_path: str) -> Item:
        """Overwrite every asset href from the flat filename<->key map.

        Reference-path-only. One consistent rule for every asset, whether or
        not it's already in the existing doc: {prefix}/{FILENAME}. Called
        after update_item, so it's harmless that update_item already set
        RODA/local hrefs first.
        """
        for key, asset in item.assets.items():
            asset.href = f"{s3_path}/{ASSET_FILENAMES[key]}"
        return item

    def add_thumbnail_asset(self, item: Item, s3_path: str) -> Item:
        """Add the thumbnail asset manually.

        create_item never produces one (make_thumbnail does, but that's
        cog-creation-path only); the reference path instead reuses the JPEG
        preview that already exists in the bucket.
        """
        asset = Asset(
            href=f"{s3_path}/{ASSET_FILENAMES[THUMBNAIL_ASSET_NAME]}",
            type=MediaType.JPEG,
            roles=["thumbnail"],
        )
        item.assets[THUMBNAIL_ASSET_NAME] = asset
        asset.set_owner(item)
        return item

    def apply_reference_file_info(
        self,
        item: Item,
        bucket_filenames: set[str],
        existing_stac_doc: dict[str, Any],
    ) -> Item:
        """Populate file:size/file:checksum for every reference-path asset.

        Reuse
        from the existing doc when it already carries both fields for this
        asset, otherwise download the object from earthsearch and compute
        them fresh. An asset missing from the bucket can't be serviced
        without create_cogs=True, so it's a hard input error here. Any doc
        asset that isn't one of `item`'s own keys is never consulted, which
        is what makes an extra doc-only asset a no-op.
        """
        doc_assets = existing_stac_doc.get("assets", {})
        for key, asset in item.assets.items():
            filename = ASSET_FILENAMES[key]
            if filename not in bucket_filenames:
                raise InvalidInput(
                    f"Expected asset '{key}' ({filename}) not found in the bucket"
                )

            fext = FileExtension.ext(asset, add_if_missing=True)
            doc_asset = doc_assets.get(key, {})
            if doc_type := doc_asset.get("type"):
                asset.type = doc_type
            size = doc_asset.get("file:size")
            checksum = doc_asset.get("file:checksum")
            if size is not None and checksum is not None:
                fext.size = size
                fext.checksum = checksum
            else:
                tmp_path = self._workdir / filename
                tmp_path.write_bytes(self.read_href(asset.href))
                try:
                    fext.size = tmp_path.stat().st_size
                    fext.checksum = sha256sum_multihash(str(tmp_path))
                finally:
                    tmp_path.unlink()

        return item

    def make_cogs_for_item(self, item: Item) -> Item:
        try:
            processing_baseline = item.properties.get("s2:processing_baseline", "0")
            if processing_baseline < "05.00" or processing_baseline == "05.09":
                raise InvalidInput(
                    f"Processing baseline is {processing_baseline}, "
                    "only >= 05.00 (not including 5.09) is supported."
                )

            item = _prune_to_canonical_assets(item)
            assets_to_cogify = [
                # pystac 2.0 renamed Asset.media_type → Asset.type
                key
                for key, asset in item.assets.items()
                if asset.type == "image/jp2"
            ]

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
        create_cogs = self._payload.get("create_cogs", False)
        s3_path = os.path.dirname(metadata_href)

        bucket_filenames = self.list_bucket_filenames(s3_path)

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
            product_metadata_href = _resolve_product_metadata_href(
                s3_path,
                bucket_filenames,
                parts["bucket"],
                l2a_tileinfo["productPath"],
            )
            self.product_metadata_xml_path.write_bytes(
                self.read_href(product_metadata_href)
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

        # Only knowable once the item (and therefore its id) exists: the doc
        # is named after the product, so this can't run any earlier.
        existing_doc_filename = find_existing_stac_doc_filename(
            bucket_filenames, item.id
        )
        existing_stac_doc = (
            self.load_existing_stac_doc(s3_path, existing_doc_filename)
            if existing_doc_filename is not None
            else None
        )
        if existing_stac_doc is not None:
            self.logger.info(
                f"Found existing STAC doc '{existing_doc_filename}' with "
                f"{len(existing_stac_doc.get('assets', {}))} assets"
            )

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

        if create_cogs and existing_stac_doc is None:
            self.logger.info("Making COGs for item assets")
            item = self.make_cogs_for_item(item)

            self.logger.info("Making preview thumbnail")
            try:
                item = make_thumbnail(item)
            except Exception:
                self.logger.exception("Cannot create JPEG thumbnail")
                raise
        elif existing_stac_doc is not None:
            # Reference/update path: an existing product doc means we're
            # refreshing an already-ingested earthsearch item.
            # Requires every expected asset to already be in the bucket.
            item = _prune_to_canonical_assets(item)

            self.logger.info("Applying earthsearch hrefs")
            item = self.apply_earthsearch_hrefs(item, s3_path)
            item = self.add_thumbnail_asset(item, s3_path)

            self.logger.info("Reusing/downloading asset file info")
            item = self.apply_reference_file_info(
                item, bucket_filenames, existing_stac_doc
            )

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


if __name__ == "__main__":
    Sentinel2ToStac.cli()
