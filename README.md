# Sentinel-2 L2A to STAC

*A Cirrus task that builds STAC Items from Sentinel-2 L2A products. See the [DEVELOPMENT.md](DEVELOPMENT.md) file for instructions on developing a task.*

**A Cirrus task that reconstructs STAC 1.1.0 Items for Sentinel-2 L2A scenes. A
scene is either COGified from a SAFE archive (`safe_href`) or built from COGs
that already exist at an Earth Search granule prefix (`metadata_href`).
It downloads the source metadata, builds an Item via `stactools-sentinel2`,
applies Earth Search overrides, and (for a SAFE archive) COGifies the JP2 imagery
and generates a thumbnail, then returns the Item(s).**

## Development

- Make a Python 3.12 enviornment

```bash
uv venv --python 3.12
uv sync
```

## Input

This task does not require complete STAC Items. Each `Feature` in the Cirrus Process Payload needs
only two **top-level** fields: an `id` and either a `metadata_href` or a `safe_href`
(see [Usage](#usage) below for the difference). (This differs from the legacy task, which read the href from `assets['metadata']['href']`.)

| Field           | Description                                                          |
| --------------- | ------------------------------------------------------------------- |
| `id`            | A unique identifier for this scene (will not be the final scene ID) |
| `metadata_href` | The URL of an already-cogified Earth Search granule's `metadata.xml` |
| `safe_href`     | The URL/path of a Sentinel-2 L2A `.SAFE` archive to COGify           |

See the [Usage](#usage) section below for the full field reference.

Example:

```json
{
  "id": "earthsearch-sentinel-2-l2a/workflow-sentinel-2-l2a-to-stac/tiles-19-T-DJ-2026-8-23-0",
  "metadata_href": "s3://sentinel-cogs-test/sentinel-2-l2a/19/T/DJ/2026/8/S2A_T19TDJ_20260823T153829_L2A/metadata.xml",
  "process": [
    {
      "workflow": "sentinel-2-l2a-to-stac",
      "upload_options": {
        "path_template": "s3://sentinel-cogs-test/${collection}/${mgrs:utm_zone}/${mgrs:latitude_band}/${mgrs:grid_square}/${year}/${month}/${id}",
        "collections": {
          "sentinel-2-l2a": "$[?(@.id =~ '.*')]"
        }
      },
      "tasks": {
        "sentinel-2-l2a-to-stac": {}
      }
    }
  ]
}

```

## Output

This task returns a single STAC **1.1.0** Item for the L2A scene.

## Usage

To use this task in a Cirrus workflow reference the Docker location in the task configuration
file in the Cirrus deployment repository. See [CHANGELOG.md](CHANGELOG.md) for version information.

This task reads its inputs from the **top level of the Cirrus process payload**,
not from `payload['process']['tasks']['sentinel-2-l2a-to-stac']` (it reads no
task-scoped config keys — that table is empty, as in the legacy task):

| Field          | Type    | Description |
| -------------- | ------- | ----------- |
| `metadata_href`  | string  | Href to any file in an already-cogified Earth Search granule prefix (e.g. `s3://.../metadata.xml`). Its directory is used as the granule prefix; granule `metadata.xml`, product `metadata.xml`, and all 19 canonical COGs are required directly under it — any of them missing raises `InvalidInput`. Mutually exclusive with `safe_href`; there is no fallback to any other source. |
| `safe_href`      | string  | Href/path of a Sentinel-2 L2A `.SAFE` archive. Always COGifies the JP2 imagery and generates a JPEG thumbnail; a missing required file inside the archive raises `InvalidInput`. Mutually exclusive with `metadata_href`. |
| `v1_output`      | boolean | Optional. When `true`, the emitted Item is downgraded from STAC 1.1 to STAC 1.0 format: `stac_version` is set to `1.0.0`, extension schema URLs are rolled back to their v1 versions, `proj:code` becomes `proj:epsg`, storage schemes are collapsed to item-level `storage:platform`/`region`/`requester_pays`, and per-asset `bands` are split back into `eo:bands` and `raster:bands`. (Default: `false`.) |

The collection each Item is assigned to is resolved from
`payload['process']['upload_options']['collections']` (a map of collection id →
JSONPath expression, first match wins), per the standard Cirrus convention.

### Existing-COGs path (`metadata_href`)

A `metadata_href` granule prefix is always treated as already-cogified:

- Every canonical COG, `metadata.xml`, and `product_metadata.xml` is required
  directly under the granule prefix; anything missing raises `InvalidInput`.
- Geometry is measured as the union of the valid-data footprint of each COG.
- `type`, `file:size` and `file:checksum` are reused from an existing
  `{item_id}.json` STAC doc where available; assets whose info is missing
  (including when there is no existing doc at all) are downloaded and
  recomputed.
- All asset hrefs are rewritten to the flat Earth Search prefix layout.
- The `thumbnail` asset reuses the existing `L2A_PVI.jpg` from the bucket — no
  re-generation needed.
- No assets already in the bucket are re-uploaded; only the STAC metadata
  document is uploaded.

This path is intended for building/refreshing the STAC Item for an
already-cogified Earth Search scene (for example, upgrading a STAC 1.0 item to
1.1.0) without re-COGifying or re-uploading imagery.

### COG-creation path (`safe_href`)

A `safe_href` archive has no pre-existing COGs, so this is the only path that
creates them: the canonical JP2 image set is COGified, geometry is measured
from the resulting COGs, a JPEG thumbnail is generated from the preview image,
and every asset is uploaded to the output prefix.

### Environment Variables

| Variable              | Default                                          | Description |
| --------------------- | ------------------------------------------------ | ----------- |
| `STAC_API_URL`        | `https://earth-search.aws.element84.com/v2`      | STAC API queried by the `is_newer_than_existing` gate. |
| `AWS_DEFAULT_REGION`  | (none — required)                                | AWS region for the S3 reads/writes. Required for any run that touches S3. |
| `CIRRUS_LOG_LEVEL`    | `WARN`                                            | Root log level. `stactools`/`botocore`/`rasterio` loggers are quieted regardless. |
| `BIGTIFF`             | `IF_SAFER`                                         | Passed through to the GDAL COG driver during COG creation. |
| `GDAL_TIFF_INTERNAL_MASK` | `True`                                        | Passed through to the GDAL COG driver during COG creation. |

## Testing

This repository uses [uv](https://docs.astral.sh/uv/getting-started/installation/) and uses [pytest](https://docs.pytest.org/en/stable/) for testing.

The `tests/test_task.py` file contains test code to iterate through the input payloads in `fixtures`, which contains a series of input and payload files, each pair in it's own folder. For expected errors in tests an `exception.txt` file is provided intead of an output payload.

To run the fast, offline test suite:

```
uv run pytest
```

### Network parity tests (`-m system`)

`tests/test_task.py` also contains full-pipeline parity tests, marked
`@pytest.mark.system`, that would compare the task's output against the legacy
Sentinel-2 C1 L2A task by hitting the real network. There are currently no
fixtures under this marker (the prior ones tested the now-unsupported behavior
of COGifying directly from a granule prefix that lacked existing COGs); add
fixtures under `tests/fixtures/payloads/failure/` (each its own directory with
an `in.json` and an `exception.txt`) to restore coverage. Run the marker
explicitly with:

```
uv run pytest -m system
```

### Update-first tests (`-m upgrade`)

`tests/test_task.py` also contains parity tests for the reference/update path.
These **hit the network**: they download scene metadata from the earthsearch
bucket and exercise the full reference path end-to-end. They are marked
`@pytest.mark.upgrade` and are **excluded by default**. Run them explicitly with:

```
uv run pytest -m upgrade
```

Like the `-m system` tests, they never write to S3 and cache downloaded files
under `tests/external-data`.

Tasks can also be run locally with the built-in CLI.

```
$ uv run sentinel-2-l2a-to-stac

usage: task.py run [-h] [--logging LOGGING] [--output OUTPUT] [--workdir WORKDIR] [--save-workdir] [--skip-upload] [--skip-validation] [--upload] [--no-upload] [--validate]
                   [--no-validate] [--local]
                   [input]

positional arguments:
  input              Full path of item collection to process (s3 or local) (default: None)

options:
  -h, --help         show this help message and exit
  --logging LOGGING  DEBUG, INFO, WARN, ERROR, CRITICAL (default: INFO)
  --output OUTPUT    Write output payload to this URL (default: None)
  --workdir WORKDIR  Use this as work directory. Will be created. (default: None)
  --save-workdir     Save workdir after completion (default: False)
  --skip-upload      DEPRECATED: Skip uploading of generated assets and STAC Items (default: False)
  --skip-validation  DEPRECATED: Skip validation of input payload (default: False)
  --upload           Upload generated assets and resulting STAC Items (default: True)
  --no-upload        Don't upload generated assets and resulting STAC Items (default: True)
  --validate         Validate input payload (default: True)
  --no-validate      Don't validate input payload (default: True)
  --local            Run local mode (save-workdir = True, upload = False, workdir = 'local-output', output = 'local-output/output-payload.json') (default: False)
```

When runing locally use the `--local` option which will store all output in a local folder called `local-output` and will
not try to upload the data files to s3.

```
$ task.py payload.json --local
```

### Updating test fixtures

To update the expected output for any given fixture, simply remove the
`out.json` file from the test's fixture directory, and rerun the tests.
This will recreate the fixture. `git diff` can be used to examine what has
changed.

If a test fails due to a mismatch, an `actual.json` file is written alongside
`out.json` so you can `diff` the two to see what changed; it is not itself
read by the tests and can be deleted once you're done comparing.

## Local Dockerized Lambda Testing

1. Copy `.env.example` to `.env` and fill in your AWS credentials.
2. `docker-compose up -d` will build and launch the local Lambda server on port 8080.
3. `bash tests/run_tests.sh` will POST all fixture payloads to the server and report pass/fail.
4. `docker-compose down` will spin down the local Lambda server.

## Building Locally on Apple Silicon (arm64)

`docker-compose` sets `platform: linux/amd64` automatically, so `docker-compose up` works on
Apple Silicon without any extra flags.

If you need to build the image directly with `docker build` (outside of compose), specify the
platform explicitly:

```bash
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build .
```

# Versions and Releases

![CalVer:YYYY.0M.0D\_MICRO](https://img.shields.io/badge/CalVer-YYYY.0M.0D__MICRO-00aa00.svg)

This project uses CalVer for versioning releases.  The format is specified as
`YYYY.0M.0D_MICRO`, where the tokens are:

| token | description                     | example(s)             |
|-------|---------------------------------|------------------------|
| YYYY  | the full year                   | 2006, 2016, 2106)      |
| 0M    | the zero-padded month           | 01, 02 ... 11, 12      |
| 0D    | the zero-padded day of month    | 01, 02 ... 30, 31      |
| MICRO | (optional) free form, as needed | alpha, rc0, post0, ... |
