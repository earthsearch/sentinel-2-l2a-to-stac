import json
import logging
import os
from pathlib import Path
from typing import Any

import requests
import stac_asset.blocking
from boto3utils import s3
from botocore.exceptions import ClientError
from pystac import Asset, Item
from pystac.extensions.file import FileExtension
from pystac.extensions.storage import StorageExtension, StorageScheme
from rasterio.errors import CRSError
from returns.result import Failure, ResultE, Success
from stac_asset import Config
from stactask import Task
from stactask.exceptions import InvalidInput
from stactask.utils import stac_jsonpath_match
from sentinel_2_l2a_to_stac.cogify import cogify, sha256sum_multihash, make_thumbnail

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


if __name__ == "__main__":
    Sentinel2ToStac.cli()
