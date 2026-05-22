from __future__ import annotations

import csv
import io
import json
import os
import re
import stat as stat_module
import subprocess
import sys
import tarfile
import time
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_no_follow,
    stat_no_follow,
    unlink_no_follow,
)
from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager, StateSnapshot, assess_freshness


class SHUDRuntimeError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class SHUDRuntimeConfig:
    workspace_root: Path | str
    object_store_root: Path | str
    object_store_prefix: str = ""
    shud_executable: str = "shud_omp"
    command_style: str = "cfg"
    output_interval_minutes: int = 1440
    timeout_seconds: int = 3600
    upload_retries: int = 3
    dry_run: bool = False
    executable: InitVar[str | None] = None

    def __post_init__(self, executable: str | None) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())
        if executable is not None and self.shud_executable == "shud_omp":
            object.__setattr__(self, "shud_executable", executable)

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> SHUDRuntimeConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            workspace_root=workspace_root,
            object_store_root=os.getenv("OBJECT_STORE_ROOT", workspace_root),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            shud_executable=os.getenv("SHUD_EXECUTABLE", "shud_omp"),
            command_style=os.getenv("SHUD_COMMAND_STYLE", "cfg"),
            output_interval_minutes=int(os.getenv("MODEL_OUTPUT_INTERVAL", "1440")),
            timeout_seconds=int(os.getenv("SHUD_TIMEOUT_SECONDS", "3600")),
            dry_run=dry_run,
        )


@dataclass(frozen=True)
class SHUDExecutionResult:
    run_id: str
    status: str
    output_uri: str | None
    log_uri: str | None
    rivqdown_file: str | None


class DryRunHydroRunRepository:
    """Repository used only when the CLI is invoked with --dry-run."""

    def create_run(self, _manifest: dict[str, Any], _run_manifest_uri: str) -> dict[str, Any]:
        return {}

    def update_status(self, _run_id: str, _status: str, **_fields: Any) -> dict[str, Any]:
        return {}

    def mark_failed(self, _run_id: str, _error_code: str, _error_message: str, **_fields: Any) -> dict[str, Any]:
        return {}

    def update_init_state(self, _run_id: str, _init_state_id: str | None) -> dict[str, Any]:
        return {}


