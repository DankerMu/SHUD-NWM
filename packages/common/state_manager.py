from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Protocol
from urllib.parse import unquote, urlparse

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.source_identity import normalize_source_id
from packages.common.state_lineage import STATE_QC_FAILED
from packages.common.state_qc import MAX_STATE_IC_BYTES, run_state_variable_qc
from workers.data_adapters.base import cycle_id_for


class StateManagerError(RuntimeError):
    """Raised when StateSnapshot operations cannot complete."""


FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION = "nhms.scheduler.file_state_snapshot_index.v1"
MAX_STATE_SNAPSHOT_INDEX_BYTES = 16 * 1024 * 1024
MAX_STATE_SNAPSHOT_INDEX_ENTRIES = 100_000
DEFAULT_STATE_SNAPSHOT_INDEX_MAX_AGE_HOURS = 168
MAX_STATE_SNAPSHOT_INDEX_JSON_DEPTH = 64
MAX_STATE_SNAPSHOT_INDEX_JSON_NODES = 300_000
STATE_INDEX_CONTROL_OBJECT_PREFIXES = frozenset({"logs", "manifests", "products", "runs"})
STATE_INDEX_CONTROL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
STATE_INDEX_CONTROL_ENCODED_FORBIDDEN_RE = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise StateManagerError("DATABASE_URL is required for state manager database operations.")
    return database_url


@dataclass(frozen=True)
class StateSnapshot:
    state_id: str
    model_id: str
    run_id: str
    valid_time: datetime
    state_uri: str
    checksum: str
    usable_flag: bool = False
    created_at: datetime | None = None
    # Lineage (M24 §2 Lane 1) - all optional, default None for backward compatibility.
    source_id: str | None = None
    cycle_id: str | None = None
    lead_hours: int | None = None
    model_package_version: str | None = None
    model_package_checksum: str | None = None
    original_shud_filename: str | None = None


@dataclass(frozen=True)
class StateSnapshotSaveResult:
    status: str
    state_id: str
    snapshot: StateSnapshot

    def __str__(self) -> str:
        return self.state_id

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.state_id == other
        if not isinstance(other, StateSnapshotSaveResult):
            return False
        return self.status == other.status and self.state_id == other.state_id and self.snapshot == other.snapshot


@dataclass(frozen=True)
class _StateIndexSnapshot:
    payload: dict[str, Any]
    content: bytes
    entries: dict[tuple[str, str, str, str, str], dict[str, Any]]
    evidence: dict[str, Any]


class StateSnapshotRepository(Protocol):
    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None: ...

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None: ...

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot: ...

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None: ...

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None: ...

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]: ...

    def insert_qc_result(self, record: Mapping[str, Any]) -> dict[str, Any]: ...


def _state_snapshot_metadata_matches(
    snapshot: StateSnapshot,
    *,
    run_id: str,
    source_id: str | None,
    cycle_id: str | None,
    lead_hours: int | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
    original_shud_filename: str | None,
) -> bool:
    if snapshot.run_id != run_id:
        return False
    if _optional_str(snapshot.source_id) != _optional_str(source_id):
        return False
    if _optional_str(snapshot.cycle_id) != _optional_str(cycle_id):
        return False
    if snapshot.lead_hours != lead_hours:
        return False
    if _optional_str(snapshot.model_package_version) != _optional_str(model_package_version):
        return False
    if bool(snapshot.model_package_checksum) != bool(model_package_checksum):
        return False
    if snapshot.model_package_checksum and not _checksum_matches(
        snapshot.model_package_checksum,
        model_package_checksum,
    ):
        return False
    return _optional_str(snapshot.original_shud_filename) == _optional_str(original_shud_filename)


def _same_checksum_lineage_repair_candidate(
    snapshot: StateSnapshot,
    *,
    cycle_id: str | None,
    lead_hours: int | None,
    model_package_version: str | None,
    model_package_checksum: str | None,
) -> bool:
    if snapshot.cycle_id not in (None, "", cycle_id):
        return False
    if snapshot.lead_hours not in (None, lead_hours):
        return False
    if snapshot.cycle_id in (None, "") or snapshot.lead_hours is None:
        return True
    target_package_version = _optional_str(model_package_version)
    if target_package_version is not None and _optional_str(snapshot.model_package_version) != target_package_version:
        return True
    target_package_checksum = _optional_str(model_package_checksum)
    if target_package_checksum is not None:
        if snapshot.model_package_checksum in (None, ""):
            return True
        if not _checksum_matches(snapshot.model_package_checksum, target_package_checksum):
            return True
    return False


