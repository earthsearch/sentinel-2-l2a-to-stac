# Cirrus Task Development

This repo builds a containerized Cirrus task, using the [stac-task](https://github.com/stac-utils/stac-task) library.
The stac-task Task class has several convenient utility methods for performing common tasks
(e.g., setting collection, uploading STAC Item assets to s3).

See the [stac-task](https://github.com/stac-utils/stac-task) README for more information on developing a task.

## Local Development

```bash
uv sync --locked                              # install
uv run pre-commit run --all-files             # ruff, mypy --strict, hygiene hooks (same as CI)
uv run pytest                                 # fast offline suite (same as CI)
uv run sentinel-2-l2a-to-stac payload.json --local   # local run, no upload
```

See the [Testing](README.md#testing) section of the README for fixtures and the
opt-in network suites.

## Deployment

The built Docker image from this code should be published to a registry that is accessible
from the Cirrus deployment. Ideally the image is in the same region as where the task will be run.

## Migration Notes (compared to v1)

### Output changes

| Change | Detail |
| ------ | ------ |
| `stac_version` | `1.0.0` → **`1.1.0`**. |
| Band metadata | Per-asset `eo:bands` + `raster:bands` arrays are **merged into the core 1.1.0 `bands` field**. Core fields (`data_type`, `nodata`, `unit`, `statistics`) land unprefixed; the rest stay `eo:`/`raster:`-prefixed. |
| Extension versions | `eo`/`raster` extensions bumped to **v2.0.0**; `projection` is **v2.0.0**, so `proj:epsg` → **`proj:code`**. |
| Storage extension | Flat `storage:platform`/`storage:region`/`storage:requester_pays` → **`storage:schemes`/`storage:refs`**. Each asset is grouped by its final href into a named scheme: `earthsearch` or `local` (`--local`/test runs). |
| Input field | Payload reads a **top-level `metadata_href`** (or `safe_href`) instead of `assets['metadata']['href']`. |
| `s2:dark_features_percentage` | **No longer emitted**: ESA dropped `DARK_FEATURES_PERCENTAGE` from the source metadata after processing baseline 05.09. Source-data change, not a code regression. |
| `processing:software` | Still emitted, but **restored explicitly**: stactask 0.7.0 no longer applies it automatically. |

`v1_output: true` items still differ from legacy v1 items in a few deliberate
ways: `s2:dark_features_percentage` is absent (see above), and workflow/task
identity fields (`processing:software` key, `earthsearch:payload_id`, hrefs)
follow the task rename rather than the STAC format.

The processing-baseline floor also changed: legacy allowed `>= 04.00` with a
Europe-MGRS carve-out for `04.xx` scenes; the rewrite requires **`>= 05.00`,
excluding `05.09`**, with no carve-out.

### Temporary shims: remove once upstream catches up

These exist only because upstream libraries haven't caught up to pystac 2.0 /
STAC 1.1.0. Each carries an inline `SHIM` comment or docstring at its definition.

| Shim | Why it exists | Remove when |
| ---- | ------------- | ----------- |
| `_patched_get_template_value` (`task.py`) | pystac 2.0's `LayoutTemplate._get_template_value` raises `TypeError` when a template variable matches an attribute of the Item itself (e.g. `id`). | Fixed upstream in pystac. |
| `_set_asset_owners` (`task.py`, called in `add_fileinfo_to_local_assets`) | `Extension.ext(asset, add_if_missing=True)` needs `asset.owner` to register the extension URI on the Item, and pystac 2.0 doesn't always set it. | pystac re-parents assets on these paths. |
| Raster band fields read via `band.extra_fields` (`cogify.py`) | pystac 2.0-dev `Band` has no typed raster accessors yet. | pystac stabilises the `Band` field API. |
| `filterwarnings`: `ignore::PendingDeprecationWarning:rasterio.*` (`pyproject.toml`) | `rasterio.transform.from_bounds` multiplies `Affine` transforms with `*`; `affine` nudges callers toward `@`. None of our code multiplies `Affine`. | rasterio switches `*` → `@`. |
| `filterwarnings`: `ignore:The exterior ring::antimeridian.*` (`pyproject.toml`) | `antimeridian.FixWindingWarning` fires when it's handed a clockwise exterior ring; it auto-corrects the winding, so the warning is noise. | The warning stops being emitted. |
