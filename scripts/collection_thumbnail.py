#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "dask[array]>=2024.1",
#     "numpy>=2.4.6",
#     "odc-stac>=0.3.11",
#     "pillow~=12.0",
#     "pystac-client>=0.8",
#     "rasterio~=1.5",
# ]
# ///
"""Render a collection thumbnail.

Run directly: ./scripts/collection_thumbnail.py [--output PATH]
Or with uv: uv run --script scripts/collection_thumbnail.py [--output PATH]
"""

import argparse
from pathlib import Path

import numpy as np
import odc.stac
import rasterio
from PIL import Image, ImageOps
from pystac_client import Client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "assets" / "thumbnail.jpg"
SIZE = 1024
BBOX = (-71.85, 43.65, -71.25, 44.1)
DATE = "2024-10-18"
STAC_URL = "https://earth-search.aws.element84.com/v1"


def main() -> None:
    """Search, mosaic acquisition segments, and render a thumbnail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        metavar="PATH",
        help="Output JPEG path (default: %(default)s).",
    )
    output = parser.parse_args().output

    catalog = Client.open(STAC_URL)
    items = list(
        catalog.search(collections=["sentinel-2-l2a"], bbox=BBOX, datetime=DATE).items()
    )
    if not items:
        raise ValueError(f"No Sentinel-2 scenes found for {DATE} within {BBOX}")

    with rasterio.Env(
        GDAL_DISABLE_READDIR_ON_OPEN="TRUE",
        GDAL_HTTP_TIMEOUT="60",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    ):
        print("Loading and mosaicking Sentinel-2 acquisition segments", flush=True)
        data = odc.stac.load(
            items,
            bands=["red", "green", "blue"],
            bbox=BBOX,
            crs="EPSG:32619",
            resolution=50,
            groupby="solar_day",
            resampling="bilinear",
            dtype="float32",
            nodata=np.nan,
            chunks={"x": 512, "y": 512},
        ).compute()
    if data.sizes["time"] != 1:
        raise ValueError("Expected one acquisition day for the thumbnail")
    rgb = (
        data[["red", "green", "blue"]]
        .to_array(dim="band")
        .isel(time=0)
        .transpose("y", "x", "band")
    )
    if not np.isfinite(rgb).all().item():
        raise ValueError("Crop contains missing source pixels")

    # These Earth Search COGs already have the BOA offset applied.
    # Use a brighter 0-0.4 reflectance stretch than the collection's 0-0.6.
    rgb = (rgb * 0.0001 / 0.4).clip(0, 1) ** 0.5
    luminance = (
        0.299 * rgb.sel(band="red")
        + 0.587 * rgb.sel(band="green")
        + 0.114 * rgb.sel(band="blue")
    )
    rgb = (luminance + 1.2 * (rgb - luminance)).clip(0, 1)
    low = 1 / (1 + np.exp(4 * 0.3))
    high = 1 / (1 + np.exp(4 * (0.3 - 1)))
    rgb = (1 / (1 + np.exp(4 * (0.3 - rgb))) - low) / (high - low)
    image = Image.fromarray((rgb.clip(0, 1) * 255).round().astype("uint8").values)
    image = ImageOps.fit(image, (SIZE, SIZE), method=Image.Resampling.LANCZOS)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="JPEG", quality=92, optimize=True, subsampling=0)
    print(f"Saved {output} ({SIZE} x {SIZE})")


if __name__ == "__main__":
    main()
