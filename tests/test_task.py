"""Full-pipeline parity tests against the legacy Sentinel-2 C1 L2A task.

These walk the payload fixtures under ``tests/fixtures/payloads/{success,failure}``
and run the *entire* ``process()`` pipeline — download, ``create_item``,
``update_item``, the ``is_newer_than_existing`` gate, COG generation, thumbnail,
and checksums — comparing the output against a checked-in ``out.json`` with the
same tolerant comparators the legacy suite used (geometry symmetric-difference
ratio, centroid threshold, thumbnail size/checksum wobble).

Network policy
--------------
Unlike the rest of the suite, these tests **hit the real network**: they
download genuine files from the public RODA/Earthsearch bucket
so the COG/thumbnail pipeline runs end-to-end for a
true behavioral comparison with legacy. They are therefore marked
``@pytest.mark.system`` and are **opt-in** — ``pyproject.toml``'s ``addopts``
deselects them by default. Run them explicitly with::

    uv run pytest -m system

They still never *write* to S3: every call uses ``upload=False``, so the
base-class ``upload_item_assets_to_s3`` no-ops. The STAC API item-lookup that
``is_newer_than_existing`` performs stays stubbed to 404 by the session-scoped
autouse ``_stub_stac_api`` fixture in ``conftest.py`` (``requests_mock`` only
patches the ``requests`` library, so the boto3 S3 asset downloads pass through
to the real bucket while the STAC gate stays deterministic — it always
"proceeds" rather than depending on what happens to be ingested in production).

Downloaded imagery is cached under ``tests/external-data/<payload-id>`` (via
``save_workdir=True``) so re-runs are fast; delete that directory to force a
clean re-fetch.
"""

import json
import os
from pathlib import Path
from typing import Any, Generator

import deepdiff
import pytest
from shapely.geometry import Point, shape
from stactask.exceptions import InvalidInput

from sentinel_2_l2a_to_stac.task import Sentinel2ToStac

# % change relative to 1-degree-squared tile geometry
# (which works out to a linear error of about 11m)
DIFFERENCE_THRESHOLD = 5e-8

# absolute difference in degrees
# (which works out to be "a particular corner of a house" https://xkcd.com/2170/)
CENTROID_DIFF_THRESHOLD = 5e-5
THUMBNAIL_SIZE_THRESHOLD = 10

WORKDIR_ROOT = Path(__file__).parent.absolute() / "external-data"
FIXTURES = Path(__file__).parent / "fixtures" / "payloads"


def normalize(output: dict[str, Any]) -> Any:
    for f in output.get("features", []):
        if p := f.get("properties"):
            del p["created"]
        for asset in f.get("assets", []).values():
            if href := asset.get("href"):
                asset["href"] = href.removeprefix(str(WORKDIR_ROOT))
        if ses := f.get("stac_extensions"):
            ses.sort()
    # normalizes the types
    return json.loads(json.dumps(output))


def diff_output(
    fixture_dir: Path, actual_output_orig: dict[str, Any]
) -> deepdiff.DeepDiff:
    filename_out = fixture_dir / "out.json"
    filename_actual_out = fixture_dir / "actual.json"

    actual_output = normalize(actual_output_orig)

    expected_output = None
    if not os.path.exists(filename_out):
        # First run for this fixture: persist the generated baseline. Use a deep
        # copy (not an alias) so the shared thumbnail pop-from-both below operates
        # on two independent dicts rather than popping the same key twice.
        filename_out.write_text(json.dumps(actual_output, indent=2) + "\n")
        expected_output = json.loads(json.dumps(actual_output))
    else:
        expected_output = json.loads(filename_out.read_text())

        if expected_output is None:
            raise ValueError("expected output file is empty")

        if not actual_output.get("features"):
            filename_actual_out.write_text(
                json.dumps(actual_output_orig, indent=2) + "\n"
            )
            raise Exception("features missing in output")

        # different platforms and containers compute inconsequentially different
        # values for geometry, so we calculate the symmetric difference in the
        # area instead of simply comparing them
        actual_geometry = actual_output["features"][0].pop("geometry", None)
        expected_geometry = expected_output["features"][0].pop("geometry", None)
        assert actual_geometry, "Actual geometry is missing"
        assert expected_geometry, "Expected geometry is missing"
        actual_geometry_shape = shape(actual_geometry)
        expected_geometry_shape = shape(expected_geometry)
        geometry_area_diff_ratio = (
            expected_geometry_shape.symmetric_difference(actual_geometry_shape).area
            / expected_geometry_shape.area
        )

        # same with the centroid, we check if the points are equal with a threshold
        actual_centroid = actual_output["features"][0]["properties"].pop(
            "proj:centroid"
        )
        expected_centroid = expected_output["features"][0]["properties"].pop(
            "proj:centroid"
        )
        actual_centroid_point = Point(actual_centroid["lon"], actual_centroid["lat"])
        expected_centroid_point = Point(
            expected_centroid["lon"], expected_centroid["lat"]
        )
        is_same_centroid = actual_centroid_point.equals_exact(
            expected_centroid_point,
            CENTROID_DIFF_THRESHOLD,
        )

        if geometry_area_diff_ratio > DIFFERENCE_THRESHOLD:
            # if the area is too different we put the geoms
            # back in so they get flagged in the diff
            actual_output["features"][0]["geometry"] = actual_geometry
            expected_output["features"][0]["geometry"] = expected_geometry

        if not is_same_centroid:
            # if the centroids are too different we put the
            # properties back in so they get flagged in the diff
            actual_output["features"][0]["properties"]["proj:centroid"] = (
                actual_centroid
            )
            expected_output["features"][0]["properties"]["proj:centroid"] = (
                expected_centroid
            )

    # TODO: need to understand why we have a difference here (probably underlying
    # libs) do this same song and dance to workaround a difference with thumbnail
    # files
    if actual_output["features"][0]["assets"]["thumbnail"]["file:checksum"]:
        actual_output["features"][0]["assets"]["thumbnail"].pop("file:checksum")
        expected_output["features"][0]["assets"]["thumbnail"].pop("file:checksum")

    expected_tn_size = expected_output["features"][0]["assets"]["thumbnail"][
        "file:size"
    ]

    if (
        expected_tn_size - THUMBNAIL_SIZE_THRESHOLD
        < actual_output["features"][0]["assets"]["thumbnail"]["file:size"]
        < expected_tn_size + THUMBNAIL_SIZE_THRESHOLD
    ):
        actual_output["features"][0]["assets"]["thumbnail"].pop("file:size")
        expected_output["features"][0]["assets"]["thumbnail"].pop("file:size")

    diff = deepdiff.DeepDiff(expected_output, actual_output, math_epsilon=0.00001)

    if diff:
        filename_actual_out.write_text(json.dumps(actual_output_orig, indent=2) + "\n")

    return diff


