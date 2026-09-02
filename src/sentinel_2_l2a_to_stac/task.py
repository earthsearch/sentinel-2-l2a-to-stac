import json
import os
from pathlib import Path
from typing import Any

import stac_asset.blocking
from boto3utils import s3
from botocore.exceptions import ClientError
from stac_asset import Config
from stactask import Task
from stactask.exceptions import InvalidInput
from stactools.sentinel2.stac import create_item

# RODA hosts the raw Sentinel-2 tiles in a public bucket with no STAC catalog,
# only the source metadata files. We reconstruct the product-level metadata
# href from tileInfo.json's `productPath`, which requires recovering the bucket
# from the granule href — that's the only use of this client.
s3_client = s3(requester_pays=False)


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

        # update_item (Earth Search overrides), is_newer_than_existing, COGs,
        # thumbnail, and upload are ported in later PRs. PR 3 returns the raw
        # create_item output.
        return [item.to_dict()]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


if __name__ == "__main__":
    Sentinel2ToStac.cli()