@dataclass(frozen=True)
class StateManager:
    repository: StateSnapshotRepository
    object_store: LocalObjectStore

    @classmethod
    def from_env(cls) -> StateManager:
        workspace_root = Path(os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
        object_store_root = Path(os.getenv("OBJECT_STORE_ROOT", str(workspace_root)))
        object_store_prefix = os.getenv("OBJECT_STORE_PREFIX", "")
        return cls(
            repository=PsycopgStateSnapshotRepository.from_env(),
            object_store=LocalObjectStore(object_store_root, object_store_prefix=object_store_prefix),
        )

    def save_state_snapshot(
        self,
        *,
        model_id: str,
        run_id: str,
        valid_time: datetime,
        ic_file_path: Path | str,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
        model_package_version: str | None = None,
        model_package_checksum: str | None = None,
        original_shud_filename: str | None = None,
    ) -> StateSnapshotSaveResult:
        parsed_valid_time = _ensure_utc(valid_time)
        state_id = state_snapshot_id(
            model_id,
            parsed_valid_time,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
        )
        path = Path(ic_file_path)
        try:
            content = read_bytes_limited_no_follow(path, max_bytes=MAX_STATE_IC_BYTES)
            if len(content) > MAX_STATE_IC_BYTES:
                raise StateManagerError(
                    f"State snapshot file {path} exceeds size limit of {MAX_STATE_IC_BYTES} bytes."
                )
        except StateManagerError:
            raise
        except (OSError, SafeFilesystemError) as error:
            raise StateManagerError(f"Failed to read state snapshot file {path}: {error}") from error

        checksum = sha256_bytes(content)
        lookup_kwargs: dict[str, Any] = {
            "model_id": model_id,
            "valid_time": parsed_valid_time,
            "source_id": source_id,
        }
        if cycle_id not in (None, ""):
            lookup_kwargs["cycle_id"] = cycle_id
        if lead_hours is not None:
            lookup_kwargs["lead_hours"] = lead_hours
        existing = self.repository.get_state_snapshot_by_model_time(**lookup_kwargs)
        if existing is None and (cycle_id not in (None, "") or lead_hours is not None):
            same_checksum_existing = self._find_same_checksum_base_snapshot(
                model_id=model_id,
                source_id=source_id,
                valid_time=parsed_valid_time,
                checksum=checksum,
            )
            if same_checksum_existing is not None and _same_checksum_lineage_repair_candidate(
                same_checksum_existing,
                cycle_id=cycle_id,
                lead_hours=lead_hours,
                model_package_version=model_package_version,
                model_package_checksum=model_package_checksum,
            ):
                existing = same_checksum_existing
        if existing is not None and _checksum_matches(existing.checksum, checksum):
            if not self._snapshot_object_matches(existing, checksum):
                repaired = self._rewrite_missing_same_checksum_snapshot(
                    existing,
                    content=content,
                    checksum=checksum,
                    run_id=run_id,
                    model_id=model_id,
                    valid_time=parsed_valid_time,
                    source_id=source_id,
                    cycle_id=cycle_id,
                    lead_hours=lead_hours,
                    model_package_version=model_package_version,
                    model_package_checksum=model_package_checksum,
                    original_shud_filename=original_shud_filename,
                )
                return StateSnapshotSaveResult(status="superseded", state_id=repaired.state_id, snapshot=repaired)
            if _state_snapshot_metadata_matches(
                existing,
                run_id=run_id,
                source_id=source_id,
                cycle_id=cycle_id,
                lead_hours=lead_hours,
                model_package_version=model_package_version,
                model_package_checksum=model_package_checksum,
                original_shud_filename=original_shud_filename,
            ):
                return StateSnapshotSaveResult(status="already_done", state_id=existing.state_id, snapshot=existing)
            repaired = self.repository.upsert_state_snapshot(
                StateSnapshot(
                    state_id=existing.state_id,
                    model_id=model_id,
                    run_id=run_id,
                    valid_time=parsed_valid_time,
                    state_uri=existing.state_uri,
                    checksum=existing.checksum,
                    usable_flag=False,
                    source_id=source_id,
                    cycle_id=cycle_id,
                    lead_hours=lead_hours,
                    model_package_version=model_package_version,
                    model_package_checksum=model_package_checksum,
                    original_shud_filename=original_shud_filename,
                )
            )
            return StateSnapshotSaveResult(status="superseded", state_id=repaired.state_id, snapshot=repaired)

        state_key = _state_object_key(
            model_id,
            parsed_valid_time,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
        )
        try:
            state_uri = self.object_store.write_bytes_atomic(state_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise StateManagerError(f"Failed to upload state snapshot {state_id}: {error}") from error

        snapshot = StateSnapshot(
            state_id=state_id,
            model_id=model_id,
            run_id=run_id,
            valid_time=parsed_valid_time,
            state_uri=state_uri,
            checksum=checksum,
            usable_flag=False,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
            original_shud_filename=original_shud_filename,
        )
        saved = self.repository.upsert_state_snapshot(snapshot)
        status = "superseded" if existing is not None else "created"
        return StateSnapshotSaveResult(status=status, state_id=saved.state_id, snapshot=saved)

    def _snapshot_object_matches(self, snapshot: StateSnapshot, checksum: str) -> bool:
        try:
            _size, actual_checksum = self.object_store.size_and_checksum_limited(
                snapshot.state_uri,
                max_bytes=MAX_STATE_IC_BYTES,
            )
        except (ObjectStoreError, OSError, ValueError):
            return False
        return _checksum_matches(snapshot.checksum, actual_checksum) and _checksum_matches(checksum, actual_checksum)

    def _rewrite_missing_same_checksum_snapshot(
        self,
        existing: StateSnapshot,
        *,
        content: bytes,
        checksum: str,
        run_id: str,
        model_id: str,
        valid_time: datetime,
        source_id: str | None,
        cycle_id: str | None,
        lead_hours: int | None,
        model_package_version: str | None,
        model_package_checksum: str | None,
        original_shud_filename: str | None,
    ) -> StateSnapshot:
        state_key = _state_object_key(
            model_id,
            valid_time,
            source_id=source_id,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
        )
        try:
            state_uri = self.object_store.write_bytes_atomic(state_key, content)
        except (OSError, ObjectStoreError, ValueError) as error:
            raise StateManagerError(f"Failed to repair missing state snapshot {existing.state_id}: {error}") from error
        return self.repository.upsert_state_snapshot(
            StateSnapshot(
                state_id=existing.state_id,
                model_id=model_id,
                run_id=run_id,
                valid_time=valid_time,
                state_uri=state_uri,
                checksum=checksum,
                usable_flag=False,
                source_id=source_id,
                cycle_id=cycle_id,
                lead_hours=lead_hours,
                model_package_version=model_package_version,
                model_package_checksum=model_package_checksum,
                original_shud_filename=original_shud_filename,
            )
        )

    def _find_same_checksum_base_snapshot(
        self,
        *,
        model_id: str,
        source_id: str | None,
        valid_time: datetime,
        checksum: str,
    ) -> StateSnapshot | None:
        finder = getattr(self.repository, "find_state_snapshot_by_model_time_checksum", None)
        if callable(finder):
            return finder(
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
                checksum=checksum,
            )
        candidate = self.repository.get_state_snapshot_by_model_time(
            model_id=model_id,
            source_id=source_id,
            valid_time=valid_time,
        )
        if candidate is not None and _checksum_matches(candidate.checksum, checksum):
            return candidate
        return None

    def run_qc(self, state_id: str | StateSnapshotSaveResult) -> bool:
        resolved_state_id = str(state_id)
        snapshot = self.repository.get_state_snapshot(resolved_state_id)
        if snapshot is None:
            self.repository.insert_qc_result(
                _qc_record(
                    state_id=resolved_state_id,
                    run_id=None,
                    passed=False,
                    severity="error",
                    checks_json={"error_code": "STATE_SNAPSHOT_NOT_FOUND"},
                    message=f"State snapshot not found: {resolved_state_id}",
                )
            )
            return False

        check = self._check_snapshot_object(snapshot)
        if check["passed"]:
            self.repository.set_usable_flag(state_id=resolved_state_id, usable_flag=True)
        else:
            self.repository.set_usable_flag(state_id=resolved_state_id, usable_flag=False)

        self.repository.insert_qc_result(
            _qc_record(
                state_id=resolved_state_id,
                run_id=snapshot.run_id,
                passed=bool(check["passed"]),
                severity="info" if check["passed"] else "error",
                checks_json=check,
                message="State snapshot QC passed." if check["passed"] else str(check["message"]),
            )
        )
        return bool(check["passed"])

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        return self.repository.get_latest_usable_state(model_id=model_id, before_time=_ensure_utc(before_time))

    def list_state_snapshots(
        self,
        *,
        model_id: str | None = None,
        usable: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.repository.list_state_snapshots(
            model_id=model_id,
            usable=usable,
            limit=limit,
            offset=offset,
        )

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        return self.repository.get_state_snapshot(state_id)

    def mark_init_state_corrupted(
        self,
        state_id: str,
        *,
        message: str = "Initial state checksum mismatch.",
        actual_checksum: str | None = None,
        expected_checksum: str | None = None,
    ) -> None:
        snapshot = self.repository.get_state_snapshot(state_id)
        self.repository.set_usable_flag(state_id=state_id, usable_flag=False)
        self.repository.insert_qc_result(
            _qc_record(
                state_id=state_id,
                run_id=snapshot.run_id if snapshot is not None else None,
                passed=False,
                severity="error",
                checks_json={
                    "passed": False,
                    "error_code": "INIT_STATE_CORRUPTED",
                    "message": message,
                    "actual_checksum": actual_checksum,
                    "expected_checksum": expected_checksum,
                },
                message=message,
            )
        )

    def _check_snapshot_object(self, snapshot: StateSnapshot) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "exists": False,
            "size_bytes": 0,
            "checksum_matches": False,
            "expected_checksum": snapshot.checksum,
        }
        try:
            exists = self.object_store.exists(snapshot.state_uri)
            checks["exists"] = exists
            if not exists:
                checks.update({"passed": False, "error_code": "STATE_FILE_MISSING", "message": "State file missing."})
                return checks

            size_bytes = self.object_store.size(snapshot.state_uri)
            checks["size_bytes"] = size_bytes
            if size_bytes <= 0:
                checks.update({"passed": False, "error_code": "STATE_FILE_EMPTY", "message": "State file is empty."})
                return checks
            if size_bytes > MAX_STATE_IC_BYTES:
                checks.update(
                    {
                        "passed": False,
                        "error_code": "STATE_FILE_TOO_LARGE",
                        "message": (
                            f"State file size {size_bytes} bytes exceeds limit of {MAX_STATE_IC_BYTES} bytes."
                        ),
                    }
                )
                return checks

            actual_checksum = self.object_store.checksum(snapshot.state_uri)
            checks["actual_checksum"] = actual_checksum
            checks["checksum_matches"] = _checksum_matches(snapshot.checksum, actual_checksum)
            if not _checksum_matches(snapshot.checksum, actual_checksum):
                checks.update(
                    {
                        "passed": False,
                        "error_code": "STATE_CHECKSUM_MISMATCH",
                        "message": "State checksum mismatch.",
                    }
                )
                return checks
        except (OSError, ObjectStoreError, ValueError) as error:
            checks.update(
                {
                    "passed": False,
                    "error_code": "STATE_OBJECT_ERROR",
                    "message": str(error),
                }
            )
            return checks

        state_qc = self._run_state_variable_qc(snapshot)
        checks["state_variable_qc"] = state_qc.to_dict()
        if not state_qc.passed:
            checks.update(
                {
                    "passed": False,
                    "error_code": STATE_QC_FAILED,
                    "message": state_qc.reason or "State-variable QC failed.",
                }
            )
            return checks

        checks.update({"passed": True, "error_code": None, "message": "State snapshot QC passed."})
        return checks

    def _run_state_variable_qc(self, snapshot: StateSnapshot) -> Any:
        """Parse the IC object and run SHUD state-variable QC.

        Production QC scope (honest boundaries):

        - Expected element counts are NOT available at the snapshot layer in this Lane,
          so QC runs with ``counts=None``: the row-count dimension is NOT exercised in
          production; only structure / range / non-negativity are enforced here.
        - Selection-time ``state_variable_qc_passed`` is not re-checked by the
          production ``StateManager`` (trust-through of the save-time usable_flag), and
          the restart first-step water-balance check is skipped (no first-step
          diagnostics wired this Lane).
        - The IC object is read with a hard byte bound (``MAX_STATE_IC_BYTES``) so a
          corrupt / oversized artifact fails QC instead of reading unboundedly into
          memory. A parse failure is reported as a QC failure (never raised) by
          ``run_state_variable_qc``.
        """

        try:
            content = self.object_store.read_bytes_limited(snapshot.state_uri, max_bytes=MAX_STATE_IC_BYTES)
        except (OSError, ObjectStoreError, ValueError) as error:
            from packages.common.state_qc import StateQCResult

            return StateQCResult(
                passed=False,
                checks={"read_error": str(error)},
                reason=f"Failed to read IC object for QC: {error}",
            )

        with tempfile.NamedTemporaryFile(suffix=".cfg.ic", delete=True) as handle:
            handle.write(content)
            handle.flush()
            return run_state_variable_qc(handle.name)


@dataclass(frozen=True)
class PsycopgStateSnapshotRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgStateSnapshotRepository:
        return cls(default_database_url())

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        row = self._fetch_optional(
            """
            SELECT *
            FROM hydro.state_snapshot
            WHERE state_id = %s
            """,
            (state_id,),
        )
        return _snapshot_from_row(row) if row is not None else None

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        del cycle_id, lead_hours
        if source_id is not None:
            row = self._fetch_optional(
                """
                SELECT *
                FROM hydro.state_snapshot
                WHERE model_id = %s
                  AND source_id = %s
                  AND valid_time = %s
                """,
                (model_id, source_id, _ensure_utc(valid_time)),
            )
            return _snapshot_from_row(row) if row is not None else None
        row = self._fetch_optional(
            """
            SELECT *
            FROM hydro.state_snapshot
            WHERE model_id = %s
              AND valid_time = %s
            """,
            (model_id, _ensure_utc(valid_time)),
        )
        return _snapshot_from_row(row) if row is not None else None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        row = self._fetch_one(
            """
            INSERT INTO hydro.state_snapshot (
                state_id,
                model_id,
                run_id,
                valid_time,
                state_uri,
                checksum,
                usable_flag,
                source_id,
                cycle_id,
                lead_hours,
                model_package_version,
                model_package_checksum,
                original_shud_filename
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, (COALESCE(source_id, ''::text)), valid_time) DO UPDATE SET
                state_id = EXCLUDED.state_id,
                run_id = EXCLUDED.run_id,
                state_uri = EXCLUDED.state_uri,
                checksum = EXCLUDED.checksum,
                usable_flag = false,
                source_id = EXCLUDED.source_id,
                cycle_id = EXCLUDED.cycle_id,
                lead_hours = EXCLUDED.lead_hours,
                model_package_version = EXCLUDED.model_package_version,
                model_package_checksum = EXCLUDED.model_package_checksum,
                original_shud_filename = EXCLUDED.original_shud_filename,
                created_at = now()
            RETURNING *
            """,
            (
                snapshot.state_id,
                snapshot.model_id,
                snapshot.run_id,
                _ensure_utc(snapshot.valid_time),
                snapshot.state_uri,
                snapshot.checksum,
                snapshot.usable_flag,
                snapshot.source_id,
                snapshot.cycle_id,
                snapshot.lead_hours,
                snapshot.model_package_version,
                snapshot.model_package_checksum,
                snapshot.original_shud_filename,
            ),
        )
        return _snapshot_from_row(row)

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
        row = self._fetch_optional(
            """
            UPDATE hydro.state_snapshot
            SET usable_flag = %s
            WHERE state_id = %s
            RETURNING *
            """,
            (usable_flag, state_id),
        )
        return _snapshot_from_row(row) if row is not None else None

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        row = self._fetch_optional(
            """
            SELECT *
            FROM hydro.state_snapshot
            WHERE model_id = %s
              AND usable_flag = true
              AND valid_time <= %s
            ORDER BY valid_time DESC
            LIMIT 1
            """,
            (model_id, _ensure_utc(before_time)),
        )
        return _snapshot_from_row(row) if row is not None else None

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if model_id is not None:
            clauses.append("model_id = %s")
            parameters.append(model_id)
        if usable is not None:
            clauses.append("usable_flag = %s")
            parameters.append(usable)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count_row = self._fetch_one(
            f"SELECT COUNT(*) AS total_count FROM hydro.state_snapshot {where}",
            tuple(parameters),
        )
        rows = self._fetch_all(
            f"""
            SELECT *
            FROM hydro.state_snapshot
            {where}
            ORDER BY valid_time DESC, state_id
            LIMIT %s OFFSET %s
            """,
            (*parameters, limit, offset),
        )
        return {
            "total_count": int(count_row["total_count"]),
            "items": [_snapshot_to_dict(_snapshot_from_row(row)) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def insert_qc_result(self, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise StateManagerError("psycopg2 is required for state manager database operations.") from error

        return self._fetch_one(
            """
            INSERT INTO ops.qc_result (
                qc_checkpoint,
                target_type,
                target_id,
                run_id,
                cycle_id,
                passed,
                severity,
                checks_json,
                message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                record["qc_checkpoint"],
                record["target_type"],
                record["target_id"],
                record.get("run_id"),
                record.get("cycle_id"),
                record["passed"],
                record["severity"],
                Json(dict(record["checks_json"])),
                record["message"],
            ),
        )

    def _fetch_one(self, statement: str, parameters: Sequence[Any]) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise StateManagerError("State manager database operation did not return a row.")
        return row

    def _fetch_optional(self, statement: str, parameters: Sequence[Any]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, statement: str, parameters: Sequence[Any]) -> list[dict[str, Any]]:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
        except ImportError as error:
            raise StateManagerError("psycopg2 is required for state manager database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            register_default_json(conn_or_curs=connection)
            register_default_jsonb(conn_or_curs=connection)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(statement, tuple(parameters))
                if cursor.description is None:
                    connection.commit()
                    return []
                rows = [dict(row) for row in cursor.fetchall()]
                connection.commit()
                return rows
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise StateManagerError(f"State manager database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True)
class FileStateSnapshotIndexRepository:
    index_uri: str
    object_store_root: Path | str | None = None
    object_store_prefix: str | None = None
    published_artifact_root: Path | str | None = None
    now: datetime | None = None
    max_age_hours: int = DEFAULT_STATE_SNAPSHOT_INDEX_MAX_AGE_HOURS
    create_missing: bool = False
    _index_snapshot_cache: _StateIndexSnapshot | None = dataclass_field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_env(cls, *, create_missing: bool = False) -> FileStateSnapshotIndexRepository:
        index_uri = os.getenv("NHMS_SCHEDULER_STATE_INDEX", "").strip()
        if not index_uri:
            raise StateManagerError("NHMS_SCHEDULER_STATE_INDEX is required for file state index operations.")
        return cls(
            index_uri=index_uri,
            object_store_root=os.getenv("OBJECT_STORE_ROOT"),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            published_artifact_root=os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT"),
            create_missing=create_missing,
        )

    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        index_snapshot = self._load_index_snapshot(
            allow_empty=self.create_missing,
            verify_objects=False,
            enforce_freshness=not self.create_missing,
        )
        for entry in index_snapshot.entries.values():
            if str(entry.get("state_id") or "") == state_id:
                return self._snapshot_from_lookup_entry(entry)
        return None

    def get_state_snapshot_by_model_time(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None = None,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> StateSnapshot | None:
        if source_id in (None, ""):
            return None
        index_snapshot = self._load_index_snapshot(
            allow_empty=self.create_missing,
            verify_objects=False,
            enforce_freshness=not self.create_missing,
        )
        entry: dict[str, Any] | None
        if cycle_id not in (None, "") or lead_hours is not None:
            key = self._snapshot_key(
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
                cycle_id=cycle_id,
                lead_hours=lead_hours,
            )
            entry = index_snapshot.entries.get(key)
        else:
            entry = self._first_entry_for_base_key(
                index_snapshot.entries,
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
        if entry is None:
            return None
        return self._snapshot_from_lookup_entry(entry)

    def find_state_snapshot_by_model_time_checksum(
        self,
        *,
        model_id: str,
        valid_time: datetime,
        source_id: str | None,
        checksum: str,
    ) -> StateSnapshot | None:
        if source_id in (None, ""):
            return None
        index_snapshot = self._load_index_snapshot(
            allow_empty=self.create_missing,
            verify_objects=False,
            enforce_freshness=not self.create_missing,
        )
        for entry in sorted(
            self._entries_for_base_key(
                index_snapshot.entries,
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            ),
            key=lambda item: str(item.get("state_id") or ""),
        ):
            if _checksum_matches(entry.get("checksum"), checksum):
                return self._snapshot_from_lookup_entry(entry)
        return None

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        with self._update_lock():
            entries = self._load_entries_for_update()
            key = self._snapshot_key(
                model_id=snapshot.model_id,
                source_id=snapshot.source_id,
                valid_time=snapshot.valid_time,
                cycle_id=snapshot.cycle_id,
                lead_hours=snapshot.lead_hours,
            )
            entry = _state_index_entry_from_snapshot(snapshot)
            self._verify_publish_entry_object(entry, field="entries[].state_uri")
            entries = {
                entry_key: entry_value
                for entry_key, entry_value in entries.items()
                if entry_value.get("state_id") != snapshot.state_id
            }
            entries[key] = entry
            self._publish_entries(entries.values(), verify_objects=False)
            self._clear_index_snapshot_cache()
        return snapshot

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
        with self._update_lock():
            entries = self._load_entries_for_update()
            selected_key: tuple[str, str, str, str, str] | None = None
            selected: dict[str, Any] | None = None
            for key, entry in entries.items():
                if str(entry.get("state_id") or "") == state_id:
                    selected_key = key
                    selected = dict(entry)
                    break
            if selected_key is None or selected is None:
                return None
            selected["usable_flag"] = _require_state_index_bool(
                usable_flag,
                field="usable_flag",
            )
            self._verify_publish_entry_object(selected, field="entries[].state_uri")
            entries[selected_key] = selected
            self._publish_entries(entries.values(), verify_objects=False)
            self._clear_index_snapshot_cache()
            return _state_snapshot_from_index_entry(selected)

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        del model_id, before_time
        raise StateManagerError("Latest usable state fallback is not supported by the file state snapshot index.")

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        snapshots = list(self._load_snapshots_for_lookup().values())
        if model_id is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.model_id == model_id]
        if usable is not None:
            snapshots = [snapshot for snapshot in snapshots if snapshot.usable_flag is usable]
        snapshots.sort(key=lambda snapshot: (snapshot.valid_time, snapshot.state_id), reverse=True)
        page = snapshots[offset : offset + limit]
        return {
            "total_count": len(snapshots),
            "items": [_snapshot_to_dict(snapshot) for snapshot in page],
            "limit": limit,
            "offset": offset,
        }

    def insert_qc_result(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {"status": "recorded_in_state_index", **dict(record)}

    def _snapshot_from_lookup_entry(self, entry: Mapping[str, Any]) -> StateSnapshot:
        if self.create_missing:
            return _state_snapshot_from_index_entry(entry)
        return _state_snapshot_from_index_entry(self._entry_with_verified_object(entry))

    def strict_warm_start_evidence(
        self,
        *,
        model_id: str,
        source_id: str,
        valid_time: datetime,
        model_package_version: str | None = None,
        model_package_checksum: str | None = None,
        required_lead_hours: int = 12,
    ) -> dict[str, Any]:
        try:
            index_snapshot = self._load_index_snapshot(allow_empty=False)
        except StateManagerError as error:
            index_evidence = self._blocked_index_evidence(error)
            return _state_index_unavailable_evidence(
                reason=_first_state_index_blocker_reason(index_evidence) or "state_snapshot_index_unavailable",
                index_evidence=index_evidence,
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
        index_evidence = index_snapshot.evidence
        expected_cycle_id = _expected_state_index_cycle_id(source_id, valid_time, required_lead_hours)
        key = self._snapshot_key(
            model_id=model_id,
            source_id=source_id,
            valid_time=valid_time,
            cycle_id=expected_cycle_id,
            lead_hours=required_lead_hours,
        )
        entry = index_snapshot.entries.get(key)
        if entry is None:
            base_entries = self._entries_for_base_key(
                index_snapshot.entries,
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
            if not base_entries:
                return _state_index_unavailable_evidence(
                    reason="state_snapshot_index_exact_checkpoint_missing",
                    index_evidence=index_evidence,
                    model_id=model_id,
                    source_id=source_id,
                    valid_time=valid_time,
                )
            entry = _best_lineage_candidate_entry(
                base_entries,
                expected_cycle_id=expected_cycle_id,
                required_lead_hours=required_lead_hours,
            )
        try:
            entry = self._entry_with_verified_object(entry)
        except StateManagerError as error:
            index_evidence = {
                **index_evidence,
                "entry_status": "object_unavailable",
                "entry_model_id": str(entry.get("model_id") or ""),
                "entry_source_id": str(entry.get("source_id") or ""),
                "entry_valid_time": str(entry.get("valid_time") or ""),
            }
            return _state_index_unavailable_evidence(
                reason=str(getattr(error, "reason", "state_snapshot_index_object_unreadable")),
                index_evidence=index_evidence,
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
        snapshot = _state_snapshot_from_index_entry(entry)
        if not snapshot.usable_flag:
            return _state_index_unavailable_evidence(
                reason="state_snapshot_index_checkpoint_unusable",
                index_evidence={**index_evidence, "entry_status": "unusable"},
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
        lineage_mismatch = _state_index_lineage_mismatch(
            snapshot,
            model_package_version=model_package_version,
            model_package_checksum=model_package_checksum,
            required_lead_hours=required_lead_hours,
        )
        if lineage_mismatch is not None:
            return _state_index_unavailable_evidence(
                reason=lineage_mismatch,
                index_evidence={**index_evidence, "entry_status": "lineage_mismatch"},
                model_id=model_id,
                source_id=source_id,
                valid_time=valid_time,
            )
        candidate_state = _candidate_state_from_snapshot(snapshot)
        return _state_index_evidence_safe(
            {
                "status": "ready",
                "ready": True,
                "reason": None,
                "candidate_state": candidate_state,
                "state_snapshot_index": {
                    **index_evidence,
                    "entry_status": "ready",
                    "entry_model_id": snapshot.model_id,
                    "entry_source_id": snapshot.source_id,
                    "entry_valid_time": _format_time(snapshot.valid_time),
                    "object_evidence": entry.get("object_evidence"),
                },
            }
        )

    def usable_state_history_evidence(
        self,
        *,
        model_id: str,
        source_id: str,
        before_time: datetime,
    ) -> dict[str, Any]:
        try:
            index_snapshot = self._load_index_snapshot(allow_empty=False)
        except StateManagerError as error:
            index_evidence = self._blocked_index_evidence(error)
            reason = _first_state_index_blocker_reason(index_evidence) or "state_snapshot_index_unavailable"
            return _state_index_evidence_safe(
                {
                    "status": "blocked",
                    "ready": False,
                    "reason": reason,
                    "model_id": model_id,
                    "source_id": source_id,
                    "before_time": _format_time(before_time),
                    "history_exists": None,
                    "state_snapshot_index": index_evidence,
                    "dependency": {
                        "name": "file_state_snapshot_index",
                        "status": "unavailable",
                        "retryable": True,
                    },
                    "failure": {
                        "classifier": "file_state_snapshot_index_unavailable",
                        "reason_code": reason.upper(),
                        "dependency": "file_state_snapshot_index",
                        "retryable": True,
                        "permanent": False,
                    },
                }
            )
        source = _normalize_state_index_source_id(source_id, field="identity.source_id")
        cutoff = _ensure_utc(before_time)
        history_entries = [
            entry
            for key, entry in index_snapshot.entries.items()
            if key[0] == str(model_id)
            and key[1] == source
            and _ensure_utc(_parse_state_index_time(entry["valid_time"], field="valid_time")) < cutoff
            and _require_state_index_bool(entry.get("usable_flag"), field="usable_flag")
        ]
        latest_entry = None
        if history_entries:
            latest_entry = sorted(
                history_entries,
                key=lambda entry: (
                    _ensure_utc(_parse_state_index_time(entry["valid_time"], field="valid_time")),
                    str(entry.get("state_id") or ""),
                ),
                reverse=True,
            )[0]
        latest_state = None
        if latest_entry is not None:
            latest_state = _candidate_state_from_snapshot(_state_snapshot_from_index_entry(latest_entry))
        return _state_index_evidence_safe(
            {
                "status": "ready",
                "ready": True,
                "reason": None,
                "model_id": model_id,
                "source_id": source,
                "before_time": _format_time(cutoff),
                "history_exists": latest_entry is not None,
                "history_entry_count": len(history_entries),
                "latest_usable_state": latest_state,
                "state_snapshot_index": {
                    **index_snapshot.evidence,
                    "history_entry_count": len(history_entries),
                },
            }
        )

    def state_index_evidence(self) -> dict[str, Any]:
        try:
            return dict(self._load_index_snapshot(allow_empty=False).evidence)
        except StateManagerError as error:
            return self._blocked_index_evidence(error)

    def refresh(self) -> None:
        self._clear_index_snapshot_cache()

    def _clear_index_snapshot_cache(self) -> None:
        object.__setattr__(self, "_index_snapshot_cache", None)

    def _snapshot_key(
        self,
        *,
        model_id: str,
        source_id: str | None,
        valid_time: datetime,
        cycle_id: str | None = None,
        lead_hours: int | None = None,
    ) -> tuple[str, str, str, str, str]:
        if source_id in (None, ""):
            raise StateManagerError("source_id is required for file state snapshot index lookups.")
        return _state_index_identity_key(
            model_id=model_id,
            source_id=str(source_id),
            valid_time=valid_time,
            cycle_id=cycle_id,
            lead_hours=lead_hours,
        )

    def _entries_for_base_key(
        self,
        entries: Mapping[tuple[str, str, str, str, str], dict[str, Any]],
        *,
        model_id: str,
        source_id: str,
        valid_time: datetime,
    ) -> list[dict[str, Any]]:
        base_key = _state_index_base_key(model_id=model_id, source_id=source_id, valid_time=valid_time)
        return [entry for key, entry in entries.items() if key[:3] == base_key]

    def _first_entry_for_base_key(
        self,
        entries: Mapping[tuple[str, str, str, str, str], dict[str, Any]],
        *,
        model_id: str,
        source_id: str,
        valid_time: datetime,
    ) -> dict[str, Any] | None:
        matches = self._entries_for_base_key(entries, model_id=model_id, source_id=source_id, valid_time=valid_time)
        if not matches:
            return None
        return sorted(matches, key=lambda entry: str(entry.get("state_id") or ""))[0]

    def _load_snapshots(self) -> dict[tuple[str, str, str, str, str], StateSnapshot]:
        return {key: _state_snapshot_from_index_entry(entry) for key, entry in self._load_entries().items()}

    def _load_snapshots_for_lookup(self) -> dict[tuple[str, str, str, str, str], StateSnapshot]:
        entries = self._load_entries_for_update() if self.create_missing else self._load_entries()
        return {key: _state_snapshot_from_index_entry(entry) for key, entry in entries.items()}

    def _load_entries(self) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
        payload, _content = self._read_payload(allow_empty=False)
        return _validate_state_snapshot_index(
            payload,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
            published_artifact_root=self.published_artifact_root,
            now=self.now,
            max_age_hours=self.max_age_hours,
        )

    def _load_entries_for_update(self) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
        payload, content = self._read_payload(allow_empty=self.create_missing)
        if not payload and not content:
            return {}
        return _validate_state_snapshot_index(
            payload,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
            published_artifact_root=self.published_artifact_root,
            now=self.now,
            max_age_hours=self.max_age_hours,
            verify_objects=False,
            enforce_freshness=False,
        )

    def _publish_entries(self, entries: Sequence[Mapping[str, Any]], *, verify_objects: bool = True) -> None:
        publish_state_snapshot_index(
            list(entries),
            self.index_uri,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
            published_artifact_root=self.published_artifact_root,
            generated_at=self.now,
            verify_objects=verify_objects,
        )

    def _load_index_snapshot(
        self,
        *,
        allow_empty: bool,
        verify_objects: bool = False,
        enforce_freshness: bool = True,
    ) -> _StateIndexSnapshot:
        use_cache = not allow_empty and not verify_objects and enforce_freshness
        cached = self._index_snapshot_cache
        if use_cache and cached is not None:
            return cached
        payload, content = self._read_payload(allow_empty=allow_empty)
        entries: dict[tuple[str, str, str, str, str], dict[str, Any]]
        if not payload and not content and allow_empty:
            entries = {}
        else:
            entries = _validate_state_snapshot_index(
                payload,
                object_store_root=self.object_store_root,
                object_store_prefix=self.object_store_prefix,
                published_artifact_root=self.published_artifact_root,
                now=self.now,
                max_age_hours=self.max_age_hours,
                verify_objects=verify_objects,
                enforce_freshness=enforce_freshness,
            )
        evidence = _state_index_evidence_safe(
            {
                "status": "ready",
                "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
                "index": _state_index_uri_evidence(self.index_uri),
                "generated_at": payload.get("generated_at"),
                "checksum": _safe_checksum(payload.get("checksum")),
                "content_checksum_verified": _checksum_matches(payload.get("checksum"), _payload_checksum(payload))
                if payload
                else False,
                "entry_count": len(entries),
                "index_bytes": len(content),
            }
        )
        snapshot = _StateIndexSnapshot(payload=dict(payload), content=content, entries=entries, evidence=evidence)
        if use_cache:
            object.__setattr__(self, "_index_snapshot_cache", snapshot)
        return snapshot

    def _entry_with_verified_object(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        verified = dict(entry)
        verified["object_evidence"] = self._verify_publish_entry_object(
            verified,
            field="entries[].state_uri",
        )
        return verified

    def _verify_publish_entry_object(self, entry: Mapping[str, Any], *, field: str) -> dict[str, Any]:
        return _verify_state_index_object(
            str(entry["state_uri"]),
            str(entry["checksum"]),
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
            published_artifact_root=self.published_artifact_root,
            field=field,
        )

    def _blocked_index_evidence(self, error: StateManagerError) -> dict[str, Any]:
        return _state_index_evidence_safe(
            {
                "status": "blocked",
                "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
                "index": _state_index_uri_evidence(self.index_uri),
                "blockers": [
                    {
                        "code": str(getattr(error, "reason", "state_snapshot_index_unavailable")),
                        "reason": str(getattr(error, "reason", "state_snapshot_index_unavailable")),
                        "field": str(getattr(error, "field", "index")),
                        "message": "File state snapshot index validation failed closed.",
                    }
                ],
            }
        )

    @contextmanager
    def _update_lock(self) -> Iterator[None]:
        lock_path, containment_root = _state_index_lock_path(
            self.index_uri,
            object_store_root=self.object_store_root,
            object_store_prefix=self.object_store_prefix,
            published_artifact_root=self.published_artifact_root,
        )
        with _exclusive_state_index_lock(lock_path, containment_root=containment_root):
            yield

    def _read_payload(self, *, allow_empty: bool) -> tuple[dict[str, Any], bytes]:
        try:
            content = _read_state_index_bytes(
                self.index_uri,
                object_store_root=self.object_store_root,
                object_store_prefix=self.object_store_prefix,
                published_artifact_root=self.published_artifact_root,
                max_bytes=MAX_STATE_SNAPSHOT_INDEX_BYTES,
            )
        except FileNotFoundError as error:
            if allow_empty:
                return {}, b""
            raise _state_index_error("state_snapshot_index_missing", field="index") from error
        except ObjectStoreError as error:
            if _is_missing_file_error(error):
                if allow_empty:
                    return {}, b""
                raise _state_index_error("state_snapshot_index_missing", field="index") from error
            raise _state_index_error("state_snapshot_index_unreadable", field="index") from error
        except (OSError, SafeFilesystemError, ValueError) as error:
            raise _state_index_error("state_snapshot_index_unreadable", field="index") from error
        if len(content) > MAX_STATE_SNAPSHOT_INDEX_BYTES:
            raise _state_index_error("state_snapshot_index_size_limit_exceeded", field="index")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise _state_index_error("state_snapshot_index_malformed_json", field="index") from error
        if not isinstance(payload, Mapping):
            raise _state_index_error("state_snapshot_index_not_object", field="index")
        return dict(payload), content


def publish_state_snapshot_index(
    entries: Sequence[Mapping[str, Any]],
    destination_uri: str | Path,
    *,
    object_store_root: str | Path | None = None,
    object_store_prefix: str | None = None,
    published_artifact_root: str | Path | None = None,
    generated_at: datetime | None = None,
    verify_objects: bool = True,
) -> dict[str, Any]:
    generated = _ensure_utc(generated_at or datetime.now(tz=UTC))
    payload: dict[str, Any] = {
        "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
        "generated_at": _format_time(generated),
        "entries": [dict(entry) for entry in entries],
    }
    payload["checksum"] = f"sha256:{_payload_checksum(payload)}"
    content = _canonical_json_bytes(payload, pretty=True)
    if len(content) > MAX_STATE_SNAPSHOT_INDEX_BYTES:
        raise _state_index_error(
            "state_snapshot_index_size_limit_exceeded",
            field="index",
            evidence={"index_bytes": len(content), "max_bytes": MAX_STATE_SNAPSHOT_INDEX_BYTES},
        )
    normalized = _validate_state_snapshot_index(
        payload,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        now=generated,
        max_age_hours=DEFAULT_STATE_SNAPSHOT_INDEX_MAX_AGE_HOURS,
        verify_objects=verify_objects,
    )
    _write_state_index_bytes(
        str(destination_uri),
        content,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
    )
    return _state_index_evidence_safe(
        {
            "status": "published",
            "schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
            "destination": _state_index_uri_evidence(destination_uri),
            "checksum": payload["checksum"],
            "generated_at": payload["generated_at"],
            "entry_count": len(normalized),
            "index_last": True,
            "atomic_write": True,
        }
    )


def _validate_state_snapshot_index(
    payload: Mapping[str, Any],
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    now: datetime | None,
    max_age_hours: int,
    verify_objects: bool = True,
    enforce_freshness: bool = True,
) -> dict[tuple[str, str, str, str, str], dict[str, Any]]:
    if payload.get("schema_version") != FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION:
        raise _state_index_error("state_snapshot_index_schema_unsupported", field="schema_version")
    _validate_state_index_json_complexity(payload)
    _require_state_index_checksum(payload)
    generated_at = _parse_state_index_generated_at(
        payload.get("generated_at"),
        now=now,
        max_age_hours=max_age_hours,
        enforce_freshness=enforce_freshness,
    )
    entries_value = payload.get("entries")
    if not isinstance(entries_value, Sequence) or isinstance(entries_value, str | bytes | bytearray):
        raise _state_index_error("state_snapshot_index_entries_invalid", field="entries")
    if len(entries_value) > MAX_STATE_SNAPSHOT_INDEX_ENTRIES:
        raise _state_index_error(
            "state_snapshot_index_entry_limit_exceeded",
            field="entries",
            evidence={"entry_count": len(entries_value), "max_entries": MAX_STATE_SNAPSHOT_INDEX_ENTRIES},
        )
    entries: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    state_ids: set[str] = set()
    for index, item in enumerate(entries_value):
        if not isinstance(item, Mapping):
            raise _state_index_error("state_snapshot_index_entry_not_object", field=f"entries[{index}]")
        entry = _normalize_state_index_entry(
            item,
            index=index,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            verify_object=verify_objects,
        )
        key = _state_index_identity_key(
            model_id=str(entry["model_id"]),
            source_id=str(entry["source_id"]),
            valid_time=_parse_state_index_time(entry["valid_time"], field=f"entries[{index}].valid_time"),
            cycle_id=_optional_str(entry.get("cycle_id")),
            lead_hours=entry.get("lead_hours"),
            field_prefix=f"entries[{index}]",
        )
        if key in entries:
            raise _state_index_error(
                "state_snapshot_index_duplicate_identity",
                field="entries[]",
                evidence={
                    "model_id": key[0],
                    "source_id": key[1],
                    "valid_time": key[2],
                    "cycle_id": key[3],
                    "lead_hours": key[4],
                },
            )
        state_id = str(entry["state_id"])
        if state_id in state_ids:
            raise _state_index_error(
                "state_snapshot_index_duplicate_state_id",
                field="entries[].state_id",
                evidence={"state_id": state_id},
            )
        state_ids.add(state_id)
        entry["index_generated_at"] = _format_time(generated_at)
        entries[key] = entry
    return entries


def _normalize_state_index_entry(
    item: Mapping[str, Any],
    *,
    index: int,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    verify_object: bool,
) -> dict[str, Any]:
    row = dict(item)
    required = ("state_id", "model_id", "run_id", "source_id", "valid_time", "state_uri", "checksum", "usable_flag")
    for field in required:
        if field == "usable_flag":
            if field not in row:
                raise _state_index_error(
                    "state_snapshot_index_required_field_missing",
                    field=f"entries[{index}].{field}",
                )
            continue
        if row.get(field) in (None, ""):
            raise _state_index_error(
                "state_snapshot_index_required_field_missing",
                field=f"entries[{index}].{field}",
            )
    source_id = _normalize_state_index_source_id(row["source_id"], field=f"entries[{index}].source_id")
    valid_time = _ensure_utc(_parse_state_index_time(row["valid_time"], field=f"entries[{index}].valid_time"))
    state_uri = str(row["state_uri"])
    checksum = str(row["checksum"])
    usable_flag = _require_state_index_bool(row.get("usable_flag"), field=f"entries[{index}].usable_flag")
    if verify_object:
        object_evidence = _verify_state_index_object(
            state_uri,
            checksum,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            field=f"entries[{index}].state_uri",
        )
    else:
        _require_supported_state_object_reference(
            state_uri,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            field=f"entries[{index}].state_uri",
        )
        object_evidence = None
    lead_hours = _optional_state_index_int(row.get("lead_hours"), field=f"entries[{index}].lead_hours")
    return {
        **row,
        "state_id": str(row["state_id"]),
        "model_id": str(row["model_id"]),
        "run_id": str(row["run_id"]),
        "source_id": source_id,
        "valid_time": _format_time(valid_time),
        "state_uri": state_uri,
        "checksum": checksum,
        "usable_flag": usable_flag,
        "created_at": _format_time(
            _parse_state_index_time(row["created_at"], field=f"entries[{index}].created_at")
        )
        if row.get("created_at") not in (None, "")
        else None,
        "cycle_id": _optional_str(row.get("cycle_id")),
        "lead_hours": lead_hours,
        "model_package_version": _optional_str(row.get("model_package_version")),
        "model_package_checksum": _optional_str(row.get("model_package_checksum")),
        "original_shud_filename": _optional_str(row.get("original_shud_filename")),
        "object_evidence": object_evidence,
    }


def _state_index_entry_from_snapshot(snapshot: StateSnapshot) -> dict[str, Any]:
    return {
        "state_id": snapshot.state_id,
        "model_id": snapshot.model_id,
        "run_id": snapshot.run_id,
        "valid_time": _format_time(snapshot.valid_time),
        "state_uri": snapshot.state_uri,
        "checksum": snapshot.checksum,
        "usable_flag": snapshot.usable_flag,
        "created_at": _format_time(snapshot.created_at),
        "source_id": snapshot.source_id,
        "cycle_id": snapshot.cycle_id,
        "lead_hours": snapshot.lead_hours,
        "model_package_version": snapshot.model_package_version,
        "model_package_checksum": snapshot.model_package_checksum,
        "original_shud_filename": snapshot.original_shud_filename,
    }


def _state_snapshot_from_index_entry(entry: Mapping[str, Any]) -> StateSnapshot:
    lead_hours = entry.get("lead_hours")
    return StateSnapshot(
        state_id=str(entry["state_id"]),
        model_id=str(entry["model_id"]),
        run_id=str(entry["run_id"]),
        valid_time=_ensure_utc(_parse_state_index_time(entry["valid_time"], field="valid_time")),
        state_uri=str(entry["state_uri"]),
        checksum=str(entry["checksum"]),
        usable_flag=_require_state_index_bool(entry.get("usable_flag"), field="usable_flag"),
        created_at=(
            _ensure_utc(_parse_state_index_time(entry["created_at"], field="created_at"))
            if entry.get("created_at") not in (None, "")
            else None
        ),
        source_id=_optional_str(entry.get("source_id")),
        cycle_id=_optional_str(entry.get("cycle_id")),
        lead_hours=int(lead_hours) if lead_hours is not None else None,
        model_package_version=_optional_str(entry.get("model_package_version")),
        model_package_checksum=_optional_str(entry.get("model_package_checksum")),
        original_shud_filename=_optional_str(entry.get("original_shud_filename")),
    )


def _candidate_state_from_snapshot(snapshot: StateSnapshot) -> dict[str, Any]:
    lineage = {
        "source_id": snapshot.source_id,
        "cycle_id": snapshot.cycle_id,
        "lead_hours": snapshot.lead_hours,
        "model_package_version": snapshot.model_package_version,
        "model_package_checksum": snapshot.model_package_checksum,
        "state_index_schema_version": FILE_STATE_SNAPSHOT_INDEX_SCHEMA_VERSION,
    }
    lineage = {key: value for key, value in lineage.items() if value not in (None, "")}
    valid_time = _format_time(snapshot.valid_time)
    return {
        "state_id": snapshot.state_id,
        "init_state_id": snapshot.state_id,
        "state_uri": snapshot.state_uri,
        "init_state_uri": snapshot.state_uri,
        "checksum": snapshot.checksum,
        "init_state_checksum": snapshot.checksum,
        "valid_time": valid_time,
        "init_state_valid_time": valid_time,
        "usable_flag": snapshot.usable_flag,
        "init_state_quality": "fresh",
        "lineage": lineage,
        "init_state_lineage": lineage,
    }


def _state_index_identity_key(
    *,
    model_id: str,
    source_id: str,
    valid_time: datetime | str,
    cycle_id: str | None,
    lead_hours: Any,
    field_prefix: str = "identity",
) -> tuple[str, str, str, str, str]:
    parsed_valid_time = (
        _parse_state_index_time(valid_time, field="valid_time") if not isinstance(valid_time, datetime) else valid_time
    )
    lead_value = _optional_state_index_int(lead_hours, field=f"{field_prefix}.lead_hours")
    lead_text = "" if lead_value is None else str(lead_value)
    return (
        str(model_id),
        _normalize_state_index_source_id(source_id, field=f"{field_prefix}.source_id"),
        _format_time(_ensure_utc(parsed_valid_time)) or "",
        str(cycle_id or ""),
        lead_text,
    )


def _state_index_base_key(*, model_id: str, source_id: str, valid_time: datetime) -> tuple[str, str, str]:
    return (
        str(model_id),
        _normalize_state_index_source_id(source_id, field="identity.source_id"),
        _format_time(_ensure_utc(valid_time)) or "",
    )


def _expected_state_index_cycle_id(source_id: str, valid_time: datetime, lead_hours: int) -> str:
    producer_cycle_time = _ensure_utc(valid_time) - timedelta(hours=int(lead_hours))
    return cycle_id_for(source_id, producer_cycle_time)


def _best_lineage_candidate_entry(
    entries: Sequence[Mapping[str, Any]],
    *,
    expected_cycle_id: str,
    required_lead_hours: int,
) -> dict[str, Any]:
    ordered = sorted((dict(entry) for entry in entries), key=lambda entry: str(entry.get("state_id") or ""))
    for entry in ordered:
        if entry.get("cycle_id") == expected_cycle_id and entry.get("lead_hours") == required_lead_hours:
            return entry
    for entry in ordered:
        if entry.get("lead_hours") == required_lead_hours:
            return entry
    return ordered[0]


def _state_index_lineage_mismatch(
    snapshot: StateSnapshot,
    *,
    model_package_version: str | None,
    model_package_checksum: str | None,
    required_lead_hours: int,
) -> str | None:
    if snapshot.lead_hours != required_lead_hours:
        return "state_snapshot_index_lead_hours_mismatch"
    expected_cycle_id = _expected_state_index_cycle_id(
        str(snapshot.source_id),
        snapshot.valid_time,
        required_lead_hours,
    )
    if snapshot.cycle_id in (None, ""):
        return "state_snapshot_index_cycle_id_missing"
    if str(snapshot.cycle_id) != expected_cycle_id:
        return "state_snapshot_index_cycle_id_mismatch"
    if (
        model_package_version not in (None, "")
        and (
            snapshot.model_package_version in (None, "")
            or str(snapshot.model_package_version) != str(model_package_version)
        )
    ):
        return "state_snapshot_index_model_package_version_mismatch"
    if snapshot.model_package_checksum in (None, "") or model_package_checksum in (None, ""):
        return "state_snapshot_index_model_package_checksum_missing"
    if not _checksum_matches(snapshot.model_package_checksum, model_package_checksum):
        return "state_snapshot_index_model_package_checksum_mismatch"
    return None


def _state_index_unavailable_evidence(
    *,
    reason: str,
    index_evidence: Mapping[str, Any],
    model_id: str,
    source_id: str,
    valid_time: datetime,
) -> dict[str, Any]:
    return _state_index_evidence_safe(
        {
            "status": "blocked",
            "ready": False,
            "reason": reason,
            "model_id": model_id,
            "source_id": source_id,
            "valid_time": _format_time(valid_time),
            "state_snapshot_index": dict(index_evidence),
            "dependency": {
                "name": "file_state_snapshot_index",
                "status": "unavailable",
                "retryable": True,
            },
            "failure": {
                "classifier": "file_state_snapshot_index_unavailable",
                "reason_code": reason.upper(),
                "dependency": "file_state_snapshot_index",
                "retryable": True,
                "permanent": False,
            },
        }
    )


def _verify_state_index_object(
    uri: str,
    expected_checksum: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    field: str,
) -> dict[str, Any]:
    _require_supported_state_object_reference(
        uri,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        field=field,
    )
    try:
        content = _read_state_object_bytes(
            uri,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            max_bytes=MAX_STATE_IC_BYTES,
        )
    except FileNotFoundError as error:
        raise _state_index_error("state_snapshot_index_object_missing", field=field) from error
    except ObjectStoreError as error:
        if _is_missing_file_error(error):
            raise _state_index_error("state_snapshot_index_object_missing", field=field) from error
        raise _state_index_error("state_snapshot_index_object_unreadable", field=field) from error
    except (OSError, SafeFilesystemError, ValueError) as error:
        raise _state_index_error("state_snapshot_index_object_unreadable", field=field) from error
    actual_checksum = sha256_bytes(content)
    if not _checksum_matches(expected_checksum, actual_checksum):
        raise _state_index_error("state_snapshot_index_object_checksum_mismatch", field=field)
    return _state_index_evidence_safe(
        {
            "exists": True,
            "uri": _state_index_uri_evidence(uri),
            "checksum_verified": True,
            "size_bytes": len(content),
        }
    )


def _require_supported_state_object_reference(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    field: str,
) -> None:
    parsed = urlparse(str(uri))
    scheme = str(parsed.scheme or "").lower()
    if parsed.username or parsed.password or "@" in parsed.netloc or parsed.query or parsed.fragment:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    _require_no_encoded_unsafe_object_key(uri, field=field)
    if scheme == "s3":
        if not (object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", "")).strip():
            raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
        _validate_state_object_key_with_store(
            uri,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            field=field,
        )
        return
    if scheme == "published":
        _validate_state_object_key_with_store(
            _state_index_object_key(uri),
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            source_uri=uri,
            field=field,
        )
        return
    if scheme:
        raise _state_index_error("state_snapshot_index_object_unsupported_uri", field=field)
    if Path(uri).is_absolute() or str(uri).startswith("~"):
        raise _state_index_error("state_snapshot_index_object_unsupported_uri", field=field)
    # Compatibility path: older file indexes may store object-store relative keys.
    # Keep accepting them only after the configured LocalObjectStore proves the key
    # is contained under OBJECT_STORE_ROOT and has no traversal/unsafe components.
    _validate_state_object_key_with_store(
        uri,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
        published_artifact_root=published_artifact_root,
        field=field,
    )


def _validate_state_object_key_with_store(
    key_or_uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    field: str,
    source_uri: str | None = None,
) -> None:
    try:
        store = _state_index_object_store(
            source_uri or key_or_uri,
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
        )
        store.resolve_path(key_or_uri)
    except (ObjectStoreError, ValueError) as error:
        raise _state_index_error(
            "state_snapshot_index_object_unsafe_uri",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error


def _require_no_encoded_unsafe_object_key(uri: str, *, field: str) -> None:
    parsed = urlparse(str(uri))
    if parsed.scheme == "s3":
        candidate = parsed.path.strip("/")
    elif parsed.scheme == "published":
        candidate = _state_index_object_key(str(uri))
    else:
        candidate = str(uri)
    lower = candidate.lower()
    decoded = unquote(candidate)
    if "%2f" in lower or "%5c" in lower or "\x00" in decoded or "\\" in decoded:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    if Path(decoded).is_absolute() or ".." in Path(decoded).parts:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)


def _read_state_index_bytes(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    max_bytes: int,
) -> bytes:
    parsed = urlparse(str(uri))
    if parsed.scheme in {"s3", "published"}:
        path, containment_root = _state_index_control_object_path(
            str(uri),
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            field="index",
        )
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
        if len(content) > max_bytes:
            raise _state_index_error("state_snapshot_index_size_limit_exceeded", field="index")
        return content
    return read_bytes_limited_no_follow(Path(uri).expanduser(), max_bytes=max_bytes)


def _write_state_index_bytes(
    uri: str,
    content: bytes,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
) -> None:
    parsed = urlparse(str(uri))
    if parsed.scheme in {"s3", "published"}:
        path, containment_root = _state_index_control_object_path(
            str(uri),
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
            field="index",
        )
        try:
            atomic_write_bytes_no_follow(path, content, containment_root=containment_root, temp_suffix="part")
        except (OSError, SafeFilesystemError) as error:
            raise _state_index_error(
                "state_snapshot_index_write_failed",
                field="index",
                evidence={"error_type": type(error).__name__},
            ) from error
        return
    try:
        atomic_write_bytes_no_follow(Path(uri).expanduser(), content)
    except (OSError, SafeFilesystemError) as error:
        raise _state_index_error(
            "state_snapshot_index_write_failed",
            field="index",
            evidence={"error_type": type(error).__name__},
        ) from error


def _read_state_object_bytes(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    max_bytes: int,
) -> bytes:
    parsed = urlparse(str(uri))
    if parsed.scheme in {"s3", "published"}:
        return _state_index_object_store(
            str(uri),
            object_store_root=object_store_root,
            object_store_prefix=object_store_prefix,
            published_artifact_root=published_artifact_root,
        ).read_bytes_limited(_state_index_object_key(str(uri)), max_bytes=max_bytes)
    if parsed.scheme:
        raise ValueError("Unsupported state object URI scheme.")
    path = Path(uri)
    if path.is_absolute() or str(uri).startswith("~"):
        raise ValueError("State object URI must be an object URI or object-store relative key.")
    root = object_store_root or os.getenv("OBJECT_STORE_ROOT")
    if root in (None, ""):
        raise ObjectStoreError("OBJECT_STORE_ROOT is required for relative state object URIs.")
    store = LocalObjectStore(
        root,
        object_store_prefix=object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", ""),
    )
    return store.read_bytes_limited(str(uri), max_bytes=max_bytes)


def _state_index_object_store(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
) -> LocalObjectStore:
    parsed = urlparse(str(uri))
    if parsed.scheme == "published":
        root = published_artifact_root or object_store_root or os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT")
        prefix = "published://"
    else:
        root = object_store_root or os.getenv("OBJECT_STORE_ROOT")
        prefix = object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", "")
    if root in (None, ""):
        raise ObjectStoreError("object store root is required for file state index object URI reads")
    return LocalObjectStore(root, object_store_prefix=prefix or "")


def _state_index_control_object_path(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
    field: str,
) -> tuple[Path, Path]:
    parsed = urlparse(str(uri))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    if parsed.scheme == "published":
        root = published_artifact_root or os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT") or object_store_root
        key = _validate_state_index_control_key(_state_index_object_key(uri), field=field, require_public_prefix=True)
    elif parsed.scheme == "s3":
        root = object_store_root or os.getenv("OBJECT_STORE_ROOT")
        key = _state_index_s3_control_key(
            uri,
            object_store_prefix=object_store_prefix or os.getenv("OBJECT_STORE_PREFIX", ""),
            field=field,
        )
    else:
        raise _state_index_error("state_snapshot_index_object_unsupported_uri", field=field)
    if root in (None, ""):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    root_path = Path(root).expanduser()
    root_path = root_path if root_path.is_absolute() else Path.cwd() / root_path
    try:
        ensure_directory_no_follow(root_path)
        target = root_path / key
        target.relative_to(root_path)
    except (OSError, SafeFilesystemError, ValueError) as error:
        raise _state_index_error(
            "state_snapshot_index_object_unsafe_uri",
            field=field,
            evidence={"error_type": type(error).__name__},
        ) from error
    return target, root_path


def _state_index_s3_control_key(
    uri: str,
    *,
    object_store_prefix: str,
    field: str,
) -> str:
    parsed = urlparse(str(uri))
    if parsed.scheme != "s3" or not parsed.netloc:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    raw_key = str(parsed.path or "").lstrip("/")
    target_key = _validate_state_index_control_key(raw_key, field=field, require_public_prefix=False)
    prefix = str(object_store_prefix or "").strip().rstrip("/")
    if not prefix:
        return _validate_state_index_control_key(raw_key, field=field, require_public_prefix=True)
    try:
        prefix_parsed = urlparse(prefix)
    except ValueError as error:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field) from error
    if (
        prefix_parsed.scheme != "s3"
        or not prefix_parsed.netloc
        or prefix_parsed.username
        or prefix_parsed.password
        or prefix_parsed.query
        or prefix_parsed.fragment
        or prefix_parsed.netloc != parsed.netloc
    ):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    prefix_key = str(prefix_parsed.path or "").lstrip("/")
    if not prefix_key:
        return _validate_state_index_control_key(raw_key, field=field, require_public_prefix=True)
    normalized_prefix = _validate_state_index_control_key(prefix_key, field=field, require_public_prefix=False)
    if target_key == normalized_prefix or not target_key.startswith(f"{normalized_prefix}/"):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    return _validate_state_index_control_key(
        target_key[len(normalized_prefix) + 1 :],
        field=field,
        require_public_prefix=False,
    )


def _validate_state_index_control_key(raw_key: str, *, field: str, require_public_prefix: bool) -> str:
    if STATE_INDEX_CONTROL_ENCODED_FORBIDDEN_RE.search(raw_key):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    decoded = unquote(str(raw_key or "").strip("/"))
    if not decoded or "\\" in decoded or any(ord(character) < 32 or ord(character) == 127 for character in decoded):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    parts = PurePosixPath(decoded).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    if require_public_prefix and parts[0] not in STATE_INDEX_CONTROL_OBJECT_PREFIXES:
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    if any(not STATE_INDEX_CONTROL_SEGMENT_RE.fullmatch(part) for part in parts):
        raise _state_index_error("state_snapshot_index_object_unsafe_uri", field=field)
    return "/".join(parts)


def _state_index_object_key(uri: str) -> str:
    parsed = urlparse(str(uri))
    if parsed.scheme == "published":
        return "/".join(part.strip("/") for part in (parsed.netloc, parsed.path) if part.strip("/"))
    return str(uri)


def _state_index_lock_path(
    uri: str,
    *,
    object_store_root: str | Path | None,
    object_store_prefix: str | None,
    published_artifact_root: str | Path | None,
) -> tuple[Path, Path | None]:
    parsed = urlparse(str(uri))
    if parsed.scheme in {"s3", "published"}:
        try:
            index_path, containment_root = _state_index_control_object_path(
                str(uri),
                object_store_root=object_store_root,
                object_store_prefix=object_store_prefix,
                published_artifact_root=published_artifact_root,
                field="index",
            )
        except StateManagerError as error:
            raise _state_index_error(
                "state_snapshot_index_lock_unavailable",
                field="index",
                evidence={"error_type": type(error).__name__},
            ) from error
        return index_path.with_name(f".{index_path.name}.lock"), containment_root
    if parsed.scheme:
        raise _state_index_error("state_snapshot_index_lock_unavailable", field="index")
    return Path(uri).expanduser().with_name(f".{Path(uri).expanduser().name}.lock"), None


@contextmanager
def _exclusive_state_index_lock(lock_path: Path, *, containment_root: Path | None) -> Iterator[None]:
    lock_fd: int | None = None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        ensure_root = containment_root if containment_root is not None else None
        ensure_directory_no_follow(lock_path.parent, containment_root=ensure_root)
        lock_fd = os.open(lock_path, flags, 0o666)
        opened = os.fstat(lock_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SafeFilesystemError(f"State index lock target must be a regular file: {lock_path}")
        current = stat_no_follow(lock_path, containment_root=containment_root)
        if opened.st_dev != current.st_dev or opened.st_ino != current.st_ino:
            raise SafeFilesystemError(f"State index lock target changed while opening: {lock_path}")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    except (OSError, SafeFilesystemError) as error:
        raise _state_index_error(
            "state_snapshot_index_lock_unavailable",
            field="index",
            evidence={"error_type": type(error).__name__},
        ) from error
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _is_missing_file_error(error: BaseException) -> bool:
    cursor: BaseException | None = error
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        if isinstance(cursor, FileNotFoundError):
            return True
        seen.add(id(cursor))
        cursor = cursor.__cause__ or cursor.__context__
    return False


def _parse_state_index_generated_at(
    value: Any,
    *,
    now: datetime | None,
    max_age_hours: int,
    enforce_freshness: bool = True,
) -> datetime:
    generated_at = _parse_state_index_time(value, field="generated_at")
    current = _ensure_utc(now or datetime.now(tz=UTC))
    generated = _ensure_utc(generated_at)
    if generated > current + timedelta(minutes=5):
        raise _state_index_error("state_snapshot_index_generated_at_future", field="generated_at")
    if enforce_freshness and current - generated > timedelta(hours=max(int(max_age_hours), 1)):
        raise _state_index_error(
            "state_snapshot_index_stale",
            field="generated_at",
            evidence={"max_age_hours": int(max_age_hours)},
        )
    return generated


def _parse_state_index_time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError) as error:
        raise _state_index_error("state_snapshot_index_time_invalid", field=field) from error


def _normalize_state_index_source_id(value: Any, *, field: str) -> str:
    try:
        return normalize_source_id(str(value))
    except (TypeError, ValueError) as error:
        raise _state_index_error("state_snapshot_index_source_id_invalid", field=field) from error


def _optional_state_index_int(value: Any, *, field: str) -> int | None:
    if value in (None, ""):
        return None
    if type(value) is bool:
        raise _state_index_error("state_snapshot_index_int_invalid", field=field)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise _state_index_error("state_snapshot_index_int_invalid", field=field)
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[0-9]+", text):
            raise _state_index_error("state_snapshot_index_int_invalid", field=field)
        parsed = int(text)
    else:
        raise _state_index_error("state_snapshot_index_int_invalid", field=field)
    if parsed < 0:
        raise _state_index_error("state_snapshot_index_int_invalid", field=field)
    return parsed


def _require_state_index_bool(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise _state_index_error("state_snapshot_index_usable_flag_invalid", field=field)
    return value


def _require_state_index_checksum(payload: Mapping[str, Any]) -> None:
    checksum = payload.get("checksum")
    if checksum in (None, ""):
        raise _state_index_error("state_snapshot_index_checksum_missing", field="checksum")
    if not _checksum_matches(checksum, _payload_checksum(payload)):
        raise _state_index_error("state_snapshot_index_checksum_mismatch", field="checksum")


def _payload_checksum(payload: Mapping[str, Any]) -> str:
    return sha256_bytes(_canonical_json_bytes({key: value for key, value in payload.items() if key != "checksum"}))


def _canonical_json_bytes(payload: Mapping[str, Any], *, pretty: bool = False) -> bytes:
    if pretty:
        return json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _checksum_matches(expected: Any, actual: Any) -> bool:
    if expected in (None, "") or actual in (None, ""):
        return False
    return _checksum_value(expected) == _checksum_value(actual)


def _checksum_value(value: Any) -> str:
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        return text.split(":", 1)[1]
    return text


def _safe_checksum(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return "sha256:[redacted]" if str(value).startswith("sha256:") else "[redacted]"


def _state_index_uri_evidence(value: str | Path) -> str:
    parsed = urlparse(str(value))
    if parsed.scheme in {"s3", "published"}:
        return "[object-uri]"
    if parsed.scheme:
        return "[uri]"
    if str(value).startswith("/") or str(value).startswith("~"):
        return "[local-path]"
    return str(value)


def _state_index_evidence_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _state_index_evidence_safe(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_state_index_evidence_safe(item) for item in value]
    if isinstance(value, str):
        if value.lower().startswith("sha256:"):
            return value
        parsed = urlparse(value)
        if parsed.scheme in {"s3", "published"}:
            return value
        if parsed.scheme:
            return "[uri]"
        if value.startswith("/") or value.startswith("~"):
            return "[local-path]"
    return value


def _first_state_index_blocker_reason(evidence: Mapping[str, Any]) -> str | None:
    blockers = evidence.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, str | bytes | bytearray) and blockers:
        first = blockers[0]
        if isinstance(first, Mapping):
            return str(first.get("reason") or first.get("code") or "") or None
    return None


def _validate_state_index_json_complexity(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if visited > MAX_STATE_SNAPSHOT_INDEX_JSON_NODES:
            raise _state_index_error(
                "state_snapshot_index_json_node_limit_exceeded",
                field="index",
                evidence={"max_nodes": MAX_STATE_SNAPSHOT_INDEX_JSON_NODES},
            )
        if depth > MAX_STATE_SNAPSHOT_INDEX_JSON_DEPTH:
            raise _state_index_error(
                "state_snapshot_index_json_depth_exceeded",
                field="index",
                evidence={"max_depth": MAX_STATE_SNAPSHOT_INDEX_JSON_DEPTH},
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            stack.extend((child, depth + 1) for child in item)


def _state_index_error(reason: str, *, field: str, evidence: Mapping[str, Any] | None = None) -> StateManagerError:
    error = StateManagerError(reason)
    error.reason = reason  # type: ignore[attr-defined]
    error.field = field  # type: ignore[attr-defined]
    error.evidence = dict(evidence or {})  # type: ignore[attr-defined]
    return error


def state_snapshot_id(
    model_id: str,
    valid_time: datetime,
    *,
    source_id: str | None = None,
    cycle_id: str | None = None,
    lead_hours: int | None = None,
) -> str:
    source_part = f"{_safe_path_component(source_id)}_" if source_id not in (None, "") else ""
    lineage_suffix = _state_lineage_id_suffix(cycle_id=cycle_id, lead_hours=lead_hours)
    return f"state_{source_part}{_safe_path_component(model_id)}_{_ensure_utc(valid_time):%Y%m%d%H}{lineage_suffix}"


def assess_freshness(
    state_valid_time: datetime | None,
    forecast_cycle_time: datetime,
    *,
    soft_threshold_days: int = 7,
    hard_threshold_days: int = 30,
) -> str:
    if state_valid_time is None:
        return "cold_start_no_state"

    age = _ensure_utc(forecast_cycle_time) - _ensure_utc(state_valid_time)
    if age <= timedelta(days=soft_threshold_days):
        return "fresh"
    if age <= timedelta(days=hard_threshold_days):
        return "degraded_stale_init_state"
    return "cold_start_stale_state"


def state_snapshot_to_dict(snapshot: StateSnapshot) -> dict[str, Any]:
    return _snapshot_to_dict(snapshot)


def _state_object_key(
    model_id: str,
    valid_time: datetime,
    *,
    source_id: str | None = None,
    cycle_id: str | None = None,
    lead_hours: int | None = None,
) -> str:
    lineage_path = _state_lineage_path_suffix(cycle_id=cycle_id, lead_hours=lead_hours)
    if source_id not in (None, ""):
        return (
            f"states/{_safe_path_component(source_id)}/{_safe_path_component(model_id)}/"
            f"{_ensure_utc(valid_time):%Y%m%d%H}{lineage_path}/state.cfg.ic"
        )
    return f"states/{_safe_path_component(model_id)}/{_ensure_utc(valid_time):%Y%m%d%H}{lineage_path}/state.cfg.ic"


def _state_lineage_id_suffix(*, cycle_id: str | None, lead_hours: int | None) -> str:
    parts: list[str] = []
    if cycle_id not in (None, ""):
        parts.append(_safe_path_component(cycle_id))
    if lead_hours is not None:
        parts.append(f"f{int(lead_hours):03d}")
    return "_" + "_".join(parts) if parts else ""


def _state_lineage_path_suffix(*, cycle_id: str | None, lead_hours: int | None) -> str:
    parts: list[str] = []
    if cycle_id not in (None, ""):
        parts.append(_safe_path_component(cycle_id))
    if lead_hours is not None:
        parts.append(f"f{int(lead_hours):03d}")
    return "/" + "/".join(parts) if parts else ""


def _qc_record(
    *,
    state_id: str,
    run_id: str | None,
    passed: bool,
    severity: str,
    checks_json: Mapping[str, Any],
    message: str,
) -> dict[str, Any]:
    return {
        "qc_checkpoint": "state_snapshot_integrity",
        "target_type": "state_snapshot",
        "target_id": state_id,
        "run_id": run_id,
        "cycle_id": None,
        "passed": passed,
        "severity": severity,
        "checks_json": dict(checks_json),
        "message": message,
    }


def _snapshot_from_row(row: Mapping[str, Any]) -> StateSnapshot:
    lead_hours = row.get("lead_hours")
    return StateSnapshot(
        state_id=str(row["state_id"]),
        model_id=str(row["model_id"]),
        run_id=str(row["run_id"]),
        valid_time=_ensure_utc(row["valid_time"]),
        state_uri=str(row["state_uri"]),
        checksum=str(row["checksum"]),
        usable_flag=bool(row["usable_flag"]),
        created_at=_ensure_utc(row["created_at"]) if row.get("created_at") is not None else None,
        source_id=_optional_str(row.get("source_id")),
        cycle_id=_optional_str(row.get("cycle_id")),
        lead_hours=int(lead_hours) if lead_hours is not None else None,
        model_package_version=_optional_str(row.get("model_package_version")),
        model_package_checksum=_optional_str(row.get("model_package_checksum")),
        original_shud_filename=_optional_str(row.get("original_shud_filename")),
    )


def _snapshot_to_dict(snapshot: StateSnapshot) -> dict[str, Any]:
    return {
        "state_id": snapshot.state_id,
        "model_id": snapshot.model_id,
        "run_id": snapshot.run_id,
        "valid_time": _format_time(snapshot.valid_time),
        "state_uri": snapshot.state_uri,
        "checksum": snapshot.checksum,
        "usable_flag": snapshot.usable_flag,
        "created_at": _format_time(snapshot.created_at),
        "source_id": snapshot.source_id,
        "cycle_id": snapshot.cycle_id,
        "lead_hours": snapshot.lead_hours,
        "model_package_version": snapshot.model_package_version,
        "model_package_checksum": snapshot.model_package_checksum,
        "original_shud_filename": snapshot.original_shud_filename,
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_path_component(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid path component.")
    if value.startswith("-") or "/" in value or "\\" in value or ".." in value or "\x00" in value:
        raise ValueError("Invalid path component.")
    if _SAFE_PATH_COMPONENT.fullmatch(value) is None:
        raise ValueError("Invalid path component.")
    return value
