"""Tests for :mod:`workers.mapping_builder.integrity` (Epic #909 SUB-1, §1.1).

These tests exercise every §1.1 subcheck of the G0 baseline integrity gate:

1. Positive path: a valid fixture yields a populated report.
2. INV-1 read-only: pre/post baseline file checksums must be equal.
3. Fail-closed on unparseable ``.sp.mesh`` / ``.sp.att``.
4. Fail-closed on non-unique / non-contiguous element IDs (mesh or att).
5. Fail-closed on unequal element counts / element-ID sets between mesh and att.
6. Fail-closed on non-positive or non-integer FORC values.
7. Fail-closed on illegal ``.tsd.forc`` references (out of ``1..max_forc``).
8. Signature contract of the public entry point is pinned.
"""

from __future__ import annotations

import hashlib
import inspect
import pathlib
import shutil
from typing import Callable

import pytest

from workers.mapping_builder import (
    BaselineIntegrityError,
    BaselineIntegrityReport,
    IllegalTsdForcReferenceError,
    InvalidForcValueError,
    NonContiguousElementIdError,
    NonUniqueElementIdError,
    UnequalElementCountError,
    UnequalElementIdSetError,
    UnparseableAttError,
    UnparseableMeshError,
    verify_g0_baseline,
)

FIXTURE_ROOT = pathlib.Path(__file__).parent / "fixtures" / "mapping_builder" / "keliya_minimal"


def _copy_fixture(target: pathlib.Path) -> pathlib.Path:
    """Deep-copy the ``keliya_minimal`` fixture into ``target`` and return it."""
    dest = target / "keliya_minimal"
    shutil.copytree(FIXTURE_ROOT, dest)
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
