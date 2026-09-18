# Sentinel-2 L2A to STAC

*A Cirrus task that builds STAC Items from Sentinel-2 L2A products. See the [DEVELOPMENT.md](DEVELOPMENT.md) file for instructions on developing a task.*

**A Cirrus task that reconstructs STAC 1.1.0 Items for Sentinel-2 L2A scenes from
the raw metadata Sinergise/AWS host on the public RODA bucket
(`s3://sentinel-s2-l2a`, no STAC catalog of its own): it downloads the source
metadata, builds an Item via `stactools-sentinel2`, applies Earth Search overrides,
optionally COGifies the JP2 imagery and generates a thumbnail, and returns the
Item(s).**

## Development

- Make a Python 3.12 enviornment

```bash
uv venv --python 3.12
uv sync
```

## Input

This task does not require complete STAC Items. Each `Feature` in the Cirrus Process Payload needs
only two **top-level** fields: an `id` and the `metadata_href` of the source
`metadata.xml`. (This differs from the legacy task, which read the href from
`assets['metadata']['href']`.)

| Field           | Description                                                          |
| --------------- | ------------------------------------------------------------------- |
| `id`            | A unique identifier for this scene (will not be the final scene ID) |
| `metadata_href` | The URL of the source granule `metadata.xml`                        |

See the [Usage](#usage) section below for the full field reference, including the
optional `create_cogs` toggle.

Example:

```json
{
  "id": "roda-sentinel-2-l2a/workflow-sentinel-2-l2a-to-stac/tiles-19-T-DJ-2026-8-23-0",
  "metadata_href": "s3://sentinel-s2-l2a/tiles/19/T/DJ/2026/8/23/0/metadata.xml",
  "process": [
    {
      "workflow": "sentinel-2-l2a-to-stac",
      "upload_options": {
        "path_template": "s3://sentinel-cogs-test/${collection}/${mgrs:utm_zone}/${mgrs:latitude_band}/${mgrs:grid_square}/${year}/${month}/${id}",
        "collections": {
          "sentinel-2-c1-l2a": "$[?(@.id =~ '.*')]"
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
| `metadata_href`  | string  | **REQUIRED.** Href to any file in the source granule prefix on RODA/S3 or the earthsearch bucket (e.g. `s3://sentinel-s2-l2a/tiles/.../tileInfo.json`). Its directory is used as the granule prefix; `tileInfo.json`, granule `metadata.xml`, and product `metadata.xml` are fetched relative to it. |
| `create_cogs`    | boolean | Optional. When `true` and no existing product doc is found in the output prefix, COGify the JP2 imagery and generate a JPEG thumbnail. When an existing product doc is present in the output prefix the reference path is taken regardless of this flag (see below). When `false` and no existing doc is present, the Item is emitted with its source asset hrefs unchanged. (Default: `false`.) |

The collection each Item is assigned to is resolved from
`payload['process']['upload_options']['collections']` (a map of collection id →
JSONPath expression, first match wins), per the standard Cirrus convention.

### Reference path (update-first)

When the output prefix already contains a `{item_id}.json`, the task
automatically operates in **reference/update mode**, regardless of `create_cogs`:

- Each expected asset is verified to be present in the bucket. An asset that
  is expected but absent raises `InvalidInput`.
- `type`,`file:size` and `file:checksum` are reused from the existing doc where
  available; assets whose info is missing are downloaded and recomputed.
- All asset hrefs are rewritten to the flat earthsearch prefix layout.
- The `thumbnail` asset reuses the existing `L2A_PVI.jpg` from the bucket — no
  re-generation needed.
- No assets already in the bucket are re-uploaded; only the STAC metadata files
  are uploaded.

This path is intended for re-ingesting an already-present earthsearch scene
(for example, upgrading a STAC 1.0 item to 1.1.0) without re-COGifying or
re-uploading imagery.

### Environment Variables

| Variable              | Default                                          | Description |
| --------------------- | ------------------------------------------------ | ----------- |
| `STAC_API_URL`        | `https://earth-search.aws.element84.com/v2`      | STAC API queried by the `is_newer_than_existing` gate. |
| `AWS_DEFAULT_REGION`  | (none — required)                                | AWS region for the S3 reads/writes (`us-west-2` for the public RODA bucket). Required for any run that touches S3. |
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

`tests/test_task.py` also contains full-pipeline parity tests that compare the
task's output against the legacy Sentinel-2 C1 L2A task. These **hit the
network**: they download genuine Sentinel-2 imagery from the public RODA/AWS
bucket (`s3://sentinel-s2-l2a`) so the COG/thumbnail pipeline runs end-to-end.
They are marked `@pytest.mark.system` and are **excluded by default**. Run them
explicitly with:

```
uv run pytest -m system
```

They never write to S3 (every call uses `upload=False`), and the STAC API
item-lookup stays stubbed to 404 so the run is deterministic. Downloaded imagery
is cached under `tests/external-data/<payload-id>`; delete that directory to
force a clean re-fetch. The expected `out.json` for each success fixture is
generated on the first run if absent.

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
`actual.json` file from the test's fixture directory, and rerun the tests.
This will recreate the fixture. `git diff` can be used to examine what has
changed.

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
