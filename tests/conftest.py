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

import os

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
