import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Optional

import boto3  # type: ignore[import-untyped]
import requests
from botocore.exceptions import ClientError
from pystac import Asset, Item, MediaType, STACObject
from pystac.errors import TemplateError
from pystac.extensions.file import FileExtension
from pystac.extensions.storage import StorageExtension, StorageScheme
from pystac.layout import LayoutTemplate
from rasterio.errors import CRSError
from returns.result import Failure, ResultE, Success
from stactask import Task
from stactask.exceptions import InvalidInput
from stactask.utils import stac_jsonpath_match

from sentinel_2_l2a_to_stac.cogify import (
    THUMBNAIL_TITLE,
    CogFile,
    FileInfo,
    cogify,
    make_thumbnail,
    sha256sum_multihash,
    stream_file_info,
)
from sentinel_2_l2a_to_stac.constants import (
    CANONICAL_L2A_IMAGE_PATHS,
    SENTINEL_BANDS,
)
from sentinel_2_l2a_to_stac.downgrade import downgrade_item
from sentinel_2_l2a_to_stac.footprint import data_footprint
from sentinel_2_l2a_to_stac.metadata import Metadata, parse_metadata
from sentinel_2_l2a_to_stac.safe import resolve_safe_layout

# SHIM(pystac-2.0): stac-asset reads asset.media_type when downloading but pystac
# 2.0 renamed the field to Asset.type. Remove once stac-asset is updated.
if not hasattr(Asset, "media_type"):
    Asset.media_type = property(lambda self: self.type)  # type: ignore[attr-defined]

from sentinel_2_l2a_to_stac.stac import create_image_assets, create_item


def _patched_get_template_value(
    self: "LayoutTemplate", stac_object: "STACObject", template_var: str
) -> Any:
    """Fixed copy of ``LayoutTemplate._get_template_value``.

    SHIM(pystac-2.0): upstream does ``if prop in v`` without guarding against
    ``v`` being a plain object (e.g. the Item itself, reached whenever
    template_var matches one of the object's own attribute names, such as
    ``id``). That raises ``TypeError: argument of type 'Item' is not
    iterable`` instead of falling through to the ``hasattr``/``getattr``
    branch. Remove once fixed upstream.
    """
    if template_var in self.ITEM_TEMPLATE_VARS:
        if isinstance(stac_object, Item):
            dt = stac_object.datetime
            if dt is None:
                dt = stac_object.common_metadata.start_datetime
            if dt is None:
                raise TemplateError(
                    f"Item {stac_object} does not have a datetime or "
                    f"datetime range set; cannot template {template_var} "
                    f"in {self.template}"
                )

            if template_var == "year":
                return dt.year
            if template_var == "month":
                return dt.month
            if template_var == "day":
                return dt.day
            if template_var == "date":
                return dt.date().isoformat()

            if template_var == "collection":
                if stac_object.collection_id is not None:
                    return stac_object.collection_id
                raise TemplateError(
                    f"Item {stac_object} does not have a collection ID set; "
                    f"cannot template {template_var} in {self.template}"
                )
        else:
            raise TemplateError(
                f'"{template_var}" cannot be used to template non-Item '
                f"{stac_object} in {self.template}"
            )

    props = template_var.split(".")
    prop_source: STACObject | dict[str, Any] | None = None
    error = TemplateError(
        f"Cannot find property {template_var} on {stac_object} for template "
        f"{self.template}"
    )

    try:
        if hasattr(stac_object, props[0]):
            prop_source = stac_object

        if prop_source is None and hasattr(stac_object, "properties"):
            obj_props: dict[str, Any] | None = stac_object.properties
            if obj_props is not None and props[0] in obj_props:
                prop_source = obj_props

        if prop_source is None and hasattr(stac_object, "extra_fields"):
            extra_fields: dict[str, Any] | None = stac_object.extra_fields
            if extra_fields is not None and props[0] in extra_fields:
                prop_source = extra_fields

        if prop_source is None:
            raise error

        v: Any = prop_source
        for prop in template_var.split("."):
            try:
                is_member = prop in v
            except TypeError:
                is_member = False
            if is_member:
                v = v[prop]
            elif hasattr(v, prop):
                v = getattr(v, prop)
            else:
                raise error
    except TemplateError as e:
        if template_var in self.defaults:
            return self.defaults[template_var]
        raise e

    return v


