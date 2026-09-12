# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This project adheres to [CalVer](https://calver.org/). See the
[README](README.md#versions-and-releases) for the specific CalVer format
used by this project.

## [Unreleased]

### Changed

- Replaced `stactools~=0.5.3` and `stactools-sentinel2==0.8.0` with a
  self-contained `metadata.py` (vendored + pruned granule/S3 path). The public
  interface is unchanged: `create_item(workdir)` returns a `pystac.Item`.
  `antimeridian`, `lxml`, and `pyproj` are now direct dependencies (they were
  previously transitive through stactools).
- `metadata.py` now emits STAC 1.1.0-native `bands` on every asset directly,
  using `pystac.Band.from_dict(...)` with merged `eo:`/`raster:` prefixed fields.
  The post-hoc `upgrade_item_to_stac_1_1` / `_consolidate_bands` fixups in
  `task.py` are removed.
- `Asset.type` is used throughout instead of the old `media_type=` kwarg; the
  `_normalize_asset_media_types` shim is removed.
- Asset owners are set inside `create_item` at construction time; the
  `_set_asset_owners` call is removed from `update_item` (still present in
  `make_cogs_for_item` and `add_fileinfo_to_local_assets` where stac-asset
  download can reset ownership).
- The `pystac.link.HREF` monkeypatch (only needed for stactools compatibility) is
  removed. The `Asset.media_type` alias for stac-asset compatibility is retained
  and marked `# SHIM(pystac-2.0)` for easy discovery when stac-asset is updated.
- EO and raster extension schema URIs are bumped to v2.0.0 inside `create_item`
  rather than as a late post-processing step.
- SCL classification classes are no longer emitted (they were always scrubbed by
  `update_item`); the scrub block is removed.
- `eo:snow_cover` is no longer set on the item (it was always deleted by
  `update_item`); the corresponding delete is removed.

## [v2026.09.03]

First release of the rewritten task: the legacy Sentinel-2 C1 L2A→STAC Cirrus
task ported forward onto STAC 1.1.0 output and a pystac 2.0 baseline. See
[Taskv2Migration.md](Taskv2Migration.md) for the detailed migration notes
(output changes, new features, and the temporary compatibility shims).

### Added

- Full Sentinel-2 L2A → STAC pipeline, ported from the legacy task:
  source-metadata download (`tileInfo.json` + granule/product `metadata.xml`),
  Item creation via `stactools-sentinel2`, Earth Search overrides (`update_item`),
  the `is_newer_than_existing` gate against the live STAC API, optional COG
  generation and JPEG thumbnail, `file:checksum`/`file:size` stamping, and S3
  upload.
- **STAC 1.1.0 band consolidation** (`upgrade_item_to_stac_1_1`): merges each
  asset's `eo:bands` + `raster:bands` into the core 1.1.0 `bands` field and bumps
  the `eo`/`raster` extensions to v2.0.0. Self-disables once `stactools-sentinel2`
  emits native `bands`.
- **Storage extension** on the `schemes`/`refs` model, with per-bucket schemes
  classified by each asset's final href (`roda` source metadata, `earthsearch`
  uploaded assets, `local` for `--local`/test runs).
- `create_cogs` payload toggle to skip COG/thumbnail generation and emit the Item
  with its source asset hrefs.
- Test suite: offline fixture-driven tests plus opt-in `@pytest.mark.system`
  network-parity tests that download real imagery and compare against the legacy
  task (`uv run pytest -m system`); a hermetic `conftest.py` (dummy AWS creds, a
  STAC-API 404 stub) that keeps the default suite fully local; and local
  dockerized Lambda testing (`tests/run_tests.sh`, `.env.example`).
- `scripts/compare_fixture.py`: dev tool for legacy↔destination parity diffing
  (strips expected-drift fields so only semantic STAC differences surface).

### Changed

- **Input** is now read from a top-level `metadata_href` (was
  `assets['metadata']['href']` in the legacy task).
- **Renamed** the template package/class to `sentinel_2_l2a_to_stac` /
  `Sentinel2ToStac`, with the CLI entry point, Dockerfile, and tests updated to
  match.
- **Adopted pystac 2.0** (unreleased; git-SHA pinned via `[tool.uv.sources]`),
  raising `requires-python` to `>=3.12`. Ported the 2.0 API deltas
  (`set_collection()`, explicit asset-owner re-parenting, `Asset.media_type` →
  `Asset.type`) and added idempotent, self-disabling compatibility shims for
  `stactools`/`stac-asset`/`stactools-sentinel2` (HREF import move, media-type
  normalization, `Asset.media_type` read alias) — see Taskv2Migration.md.
- **Upgraded the dependency baseline**: pystac → 1.15.2 then the 2.0 dev build,
  stactask 0.6.1 → 0.7.0, stac-asset 0.4.6 → 0.4.7, boto3/botocore 1.37.1 →
  1.43.56, plus dev tools (mypy, pytest, ruff, pre-commit).
- **Restored `processing:software`** on the output Item, which stactask 0.7.0 no
  longer applies automatically.
- Refreshed the success fixtures onto processing baseline `> 05.09` (same tiles;
  side effect: `s2:dark_features_percentage` is no longer emitted, since ESA
  dropped it from the source metadata after 05.09 — not a code regression).
- Switched to dot-delimited CalVer and added a Versioning section to the README.
- **Dockerfile/compose**: Python 3.12 base images, single `uv pip install
  --frozen` step (reads `uv.lock` directly, no intermediate `requirements.txt`),
  and `linux/amd64` platform pinning for Apple Silicon. Dropped the
  `ghcr.io/lambgeo/lambda-gdal` build stage and its `GDAL_DATA`/`PROJ_LIB`/
  `GDAL_CONFIG`/`GEOS_CONFIG` env plumbing: the pinned `rasterio` (1.5.x) and
  `pyproj` (3.7.x) wheels are `manylinux_2_28` and bundle their own GDAL/PROJ,
  which the Amazon Linux 2023 base (glibc 2.34) satisfies — removing a GDAL
  3.8-vs-wheel version conflict and the dependency on a Python 3.12 lambgeo tag
  that blocked the build (build deps trimmed to just `git` for the pystac git
  dependency). Fixed the compose handler string to
  `sentinel_2_l2a_to_stac.task.lambda_handler` (was `task.handler`, a
  non-existent module/function) so it matches the Dockerfile `CMD`.

## [v2025.03.12]

### Changed

- Dockerfile now uses UV build scheme, along with lambgeo base. ([#2])
- CLI specified via pyproject.toml. ([#1])

## [v2025.03.11]

Initial release

[unreleased]: https://github.com/cirrus-geo/cirrus-task-example/compare/v2026.09.03..main
[v2026.09.03]: https://github.com/cirrus-geo/cirrus-task-example/compare/v2025.03.12..v2026.09.03
[v2025.03.12]: https://github.com/cirrus-geo/cirrus-task-example/compare/v2025.03.11..v2025.03.12
[v2025.03.11]: https://github.com/cirrus-geo/cirrus-task-example/tree/v2025.03.11
[#1]: https://github.com/cirrus-geo/cirrus-task-example/pull/1
[#2]: https://github.com/cirrus-geo/cirrus-task-example/pull/2
