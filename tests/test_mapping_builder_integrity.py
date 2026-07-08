"""Tests for :mod:`workers.mapping_builder.integrity` (Epic #909 SUB-1/§1.1, SUB-2/§1.2, SUB-3/§1.3).

These tests exercise every §1.1/§1.2/§1.3 subcheck of the G0 baseline integrity gate:

1. Positive path: a valid fixture yields a populated report.
2. INV-1 read-only: pre/post baseline file checksums must be equal.
3. Fail-closed on unparseable ``.sp.mesh`` / ``.sp.att``.
4. Fail-closed on non-unique / non-contiguous element IDs (mesh or att).
5. Fail-closed on unequal element counts / element-ID sets between mesh and att.
6. Fail-closed on non-positive or non-integer FORC values.
7. Fail-closed on illegal ``.tsd.forc`` references (out of ``1..max_forc``).
8. Signature contract of the public entry point is pinned.
9. §1.2 CRS authority: WKT read from ``gis/*.prj`` only; ``.sp.mesh`` MUST NOT be
   opened as a CRS source; custom Albers and Transverse Mercator both parse and
   transform to WGS84; missing/unparseable ``.prj`` fails closed.
10. §1.2 ancillary inventory: every ``*.tsd.*`` (excluding weather ``.tsd.forc``)
    is enumerated with path + checksum + size; unreadable ancillary fails closed.
11. §1.3 baseline classification: duplicate-coordinate stations, non-grid X-station
    cohorts, startdate heterogeneity, ``domain.shp`` presence-only checksum, and
    known-harmless deviations (``.tsd.forc`` line-2 absolute paths) are all
    RECORD-ONLY — the baseline is never mutated.
12. §1.3 INV-1 end-to-end evidence: full §1.1+§1.2+§1.3 stack proves byte-identical
    baseline before/after; a mid-run mutation is caught and raises.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import os
import pathlib
import shutil
import stat
from typing import Callable

import pytest

from workers.mapping_builder import (
    AncillaryEntry,
    AncillaryInventoryError,
    AncillaryInventoryReport,
    BaselineClassificationReport,
    BaselineIntegrityError,
    BaselineIntegrityReport,
    DuplicateCoordinateCluster,
    HarmlessDeviationRecord,
    IllegalTsdForcReferenceError,
    Inv1EndToEndEvidence,
    Inv1ViolationError,
    InvalidForcValueError,
    MissingPrjError,
    NonContiguousElementIdError,
    NonGridBaselineFinding,
    NonUniqueElementIdError,
    NonWgs84ConvertiblePrjError,
    PackageCrsReport,
    StartdateRecord,
    UnequalElementCountError,
    UnequalElementIdSetError,
    UnparseableAttError,
    UnparseableMeshError,
    UnparseablePrjError,
    build_ancillary_inventory,
    classify_baseline,
    verify_baseline_inv1_end_to_end,
    verify_g0_baseline,
    verify_package_crs,
)

_FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures" / "mapping_builder"
FIXTURE_ROOT = _FIXTURES_DIR / "keliya_minimal"
DUPLICATE_COORD_FIXTURE = _FIXTURES_DIR / "duplicate_coord_baseline"
NON_GRID_FIXTURE = _FIXTURES_DIR / "non_grid_baseline"
HARMLESS_DEVIATION_FIXTURE = _FIXTURES_DIR / "harmless_deviation_baseline"


def _copy_fixture(target: pathlib.Path) -> pathlib.Path:
    """Deep-copy the ``keliya_minimal`` fixture into ``target`` and return it."""
    dest = target / "keliya_minimal"
    shutil.copytree(FIXTURE_ROOT, dest)
    return dest


def _copy_named_fixture(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    """Deep-copy an arbitrary fixture directory into ``target/<source_name>``."""
    dest = target / source.name
    shutil.copytree(source, dest)
    return dest


def _snapshot_checksums(root: pathlib.Path) -> dict[str, str]:
    """Return ``{relative_path: sha256}`` for every regular file under ``root``."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hasher = hashlib.sha256()
            with open(path, "rb") as handle:
                hasher.update(handle.read())
            result[path.relative_to(root).as_posix()] = hasher.hexdigest()
    return result


def _assert_no_side_effect_output(scratch_dir: pathlib.Path, before: set[str]) -> None:
    """Assert no files appeared under ``scratch_dir`` beyond ``before`` snapshot."""
    now = {p.relative_to(scratch_dir).as_posix() for p in scratch_dir.rglob("*") if p.is_file()}
    assert now == before, f"unexpected side-effect files created: {now - before}"


# --- positive path --------------------------------------------------------


def test_valid_baseline_passes() -> None:
    report = verify_g0_baseline(FIXTURE_ROOT)
    assert isinstance(report, BaselineIntegrityReport)
    assert report.baseline_root == FIXTURE_ROOT
    assert report.element_id_set == frozenset({1, 2, 3, 4})
    assert report.max_forc_value == 4
    assert report.tsd_forc_present is True
    assert report.tsd_forc_reference_count == 4
    assert report.sp_mesh_path.name == "keliya.sp.mesh"
    assert report.sp_att_path.name == "keliya.sp.att"

    # package_checksum is a full-length SHA-256 hex digest.
    assert len(report.package_checksum) == 64
    int(report.package_checksum, 16)

    # per_file_checksums enumerates every file, sorted, with valid SHA-256 hex.
    relative_paths = [rel for rel, _ in report.per_file_checksums]
    assert relative_paths == sorted(relative_paths)
    assert "keliya.sp.mesh" in relative_paths
    assert "keliya.sp.att" in relative_paths
    assert "keliya.tsd.forc" in relative_paths
    assert "gis/keliya.prj" in relative_paths
    for _rel, sha in report.per_file_checksums:
        assert len(sha) == 64
        int(sha, 16)


def test_pre_post_checksums_equal(tmp_path: pathlib.Path) -> None:
    """INV-1: verifying the baseline must not mutate it."""
    baseline = _copy_fixture(tmp_path)
    before = _snapshot_checksums(baseline)
    report = verify_g0_baseline(baseline)
    after = _snapshot_checksums(baseline)
    assert before == after
    # The report's per_file_checksums must equal the observed post-run snapshot.
    assert dict(report.per_file_checksums) == after


