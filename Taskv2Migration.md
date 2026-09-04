# Task v2 Migration Notes

Reference for the rewrite of the Sentinel-2 L2A→STAC Cirrus task: the legacy
`sentinel-2-c1-l2a-to-stac` logic ported forward onto **STAC 1.1.0** output and a
**pystac 2.0** baseline. This captures what a consumer or maintainer needs to know
beyond the CHANGELOG: how the output shape changed, what's new, and — most
importantly — the temporary shims kept alive only until upstream libraries catch
up.
## 1. Output changes (vs. the legacy task)

| Change | Detail |
| ------ | ------ |
| `stac_version` | `1.0.0` → **`1.1.0`** (emitted by the current stack; asserted, not forced). |
| Band metadata | Per-asset `eo:bands` + `raster:bands` arrays are **merged into the core 1.1.0 `bands` field** (aligned by index). Core fields (`data_type`, `nodata`, `unit`, `statistics`) land unprefixed; the rest stay `eo:`/`raster:`-prefixed inside `bands`. |
| Extension versions | `eo`/`raster` extensions bumped to **v2.0.0**; `projection` is **v2.0.0**, so `proj:epsg` → **`proj:code`** (accepted, intentional consumer-visible rename). |
| Storage extension | Flat `storage:platform`/`storage:region`/`storage:requester_pays` → **`storage:schemes`/`storage:refs`**. Each asset is grouped by its final href into a named scheme: `roda` (source metadata left on the public bucket), `earthsearch` (uploaded assets), or `local` (`--local`/test runs). |
| Input field | Payload now reads a **top-level `metadata_href`** instead of `assets['metadata']['href']`. |
| `s2:dark_features_percentage` | **No longer emitted** — ESA dropped `DARK_FEATURES_PERCENTAGE` from the source metadata after processing baseline 05.09. Source-data change, not a code regression. |
| `processing:software` | Still emitted (`{task-name: CalVer version}`), but now **restored explicitly** — stactask 0.7.0 no longer applies it automatically (see §4). |

