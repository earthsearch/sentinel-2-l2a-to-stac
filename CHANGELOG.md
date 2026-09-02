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

### Changed

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
