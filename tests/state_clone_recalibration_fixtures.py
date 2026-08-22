"""Shared fixtures, fakes and the independent fingerprint oracle for the
recalibration state carry-over suites.

Consumed by :file:`tests/test_state_clone_recalibration.py` (the eight-surface
gate) and :file:`tests/test_state_clone_recalibration_cli.py` (the dual-index
CLI end-to-end). Extracted into its own non-suite module (house style:
``tests/river_identity_backfill_fakes.py``) so both modules share ONE package
fixture builder and ONE oracle instead of two drifting copies.

Expected fingerprint values are derived INDEPENDENTLY: ``_expected_state_
compatibility_fingerprint`` re-implements the documented domain-separated hash
format (per-surface hash, one ``f"{label}\\t{hash}\\n"`` line per covered
surface in alphabetical label order, SHA-256 of the joined buffer) directly
from the fixture bytes. It never calls the production fingerprint code, so a
drift in either the surface set or the hash format detonates here rather than
agreeing with itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.state_clone import fingerprint_gated_state_clone
from packages.common.state_manager import (
    StateSnapshot,
    state_snapshot_id,
)
from scripts.node22_clone_direct_grid_cutover_states import _state_compatibility_category_files
from workers.data_adapters.base import cycle_id_for
from workers.mapping_builder.rewrite import STATE_COMPATIBILITY_SURFACES

# --- Identities -------------------------------------------------------------

BASIN = "huai"
# Transfer SOURCE (M1) and transfer TARGET (M1'): both direct-grid variants.
M1_MODEL_ID = "huai_dg_gfs_v1"
M1P_MODEL_ID = "huai_dg_gfs_v2"
M1_PACKAGE_URI = "s3://nhms/models/huai_dg_gfs_v1/package"
M1P_PACKAGE_URI = "s3://nhms/models/huai_dg_gfs_v2/package"
M1_PACKAGE_CHECKSUM = "sha256:pkg-m1"
M1P_PACKAGE_CHECKSUM = "sha256:pkg-m1prime"
# The ORIGINAL baseline both variants were provisioned from. `M1'` still
# carries it in `resource_profile.baseline_model_id`, which is exactly why
# `--pairs` cannot resolve through the baseline-keyed variant map (D8).
ORIGINAL_BASELINE_MODEL_ID = "huai_baseline"
SOURCE_ID = "gfs"
CUTOVER_VALID_TIME = datetime(2026, 8, 15, 12, tzinfo=UTC)
CYCLE_ID = cycle_id_for(SOURCE_ID, CUTOVER_VALID_TIME - timedelta(hours=12))

# --- Package fixture bytes --------------------------------------------------

# The invariant hydrologic core: everything the eight-surface gate covers apart
# from `cfg.ic` (the `state_schema` surface) and the `.sp.att` non-FORC fields.
_INVARIANT_CORE_FILES: dict[str, bytes] = {
    f"{BASIN}.sp.mesh": b"mesh-topology-v1\n",
    f"{BASIN}.sp.riv": b"river-reach-v1\n",
    f"{BASIN}.sp.rivseg": b"river-segment-v1\n",
    f"{BASIN}.para.soil": b"soil-para-v1\n",
    f"{BASIN}.para.geol": b"geol-para-v1\n",
    f"{BASIN}.para.lc": b"land-cover-v1\n",
}
_LAKE_RELATIVE_PATH = f"{BASIN}.lake.att"
_LAKE_BYTES = b"lake-topology-v1\n"

# The recalibration itself: the ONLY files that legally differ.
_CALIB_V1 = b"cfg.calib\nksat=1.0e-5\n"
_CALIB_V2 = b"cfg.calib\nksat=3.7e-5\n"
_CALIB_TABLE_V1 = b"CALIB/table\nrow,1.0\n"
_CALIB_TABLE_V2 = b"CALIB/table\nrow,2.5\n"
_PARA_V1 = b"cfg.para\ndt=3600\n"
_PARA_V2 = b"cfg.para\ndt=1800\n"

# The initial-condition file. A NEW cfg.ic declares a new modeling starting
# point and must refuse the carry-over (D3 / Invariant Matrix row 2).
_IC_V1 = b"cfg.ic\nsoil_moisture=0.31\n"
_IC_V2 = b"cfg.ic\nsoil_moisture=0.55\n"

_SP_ATT_SCHEMA = ("INDEX", "SOIL", "GEOL", "LC", "FORC", "MF", "BC", "SS", "LAKE")
_FORC_COLUMN_INDEX = 4


# --- Fixture writers --------------------------------------------------------


def _sp_att_rows(
    *,
    forc_values: Sequence[int] = (1, 2, 3, 4),
    soil: int = 1,
) -> tuple[tuple[int, ...], ...]:
    return tuple(
        (index + 1, soil, 1, 11, forc_values[index], 1, 0, 0, 0) for index in range(4)
    )


def _write_sp_att(path: Path, rows: Sequence[Sequence[int]]) -> Path:
    lines = [f"{len(rows)}\t{len(_SP_ATT_SCHEMA)}", "\t".join(_SP_ATT_SCHEMA)]
    lines.extend("\t".join(str(value) for value in row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _direct_grid_manifest(model_id: str) -> dict[str, Any]:
    """A ``direct_grid_forcing`` section that classifies as direct-grid."""

    return {
        "forcing_mapping_mode": "direct_grid",
        "binding_uri": f"s3://nhms/models/{model_id}/direct-grid/binding.json",
        "binding_checksum": f"sha256:binding-{model_id}",
        "model_input_package_id": f"model-input-{model_id}",
        "sp_att_path": f"{BASIN}.sp.att",
        "sp_att_checksum": f"sha256:sp-att-{model_id}",
        "applicable_source_ids": [SOURCE_ID],
        "grid_id": "grid_gfs_025",
        "grid_signature": "sha256:grid-signature",
        "station_bindings": [
            {
                "station_id": "s001",
                "shud_forcing_index": 1,
                "forcing_filename": "X114.00Y33.00.csv",
                "longitude": 114.0,
                "latitude": 33.0,
                "x": 1.0,
                "y": 2.0,
                "z": 3.0,
                "grid_id": "grid_gfs_025",
                "grid_cell_id": "cell-001",
            },
        ],
    }


def _write_package(
    root: Path,
    *,
    model_id: str,
    calib: bytes,
    calib_table: bytes,
    para: bytes,
    ic: bytes,
    lake: bool = True,
    core_overrides: Mapping[str, bytes] | None = None,
    sp_att_rows: Sequence[Sequence[int]] | None = None,
    legacy_manifest: bool = False,
) -> Path:
    """Write one direct-grid model input package."""

    root.mkdir(parents=True, exist_ok=True)
    payloads = dict(_INVARIANT_CORE_FILES)
    payloads.update(core_overrides or {})
    for relative_path, payload in payloads.items():
        (root / relative_path).write_bytes(payload)
    if lake:
        (root / _LAKE_RELATIVE_PATH).write_bytes(_LAKE_BYTES)
    (root / f"{BASIN}.cfg.calib").write_bytes(calib)
    (root / "CALIB").mkdir(exist_ok=True)
    (root / "CALIB" / "table.csv").write_bytes(calib_table)
    (root / f"{BASIN}.cfg.para").write_bytes(para)
    (root / f"{BASIN}.cfg.ic").write_bytes(ic)
    _write_sp_att(root / f"{BASIN}.sp.att", sp_att_rows or _sp_att_rows())
    manifest: dict[str, Any] = {"model_id": model_id}
    manifest["direct_grid_forcing"] = {} if legacy_manifest else _direct_grid_manifest(model_id)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


# --- Independent fingerprint oracle ----------------------------------------


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _expected_file_category_hash(root: Path, relative_paths: Sequence[str]) -> str:
    """SHA-256 over ``f"{path}\\t{file_sha256}\\n"`` per file, sorted by path."""

    joined = "".join(
        f"{relative_path}\t{_sha256((root / relative_path).read_bytes())}\n"
        for relative_path in sorted(relative_paths)
    )
    return _sha256(joined.encode("utf-8"))


def _expected_sp_att_non_forc_hash(rows: Sequence[Sequence[int]]) -> str:
    """Canonicalized non-``FORC`` payload keyed by element id, schema-prefixed."""

    non_forc_columns = [
        index for index in range(len(_SP_ATT_SCHEMA)) if index != _FORC_COLUMN_INDEX
    ]
    lines = ["schema\t" + "\t".join(_SP_ATT_SCHEMA[i] for i in non_forc_columns) + "\n"]
    for row in sorted(rows, key=lambda item: int(item[0])):
        lines.append(
            f"{int(row[0])}\t" + "\t".join(str(row[i]) for i in non_forc_columns) + "\n"
        )
    return _sha256("".join(lines).encode("utf-8"))


def _expected_state_compatibility_fingerprint(
    root: Path,
    *,
    category_files: Mapping[str, Sequence[str]],
    ic_bytes: bytes,
    sp_att_rows: Sequence[Sequence[int]] | None = None,
) -> str:
    """The eight-surface fingerprint, recomputed from the fixture bytes alone."""

    per_surface = {
        category: _expected_file_category_hash(root, paths)
        for category, paths in category_files.items()
    }
    per_surface["sp_att_non_forc"] = _expected_sp_att_non_forc_hash(
        sp_att_rows or _sp_att_rows()
    )
    per_surface["state_schema"] = _sha256(ic_bytes)
    assert set(per_surface) == set(STATE_COMPATIBILITY_SURFACES), sorted(per_surface)
    top = "".join(f"{label}\t{per_surface[label]}\n" for label in sorted(per_surface))
    return _sha256(top.encode("utf-8"))


# --- Fakes ------------------------------------------------------------------


class _FakeCloneRepository:
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
            if snapshot.model_id != model_id or snapshot.source_id != source_id:
                continue
            if snapshot.valid_time != valid_time:
                continue
            if lead_hours is not None and snapshot.lead_hours != lead_hours:
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
            and snapshot.valid_time < before_time
        ]
        return max(candidates, key=lambda item: item.valid_time) if candidates else None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.upserted.append(snapshot)
        self.snapshots[snapshot.state_id] = snapshot
        return snapshot


class _FakeAuditRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record_refusal(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))


def _m1_source_snapshot(*, state_uri: str | None = None, checksum: str | None = None) -> StateSnapshot:
    """The qualified ``(M1, gfs, t*)`` +12h checkpoint the carry-over reuses."""

    run_id = f"fcst_{SOURCE_ID}_{CYCLE_ID}_{M1_MODEL_ID}"
    return StateSnapshot(
        state_id=state_snapshot_id(
            M1_MODEL_ID,
            CUTOVER_VALID_TIME,
            source_id=SOURCE_ID,
            cycle_id=CYCLE_ID,
            lead_hours=12,
        ),
        model_id=M1_MODEL_ID,
        run_id=run_id,
        valid_time=CUTOVER_VALID_TIME,
        state_uri=state_uri or f"states/{SOURCE_ID}/{M1_MODEL_ID}/2026081512/state.cfg.ic",
        checksum=checksum or "sha256:m1-state-payload",
        usable_flag=True,
        source_id=SOURCE_ID,
        cycle_id=CYCLE_ID,
        lead_hours=12,
        model_package_version=M1_PACKAGE_URI,
        model_package_checksum=M1_PACKAGE_CHECKSUM,
        original_shud_filename="run.cfg.ic",
    )


# --- Package pair fixtures --------------------------------------------------


def _build_pair(
    tmp_path: Path,
    *,
    target_ic: bytes = _IC_V1,
    target_core_overrides: Mapping[str, bytes] | None = None,
    target_sp_att_rows: Sequence[Sequence[int]] | None = None,
    source_lake: bool = True,
    target_lake: bool = True,
    target_legacy_manifest: bool = False,
) -> dict[str, Any]:
    """Two dg packages differing only in calibration unless a drift is asked for."""

    source_root = _write_package(
        tmp_path / "m1",
        model_id=M1_MODEL_ID,
        calib=_CALIB_V1,
        calib_table=_CALIB_TABLE_V1,
        para=_PARA_V1,
        ic=_IC_V1,
        lake=source_lake,
    )
    target_root = _write_package(
        tmp_path / "m1prime",
        model_id=M1P_MODEL_ID,
        calib=_CALIB_V2,
        calib_table=_CALIB_TABLE_V2,
        para=_PARA_V2,
        ic=target_ic,
        lake=target_lake,
        core_overrides=target_core_overrides,
        sp_att_rows=target_sp_att_rows,
        legacy_manifest=target_legacy_manifest,
    )
    category_files = _state_compatibility_category_files(source_root, target_root)
    return {
        "source_root": source_root,
        "target_root": target_root,
        "category_files": category_files,
        "source_ic": _IC_V1,
        "target_ic": target_ic,
    }


def _recalibration_kwargs(pair: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "m0_model_id": M1_MODEL_ID,
        "m1_model_id": M1P_MODEL_ID,
        "m1_model_package_version": M1P_PACKAGE_URI,
        "m1_model_package_checksum": M1P_PACKAGE_CHECKSUM,
        "source_id": SOURCE_ID,
        "cutover_valid_time": CUTOVER_VALID_TIME,
        "m0_package_root": pair["source_root"],
        "m1_package_root": pair["target_root"],
        "m0_sp_att_path": pair["source_root"] / f"{BASIN}.sp.att",
        "m1_sp_att_path": pair["target_root"] / f"{BASIN}.sp.att",
        "m1_category_files": pair["category_files"],
        "m1_recorded_hydrologic_core_fingerprint": None,
        "state_schema_bytes": pair["target_ic"],
        "solver_config_bytes": _PARA_V2,
        "m0_state_schema_bytes": pair["source_ic"],
        "m0_solver_config_bytes": _PARA_V1,
        "m1_forcing_mapping_manifest": _direct_grid_manifest(M1P_MODEL_ID),
        "transfer_mode": "recalibration",
    }


def _run_recalibration_clone(
    pair: Mapping[str, Any],
    **overrides: Any,
) -> tuple[Any, _FakeCloneRepository, _FakeAuditRecorder]:
    repository = _FakeCloneRepository()
    repository.add(_m1_source_snapshot())
    audit = _FakeAuditRecorder()
    kwargs = _recalibration_kwargs(pair)
    kwargs.update(overrides)
    result = fingerprint_gated_state_clone(
        repository=repository,
        audit_recorder=audit,
        **kwargs,
    )
    return result, repository, audit
