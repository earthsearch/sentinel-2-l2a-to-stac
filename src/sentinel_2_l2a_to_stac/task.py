import json
import os
from pathlib import Path
from typing import Any

import requests
import stac_asset.blocking
from boto3utils import s3
from botocore.exceptions import ClientError
from pystac import Item, Link, MediaType, RelType
from pystac.extensions.storage import StorageExtension, StorageScheme
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

        # COGs, thumbnail, and upload are ported in later PRs.
        return [item.to_dict()]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


if __name__ == "__main__":
    Sentinel2ToStac.cli()