Unchanged from legacy (called out because they're easy to assume changed):
`providers` dropped, `license` link removed, the `via` link to the RODA granule
metadata, and the "scrub extra metadata" block (`eo:snow_cover`, the
classification extension, and `scl`'s `classification:classes`).

## 2. New features / behavior changes

- **STAC 1.1.0 upgrade step** (`upgrade_item_to_stac_1_1`): this task now owns the
  1.1.0 band consolidation, since `stactools-sentinel2` does not emit 1.1.0
  natively. Runs last, as a pure serialization-shape transform on the finished
  Item. Written to **self-disable** the day upstream emits native `bands` (see §3).
- **Per-bucket storage schemes**: more granular than the legacy single scheme —
  source vs. uploaded vs. local assets each get their own scheme so the `bucket`
  template variable is always defined.
- **Processing-baseline floor changed**: legacy allowed `>= 04.00` with a
  ~640-entry Europe-MGRS carve-out for `04.xx` scenes. The rewrite requires
  **`>= 05.00`, excluding `05.09`**, and the Europe carve-out (`EUROPE_MGRS_IDS`)
  is **dropped entirely**. This is a deliberate behavior change — confirm it
  matches ingest expectations before deploying.
- **Test tooling**: opt-in `@pytest.mark.system` network-parity tests that
  download real imagery and diff against the legacy task, plus
  `scripts/compare_fixture.py` for structural, drift-tolerant parity review. The
  default suite stays fully offline (dummy AWS creds + a STAC-API 404 stub in
  `conftest.py`).

## 3. Temporary external-library shims — **remove once upstream catches up**

These exist only because downstream libraries have not yet caught up to pystac
2.0 / STAC 1.1.0. All are **idempotent** and, where possible, **self-disabling**
(guarded so they no-op automatically once upstream is fixed). Each carries an
inline comment at its definition; this is the consolidated checklist.

| # | Shim (location in `task.py`) | Why it exists | Remove when |
| - | ---------------------------- | ------------- | ----------- |
| 1 | **HREF import shim** — `pystac.link.HREF = pystac.utils.HREF` (module top, before `import stactools`) | `stactools.core.io` does `from pystac.link import HREF`; pystac 2.0 moved `HREF` to `pystac.utils`. Without the shim `stactools`/`create_item` fails to import. | `stactools` stops importing `HREF` from `pystac.link`. |
| 2 | **`Asset.media_type` read alias** — `Asset.media_type = property(...)`, guarded on `hasattr` | `stac-asset` 0.4.7 still reads `asset.media_type` when downloading; pystac 2.0 renamed it to `Asset.type`. | `stac-asset` migrates to `Asset.type` (the `hasattr` guard then self-disables it). |
| 3 | **`_normalize_asset_media_types`** (called in `update_item`) | `stactools-sentinel2` 0.8.0 builds assets with pystac 1.x's `media_type=` kwarg, which pystac 2.0 strands in `extra_fields["media_type"]`, leaving `asset.type=None`. Silent + severe: it made the `image/jp2` match find nothing (zero assets COGified) and serialized an invalid `media_type` field. Moves the stray value to `asset.type`. | `stactools-sentinel2` is pystac-2.0-compatible. |
| 4 | **`_set_asset_owners`** (called in `update_item`, `make_cogs_for_item`, `add_fileinfo_to_local_assets`) | In pystac 2.0, `create_item` / `stac_asset.download_item` / `Item.add_asset` all leave `asset.owner=None`, but `Extension.ext(asset, add_if_missing=True)` needs the owner to register the extension URI on the Item. | Upstream re-parents assets on these paths (or pystac restores implicit owner-setting). |
| 5 | **`filterwarnings` — rasterio** — `ignore::PendingDeprecationWarning:rasterio.*` (`pyproject.toml`) | `rasterio.from_bounds` multiplies `affine.Affine` transforms with `*`; `affine` nudges callers toward `@`. Fires ~40×/run via `stactools-sentinel2`; none of our code multiplies `Affine`. | rasterio switches `*` → `@`. |
| 6 | **`filterwarnings` — antimeridian** — `ignore:The exterior ring::antimeridian.*` (`pyproject.toml`) | `antimeridian.FixWindingWarning` fires when `stactools-sentinel2` hands it a clockwise exterior ring; it auto-corrects the winding, so the warning is advisory noise from a call we don't control. | `stactools-sentinel2` passes correctly-wound rings (or the warning stops being emitted). |

> **Forward-compat seam (not a shim, but related):** `_consolidate_bands` returns
> early if `asset.bands` is already populated. When a future `stactools-sentinel2`
> emits native 1.1.0 `bands`, the whole band-merge in §2 dead-codes itself with no
> code change (contingent on upstream using pystac's `Band` representation).

## 4. Other dependency-driven changes (deprecated / moved APIs)

Applied during the port; permanent (not temporary shims), listed so the deltas
from legacy are traceable.

**pystac 2.0**
- `item.collection_id = x` → **`item.set_collection(x)`** (`collection_id` is now read-only).
- `Asset.media_type` → **`Asset.type`** (read in `make_cogs_for_item`, written in `cogify`, constructor kwarg in `make_thumbnail`).
- Native **`Band` / `Asset.bands`** API is the target container for the 1.1.0 band consolidation.
- `item.stac_extensions` is typed `list[str] | None`; iteration guards with `(item.stac_extensions or [])`.
- `requires-python` raised `>= 3.10` → **`>= 3.12`** (pystac 2.0 requires 3.12).

**pystac storage extension (1.15.2+)**
- `CloudPlatform` and `StorageExtension.apply(platform=, region=, requester_pays=)` **removed** → `StorageScheme.create(...)` + `add_scheme` / `add_ref` (the `schemes`/`refs` model in §1).

**stactask 0.7.0**
- `validate()` is now an **instance method** reading `self._payload` (legacy was `@classmethod validate(cls, payload)`).
- `add_software_version_to_item` is **no longer auto-called** — restored explicitly at the end of `process()` (§1).
- `Task.__init__` now installs a payload-id `TaskLoggerAdapter`, so the legacy hand-rolled adapter was **not ported** (inherited instead). A small module-level logging-config block was kept to also quiet `stactools`/`botocore`/`rasterio` and honor `CIRRUS_LOG_LEVEL` in the Lambda path.

**Baseline / tooling**
- Dependency baseline upgraded (pystac → 2.0 dev build, stactask 0.6.1 → 0.7.0, stac-asset 0.4.6 → 0.4.7, boto3/botocore 1.37.1 → 1.43.56, plus dev tools). pystac 2.0 is **git-SHA pinned** via `[tool.uv.sources]` (never a floating `@main`).
- `tests/conftest.py` supplies static dummy AWS creds — botocore ≥ 1.43 adds an IAM Identity Center provider that otherwise crashes at import when an SSO profile is configured.
