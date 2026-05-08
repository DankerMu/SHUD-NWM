from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes


class StateManagerError(RuntimeError):
    """Raised when StateSnapshot operations cannot complete."""


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
        return (
            self.status == other.status
            and self.state_id == other.state_id
            and self.snapshot == other.snapshot
        )


class StateSnapshotRepository(Protocol):
    def get_state_snapshot(self, state_id: str) -> StateSnapshot | None:
        ...

    def get_state_snapshot_by_model_time(self, *, model_id: str, valid_time: datetime) -> StateSnapshot | None:
        ...

    def upsert_state_snapshot(self, snapshot: StateSnapshot) -> StateSnapshot:
        ...

    def set_usable_flag(self, *, state_id: str, usable_flag: bool) -> StateSnapshot | None:
        ...

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        ...

    def list_state_snapshots(
        self,
        *,
        model_id: str | None,
        usable: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        ...

    def insert_qc_result(self, record: Mapping[str, Any]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class StateManager:
    repository: StateSnapshotRepository
    object_store: LocalObjectStore

    @classmethod
    def from_env(cls) -> StateManager:
        workspace_root = Path(os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
        object_store_prefix = os.getenv("OBJECT_STORE_PREFIX", "")
        return cls(
            repository=PsycopgStateSnapshotRepository.from_env(),
            object_store=LocalObjectStore(workspace_root, object_store_prefix=object_store_prefix),
        )

    def save_state_snapshot(
        self,
        *,
        model_id: str,
        run_id: str,
        valid_time: datetime,
        ic_file_path: Path | str,
    ) -> StateSnapshotSaveResult:
        parsed_valid_time = _ensure_utc(valid_time)
        state_id = state_snapshot_id(model_id, parsed_valid_time)
        path = Path(ic_file_path)
        try:
            content = path.read_bytes()
        except OSError as error:
            raise StateManagerError(f"Failed to read state snapshot file {path}: {error}") from error

        checksum = sha256_bytes(content)
        existing = self.repository.get_state_snapshot_by_model_time(
            model_id=model_id,
            valid_time=parsed_valid_time,
        )
        if existing is not None and existing.checksum == checksum:
            return StateSnapshotSaveResult(status="already_done", state_id=existing.state_id, snapshot=existing)

        state_key = _state_object_key(model_id, parsed_valid_time)
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
        )
        saved = self.repository.upsert_state_snapshot(snapshot)
        status = "superseded" if existing is not None else "created"
        return StateSnapshotSaveResult(status=status, state_id=saved.state_id, snapshot=saved)

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

            actual_checksum = self.object_store.checksum(snapshot.state_uri)
            checks["actual_checksum"] = actual_checksum
            checks["checksum_matches"] = actual_checksum == snapshot.checksum
            if actual_checksum != snapshot.checksum:
                checks.update({
                    "passed": False,
                    "error_code": "STATE_CHECKSUM_MISMATCH",
                    "message": "State checksum mismatch.",
                })
                return checks
        except (OSError, ObjectStoreError, ValueError) as error:
            checks.update({
                "passed": False,
                "error_code": "STATE_OBJECT_ERROR",
                "message": str(error),
            })
            return checks

        checks.update({"passed": True, "error_code": None, "message": "State snapshot QC passed."})
        return checks


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

    def get_state_snapshot_by_model_time(self, *, model_id: str, valid_time: datetime) -> StateSnapshot | None:
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
                usable_flag
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (model_id, valid_time) DO UPDATE SET
                state_id = EXCLUDED.state_id,
                run_id = EXCLUDED.run_id,
                state_uri = EXCLUDED.state_uri,
                checksum = EXCLUDED.checksum,
                usable_flag = false,
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


def state_snapshot_id(model_id: str, valid_time: datetime) -> str:
    return f"state_{_safe_path_component(model_id)}_{_ensure_utc(valid_time):%Y%m%d%H}"


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


def _state_object_key(model_id: str, valid_time: datetime) -> str:
    return f"states/{_safe_path_component(model_id)}/{_ensure_utc(valid_time):%Y%m%d%H}/state.cfg.ic"


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
    return StateSnapshot(
        state_id=str(row["state_id"]),
        model_id=str(row["model_id"]),
        run_id=str(row["run_id"]),
        valid_time=_ensure_utc(row["valid_time"]),
        state_uri=str(row["state_uri"]),
        checksum=str(row["checksum"]),
        usable_flag=bool(row["usable_flag"]),
        created_at=_ensure_utc(row["created_at"]) if row.get("created_at") is not None else None,
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
    }


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