LayoutTemplate._get_template_value = _patched_get_template_value  # type: ignore[method-assign]

# stactask 0.7.0 already prefixes lines with payload id, so the legacy
# logging change was deliberately left off.
logging.getLogger().setLevel(os.getenv("CIRRUS_LOG_LEVEL", "WARN"))
for _noisy_logger in ("botocore", "rasterio"):
    logging.getLogger(_noisy_logger).propagate = False

# Every source read (listing, get_object, download_file) goes through this
# client with RequestPayer="requester", since some source buckets (e.g. the
# AWS Open Data Sentinel-2 archive) are requester-pays. Uploads (writes) are
# handled separately by stactask's own authenticated client.
_s3_client = boto3.client("s3")
S3_REQUEST_PAYER = "requester"

# Read size for streamed S3 bodies, which are measured rather than buffered.
S3_STREAM_CHUNK_SIZE = 8 * 1024 * 1024


def _parse_s3_url(url: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URL into (bucket, key)."""
    if not url.startswith("s3://"):
        raise ValueError(f"Not an S3 URL: {url}")
    bucket, _, key = url.removeprefix("s3://").partition("/")
    return bucket, key


def _list_s3_keys(client: Any, bucket: str, prefix: str) -> Iterator[str]:
    """Yield every object key in `bucket` starting with `prefix`, paginated."""
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, RequestPayer=S3_REQUEST_PAYER
    ):
        for obj in page.get("Contents", []):
            yield obj["Key"]


# Storage extension (schemes/refs model, from the pinned pystac 2.0-dev build;
# see [tool.uv.sources] in pyproject.toml). One aws-s3 scheme,
# "earthsearch", for assets uploaded to the Earth Search output bucket or
# already living there. A "local" placeholder scheme is used in --local/test
# runs where no upload occurs. In v2 `platform` is the access-endpoint
# URI/template (provider identity moved to `type`), unlike v1's literal
# "AWS". `bucket` is an additional property required by the aws-s3
# best-practices template.
EARTHSEARCH_SCHEME_KEY = "earthsearch"
LOCAL_SCHEME_KEY = "local"
LOCAL_BUCKET = "local"
STORAGE_PLATFORM = "https://{bucket}.s3.{region}.amazonaws.com"
STORAGE_REGION = "us-west-2"

THUMBNAIL_ASSET_NAME = "thumbnail"
THUMBNAIL_SOURCE_ASSET_NAME = "preview"

EXPECTED_COGIFIED_COUNT = 19

# Filename of each canonical source image once it is in the (flat) workdir.
CANONICAL_IMAGE_FILENAMES: dict[str, str] = {
    key: os.path.basename(path) for key, path in CANONICAL_L2A_IMAGE_PATHS.items()
}

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
}


def find_existing_stac_doc_filename(
    bucket_filenames: set[str], item_id: str
) -> Optional[str]:
    """Locate the existing product STAC doc's filename in a prefix listing.

    The doc is named after the product. Requires
    item_id, so this can only run after create_item builds the item.
    """
    filename = f"{item_id}.json"
    return filename if filename in bucket_filenames else None


@dataclass(frozen=True)
class SourceProduct:
    """A resolved input product: where its files are and how to process it.

    Produced by resolving either an already-cogified Earth Search granule
    prefix (`metadata_href`) or a SAFE archive (`safe_href`) into the one
    shape the rest of the pipeline works in. A granule prefix must already
    carry the flat Earth Search COG layout, and a missing required file is a
    hard failure rather than a fallback.
    """

    prefix: str
    # Canonical asset key -> href of the raster to read (a COG for a granule
    # prefix, a JP2 to COGify for a SAFE archive).
    image_hrefs: dict[str, str]
    # Basenames present under `prefix`; empty for a SAFE archive.
    bucket_filenames: set[str]
    # True only for a SAFE archive, which always needs COGs created.
    create_cogs: bool


def _validate_processing_baseline(processing_baseline: str) -> None:
    if (
        processing_baseline < "05.00"
        or processing_baseline == "05.09"
        or processing_baseline > "05.99"
    ):
        raise InvalidInput(f"Invalid processing baseline ({processing_baseline})")


@contextmanager
def _item_errors(logger: Any) -> Iterator[None]:
    """Translate metadata-parsing/item-building failures into InvalidInput."""
    try:
        yield
    except InvalidInput:
        raise
    except ValueError as ex:
        logger.error(ex)
        raise InvalidInput(f"Invalid input metadata: {ex}")
    except AssertionError as ex:
        logger.error(ex)
        raise InvalidInput(
            f"Assertion failed, likely because of an invalid geometry: {ex}"
        )
    except Exception as ex:
        logger.error(ex)
        if "Cannot find granule tile_id granule metadata" in str(ex):
            raise InvalidInput("Unable to parse older metadata file format")
        raise Exception(f"Unable to create item: {ex}") from ex


class Sentinel2ToStac(Task):
    name = "sentinel-2-l2a-to-stac"
    description = "Sentinel-2 L2A to STAC Cirrus task"
    version = "v2026.09.30"  # keep in sync with pyproject.toml and CHANGELOG.md

    def validate(self) -> bool:
        # Rewritten for stactask 0.6.1 (requires self._payload instead of
        # payload arg)
        if "metadata_href" not in self._payload and "safe_href" not in self._payload:
            raise InvalidInput("metadata_href or safe_href required")
        return True

    @property
    def granule_metadata_xml_path(self) -> Path:
        return Path(self._workdir.joinpath("metadata.xml"))

    @property
    def product_metadata_xml_path(self) -> Path:
        return Path(self._workdir.joinpath("product_metadata.xml"))

    def update_item(self, item: Item) -> Item:
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
        if existing_item_r.status_code == 403:
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
        """List the basenames of every file directly under a prefix, local or S3."""
        if "://" not in s3_path:
            return {p.name for p in Path(s3_path).iterdir() if p.is_file()}
        bucket, prefix = _parse_s3_url(
            s3_path if s3_path.endswith("/") else f"{s3_path}/"
        )
        return {
            key.rsplit("/", 1)[-1] for key in _list_s3_keys(_s3_client, bucket, prefix)
        }

    def load_existing_stac_doc(self, s3_path: str, filename: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.read_href(f"{s3_path}/{filename}"))
        return result

    def apply_earthsearch_hrefs(self, item: Item, s3_path: str) -> Item:
        """Overwrite every asset href from the flat filename<->key map.

        Reference-path-only. One consistent rule for every asset, whether or
        not it's already in the existing doc: {prefix}/{FILENAME}. Called
        after update_item, which never touches asset hrefs, so it's harmless
        that they still point at wherever create_item first put them.
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
            title=THUMBNAIL_TITLE,
        )
        item.assets[THUMBNAIL_ASSET_NAME] = asset
        asset.set_owner(item)
        return item

    def apply_reference_file_info(
        self,
        item: Item,
        bucket_filenames: set[str],
        existing_stac_doc: dict[str, Any],
        measured: dict[str, FileInfo],
    ) -> Item:
        """Populate file:size/file:checksum for every reference-path asset.

        Reuse them from the existing doc when it already carries both fields
        for this asset, then from `measured` (the footprint pass already had
        those rasters on disk), and only otherwise go back to the bucket --
        streamed, so no asset is ever held in memory or written to the
        workdir. An asset missing from the bucket is a hard input error. Any
        doc asset that isn't one of `item`'s own keys is never consulted,
        which is what makes an extra doc-only asset a no-op.
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
                continue

            info = measured.get(key) or self.remote_file_info(asset.href)
            fext.size = info.size
            fext.checksum = info.checksum

        return item

    def measure_reference_images(
        self, image_hrefs: dict[str, str]
    ) -> tuple[Optional[dict[str, Any]], dict[str, FileInfo]]:
        """Footprint and file info for an already-cogified granule.

        Each raster is fetched, measured, hashed and deleted before the next
        one is fetched, so the workdir never holds more than a single image.
        Only the reflectance bands are fetched at all; every other asset is
        measured straight off the bucket by `apply_reference_file_info`.
        """
        hrefs = {k: h for k, h in image_hrefs.items() if k in SENTINEL_BANDS}
        self.logger.info(f"Computing the data footprint from {len(hrefs)} rasters")
        file_info: dict[str, FileInfo] = {}

        def staged() -> Iterator[str]:
            for key, href in hrefs.items():
                local = self._workdir / CANONICAL_IMAGE_FILENAMES[key]
                self.fetch_source_images({key: href})
                try:
                    file_info[key] = FileInfo(
                        local.stat().st_size, sha256sum_multihash(str(local))
                    )
                    # data_footprint is done with each href before it asks for
                    # the next one, so the file can go as soon as it resumes.
                    yield str(local)
                finally:
                    local.unlink(missing_ok=True)

        return data_footprint(staged()), file_info

    def fetch_source_images(self, image_hrefs: dict[str, str]) -> None:
        """Fetch each canonical source image into the workdir.

        Every image lands under its canonical flat filename (``B02.jp2``,
        ``CLD_20m.jp2``, ...) regardless of what it was called in the archive.
        Already-present files are left alone so a saved workdir is reused.
        """
        pending = {
            key: href
            for key, href in image_hrefs.items()
            if not (self._workdir / CANONICAL_IMAGE_FILENAMES[key]).exists()
        }
        remote = {k: h for k, h in pending.items() if "://" in h}

        for key, href in pending.items():
            if key not in remote:
                shutil.copy(href, self._workdir / CANONICAL_IMAGE_FILENAMES[key])

        if not remote:
            return

        self.logger.info(f"Downloading {len(remote)} source images")
        for key, href in remote.items():
            bucket, s3_key = _parse_s3_url(href)
            _s3_client.download_file(
                bucket,
                s3_key,
                str(self._workdir / CANONICAL_IMAGE_FILENAMES[key]),
                ExtraArgs={"RequestPayer": S3_REQUEST_PAYER},
            )

    def cogify_source_images(
        self, metadata: Metadata, image_hrefs: dict[str, str]
    ) -> dict[str, CogFile]:
        """Fetch and COGify the canonical image set, before any Item exists.

        The COGs are the assets the Item is subsequently built from, so this
        has to run first. The throwaway assets handed to ``cogify`` come from
        the same builder ``create_item`` uses, so the COG parameters match the
        published band metadata exactly.
        """
        try:
            self.fetch_source_images(image_hrefs)

            assets = create_image_assets(
                str(self._workdir),
                metadata,
                [CANONICAL_IMAGE_FILENAMES[key] for key in image_hrefs],
            )

            cogs: dict[str, CogFile] = {}
            for asset_name, asset in assets.items():
                self.logger.info(f"Converting {asset_name} {asset.href} to COG")
                cogs[asset_name] = cogify(asset_name, asset)
                # The footprint was measured before this ran and nothing else
                # reads the source image again, so drop it rather than keeping
                # both representations of every band in the workdir.
                Path(asset.href).unlink(missing_ok=True)
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

        self.logger.info(f"Cogified {len(cogs)} assets.")
        if len(cogs) != EXPECTED_COGIFIED_COUNT:
            self.logger.error(
                f"Cogified {len(cogs)} assets, expected {EXPECTED_COGIFIED_COUNT}"
            )

        return cogs

    def is_local_asset(self, asset: Asset) -> bool:
        return bool(asset.href.startswith(str(self._workdir)))

    def get_local_asset_keys(self, item: Item) -> list[str]:
        return [key for key, asset in item.assets.items() if self.is_local_asset(asset)]

    def add_storage_schemes(self, item: Item) -> Item:
        """Add per-bucket storage schemes and asset refs after upload.

        Classifies each asset by its final href: Earth Search bucket (uploaded
        COGs, thumbnail, and the source metadata files, which are re-uploaded
        rather than referenced in place) or local path (--local/test runs).
        Each group gets its own named scheme so the `bucket` template variable
        is always defined.
        """
        earthsearch_keys: list[str] = []
        local_keys: list[str] = []

        for key, asset in item.assets.items():
            if asset.href.startswith("s3://"):
                earthsearch_keys.append(key)
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

        if earthsearch_keys:
            es_bucket, _ = _parse_s3_url(item.assets[earthsearch_keys[0]].href)
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
        if "://" not in href:
            return Path(href).read_bytes()
        with self._s3_object_body(href) as body:
            return bytes(body.read())

    def remote_file_info(self, href: str) -> FileInfo:
        """Size and checksum of an object, streamed rather than buffered."""
        if "://" not in href:
            return FileInfo(os.path.getsize(href), sha256sum_multihash(href))
        with self._s3_object_body(href) as body:
            return stream_file_info(iter(lambda: body.read(S3_STREAM_CHUNK_SIZE), b""))

    @contextmanager
    def _s3_object_body(self, href: str) -> Iterator[Any]:
        bucket, key = _parse_s3_url(href)
        try:
            response = _s3_client.get_object(
                Bucket=bucket, Key=key, RequestPayer=S3_REQUEST_PAYER
            )
        except ClientError as err:
            if err.response["Error"]["Code"] == "NoSuchKey":
                msg = f"Failed fetching href '{href}' ({err})"
                self.logger.error(msg, exc_info=True)
                raise InvalidInput(msg)
            else:
                raise
        body = response["Body"]
        try:
            yield body
        finally:
            body.close()

    def list_files(self, prefix: str) -> list[str]:
        """List every file href underneath a prefix, local or S3."""
        if "://" in prefix:
            bucket, key_prefix = _parse_s3_url(f"{prefix}/")
            return [
                f"s3://{bucket}/{key}"
                for key in _list_s3_keys(_s3_client, bucket, key_prefix)
            ]
        return [str(p) for p in Path(prefix).rglob("*") if p.is_file()]

    def resolve_source(self) -> SourceProduct:
        if safe_href := self._payload.get("safe_href"):
            return self.resolve_safe_source(safe_href)
        return self.resolve_granule_source(self._payload["metadata_href"])

    def resolve_granule_source(self, metadata_href: str) -> SourceProduct:
        """Resolve an already-cogified Earth Search granule prefix.

        Product/granule metadata and every canonical COG are required
        directly under `prefix`; any of them missing is a hard failure, not a
        fallback. Each download is guarded by .exists() so a saved workdir is
        reused without re-fetching.
        """
        prefix = os.path.dirname(metadata_href)
        bucket_filenames = self.list_bucket_filenames(prefix)

        if not self.product_metadata_xml_path.exists():
            self.product_metadata_xml_path.write_bytes(
                self.read_href(f"{prefix}/product_metadata.xml")
            )

        if not self.granule_metadata_xml_path.exists():
            self.granule_metadata_xml_path.write_bytes(
                self.read_href(f"{prefix}/metadata.xml")
            )

        return SourceProduct(
            prefix=prefix,
            image_hrefs=self.resolve_image_hrefs(prefix, bucket_filenames),
            bucket_filenames=bucket_filenames,
            create_cogs=False,
        )

    @staticmethod
    def resolve_image_hrefs(prefix: str, bucket_filenames: set[str]) -> dict[str, str]:
        """Locate the canonical COGs at the flat Earth Search product prefix.

        Every one of them is required; a granule prefix with any missing is
        not a supported input, so this fails rather than falling back to
        anything else.
        """
        missing = sorted(
            ASSET_FILENAMES[key]
            for key in CANONICAL_L2A_IMAGE_PATHS
            if ASSET_FILENAMES[key] not in bucket_filenames
        )
        if missing:
            raise InvalidInput(
                f"Expected COG(s) not found at {prefix}: {', '.join(missing)}"
            )
        return {
            key: f"{prefix}/{ASSET_FILENAMES[key]}" for key in CANONICAL_L2A_IMAGE_PATHS
        }

    def resolve_safe_source(self, safe_href: str) -> SourceProduct:
        """Resolve a SAFE archive and copy its metadata into the workdir.

        A SAFE archive carries no COGs, so COG creation is the only way to get
        publishable assets out of it and is always on.
        """
        prefix = safe_href.removesuffix("/manifest.safe")
        layout = resolve_safe_layout(prefix, self.list_files(prefix))

        if not self.product_metadata_xml_path.exists():
            self.product_metadata_xml_path.write_bytes(
                self.read_href(layout.product_metadata_href)
            )
        if not self.granule_metadata_xml_path.exists():
            self.granule_metadata_xml_path.write_bytes(
                self.read_href(layout.granule_metadata_href)
            )

        return SourceProduct(
            prefix=prefix,
            image_hrefs=layout.image_hrefs,
            bucket_filenames=set(),
            create_cogs=True,
        )

    def data_geometry(self, source: SourceProduct) -> Optional[dict[str, Any]]:
        """Union the valid-data footprints of the product's reflectance bands.

        Only the spectral bands (B01, B02, ... not AOT/WVP/SCL/TCI/PVI/cloud/
        snow) are used; the derived/aux products don't represent the actual
        data extent. Returns None when no raster could be read.
        """
        hrefs = [
            str(local)
            if (local := self._workdir / CANONICAL_IMAGE_FILENAMES[key]).exists()
            else href
            for key, href in source.image_hrefs.items()
            if key in SENTINEL_BANDS
        ]
        self.logger.info(f"Computing the data footprint from {len(hrefs)} rasters")
        return data_footprint(hrefs)

    def build_item(self, metadata: Metadata) -> Item:
        """Build the Item from the workdir and apply the Earth Search overrides."""
        with _item_errors(self.logger):
            item = create_item(str(self._workdir), metadata=metadata)

        try:
            return self.update_item(item)
        except Exception as ex:
            self.logger.error(ex)
            raise Exception(f"Unable to update item: {ex}")

    def process(self, **kwargs: Any) -> list[dict[str, Any]]:
        v1_output = self._payload.get("v1_output", False)
        source = self.resolve_source()

        with _item_errors(self.logger):
            metadata = parse_metadata(str(self._workdir))

        _validate_processing_baseline(
            metadata.metadata_dict.get("s2:processing_baseline", "0")
        )

        # The SAFE path fetches the whole image set up front: the COG step
        # reuses the same files, and footprint reads are local rather than
        # full-band reads off the remote bucket (which GDAL has no retry logic
        # for). The reference path has nothing to COGify, so it measures and
        # discards one raster at a time instead of holding all of them.
        measured: dict[str, FileInfo] = {}
        if source.create_cogs:
            self.fetch_source_images(source.image_hrefs)
            geometry = self.data_geometry(source)
        else:
            geometry, measured = self.measure_reference_images(source.image_hrefs)

        if geometry is not None:
            metadata = replace(metadata, geometry=geometry)

        # Build the Item up front to run the two cheap gates: the collection
        # lookup needs a whole Item dict, and COGifying is far too expensive to
        # do before knowing whether this scene will be emitted at all.
        item = self.build_item(metadata)

        existing_doc_filename = find_existing_stac_doc_filename(
            source.bucket_filenames, item.id
        )
        existing_stac_doc: dict[str, Any] = (
            self.load_existing_stac_doc(source.prefix, existing_doc_filename)
            if existing_doc_filename is not None
            else {}
        )
        if existing_doc_filename is not None:
            self.logger.info(
                f"Found existing STAC doc '{existing_doc_filename}' with "
                f"{len(existing_stac_doc.get('assets', {}))} assets"
            )

        match self.is_newer_than_existing(item):
            case Failure(e):
                raise e
            case Success(False):
                return []
            case _:
                pass

        if source.create_cogs:
            self.logger.info("Making COGs for the canonical image set")
            cogs = self.cogify_source_images(metadata, source.image_hrefs)

            # Rebuild the Item over the COGs just written to the workdir.
            self.logger.info("Rebuilding item from the created COGs")
            item = self.build_item(
                replace(
                    metadata,
                    image_paths=[cog.filename for cog in cogs.values()],
                    image_media_type=MediaType.COG,
                )
            )
            for key, cog in cogs.items():
                FileExtension.ext(item.assets[key], add_if_missing=True).apply(
                    checksum=cog.checksum, size=cog.size
                )

            self.logger.info("Making preview thumbnail")
            try:
                item = make_thumbnail(item)
            except Exception:
                self.logger.exception("Cannot create JPEG thumbnail")
                raise
        else:
            # The item references the already-cogified assets on Earth Search;
            # reuse file info from a prior STAC doc when one was found above.
            self.logger.info("Applying earthsearch hrefs")
            item = self.apply_earthsearch_hrefs(item, source.prefix)
            item = self.add_thumbnail_asset(item, source.prefix)

            self.logger.info("Reusing/downloading asset file info")
            item = self.apply_reference_file_info(
                item, source.bucket_filenames, existing_stac_doc, measured
            )

        self.logger.info("Adding fileinfo to assets")
        item = self.add_fileinfo_to_local_assets(item)

        self.logger.info("Uploading assets")
        item = self.upload_item_assets_to_s3(item, self.get_local_asset_keys(item))

        self.logger.info("Adding storage schemes")
        item = self.add_storage_schemes(item)

        out = self.add_software_version_to_item(item.to_dict())
        return [downgrade_item(out) if v1_output else out]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


def _set_asset_owners(item: Item) -> None:
    for asset in item.assets.values():
        asset.set_owner(item)


if __name__ == "__main__":
    Sentinel2ToStac.cli()