def failure_cases() -> Generator[Path, None, None]:
    # Only the network parity failures (full pipeline reaches a downstream
    # InvalidInput). `missing-metadata-href` fails offline in validate() and
    # lacks a top-level `id` for the workdir path; it has its own fast test
    # below (test_validate_requires_metadata_href), so skip it here.
    for d in (FIXTURES / "failure").iterdir():
        if d.name == "missing-metadata-href":
            continue
        yield d


def success_cases() -> Generator[Path, None, None]:
    yield from (FIXTURES / "success").iterdir()


def upgrade_cases() -> Generator[Path, None, None]:
    yield from (FIXTURES / "upgrade").iterdir()


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the full pipeline for a payload, caching imagery, never uploading.

    ``upload=False`` (not the deprecated ``skip_upload``) no-ops the S3 upload;
    ``save_workdir`` caches the downloaded imagery under ``external-data`` keyed
    by the payload id so re-runs skip the re-download.
    """
    return Sentinel2ToStac.handler(
        payload,
        upload=False,
        workdir=WORKDIR_ROOT / payload["id"],
        save_workdir=True,
    )


@pytest.mark.system
@pytest.mark.parametrize("fixture_dir", failure_cases(), ids=lambda x: x.name)
def test_failing_files(fixture_dir: Path) -> None:
    payload = json.loads((fixture_dir / "in.json").read_text())
    exception_msg = (fixture_dir / "exception.txt").read_text().strip()

    with pytest.raises(InvalidInput) as excinfo:
        _run(payload)

    assert exception_msg in str(excinfo.value)


@pytest.mark.system
@pytest.mark.parametrize("fixture_dir", success_cases(), ids=lambda x: x.name)
def test_successful_payload_to_input_item(fixture_dir: Path) -> None:
    payload = json.loads((fixture_dir / "in.json").read_text())

    actual_output = _run(payload)

    diff = diff_output(fixture_dir, actual_output)

    if diff:
        pytest.fail(
            f"expected output does not match:\n{diff.to_json(indent=4)}", pytrace=False
        )


# Update-first parity tests.
# They are opt-in with `-m upgrade` and never write to S3 (`upload=False`).
@pytest.mark.upgrade
@pytest.mark.parametrize("fixture_dir", upgrade_cases(), ids=lambda x: x.name)
def test_upgrade_payload_to_input_item(fixture_dir: Path) -> None:
    payload = json.loads((fixture_dir / "in.json").read_text())

    actual_output = _run(payload)

    diff = diff_output(fixture_dir, actual_output)

    if diff:
        pytest.fail(
            f"expected output does not match:\n{diff.to_json(indent=4)}", pytrace=False
        )


# Standalone regression payloads (single scenes, no out.json comparison — they
# only assert the pipeline runs to completion without error).


@pytest.mark.system
def test_tile_info_missing_tile_data_geometry() -> None:
    # Previously failed when tileInfo.json lacked tileDataGeometry; now falls
    # back to the product metadata. Regression guard.
    payload = json.loads(
        (FIXTURES / "payload-tileinfo-no-tileDataGeometry.json").read_text()
    )
    _run(payload)


@pytest.mark.system
def test_antimeridian_pole_geometry_problem() -> None:
    # This scene previously passed on arm64 but failed on amd64 (or vice-versa).
    # Regression guard for the antimeridian/pole geometry handling.
    payload = json.loads((FIXTURES / "payload-antimeridian-pole.json").read_text())
    _run(payload)


# Fast, offline coverage kept out of the `system` suite: the metadata_href
# validation failure needs no network (it raises in validate() before any
# download), so it runs in the default `uv run pytest`.
def test_validate_requires_metadata_href() -> None:
    fixture_dir = FIXTURES / "failure" / "missing-metadata-href"
    payload = json.loads((fixture_dir / "in.json").read_text())
    exception_msg = (fixture_dir / "exception.txt").read_text().strip()

    with pytest.raises(InvalidInput) as excinfo:
        Sentinel2ToStac.handler(payload, upload=False)

    assert exception_msg in str(excinfo.value)