@dataclass(frozen=True)
class PsycopgHydroRunRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgHydroRunRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise SHUDRuntimeError("DATABASE_URL_MISSING", "DATABASE_URL is required for hydro_run updates.")
        return cls(database_url)

    def create_run(self, manifest: dict[str, Any], run_manifest_uri: str) -> dict[str, Any]:
        inserted = self._fetch_optional(
            """
            INSERT INTO hydro.hydro_run (
                run_id,
                run_type,
                scenario_id,
                model_id,
                basin_version_id,
                forcing_version_id,
                init_state_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                status,
                run_manifest_uri
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'created', %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                error_code = NULL,
                error_message = NULL,
                output_uri = NULL,
                log_uri = NULL,
                updated_at = now()
            WHERE hydro.hydro_run.status IN ('failed', 'cancelled', 'pending')
            RETURNING *
            """,
            (
                manifest["run_id"],
                manifest.get("run_type", "forecast"),
                manifest.get("scenario_id", "forecast_gfs_deterministic"),
                manifest["model"]["model_id"],
                manifest["model"]["basin_version_id"],
                manifest.get("forcing", {}).get("forcing_version_id"),
                _initial_state_id(manifest),
                manifest.get("source_id"),
                _parse_time_or_none(manifest.get("cycle_time")),
                _parse_time(manifest["start_time"]),
                _parse_time(manifest["end_time"]),
                run_manifest_uri,
            ),
        )
        if inserted is not None:
            return inserted

        existing = self._fetch_optional(
            """
            SELECT *
            FROM hydro.hydro_run
            WHERE run_id = %s
              AND status IN ('created', 'staged', 'submitted', 'running', 'pending')
            """,
            (manifest["run_id"],),
        )
        if existing is not None:
            return existing
        raise SHUDRuntimeError("HYDRO_RUN_NOT_RETRIABLE", "hydro_run already exists with a non-retriable status.")

    def update_status(self, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        assignments = ["status = %s", "updated_at = now()"]
        parameters: list[Any] = [status]
        for column in ("slurm_job_id", "output_uri", "log_uri"):
            if fields.get(column) is not None:
                assignments.append(f"{column} = %s")
                parameters.append(fields[column])
        parameters.append(run_id)
        return self._fetch_one(
            f"""
            UPDATE hydro.hydro_run
            SET {", ".join(assignments)}
            WHERE run_id = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def mark_failed(self, run_id: str, error_code: str, error_message: str, **fields: Any) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE hydro.hydro_run
            SET status = 'failed',
                error_code = %s,
                error_message = %s,
                output_uri = COALESCE(%s, output_uri),
                log_uri = COALESCE(%s, log_uri),
                updated_at = now()
            WHERE run_id = %s
            RETURNING *
            """,
            (error_code, error_message, fields.get("output_uri"), fields.get("log_uri"), run_id),
        )

    def update_init_state(self, run_id: str, init_state_id: str | None) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE hydro.hydro_run
            SET init_state_id = %s,
                updated_at = now()
            WHERE run_id = %s
            RETURNING *
            """,
            (init_state_id, run_id),
        )

    def _fetch_one(
        self,
        statement: str,
        parameters: tuple[Any, ...],
        *,
        missing_error_code: str = "HYDRO_RUN_NOT_FOUND",
        missing_message: str = "hydro_run update did not return a row.",
    ) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise SHUDRuntimeError(missing_error_code, missing_message)
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as error:
            raise SHUDRuntimeError("PSYCOPG2_MISSING", "psycopg2 is required for hydro_run updates.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(statement, parameters)
                row = cursor.fetchone()
                connection.commit()
                return dict(row) if row is not None else None
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise SHUDRuntimeError("HYDRO_RUN_DB_ERROR", f"hydro_run database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()


class SHUDRuntime:
    def __init__(
        self,
        *,
        config: SHUDRuntimeConfig,
        repository: Any | None = None,
        object_store: LocalObjectStore | None = None,
        state_manager: StateManager | None = None,
    ) -> None:
        self.config = config
        self.object_store = object_store or LocalObjectStore(config.object_store_root, config.object_store_prefix)
        if repository is not None:
            self.repository = repository
        elif config.dry_run:
            self.repository = DryRunHydroRunRepository()
        else:
            self.repository = PsycopgHydroRunRepository.from_env()
        if state_manager is not None:
            self.state_manager = state_manager
        elif config.dry_run or repository is not None:
            self.state_manager = None
        else:
            self.state_manager = StateManager(
                repository=PsycopgStateSnapshotRepository.from_env(),
                object_store=self.object_store,
            )

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> SHUDRuntime:
        config = SHUDRuntimeConfig.from_env(dry_run=dry_run)
        return cls(config=config)

    def execute_manifest_path(self, manifest_path: str | Path) -> SHUDExecutionResult:
        manifest = json.loads(_read_text_no_follow(Path(manifest_path), containment_root=Path(manifest_path).parent))
        return self.execute(manifest)

    def execute(self, manifest: dict[str, Any]) -> SHUDExecutionResult:
        _validate_manifest_path_components(manifest)
        run_id = _safe_path_component(manifest["run_id"])
        run_manifest_uri = _run_manifest_uri(manifest)
        self.repository.create_run(manifest, run_manifest_uri)

        workspace = Path(self.config.workspace_root) / "runs" / run_id
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        log_dir = workspace / "logs"
        ensure_directory_no_follow(Path(self.config.workspace_root))
        for directory in (input_dir, output_dir, log_dir):
            _ensure_directory(directory, containment_root=Path(self.config.workspace_root))
        _write_text_no_follow(
            input_dir / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
            containment_root=input_dir,
        )

        try:
            self.prepare_workspace(manifest, input_dir)
            self._persist_manifest(manifest, input_dir)
            cfg_path = self.generate_cfg_para(manifest, input_dir, output_dir)
            self.repository.update_status(run_id, "staged")
            self.repository.update_status(run_id, "running")
            self.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)
            rivqdown_file = self.verify_output(manifest, output_dir)
            uris = self.upload_results(run_id, output_dir, log_dir)
            self.repository.update_status(
                run_id,
                "succeeded",
                output_uri=uris["output_uri"],
                log_uri=uris["log_uri"],
            )
            return SHUDExecutionResult(run_id, "succeeded", uris["output_uri"], uris["log_uri"], str(rivqdown_file))
        except Exception as error:
            runtime_error = _as_runtime_error(error)
            self._write_failure_log(log_dir, runtime_error)
            log_uri = None
            try:
                log_uri = self.upload_logs(run_id, log_dir)
            except Exception:
                log_uri = None
            self.repository.mark_failed(
                run_id,
                runtime_error.error_code,
                runtime_error.message,
                log_uri=log_uri,
            )
            raise runtime_error from error

    def prepare_workspace(self, manifest: dict[str, Any], input_dir: Path) -> None:
        _ensure_directory(input_dir)
        model_input_dir = _model_input_dir(manifest, input_dir, self.config.command_style)
        _ensure_directory(model_input_dir, containment_root=input_dir)
        self._stage_artifact(manifest["model"]["model_package_uri"], model_input_dir)
        self._verify_forcing_manifest_checksums(manifest)
        self._stage_artifact(manifest["forcing"]["forcing_uri"], model_input_dir)
        self._verify_staged_forcing_checksums(manifest, model_input_dir)
        self._stage_initial_state(manifest, model_input_dir)
        if _is_shud_project_mode(manifest, self.config.command_style):
            self._prepare_shud_project_forcing(manifest, model_input_dir)

        for suffix in (".mesh", ".para", ".calib", ".tsd.forc"):
            matches = _find_regular_files(model_input_dir, suffix=suffix, non_empty=True)
            if not matches:
                raise SHUDRuntimeError("WORKSPACE_INCOMPLETE", f"Missing required staged file: *{suffix}")
        if _initial_state_uri(manifest):
            matches = _find_regular_files(model_input_dir, pattern="*.cfg.ic", non_empty=True)
            if not matches:
                raise SHUDRuntimeError(
                    "INIT_STATE_INCOMPLETE",
                    "Initial state URI did not stage a non-empty *.cfg.ic file.",
                )

    def generate_cfg_para(self, manifest: dict[str, Any], input_dir: Path, output_dir: Path) -> Path:
        model_input_dir = _model_input_dir(manifest, input_dir, self.config.command_style)
        template_path = _first_file(model_input_dir, "*.cfg.para") or _first_file(model_input_dir, "*.para")
        if template_path is None:
            raise SHUDRuntimeError("CFG_TEMPLATE_MISSING", "No .para template found in staged model package.")

        output_path = model_input_dir / f"{_project_name(manifest)}.cfg.para"
        content = _read_text_no_follow(template_path, containment_root=model_input_dir)

        init_mode = _init_mode(manifest)
        replacements = _cfg_replacements(manifest, output_dir, init_mode, self.config.output_interval_minutes)
        if _is_shud_project_mode(manifest, self.config.command_style):
            content = "\n".join(line for line in content.splitlines() if "BINARY_OUTPUT" not in line)
            replacements.update(
                {
                    "START": str(_shud_start_day(manifest)),
                    "END": str(_shud_end_day(manifest)),
                    "ASCII_OUTPUT": "1",
                    "BINARY_OUTPUT": "0",
                    "DT_QR_DOWN": str(_output_interval_minutes(manifest, self.config.output_interval_minutes)),
                    "SCR_INTV": str(_output_interval_minutes(manifest, self.config.output_interval_minutes)),
                }
            )
        else:
            content = "\n".join(line for line in content.splitlines() if ".cfg.ic" not in line)
        for key, value in replacements.items():
            content = _replace_or_append(
                content,
                key,
                value,
                separator="\t" if _is_shud_project_mode(manifest, self.config.command_style) else " = ",
            )
        _write_text_no_follow(output_path, content.rstrip() + "\n", containment_root=model_input_dir)
        return output_path

    def run_shud(
        self,
        manifest: dict[str, Any],
        cfg_path: Path,
        workspace: Path,
        output_dir: Path,
        log_dir: Path,
    ) -> None:
        command = _runtime_command(
            self.config.shud_executable,
            cfg_path,
            manifest=manifest,
            output_dir=output_dir,
            command_style=self.config.command_style,
            output_interval_minutes=self.config.output_interval_minutes,
        )
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            _write_text_no_follow(
                log_dir / "shud_stdout.log",
                _subprocess_output_text(error.stdout),
                containment_root=log_dir,
            )
            _write_text_no_follow(
                log_dir / "shud_stderr.log",
                _subprocess_output_text(error.stderr),
                containment_root=log_dir,
            )
            message = f"SHUD executable timed out after {self.config.timeout_seconds}s"
            raise SHUDRuntimeError("SHUD_TIMEOUT", message) from error

        _write_text_no_follow(log_dir / "shud_stdout.log", completed.stdout or "", containment_root=log_dir)
        _write_text_no_follow(log_dir / "shud_stderr.log", completed.stderr or "", containment_root=log_dir)
        if completed.returncode != 0:
            detail = _last_lines(completed.stderr or "", 50)
            raise SHUDRuntimeError(
                f"SHUD_EXIT_{completed.returncode}",
                f"SHUD executable exited with code {completed.returncode}: {detail}",
            )
        _ensure_directory(output_dir)

    def _prepare_shud_project_forcing(self, manifest: dict[str, Any], model_input_dir: Path) -> None:
        if self._stage_standard_shud_forcing(manifest, model_input_dir):
            return
        source = model_input_dir / "forcing_debug.csv"
        if not _regular_file_exists(source, containment_root=model_input_dir):
            source = model_input_dir / "forcing.tsd.forc"
        if not _regular_file_exists(source, containment_root=model_input_dir):
            raise SHUDRuntimeError(
                "FORCING_DEBUG_MISSING",
                "SHUD project mode requires forcing_debug.csv or internal forcing.tsd.forc in the forcing package.",
            )
        rows = _read_internal_forcing_rows(source)
        if not rows:
            raise SHUDRuntimeError("FORCING_EMPTY", f"No forcing rows found in {source}.")
        station = _shud_forcing_station(manifest)
        _write_shud_forcing_files(
            model_input_dir,
            rows,
            station=station,
            project_name=_project_name(manifest),
        )

    def _stage_standard_shud_forcing(self, manifest: dict[str, Any], model_input_dir: Path) -> bool:
        shud_dir = model_input_dir / "shud"
        source_tsd = shud_dir / "qhh.tsd.forc"
        if not _regular_file_exists(source_tsd, containment_root=model_input_dir):
            return False
        rows = _read_shud_forcing_station_rows(source_tsd)
        if not rows:
            raise SHUDRuntimeError("SHUD_FORCING_STATIONS_EMPTY", f"No stations found in {source_tsd}.")
        first_csv = shud_dir / str(rows[0]["filename"])
        first_time = _first_shud_forcing_time(first_csv)
        _shift_project_time_inputs(model_input_dir, _project_name(manifest), first_time)
        start_date = first_time.strftime("%Y%m%d")
        target_tsd = model_input_dir / f"{_project_name(manifest)}.tsd.forc"
        output_lines = [
            f"{len(rows)} {start_date}",
            str(model_input_dir),
            "ID\tLon\tLat\tX\tY\tZ\tFilename",
        ]
        for row in rows:
            filename = str(row["filename"])
            source_csv = shud_dir / filename
            if not _regular_file_exists(source_csv, containment_root=model_input_dir):
                raise SHUDRuntimeError("SHUD_FORCING_CSV_MISSING", f"Missing SHUD forcing CSV: {source_csv}")
            _copy_staged_file_no_follow(source_csv, model_input_dir / filename, root=model_input_dir)
            output_lines.append(
                "\t".join(
                    [
                        str(row["id"]),
                        _format_float(float(row["longitude"])),
                        _format_float(float(row["latitude"])),
                        _format_float(float(row["x"])),
                        _format_float(float(row["y"])),
                        _format_float(float(row["z"])),
                        filename,
                    ]
                )
            )
        _write_text_no_follow(target_tsd, "\n".join(output_lines) + "\n", containment_root=model_input_dir)
        return True

    def verify_output(self, manifest: dict[str, Any], output_dir: Path) -> Path:
        files = sorted(
            path
            for path in _find_regular_files(output_dir)
            if path.name.endswith((".rivqdown", ".rivqdown.csv"))
        )
        if not files:
            raise SHUDRuntimeError(
                "OUTPUT_MISSING",
                "Output verification failed: .rivqdown file not found",
            )
        rivqdown = files[0]
        lines = [
            line.strip()
            for line in _read_staged_bytes(rivqdown, root=output_dir).decode("utf-8").splitlines()
            if line.strip()
        ]
        if not lines:
            raise SHUDRuntimeError("OUTPUT_EMPTY", "Output verification failed: .rivqdown file is empty")

        expected_columns = _segment_count(manifest) + 1
        data_lines = _rivqdown_data_lines(lines)
        expected_timesteps = _expected_timesteps(manifest, self.config.output_interval_minutes)
        if len(data_lines) != expected_timesteps:
            raise SHUDRuntimeError(
                "OUTPUT_ROW_COUNT_MISMATCH",
                f"Output verification failed: expected {expected_timesteps} data rows, found {len(data_lines)}",
            )
        for line_number, line in enumerate(data_lines, start=1):
            column_count = len(_split_row(line))
            if column_count != expected_columns:
                raise SHUDRuntimeError(
                    "OUTPUT_COLUMN_COUNT_MISMATCH",
                    "Output verification failed: "
                    f"line {line_number} expected {expected_columns} columns, found {column_count}",
                )
        return rivqdown

    def upload_results(self, run_id: str, output_dir: Path, log_dir: Path) -> dict[str, str]:
        run_id = _safe_path_component(run_id)
        self._upload_directory(output_dir, f"runs/{run_id}/output")
        log_uri = self.upload_logs(run_id, log_dir)
        return {
            "output_uri": self._directory_uri(f"runs/{run_id}/output"),
            "log_uri": log_uri,
        }

    def upload_logs(self, run_id: str, log_dir: Path) -> str:
        run_id = _safe_path_component(run_id)
        self._upload_directory(log_dir, f"runs/{run_id}/logs")
        return self._directory_uri(f"runs/{run_id}/logs")

    def _persist_manifest(self, manifest: dict[str, Any], input_dir: Path) -> None:
        content = json.dumps(manifest, indent=2, sort_keys=True)
        _write_text_no_follow(input_dir / "manifest.json", content, containment_root=input_dir)
        run_manifest_uri = _run_manifest_uri(manifest)
        self.object_store.write_bytes_atomic(run_manifest_uri, content.encode("utf-8"))

    def _stage_initial_state(self, manifest: dict[str, Any], input_dir: Path) -> None:
        if not _initial_state_id(manifest) and not _initial_state_uri(manifest):
            if str(manifest.get("runtime", {}).get("init_mode", "")) == "3":
                packaged_states = _find_regular_files(input_dir, pattern="*.cfg.ic", non_empty=True)
                if packaged_states:
                    manifest["initial_state"] = {
                        "state_id": manifest.get("runtime", {}).get("packaged_init_state_id", "packaged_initial_state"),
                        "ic_file_uri": None,
                        "valid_time": manifest.get("start_time"),
                        "checksum": sha256_bytes(_read_staged_bytes(packaged_states[0], root=input_dir)),
                        "quality": _initial_state_quality(manifest) or "packaged_calibrated_state",
                    }
                    _set_runtime_init_mode(manifest, 3)
                    self._sync_init_state_id(manifest)
                    return
            _set_cold_start_initial_state(manifest, quality=_initial_state_quality(manifest) or "cold_start_no_state")
            self._sync_init_state_id(manifest)
            return
        if str(manifest.get("runtime", {}).get("init_mode", "")) == "3" and not _initial_state_uri(manifest):
            packaged_states = _find_regular_files(input_dir, pattern="*.cfg.ic", non_empty=True)
            if packaged_states:
                manifest["initial_state"] = {
                    "state_id": _initial_state_id(manifest) or "packaged_initial_state",
                    "ic_file_uri": None,
                    "valid_time": manifest.get("start_time"),
                    "checksum": sha256_bytes(_read_staged_bytes(packaged_states[0], root=input_dir)),
                    "quality": _initial_state_quality(manifest) or "packaged_calibrated_state",
                }
                _set_runtime_init_mode(manifest, 3)
                self._sync_init_state_id(manifest)
                return

        rejected_state_ids: set[str] = set()
        before_time = _parse_time(manifest.get("cycle_time") or manifest["start_time"])
        while True:
            state_id = _initial_state_id(manifest)
            state_uri = _initial_state_uri(manifest)
            expected_checksum = _initial_state_checksum(manifest)
            snapshot = self._state_snapshot(state_id)
            if snapshot is not None:
                state_uri = snapshot.state_uri
                expected_checksum = snapshot.checksum
                _set_initial_state_from_snapshot(
                    manifest,
                    snapshot,
                    quality=_initial_state_quality(manifest) or "fresh",
                )

            if not state_uri:
                _set_cold_start_initial_state(manifest, quality="cold_start_no_state")
                self._sync_init_state_id(manifest)
                return

            self._clear_staged_initial_states(input_dir)
            staged_path, actual_checksum, error_message = self._stage_and_checksum_initial_state(state_uri, input_dir)
            if staged_path is not None and (expected_checksum is None or actual_checksum == expected_checksum):
                _set_runtime_init_mode(manifest, 3)
                self._sync_init_state_id(manifest)
                return

            message = error_message or "Initial state checksum mismatch."
            if state_id:
                rejected_state_ids.add(state_id)
                self._mark_init_state_corrupted(
                    state_id,
                    message=message,
                    actual_checksum=actual_checksum,
                    expected_checksum=expected_checksum,
                )

            next_state = self._next_usable_state(manifest, before_time, rejected_state_ids)
            if next_state is None:
                self._clear_staged_initial_states(input_dir)
                _set_cold_start_initial_state(manifest, quality="cold_start_no_state")
                self._sync_init_state_id(manifest)
                return
            _set_initial_state_from_snapshot(
                manifest,
                next_state,
                quality=assess_freshness(next_state.valid_time, before_time),
            )

    def _state_snapshot(self, state_id: str | None) -> StateSnapshot | None:
        if state_id is None or self.state_manager is None:
            return None
        get_state_snapshot = getattr(self.state_manager, "get_state_snapshot", None)
        if not callable(get_state_snapshot):
            return None
        try:
            return get_state_snapshot(state_id)
        except Exception as error:
            message = f"Failed to load initial state {state_id}: {error}"
            raise SHUDRuntimeError("STATE_LOOKUP_FAILED", message) from error

    def _stage_and_checksum_initial_state(
        self,
        state_uri: str,
        input_dir: Path,
    ) -> tuple[Path | None, str | None, str | None]:
        try:
            self._stage_artifact(state_uri, input_dir)
        except SHUDRuntimeError as error:
            return None, None, error.message

        matches = _find_regular_files(input_dir, pattern="*.cfg.ic", non_empty=True)
        if not matches:
            return None, None, "Initial state URI did not stage a non-empty *.cfg.ic file."

        staged_path = matches[0]
        try:
            return staged_path, sha256_bytes(_read_staged_bytes(staged_path, root=input_dir)), None
        except SHUDRuntimeError as error:
            return None, None, error.message
        except OSError as error:
            return None, None, f"Failed to read staged initial state {staged_path}: {error}"

    def _mark_init_state_corrupted(
        self,
        state_id: str,
        *,
        message: str,
        actual_checksum: str | None,
        expected_checksum: str | None,
    ) -> None:
        if self.state_manager is None:
            return
        mark_corrupted = getattr(self.state_manager, "mark_init_state_corrupted", None)
        if not callable(mark_corrupted):
            return
        mark_corrupted(
            state_id,
            message=message,
            actual_checksum=actual_checksum,
            expected_checksum=expected_checksum,
        )

    def _next_usable_state(
        self,
        manifest: dict[str, Any],
        before_time: datetime,
        rejected_state_ids: set[str],
    ) -> StateSnapshot | None:
        if self.state_manager is None:
            return None

        model_id = manifest["model"]["model_id"]
        cursor_time = before_time
        while True:
            state = self.state_manager.get_latest_usable_state(model_id=model_id, before_time=cursor_time)
            if state is None:
                return None
            if state.state_id not in rejected_state_ids:
                return state
            cursor_time = state.valid_time - timedelta(microseconds=1)

    def _clear_staged_initial_states(self, input_dir: Path) -> None:
        for path in _find_regular_files(input_dir, pattern="*.cfg.ic"):
            try:
                unlink_no_follow(path, containment_root=input_dir, missing_ok=True)
            except SafeFilesystemError as error:
                raise SHUDRuntimeError(
                    "WORKSPACE_PATH_UNSAFE",
                    f"Unsafe staged initial state path {path}: {error}",
                ) from error

    def _sync_init_state_id(self, manifest: dict[str, Any]) -> None:
        update_init_state = getattr(self.repository, "update_init_state", None)
        if callable(update_init_state):
            update_init_state(manifest["run_id"], _initial_state_id(manifest))

    def _stage_artifact(self, uri: str, destination: Path) -> None:
        source = self._object_store_path(uri)
        try:
            source_stat = stat_no_follow(source, containment_root=Path(self.config.object_store_root))
        except FileNotFoundError:
            raise SHUDRuntimeError("ARTIFACT_NOT_FOUND", f"Object storage artifact not found: {uri}")
        except SafeFilesystemError as error:
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Unsafe object storage artifact {uri}: {error}") from error

        _ensure_directory(destination)
        if stat_module.S_ISDIR(source_stat.st_mode):
            self._stage_directory_artifact(source, destination)
            return

        if not stat_module.S_ISREG(source_stat.st_mode):
            raise SHUDRuntimeError(
                "ARTIFACT_UNSAFE",
                f"Object storage artifact must be a regular file or directory: {uri}",
            )

        content = self._read_object_artifact_bytes(source, uri)
        if _stage_tar_artifact_bytes(content, destination):
            return

        target = destination / source.name
        _write_staged_bytes(target, content, root=destination)

    def _stage_directory_artifact(self, source: Path, destination: Path) -> None:
        object_root = Path(self.config.object_store_root)
        for source_file in _iter_regular_descendant_files_no_follow(source, containment_root=object_root):
            relative = source_file.relative_to(source)
            content = self._read_object_artifact_bytes(source_file, str(source_file))
            _write_staged_bytes(destination / relative, content, root=destination)

    def _read_object_artifact_bytes(self, source: Path, label: str) -> bytes:
        try:
            return read_bytes_no_follow(source, containment_root=Path(self.config.object_store_root))
        except SafeFilesystemError as error:
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Unsafe object storage artifact {label}: {error}") from error
        except OSError as error:
            raise SHUDRuntimeError(
                "ARTIFACT_READ_FAILED",
                f"Failed to read object storage artifact {label}: {error}",
            ) from error

    def _verify_forcing_manifest_checksums(self, manifest: dict[str, Any]) -> None:
        forcing = manifest.get("forcing") or {}
        package_manifest_uri = str(forcing.get("package_manifest_uri") or "").strip()
        expected_package_checksum = str(forcing.get("package_manifest_checksum") or "").strip()
        if package_manifest_uri or expected_package_checksum:
            if not package_manifest_uri or not expected_package_checksum:
                raise SHUDRuntimeError(
                    "FORCING_CHECKSUM_MISSING",
                    "Forcing package manifest URI and checksum must be provided together.",
                )
            actual_checksum = self._object_checksum(package_manifest_uri)
            if actual_checksum != expected_package_checksum:
                raise SHUDRuntimeError(
                    "FORCING_PACKAGE_CHECKSUM_MISMATCH",
                    "Forcing package manifest checksum mismatch: "
                    f"expected {expected_package_checksum}, got {actual_checksum}.",
                )

        for file_entry in _forcing_checksum_entries(manifest):
            uri = str(file_entry["uri"])
            expected_checksum = str(file_entry["checksum"])
            actual_checksum = self._object_checksum(uri)
            if actual_checksum != expected_checksum:
                raise SHUDRuntimeError(
                    "FORCING_FILE_CHECKSUM_MISMATCH",
                    f"Forcing file checksum mismatch for {uri}: expected {expected_checksum}, got {actual_checksum}.",
                )

    def _verify_staged_forcing_checksums(self, manifest: dict[str, Any], model_input_dir: Path) -> None:
        forcing_root = _object_key(manifest["forcing"]["forcing_uri"], self.config.object_store_prefix).rstrip("/")
        for file_entry in _forcing_checksum_entries(manifest):
            relative_path = str(file_entry.get("relative_path") or "").strip()
            if not relative_path:
                file_key = _object_key(str(file_entry["uri"]), self.config.object_store_prefix)
                if forcing_root and file_key.startswith(f"{forcing_root}/"):
                    relative_path = file_key[len(forcing_root) + 1 :]
                else:
                    relative_path = Path(file_key).name
            staged_path = _resolve_staged_forcing_path(model_input_dir, relative_path)
            try:
                staged_stat = stat_no_follow(staged_path, containment_root=model_input_dir)
            except FileNotFoundError:
                raise SHUDRuntimeError(
                    "FORCING_FILE_NOT_STAGED",
                    f"Forcing checksum entry was not staged: {relative_path}",
                )
            except SafeFilesystemError as error:
                raise SHUDRuntimeError(
                    "FORCING_FILE_NOT_STAGED",
                    f"Unsafe staged forcing checksum path {relative_path}: {error}",
                ) from error
            if not stat_module.S_ISREG(staged_stat.st_mode):
                raise SHUDRuntimeError(
                    "FORCING_FILE_NOT_STAGED",
                    f"Forcing checksum entry is not a regular staged file: {relative_path}",
                )
            actual_checksum = sha256_bytes(_read_staged_bytes(staged_path, root=model_input_dir))
            expected_checksum = str(file_entry["checksum"])
            if actual_checksum != expected_checksum:
                raise SHUDRuntimeError(
                    "FORCING_FILE_CHECKSUM_MISMATCH",
                    f"Staged forcing file checksum mismatch for {relative_path}: "
                    f"expected {expected_checksum}, got {actual_checksum}.",
                )

    def _object_checksum(self, uri_or_key: str) -> str:
        try:
            return self.object_store.checksum(uri_or_key)
        except Exception as error:
            raise SHUDRuntimeError(
                "FORCING_CHECKSUM_READ_FAILED",
                f"Failed to read forcing artifact checksum for {uri_or_key}: {error}",
            ) from error

    def _upload_directory(self, directory: Path, key_prefix: str) -> None:
        for file in _find_regular_files(directory):
            relative = file.relative_to(directory)
            key = f"{key_prefix.rstrip('/')}/{relative.as_posix()}"
            content = _read_staged_bytes(file, root=directory)
            for attempt in range(self.config.upload_retries):
                try:
                    target = self.object_store.resolve_path(key)
                    if _absolute_lexical_path(file) == _absolute_lexical_path(target):
                        break
                    self.object_store.write_bytes_atomic(key, content)
                    break
                except Exception as error:
                    if attempt + 1 >= self.config.upload_retries:
                        raise SHUDRuntimeError("UPLOAD_FAILED", f"Failed to upload {key}: {error}") from error
                    time.sleep(0.1 * (2**attempt))

    def _object_store_path(self, uri_or_key: str) -> Path:
        try:
            key = self.object_store.normalize_key(uri_or_key)
        except ValueError as error:
            raise SHUDRuntimeError("INVALID_OBJECT_KEY", str(error)) from error
        if any(part in {"", ".", ".."} for part in Path(key).parts):
            raise SHUDRuntimeError("INVALID_OBJECT_KEY", f"Unsafe object key: {uri_or_key}")
        path = Path(self.config.object_store_root) / key
        try:
            _ensure_relative_to_root(path, Path(self.config.object_store_root))
        except SafeFilesystemError as error:
            raise SHUDRuntimeError(
                "INVALID_OBJECT_KEY",
                f"Object key escapes object store root: {uri_or_key}",
            ) from error
        return path

    def _directory_uri(self, key_prefix: str) -> str:
        prefix = self.config.object_store_prefix.rstrip("/")
        if not prefix:
            return key_prefix.rstrip("/") + "/"
        return f"{prefix}/{key_prefix.strip('/')}/"

    def _write_failure_log(self, log_dir: Path, error: SHUDRuntimeError) -> None:
        _ensure_directory(log_dir)
        _write_text_no_follow(
            log_dir / "runtime_error.log",
            f"{error.error_code}: {error.message}\n",
            containment_root=log_dir,
        )


def _ensure_directory(path: Path, *, containment_root: Path | None = None) -> Path:
    try:
        return ensure_directory_no_follow(path, containment_root=containment_root)
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace directory {path}: {error}") from error


def _write_text_no_follow(path: Path, content: str, *, containment_root: Path) -> Path:
    return _write_staged_bytes(path, content.encode("utf-8"), root=containment_root)


def _write_staged_bytes(path: Path, content: bytes, *, root: Path) -> Path:
    try:
        _ensure_relative_to_root(path, root)
        return atomic_write_bytes_no_follow(path, content, containment_root=root, temp_suffix="part")
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace file target {path}: {error}") from error
    except OSError as error:
        raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: {error}") from error


def _read_text_no_follow(path: Path, *, containment_root: Path) -> str:
    return _read_staged_bytes(path, root=containment_root).decode("utf-8")


def _read_staged_bytes(path: Path, *, root: Path) -> bytes:
    try:
        _ensure_relative_to_root(path, root)
        return read_bytes_no_follow(path, containment_root=root)
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace file source {path}: {error}") from error
    except OSError as error:
        raise SHUDRuntimeError("WORKSPACE_READ_FAILED", f"Failed to read staged file {path}: {error}") from error


def _copy_staged_file_no_follow(source: Path, target: Path, *, root: Path) -> None:
    _write_staged_bytes(target, _read_staged_bytes(source, root=root), root=root)


def _regular_file_exists(path: Path, *, containment_root: Path) -> bool:
    try:
        result = _stat_path_no_follow(path, containment_root=containment_root)
    except FileNotFoundError:
        return False
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace file {path}: {error}") from error
    return stat_module.S_ISREG(result.st_mode)


def _stat_path_no_follow(path: Path, *, containment_root: Path) -> os.stat_result:
    target = _absolute_lexical_path(path)
    root = _absolute_lexical_path(containment_root)
    _ensure_relative_to_root(target, root)
    if target == root:
        try:
            result = target.lstat()
        except OSError as error:
            raise SafeFilesystemError(f"Failed to stat {target}: {error}", kind="io") from error
        if stat_module.S_ISLNK(result.st_mode):
            raise SafeFilesystemError(f"Target path must not be a symlink: {target}")
        return result
    return stat_no_follow(target, containment_root=root)


def _find_regular_files(
    directory: Path,
    *,
    pattern: str | None = None,
    suffix: str | None = None,
    non_empty: bool = False,
) -> list[Path]:
    matches: list[Path] = []
    for path in _iter_regular_descendant_files_no_follow(directory, containment_root=directory):
        name = path.name
        if pattern is not None and not path.match(pattern):
            continue
        if suffix is not None and not name.endswith(suffix):
            continue
        try:
            result = stat_no_follow(path, containment_root=directory)
        except SafeFilesystemError as error:
            raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace file {path}: {error}") from error
        if non_empty and result.st_size <= 0:
            continue
        matches.append(path)
    return sorted(matches)


def _iter_regular_descendant_files_no_follow(directory: Path, *, containment_root: Path) -> list[Path]:
    try:
        root_stat = _stat_path_no_follow(directory, containment_root=containment_root)
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Unsafe directory artifact {directory}: {error}") from error
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Directory artifact is not a directory: {directory}")

    files: list[Path] = []
    dir_fd = _open_directory_no_follow(directory)
    try:
        _walk_regular_descendants_fd(dir_fd, directory, files)
    finally:
        os.close(dir_fd)
    return sorted(files)


def _open_directory_no_follow(directory: Path) -> int:
    try:
        return os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Failed to open artifact directory {directory}: {error}") from error


def _walk_regular_descendants_fd(dir_fd: int, directory: Path, files: list[Path]) -> None:
    for name in os.listdir(dir_fd):
        entry_path = directory / name
        entry_stat = _stat_child_no_follow(dir_fd, name, entry_path)
        if stat_module.S_ISLNK(entry_stat.st_mode):
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Artifact directory contains a symlink: {entry_path}")
        if stat_module.S_ISDIR(entry_stat.st_mode):
            child_fd = _open_child_directory_no_follow(dir_fd, name, entry_path, expected=entry_stat)
            try:
                _walk_regular_descendants_fd(child_fd, entry_path, files)
            finally:
                os.close(child_fd)
            continue
        if stat_module.S_ISREG(entry_stat.st_mode):
            files.append(entry_path)
            continue
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Artifact directory contains a non-regular file: {entry_path}")


def _open_child_directory_no_follow(
    dir_fd: int,
    name: str,
    path_label: Path,
    *,
    expected: os.stat_result,
) -> int:
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=dir_fd,
        )
    except OSError as error:
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Failed to open artifact directory {path_label}: {error}") from error
    try:
        opened = os.fstat(child_fd)
        if expected.st_dev != opened.st_dev or expected.st_ino != opened.st_ino:
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Artifact directory changed while being opened: {path_label}")
        return child_fd
    except Exception:
        os.close(child_fd)
        raise


def _stat_child_no_follow(dir_fd: int, name: str, path_label: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as error:
        raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Failed to inspect artifact entry {path_label}: {error}") from error


def _stage_tar_artifact_bytes(content: bytes, destination: Path) -> bool:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except tarfile.ReadError:
        return False
    except tarfile.TarError as error:
        raise SHUDRuntimeError("ARTIFACT_TAR_INVALID", f"Failed to inspect tar artifact: {error}") from error

    with archive:
        for member in archive:
            relative = _safe_tar_member_path(member)
            target = destination / relative
            if member.isdir():
                _ensure_directory(target, containment_root=destination)
                continue
            if not member.isfile():
                raise SHUDRuntimeError(
                    "ARTIFACT_TAR_UNSAFE",
                    f"Tar artifact member must be a regular file or directory: {member.name}",
                )
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SHUDRuntimeError("ARTIFACT_TAR_INVALID", f"Failed to read tar member: {member.name}")
            with extracted:
                _write_staged_bytes(target, extracted.read(), root=destination)
    return True


def _safe_tar_member_path(member: tarfile.TarInfo) -> Path:
    if member.issym() or member.islnk() or member.isdev():
        raise SHUDRuntimeError("ARTIFACT_TAR_UNSAFE", f"Tar artifact member is unsafe: {member.name}")
    name = str(member.name)
    candidate = PurePosixPath(name)
    if not name or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SHUDRuntimeError("ARTIFACT_TAR_UNSAFE", f"Tar artifact member path escapes destination: {name}")
    return Path(*candidate.parts)


def _ensure_relative_to_root(path: Path, root: Path) -> None:
    target = _absolute_lexical_path(path)
    containment_root = _absolute_lexical_path(root)
    try:
        relative = target.relative_to(containment_root)
    except ValueError as error:
        raise SafeFilesystemError(f"Path must stay under containment root: {target}") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise SafeFilesystemError(f"Unsafe path component under containment root: {target}")


def _absolute_lexical_path(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


_SAFE_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_path_component(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Path component must be a string.")
    if not value or value.startswith("-"):
        raise ValueError("Invalid path component.")
    if "\x00" in value or "/" in value or "\\" in value or ".." in value:
        raise ValueError("Invalid path component.")
    if _SAFE_PATH_COMPONENT.fullmatch(value) is None:
        raise ValueError("Invalid path component.")
    return value


def _validate_manifest_path_components(manifest: dict[str, Any]) -> None:
    _safe_path_component(manifest["run_id"])
    model = manifest.get("model") or {}
    forcing = manifest.get("forcing") or {}
    for section in (model, forcing):
        for key, value in section.items():
            if value is not None and key.endswith("_id"):
                _safe_path_component(value)
    if model.get("project_name") is not None:
        _safe_path_component(model["project_name"])


def _runtime_command(
    shud_executable: str,
    cfg_path: Path,
    *,
    manifest: dict[str, Any] | None = None,
    output_dir: Path | None = None,
    command_style: str = "cfg",
    output_interval_minutes: int = 1440,
) -> list[str]:
    executable = str(shud_executable)
    if _is_shud_project_mode(manifest or {}, command_style):
        runtime = (manifest or {}).get("runtime") or {}
        threads = int(runtime.get("threads") or runtime.get("num_threads") or 1)
        args = ["-o", str(output_dir or cfg_path.parent), "-n", str(threads), _project_name(manifest or {})]
    else:
        args = [str(cfg_path)]
    if executable.endswith(".py"):
        return [sys.executable, executable, *args]
    return [executable, *args]


def _is_shud_project_mode(manifest: dict[str, Any], command_style: str) -> bool:
    style = str((manifest.get("runtime") or {}).get("command_style") or command_style or "cfg")
    return style in {"shud_project", "project", "native_shud"}


def _model_input_dir(manifest: dict[str, Any], input_dir: Path, command_style: str) -> Path:
    if _is_shud_project_mode(manifest, command_style):
        return input_dir / _project_name(manifest)
    return input_dir


def _cfg_replacements(
    manifest: dict[str, Any],
    output_dir: Path,
    init_mode: str,
    default_interval_minutes: int,
) -> dict[str, str]:
    return {
        "START_TIME": _format_time(_parse_time(manifest["start_time"])),
        "END_TIME": _format_time(_parse_time(manifest["end_time"])),
        "OUTPUT_DIR": str(output_dir),
        "MODEL_OUTPUT_INTERVAL": str(_output_interval_minutes(manifest, default_interval_minutes)),
        "INIT_MODE": init_mode,
        "SEGMENT_COUNT": str(_segment_count(manifest)),
    }


def _shud_start_day(manifest: dict[str, Any]) -> float:
    runtime = manifest.get("runtime") or {}
    if runtime.get("shud_start_day") is not None:
        return float(runtime["shud_start_day"])
    return _shud_start_minute(manifest) / 1440.0


def _shud_end_day(manifest: dict[str, Any]) -> float:
    runtime = manifest.get("runtime") or {}
    if runtime.get("shud_end_day") is not None:
        return float(runtime["shud_end_day"])
    duration_days = (_parse_time(manifest["end_time"]) - _parse_time(manifest["start_time"])).total_seconds() / 86_400.0
    return _shud_start_day(manifest) + duration_days


def _shud_start_minute(manifest: dict[str, Any]) -> float:
    runtime = manifest.get("runtime") or {}
    if runtime.get("shud_start_minute") is not None:
        return float(runtime["shud_start_minute"])
    return _parse_time(manifest["start_time"]).timestamp() / 60.0


def _read_internal_forcing_rows(path: Path) -> list[dict[str, Any]]:
    buckets: dict[datetime, dict[str, float]] = {}
    with io.StringIO(_read_text_no_follow(path, containment_root=path.parent), newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        if {"valid_time", "variable", "value"}.issubset(set(reader.fieldnames)):
            for row in reader:
                valid_time = _parse_time(str(row["valid_time"]))
                variable = str(row["variable"])
                buckets.setdefault(valid_time, {})[variable] = float(row["value"])
        elif "valid_time" in reader.fieldnames and "variable" in reader.fieldnames:
            station_columns = [name for name in reader.fieldnames if name not in {"valid_time", "variable"}]
            if not station_columns:
                raise SHUDRuntimeError("FORCING_FORMAT_UNSUPPORTED", f"No station value column found in {path}.")
            value_column = station_columns[0]
            for row in reader:
                valid_time = _parse_time(str(row["valid_time"]))
                variable = str(row["variable"])
                buckets.setdefault(valid_time, {})[variable] = float(row[value_column])
        else:
            raise SHUDRuntimeError(
                "FORCING_FORMAT_UNSUPPORTED",
                f"Unsupported forcing CSV columns in {path}: {reader.fieldnames}",
            )
    return [{"valid_time": time_value, **values} for time_value, values in sorted(buckets.items())]


def _write_shud_forcing_files(
    model_input_dir: Path,
    rows: list[dict[str, Any]],
    *,
    station: dict[str, Any],
    project_name: str,
) -> None:
    first_time = _ensure_utc(rows[0]["valid_time"])
    start_minute = first_time.timestamp() / 60.0
    _shift_cfg_ic_time(model_input_dir / f"{project_name}.cfg.ic", first_time)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.lai", first_time, start_minute=start_minute)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.mf", first_time, start_minute=start_minute)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.rl", first_time, start_minute=start_minute)
    _remap_sp_att_forcing(model_input_dir / f"{project_name}.sp.att", forcing_index=1)
    start_date = first_time.strftime("%Y%m%d")
    last_time = _ensure_utc(rows[-1]["valid_time"])
    end_date = last_time.strftime("%Y%m%d")
    forcing_csv = model_input_dir / "forcing.csv"
    forcing_lines = [
        f"{len(rows)}\t6\t{start_date}\t{end_date}",
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN",
    ]
    for row in rows:
        valid_time = _ensure_utc(row["valid_time"])
        time_days = start_minute / 1440.0 + (valid_time - first_time).total_seconds() / 86_400.0
        forcing_lines.append(
            "\t".join(
                [
                    _format_float(time_days),
                    _format_float(float(row.get("PRCP", 0.0))),
                    _format_float(float(row.get("TEMP", 0.0))),
                    _format_float(float(row.get("RH", 0.0))),
                    _format_float(float(row.get("wind", 0.0))),
                    _format_float(float(row.get("Rn", 0.0))),
                ]
            )
        )
    _write_text_no_follow(forcing_csv, "\n".join(forcing_lines) + "\n", containment_root=model_input_dir)

    tsd_forc = model_input_dir / f"{project_name}.tsd.forc"
    tsd_content = (
        f"1 {start_date}\n"
        f"{model_input_dir}\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        "1\t"
        f"{_format_float(float(station.get('longitude', 0.0)))}\t"
        f"{_format_float(float(station.get('latitude', 0.0)))}\t"
        f"{_format_float(float(station.get('x', 0.0)))}\t"
        f"{_format_float(float(station.get('y', 0.0)))}\t"
        f"{_format_float(float(station.get('z', 0.0)))}\t"
        "forcing.csv\n"
    )
    _write_text_no_follow(tsd_forc, tsd_content, containment_root=model_input_dir)


def _shift_project_time_inputs(model_input_dir: Path, project_name: str, first_time: datetime) -> None:
    start_minute = first_time.timestamp() / 60.0
    _shift_cfg_ic_time(model_input_dir / f"{project_name}.cfg.ic", first_time)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.lai", first_time, start_minute=start_minute)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.mf", first_time, start_minute=start_minute)
    _shift_tsd_time_axis(model_input_dir / f"{project_name}.tsd.rl", first_time, start_minute=start_minute)


def _read_shud_forcing_station_rows(path: Path) -> list[dict[str, Any]]:
    lines = [
        line.strip()
        for line in _read_text_no_follow(path, containment_root=path.parent).splitlines()
        if line.strip()
    ]
    if len(lines) < 4:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[3:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        rows.append(
            {
                "id": int(float(parts[0])),
                "longitude": float(parts[1]),
                "latitude": float(parts[2]),
                "x": float(parts[3]),
                "y": float(parts[4]),
                "z": float(parts[5]),
                "filename": Path(parts[6]).name,
            }
        )
    return rows


def _first_shud_forcing_time(path: Path) -> datetime:
    lines = [
        line.strip()
        for line in _read_text_no_follow(path, containment_root=path.parent).splitlines()
        if line.strip()
    ]
    if len(lines) < 3:
        raise SHUDRuntimeError("SHUD_FORCING_CSV_EMPTY", f"Invalid SHUD forcing CSV: {path}")
    header = lines[0].split()
    if len(header) < 3:
        raise SHUDRuntimeError("SHUD_FORCING_CSV_HEADER_INVALID", f"Invalid SHUD forcing CSV header: {path}")
    start_date = datetime.strptime(header[2], "%Y%m%d").replace(tzinfo=UTC)
    first_value = lines[2].split()[0]
    try:
        time_day = float(first_value)
    except ValueError as error:
        raise SHUDRuntimeError("SHUD_FORCING_CSV_TIME_INVALID", f"Invalid SHUD forcing time in {path}") from error
    # SHUD/rSHUD forcing files may use either absolute Unix-day-like Time_Day values
    # or days relative to the header date. The generated operational forcing uses
    # absolute values so SHUD output can be mapped back to real valid_time.
    if time_day > 10_000:
        return datetime.fromtimestamp(time_day * 86_400.0, tz=UTC)
    return start_date + timedelta(days=time_day)


def _remap_sp_att_forcing(path: Path, *, forcing_index: int) -> None:
    if not _regular_file_exists(path, containment_root=path.parent):
        return
    lines = _read_text_no_follow(path, containment_root=path.parent).splitlines()
    if len(lines) < 3:
        return
    header_tokens = lines[1].split()
    try:
        forcing_column = next(index for index, token in enumerate(header_tokens) if token.upper() == "FORC")
    except StopIteration:
        forcing_column = 4
    for index in range(2, len(lines)):
        parts = lines[index].split()
        if len(parts) <= forcing_column:
            continue
        parts[forcing_column] = str(forcing_index)
        lines[index] = "\t".join(parts)
    _write_text_no_follow(path, "\n".join(lines) + "\n", containment_root=path.parent)


def _shift_tsd_time_axis(path: Path, start_time: datetime, *, start_minute: float) -> None:
    if not _regular_file_exists(path, containment_root=path.parent):
        return
    lines = _read_text_no_follow(path, containment_root=path.parent).splitlines()
    if not lines:
        return
    header = lines[0].split()
    if len(header) >= 3:
        header[2] = _ensure_utc(start_time).strftime("%Y%m%d")
        if len(header) >= 4:
            header[3] = (_ensure_utc(start_time) + timedelta(days=366)).strftime("%Y%m%d")
        lines[0] = "\t".join(header)
        for index in range(2, len(lines)):
            parts = lines[index].split()
            if not parts:
                continue
            try:
                original_day = float(parts[0])
            except ValueError:
                continue
            parts[0] = _format_float(start_minute / 1440.0 + original_day)
            lines[index] = "\t".join(parts)
        _write_text_no_follow(path, "\n".join(lines) + "\n", containment_root=path.parent)


def _shift_cfg_ic_time(path: Path, start_time: datetime) -> None:
    if not _regular_file_exists(path, containment_root=path.parent):
        return
    lines = _read_text_no_follow(path, containment_root=path.parent).splitlines()
    if not lines:
        return
    header = lines[0].split()
    if len(header) >= 3:
        header[2] = f"{_ensure_utc(start_time).timestamp() / 60.0:.6f}"
        lines[0] = "\t".join(header)
        _write_text_no_follow(path, "\n".join(lines) + "\n", containment_root=path.parent)


def _shud_forcing_station(manifest: dict[str, Any]) -> dict[str, Any]:
    forcing = manifest.get("forcing") or {}
    station = forcing.get("shud_station") or forcing.get("station") or {}
    if station:
        return dict(station)
    return {"longitude": 0.0, "latitude": 0.0, "x": 0.0, "y": 0.0, "z": 0.0}


def _format_float(value: float) -> str:
    return f"{value:.10g}"


def _replace_or_append(content: str, key: str, value: str, *, separator: str = " = ") -> str:
    patterns = (
        f"{{{{{key}}}}}",
        f"${{{key}}}",
    )
    for pattern in patterns:
        if pattern in content:
            content = content.replace(pattern, value)

    lines = content.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(maxsplit=1)
        first_token = parts[0].split("=", 1)[0]
        if first_token == key:
            lines[index] = f"{key}{separator}{value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}{separator}{value}")
    return "\n".join(lines)


def _first_file(directory: Path, pattern: str) -> Path | None:
    matches = _find_regular_files(directory, pattern=pattern)
    return matches[0] if matches else None


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    candidate = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(candidate)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_time_or_none(value: str | datetime | None) -> datetime | None:
    return _parse_time(value) if value is not None else None


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _expected_timesteps(manifest: dict[str, Any], default_interval_minutes: int) -> int:
    explicit = (
        manifest.get("expected_timesteps")
        or manifest.get("outputs", {}).get("expected_timesteps")
        or manifest.get("runtime", {}).get("expected_timesteps")
    )
    if explicit is not None:
        return int(explicit)
    interval_minutes = _output_interval_minutes(manifest, default_interval_minutes)
    duration_seconds = (_parse_time(manifest["end_time"]) - _parse_time(manifest["start_time"])).total_seconds()
    if duration_seconds <= 0:
        raise SHUDRuntimeError("INVALID_TIME_WINDOW", "end_time must be after start_time.")
    step_seconds = interval_minutes * 60
    if duration_seconds % step_seconds != 0:
        raise SHUDRuntimeError("INVALID_TIME_WINDOW", "time window is not divisible by output interval.")
    return int(duration_seconds // step_seconds)


def _output_interval_minutes(manifest: dict[str, Any], default_interval_minutes: int) -> int:
    value = (
        manifest.get("output_interval_minutes")
        or manifest.get("runtime", {}).get("output_interval_minutes")
        or manifest.get("runtime", {}).get("model_output_interval")
        or default_interval_minutes
    )
    return int(value)


def _segment_count(manifest: dict[str, Any]) -> int:
    value = (
        manifest.get("segment_count")
        or manifest.get("model", {}).get("segment_count")
        or manifest.get("model", {}).get("river_segment_count")
        or manifest.get("outputs", {}).get("segment_count")
    )
    if value is None:
        raise SHUDRuntimeError("SEGMENT_COUNT_MISSING", "segment_count is required for output verification.")
    return int(value)


def _project_name(manifest: dict[str, Any]) -> str:
    return _safe_path_component(str(manifest.get("model", {}).get("project_name") or manifest["model"]["model_id"]))


def _initial_state_uri(manifest: dict[str, Any]) -> str | None:
    initial_state = manifest.get("initial_state") or {}
    return initial_state.get("ic_file_uri") or initial_state.get("state_uri")


def _initial_state_id(manifest: dict[str, Any]) -> str | None:
    initial_state = manifest.get("initial_state") or {}
    return initial_state.get("state_id") or manifest.get("init_state_id")


def _initial_state_checksum(manifest: dict[str, Any]) -> str | None:
    initial_state = manifest.get("initial_state") or {}
    checksum = initial_state.get("checksum")
    return str(checksum) if checksum else None


def _initial_state_quality(manifest: dict[str, Any]) -> str | None:
    initial_state = manifest.get("initial_state") or {}
    quality = initial_state.get("quality") or manifest.get("init_state_quality")
    return str(quality) if quality else None


def _forcing_checksum_entries(manifest: dict[str, Any]) -> list[dict[str, str]]:
    forcing = manifest.get("forcing") or {}
    files = forcing.get("files") or forcing.get("file_checksums") or []
    if not files:
        return []
    if not isinstance(files, list):
        raise SHUDRuntimeError("FORCING_CHECKSUM_INVALID", "Forcing checksum entries must be a list.")
    entries: list[dict[str, str]] = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            raise SHUDRuntimeError("FORCING_CHECKSUM_INVALID", "Forcing checksum entries must be objects.")
        uri = str(file_entry.get("uri") or "").strip()
        checksum = str(file_entry.get("checksum") or "").strip()
        if not uri or not checksum:
            raise SHUDRuntimeError("FORCING_CHECKSUM_MISSING", "Forcing file checksum entry is missing uri/checksum.")
        entries.append(
            {
                "role": str(file_entry.get("role") or ""),
                "relative_path": str(file_entry.get("relative_path") or ""),
                "uri": uri,
                "checksum": checksum,
            }
        )
    return entries


def _resolve_staged_forcing_path(model_input_dir: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate == Path(".") or candidate.is_absolute() or ".." in candidate.parts:
        raise SHUDRuntimeError(
            "FORCING_FILE_PATH_INVALID",
            f"Forcing checksum relative_path escapes model input directory: {relative_path}",
        )
    return model_input_dir / candidate


def _set_initial_state_from_snapshot(manifest: dict[str, Any], snapshot: StateSnapshot, *, quality: str) -> None:
    manifest["initial_state"] = {
        "state_id": snapshot.state_id,
        "ic_file_uri": snapshot.state_uri,
        "valid_time": _format_time(snapshot.valid_time),
        "checksum": snapshot.checksum,
        "quality": quality,
    }
    _set_runtime_init_mode(manifest, 3)


def _set_cold_start_initial_state(manifest: dict[str, Any], *, quality: str) -> None:
    manifest["initial_state"] = {
        "state_id": None,
        "ic_file_uri": None,
        "valid_time": None,
        "checksum": None,
        "quality": quality,
    }
    _set_runtime_init_mode(manifest, 1)


def _set_runtime_init_mode(manifest: dict[str, Any], init_mode: int) -> None:
    runtime = manifest.setdefault("runtime", {})
    runtime["init_mode"] = init_mode


def _init_mode(manifest: dict[str, Any]) -> str:
    runtime = manifest.get("runtime") or {}
    if runtime.get("init_mode") is not None:
        return str(runtime["init_mode"])
    if _initial_state_id(manifest) or _initial_state_uri(manifest):
        return "3"
    return "1"


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = uri_or_key.strip()
    prefix = object_store_prefix.rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")


def _run_manifest_uri(manifest: dict[str, Any]) -> str:
    return (
        manifest.get("run_manifest_uri")
        or manifest.get("outputs", {}).get("run_manifest_uri")
        or f"runs/{_safe_path_component(manifest['run_id'])}/input/manifest.json"
    )


def _split_row(line: str) -> list[str]:
    if "," in line:
        return [token.strip() for token in line.split(",") if token.strip()]
    return line.split()


def _rivqdown_data_lines(lines: list[str]) -> list[str]:
    candidate_lines = [line for line in lines if not line.lstrip().startswith("#")]
    if candidate_lines:
        header_tokens = _split_row(candidate_lines[0])
        has_header = any(not _is_number(token) for token in header_tokens)
        if has_header:
            return candidate_lines[1:]
    data_lines: list[str] = []
    start_index = 0
    if len(candidate_lines) >= 2:
        first_tokens = _split_row(candidate_lines[0])
        second_tokens = _split_row(candidate_lines[1])
        if first_tokens and all(_is_number(token) for token in first_tokens) and any(
            not _is_number(token) for token in second_tokens
        ):
            start_index = 2
    for line in candidate_lines[start_index:]:
        tokens = _split_row(line)
        if len(tokens) < 2:
            continue
        if _looks_like_shud_metadata_row(tokens):
            continue
        if not _is_number(tokens[0]):
            continue
        if any(not _is_number(token) for token in tokens[1:]):
            continue
        data_lines.append(line)
    return data_lines


def _looks_like_shud_metadata_row(tokens: list[str]) -> bool:
    return len(tokens) == 3 and tokens[0] == "0" and tokens[2].isdigit() and len(tokens[2]) == 8


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _last_lines(text: str, count: int) -> str:
    return "\n".join(text.splitlines()[-count:])


def _subprocess_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _as_runtime_error(error: Exception) -> SHUDRuntimeError:
    if isinstance(error, SHUDRuntimeError):
        return error
    return SHUDRuntimeError("RUNTIME_ERROR", str(error))
