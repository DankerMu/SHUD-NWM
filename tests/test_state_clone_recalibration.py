"""Recalibration state carry-over: the eight-surface state-compatibility gate.

Covers the OpenSpec scenarios pinned in
``openspec/changes/recalibration-state-carryover/specs/no-rollback-state-semantics/spec.md``
and ``.../specs/fingerprint-gated-state-clone/spec.md``, plus the change's
evidence mapping §6.1-6.4 and §6.6.

The dual-index CLI end-to-end half (§6.7-6.9) lives in
:file:`tests/test_state_clone_recalibration_cli.py`; the fixtures, fakes and the
independent fingerprint oracle both halves share live in
:file:`tests/state_clone_recalibration_fixtures.py`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from packages.common.state_clone import (
    STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
    fingerprint_gated_state_clone,
)
from packages.common.state_manager import (
    _state_index_entry_from_snapshot,
    _state_snapshot_from_index_entry,
)
from scripts.node22_clone_direct_grid_cutover_states import (
    _state_compatibility_category_files,
)
from tests.state_clone_recalibration_fixtures import (
    _CALIB_TABLE_V1,
    _CALIB_V1,
    _IC_V1,
    _IC_V2,
    _LAKE_RELATIVE_PATH,
    _PARA_V1,
    BASIN,
    CUTOVER_VALID_TIME,
    M1_MODEL_ID,
    M1P_MODEL_ID,
    M1P_PACKAGE_CHECKSUM,
    M1P_PACKAGE_URI,
    SOURCE_ID,
    _build_pair,
    _direct_grid_manifest,
    _expected_file_category_hash,
    _expected_sp_att_non_forc_hash,
    _expected_state_compatibility_fingerprint,
    _FakeAuditRecorder,
    _FakeCloneRepository,
    _m1_source_snapshot,
    _recalibration_kwargs,
    _run_recalibration_clone,
    _sha256,
    _sp_att_rows,
    _write_package,
)
from workers.mapping_builder.rewrite import STATE_COMPATIBILITY_SURFACES

# --- §6.1 Run manifest / QC provenance: the admit path (row 1) --------------


def test_calibration_only_update_carries_state_over(tmp_path: Path) -> None:
    """Only cfg.calib + CALIB/* + cfg.para differ -> clone admitted."""

    pair = _build_pair(tmp_path)
    source = _m1_source_snapshot()

    result, repository, audit = _run_recalibration_clone(pair)

    assert result.refused is False
    assert result.refusal_code is None
    assert result.refusal_scope is None
    assert audit.records == []
    assert result.cloned_row is not None
    clone = result.cloned_row

    # The gate that admitted it, named on the row.
    assert clone.clone_gate_kind == "state_compatibility"

    # The accepted fingerprint equals the INDEPENDENTLY computed 8-surface hash.
    expected = _expected_state_compatibility_fingerprint(
        pair["source_root"],
        category_files=pair["category_files"],
        ic_bytes=_IC_V1,
    )
    assert clone.clone_gate_fingerprint == expected

    # M1' identity on the row; M1 named as the origin.
    assert clone.model_id == M1P_MODEL_ID
    assert clone.model_package_version == M1P_PACKAGE_URI
    assert clone.model_package_checksum == M1P_PACKAGE_CHECKSUM
    assert clone.cloned_from_model_id == M1_MODEL_ID
    assert clone.cloned_from_state_id == source.state_id

    # Physical state reused verbatim; no file copied.
    assert clone.state_uri == source.state_uri
    assert clone.checksum == source.checksum
    assert clone.run_id == source.run_id
    assert clone.lead_hours == source.lead_hours
    assert clone.cycle_id == source.cycle_id
    assert clone.usable_flag is True
    assert repository.upserted == [clone]


def test_state_compatibility_fingerprint_is_not_the_ten_surface_fingerprint(
    tmp_path: Path,
) -> None:
    """The subgate really restricted the surface set.

    If ``surfaces`` were ignored the clone would have accepted the ten-surface
    value, which for a calibration-only update is unequal between the two
    packages and would have refused. Assert positively that the accepted value
    is the eight-surface one and differs from the ten-surface one for the SAME
    package -- a different line set is a different hash by construction.
    """

    pair = _build_pair(tmp_path)
    result, _repository, _audit = _run_recalibration_clone(pair)
    assert result.cloned_row is not None

    eight_surface = _expected_state_compatibility_fingerprint(
        pair["source_root"],
        category_files=pair["category_files"],
        ic_bytes=_IC_V1,
    )
    calibration_paths = (f"{BASIN}.cfg.calib", "CALIB/table.csv")
    ten_surface_lines = {
        label: _expected_file_category_hash(pair["source_root"], paths)
        for label, paths in pair["category_files"].items()
    }
    ten_surface_lines["calibration"] = _expected_file_category_hash(
        pair["source_root"], calibration_paths
    )
    ten_surface_lines["solver_config"] = _sha256(_PARA_V1)
    ten_surface_lines["sp_att_non_forc"] = _expected_sp_att_non_forc_hash(_sp_att_rows())
    ten_surface_lines["state_schema"] = _sha256(_IC_V1)
    ten_surface = _sha256(
        "".join(
            f"{label}\t{ten_surface_lines[label]}\n" for label in sorted(ten_surface_lines)
        ).encode("utf-8")
    )

    assert result.cloned_row.clone_gate_fingerprint == eight_surface
    assert eight_surface != ten_surface


def test_recalibration_never_enumerates_calibration_files(tmp_path: Path) -> None:
    """8-surface mode neither requires nor accepts the ``calibration`` category."""

    pair = _build_pair(tmp_path)
    assert "calibration" not in pair["category_files"]
    assert set(pair["category_files"]) == {
        "geol",
        "lake",
        "land",
        "mesh",
        "river",
        "soil",
    }


# --- §6.2 SHUD runtime / restart compatibility: per-side cfg.ic (row 2) -----


def test_new_cfg_ic_refuses_the_carry_over(tmp_path: Path) -> None:
    """M1' ships a different cfg.ic -> refused, no row written.

    This is the D3 false pass. A single-shared-bytes implementation feeds ONE
    side's ``state_schema_bytes`` to BOTH sides of the equality gate, which
    makes the ``state_schema`` surface compare equal and admits the clone. With
    per-side bytes the surface differs and the clone refuses.
    """

    pair = _build_pair(tmp_path, target_ic=_IC_V2)

    result, repository, audit = _run_recalibration_clone(pair)

    assert result.refused is True
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "state_compatibility_unequal"
    assert result.cloned_row is None
    assert repository.upserted == []
    assert [record["refusal_scope"] for record in audit.records] == [
        "state_compatibility_unequal"
    ]


def test_shared_gate_bytes_would_false_pass_the_new_cfg_ic(tmp_path: Path) -> None:
    """Pins WHY the per-side override exists: without it the same pair passes.

    Same fixture as the test above, but the ``m0_*`` overrides are omitted --
    the pre-change behavior, in which the target's bytes stand in for both
    sides. The clone is then admitted despite M1' shipping a new cfg.ic. This
    test documents the false pass; the test above is the contract.
    """

    pair = _build_pair(tmp_path, target_ic=_IC_V2)

    result, _repository, _audit = _run_recalibration_clone(
        pair,
        m0_state_schema_bytes=None,
        m0_solver_config_bytes=None,
    )

    assert result.refused is False


# --- §6.3 Geospatial / basin geometry: the remaining surfaces (row 3) -------


@pytest.mark.parametrize(
    ("surface", "relative_path"),
    [
        ("mesh", f"{BASIN}.sp.mesh"),
        ("river", f"{BASIN}.sp.riv"),
        ("river", f"{BASIN}.sp.rivseg"),
        ("soil", f"{BASIN}.para.soil"),
        ("geol", f"{BASIN}.para.geol"),
        ("land", f"{BASIN}.para.lc"),
    ],
)
def test_geometry_surface_drift_refuses(
    tmp_path: Path, surface: str, relative_path: str
) -> None:
    """Any of mesh/river/soil/geol/land drifting refuses the carry-over."""

    pair = _build_pair(
        tmp_path,
        target_core_overrides={relative_path: b"drifted-payload-v2\n"},
    )

    result, repository, audit = _run_recalibration_clone(pair)

    assert result.refused is True, surface
    assert result.refusal_scope == "state_compatibility_unequal"
    assert result.cloned_row is None
    assert repository.upserted == []
    assert audit.records[0]["refusal_scope"] == "state_compatibility_unequal"


def test_sp_att_non_forc_drift_refuses(tmp_path: Path) -> None:
    """A non-``FORC`` ``.sp.att`` field drifting refuses the carry-over."""

    pair = _build_pair(tmp_path, target_sp_att_rows=_sp_att_rows(soil=7))

    result, _repository, _audit = _run_recalibration_clone(pair)

    assert result.refused is True
    assert result.refusal_scope == "state_compatibility_unequal"


def test_sp_att_forc_only_drift_still_carries_over(tmp_path: Path) -> None:
    """FORC is outside both surface sets: a FORC rewrite alone still admits."""

    pair = _build_pair(tmp_path, target_sp_att_rows=_sp_att_rows(forc_values=(4, 3, 2, 1)))

    result, _repository, _audit = _run_recalibration_clone(pair)

    assert result.refused is False


# --- §6.4 Lake union enumeration (rows 4, 5) -------------------------------


def test_lake_file_added_only_on_target_refuses(tmp_path: Path) -> None:
    """A ``*.lake.*`` present only in M1' refuses (union enumeration)."""

    pair = _build_pair(tmp_path, source_lake=False, target_lake=True)
    assert pair["category_files"]["lake"] == (_LAKE_RELATIVE_PATH,)

    result, repository, audit = _run_recalibration_clone(pair)

    assert result.refused is True
    assert result.refusal_scope == "state_compatibility_unequal"
    assert repository.upserted == []
    record = audit.records[0]
    assert record["missing_category"] == "lake"
    assert record["missing_relative_path"] == _LAKE_RELATIVE_PATH
    assert record["missing_side"] == "baseline"


def test_lake_file_removed_in_target_refuses(tmp_path: Path) -> None:
    """A ``*.lake.*`` present only in M1 (REMOVED in M1') refuses.

    This is the case a single-root enumeration silently false-passes: a list
    derived from the M1' root alone would never name the removed file, so no
    surface would notice its disappearance. The union of both roots names it
    and the target side fails to resolve it.
    """

    pair = _build_pair(tmp_path, source_lake=True, target_lake=False)
    assert pair["category_files"]["lake"] == (_LAKE_RELATIVE_PATH,)

    result, repository, audit = _run_recalibration_clone(pair)

    assert result.refused is True
    assert result.refusal_scope == "state_compatibility_unequal"
    assert result.cloned_row is None
    assert repository.upserted == []
    record = audit.records[0]
    assert record["missing_category"] == "lake"
    assert record["missing_relative_path"] == _LAKE_RELATIVE_PATH
    assert record["missing_side"] == "variant"


def test_no_lake_file_on_either_side_uses_the_sp_mesh_placeholder(tmp_path: Path) -> None:
    """Basins without a lake file carry over; the placeholder is auditable.

    The placeholder must NOT be ``cfg.para``: that is exactly the file a
    recalibration changes, so hashing it into the ``lake`` surface would refuse
    the very case this route exists to permit.
    """

    pair = _build_pair(tmp_path, source_lake=False, target_lake=False)

    assert pair["category_files"]["lake"] == (f"{BASIN}.sp.mesh",)
    assert f"{BASIN}.cfg.para" not in pair["category_files"]["lake"]

    result, _repository, _audit = _run_recalibration_clone(pair)

    assert result.refused is False
    assert result.cloned_row is not None
    assert result.cloned_row.clone_gate_fingerprint == (
        _expected_state_compatibility_fingerprint(
            pair["source_root"],
            category_files=pair["category_files"],
            ic_bytes=_IC_V1,
        )
    )


def test_mesh_drift_still_refuses_when_lake_uses_the_placeholder(tmp_path: Path) -> None:
    """The placeholder removes no signal: mesh drift refuses via the mesh face."""

    pair = _build_pair(
        tmp_path,
        source_lake=False,
        target_lake=False,
        target_core_overrides={f"{BASIN}.sp.mesh": b"drifted-mesh\n"},
    )

    result, _repository, _audit = _run_recalibration_clone(pair)

    assert result.refused is True
    assert result.refusal_scope == "state_compatibility_unequal"


# --- Shared pre-gate checks are unconditional in both modes ----------------


def test_recalibration_still_refuses_a_legacy_target(tmp_path: Path) -> None:
    """The no-reverse-clone guard runs first in recalibration mode too."""

    pair = _build_pair(tmp_path)

    result, repository, _audit = _run_recalibration_clone(
        pair, m1_forcing_mapping_manifest={}
    )

    assert result.refused is True
    assert result.refusal_scope == "reverse_clone_target_not_direct_grid"
    assert repository.upserted == []


@pytest.mark.parametrize(
    "override",
    [
        {"state_schema_bytes": b""},
        {"solver_config_bytes": b""},
        {"m0_state_schema_bytes": b""},
        {"m0_solver_config_bytes": b""},
    ],
)
def test_recalibration_refuses_degenerate_gate_inputs(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    """Empty gate bytes -- required OR per-side override -- refuse fail-closed."""

    pair = _build_pair(tmp_path)

    result, repository, _audit = _run_recalibration_clone(pair, **override)

    assert result.refused is True
    assert result.refusal_scope == "degenerate_gate_inputs"
    assert repository.upserted == []


def test_recalibration_refuses_a_missing_qualified_source(tmp_path: Path) -> None:
    """No ``(M1, gfs, t*)`` row -> the existing missing-source refusal."""

    pair = _build_pair(tmp_path)
    audit = _FakeAuditRecorder()
    repository = _FakeCloneRepository()

    result = fingerprint_gated_state_clone(
        repository=repository,
        audit_recorder=audit,
        **_recalibration_kwargs(pair),
    )

    assert result.refused is True
    assert result.refusal_scope == "missing_qualified_source"


def test_recalibration_cross_checks_a_supplied_recorded_fingerprint(
    tmp_path: Path,
) -> None:
    """A recalibration caller that DOES supply a recorded value is cross-checked."""

    pair = _build_pair(tmp_path)

    result, _repository, _audit = _run_recalibration_clone(
        pair, m1_recorded_hydrologic_core_fingerprint="sha256:not-the-computed-value"
    )

    assert result.refused is True
    assert result.refusal_scope == "evidence_fingerprint_mismatch"

    expected = _expected_state_compatibility_fingerprint(
        pair["source_root"],
        category_files=pair["category_files"],
        ic_bytes=_IC_V1,
    )
    matched, _repository, _audit = _run_recalibration_clone(
        pair, m1_recorded_hydrologic_core_fingerprint=expected
    )
    assert matched.refused is False


# --- §6.6 clone_gate_kind round-trips on the file plane (row 8) -------------


def test_clone_gate_kind_round_trips_through_the_state_index_entry(
    tmp_path: Path,
) -> None:
    """The index entry carries the kind; an older entry without it reads None."""

    pair = _build_pair(tmp_path)
    result, _repository, _audit = _run_recalibration_clone(pair)
    assert result.cloned_row is not None

    entry = _state_index_entry_from_snapshot(result.cloned_row)
    assert entry["clone_gate_kind"] == "state_compatibility"
    assert _state_snapshot_from_index_entry(entry).clone_gate_kind == "state_compatibility"

    legacy_entry = dict(entry)
    legacy_entry.pop("clone_gate_kind")
    rehydrated = _state_snapshot_from_index_entry(legacy_entry)
    assert rehydrated.clone_gate_kind is None
    # Every other field survives the older-entry read unchanged.
    assert rehydrated.clone_gate_fingerprint == result.cloned_row.clone_gate_fingerprint
    assert rehydrated.cloned_from_model_id == M1_MODEL_ID


def test_non_clone_snapshot_carries_no_clone_gate_kind() -> None:
    """An ordinary forecast save-state row keeps ``clone_gate_kind`` NULL."""

    snapshot = _m1_source_snapshot()
    assert snapshot.clone_gate_kind is None
    assert _state_index_entry_from_snapshot(snapshot)["clone_gate_kind"] is None


# --- Fix-forward contract does not weaken (§6.5 / row 7) -------------------


def test_fix_forward_without_a_recorded_evidence_value_refuses(tmp_path: Path) -> None:
    """``fix_forward`` + ``None`` recorded fingerprint -> evidence_fingerprint_mismatch.

    The recalibration waiver is scoped to its own mode: omitting the recorded
    value on the default route still refuses, so the fix-forward cross-check
    obligation cannot be waived by simply not passing the argument.
    """

    # Two byte-equal packages so the ten-surface gate itself would pass.
    source_root = _write_package(
        tmp_path / "m0",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    target_root = _write_package(
        tmp_path / "m1",
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    category_files = dict(_state_compatibility_category_files(source_root, target_root))
    category_files["calibration"] = (f"{BASIN}.cfg.calib", "CALIB/table.csv")

    repository = _FakeCloneRepository()
    repository.add(_m1_source_snapshot())
    audit = _FakeAuditRecorder()

    common: dict[str, Any] = {
        "m0_model_id": M1_MODEL_ID,
        "m1_model_id": M1P_MODEL_ID,
        "m1_model_package_version": M1P_PACKAGE_URI,
        "m1_model_package_checksum": M1P_PACKAGE_CHECKSUM,
        "source_id": SOURCE_ID,
        "cutover_valid_time": CUTOVER_VALID_TIME,
        "m0_package_root": source_root,
        "m1_package_root": target_root,
        "m0_sp_att_path": source_root / f"{BASIN}.sp.att",
        "m1_sp_att_path": target_root / f"{BASIN}.sp.att",
        "m1_category_files": category_files,
        "state_schema_bytes": _IC_V1,
        "solver_config_bytes": _PARA_V1,
        "m1_forcing_mapping_manifest": _direct_grid_manifest(M1P_MODEL_ID),
        "repository": repository,
        "audit_recorder": audit,
    }

    for absent in (None, ""):
        result = fingerprint_gated_state_clone(
            m1_recorded_hydrologic_core_fingerprint=absent, **common
        )
        assert result.refused is True, absent
        assert result.refusal_scope == "evidence_fingerprint_mismatch", absent
        assert repository.upserted == []


def test_fix_forward_default_records_the_hydrologic_core_gate_kind(
    tmp_path: Path,
) -> None:
    """§6.5 / row 6: the default route names the ten-surface gate on the row."""

    source_root = _write_package(
        tmp_path / "m0",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    target_root = _write_package(
        tmp_path / "m1",
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    category_files = dict(_state_compatibility_category_files(source_root, target_root))
    category_files["calibration"] = (f"{BASIN}.cfg.calib", "CALIB/table.csv")

    # The ten-surface value, recomputed independently.
    per_surface = {
        label: _expected_file_category_hash(source_root, paths)
        for label, paths in category_files.items()
    }
    per_surface["sp_att_non_forc"] = _expected_sp_att_non_forc_hash(_sp_att_rows())
    per_surface["state_schema"] = _sha256(_IC_V1)
    per_surface["solver_config"] = _sha256(_PARA_V1)
    expected_ten = _sha256(
        "".join(f"{label}\t{per_surface[label]}\n" for label in sorted(per_surface)).encode(
            "utf-8"
        )
    )

    repository = _FakeCloneRepository()
    repository.add(_m1_source_snapshot())
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        m0_model_id=M1_MODEL_ID,
        m1_model_id=M1P_MODEL_ID,
        m1_model_package_version=M1P_PACKAGE_URI,
        m1_model_package_checksum=M1P_PACKAGE_CHECKSUM,
        source_id=SOURCE_ID,
        cutover_valid_time=CUTOVER_VALID_TIME,
        m0_package_root=source_root,
        m1_package_root=target_root,
        m0_sp_att_path=source_root / f"{BASIN}.sp.att",
        m1_sp_att_path=target_root / f"{BASIN}.sp.att",
        m1_category_files=category_files,
        m1_recorded_hydrologic_core_fingerprint=expected_ten,
        state_schema_bytes=_IC_V1,
        solver_config_bytes=_PARA_V1,
        m1_forcing_mapping_manifest=_direct_grid_manifest(M1P_MODEL_ID),
        repository=repository,
        audit_recorder=audit,
    )

    assert result.refused is False
    assert result.cloned_row is not None
    assert result.cloned_row.clone_gate_kind == "hydrologic_core"
    assert result.cloned_row.clone_gate_fingerprint == expected_ten


def test_fix_forward_propagates_a_missing_package_file(tmp_path: Path) -> None:
    """Only recalibration maps MissingPackageFileError into a refusal scope."""

    from workers.mapping_builder.rewrite import MissingPackageFileError

    source_root = _write_package(
        tmp_path / "m0",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    target_root = _write_package(
        tmp_path / "m1",
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
        lake=False,
    )
    category_files = dict(_state_compatibility_category_files(source_root, target_root))
    category_files["calibration"] = (f"{BASIN}.cfg.calib", "CALIB/table.csv")

    repository = _FakeCloneRepository()
    repository.add(_m1_source_snapshot())

    with pytest.raises(MissingPackageFileError):
        fingerprint_gated_state_clone(
            m0_model_id=M1_MODEL_ID,
            m1_model_id=M1P_MODEL_ID,
            m1_model_package_version=M1P_PACKAGE_URI,
            m1_model_package_checksum=M1P_PACKAGE_CHECKSUM,
            source_id=SOURCE_ID,
            cutover_valid_time=CUTOVER_VALID_TIME,
            m0_package_root=source_root,
            m1_package_root=target_root,
            m0_sp_att_path=source_root / f"{BASIN}.sp.att",
            m1_sp_att_path=target_root / f"{BASIN}.sp.att",
            m1_category_files=category_files,
            m1_recorded_hydrologic_core_fingerprint="sha256:whatever",
            state_schema_bytes=_IC_V1,
            solver_config_bytes=_PARA_V1,
            m1_forcing_mapping_manifest=_direct_grid_manifest(M1P_MODEL_ID),
            repository=repository,
            audit_recorder=_FakeAuditRecorder(),
        )
    assert repository.upserted == []


# --- Surface-set validation (§1.2) -----------------------------------------


def test_state_compatibility_surfaces_is_the_ten_labels_minus_two() -> None:
    from workers.mapping_builder.rewrite import HYDROLOGIC_CORE_FINGERPRINT_LABELS

    assert len(HYDROLOGIC_CORE_FINGERPRINT_LABELS) == 10
    assert STATE_COMPATIBILITY_SURFACES == (
        "geol",
        "lake",
        "land",
        "mesh",
        "river",
        "soil",
        "sp_att_non_forc",
        "state_schema",
    )
    assert set(HYDROLOGIC_CORE_FINGERPRINT_LABELS) - set(STATE_COMPATIBILITY_SURFACES) == {
        "calibration",
        "solver_config",
    }


@pytest.mark.parametrize(
    "surfaces",
    [(), ("mesh", "mesh"), ("mesh", "not_a_surface")],
)
def test_invalid_surface_set_is_refused(tmp_path: Path, surfaces: tuple[str, ...]) -> None:
    from workers.mapping_builder.rewrite import (
        SpAttRewriteError,
        compute_hydrologic_core_fingerprint,
    )

    root = _write_package(
        tmp_path / "pkg",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    with pytest.raises(SpAttRewriteError):
        compute_hydrologic_core_fingerprint(
            root,
            sp_att_path=root / f"{BASIN}.sp.att",
            category_files={"mesh": (f"{BASIN}.sp.mesh",)},
            state_schema_bytes=_IC_V1,
            solver_config_bytes=_PARA_V1,
            surfaces=surfaces,
        )


def test_surface_order_does_not_change_the_fingerprint(tmp_path: Path) -> None:
    """The domain separation is alphabetical regardless of argument order."""

    from workers.mapping_builder.rewrite import compute_hydrologic_core_fingerprint

    root = _write_package(
        tmp_path / "pkg",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
    )
    category_files = {
        "geol": (f"{BASIN}.para.geol",),
        "lake": (_LAKE_RELATIVE_PATH,),
        "land": (f"{BASIN}.para.lc",),
        "mesh": (f"{BASIN}.sp.mesh",),
        "river": (f"{BASIN}.sp.riv", f"{BASIN}.sp.rivseg"),
        "soil": (f"{BASIN}.para.soil",),
    }
    kwargs: dict[str, Any] = {
        "sp_att_path": root / f"{BASIN}.sp.att",
        "category_files": category_files,
        "state_schema_bytes": _IC_V1,
        "solver_config_bytes": _PARA_V1,
    }
    forward = compute_hydrologic_core_fingerprint(
        root, surfaces=STATE_COMPATIBILITY_SURFACES, **kwargs
    )
    reversed_order = compute_hydrologic_core_fingerprint(
        root, surfaces=tuple(reversed(STATE_COMPATIBILITY_SURFACES)), **kwargs
    )
    assert forward.hash == reversed_order.hash
    assert forward.hash == _expected_state_compatibility_fingerprint(
        root, category_files=category_files, ic_bytes=_IC_V1
    )
    assert len(forward.covered_paths) == 8
    assert not any(entry.startswith("calibration:") for entry in forward.covered_paths)
    assert not any(entry.startswith("solver_config:") for entry in forward.covered_paths)


def test_replace_on_state_snapshot_preserves_clone_gate_kind() -> None:
    """`dataclasses.replace` round-trip keeps the new field (no positional drift)."""

    snapshot = replace(_m1_source_snapshot(), clone_gate_kind="state_compatibility")
    assert replace(snapshot, usable_flag=False).clone_gate_kind == "state_compatibility"
