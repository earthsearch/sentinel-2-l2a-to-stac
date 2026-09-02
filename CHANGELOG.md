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
