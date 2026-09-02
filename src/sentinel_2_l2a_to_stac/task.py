from datetime import datetime
from typing import Any

import pystac
from stactask import Task
from stactask.exceptions import InvalidInput


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

    def process(self, **kwargs: Any) -> list[dict[str, Any]]:
        if "invalid" in self.process_definition.get("id", ""):
            raise Exception("invalid")

        # create an item
        item = pystac.Item(
            id="example-item",
            geometry={
                "type": "Polygon",
                "coordinates": [
                    [
                        [-71.4667693618289, 43.3262376051166],
                        [-71.4278035338514, 42.3392708844627],
                        [-70.6447744862405, 42.3532726633038],
                        [-70.2637344878527, 43.3458642540582],
                        [-71.4667693618289, 43.3262376051166],
                    ]
                ],
            },
            bbox=[-71.466769, 42.339271, -70.263734, 43.345864],
            datetime=datetime(2025, 3, 4, 15, 41, 14),
            properties={"example-property": "value"},
        )

        # add an asset
        item.add_asset(
            "example_asset",
            pystac.Asset(
                title="Example",
                href="http://example.com/example_asset.tif",
                media_type="image/tiff",
            ),
        )

        # return a list of Items
        return [item.to_dict()]


def lambda_handler(
    event: dict[str, Any], context: dict[str, Any] = {}
) -> dict[str, Any]:
    return Sentinel2ToStac.handler(payload=event)


if __name__ == "__main__":
    Sentinel2ToStac.cli()