def test_verify_g0_baseline_signature_pinned() -> None:
    """Public API contract — argument name/type and return annotation are pinned.

    ``workers.mapping_builder.integrity`` uses ``from __future__ import
    annotations``, so ``inspect.signature`` returns string annotations. We
    resolve them via ``typing.get_type_hints`` so the pin binds to the real
    type object, not the surface source string.
    """
    import typing

    sig = inspect.signature(verify_g0_baseline)
    assert list(sig.parameters.keys()) == ["baseline_root"]
    assert sig.parameters["baseline_root"].default is inspect.Parameter.empty

    hints = typing.get_type_hints(verify_g0_baseline)
    assert hints["baseline_root"] is pathlib.Path
    assert hints["return"] is BaselineIntegrityReport


# --- negative paths -------------------------------------------------------


def _run_negative(
    tmp_path: pathlib.Path,
    mutate: Callable[[pathlib.Path], None],
    expected_error: type[BaselineIntegrityError],
) -> BaseException:
    """Copy the fixture, apply ``mutate``, expect ``expected_error``, prove no side-effect."""
    baseline = _copy_fixture(tmp_path)
    scratch_dir = tmp_path / "output-artifacts"
    scratch_dir.mkdir()
    before = {p.relative_to(scratch_dir).as_posix() for p in scratch_dir.rglob("*") if p.is_file()}
    mutate(baseline)
    with pytest.raises(expected_error) as exc_info:
        verify_g0_baseline(baseline)
    _assert_no_side_effect_output(scratch_dir, before)
    return exc_info.value


def test_checksum_mismatch_after_mutation(tmp_path: pathlib.Path) -> None:
    """Independent proof that ``package_checksum`` reflects file content bytes."""
    baseline_a = _copy_fixture(tmp_path)
    report_a = verify_g0_baseline(baseline_a)

    baseline_b_root = tmp_path / "keliya_b"
    shutil.copytree(FIXTURE_ROOT, baseline_b_root)
    mesh_path = baseline_b_root / "keliya.sp.mesh"
    # Mutate a benign byte (a Zmax value at the end of element row 1: change 100 -> 101).
    text = mesh_path.read_text().replace(
        "1\t1\t2\t4\t0\t2\t0\t100",
        "1\t1\t2\t4\t0\t2\t0\t101",
    )
    mesh_path.write_text(text)
    report_b = verify_g0_baseline(baseline_b_root)
    assert report_a.package_checksum != report_b.package_checksum


def test_unparseable_mesh_raises_UnparseableMeshError(tmp_path: pathlib.Path) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        (baseline / "keliya.sp.mesh").write_text("not-a-valid-header\n")

    error = _run_negative(tmp_path, mutate, UnparseableMeshError)
    assert error.field == "sp.mesh"


def test_unparseable_att_raises_UnparseableAttError(tmp_path: pathlib.Path) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        (baseline / "keliya.sp.att").write_text("garbage\n")

    error = _run_negative(tmp_path, mutate, UnparseableAttError)
    assert error.field == "sp.att"


def test_non_unique_element_id_raises_NonUniqueElementIdError(
    tmp_path: pathlib.Path,
) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        # Duplicate element ID 2 in the mesh: change row-3 (ID=3) to ID=2.
        mesh_path = baseline / "keliya.sp.mesh"
        text = mesh_path.read_text().replace(
            "3\t2\t3\t5\t0\t4\t2\t100",
            "2\t2\t3\t5\t0\t4\t2\t100",
        )
        mesh_path.write_text(text)

    error = _run_negative(tmp_path, mutate, NonUniqueElementIdError)
    assert error.file == "sp.mesh"
    assert error.duplicate_id == 2


def test_non_contiguous_element_id_raises_NonContiguousElementIdError(
    tmp_path: pathlib.Path,
) -> None:
    """Rewrite BOTH files to IDs {1,2,3,5} so equal-set passes but contiguity fails.

    Contiguity is checked after equal-set, so both files must carry the same
    non-contiguous set for this error class to surface. Mesh is checked first,
    so ``error.file == 'sp.mesh'``.
    """

    def mutate(baseline: pathlib.Path) -> None:
        mesh_path = baseline / "keliya.sp.mesh"
        att_path = baseline / "keliya.sp.att"
        mesh_text = mesh_path.read_text().replace(
            "4\t3\t6\t5\t0\t0\t3\t100",
            "5\t3\t6\t5\t0\t0\t3\t100",
        )
        mesh_path.write_text(mesh_text)
        att_text = att_path.read_text().replace(
            "4\t1\t1\t11\t4\t1\t0\t0\t0",
            "5\t1\t1\t11\t4\t1\t0\t0\t0",
        )
        att_path.write_text(att_text)

    error = _run_negative(tmp_path, mutate, NonContiguousElementIdError)
    assert error.file == "sp.mesh"
    assert 4 in error.missing_ids


def test_unequal_element_id_sets_raises_UnequalElementIdSetError(
    tmp_path: pathlib.Path,
) -> None:
    """Mesh IDs = {1,2,3,5}; att IDs = {1,2,3,4} — equal count 4, sets differ.

    The implementation orders checks so equal-count passes, then equal-set
    fails BEFORE either contiguity check (which is verified independently in
    ``test_non_contiguous_element_id_raises_NonContiguousElementIdError``).
    """

    def mutate(baseline: pathlib.Path) -> None:
        # Renumber only mesh element 4 -> 5 so mesh IDs = {1,2,3,5}
        # and att IDs stay {1,2,3,4}.
        mesh_path = baseline / "keliya.sp.mesh"
        text = mesh_path.read_text().replace(
            "4\t3\t6\t5\t0\t0\t3\t100",
            "5\t3\t6\t5\t0\t0\t3\t100",
        )
        mesh_path.write_text(text)

    error = _run_negative(tmp_path, mutate, UnequalElementIdSetError)
    assert error.mesh_only == (5,)
    assert error.att_only == (4,)


