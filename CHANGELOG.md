# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This project adheres to [CalVer](https://calver.org/). See the
[README](README.md#versions-and-releases) for the specific CalVer format
used by this project.

## [Unreleased]

### Added

- Versioning section to README.md, and switched to using dot-delimited CalVer.
- `tests/conftest.py` sets a hermetic, fully-local AWS environment (static dummy
  credentials + region) so `pytest` never touches live AWS services or the
  developer's ambient identity. Needed because botocore >= 1.43 adds an IAM
  Identity Center credential provider (requiring `botocore[crt]`) that otherwise
  crashes at import time when an SSO profile is configured. Uses `setdefault`,
  so explicitly-exported real credentials are still respected.
- Source-metadata download plumbing ported from the legacy task: `read_href()`,
  the `tileinfo_path`/`granule_metadata_xml_path`/`product_metadata_xml_path`
  properties, and the download block in `process()` that fetches `tileInfo.json`,
  product-level `metadata.xml` (located via tileInfo's `productPath`), and
  granule `metadata.xml` into the workdir. `create_item` and later stages are not
  wired yet, so `process()` still returns a stub. Added `boto3-utils` dependency
  (used to recover the bucket for the product-metadata href).
- `tests/test_download.py`: local-only unit tests for the download plumbing (no
  network / live S3) — happy path, `.exists()` idempotency, `Corrupted
  tileInfo.json` handling, and the `NoSuchKey`→`InvalidInput` translation.
- `src/sentinel_2_l2a_to_stac/py.typed` marker so the strictly-typed package
  type-checks correctly when imported from the test suite.
- STAC Item creation wired into `process()` via
  `stactools.sentinel2.stac.create_item`, with the legacy exception translation
  (`ValueError`/`AssertionError` and the "older metadata format" message →
  `InvalidInput`; anything else → `Exception`). Later stages (`update_item`,
  COGs, thumbnail, upload) are not wired yet, so `process()` returns the raw
  `create_item` output. Added `stactools~=0.5.3` and `stactools-sentinel2==0.8.0`.
- `tests/fixtures/source-metadata/tiles-19-T-DJ-2023-4-19-0/` (the three real
  source metadata files) and `tests/test_create_item.py`, which pre-seeds a
  workdir with them so the download block is a no-op and `create_item` runs
  fully locally. Asserts narrow properties (id / geometry / processing baseline
  / `stac_version`) rather than a full output diff.
- `tests/fixtures/payloads/success/create-item-baseline/in.json` (payload only;
  no `out.json` yet — the full pipeline output isn't meaningful until PR 8/9).
- `update_item()` (Earth Search overrides) wired into `process()` after
  `create_item`: collection assignment via `stac_jsonpath_match` (fail-fast if
  none match), `earthsearch:payload_id`, item-datetime presence check, dropping
  `providers` and the `license` link, a `via` link back to the RODA granule
  metadata, the storage extension, the "scrub extra metadata" block
  (`raster:bands[0].classification:classes` on `scl`, `eo:snow_cover`, and the
  classification extension), asset href rewriting (imagery → RODA S3, metadata →
  local files), and `proj:bbox` stripping.
- The storage extension is written against pystac 1.15.2's **storage v2
  schemes/refs model**, not the removed v1 `CloudPlatform`/`.apply()` API. One
  `aws-s3` scheme (`storage:schemes`, keyed `"aws"`, `platform` = the templated
  endpoint `https://{bucket}.s3.{region}.amazonaws.com`, `region` `us-west-2`,
  `requester_pays` false), referenced from every asset via `storage:refs`.
  Output shape is `storage:schemes`/`storage:refs`, **not** flat
  `storage:platform`/`storage:region`/`storage:requester_pays`.
- `tests/test_update_item.py` and a session-scoped `baseline_item_dict` fixture
  in `conftest.py` (runs the full local `process()` once). `test_create_item.py`
  refactored to consume the shared fixture.
- Scoped `filterwarnings` entries in `pyproject.toml` so `uv run pytest` reports
  0 warnings without hiding warnings from our own code:
  - `ignore::PendingDeprecationWarning:rasterio.*` — rasterio's `from_bounds`
    (`transform.py`) multiplies two `affine.Affine` transforms with `*`; the
    `affine` package now nudges callers toward the `@` matmul operator via a
    `PendingDeprecationWarning`. `create_item` triggers it ~40× per run through
    stactools-sentinel2. It's third-party internal (none of our code multiplies
    `Affine` objects) and resolves when rasterio switches `*`→`@`.
  - `ignore:The exterior ring::antimeridian.*` — `antimeridian.FixWindingWarning`
    fires when stactools-sentinel2 hands it a clockwise exterior ring; it
    auto-corrects the winding, so the warning is advisory noise from a
    third-party call we don't control. Matched by message + module so unrelated
    warnings stay visible.
- `is_newer_than_existing()` wired into `process()` after `update_item`: it GETs
  the item from the live STAC API (`STAC_API_URL`, default
  `https://earth-search.aws.element84.com/v1`) and, comparing
  `s2:generation_time`, skips the ingest (`process()` returns `[]`) when an
  equal-or-newer item is already published — the guard against regressing an
  already-ingested item. 404 → proceed; non-200/404 → internal failure. Added
  `requests` and `returns~=0.29` dependencies.
- `tests/test_is_newer.py`: regression tests for the gate (404 / 500 / older /
  same / newer), ported from the legacy suite. Each builds an Item from the
  shared `baseline_item_dict` and calls `is_newer_than_existing` under a
  per-test `requests_mock` override.
- Session-scoped autouse `_stub_stac_api` fixture in `conftest.py` that stubs
  every `GET .../collections/*/items/*` to 404 by default, so no test can reach
  the live STAC API (`requests_mock` raises `NoMockAddress` on any un-stubbed
  request). Session-scoped and depended on by `baseline_item_dict` so the stub
  is active before that session-scoped fixture runs `process()` through the
  gate. Added `requests-mock` and `types-requests` dev dependencies.
- COG generation ported from the legacy task and wired into `process()` inside
  the `if create_cogs:` block (after the `is_newer_than_existing` gate):
  `make_cogs_for_item()` (processing-baseline floor `>= 04.00` with the
  Europe-MGRS `04.xx` carve-out — `EUROPE_MGRS_IDS`, ~640 entries, preserved
  verbatim as data), the module-level `cogify()`/`write_cog()`, and the
  `get_band_scales_offsets_nodatas_resolutions()`,
  `asset_name_to_resample_algorithm()`, `gsd_to_blocksize()`, `is_local_asset()`,
  and `get_local_asset_keys()` helpers. `AssetRasterExtension.bands` is read in
  its pre-1.1.0 `raster:bands` shape by design; PR 9 consolidates it. Added
  `rasterio~=1.4`, `numpy`, and `multiformats~=0.3.1` dependencies.
- `tests/test_make_cogs.py`: network-free tests for the make_cogs branching
  (baseline floor, Europe carve-out, `>= 05.xx` skip — driven off the shared
  `baseline_item_dict` with overridden `s2:processing_baseline`/`grid:code`, and
  `download_item`/`cogify` stubbed) plus a `cogify`/`write_cog` unit test over a
  synthetic GeoTIFF asserting the COG output (`.tif` href, COG media type,
  `file:checksum`/`file:size`, readable band count).
- Thumbnail + checksum steps ported verbatim from the legacy task and wired into
  `process()`: `make_thumbnail()` (re-encodes the `preview` asset to a JPEG
  registered under the `thumbnail` key; runs inside `if create_cogs:`),
  `add_fileinfo_to_local_assets()` (stamps `file:checksum`/`file:size` on
  workdir-local assets; runs unconditionally), and the module-level
  `sha256sum_multihash()` helper. `op.getsize` → `os.path.getsize` (the
  `os.path as op` alias was dropped in PR 6). Added `pillow~=12.0` dependency
  (`Image.open`/`Image.save`). Upload wiring remains deferred to PR 8.
- S3 upload wired into `process()` as the final step — `self.upload_item_assets_to_s3(
  item, self.get_local_asset_keys(item))`. This is a base-class method (no new task
  code, just the call site). It no-ops whenever `self._upload` is False, which is
  exactly what `--local` / `skip_upload=True` / `upload=False` produce (verified
  against stactask 0.7.0's `__init__` and `upload_item_assets_to_s3`), so a local
  run never writes to S3. `process()` now returns the fully-built, uploaded item.
- Full-pipeline parity tests in `tests/test_task.py` (rewritten from the template's
  strict `==` walker). Ported legacy's tolerant comparators `normalize()` /
  `diff_output()` (geometry symmetric-difference ratio, centroid threshold,
  thumbnail size/checksum wobble) over `deepdiff` + `shapely`, and the payload
  fixtures `success/payload-2022`, `success/antimeridian`,
  `failure/processing_baseline_02.13`, plus the standalone regression payloads
  `payload-tileinfo-no-tileDataGeometry.json` and `payload-antimeridian-pole.json`.
  These **hit the real network** (download genuine imagery from the public RODA/AWS
  bucket to run the COG/thumbnail pipeline end-to-end) and are therefore marked
  `@pytest.mark.system` and **opt-in**: run them with `uv run pytest -m system`.
  They never write to S3 (`upload=False`), and the STAC API item-lookup stays
  stubbed to 404 by the autouse `_stub_stac_api` fixture so the gate is
  deterministic. Downloaded imagery caches under `tests/external-data/`. Added
  `deepdiff` and `shapely` dev dependencies. `out.json` for the two success cases
  is generated on the first `-m system` run, then diffed against legacy's to
  confirm only expected drift (storage `schemes`/`refs` shape, dependency-version
  deltas). The offline `missing-metadata-href` validate failure moved to a
  dedicated non-`system` test so it still runs in the default fast suite.

- `scripts/compare_fixture.py`: a dev tool (not part of the test suite) for
  reviewing legacy↔destination parity — a structural, key-order-insensitive
  `deepdiff` comparison that strips expected-drift fields (`created`, hrefs,
  `file:checksum`/`file:size`, geometry, `proj:centroid`, extension order) so only
  semantic STAC differences surface. Used during the migration to confirm that all
  output deltas vs legacy trace to the dependency-baseline bump, not the port.

- STAC 1.1.0 band upgrade: `upgrade_item_to_stac_1_1()` runs as the final
  normalization step in `process()` (after COGs/thumbnail/fileinfo, before upload).
  Merges each asset's `eo:bands` + `raster:bands` arrays into the core 1.1.0 `bands`
  field (via pystac 2.0's `Band`), and bumps the `eo`/`raster` extensions to v2.0.0
  (both kept — `eo:cloud_cover` stays an item property and the merged bands still
  carry `eo:`/`raster:` fields). `stactools-sentinel2` doesn't emit 1.1.0 natively, so
  this task owns the upgrade; the consolidation self-disables (`if asset.bands:`) once
  upstream emits native bands. Restores `processing:software` (task name → CalVer
  `version`) via `add_software_version_to_item`, which stactask 0.7.0 no longer applies
  automatically.

### Changed

- With `add_fileinfo_to_local_assets` now wired unconditionally into
  `process()`, the pre-COG `baseline_item_dict` fixture (`create_cogs=False`)
  carries `file:checksum`/`file:size` on its three local metadata assets
  (imagery assets are `s3://`, so skipped). `_StubItem` in `test_download.py`
  gained an empty `assets` dict so that unconditional step is a no-op there.
- The shared `baseline_item_dict` fixture now forces `create_cogs=False` so it
  stays the *pre-COG* baseline the PR 3/4/5 tests assert against. With COGs wired
  into `process()`, the baseline tile (processing baseline `05.09`) would
  otherwise pass the gate and download real JP2s from live S3, both rewriting the
  asset hrefs those tests check and violating the no-network constraint. The full
  COG end-to-end path is exercised in PR 8; the make_cogs branching is covered
  directly in `tests/test_make_cogs.py`.
- Raised `requires-python` from `>=3.10` to `>=3.11` (aligns with the legacy
  task's `~=3.11` and the Python 3.11 Lambda runtime). Forced by `returns 0.29`,
  which requires Python 3.11+; the bump also prunes Python-<3.11 backport shims
  (`exceptiongroup`, `tomli`, `async-timeout`) from `uv.lock`.

- Upgraded dependencies to latest stable: pystac 1.12.2 → 1.15.2, stactask
  0.6.1 → 0.7.0, stac-asset 0.4.6 → 0.4.7, boto3/botocore 1.37.1 → 1.43.56, and
  dev tools (mypy 2.3.1, pytest 9.1.1, ruff 0.16.5, pre-commit 4.6.2). Note:
  stable pystac 1.15.x is now a meta-package over `pystac-core` +
  separately-versioned `pystac-ext-*` packages; the storage extension moved to
  the new `schemes`/`refs` API (`CloudPlatform` and `StorageExtension.apply()`
  are removed) — see MIGRATION_PLAN.md.

- Renamed the template package `cirrus_task_example` to `sentinel_2_l2a_to_stac`
  and the task class `CirrusTaskExample` to `Sentinel2ToStac`, updating the
  project/CLI name, Dockerfile paths, and tests to match. No behavior change.
- `validate()` now requires a top-level `metadata_href` in the payload, raising
  `InvalidInput` when absent (ported from the legacy task; rewritten for
  stactask 0.6.1's instance-method `validate(self)` signature).
- Retired the two inherited template `payload1` fixtures
  (`tests/fixtures/payloads/{success,failure}/payload1`); their placeholder
  `metadata_href` pointed at a real public AWS bucket, which the now-inline
  download in `process()` would fetch during the fixture walk. The download
  plumbing is covered by the network-free `tests/test_download.py` instead;
  `failure/missing-metadata-href` remains until PR 3 adds a real success fixture.

- Adopted **pystac 2.0** (unreleased; git-SHA pinned via `[tool.uv.sources]`) as the
  baseline, replacing stable 1.15.2 — it provides the native `Band`/`Asset.bands` API
  the STAC 1.1.0 band upgrade targets, so the eventual switch to upstream-native bands
  is zero-touch. Consequences: `requires-python` raised `>=3.11` → `>=3.12` (pystac 2.0
  requires 3.12); an HREF compat shim (`pystac.link.HREF = pystac.utils.HREF`) is needed
  before importing `stactools`; and several 2.0 API changes were ported —
  `item.collection_id =` → `set_collection()`, explicit asset-owner re-parenting
  (`create_item`/`download_item`/`add_asset` no longer set `owner`), and
  `Asset.media_type` → `Asset.type`. `stactools-sentinel2` 0.8.0 is not yet
  2.0-compatible: it strands asset media types in `extra_fields["media_type"]`, so a
  normalization shim (`_normalize_asset_media_types`) restores them to `asset.type` (the
  cogify jp2 detection and valid `type` serialization both depend on it). All shims are
  idempotent and self-disable once upstream is 2.0-ready. See MIGRATION_PLAN.md PR 9.

## [v2025.03.12]

### Changed

- Dockerfile now uses UV build scheme, along with lambgeo base. ([#2])
- CLI specified via pyproject.toml. ([#1])

## [v2025.03.11]

Initial release

[unreleased]: https://github.com/cirrus-geo/cirrus-task-example/compare/v2025.03.12..main
[v2025.03.12]: https://github.com/cirrus-geo/cirrus-task-example/compare/v2025.03.11..v2025.03.12
[v2025.03.11]: https://github.com/cirrus-geo/cirrus-task-example/tree/v2025.03.11
[#1]: https://github.com/cirrus-geo/cirrus-task-example/pull/1
[#2]: https://github.com/cirrus-geo/cirrus-task-example/pull/2
