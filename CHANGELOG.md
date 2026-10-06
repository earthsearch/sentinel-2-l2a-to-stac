# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This project adheres to [CalVer](https://calver.org/). See the
[README](README.md#versions-and-releases) for the specific CalVer format
used by this project.

## [Unreleased]

### Changed

- Reduced peak memory and disk use of the COG pipeline:
  - `cogify` opens the source image directly instead of copying the whole
    encoded file through a `MemoryFile`, and writes the COG straight to disk
    instead of buffering it in a `MemoryFile` and reading it back into memory
    to checksum it. The checksum now streams the written file.
  - Source images are deleted from the workdir once their COG has been
    written, so both representations of every band are no longer held at once.
- Reduced peak disk and memory use of the `metadata_href` (existing-COGs)
  path, which used to download all 19 COGs into the workdir and hold them for
  the whole run, then fetch each asset a second time, in full, in memory, to
  checksum it. It now fetches only the reflectance bands the footprint
  actually needs, one at a time, hashing and deleting each before fetching the
  next, and measures the remaining assets by streaming them from the bucket
  instead of buffering them.

## [v2026.09.30]

First release of the rewritten task: the legacy Sentinel-2 C1 L2A→STAC Cirrus
task ported forward onto STAC 1.1.0 output and a pystac 2.0 baseline.

### Added

- Full Sentinel-2 L2A → STAC pipeline with two supported inputs:
  - `safe_href`: a `.SAFE` archive, always COGified from scratch.
  - `metadata_href`: an Earth Search granule prefix that must already carry
    the full flat COG layout (product/granule metadata plus all 19 canonical
    COGs). This path never COGifies source imagery in place and has no
    fallback for locating product metadata outside the granule prefix; any
    required file that isn't present is a hard `InvalidInput` failure.
- Reference/update path: when the output prefix already contains a
  `{item_id}.json`, the task reuses `file:size`/`file:checksum` from the
  existing doc for assets whose info is already present, downloads only the
  assets whose info is missing, rewrites hrefs to the flat Earth Search
  layout, and skips re-uploading assets already in the bucket. The existing
  `L2A_PVI.jpg` is referenced directly as the `thumbnail` asset with no
  re-generation needed.
- Item geometry measured from the rasters: the union of the valid-data
  footprints of the canonical image set, extracted with
  [`raster-footprint`](https://github.com/stac-utils/raster-footprint)
  footprint tool used, falling back to the product metadata footprint if no
  raster can be read. `datetime` comes from the granule metadata's
  `SENSING_TIME`.
- STAC 1.1.0-native `bands` on every asset (merged `eo:`/`raster:` fields),
  with the `eo`/`raster` extension schemas bumped to v2.0.0.
- Updated storage extension
- `v1_output` payload flag (default `false`): downgrades the emitted Item
  from STAC 1.1 to STAC 1.0 (schema/version rollback, `proj:code` →
  `proj:epsg`, storage schemes collapsed to item-level properties, `bands`
  split back into `eo:bands`/`raster:bands`). Applies after all build modes.
- Test suite: offline fixture-driven tests; opt-in `-m system`, `-m upgrade`
  and `-m downgrade` network-parity tests that download real imagery and
  exercise the various data paths against real Earth
  Search data (none write to S3; all cache downloads under
  `tests/external-data`); a hermetic `conftest.py`; local dockerized Lambda
  testing (`tests/run_tests.sh`, `.env.example`); `scripts/compare_fixture.py`
  for legacy↔destination parity diffing.
- CI workflow; Dockerfile/compose targeting Python 3.12 on an Amazon Linux
  2023 base via `uv`.

### Changed

- Input is read from a top-level `metadata_href` (was
  `assets['metadata']['href']` in the legacy task).
- Renamed the template package/class to `sentinel_2_l2a_to_stac` /
  `Sentinel2ToStac`.
- Adopted pystac 2.0 (git-SHA pinned via `[tool.uv.sources]`), raising
  `requires-python` to `>=3.12`.
- Replaced `stactools`/`stactools-sentinel2` with a self-contained metadata
  pipeline, split into `constants.py` (band definitions/lookup tables),
  `metadata.py` (XML parsing), `stac.py` (`create_item`), `cogify.py`
  (COG/thumbnail generation, `sha256sum_multihash`), and `utils.py` (shared
  XML helpers).
- All S3 access goes through plain `boto3` instead of `boto3utils`/
  `stac-asset`; `boto3`, `shapely`, `antimeridian`, `lxml`, and `pyproj` are
  direct runtime dependencies, `boto3-utils` is dropped.
- Processing baseline floor raised to `>= 05.00` (excluding `05.09`); the
  legacy task's `>= 04.00` with a Europe-tile carve-out is gone.
- Switched to dot-delimited CalVer.
- Dockerfile/compose: single `uv pip install --frozen` step,
  dropped the `lambgeo` GDAL build stage in favor of `manylinux_2_28`
  `rasterio`/`pyproj` wheels that bundle their own GDAL/PROJ.

### Removed

- All use of `tileInfo.json` and `productInfo.json`: geometry, `datetime`,
  and `s2:product_type` are derived from raster footprints and product/
  granule metadata instead.
- The COG-from-granule path for `metadata_href` inputs, and the
  `create_cogs` payload field — whether COGs are created is now solely
  determined by which input field is given.
- `eo:bands` on assets (superseded by the STAC 1.1.0 `bands` field),
  `s2:dark_features_percentage`, `eo:snow_cover`, and SCL classification
  classes (none are emitted anymore).

[unreleased]: https://github.com/earthsearch/sentinel-2-l2a-to-stac/compare/v2026.09.30...main
[v2026.09.30]: https://github.com/earthsearch/sentinel-2-l2a-to-stac/compare/3e01c13...v2026.09.30