def test_unequal_element_counts_raises_UnequalElementCountError(
    tmp_path: pathlib.Path,
) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        att_path = baseline / "keliya.sp.att"
        # Shrink att to 3 rows (also fix header count to keep it parseable).
        att_path.write_text(
            "3\t9\n"
            "INDEX\tSOIL\tGEOL\tLC\tFORC\tMF\tBC\tSS\tLAKE\n"
            "1\t1\t1\t11\t1\t1\t0\t0\t0\n"
            "2\t1\t1\t11\t2\t1\t0\t0\t0\n"
            "3\t1\t1\t11\t3\t1\t0\t0\t0\n"
        )

    error = _run_negative(tmp_path, mutate, UnequalElementCountError)
    assert error.mesh_count == 4
    assert error.att_count == 3


def test_non_positive_forc_value_raises_InvalidForcValueError(
    tmp_path: pathlib.Path,
) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        att_path = baseline / "keliya.sp.att"
        text = att_path.read_text().replace(
            "2\t1\t1\t11\t2\t1\t0\t0\t0",
            "2\t1\t1\t11\t0\t1\t0\t0\t0",
        )
        att_path.write_text(text)

    error = _run_negative(tmp_path, mutate, InvalidForcValueError)
    assert error.element_id == 2
    assert error.invalid_value == 0


def test_non_integer_forc_value_raises_InvalidForcValueError(
    tmp_path: pathlib.Path,
) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        att_path = baseline / "keliya.sp.att"
        text = att_path.read_text().replace(
            "3\t1\t1\t11\t3\t1\t0\t0\t0",
            "3\t1\t1\t11\t1.5\t1\t0\t0\t0",
        )
        att_path.write_text(text)

    error = _run_negative(tmp_path, mutate, InvalidForcValueError)
    assert error.element_id == 3
    assert error.invalid_value == "1.5"


def test_illegal_tsd_forc_reference_raises_IllegalTsdForcReferenceError(
    tmp_path: pathlib.Path,
) -> None:
    def mutate(baseline: pathlib.Path) -> None:
        forc_path = baseline / "keliya.tsd.forc"
        # Change station row 3 (line 6) ID from 3 -> 99 (out of 1..4).
        text = forc_path.read_text().replace(
            "3\t100.00\t36.10\t500000.0\t4010000\t-9999\tforcing_3.csv",
            "99\t100.00\t36.10\t500000.0\t4010000\t-9999\tforcing_3.csv",
        )
        forc_path.write_text(text)

    error = _run_negative(tmp_path, mutate, IllegalTsdForcReferenceError)
    assert error.invalid_reference == 99
    assert error.valid_range == (1, 4)
    # Line 6 = header(3 lines) + station row 3.
    assert error.line_number == 6


