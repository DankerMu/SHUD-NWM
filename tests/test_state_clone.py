"""Tests for the fingerprint-gated state clone (Epic #982 SUB-2 §2.1).

Covers the OpenSpec ``fingerprint-gated-state-clone`` scenarios pinned in
``openspec/changes/mapping-variant-state-compatibility/specs/fingerprint-gated-state-clone/spec.md``:

- Equal-fingerprint clone succeeds with the pinned column disposition and
  no physical file copy.
- Unequal-fingerprint refusal, missing-source refusal, and stale-latest
  refusal all fail closed with no ``(M1, source, t*)`` row and the stable
  code ``state_clone_cold_start_approval_required``.
- Degenerate ``state_schema_bytes`` / ``solver_config_bytes`` refuse
  fail-closed (no false-pass on symmetric-empty inputs).
- Recomputed-vs-evidence fingerprint mismatch refuses fail-closed.
- Integration: the cloned ``(M1, source, t*)`` row is accepted by the
  EXISTING strict warm-start selection on BOTH planes — the DB-path
  validator (``services/orchestrator/chain_forecast_state``) and the
  file-state-index path
  (``packages/common/state_manager.FileStateSnapshotIndexRepository.strict_warm_start_evidence``)
  — with zero modification to the validator.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.state_clone import (
    STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
    StateCloneResult,
    fingerprint_gated_state_clone,
)
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateSnapshot,
    _snapshot_from_row,
    _snapshot_to_dict,
    _state_index_entry_from_snapshot,
    _state_snapshot_from_index_entry,
    publish_state_snapshot_index,
    state_snapshot_id,
)
from services.orchestrator import chain_forecast_state
from workers.data_adapters.base import cycle_id_for
from workers.mapping_builder.rewrite import (
    compute_hydrologic_core_fingerprint,
)

# --- Package + .sp.att builders (mirror test_mapping_builder_rewrite) ------


_SP_ATT_SCHEMA = ("INDEX", "SOIL", "GEOL", "LC", "FORC", "MF", "BC", "SS", "LAKE")

_DEFAULT_NON_SP_ATT_STUBS: dict[str, tuple[str, bytes]] = {
    "calibration": ("basin.calib", b"calibration-payload-v1\n"),
    "geol": ("basin.geol", b"geol-payload-v1\n"),
    "lake": ("basin.lake", b"lake-payload-v1\n"),
    "land": ("basin.land", b"land-payload-v1\n"),
    "mesh": ("basin.sp.mesh", b"mesh-payload-v1\n"),
    "river": ("basin.riv", b"river-payload-v1\n"),
    "soil": ("basin.soil", b"soil-payload-v1\n"),
}

DEFAULT_STATE_SCHEMA_BYTES = b"state_schema:v1\nfields=[soil_moisture,swe,gw]\n"
DEFAULT_SOLVER_CONFIG_BYTES = b"solver_config:v1\ndt=3600\ntol=1e-6\n"


def _default_category_files() -> dict[str, tuple[str, ...]]:
    return {c: (f,) for c, (f, _p) in _DEFAULT_NON_SP_ATT_STUBS.items()}


def _write_package(
    root: Path,
    *,
    stubs: Mapping[str, tuple[str, bytes]] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payloads = stubs if stubs is not None else _DEFAULT_NON_SP_ATT_STUBS
    for _category, (filename, payload) in payloads.items():
        (root / filename).write_bytes(payload)
    return root


def _write_sp_att(
    path: Path,
    *,
    forc_values: Sequence[int] = (1, 2, 3, 4),
    non_forc_soil: int = 1,
) -> Path:
    """Write a 4-row ``.sp.att`` file. FORC drift never breaks fingerprint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [(i + 1, non_forc_soil, 1, 11, forc_values[i], 1, 0, 0, 0) for i in range(4)]
    lines = [f"{len(rows)}\t{len(_SP_ATT_SCHEMA)}", "\t".join(_SP_ATT_SCHEMA)]
    for row in rows:
        lines.append("\t".join(str(v) for v in row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def m0_m1_equal_packages(tmp_path: Path) -> dict[str, Any]:
    """Two byte-equal packages so ``verify_...equal`` returns a shared hash."""
    m0_root = _write_package(tmp_path / "m0")
    m1_root = _write_package(tmp_path / "m1")
    m0_sp_att = _write_sp_att(m0_root / "basin.sp.att", forc_values=(1, 2, 3, 4))
    # FORC drift only — non-FORC columns identical => fingerprint equal.
    m1_sp_att = _write_sp_att(m1_root / "basin.sp.att", forc_values=(4, 3, 2, 1))
    fp = compute_hydrologic_core_fingerprint(
        m0_root,
        sp_att_path=m0_sp_att,
        category_files=_default_category_files(),
        state_schema_bytes=DEFAULT_STATE_SCHEMA_BYTES,
        solver_config_bytes=DEFAULT_SOLVER_CONFIG_BYTES,
    )
    return {
        "m0_root": m0_root,
        "m1_root": m1_root,
        "m0_sp_att": m0_sp_att,
        "m1_sp_att": m1_sp_att,
        "category_files": _default_category_files(),
        "fingerprint_hash": fp.hash,
    }


@pytest.fixture
def m0_m1_unequal_packages(tmp_path: Path) -> dict[str, Any]:
    """Two packages differing on ONE non-FORC surface — fingerprint drifts."""
    drifted = dict(_DEFAULT_NON_SP_ATT_STUBS)
    drifted["soil"] = ("basin.soil", b"soil-payload-v2-drifted\n")
    m0_root = _write_package(tmp_path / "m0")
    m1_root = _write_package(tmp_path / "m1", stubs=drifted)
    m0_sp_att = _write_sp_att(m0_root / "basin.sp.att")
    m1_sp_att = _write_sp_att(m1_root / "basin.sp.att")
    m0_fp = compute_hydrologic_core_fingerprint(
        m0_root,
        sp_att_path=m0_sp_att,
        category_files=_default_category_files(),
        state_schema_bytes=DEFAULT_STATE_SCHEMA_BYTES,
        solver_config_bytes=DEFAULT_SOLVER_CONFIG_BYTES,
    )
    return {
        "m0_root": m0_root,
        "m1_root": m1_root,
        "m0_sp_att": m0_sp_att,
        "m1_sp_att": m1_sp_att,
        "category_files": _default_category_files(),
        # Evidence would carry M1's OWN fingerprint; but for tests that
        # exercise the unequal-fingerprint refusal we only need *some*
        # value — the guard rejects before the evidence cross-check runs.
        "fingerprint_hash": m0_fp.hash,
    }


# --- Fake repository + audit recorder --------------------------------------


class _FakeCloneRepository:
    """In-memory clone repository backing.

    Tracks snapshots keyed by ``(model_id, source_id, valid_time)`` — the
    same unique key the DB migration ``000028`` locked in.
    Distinguishes the missing vs stale refusal paths via
    ``get_latest_state_before`` (source-scoped).
    """

    def __init__(self) -> None:
        self.snapshots: dict[str, StateSnapshot] = {}
        self.upserted: list[StateSnapshot] = []

    def add(self, snapshot: StateSnapshot) -> None:
        self.snapshots[snapshot.state_id] = snapshot

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        for snapshot in self.snapshots.values():
            if snapshot.model_id != model_id:
                continue
            if snapshot.source_id != source_id:
                continue
            if _ensure_utc(snapshot.valid_time) != _ensure_utc(valid_time):
                continue
            if lead_hours is not None and snapshot.lead_hours != lead_hours:
                continue
            if cycle_id is not None and snapshot.cycle_id != cycle_id:
                continue
            return snapshot
        return None

    def get_latest_state_before(
        self,
        *,
        model_id: str,
        source_id: str,
        before_time: datetime,
    ) -> StateSnapshot | None:
        candidates = [
            snapshot
            for snapshot in self.snapshots.values()
            if snapshot.model_id == model_id
            and snapshot.source_id == source_id
            and _ensure_utc(snapshot.valid_time) < _ensure_utc(before_time)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda snapshot: snapshot.valid_time)

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.upserted.append(snapshot)
        stored = replace(
            snapshot,
            created_at=snapshot.created_at or _dt("2026-07-01T12:00:00Z"),
        )
        self.snapshots[stored.state_id] = stored
        return stored


class _FakeAuditRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


# --- Test constants --------------------------------------------------------


M0_MODEL_ID = "basin_v1_m0"
M1_MODEL_ID = "basin_v1_m1"
M1_PACKAGE_VERSION = "s3://nhms/models/basin_v1_m1/package/"
M1_PACKAGE_CHECKSUM = "sha256:pkg-m1"
M0_PACKAGE_VERSION = "s3://nhms/models/basin_v1_m0/package/"
M0_PACKAGE_CHECKSUM = "sha256:pkg-m0"
SOURCE_ID = "gfs"
CUTOVER_VALID_TIME = datetime(2026, 6, 15, 6, tzinfo=UTC)  # t*
PRODUCER_CYCLE_TIME = CUTOVER_VALID_TIME - timedelta(hours=12)
CYCLE_ID = cycle_id_for(SOURCE_ID, PRODUCER_CYCLE_TIME)


def _make_source_snapshot(
    *,
    valid_time: datetime = CUTOVER_VALID_TIME,
    usable_flag: bool = True,
    lead_hours: int = 12,
    checksum: str = "sha256:state-payload",
    cycle_id_override: str | None = None,
) -> StateSnapshot:
    cycle = cycle_id_override or cycle_id_for(SOURCE_ID, valid_time - timedelta(hours=lead_hours))
    return StateSnapshot(
        state_id=state_snapshot_id(
            M0_MODEL_ID,
            valid_time,
            source_id=SOURCE_ID,
            cycle_id=cycle,
            lead_hours=lead_hours,
        ),
        model_id=M0_MODEL_ID,
        run_id=f"fcst_{SOURCE_ID}_{cycle}_{M0_MODEL_ID}",
        valid_time=valid_time,
        state_uri=f"states/{SOURCE_ID}/{M0_MODEL_ID}/2026061506/state.cfg.ic",
        checksum=checksum,
        usable_flag=usable_flag,
        source_id=SOURCE_ID,
        cycle_id=cycle,
        lead_hours=lead_hours,
        model_package_version=M0_PACKAGE_VERSION,
        model_package_checksum=M0_PACKAGE_CHECKSUM,
        original_shud_filename="run.cfg.ic",
    )


def _default_clone_kwargs(pkg: dict[str, Any]) -> dict[str, Any]:
    return {
        "m0_model_id": M0_MODEL_ID,
        "m1_model_id": M1_MODEL_ID,
        "m1_model_package_version": M1_PACKAGE_VERSION,
        "m1_model_package_checksum": M1_PACKAGE_CHECKSUM,
        "source_id": SOURCE_ID,
        "cutover_valid_time": CUTOVER_VALID_TIME,
        "m0_package_root": pkg["m0_root"],
        "m1_package_root": pkg["m1_root"],
        "m0_sp_att_path": pkg["m0_sp_att"],
        "m1_sp_att_path": pkg["m1_sp_att"],
        "m1_category_files": pkg["category_files"],
        "m1_recorded_hydrologic_core_fingerprint": pkg["fingerprint_hash"],
        "state_schema_bytes": DEFAULT_STATE_SCHEMA_BYTES,
        "solver_config_bytes": DEFAULT_SOLVER_CONFIG_BYTES,
    }


# --- (a) equal-fingerprint clone succeeds ----------------------------------


def test_equal_fingerprint_clone_writes_row_with_pinned_column_disposition(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Green path: equal fingerprints → clone row written with pinned columns."""

    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert isinstance(result, StateCloneResult)
    assert result.refused is False
    assert result.refusal_code is None
    assert result.refusal_scope is None
    assert audit.records == []
    assert result.cloned_row is not None
    clone = result.cloned_row

    # Preserved verbatim from the source row.
    assert clone.state_uri == source.state_uri
    assert clone.checksum == source.checksum
    assert clone.source_id == source.source_id
    assert _ensure_utc(clone.valid_time) == _ensure_utc(source.valid_time)
    assert clone.cycle_id == source.cycle_id
    assert clone.lead_hours == source.lead_hours
    assert clone.usable_flag is True
    assert clone.original_shud_filename == source.original_shud_filename
    # Physical provenance: M0 run_id preserved (docs §Decision 3).
    assert clone.run_id == source.run_id

    # Overwritten to the M1 target identity.
    assert clone.model_id == M1_MODEL_ID
    assert clone.model_package_version == M1_PACKAGE_VERSION
    assert clone.model_package_checksum == M1_PACKAGE_CHECKSUM

    # state_id is minted via the convention under the M1 identity and is
    # distinct from the source row's state_id (docs §Decision 2 identity).
    assert clone.state_id != source.state_id
    assert clone.state_id == state_snapshot_id(
        M1_MODEL_ID,
        source.valid_time,
        source_id=source.source_id,
        cycle_id=source.cycle_id,
        lead_hours=source.lead_hours,
    )


def test_equal_fingerprint_clone_does_not_copy_physical_state_file(
    tmp_path: Path,
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """INV-2: the clone reuses ``state_uri`` verbatim, no file bytes touched.

    Wired defensively by (i) not passing a physical file / object store into
    the function (see its signature) and (ii) placing a real byte-different
    file at the source ``state_uri`` and asserting neither the object exists
    at a putative M1 path nor the source file is rewritten.
    """

    object_root = tmp_path / "objects"
    object_root.mkdir()
    m0_uri_key = "states/gfs/basin_v1_m0/2026061506/state.cfg.ic"
    m1_uri_key = "states/gfs/basin_v1_m1/2026061506/state.cfg.ic"
    (object_root / m0_uri_key).parent.mkdir(parents=True, exist_ok=True)
    (object_root / m0_uri_key).write_bytes(b"m0-physical-state-file")

    source_snapshot = replace(_make_source_snapshot(), state_uri=m0_uri_key)
    repo = _FakeCloneRepository()
    repo.add(source_snapshot)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is False
    assert result.cloned_row is not None
    # Clone points at the SAME on-NFS file — no M1-specific copy exists.
    assert result.cloned_row.state_uri == m0_uri_key
    assert not (object_root / m1_uri_key).exists()
    # Source file bytes are unchanged.
    assert (object_root / m0_uri_key).read_bytes() == b"m0-physical-state-file"


# --- (b) unequal-fingerprint refused ---------------------------------------


def test_unequal_fingerprint_refuses_no_row_written(
    m0_m1_unequal_packages: dict[str, Any],
) -> None:
    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_unequal_packages),
    )

    assert result.refused is True
    assert result.cloned_row is None
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "unequal_fingerprint"
    assert repo.upserted == []
    # Source row still the only row under (M0, source, t*).
    assert len(repo.snapshots) == 1
    assert audit.records == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "unequal_fingerprint",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": SOURCE_ID,
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


# --- (c) missing qualified source ------------------------------------------


def test_missing_qualified_source_refuses_no_row_written(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """No (M0, source, t*) row at all: refuse with missing_qualified_source."""

    repo = _FakeCloneRepository()  # empty
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is True
    assert result.cloned_row is None
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "missing_qualified_source"
    assert repo.upserted == []
    assert audit.records[0]["refusal_scope"] == "missing_qualified_source"


def test_unqualified_row_at_cutover_refuses_missing_scope(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """A row exists at ``t*`` but fails Gate G10 (usable_flag=False) → refuse.

    Also verifies no earlier snapshot exists on the source; therefore the
    scope is missing_qualified_source, NOT stale_latest_snapshot.
    """

    unqualified = _make_source_snapshot(usable_flag=False)
    repo = _FakeCloneRepository()
    repo.add(unqualified)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is True
    assert result.refusal_scope == "missing_qualified_source"
    assert repo.upserted == []
    assert audit.records == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "missing_qualified_source",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": SOURCE_ID,
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


# --- (d) stale latest snapshot ---------------------------------------------


def test_stale_latest_snapshot_refuses_distinct_from_missing_scope(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Latest (M0, source) has valid_time < t*: refuse with stale_latest_snapshot.

    Verifies Gate G10 condition 4 is enforced as a distinct scope from
    missing so the audit record can drive the right operator remediation
    (repair M0 state vs. approve a cold start).
    """

    stale_valid_time = CUTOVER_VALID_TIME - timedelta(hours=12)
    stale = _make_source_snapshot(valid_time=stale_valid_time)
    repo = _FakeCloneRepository()
    repo.add(stale)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is True
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "stale_latest_snapshot"
    assert repo.upserted == []
    # No (M1, source, t*) row written.
    assert not any(
        s.model_id == M1_MODEL_ID and _ensure_utc(s.valid_time) == CUTOVER_VALID_TIME
        for s in repo.snapshots.values()
    )
    assert audit.records[0]["refusal_scope"] == "stale_latest_snapshot"


# --- Gate-input contract: degenerate bytes and evidence cross-check --------


def test_empty_state_schema_bytes_refused_fail_closed(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    kwargs = _default_clone_kwargs(m0_m1_equal_packages)
    kwargs["state_schema_bytes"] = b""

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **kwargs,
    )

    assert result.refused is True
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "degenerate_gate_inputs"
    assert repo.upserted == []
    assert audit.records == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "degenerate_gate_inputs",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": SOURCE_ID,
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


def test_empty_solver_config_bytes_refused_fail_closed(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    kwargs = _default_clone_kwargs(m0_m1_equal_packages)
    kwargs["solver_config_bytes"] = b""

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **kwargs,
    )

    assert result.refused is True
    assert result.refusal_scope == "degenerate_gate_inputs"
    assert repo.upserted == []
    assert audit.records == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "degenerate_gate_inputs",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": SOURCE_ID,
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


def test_recomputed_fingerprint_mismatches_evidence_refuses_fail_closed(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Gate passes but the evidence-recorded hash disagrees → refuse.

    Blocks a caller passing degenerate-but-symmetric inputs that survive
    equality but do not match what the M1 mapping evidence package
    recorded at build time.
    """

    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    kwargs = _default_clone_kwargs(m0_m1_equal_packages)
    kwargs["m1_recorded_hydrologic_core_fingerprint"] = "0" * 64  # not the real hash

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **kwargs,
    )

    assert result.refused is True
    assert result.refusal_code == STATE_CLONE_COLD_START_APPROVAL_REQUIRED
    assert result.refusal_scope == "evidence_fingerprint_mismatch"
    assert repo.upserted == []
    assert audit.records == [
        {
            "refusal_code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
            "refusal_scope": "evidence_fingerprint_mismatch",
            "m0_model_id": M0_MODEL_ID,
            "m1_model_id": M1_MODEL_ID,
            "source_id": SOURCE_ID,
            "cutover_valid_time": CUTOVER_VALID_TIME,
        }
    ]


def test_clone_scope_is_single_source_only(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Per-source scope: cloning for source_A must not touch source_B rows.

    spec.md pins "The clone executes per source across the activation source
    scope" — the SUB-2 function takes ONE source_id and the caller (SUB-4)
    iterates. Seed two qualified `(M0, source, t*)` snapshots differing only
    on source_id; invoke the clone with source_A; assert only the source_A
    (M1, source_A, t*) row appears and source_B remains a single (M0) row
    with no (M1) row created and no repository upsert touching source_B.
    """

    source_a = "gfs"
    source_b = "ifs"
    source_a_snapshot = replace(
        _make_source_snapshot(),
        state_id=state_snapshot_id(
            M0_MODEL_ID,
            CUTOVER_VALID_TIME,
            source_id=source_a,
            cycle_id=cycle_id_for(source_a, CUTOVER_VALID_TIME - timedelta(hours=12)),
            lead_hours=12,
        ),
        source_id=source_a,
        cycle_id=cycle_id_for(source_a, CUTOVER_VALID_TIME - timedelta(hours=12)),
    )
    source_b_snapshot = replace(
        _make_source_snapshot(),
        state_id=state_snapshot_id(
            M0_MODEL_ID,
            CUTOVER_VALID_TIME,
            source_id=source_b,
            cycle_id=cycle_id_for(source_b, CUTOVER_VALID_TIME - timedelta(hours=12)),
            lead_hours=12,
        ),
        source_id=source_b,
        cycle_id=cycle_id_for(source_b, CUTOVER_VALID_TIME - timedelta(hours=12)),
    )
    repo = _FakeCloneRepository()
    repo.add(source_a_snapshot)
    repo.add(source_b_snapshot)
    audit = _FakeAuditRecorder()

    kwargs = _default_clone_kwargs(m0_m1_equal_packages)
    kwargs["source_id"] = source_a

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **kwargs,
    )

    assert result.refused is False
    assert result.cloned_row is not None
    assert result.cloned_row.source_id == source_a

    # Exactly one upsert, and it targets source_a.
    assert len(repo.upserted) == 1
    assert repo.upserted[0].source_id == source_a
    assert repo.upserted[0].model_id == M1_MODEL_ID

    # source_b remains a single M0 row with no (M1) sibling.
    source_b_rows = [s for s in repo.snapshots.values() if s.source_id == source_b]
    assert len(source_b_rows) == 1
    assert source_b_rows[0].model_id == M0_MODEL_ID
    assert not any(
        s.source_id == source_b and s.model_id == M1_MODEL_ID
        for s in repo.snapshots.values()
    )

    # SUB-3 provenance columns are source-scoped: `cloned_from_state_id` on the
    # source_a clone references source_a's origin row, NOT source_b's. A future
    # cross-source lookup drift (e.g. dropping source_id from the query filter)
    # would silently attribute a source_a clone to source_b's `(M0, source_b, t*)`
    # origin, breaking Decision 3's per-source attribution.
    assert result.cloned_row.cloned_from_state_id == source_a_snapshot.state_id
    assert result.cloned_row.cloned_from_state_id != source_b_snapshot.state_id
    assert result.cloned_row.cloned_from_model_id == M0_MODEL_ID


# --- SUB-3 (§2.2 / §2.3): clone-row identity + cloned_from provenance ------


def test_equal_fingerprint_clone_populates_all_three_provenance_columns(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Green path: successful clone writes the three provenance columns.

    Epic #982 SUB-3 §2.2: the clone row's ``cloned_from_state_id`` /
    ``cloned_from_model_id`` name the M0 origin, and the
    ``clone_gate_fingerprint`` records the shared hash the equality gate
    accepted on (the same value returned by
    ``verify_hydrologic_core_fingerprint_equal``). The clone row's
    ``state_id`` still uses the ``state_snapshot_id`` convention under M1
    and differs from the source row's ``state_id`` (docs §Decision 2).
    ``run_id`` remains the M0 producing run's id (docs §Decision 3).
    """

    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is False
    assert result.cloned_row is not None
    clone = result.cloned_row

    # All three provenance columns populated with the pinned values.
    assert clone.cloned_from_state_id == source.state_id
    assert clone.cloned_from_model_id == M0_MODEL_ID
    assert clone.clone_gate_fingerprint == m0_m1_equal_packages["fingerprint_hash"]

    # Identity: state_id follows the convention under M1 and differs from source.
    assert clone.state_id != source.state_id
    assert clone.state_id == state_snapshot_id(
        M1_MODEL_ID,
        source.valid_time,
        source_id=source.source_id,
        cycle_id=source.cycle_id,
        lead_hours=source.lead_hours,
    )

    # M0 producing run's id is preserved on the clone row (Decision 3).
    assert clone.run_id == source.run_id


def test_attribution_rule_attributes_to_m1_via_model_id_not_run_id(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """MUST-level attribution: model_id + cloned_from_* — never run_id alone.

    Encodes the Epic #982 SUB-3 attribution invariant: a warm-start-lineage
    reader that keys on ``clone.model_id`` sees the M1 target identity; a
    reader that keys on ``clone.run_id`` still sees the M0 producing run's
    id. The two legitimately diverge, and the caller MUST NOT use
    ``run_id`` alone for model attribution — the audit trail is
    ``model_id`` + ``cloned_from_model_id`` + ``cloned_from_state_id``.
    """

    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()

    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )

    assert result.refused is False
    assert result.cloned_row is not None
    clone = result.cloned_row

    # Snapshot attributes to M1 via model_id...
    assert clone.model_id == M1_MODEL_ID
    # ...while run_id still points at the M0 producing run.
    assert clone.run_id == source.run_id
    # ...and the audit trail names M0 explicitly via cloned_from_model_id.
    assert clone.cloned_from_model_id == M0_MODEL_ID

    # Encoded MUST tuple: (attribution model, origin model, producer prefix).
    assert (
        clone.model_id,
        clone.cloned_from_model_id,
        clone.run_id.startswith("fcst_"),
    ) == (M1_MODEL_ID, M0_MODEL_ID, True)

    # Defensive contrast: reading run_id alone would mis-attribute to M0.
    # The clone.run_id embeds M0_MODEL_ID (it is the M0 producing run's id),
    # so a run_id-only reader cannot recover the M1 target identity — the
    # ``model_id`` + ``cloned_from_*`` columns exist precisely for this.
    assert M0_MODEL_ID in clone.run_id
    assert M1_MODEL_ID not in clone.run_id


def test_pre_clone_and_legacy_rows_keep_null_provenance_and_remain_selectable(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Legacy rows without provenance stay selectable and NULL on all three.

    Epic #982 SUB-3 §2.2 non-goal: no data backfill. Rows persisted before
    migration ``000046`` have ``NULL`` in the three provenance columns.
    The unchanged warm-start lookup (SUB-2 preserved the query shape) must
    still return them, and the three provenance fields must read as
    ``None`` from the ``StateSnapshot`` dataclass.
    """

    # Legacy source row — constructed WITHOUT any of the three provenance
    # kwargs, exercising the dataclass defaults.
    legacy = StateSnapshot(
        state_id=state_snapshot_id(
            M0_MODEL_ID,
            CUTOVER_VALID_TIME,
            source_id=SOURCE_ID,
            cycle_id=CYCLE_ID,
            lead_hours=12,
        ),
        model_id=M0_MODEL_ID,
        run_id=f"fcst_{SOURCE_ID}_{CYCLE_ID}_{M0_MODEL_ID}",
        valid_time=CUTOVER_VALID_TIME,
        state_uri="states/gfs/basin_v1_m0/2026061506/state.cfg.ic",
        checksum="sha256:legacy-state",
        usable_flag=True,
        source_id=SOURCE_ID,
        cycle_id=CYCLE_ID,
        lead_hours=12,
        model_package_version=M0_PACKAGE_VERSION,
        model_package_checksum=M0_PACKAGE_CHECKSUM,
        original_shud_filename="run.cfg.ic",
    )
    assert legacy.cloned_from_state_id is None
    assert legacy.cloned_from_model_id is None
    assert legacy.clone_gate_fingerprint is None

    repo = _FakeCloneRepository()
    repo.add(legacy)

    # The unchanged SUB-2 lookup still returns the legacy row.
    fetched = repo.get_state_snapshot_by_model_time(
        model_id=M0_MODEL_ID,
        valid_time=CUTOVER_VALID_TIME,
        source_id=SOURCE_ID,
        lead_hours=12,
    )
    assert fetched is legacy
    assert fetched.cloned_from_state_id is None
    assert fetched.cloned_from_model_id is None
    assert fetched.clone_gate_fingerprint is None


def test_production_serializers_round_trip_all_three_provenance_columns() -> None:
    """SUB-3 fold: production write path must persist and read back provenance.

    The SUB-2 fake `_FakeCloneRepository` stores the dataclass verbatim via
    ``dataclasses.replace``, so unit tests silently pass even if the four
    production-side serializer helpers drop the three new provenance columns.
    Under compact + state-clone-core inherent risk (production business
    continuity), a silent write-path drop would degrade every real clone row
    to a legacy-shaped row (Python-side always None), defeating the MUST
    attribution audit trail. This test round-trips a clone snapshot through
    each of the four production helpers and asserts all 3 provenance columns
    survive.
    """

    clone = StateSnapshot(
        state_id="clone_state_1",
        model_id=M1_MODEL_ID,
        run_id="fcst_gfs_2026061418_basin_v1_m0",
        valid_time=CUTOVER_VALID_TIME,
        state_uri="states/gfs/basin_v1_m0/2026061506/state.cfg.ic",
        checksum="sha256:clone-payload",
        usable_flag=True,
        source_id=SOURCE_ID,
        cycle_id=CYCLE_ID,
        lead_hours=12,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        original_shud_filename="run.cfg.ic",
        cloned_from_state_id="source_state_m0",
        cloned_from_model_id=M0_MODEL_ID,
        clone_gate_fingerprint="a" * 64,
    )

    # 1. Psycopg row-hydration helper (used by upsert_state_snapshot RETURNING).
    row_dict = {
        "state_id": clone.state_id,
        "model_id": clone.model_id,
        "run_id": clone.run_id,
        "valid_time": clone.valid_time,
        "state_uri": clone.state_uri,
        "checksum": clone.checksum,
        "usable_flag": clone.usable_flag,
        "created_at": _dt("2026-06-15T07:00:00Z"),
        "source_id": clone.source_id,
        "cycle_id": clone.cycle_id,
        "lead_hours": clone.lead_hours,
        "model_package_version": clone.model_package_version,
        "model_package_checksum": clone.model_package_checksum,
        "original_shud_filename": clone.original_shud_filename,
        "cloned_from_state_id": clone.cloned_from_state_id,
        "cloned_from_model_id": clone.cloned_from_model_id,
        "clone_gate_fingerprint": clone.clone_gate_fingerprint,
    }
    hydrated = _snapshot_from_row(row_dict)
    assert hydrated.cloned_from_state_id == clone.cloned_from_state_id
    assert hydrated.cloned_from_model_id == clone.cloned_from_model_id
    assert hydrated.clone_gate_fingerprint == clone.clone_gate_fingerprint

    # 2. Snapshot dict serializer.
    serialized = _snapshot_to_dict(clone)
    assert serialized["cloned_from_state_id"] == clone.cloned_from_state_id
    assert serialized["cloned_from_model_id"] == clone.cloned_from_model_id
    assert serialized["clone_gate_fingerprint"] == clone.clone_gate_fingerprint

    # 3. File-state-index entry emit + hydrate (Task 3.3 substrate).
    index_entry = _state_index_entry_from_snapshot(clone)
    assert index_entry["cloned_from_state_id"] == clone.cloned_from_state_id
    assert index_entry["cloned_from_model_id"] == clone.cloned_from_model_id
    assert index_entry["clone_gate_fingerprint"] == clone.clone_gate_fingerprint

    rehydrated = _state_snapshot_from_index_entry(index_entry)
    assert rehydrated.cloned_from_state_id == clone.cloned_from_state_id
    assert rehydrated.cloned_from_model_id == clone.cloned_from_model_id
    assert rehydrated.clone_gate_fingerprint == clone.clone_gate_fingerprint

    # 4. Legacy row (all provenance None) still hydrates without KeyError.
    legacy_row = dict(row_dict)
    legacy_row.pop("cloned_from_state_id")
    legacy_row.pop("cloned_from_model_id")
    legacy_row.pop("clone_gate_fingerprint")
    legacy_hydrated = _snapshot_from_row(legacy_row)
    assert legacy_hydrated.cloned_from_state_id is None
    assert legacy_hydrated.cloned_from_model_id is None
    assert legacy_hydrated.clone_gate_fingerprint is None

    legacy_entry = dict(index_entry)
    legacy_entry.pop("cloned_from_state_id")
    legacy_entry.pop("cloned_from_model_id")
    legacy_entry.pop("clone_gate_fingerprint")
    legacy_rehydrated = _state_snapshot_from_index_entry(legacy_entry)
    assert legacy_rehydrated.cloned_from_state_id is None
    assert legacy_rehydrated.cloned_from_model_id is None
    assert legacy_rehydrated.clone_gate_fingerprint is None


# --- Integration: strict warm-start acceptance on BOTH planes --------------


class _MinimalOrchestrator:
    """Bind ``chain_forecast_state`` free functions to ``self`` for tests.

    Mirrors the runtime wrapper pattern in
    ``services/orchestrator/chain_forecast_orchestrator_runtime.py``: the
    module-level functions receive ``self`` and delegate to
    ``self.state_manager``, so a minimal binding is enough to exercise
    the strict-validator without spinning up the full orchestrator.
    """

    def __init__(self, state_manager: Any) -> None:
        self.state_manager = state_manager

    def _state_passes_qc(self, state: StateSnapshot) -> bool:
        return chain_forecast_state._state_passes_qc(self, state)

    def _get_exact_forecast_state(self, **kwargs: Any) -> StateSnapshot | None:
        return chain_forecast_state._get_exact_forecast_state(self, **kwargs)

    def _validate_strict_forecast_state(
        self,
        state: StateSnapshot,
        **kwargs: Any,
    ) -> Any:
        return chain_forecast_state._validate_strict_forecast_state(self, state, **kwargs)


class _StateManagerFacade:
    """State-manager shape ``_get_exact_forecast_state`` expects.

    ``chain_forecast_state`` looks up the exact successor row via
    ``self.state_manager.repository.get_state_snapshot_by_model_time``,
    so we expose ``repository`` as the fake clone repository directly.
    """

    def __init__(self, repository: _FakeCloneRepository) -> None:
        self.repository = repository


def test_clone_row_accepted_by_db_path_strict_warm_start_validator(
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Plane 1: DB path — the cloned row is the exact +12h successor for M1.

    Drives ``_select_strict_forecast_initial_state`` (which internally
    calls ``_validate_strict_forecast_state``) with the cloned row present
    in the repository. NO validator code is modified; acceptance proves
    the clone's column disposition satisfies the existing strict path.
    """

    # Populate source + write the clone via the SUT.
    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()
    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )
    assert result.refused is False
    assert result.cloned_row is not None

    orchestrator = _MinimalOrchestrator(_StateManagerFacade(repo))
    selection = chain_forecast_state._select_strict_forecast_initial_state(
        orchestrator,
        model_id=M1_MODEL_ID,
        cycle_time=CUTOVER_VALID_TIME,
        source_id=SOURCE_ID,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
    )

    # Strict acceptance: the clone's identity is the exact successor state.
    assert selection.state_id == result.cloned_row.state_id
    assert selection.state_uri == result.cloned_row.state_uri
    assert selection.checksum == result.cloned_row.checksum
    assert _ensure_utc(selection.valid_time) == CUTOVER_VALID_TIME
    assert selection.source_id == SOURCE_ID
    assert selection.cycle_id == CYCLE_ID
    assert selection.lead_hours == 12
    assert selection.model_package_version == M1_PACKAGE_VERSION
    assert selection.model_package_checksum == M1_PACKAGE_CHECKSUM
    assert selection.rejection_code is None


def test_clone_row_accepted_by_file_index_path_strict_warm_start_evidence(
    tmp_path: Path,
    m0_m1_equal_packages: dict[str, Any],
) -> None:
    """Plane 2: file-index path — the cloned row is ready via evidence.

    The scheduler DB-free path consumes strict warm-start evidence from
    ``FileStateSnapshotIndexRepository.strict_warm_start_evidence``. This
    test publishes an index entry with the cloned ``(M1, source, t*)``
    identity + lineage and asserts the same validator returns ready.
    NO ``state_manager`` code is modified.
    """

    # Build the source + drive the clone through the SUT to obtain the
    # cloned row's identity (state_id + state_uri).
    source = _make_source_snapshot()
    repo = _FakeCloneRepository()
    repo.add(source)
    audit = _FakeAuditRecorder()
    result = fingerprint_gated_state_clone(
        repository=repo,
        audit_recorder=audit,
        **_default_clone_kwargs(m0_m1_equal_packages),
    )
    assert result.refused is False
    assert result.cloned_row is not None
    clone = result.cloned_row

    # Publish the underlying object so the file index's object verification
    # succeeds. The clone reuses the source's state_uri per INV-2.
    object_store = LocalObjectStore(tmp_path / "objects", object_store_prefix="s3://nhms")
    ic_content = _valid_ic_bytes(b"clone-row-evidence")
    state_uri = object_store.write_bytes_atomic(
        "states/gfs/basin_v1_m0/2026061506/state.cfg.ic",
        ic_content,
    )
    checksum = f"sha256:{sha256_bytes(ic_content)}"

    index_path = tmp_path / "state-index.json"
    publish_state_snapshot_index(
        [
            {
                "state_id": clone.state_id,
                "model_id": M1_MODEL_ID,
                "run_id": clone.run_id,
                "source_id": SOURCE_ID,
                "valid_time": _iso(CUTOVER_VALID_TIME),
                "state_uri": state_uri,
                "checksum": checksum,
                "usable_flag": True,
                "cycle_id": CYCLE_ID,
                "lead_hours": 12,
                "model_package_version": M1_PACKAGE_VERSION,
                "model_package_checksum": M1_PACKAGE_CHECKSUM,
            }
        ],
        index_path,
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        generated_at=CUTOVER_VALID_TIME + timedelta(hours=1),
    )
    index_repository = FileStateSnapshotIndexRepository(
        str(index_path),
        object_store_root=tmp_path / "objects",
        object_store_prefix="s3://nhms",
        now=CUTOVER_VALID_TIME + timedelta(hours=1),
    )

    evidence = index_repository.strict_warm_start_evidence(
        model_id=M1_MODEL_ID,
        source_id=SOURCE_ID,
        valid_time=CUTOVER_VALID_TIME,
        model_package_version=M1_PACKAGE_VERSION,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
    )

    assert evidence["status"] == "ready"
    assert evidence["ready"] is True
    assert evidence["reason"] is None
    candidate = evidence["candidate_state"]
    assert candidate["init_state_uri"] == state_uri
    assert candidate["init_state_checksum"] == checksum
    assert candidate["init_state_lineage"]["lead_hours"] == 12
    assert candidate["init_state_lineage"]["source_id"] == SOURCE_ID
    assert candidate["init_state_lineage"]["model_package_version"] == M1_PACKAGE_VERSION


# --- Test helpers ----------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _valid_ic_bytes(content: bytes) -> bytes:
    """Structurally-valid SHUD .cfg.ic body (mirrors test_state_manager helper).

    Vary the minute-time token so distinct callers keep distinct checksums.
    """
    minute = 27_000_000.0 + (int.from_bytes(content[:4].ljust(4, b"\x00"), "big") % 1000)
    lines = [
        f"2\t1\t{minute:.6f}",
        "1\t0.1\t0.1\t0.1\t0.1\t0.1",
        "2\t0.1\t0.1\t0.1\t0.1\t0.1",
        "1\t0.5",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
