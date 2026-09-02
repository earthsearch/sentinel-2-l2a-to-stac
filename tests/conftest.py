"""Shared pytest configuration.

Establishes a hermetic, fully-local AWS environment for the test suite so that
running ``pytest`` never touches live AWS services and never implicitly uses
whatever AWS identity the developer happens to have loaded in their shell.

Why this exists
---------------
``stactask`` constructs an S3 client at *import time* (``boto3utils.s3()`` in
``stactask.asset_io``), so merely importing the task module during test
collection triggers botocore credential resolution. botocore >= 1.43 added an
IAM Identity Center ("login") credential provider to its default resolution
chain that requires the optional ``botocore[crt]`` dependency; if a developer
has an SSO session configured (e.g. an ``AWS_PROFILE`` pointing at
``~/.aws/config``), that resolution raises ``MissingDependencyException`` at
import time and the whole suite fails to collect.

The fix is to provide *static dummy credentials* via environment variables.
botocore resolves the environment-variable provider first — ahead of the
shared-config/SSO providers — so the chain short-circuits before it ever
reaches the login provider. No CRT, no crash, no real credentials, no network.
This is good test hygiene regardless of the botocore version: tests should not
depend on the developer's ambient AWS identity.

Locality contract
-----------------
* ``pytest`` stays local. This is guaranteed primarily by test *design* (no
  live calls: ``skip_upload``/``upload=False``, local ``file://`` fixtures, and
  from PR 5 onward a ``requests_mock`` stub for the STAC API). The dummy
  credentials here are a safety net, not the mechanism.
* Running the task with ``--local`` stays local (a stactask runtime flag,
  orthogonal to and unaffected by this file).
* Running the task "properly" (the Lambda handler or the
  ``uv run sentinel-2-l2a-to-stac`` CLI) resolves credentials normally and can
  hit live services when configured — this file is only loaded by pytest and
  has no effect there.

We use ``setdefault`` so a developer who *deliberately* exports real
credentials (a future opt-in integration test that hits live services) is
respected rather than overridden; a plain ``pytest`` run remains hermetic.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
import requests_mock as requests_mock_module

# Set before any test module is collected/imported, because the import-time S3
# client construction described above happens during collection. Module-level
# code in a conftest runs before the test modules in its directory are imported.
_HERMETIC_AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-west-2",
    # Belt-and-suspenders: never let a stray call fall back to the EC2/ECS
    # instance metadata endpoint during tests.
    "AWS_EC2_METADATA_DISABLED": "true",
}

for _key, _value in _HERMETIC_AWS_ENV.items():
    os.environ.setdefault(_key, _value)


_FIXTURES = Path(__file__).parent / "fixtures"
_BASELINE_TILE = "tiles-19-T-DJ-2023-4-19-0"
_SOURCE_FILES = ("tileInfo.json", "metadata.xml", "product_metadata.xml")


@pytest.fixture(scope="session", autouse=True)
def _stub_stac_api() -> Any:
    """Session-wide STAC API stub — prevents any test from hitting the live API.

    Returns 404 for all GET .../collections/.../items/... requests, which the
    is_newer_than_existing gate treats as "item not yet ingested, proceed".
    requests_mock raises NoMockAddress for any un-stubbed request, so a test
    that forgets to stub its own URL will fail loudly rather than quietly
    hitting the network.
    """
    with requests_mock_module.Mocker() as m:
        m.get(re.compile(r"/collections/.+/items/.+"), status_code=404)
        yield m


@pytest.fixture(scope="session")
def baseline_item_dict(_stub_stac_api: Any) -> dict[str, Any]:
    """Run the full local process() over the baseline fixture, once.

    Seeds a workdir with the checked-in source metadata so the PR 2 download
    block is a no-op (fully local, no S3), then returns the single item dict
    produced by create_item + update_item. Session-scoped because create_item
    parsing the ~600 KB granule metadata is the slow part; tests treat the
    result as read-only.

    ``create_cogs`` is forced off. This fixture is the *pre-COG* baseline that
    PR 3/4/5 tests assert against (asset hrefs still pointing at RODA, no
    file:checksum, etc.). With PR 6, process() runs make_cogs_for_item inside
    the ``if create_cogs:`` block; the baseline tile's processing_baseline
    (05.09) passes the gate, so leaving COGs on would (a) rewrite those hrefs to
    local ``.tif`` paths and (b) download the real JP2s from live S3 — violating
    the no-network constraint. The make_cogs branching is exercised directly in
    test_make_cogs.py; the full COG e2e path lands in PR 8.
    """
    from sentinel_2_l2a_to_stac.task import Sentinel2ToStac

    source = _FIXTURES / "source-metadata" / _BASELINE_TILE
    workdir = Path(tempfile.mkdtemp())
    for name in _SOURCE_FILES:
        shutil.copy(source / name, workdir / name)

    payload = json.loads(
        (_FIXTURES / "payloads" / "success" / "create-item-baseline" / "in.json")
        .read_text()
    )
    payload["create_cogs"] = False
    result = Sentinel2ToStac(payload, workdir=workdir, upload=False).process()
    assert len(result) == 1
    return result[0]