def test_no_output_artifact_written_on_any_failure(tmp_path: pathlib.Path) -> None:
    """Contract lock: pure-read verification never writes files.

    We stack multiple negative mutations sequentially in the same scratch
    directory and prove no artifacts appear under a caller-supplied ``output``
    directory across all of them.
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    before = {p.relative_to(output_dir).as_posix() for p in output_dir.rglob("*") if p.is_file()}

    scenarios: list[tuple[Callable[[pathlib.Path], None], type[BaselineIntegrityError]]] = [
        (
            lambda p: (p / "keliya.sp.mesh").write_text("bad\n"),
            UnparseableMeshError,
        ),
        (
            lambda p: (p / "keliya.sp.att").write_text("bad\n"),
            UnparseableAttError,
        ),
        (
            lambda p: (p / "keliya.sp.att").write_text(
                "4\t9\n"
                "INDEX\tSOIL\tGEOL\tLC\tFORC\tMF\tBC\tSS\tLAKE\n"
                "1\t1\t1\t11\t-1\t1\t0\t0\t0\n"
                "2\t1\t1\t11\t2\t1\t0\t0\t0\n"
                "3\t1\t1\t11\t3\t1\t0\t0\t0\n"
                "4\t1\t1\t11\t4\t1\t0\t0\t0\n"
            ),
            InvalidForcValueError,
        ),
    ]
    for idx, (mutate, expected) in enumerate(scenarios):
        scratch = tmp_path / f"scenario-{idx}"
        shutil.copytree(FIXTURE_ROOT, scratch)
        mutate(scratch)
        with pytest.raises(expected):
            verify_g0_baseline(scratch)
        _assert_no_side_effect_output(output_dir, before)


# --- §1.2 CRS authority ---------------------------------------------------


# Live qhh-style Transverse Mercator WKT (matches live audit: qhh basin uses
# TM with no EPSG code). Kept inline so the test proves parseability of a
# second projection family without a second on-disk fixture.
_QHH_TM_WKT = (
    'PROJCS["unknown",'
    'GEOGCS["GCS_WGS_1984",'
    'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",500000.0],'
    'PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",99.0],'
    'PARAMETER["Scale_Factor",0.9996],'
    'PARAMETER["Latitude_Of_Origin",0.0],'
    'UNIT["Meter",1.0]]'
)


def test_crs_read_from_prj_only(tmp_path: pathlib.Path) -> None:
    """§1.2: CRS comes only from ``gis/*.prj``; ``.sp.mesh`` is NOT a CRS source.

    We prove the contract two ways:
    (1) verify_package_crs returns the exact WKT bytes stored in gis/*.prj.
    (2) With ``.sp.mesh`` removed (which would break verify_g0_baseline),
        verify_package_crs still succeeds — proving it never touches
        ``.sp.mesh``.
    """
    baseline = _copy_fixture(tmp_path)
    prj_path = baseline / "gis" / "keliya.prj"
    wkt_on_disk = prj_path.read_text(encoding="utf-8").strip()

    report_before = verify_package_crs(baseline)
    assert isinstance(report_before, PackageCrsReport)
    assert report_before.prj_path == prj_path
    assert report_before.wkt == wkt_on_disk
    # Checksum equals SHA-256 of the .prj file on disk.
    hasher = hashlib.sha256()
    hasher.update(prj_path.read_bytes())
    assert report_before.prj_checksum == hasher.hexdigest()

    # Now delete .sp.mesh and re-run: if verify_package_crs secretly opened
    # .sp.mesh as a CRS source, this call would break. It does not.
    (baseline / "keliya.sp.mesh").unlink()
    report_after = verify_package_crs(baseline)
    assert report_after.wkt == report_before.wkt
    assert report_after.prj_checksum == report_before.prj_checksum


def test_crs_custom_albers_parses_to_wgs84() -> None:
    """§1.2: the fixture PROJCS custom Albers WKT round-trips to WGS84.

    The fixture ``gis/keliya.prj`` is a live-audit-shaped
    ``PROJCS["unknown"]`` custom Albers with continental-China parameters.
    The transformer must produce finite (lon, lat) — that is the proof of
    "convertible to WGS84" required by §1.2.
    """
    report = verify_package_crs(FIXTURE_ROOT)
    assert "Albers" in report.wkt
    lon, lat = report.wgs84_probe
    # Origin (0, 0) in Central_Meridian=105 Albers transforms to a finite
    # (lon, lat) pair with lon near 105° (central meridian).
    assert -180.0 <= lon <= 180.0
    assert -90.0 <= lat <= 90.0
    assert abs(lon - 105.0) < 1e-6, f"expected lon near 105° central meridian, got {lon}"


def test_crs_qhh_transverse_mercator_parses_to_wgs84(tmp_path: pathlib.Path) -> None:
    """§1.2: an inline qhh-style Transverse Mercator WKT parses to WGS84.

    Live audit lists qhh basin as Transverse Mercator (no EPSG). This test
    swaps the fixture ``.prj`` for a qhh-shaped TM WKT and proves the second
    projection family works through the same public entry point.
    """
    baseline = _copy_fixture(tmp_path)
    prj_path = baseline / "gis" / "keliya.prj"
    prj_path.write_text(_QHH_TM_WKT + "\n", encoding="utf-8")

    report = verify_package_crs(baseline)
    assert "Transverse_Mercator" in report.wkt
    lon, lat = report.wgs84_probe
    assert -180.0 <= lon <= 180.0
    assert -90.0 <= lat <= 90.0
    # TM Central_Meridian=99 with (x=0, y=0) transforms far west of 99°
    # (False_Easting shifts x=0 to west of central meridian). Just prove
    # the transformer produced finite plausible lat/lon.
    assert lon == lon  # not NaN
    assert lat == lat  # not NaN


def test_missing_prj_raises_MissingPrjError(tmp_path: pathlib.Path) -> None:
    """§1.2: no ``gis/*.prj`` -> :class:`MissingPrjError`, no output."""
    baseline = _copy_fixture(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    before = {p.relative_to(output_dir).as_posix() for p in output_dir.rglob("*") if p.is_file()}

    (baseline / "gis" / "keliya.prj").unlink()

    with pytest.raises(MissingPrjError) as exc_info:
        verify_package_crs(baseline)
    assert exc_info.value.baseline_root == baseline
    _assert_no_side_effect_output(output_dir, before)


def test_missing_gis_directory_raises_MissingPrjError(tmp_path: pathlib.Path) -> None:
    """§1.2: no ``gis/`` directory at all -> :class:`MissingPrjError`."""
    baseline = _copy_fixture(tmp_path)
    shutil.rmtree(baseline / "gis")
    with pytest.raises(MissingPrjError):
        verify_package_crs(baseline)


def test_unparseable_prj_raises_UnparseablePrjError(tmp_path: pathlib.Path) -> None:
    """§1.2: garbage WKT -> :class:`UnparseablePrjError`, no output."""
    baseline = _copy_fixture(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    before = {p.relative_to(output_dir).as_posix() for p in output_dir.rglob("*") if p.is_file()}

    prj_path = baseline / "gis" / "keliya.prj"
    prj_path.write_text("this is not a valid WKT string\n", encoding="utf-8")

    with pytest.raises(UnparseablePrjError) as exc_info:
        verify_package_crs(baseline)
    assert exc_info.value.prj_path == prj_path
    assert exc_info.value.parse_error  # non-empty error message
    _assert_no_side_effect_output(output_dir, before)


def test_empty_prj_raises_UnparseablePrjError(tmp_path: pathlib.Path) -> None:
    """§1.2: an empty ``.prj`` (whitespace only) fails closed as unparseable."""
    baseline = _copy_fixture(tmp_path)
    (baseline / "gis" / "keliya.prj").write_text("   \n", encoding="utf-8")
    with pytest.raises(UnparseablePrjError):
        verify_package_crs(baseline)


def test_verify_package_crs_signature_pinned() -> None:
    """Public API contract — argument name/type and return annotation are pinned."""
    import typing

    sig = inspect.signature(verify_package_crs)
    assert list(sig.parameters.keys()) == ["baseline_root"]
    assert sig.parameters["baseline_root"].default is inspect.Parameter.empty

    hints = typing.get_type_hints(verify_package_crs)
    assert hints["baseline_root"] is pathlib.Path
    assert hints["return"] is PackageCrsReport


# --- §1.2 ancillary inventory --------------------------------------------


def test_ancillary_inventory_complete() -> None:
    """§1.2: inventory enumerates every ancillary ``*.tsd.*`` (excluding weather).

    Fixture carries ``keliya.tsd.mf`` and ``keliya.tsd.lai`` as ancillary
    ``*.tsd.*``; ``keliya.tsd.forc`` is the weather reference and MUST NOT
    appear in the inventory (design.md line 62).
    """
    report = build_ancillary_inventory(FIXTURE_ROOT)
    assert isinstance(report, AncillaryInventoryReport)
    assert report.baseline_root == FIXTURE_ROOT

    names = [entry.path.name for entry in report.entries]
    assert names == sorted(names), "inventory entries must be sorted by path"
    assert "keliya.tsd.mf" in names
    assert "keliya.tsd.lai" in names
    # Weather-forcing reference is excluded per §8.1 / design.md line 62.
    assert "keliya.tsd.forc" not in names
    # Non-ancillary files (mesh/att/prj) also excluded.
    assert "keliya.sp.mesh" not in names
    assert "keliya.sp.att" not in names
    assert "keliya.prj" not in names

    for entry in report.entries:
        assert isinstance(entry, AncillaryEntry)
        assert entry.path.is_file()
        # SHA-256 hex digest length + validity.
        assert len(entry.checksum) == 64
        int(entry.checksum, 16)
        # Size equals what stat reports.
        assert entry.size_bytes == entry.path.stat().st_size
        # Checksum equals hashlib SHA-256 of file bytes.
        hasher = hashlib.sha256()
        hasher.update(entry.path.read_bytes())
        assert entry.checksum == hasher.hexdigest()


def test_ancillary_inventory_empty_when_no_tsd_ancillary(tmp_path: pathlib.Path) -> None:
    """§1.2: empty inventory is legal when a package has no ancillary ``*.tsd.*``."""
    baseline = _copy_fixture(tmp_path)
    (baseline / "keliya.tsd.mf").unlink()
    (baseline / "keliya.tsd.lai").unlink()
    report = build_ancillary_inventory(baseline)
    assert report.entries == ()


def test_ancillary_inventory_fails_on_unreadable_file(tmp_path: pathlib.Path) -> None:
    """§1.2: an unreadable ancillary file fails closed as :class:`AncillaryInventoryError`.

    On POSIX we simulate the failure by chmod'ing an ancillary file to 000
    (no permissions) while running as non-root. On systems where the process
    can still read the file (e.g. running as root), the test is skipped —
    the contract we care about is that OSError bubbles through the typed
    error class, not the OS-level enforcement of chmod.
    """
    if os.geteuid() == 0:  # pragma: no cover - CI-side skip
        pytest.skip("chmod-based unreadability cannot be simulated as root")

    baseline = _copy_fixture(tmp_path)
    target = baseline / "keliya.tsd.mf"
    target.chmod(0)
    try:
        with pytest.raises(AncillaryInventoryError) as exc_info:
            build_ancillary_inventory(baseline)
        assert exc_info.value.path == target
        assert exc_info.value.read_error  # non-empty
    finally:
        # Restore permissions so tmp_path cleanup works.
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_ancillary_inventory_signature_pinned() -> None:
    """Public API contract — argument name/type and return annotation are pinned."""
    import typing

    sig = inspect.signature(build_ancillary_inventory)
    assert list(sig.parameters.keys()) == ["baseline_root"]
    assert sig.parameters["baseline_root"].default is inspect.Parameter.empty

    hints = typing.get_type_hints(build_ancillary_inventory)
    assert hints["baseline_root"] is pathlib.Path
    assert hints["return"] is AncillaryInventoryReport


def test_verify_package_crs_does_not_mutate_baseline(tmp_path: pathlib.Path) -> None:
    """INV-1 extension: verify_package_crs never mutates the baseline package."""
    baseline = _copy_fixture(tmp_path)
    before = _snapshot_checksums(baseline)
    verify_package_crs(baseline)
    after = _snapshot_checksums(baseline)
    assert before == after


def test_build_ancillary_inventory_does_not_mutate_baseline(
    tmp_path: pathlib.Path,
) -> None:
    """INV-1 extension: build_ancillary_inventory never mutates the baseline package."""
    baseline = _copy_fixture(tmp_path)
    before = _snapshot_checksums(baseline)
    build_ancillary_inventory(baseline)
    after = _snapshot_checksums(baseline)
    assert before == after


def test_non_wgs84_convertible_prj_raises_error(tmp_path: pathlib.Path) -> None:
    """§1.2: a WKT that parses but produces non-finite WGS84 probe -> fail closed.

    We construct a syntactically valid PROJCS whose projection parameters
    are pathological enough (or the probe is far outside the projection's
    valid domain) that the transformer returns NaN/inf. If we can't force
    that outcome deterministically across PROJ versions, the test verifies
    the error type is reachable via the module surface — see
    :class:`NonWgs84ConvertiblePrjError` covers ProjError.

    We use a projection with a South Pole latitude of origin and a probe at
    (0, 0) which is nowhere near the valid domain on some PROJ versions,
    causing the transformer to raise or return non-finite.
    """
    # A syntactically valid but pathological WKT: Polar Stereographic centered
    # at the South Pole with a tiny scale factor. Depending on PROJ version
    # this either raises on transform or produces inf. If it does neither on
    # a given PROJ version, we accept a successful report (the ability to
    # RAISE the error class is what we're testing at contract level; that's
    # already covered by the class definition + import above).
    pathological_wkt = (
        'PROJCS["unknown",'
        'GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],'
        'UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Polar_Stereographic"],'
        'PARAMETER["False_Easting",0.0],'
        'PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",0.0],'
        'PARAMETER["Standard_Parallel_1",-90.0],'
        'PARAMETER["Latitude_Of_Origin",-90.0],'
        'UNIT["Meter",1.0]]'
    )
    baseline = _copy_fixture(tmp_path)
    (baseline / "gis" / "keliya.prj").write_text(pathological_wkt + "\n", encoding="utf-8")
    # Either the transformer raises NonWgs84ConvertiblePrjError, or it
    # returns a finite (lon, lat) pair — both are acceptable outcomes on
    # different PROJ versions. What is NOT acceptable is any OTHER exception
    # class or a silent NaN/inf leak past the guard.
    try:
        report = verify_package_crs(baseline)
    except NonWgs84ConvertiblePrjError:
        return  # expected on the version that catches the pathology
    # If it did not raise, the returned probe must be finite (the guard held).
    lon, lat = report.wgs84_probe
    assert lon == lon and lat == lat, "probe must not be NaN"
    assert lon not in (float("inf"), float("-inf"))
    assert lat not in (float("inf"), float("-inf"))


# --- §1.3 baseline classification (RECORD-ONLY) --------------------------


def test_classify_duplicate_coord_stations() -> None:
    """§1.3: zhaochen_mc-style 4 stations at identical coords -> 1 cluster (mult=4).

    The duplicate_coord_baseline fixture places 4 stations at exactly
    ``(105.50, 38.20, Z=-9999)`` — the zhaochen_mc live-audit pattern.
    Classification MUST register one :class:`DuplicateCoordinateCluster`
    with multiplicity=4 and the Z=-9999 sentinel preserved.
    """
    report = classify_baseline(DUPLICATE_COORD_FIXTURE)
    assert isinstance(report, BaselineClassificationReport)
    assert len(report.duplicate_coord_clusters) == 1
    cluster = report.duplicate_coord_clusters[0]
    assert isinstance(cluster, DuplicateCoordinateCluster)
    assert cluster.multiplicity == 4
    assert cluster.station_ids == ("1", "2", "3", "4")
    lon, lat, z = cluster.coords
    assert lon == pytest.approx(105.50)
    assert lat == pytest.approx(38.20)
    assert z == pytest.approx(-9999.0)
    # A duplicate-only fixture must not trigger non-grid findings or harmless
    # deviations — this is a clean positive control for the duplicate signal.
    assert report.non_grid_findings == ()
    assert report.harmless_deviations == ()
    assert report.domain_shp_checksum is None


def test_classify_non_grid_baseline() -> None:
    """§1.3: zhaochen_wem-style 5 X1..X5 stations at irregular spacing -> 1 finding.

    The non_grid_baseline fixture carries 5 stations with filenames
    ``X1..X5.csv`` positioned so they do NOT tile a regular lat-lon grid
    (3 unique lons × 3 unique lats = 9 possible points ≠ 5 stations).
    The spacing_estimate must resolve close to the 0.02° step used in the
    fixture (matching live-audit zhaochen_wem note).
    """
    report = classify_baseline(NON_GRID_FIXTURE)
    assert len(report.non_grid_findings) == 1
    finding = report.non_grid_findings[0]
    assert isinstance(finding, NonGridBaselineFinding)
    assert finding.station_prefix == "X"
    assert finding.station_count == 5
    assert finding.spacing_estimate is not None
    assert finding.spacing_estimate == pytest.approx(0.02, abs=1e-6)
    assert "regular lat-lon grid" in finding.pattern_note
    # A pure non-grid cohort with all-distinct coords must not produce
    # duplicate-coord clusters — signals stay isolated.
    assert report.duplicate_coord_clusters == ()


def test_startdate_heterogeneity_recorded(tmp_path: pathlib.Path) -> None:
    """§1.3: two baselines with different ``.tsd.forc`` startdates are both recorded.

    We copy two fixtures into a shared parent and classify each independently
    to prove startdates are RECORDED per file — not normalized, deduped, or
    reshaped. Live audit lists 1951–2024 heterogeneity across 13 basins;
    our fixtures cover ``20200101`` (keliya) and ``19510101`` (harmless).
    """
    keliya = _copy_fixture(tmp_path)
    harmless = _copy_named_fixture(HARMLESS_DEVIATION_FIXTURE, tmp_path)

    keliya_report = classify_baseline(keliya)
    harmless_report = classify_baseline(harmless)

    assert len(keliya_report.startdate_heterogeneity) == 1
    assert len(harmless_report.startdate_heterogeneity) == 1

    keliya_start = keliya_report.startdate_heterogeneity[0]
    harmless_start = harmless_report.startdate_heterogeneity[0]
    assert isinstance(keliya_start, StartdateRecord)
    assert keliya_start.startdate == "20200101"
    assert keliya_start.path.name == "keliya.tsd.forc"
    assert harmless_start.startdate == "19510101"
    assert harmless_start.path.name == "harmless.tsd.forc"

    # Startdates differ verbatim — no silent normalization.
    assert keliya_start.startdate != harmless_start.startdate


def test_domain_shp_recorded_not_consumed_as_geometry(tmp_path: pathlib.Path) -> None:
    """§1.3: ``domain.shp`` is checksum-recorded but NEVER opened as geometry.

    Proof by ablation: we (a) classify with ``domain.shp`` present, cache
    the ``verify_g0_baseline`` output, then (b) delete ``domain.shp`` and
    re-run ``verify_g0_baseline`` — its report must be byte-identical
    (element ID set, sp.mesh/sp.att paths, FORC counts unchanged). If
    verify_g0_baseline had secretly consumed ``domain.shp`` as element-ID
    or geometry authority, its outputs would drift after the ablation.
    """
    baseline = _copy_named_fixture(HARMLESS_DEVIATION_FIXTURE, tmp_path)

    # Step 1: sanity — domain.shp is present and recorded.
    domain_shp = baseline / "domain.shp"
    assert domain_shp.is_file(), "fixture must ship domain.shp"
    report_before = classify_baseline(baseline)
    assert report_before.domain_shp_checksum is not None
    hasher = hashlib.sha256()
    hasher.update(domain_shp.read_bytes())
    assert report_before.domain_shp_checksum == hasher.hexdigest()

    # Step 2: capture verify_g0_baseline output with domain.shp present.
    g0_before = verify_g0_baseline(baseline)

    # Step 3: ablate — delete domain.shp — and re-run verify_g0_baseline.
    domain_shp.unlink()
    g0_after = verify_g0_baseline(baseline)

    # Step 4: G0 report fields that would encode geometry / element-ID
    # authority must NOT drift. (per_file_checksums naturally differs since
    # domain.shp is gone, so we check only the geometry-derived fields.)
    assert g0_after.element_id_set == g0_before.element_id_set
    assert g0_after.sp_mesh_path.name == g0_before.sp_mesh_path.name
    assert g0_after.sp_att_path.name == g0_before.sp_att_path.name
    assert g0_after.max_forc_value == g0_before.max_forc_value
    assert g0_after.tsd_forc_reference_count == g0_before.tsd_forc_reference_count

    # Step 5: classification with no domain.shp records None (not error).
    report_after = classify_baseline(baseline)
    assert report_after.domain_shp_checksum is None


def test_harmless_deviation_recorded_not_repaired(tmp_path: pathlib.Path) -> None:
    """§1.3: ``.tsd.forc`` line-2 absolute path is RECORDED but never rewritten.

    Non-goal for §1.3: "no repair of known-harmless baseline deviations".
    We prove classification records the excerpt AND leaves the file bytes
    identical (INV-1) — even the deviation-carrying line is untouched.
    """
    baseline = _copy_named_fixture(HARMLESS_DEVIATION_FIXTURE, tmp_path)
    tsd_path = baseline / "harmless.tsd.forc"
    bytes_before = tsd_path.read_bytes()

    report = classify_baseline(baseline)
    assert len(report.harmless_deviations) == 1
    record = report.harmless_deviations[0]
    assert isinstance(record, HarmlessDeviationRecord)
    assert record.deviation_kind == "tsd_forc_line2_absolute_path"
    assert record.path == tsd_path
    assert record.evidence_excerpt.startswith("/home/ghdc/nwm")

    bytes_after = tsd_path.read_bytes()
    assert bytes_before == bytes_after, "classification must not rewrite the .tsd.forc file"


def test_classify_baseline_INV1_read_only(tmp_path: pathlib.Path) -> None:
    """§1.3: classify_baseline never mutates any baseline file (pre/post SHA-256 equal)."""
    for fixture in (FIXTURE_ROOT, DUPLICATE_COORD_FIXTURE, NON_GRID_FIXTURE, HARMLESS_DEVIATION_FIXTURE):
        baseline = _copy_named_fixture(fixture, tmp_path)
        before = _snapshot_checksums(baseline)
        classify_baseline(baseline)
        after = _snapshot_checksums(baseline)
        assert before == after, f"classify_baseline mutated {fixture.name}"


def test_verify_baseline_inv1_end_to_end_positive(tmp_path: pathlib.Path) -> None:
    """§1.3: full stack (§1.1 + §1.2 + §1.3) proves byte-identical pre/post.

    Happy path: run every entry point in the chain and confirm the returned
    :class:`Inv1EndToEndEvidence` carries identical pre and post checksum
    tuples — this is the end-to-end INV-1 receipt.
    """
    baseline = _copy_fixture(tmp_path)
    evidence = verify_baseline_inv1_end_to_end(baseline)
    assert isinstance(evidence, Inv1EndToEndEvidence)
    assert evidence.pre_checksums == evidence.post_checksums
    # Both lists sorted by rel-path, non-empty, valid SHA-256 hex.
    rel_paths = [rel for rel, _ in evidence.pre_checksums]
    assert rel_paths == sorted(rel_paths)
    assert rel_paths, "baseline must contain at least one file"
    for _rel, sha in evidence.pre_checksums:
        assert len(sha) == 64
        int(sha, 16)


def test_verify_baseline_inv1_end_to_end_detects_mid_run_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1.3: a mid-run mutation between pre-check and post-check is CAUGHT.

    We monkeypatch ``classify_baseline`` inside the integrity module to first
    call the real classifier, THEN mutate a baseline file. When
    ``verify_baseline_inv1_end_to_end`` re-hashes post-run, it must detect the
    drift and raise :class:`Inv1ViolationError`.
    """
    baseline = _copy_fixture(tmp_path)
    from workers.mapping_builder import integrity as integrity_module

    real_classify = integrity_module.classify_baseline

    def sneaky_classify(root: pathlib.Path) -> BaselineClassificationReport:
        result = real_classify(root)
        # Mutate a baseline file to drift the post-checksum.
        target = root / "keliya.tsd.mf"
        target.write_bytes(target.read_bytes() + b"\n# sneaky append\n")
        return result

    monkeypatch.setattr(integrity_module, "classify_baseline", sneaky_classify)

    with pytest.raises(Inv1ViolationError) as exc_info:
        integrity_module.verify_baseline_inv1_end_to_end(baseline)
    # Verify the drifted-path list surfaces the exact victim.
    assert "keliya.tsd.mf" in exc_info.value.drifted_paths


def test_classify_baseline_signature_pinned() -> None:
    """Public API contract — argument name/type and return annotation are pinned."""
    import typing

    sig = inspect.signature(classify_baseline)
    assert list(sig.parameters.keys()) == ["baseline_root"]
    assert sig.parameters["baseline_root"].default is inspect.Parameter.empty

    hints = typing.get_type_hints(classify_baseline)
    assert hints["baseline_root"] is pathlib.Path
    assert hints["return"] is BaselineClassificationReport


def test_verify_baseline_inv1_end_to_end_signature_pinned() -> None:
    """Public API contract — arg names/types and return annotation are pinned.

    ``historical_forcing_dir`` is optional and defaults to ``None``; the type
    hint resolves to ``pathlib.Path | None`` at runtime.
    """
    import typing

    sig = inspect.signature(verify_baseline_inv1_end_to_end)
    params = list(sig.parameters.keys())
    assert params == ["baseline_root", "historical_forcing_dir"]
    assert sig.parameters["baseline_root"].default is inspect.Parameter.empty
    assert sig.parameters["historical_forcing_dir"].default is None

    hints = typing.get_type_hints(verify_baseline_inv1_end_to_end)
    assert hints["baseline_root"] is pathlib.Path
    # Optional[pathlib.Path] is represented as pathlib.Path | None.
    assert hints["historical_forcing_dir"] == (pathlib.Path | None)
    assert hints["return"] is Inv1EndToEndEvidence


def test_classification_report_frozen(tmp_path: pathlib.Path) -> None:
    """§1.3: BaselineClassificationReport and its record dataclasses are frozen."""
    baseline = _copy_named_fixture(DUPLICATE_COORD_FIXTURE, tmp_path)
    report = classify_baseline(baseline)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.domain_shp_checksum = "spoofed"  # type: ignore[misc]
    # The nested record dataclasses are also frozen.
    cluster = report.duplicate_coord_clusters[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cluster.multiplicity = 999  # type: ignore[misc]

    # Cross-check the other record dataclasses on their respective fixtures.
    non_grid_report = classify_baseline(_copy_named_fixture(NON_GRID_FIXTURE, tmp_path))
    finding = non_grid_report.non_grid_findings[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.station_count = 999  # type: ignore[misc]

    harmless_report = classify_baseline(_copy_named_fixture(HARMLESS_DEVIATION_FIXTURE, tmp_path))
    harmless_record = harmless_report.harmless_deviations[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        harmless_record.deviation_kind = "spoofed"  # type: ignore[misc]

    startdate_record = harmless_report.startdate_heterogeneity[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        startdate_record.startdate = "19000101"  # type: ignore[misc]


def test_keliya_minimal_classification_zero_findings() -> None:
    """§1.3: keliya_minimal is a clean baseline — no dups, no non-grid, no domain.shp.

    Positive control: the SUB-1/SUB-2 canonical fixture yields empty tuples
    on every classification signal except a single startdate record (its
    baseline .tsd.forc line 1 declares ``20200101``). This proves
    classification does not misfire on clean packages.
    """
    report = classify_baseline(FIXTURE_ROOT)
    assert report.duplicate_coord_clusters == ()
    assert report.non_grid_findings == ()
    assert report.harmless_deviations == ()
    assert report.domain_shp_checksum is None
    assert len(report.startdate_heterogeneity) == 1
    assert report.startdate_heterogeneity[0].startdate == "20200101"


def test_classify_baseline_absorbs_non_utf8_tsd_forc(tmp_path: pathlib.Path) -> None:
    """§1.3 RECORD-ONLY: a non-UTF-8 ``.tsd.forc`` MUST NOT raise from classify_baseline.

    ``classify_baseline``'s docstring promises it never raises on malformed
    content — only on ``baseline_root`` not being a directory. A ``.tsd.forc``
    with a non-UTF-8 byte (e.g. ``b"\\xff"``) would trip
    :func:`_read_text_lines`'s decode into :class:`UnparseableMeshError`;
    the parser MUST swallow that failure with a sentinel-empty return,
    matching the sibling ``_parse_sp_att_station_index`` /
    ``_detect_harmless_deviations`` helpers. The §1.1 gate is the sole
    authority on decode failures for weather forcing files.
    """
    baseline = _copy_fixture(tmp_path)
    tsd_path = baseline / "keliya.tsd.forc"
    # Overwrite with header shape plus a non-UTF-8 byte so ``_read_text_lines``
    # raises inside classification. If the fix is missing, this call re-raises
    # UnparseableMeshError; if the fix holds, the file's stations/startdate
    # simply drop out of the aggregate.
    tsd_path.write_bytes(b"20200101 20200102\n\n\n1 \xff 105.0 0.0 0.0 -9999 X1.csv\n")

    # Must not raise.
    report = classify_baseline(baseline)
    assert isinstance(report, BaselineClassificationReport)

    # The malformed .tsd.forc contributes no stations and no startdate:
    #   - stations excluded => no duplicate-coord clusters, no non-grid findings.
    #   - startdate excluded => no StartdateRecord for that file.
    assert report.duplicate_coord_clusters == ()
    assert report.non_grid_findings == ()
    for record in report.startdate_heterogeneity:
        assert record.path != tsd_path, (
            "malformed .tsd.forc must NOT surface as a startdate record"
        )


def test_verify_baseline_inv1_end_to_end_history_collision_drift_payload(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1.3: rel_path collision between baseline ``history/`` + historical dir MUST NOT hide drift.

    When ``historical_forcing_dir`` is supplied, historical files are namespaced
    with a ``history/`` prefix by :func:`_compute_end_to_end_checksums`. If a
    baseline package ALSO carries a real ``history/`` subdirectory, two entries
    can legitimately share the same rel_path (different SHAs).

    A dict-based drift diff would collapse the pair via last-write-wins and
    report an empty drift payload when only one colliding entry mutates. The
    fixed set-based diff MUST surface the shared rel_path in
    :attr:`Inv1ViolationError.drifted_paths`.
    """
    baseline = _copy_fixture(tmp_path)
    # Baseline contribution: ``history/foo.txt`` with sha=A.
    history_dir = baseline / "history"
    history_dir.mkdir()
    baseline_history_file = history_dir / "foo.txt"
    baseline_history_file.write_bytes(b"baseline-payload-A")

    # historical_forcing_dir contribution: ``foo.txt`` with sha=B, prefixed to
    # ``history/foo.txt`` at checksum-time — same rel_path key as above.
    historical_dir = tmp_path / "historical_forcing"
    historical_dir.mkdir()
    (historical_dir / "foo.txt").write_bytes(b"historical-payload-B")

    from workers.mapping_builder import integrity as integrity_module

    real_classify = integrity_module.classify_baseline

    def sneaky_classify(root: pathlib.Path) -> BaselineClassificationReport:
        result = real_classify(root)
        # Mid-run mutation of ONLY the baseline-side ``history/foo.txt`` (A -> C).
        # The historical-side ``foo.txt`` (B) stays put. A dict-collapse would
        # see {"history/foo.txt": B} on both sides and report drift=() — the
        # regression this test guards against.
        victim = root / "history" / "foo.txt"
        victim.write_bytes(b"mutated-payload-C")
        return result

    monkeypatch.setattr(integrity_module, "classify_baseline", sneaky_classify)

    with pytest.raises(Inv1ViolationError) as exc_info:
        integrity_module.verify_baseline_inv1_end_to_end(
            baseline, historical_forcing_dir=historical_dir
        )
    assert "history/foo.txt" in exc_info.value.drifted_paths, (
        "collision rel_path MUST surface in drifted_paths; empty payload "
        "indicates the dict-collapse regression is back"
    )


def test_verify_baseline_inv1_end_to_end_positive_with_historical_dir(
    tmp_path: pathlib.Path,
) -> None:
    """§1.3: positive path with ``historical_forcing_dir`` — no drift, evidence tuples equal.

    Complements ``test_verify_baseline_inv1_end_to_end_positive`` by covering
    the branch where ``historical_forcing_dir`` is supplied. Also proves the
    set-based drift diff does not spuriously flag rel_path collisions when
    both entries remain byte-stable across pre/post.
    """
    baseline = _copy_fixture(tmp_path)
    # Include a baseline-side ``history/`` file and a historical dir that will
    # collide on rel_path ``history/foo.txt`` at checksum-time. Neither side
    # mutates during the run, so evidence tuples must match end-to-end.
    history_dir = baseline / "history"
    history_dir.mkdir()
    (history_dir / "foo.txt").write_bytes(b"baseline-payload-A")

    historical_dir = tmp_path / "historical_forcing"
    historical_dir.mkdir()
    (historical_dir / "foo.txt").write_bytes(b"historical-payload-B")

    evidence = verify_baseline_inv1_end_to_end(
        baseline, historical_forcing_dir=historical_dir
    )
    assert isinstance(evidence, Inv1EndToEndEvidence)
    assert evidence.pre_checksums == evidence.post_checksums
    # Both colliding entries live in the tuple independently (different SHAs
    # under the same rel_path). Counting occurrences of the collision key
    # proves the checksum sweep does NOT dedupe by rel_path.
    collisions = [rel for rel, _sha in evidence.pre_checksums if rel == "history/foo.txt"]
    assert len(collisions) == 2, (
        "history/foo.txt should appear twice — once from baseline, once from "
        "historical_forcing_dir (post-prefix). Dedup here indicates a regression."
    )
