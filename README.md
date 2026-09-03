# Sentinel-2 L2A to STAC

*A Cirrus task that builds STAC Items from Sentinel-2 L2A products. See the [DEVELOPMENT.md](DEVELOPMENT.md) file for instructions on developing a task.*

**A Cirrus task that reconstructs STAC 1.1.0 Items for Sentinel-2 L2A scenes from
the raw metadata Sinergise/AWS host on the public RODA bucket
(`s3://sentinel-s2-l2a`, no STAC catalog of its own): it downloads the source
metadata, builds an Item via `stactools-sentinel2`, applies Earth Search overrides,
optionally COGifies the JP2 imagery and generates a thumbnail, and returns the
Item(s).**

## Usage

To use this task in a Cirrus workflow reference the Docker location in the task configuration
file in the Cirrus deployment repository. See [CHANGELOG.md](CHANGELOG.md) for version information.

```
Docker URL
```

This task reads its inputs from the **top level of the Cirrus process payload**,
not from `payload['process']['tasks']['sentinel-2-l2a-to-stac']` (it reads no
task-scoped config keys — that table is empty, as in the legacy task):

| Field          | Type    | Description |
| -------------- | ------- | ----------- |
| `metadata_href`  | string  | **REQUIRED.** Href to the source granule `metadata.xml` on RODA/S3 (e.g. `s3://sentinel-s2-l2a/tiles/.../metadata.xml`). Drives the whole task; the sibling `tileInfo.json` and product-level `metadata.xml` are located relative to it. |
| `create_cogs`    | boolean | Optional. When `true`, COGify the JP2 imagery assets (enforcing the processing-baseline floor) and generate a JPEG thumbnail; when `false`, skip both and emit the Item with its source asset hrefs. (Default: `true`.) |

The collection each Item is assigned to is resolved from
`payload['process']['upload_options']['collections']` (a map of collection id →
JSONPath expression, first match wins), per the standard Cirrus convention.

## Development

Tasks can be run locally with the built-in CLI.

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
