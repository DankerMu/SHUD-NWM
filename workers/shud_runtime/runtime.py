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
from collections.abc import Mapping
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.redaction import redact_text
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    read_bytes_no_follow,
    stat_no_follow,
    unlink_no_follow,
)
from packages.common.shud_preflight import check_shud_executable
from packages.common.state_manager import PsycopgStateSnapshotRepository, StateManager, StateSnapshot, assess_freshness
from packages.common.state_qc import cfg_ic_header_minute_index, cfg_ic_header_minute_time
from services.orchestrator.time_consistency import check_three_way_time_consistency as _check_three_way_time_consistency


class SHUDRuntimeError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# Authoritative SHUD forcing PRCP unit (Decision A): SHUD reads precip from
# ``qhh.tsd.forc`` as a daily rate in millimetres per day.
EXPECTED_PRCP_UNIT = "mm/day"
# Forcing package manifests are JSON metadata that grow with station count
# (per-station ``shud_file_entries``). The PRCP unit check is best-effort, so cap
# the read generously and tolerate-skip (never hard-fail) if a multi-station
# manifest exceeds the cap.
MAX_PACKAGE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_DIRECT_GRID_TSD_FORC_BYTES = 8 * 1024 * 1024
MAX_DIRECT_GRID_FORCING_CSV_BYTES = 8 * 1024 * 1024
MAX_DIRECT_GRID_SP_ATT_BYTES = 32 * 1024 * 1024
MAX_DIRECT_GRID_TSD_FORC_LINES = 250_000
MAX_DIRECT_GRID_FORCING_CSV_LINES = 250_000
MAX_DIRECT_GRID_SP_ATT_LINES = 2_000_000
MAX_DIRECT_GRID_STAGING_LINE_BYTES = 64 * 1024


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
class _ForcingPackageContext:
    package_manifest: dict[str, Any] | None
    checksum_entries: tuple[dict[str, str], ...]
    is_direct_grid: bool


@dataclass(frozen=True)
class _DirectGridSensitiveMemberLimit:
    max_bytes: int
    error_code: str
    message: str


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
        manifest = json.loads(_read_runtime_manifest_text_no_follow(Path(manifest_path)))
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
        forcing_context = self._prepare_forcing_package_context(manifest)
        self._stage_artifact(
            manifest["forcing"]["forcing_uri"],
            model_input_dir,
            forcing_context=forcing_context,
        )
        self._verify_staged_forcing_checksums(manifest, model_input_dir, forcing_context=forcing_context)
        self._stage_initial_state(manifest, model_input_dir)
        if _is_shud_project_mode(manifest, self.config.command_style):
            self._prepare_shud_project_forcing(manifest, model_input_dir, forcing_context=forcing_context)

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
            update_ic_step = _update_ic_step_minutes(manifest)
            if update_ic_step is not None:
                # Restart cadence inside the same SHUD long run. Forecast
                # checkpoint states are copied opportunistically from
                # cfg.ic.update; they are not produced by extra short runs.
                replacements["Update_IC_STEP"] = str(update_ic_step)
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
        self._preflight_shud_executable()
        command = _runtime_command(
            self.config.shud_executable,
            cfg_path,
            manifest=manifest,
            output_dir=output_dir,
            command_style=self.config.command_style,
            output_interval_minutes=self.config.output_interval_minutes,
        )
        checkpoint_tracker = _StateCheckpointTracker(manifest, output_dir)
        stdout_path = log_dir / "shud_stdout.log"
        stderr_path = log_dir / "shud_stderr.log"
        _ensure_directory(log_dir, containment_root=workspace)
        try:
            with _open_log_file_no_follow(stdout_path, containment_root=log_dir) as stdout_file, (
                _open_log_file_no_follow(stderr_path, containment_root=log_dir)
            ) as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=workspace,
                    text=True,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                self._wait_for_shud_process(process, manifest, checkpoint_tracker)
        except OSError as error:
            raise SHUDRuntimeError("SHUD_EXECUTION_FAILED", f"Failed to start SHUD executable: {error}") from error
        except subprocess.TimeoutExpired as error:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            message = f"SHUD executable timed out after {self.config.timeout_seconds}s"
            raise SHUDRuntimeError("SHUD_TIMEOUT", message) from error

        if process.returncode != 0:
            detail = _last_lines(_read_text_no_follow(stderr_path, containment_root=log_dir), 50)
            raise SHUDRuntimeError(
                f"SHUD_EXIT_{process.returncode}",
                f"SHUD executable exited with code {process.returncode}: {detail}",
            )
        checkpoint_tracker.capture_final()
        checkpoint_tracker.write_manifest()
        _ensure_directory(output_dir)

    def _wait_for_shud_process(
        self,
        process: subprocess.Popen[str],
        manifest: dict[str, Any],
        checkpoint_tracker: "_StateCheckpointTracker",
    ) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while process.poll() is None:
            checkpoint_tracker.capture_available()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, self.config.timeout_seconds)
            time.sleep(_state_checkpoint_poll_seconds(manifest))
        checkpoint_tracker.capture_available()

    def _preflight_shud_executable(self) -> None:
        """Reject stub/missing SHUD executables before invoking the solver.

        ``.py`` runtime engines (test/mock harnesses) are run through
        ``sys.executable`` and are intentionally not compiled SHUD binaries, so
        the stub/ldd/version-banner probes do not apply: for those we only assert
        the script exists and is a regular file. Compiled executables go through
        the full shared production preflight (stub basename, exec bit, shared
        libraries, bounded SHUD version/help signal).
        """

        executable = str(self.config.shud_executable or "").strip()
        if not executable:
            raise SHUDRuntimeError(
                "SHUD_EXECUTABLE_NOT_CONFIGURED",
                "SHUD_EXECUTABLE is empty or unset; refusing to run a hydro run.",
            )
        if executable.endswith(".py"):
            script = Path(executable).expanduser()
            if not _python_runtime_script_present(script):
                raise SHUDRuntimeError(
                    "SHUD_EXECUTABLE_MISSING",
                    f"SHUD runtime script not found: {redact_text(script.name)}",
                )
            return

        result = check_shud_executable(executable)
        if not result.ok:
            primary = result.blockers[0]
            raise SHUDRuntimeError(
                str(primary.get("error_code", "SHUD_EXECUTABLE_PREFLIGHT_FAILED")),
                str(primary.get("message", "SHUD executable preflight failed.")),
            )

    def _prepare_shud_project_forcing(
        self,
        manifest: dict[str, Any],
        model_input_dir: Path,
        *,
        forcing_context: _ForcingPackageContext | None = None,
    ) -> None:
        is_direct_grid = (
            forcing_context.is_direct_grid
            if forcing_context is not None
            else self._forcing_declares_direct_grid(manifest)
        )
        staged_ids = self._stage_standard_shud_forcing(manifest, model_input_dir, is_direct_grid=is_direct_grid)
        if staged_ids is not None:
            if is_direct_grid:
                _validate_direct_grid_sp_att_forcing_ids(
                    model_input_dir / f"{_project_name(manifest)}.sp.att",
                    staged_ids,
                )
            return
        if is_direct_grid:
            raise SHUDRuntimeError(
                "DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING",
                "Direct-grid forcing requires standard SHUD package staging with shud/qhh.tsd.forc; "
                "refusing legacy fallback that rewrites .sp.att FORC ownership.",
            )
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

    def _stage_standard_shud_forcing(
        self,
        manifest: dict[str, Any],
        model_input_dir: Path,
        *,
        is_direct_grid: bool,
    ) -> set[int] | None:
        shud_dir = model_input_dir / "shud"
        source_tsd = shud_dir / "qhh.tsd.forc"
        if not _regular_file_exists(source_tsd, containment_root=model_input_dir):
            return None
        rows = _read_shud_forcing_station_rows(source_tsd, is_direct_grid=is_direct_grid)
        if not rows:
            raise SHUDRuntimeError("SHUD_FORCING_STATIONS_EMPTY", f"No stations found in {source_tsd}.")
        first_csv = shud_dir / str(rows[0]["filename"])
        first_time = _first_shud_forcing_time(first_csv, is_direct_grid=is_direct_grid)
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
            target_csv = model_input_dir / filename
            if is_direct_grid:
                _validate_direct_grid_station_filename_target(
                    target_csv,
                    model_input_dir=model_input_dir,
                    project_name=_project_name(manifest),
                )
                _copy_direct_grid_station_csv_no_follow(source_csv, target_csv, root=model_input_dir)
            else:
                _copy_staged_file_no_follow(source_csv, target_csv, root=model_input_dir)
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
        return {int(row["id"]) for row in rows}

    def _forcing_declares_direct_grid(
        self,
        manifest: dict[str, Any],
        *,
        package_manifest: Mapping[str, Any] | None = None,
    ) -> bool:
        forcing = manifest.get("forcing") or {}
        forcing_declares_direct = _mapping_metadata_declares_direct_grid(forcing)
        forcing_declares_non_direct = _mapping_metadata_declares_non_direct_grid(forcing)
        if package_manifest is None:
            package_manifest = self._read_authoritative_forcing_package_manifest(forcing)
        if package_manifest is not None:
            if _mapping_metadata_declares_direct_grid(package_manifest):
                return True
            if _mapping_metadata_declares_non_direct_grid(package_manifest):
                return False
            if forcing_declares_direct:
                raise SHUDRuntimeError(
                    "FORCING_PACKAGE_MAPPING_MODE_MISSING",
                    "A checksum-verified forcing_package.json is authoritative for mapping mode; "
                    "outer direct-grid metadata cannot activate direct-grid for a neutral package manifest.",
                )
            return False
        if forcing_declares_direct:
            return True
        if forcing_declares_non_direct:
            return False
        return False

    def _prepare_forcing_package_context(self, manifest: dict[str, Any]) -> _ForcingPackageContext:
        forcing = manifest.get("forcing") or {}
        package_manifest = self._verify_forcing_package_manifest(manifest)
        is_direct_grid = self._forcing_declares_direct_grid(manifest, package_manifest=package_manifest)
        if is_direct_grid:
            if package_manifest is None:
                raise SHUDRuntimeError(
                    "FORCING_PACKAGE_MANIFEST_REQUIRED",
                    "Direct-grid forcing requires a checksum-verified forcing_package.json; "
                    "runtime forcing.files metadata is not authoritative for direct-grid staging.",
                )
            checksum_entries = _authoritative_package_manifest_checksum_entries(
                package_manifest,
                forcing_uri=str(forcing.get("forcing_uri") or ""),
                object_store_prefix=self.config.object_store_prefix,
            )
            self._validate_direct_grid_package_station_filenames(checksum_entries)
        else:
            checksum_entries = _forcing_checksum_entries(manifest)
        self._verify_forcing_object_checksums(checksum_entries, is_direct_grid=is_direct_grid)
        return _ForcingPackageContext(
            package_manifest=package_manifest,
            checksum_entries=tuple(checksum_entries),
            is_direct_grid=is_direct_grid,
        )

    def _read_authoritative_forcing_package_manifest(self, forcing: Mapping[str, Any]) -> dict[str, Any] | None:
        package_manifest_uri = _forcing_package_manifest_uri(forcing)
        if not package_manifest_uri:
            return None
        try:
            content = self.object_store.read_bytes_limited(
                package_manifest_uri, max_bytes=MAX_PACKAGE_MANIFEST_BYTES
            )
        except Exception as error:
            raise SHUDRuntimeError(
                "FORCING_PACKAGE_MANIFEST_READ_FAILED",
                "Forcing package manifest could not be read for authoritative mapping-mode detection: "
                f"{package_manifest_uri}",
            ) from error
        try:
            package_manifest = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SHUDRuntimeError(
                "FORCING_PACKAGE_MANIFEST_INVALID",
                "Forcing package manifest is not valid JSON for authoritative mapping-mode detection: "
                f"{package_manifest_uri}",
            ) from error
        if not isinstance(package_manifest, dict):
            raise SHUDRuntimeError(
                "FORCING_PACKAGE_MANIFEST_INVALID",
                "Forcing package manifest must be a JSON object for authoritative mapping-mode detection: "
                f"{package_manifest_uri}",
            )
        return package_manifest

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
                self._materialize_ic_to_project_name(manifest, staged_path, input_dir)
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

    def _materialize_ic_to_project_name(
        self,
        manifest: dict[str, Any],
        staged_path: Path,
        input_dir: Path,
    ) -> None:
        """Rename the staged canonical ``state.cfg.ic`` to ``<project_name>.cfg.ic``.

        The warm-start object is stored canonically as ``state.cfg.ic``; SHUD actually
        reads ``<project_name>.cfg.ic`` from the model input directory. Materialize the
        consuming filename and shift its header minute-time to the run start, then verify
        the three-way time consistency (snapshot valid_time / IC header / run start_time)
        so a wrong-time restart is a recorded blocker, not silent (M24 §2 Lane 2).
        """

        model_input_dir = _materialized_model_input_dir(manifest, input_dir, self.config.command_style)
        target = model_input_dir / f"{_project_name(manifest)}.cfg.ic"
        if staged_path.resolve() != target.resolve():
            content = _read_staged_bytes(staged_path, root=input_dir)
            _ensure_directory(model_input_dir)
            atomic_write_bytes_no_follow(target, content, containment_root=input_dir, temp_suffix="part")
            if staged_path.name.endswith(".cfg.ic") or staged_path.name.endswith(".cfg.ic.update"):
                try:
                    unlink_no_follow(staged_path, containment_root=input_dir, missing_ok=True)
                except SafeFilesystemError:
                    pass

        # The saved artifact's NATIVE header minute-time must match the snapshot
        # valid_time it was recorded at (state-integrity invariant) BEFORE the header is
        # re-stamped to the run start. A native header that disagrees with the recorded
        # snapshot valid_time means the staged artifact is not the state it claims to be
        # -- a recorded blocker, not a silent restart at the wrong time. (The three-way
        # snapshot/header/run-start equality of warm continuity is enforced at daemon
        # selection time; here a forecast may legitimately reuse an older state and the
        # header is deliberately re-stamped to the run start below.)
        start_time = _parse_time(manifest["start_time"])
        native_header_minute = _read_cfg_ic_header_minute(target)
        self._verify_ic_time_consistency(manifest, native_header_minute)
        _shift_cfg_ic_time(target, start_time)

    def _verify_ic_time_consistency(
        self,
        manifest: dict[str, Any],
        ic_header_minute_time: float | None,
    ) -> None:
        """Enforce warm-start time consistency on the production consume path.

        Two legitimate cases (M24 §2 Lane 2):

        - **Warm-continuity / exact successor**: the snapshot is the prior cycle's
          state recorded at ``T_{N+1}`` and ``valid_time == run start_time`` (the run
          consumes it as the precise next-cycle init). Here all three of snapshot
          ``valid_time`` / native ``.cfg.ic`` header minute-time / run ``start_time``
          must agree exactly; any mismatch is a recorded ``WARM_START_TIME_MISMATCH``
          blocker, not a silent restart at the wrong time. The three-way check is run
          via the shared ``_check_three_way_time_consistency`` helper.
        - **Degraded / stale reuse**: a forecast legitimately reuses an *older* state
          (``valid_time < run start_time``); the header is deliberately re-stamped to
          the run start downstream. Here only the snapshot/header equality is
          enforced (the artifact must be the state it claims to be); the run-start leg
          is intentionally NOT forced.
        """

        snapshot_valid_time = _parse_time_or_none((manifest.get("initial_state") or {}).get("valid_time"))
        if snapshot_valid_time is None or ic_header_minute_time is None:
            return

        run_start_time = _parse_time_or_none(manifest.get("start_time"))
        snapshot_minute = round(_ensure_utc(snapshot_valid_time).timestamp() / 60.0)
        run_start_minute = (
            round(_ensure_utc(run_start_time).timestamp() / 60.0) if run_start_time is not None else None
        )
        is_warm_continuity = run_start_minute is not None and snapshot_minute == run_start_minute

        if is_warm_continuity:
            # Exact successor: enforce strict snapshot / header / run-start three-way
            # equality through the shared helper (single source of truth).
            reason = _check_three_way_time_consistency(
                snapshot_valid_time=snapshot_valid_time,
                ic_header_minute_time=ic_header_minute_time,
                run_start_time=run_start_time,
            )
            if reason is not None:
                raise SHUDRuntimeError("WARM_START_TIME_MISMATCH", reason)
            return

        # Degraded/stale reuse of an older state: only the snapshot/header identity is
        # enforced; the header is re-stamped to run start downstream by design.
        header_minute = round(ic_header_minute_time)
        if snapshot_minute != header_minute:
            raise SHUDRuntimeError(
                "WARM_START_TIME_MISMATCH",
                f"Warm-start time mismatch: ic_header_minute_time={header_minute} != "
                f"snapshot_valid_time={snapshot_minute} (minutes since epoch); "
                "restart at the wrong time is a blocker.",
            )

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

    def _stage_artifact(
        self,
        uri: str,
        destination: Path,
        *,
        forcing_context: _ForcingPackageContext | None = None,
    ) -> None:
        source = self._object_store_path(uri)
        try:
            source_stat = stat_no_follow(source, containment_root=Path(self.config.object_store_root))
        except FileNotFoundError:
            raise SHUDRuntimeError("ARTIFACT_NOT_FOUND", f"Object storage artifact not found: {uri}")
        except SafeFilesystemError as error:
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Unsafe object storage artifact {uri}: {error}") from error

        _ensure_directory(destination)
        if stat_module.S_ISDIR(source_stat.st_mode):
            self._stage_directory_artifact(source, destination, forcing_context=forcing_context)
            return

        if not stat_module.S_ISREG(source_stat.st_mode):
            raise SHUDRuntimeError(
                "ARTIFACT_UNSAFE",
                f"Object storage artifact must be a regular file or directory: {uri}",
            )

        if forcing_context is not None and forcing_context.is_direct_grid:
            raise SHUDRuntimeError(
                "DIRECT_GRID_FORCING_TAR_UNSUPPORTED",
                "Direct-grid forcing tar artifacts cannot be staged safely before bounded member reads; "
                "use an object-store directory artifact with forcing_package.json files entries.",
            )

        content = self._read_object_artifact_bytes(source, uri)
        if _stage_tar_artifact_bytes(content, destination):
            return

        target = destination / source.name
        _write_staged_bytes(target, content, root=destination)

    def _stage_directory_artifact(
        self,
        source: Path,
        destination: Path,
        *,
        forcing_context: _ForcingPackageContext | None = None,
    ) -> None:
        if forcing_context is not None and forcing_context.is_direct_grid:
            self._stage_direct_grid_directory_artifact(source, destination, forcing_context=forcing_context)
            return
        object_root = Path(self.config.object_store_root)
        for source_file in _iter_regular_descendant_files_no_follow(source, containment_root=object_root):
            relative = source_file.relative_to(source)
            content = self._read_object_artifact_bytes(source_file, str(source_file))
            _write_staged_bytes(destination / relative, content, root=destination)

    def _stage_direct_grid_directory_artifact(
        self,
        source: Path,
        destination: Path,
        *,
        forcing_context: _ForcingPackageContext,
    ) -> None:
        object_root = Path(self.config.object_store_root)
        staged_relative_paths: set[str] = set()
        for file_entry in forcing_context.checksum_entries:
            relative_posix = str(file_entry["relative_path"])
            if relative_posix in staged_relative_paths:
                raise SHUDRuntimeError(
                    "FORCING_CHECKSUM_INVALID",
                    f"Duplicate direct-grid forcing package manifest relative_path: {relative_posix}",
                )
            staged_relative_paths.add(relative_posix)
            relative = Path(*PurePosixPath(relative_posix).parts)
            source_file = source / relative
            try:
                source_stat = stat_no_follow(source_file, containment_root=object_root)
            except FileNotFoundError:
                raise SHUDRuntimeError(
                    "ARTIFACT_NOT_FOUND",
                    f"Direct-grid forcing package manifest member was not found: {relative_posix}",
                )
            except SafeFilesystemError as error:
                raise SHUDRuntimeError(
                    "ARTIFACT_UNSAFE",
                    f"Unsafe direct-grid forcing package manifest member {relative_posix}: {error}",
                ) from error
            if not stat_module.S_ISREG(source_stat.st_mode):
                raise SHUDRuntimeError(
                    "ARTIFACT_UNSAFE",
                    f"Direct-grid forcing package manifest member must be a regular file: {relative_posix}",
                )
            content = self._read_direct_grid_forcing_member_bytes(source_file, relative_posix)
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

    def _read_direct_grid_forcing_member_bytes(self, source: Path, relative_path: str) -> bytes:
        sensitive_limit = _direct_grid_sensitive_member_limit(relative_path)
        if sensitive_limit is None:
            return self._read_object_artifact_bytes(source, str(source))
        try:
            content = read_bytes_limited_no_follow(
                source,
                max_bytes=sensitive_limit.max_bytes,
                containment_root=Path(self.config.object_store_root),
            )
        except SafeFilesystemError as error:
            raise SHUDRuntimeError("ARTIFACT_UNSAFE", f"Unsafe object storage artifact {source}: {error}") from error
        except OSError as error:
            raise SHUDRuntimeError(
                "ARTIFACT_READ_FAILED",
                f"Failed to read object storage artifact {source}: {error}",
            ) from error
        if len(content) > sensitive_limit.max_bytes:
            raise SHUDRuntimeError(
                sensitive_limit.error_code,
                f"{sensitive_limit.message}: {relative_path}",
            )
        return content

    def _verify_forcing_package_manifest(self, manifest: dict[str, Any]) -> dict[str, Any] | None:
        forcing = manifest.get("forcing") or {}
        package_manifest_uri = _forcing_package_manifest_uri(forcing)
        expected_package_checksum = _forcing_package_manifest_checksum(forcing)
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
            package_manifest = self._read_authoritative_forcing_package_manifest(forcing)
            self._assert_forcing_prcp_unit(package_manifest_uri)
            return package_manifest
        return None

    def _verify_forcing_object_checksums(
        self,
        checksum_entries: list[dict[str, str]],
        *,
        is_direct_grid: bool = False,
    ) -> None:
        for file_entry in checksum_entries:
            uri = str(file_entry["uri"])
            expected_checksum = str(file_entry["checksum"])
            relative_path = str(file_entry.get("relative_path") or "").strip()
            limit = _direct_grid_sensitive_member_limit(relative_path) if is_direct_grid else None
            actual_checksum = (
                self._object_checksum_limited(uri, limit=limit)
                if limit is not None
                else self._object_checksum(uri)
            )
            if actual_checksum != expected_checksum:
                raise SHUDRuntimeError(
                    "FORCING_FILE_CHECKSUM_MISMATCH",
                    f"Forcing file checksum mismatch for {uri}: expected {expected_checksum}, got {actual_checksum}.",
                )

    def _validate_direct_grid_package_station_filenames(self, checksum_entries: list[dict[str, str]]) -> None:
        tsd_entries = [entry for entry in checksum_entries if entry["relative_path"] == "shud/qhh.tsd.forc"]
        if not tsd_entries:
            return
        tsd_entry = tsd_entries[0]
        tsd_uri = str(tsd_entry["uri"])
        limit = _direct_grid_sensitive_member_limit("shud/qhh.tsd.forc")
        if limit is None:
            return
        try:
            content = self.object_store.read_bytes_limited(tsd_uri, max_bytes=limit.max_bytes)
        except Exception as error:
            raise SHUDRuntimeError(
                limit.error_code,
                f"{limit.message}: {tsd_uri}",
            ) from error
        if len(content) > limit.max_bytes:
            raise SHUDRuntimeError(
                limit.error_code,
                f"{limit.message}: {tsd_uri}",
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SHUDRuntimeError(
                "SHUD_FORCING_STATIONS_INVALID",
                f"Direct-grid SHUD forcing station file is not valid UTF-8: {tsd_uri}",
            ) from error
        lines = _bounded_nonempty_lines(
            text,
            path=Path(tsd_uri),
            max_lines=MAX_DIRECT_GRID_TSD_FORC_LINES,
            max_line_bytes=MAX_DIRECT_GRID_STAGING_LINE_BYTES,
            too_many_lines_code="DIRECT_GRID_TSD_FORC_TOO_MANY_LINES",
            line_too_long_code="DIRECT_GRID_TSD_FORC_LINE_TOO_LONG",
        )
        for line in lines[3:]:
            parts = line.split()
            if len(parts) >= 7:
                _direct_grid_station_filename(parts[6])

    def _assert_forcing_prcp_unit(self, package_manifest_uri: str) -> None:
        """Best-effort guard: fail loudly only on an explicit non-mm/day PRCP unit.

        This is the SHUD staging terminus guard for #270: SHUD reads PRCP from
        ``qhh.tsd.forc`` as a daily rate (``mm/day``, Decision A). If an upstream
        regression re-introduced a per-step ``mm`` accumulation, SHUD would silently
        consume a physically wrong precip amount; an explicitly declared non-mm/day
        PRCP unit is therefore the one and only hard failure here.

        Everything else is tolerated and the check is skipped, because this is an
        optional hardening layer that must never break an otherwise-runnable run for
        an incidental reason: a manifest that is unreadable / missing / over the size
        cap, invalid JSON, or simply lacking unit metadata (old packages) all
        tolerate-skip. The manifest's real content integrity is already covered by
        the checksum verification performed before this call, so skipping the unit
        peek here loses no safety guarantee.
        """
        try:
            content = self.object_store.read_bytes_limited(
                package_manifest_uri, max_bytes=MAX_PACKAGE_MANIFEST_BYTES
            )
        except Exception:
            # Read failure / object missing / size-cap exceeded: tolerate-skip.
            # A large multi-station manifest must not turn a runnable package into a
            # hard failure just because the optional unit peek could not read it.
            return
        try:
            package_manifest = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Malformed manifest: tolerate-skip. Content integrity is the checksum's
            # job; the unit peek stays best-effort and does not fail the run.
            return

        units = package_manifest.get("units")
        if not isinstance(units, Mapping):
            # Backward compatibility: packages without a units block predate this
            # metadata; do not fail the run on missing unit information.
            return
        observed = units.get("PRCP")
        if observed is None:
            return
        if str(observed).strip().lower() != EXPECTED_PRCP_UNIT:
            raise SHUDRuntimeError(
                "FORCING_PRCP_UNIT_MISMATCH",
                "Forcing package PRCP unit mismatch: "
                f"expected '{EXPECTED_PRCP_UNIT}', observed '{observed}'. "
                "SHUD requires PRCP in mm/day; refusing to stage forcing.",
            )

    def _verify_staged_forcing_checksums(
        self,
        manifest: dict[str, Any],
        model_input_dir: Path,
        *,
        forcing_context: _ForcingPackageContext | None = None,
    ) -> None:
        forcing_root = _object_key(manifest["forcing"]["forcing_uri"], self.config.object_store_prefix).rstrip("/")
        is_direct_grid = (
            forcing_context.is_direct_grid
            if forcing_context is not None
            else self._forcing_declares_direct_grid(manifest)
        )
        checksum_entries = (
            list(forcing_context.checksum_entries)
            if forcing_context is not None
            else _forcing_checksum_entries(manifest)
        )
        for file_entry in checksum_entries:
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
            normalized_relative_path = _staged_relative_posix(model_input_dir, staged_path)
            if is_direct_grid and normalized_relative_path == "shud/qhh.tsd.forc":
                staged_bytes = _read_limited_staged_bytes(
                    staged_path,
                    root=model_input_dir,
                    max_bytes=MAX_DIRECT_GRID_TSD_FORC_BYTES,
                    too_large_code="DIRECT_GRID_TSD_FORC_TOO_LARGE",
                    too_large_message="Direct-grid SHUD forcing station file exceeds the staging read cap",
                )
            elif is_direct_grid and _is_direct_grid_station_csv_relative_path(normalized_relative_path):
                staged_bytes = _read_limited_staged_bytes(
                    staged_path,
                    root=model_input_dir,
                    max_bytes=MAX_DIRECT_GRID_FORCING_CSV_BYTES,
                    too_large_code="DIRECT_GRID_FORCING_CSV_TOO_LARGE",
                    too_large_message="Direct-grid SHUD forcing CSV exceeds the staging read cap",
                )
            else:
                staged_bytes = _read_staged_bytes(staged_path, root=model_input_dir)
            actual_checksum = sha256_bytes(staged_bytes)
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

    def _object_checksum_limited(self, uri_or_key: str, *, limit: _DirectGridSensitiveMemberLimit) -> str:
        try:
            return self.object_store.checksum_limited(uri_or_key, max_bytes=limit.max_bytes)
        except Exception as error:
            raise SHUDRuntimeError(
                limit.error_code,
                f"{limit.message}: {uri_or_key}",
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


def _read_limited_text_no_follow(
    path: Path,
    *,
    containment_root: Path,
    max_bytes: int,
    too_large_code: str,
    too_large_message: str,
) -> str:
    content = _read_limited_staged_bytes(
        path,
        root=containment_root,
        max_bytes=max_bytes,
        too_large_code=too_large_code,
        too_large_message=too_large_message,
    )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SHUDRuntimeError("WORKSPACE_READ_FAILED", f"Failed to decode staged file {path}: {error}") from error


def _read_limited_staged_bytes(
    path: Path,
    *,
    root: Path,
    max_bytes: int,
    too_large_code: str,
    too_large_message: str,
) -> bytes:
    try:
        _ensure_relative_to_root(path, root)
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=root)
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace file source {path}: {error}") from error
    except OSError as error:
        raise SHUDRuntimeError("WORKSPACE_READ_FAILED", f"Failed to read staged file {path}: {error}") from error
    if len(content) > max_bytes:
        raise SHUDRuntimeError(too_large_code, f"{too_large_message}: {path}")
    return content


def _bounded_nonempty_lines(
    text: str,
    *,
    path: Path,
    max_lines: int,
    max_line_bytes: int,
    too_many_lines_code: str,
    line_too_long_code: str,
) -> list[str]:
    lines: list[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if len(raw_line.encode("utf-8")) > max_line_bytes:
            raise SHUDRuntimeError(
                line_too_long_code,
                f"Direct-grid staging file line {line_number} exceeds {max_line_bytes} bytes: {path}",
            )
        stripped = raw_line.strip()
        if not stripped:
            continue
        lines.append(stripped)
        if len(lines) > max_lines:
            raise SHUDRuntimeError(
                too_many_lines_code,
                f"Direct-grid staging file exceeds {max_lines} non-empty lines: {path}",
            )
    return lines


def _open_log_file_no_follow(path: Path, *, containment_root: Path) -> Any:
    try:
        _ensure_relative_to_root(path, containment_root)
    except SafeFilesystemError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace log target {path}: {error}") from error
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as error:
        raise SHUDRuntimeError("WORKSPACE_PATH_UNSAFE", f"Unsafe workspace log target {path}: {error}") from error
    return os.fdopen(fd, "w", encoding="utf-8")


def _read_runtime_manifest_text_no_follow(path: Path) -> str:
    try:
        return _read_text_no_follow(path, containment_root=path.parent)
    except SHUDRuntimeError as error:
        if _caused_by_missing_path(error):
            raise SHUDRuntimeError("RUNTIME_MANIFEST_MISSING", f"Runtime manifest not found: {path}") from error
        raise


def _caused_by_missing_path(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, FileNotFoundError):
            return True
        current = current.__cause__
    return False


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


def _copy_direct_grid_station_csv_no_follow(source: Path, target: Path, *, root: Path) -> None:
    _write_staged_bytes(
        target,
        _read_limited_staged_bytes(
            source,
            root=root,
            max_bytes=MAX_DIRECT_GRID_FORCING_CSV_BYTES,
            too_large_code="DIRECT_GRID_FORCING_CSV_TOO_LARGE",
            too_large_message="Direct-grid SHUD forcing CSV exceeds the staging read cap",
        ),
        root=root,
    )


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
    runtime = manifest.get("runtime") or {}
    for section in (model, runtime):
        for key in ("project_name", "shud_input_name"):
            if section.get(key) is not None:
                _safe_path_component(section[key])


def _python_runtime_script_present(script: Path) -> bool:
    try:
        script_stat = os.stat(script)
    except OSError:
        return False
    return stat_module.S_ISREG(script_stat.st_mode)


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


def _materialized_model_input_dir(manifest: dict[str, Any], input_dir: Path, command_style: str) -> Path:
    project_name = _project_name(manifest)
    if _is_shud_project_mode(manifest, command_style) and input_dir.name != project_name:
        return input_dir / project_name
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
    if _is_shud_project_mode(manifest, str(runtime.get("command_style") or "")):
        return 0.0
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


def _read_shud_forcing_station_rows(path: Path, *, is_direct_grid: bool) -> list[dict[str, Any]]:
    if is_direct_grid:
        text = _read_limited_text_no_follow(
            path,
            containment_root=path.parent,
            max_bytes=MAX_DIRECT_GRID_TSD_FORC_BYTES,
            too_large_code="DIRECT_GRID_TSD_FORC_TOO_LARGE",
            too_large_message="Direct-grid SHUD forcing station file exceeds the staging read cap",
        )
        lines = _bounded_nonempty_lines(
            text,
            path=path,
            max_lines=MAX_DIRECT_GRID_TSD_FORC_LINES,
            max_line_bytes=MAX_DIRECT_GRID_STAGING_LINE_BYTES,
            too_many_lines_code="DIRECT_GRID_TSD_FORC_TOO_MANY_LINES",
            line_too_long_code="DIRECT_GRID_TSD_FORC_LINE_TOO_LONG",
        )
    else:
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
        filename_token = parts[6]
        filename = _direct_grid_station_filename(filename_token) if is_direct_grid else Path(filename_token).name
        rows.append(
            {
                "id": int(float(parts[0])),
                "longitude": float(parts[1]),
                "latitude": float(parts[2]),
                "x": float(parts[3]),
                "y": float(parts[4]),
                "z": float(parts[5]),
                "filename": filename,
            }
        )
    return rows


def _direct_grid_station_filename(raw_token: str) -> str:
    token = raw_token.strip()
    candidate = PurePosixPath(token)
    if (
        not token
        or "/" in token
        or "\\" in token
        or candidate.is_absolute()
        or token in {".", ".."}
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or not token.endswith(".csv")
    ):
        raise SHUDRuntimeError(
            "DIRECT_GRID_STATION_FILENAME_INVALID",
            f"Direct-grid SHUD forcing station Filename must be a safe single .csv filename: {raw_token}",
        )
    return token


def _mapping_metadata_declares_direct_grid(metadata: Mapping[str, Any]) -> bool:
    for payload in _iter_mapping_metadata_payloads(metadata):
        if str(payload.get("forcing_mapping_mode") or "").strip() == "direct_grid":
            return True
        if str(payload.get("spatial_mapping_method") or "").strip() == "direct_grid":
            return True
    return False


def _mapping_metadata_declares_non_direct_grid(metadata: Mapping[str, Any]) -> bool:
    for payload in _iter_mapping_metadata_payloads(metadata):
        for key in ("forcing_mapping_mode", "spatial_mapping_method"):
            value = str(payload.get(key) or "").strip()
            if value and value != "direct_grid":
                return True
    return False


def _iter_mapping_metadata_payloads(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = [metadata]
    for key in ("lineage", "lineage_json", "metadata", "properties", "properties_json"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            payloads.append(value)
    return payloads


def _forcing_package_manifest_uri(forcing: Mapping[str, Any]) -> str:
    return str(forcing.get("package_manifest_uri") or forcing.get("forcing_package_manifest_uri") or "").strip()


def _forcing_package_manifest_checksum(forcing: Mapping[str, Any]) -> str:
    return str(
        forcing.get("package_manifest_checksum") or forcing.get("forcing_manifest_checksum") or ""
    ).strip()


def _validate_direct_grid_station_filename_target(target: Path, *, model_input_dir: Path, project_name: str) -> None:
    reserved_names = {
        f"{project_name}.sp.att",
        f"{project_name}.sp.mesh",
        f"{project_name}.sp.riv",
        f"{project_name}.sp.rivseg",
        f"{project_name}.cfg.para",
        f"{project_name}.cfg.calib",
        f"{project_name}.cfg.ic",
        f"{project_name}.tsd.forc",
    }
    if target.name in reserved_names or _regular_file_exists(target, containment_root=model_input_dir):
        raise SHUDRuntimeError(
            "DIRECT_GRID_STATION_FILENAME_COLLISION",
            "Direct-grid SHUD forcing station filename collides with a staged model/runtime file: "
            f"{target.name}",
        )


def _validate_direct_grid_sp_att_forcing_ids(path: Path, allowed_ids: set[int]) -> None:
    observed_ids = _read_sp_att_forcing_ids(path)
    missing = sorted(observed_ids - allowed_ids)
    if missing:
        raise SHUDRuntimeError(
            "DIRECT_GRID_FORCING_OWNERSHIP_RANGE",
            "Direct-grid .sp.att FORC ownership references forcing IDs absent from staged .tsd.forc: "
            f"missing={missing}, allowed={sorted(allowed_ids)}.",
        )


def _read_sp_att_forcing_ids(path: Path) -> set[int]:
    if not _regular_file_exists(path, containment_root=path.parent):
        raise SHUDRuntimeError(
            "DIRECT_GRID_SP_ATT_MISSING",
            f"Direct-grid forcing requires staged .sp.att ownership file: {path}",
        )
    text = _read_limited_text_no_follow(
        path,
        containment_root=path.parent,
        max_bytes=MAX_DIRECT_GRID_SP_ATT_BYTES,
        too_large_code="DIRECT_GRID_SP_ATT_TOO_LARGE",
        too_large_message="Direct-grid .sp.att ownership file exceeds the staging read cap",
    )
    lines = _bounded_nonempty_lines(
        text,
        path=path,
        max_lines=MAX_DIRECT_GRID_SP_ATT_LINES,
        max_line_bytes=MAX_DIRECT_GRID_STAGING_LINE_BYTES,
        too_many_lines_code="DIRECT_GRID_SP_ATT_TOO_MANY_LINES",
        line_too_long_code="DIRECT_GRID_SP_ATT_LINE_TOO_LONG",
    )
    if len(lines) < 3:
        raise SHUDRuntimeError(
            "DIRECT_GRID_SP_ATT_INVALID",
            f"Direct-grid .sp.att ownership file has no element rows: {path}",
        )
    header_tokens = lines[1].split()
    try:
        forcing_column = next(index for index, token in enumerate(header_tokens) if token.upper() == "FORC")
    except StopIteration as error:
        raise SHUDRuntimeError(
            "DIRECT_GRID_SP_ATT_FORC_MISSING",
            f"Direct-grid .sp.att ownership file is missing FORC column: {path}",
        ) from error

    forcing_ids: set[int] = set()
    for line_number, line in enumerate(lines[2:], start=3):
        parts = line.split()
        if not parts:
            continue
        if len(parts) <= forcing_column:
            raise SHUDRuntimeError(
                "DIRECT_GRID_SP_ATT_FORC_MISSING",
                f"Direct-grid .sp.att row {line_number} is missing FORC value in {path}.",
            )
        value = parts[forcing_column]
        try:
            forcing_id = int(value)
        except ValueError as error:
            raise SHUDRuntimeError(
                "DIRECT_GRID_SP_ATT_FORC_INVALID",
                f"Direct-grid .sp.att row {line_number} has non-integer FORC value {value!r} in {path}.",
            ) from error
        forcing_ids.add(forcing_id)
    if not forcing_ids:
        raise SHUDRuntimeError(
            "DIRECT_GRID_SP_ATT_FORC_MISSING",
            f"Direct-grid .sp.att ownership file has no FORC values: {path}",
        )
    return forcing_ids


def _first_shud_forcing_time(path: Path, *, is_direct_grid: bool) -> datetime:
    if is_direct_grid:
        text = _read_limited_text_no_follow(
            path,
            containment_root=path.parent,
            max_bytes=MAX_DIRECT_GRID_FORCING_CSV_BYTES,
            too_large_code="DIRECT_GRID_FORCING_CSV_TOO_LARGE",
            too_large_message="Direct-grid SHUD forcing CSV exceeds the staging read cap",
        )
        lines = _bounded_nonempty_lines(
            text,
            path=path,
            max_lines=MAX_DIRECT_GRID_FORCING_CSV_LINES,
            max_line_bytes=MAX_DIRECT_GRID_STAGING_LINE_BYTES,
            too_many_lines_code="DIRECT_GRID_FORCING_CSV_TOO_MANY_LINES",
            line_too_long_code="DIRECT_GRID_FORCING_CSV_LINE_TOO_LONG",
        )
    else:
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


def _read_cfg_ic_header_minute(path: Path) -> float | None:
    """Return the SHUD ``.cfg.ic`` header minute-time, or None.

    The minute-time is the LAST numeric token in the header (shared rule with
    ``state_qc``), so a 4-token lake header ``<mesh> <river> <lake> <minute-time>``
    is read correctly rather than mistaking the lake count for the minute-time.
    """
    if not _regular_file_exists(path, containment_root=path.parent):
        return None
    lines = _read_text_no_follow(path, containment_root=path.parent).splitlines()
    if not lines:
        return None
    header = lines[0].split()
    return cfg_ic_header_minute_time(header)


def _shift_cfg_ic_time(path: Path, start_time: datetime) -> None:
    if not _regular_file_exists(path, containment_root=path.parent):
        return
    lines = _read_text_no_follow(path, containment_root=path.parent).splitlines()
    if not lines:
        return
    header = lines[0].split()
    minute_index = cfg_ic_header_minute_index(header)
    if minute_index is None:
        # Cannot locate the minute-time token (header lacks a count + minute-time
        # pair) -- leave the file untouched rather than corrupt it.
        return
    header[minute_index] = f"{_ensure_utc(start_time).timestamp() / 60.0:.6f}"
    lines[0] = "\t".join(header)
    _write_text_no_follow(path, "\n".join(lines) + "\n", containment_root=path.parent)


class _StateCheckpointTracker:
    def __init__(self, manifest: dict[str, Any], output_dir: Path) -> None:
        self.manifest = manifest
        self.output_dir = output_dir
        self.start_time = _parse_time(manifest["start_time"])
        self.project_name = _project_name(manifest)
        self.source_path = output_dir / f"{self.project_name}.cfg.ic.update"
        self.checkpoint_dir = output_dir / "state_checkpoints"
        self.targets = {
            int(hour): {
                "valid_time": self.start_time + timedelta(hours=int(hour)),
                "relative_minute": float(int(hour) * 60),
            }
            for hour in _state_checkpoint_hours(manifest)
        }
        self.captured: dict[int, dict[str, Any]] = {}

    def capture_available(self) -> None:
        if not self.targets:
            return
        header_minute = _read_cfg_ic_header_minute(self.source_path)
        if header_minute is None:
            return
        for hour, target in self.targets.items():
            if hour in self.captured:
                continue
            valid_time = target["valid_time"]
            if not _header_minute_matches_checkpoint(
                header_minute,
                valid_time=valid_time,
                relative_minute=target["relative_minute"],
            ):
                continue
            self._capture(hour, valid_time)

    def capture_final(self) -> None:
        self.capture_available()

    def write_manifest(self) -> None:
        if not self.captured:
            return
        _ensure_directory(self.checkpoint_dir, containment_root=self.output_dir)
        checkpoints = [self.captured[hour] for hour in sorted(self.captured)]
        _write_text_no_follow(
            self.checkpoint_dir / "state_checkpoints.json",
            json.dumps({"checkpoints": checkpoints}, indent=2, sort_keys=True) + "\n",
            containment_root=self.output_dir,
        )

    def _capture(self, hour: int, valid_time: datetime) -> None:
        _ensure_directory(self.checkpoint_dir, containment_root=self.output_dir)
        target = self.checkpoint_dir / f"{self.project_name}.f{hour:03d}.cfg.ic.update"
        _copy_staged_file_no_follow(self.source_path, target, root=self.output_dir)
        self.captured[hour] = {
            "lead_hours": hour,
            "valid_time": _format_time(valid_time),
            "path": str(target),
            "relative_path": str(target.relative_to(self.output_dir)),
            "original_shud_filename": self.source_path.name,
            "checkpoint_filename": target.name,
            "checksum": sha256_bytes(_read_staged_bytes(target, root=self.output_dir)),
        }


def _state_checkpoint_hours(manifest: dict[str, Any]) -> list[int]:
    runtime = manifest.get("runtime") or {}
    raw = runtime.get("state_checkpoint_hours") or runtime.get("state_checkpoints_hours") or []
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, list | tuple):
        values = list(raw)
    else:
        values = []
    hours: list[int] = []
    horizon = _forecast_horizon_hours(manifest)
    for value in values:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            continue
        if hour > 0 and (horizon is None or hour <= horizon) and hour not in hours:
            hours.append(hour)
    return sorted(hours)


def _forecast_horizon_hours(manifest: dict[str, Any]) -> int | None:
    value = manifest.get("forecast_horizon_hours") or (manifest.get("identity") or {}).get("forecast_horizon_hours")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _state_checkpoint_poll_seconds(manifest: dict[str, Any]) -> float:
    runtime = manifest.get("runtime") or {}
    value = runtime.get("state_checkpoint_poll_seconds")
    if value in (None, ""):
        return 0.1
    try:
        return max(float(value), 0.1)
    except (TypeError, ValueError):
        return 0.1


def _header_minute_matches_checkpoint(
    header_minute: float,
    *,
    valid_time: datetime,
    relative_minute: float,
) -> bool:
    rounded_header = round(header_minute)
    # SHUD writes forecast .cfg.ic.update headers as minutes since run start.
    if rounded_header == round(relative_minute):
        return True
    # Saved/warm-state fixtures and materialized canonical ICs may use epoch minutes.
    return rounded_header == round(_ensure_utc(valid_time).timestamp() / 60.0)


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


def _update_ic_step_minutes(manifest: dict[str, Any]) -> int | None:
    """Restart cadence (minutes) for SHUD ``Update_IC_STEP``, or None when unset.

    For forecast business runs this is a state-output cadence within the full
    product horizon, not a request to split the forecast into shorter SHUD runs.
    The runtime watches the long run and preserves selected ``cfg.ic.update``
    snapshots such as T+6/T+12 for later warm starts.
    """
    runtime = manifest.get("runtime") or {}
    value = runtime.get("update_ic_step_minutes") or runtime.get("Update_IC_STEP")
    return int(value) if value not in (None, "") else None


def _segment_count(manifest: dict[str, Any]) -> int:
    value = (
        manifest.get("output_segment_count")
        or manifest.get("shud_output_segment_count")
        or manifest.get("model", {}).get("output_segment_count")
        or manifest.get("outputs", {}).get("output_segment_count")
        or manifest.get("runtime", {}).get("output_river", {}).get("output_segment_count")
        or manifest.get("runtime", {}).get("output_river", {}).get("segment_count")
        or manifest.get("segment_count")
        or manifest.get("model", {}).get("segment_count")
        or manifest.get("model", {}).get("river_segment_count")
        or manifest.get("outputs", {}).get("segment_count")
    )
    if value is None:
        raise SHUDRuntimeError("SEGMENT_COUNT_MISSING", "segment_count is required for output verification.")
    return int(value)


def _project_name(manifest: dict[str, Any]) -> str:
    model = manifest.get("model") or {}
    runtime = manifest.get("runtime") or {}
    value = (
        model.get("project_name")
        or model.get("shud_input_name")
        or runtime.get("project_name")
        or runtime.get("shud_input_name")
        or model["model_id"]
    )
    return _safe_path_component(str(value))


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


def _authoritative_package_manifest_checksum_entries(
    package_manifest: Mapping[str, Any],
    *,
    forcing_uri: str,
    object_store_prefix: str,
) -> list[dict[str, str]]:
    files = package_manifest.get("files") or []
    if not isinstance(files, list):
        raise SHUDRuntimeError(
            "FORCING_CHECKSUM_INVALID",
            "Direct-grid forcing package manifest files must be a list.",
        )
    entries: list[dict[str, str]] = []
    for file_entry in files:
        if not isinstance(file_entry, Mapping):
            raise SHUDRuntimeError(
                "FORCING_CHECKSUM_INVALID",
                "Direct-grid forcing package manifest file entries must be objects.",
            )
        relative_path = _normalize_package_manifest_file_relative_path(
            file_entry,
            forcing_uri=forcing_uri,
            object_store_prefix=object_store_prefix,
        )
        checksum = str(file_entry.get("checksum") or "").strip()
        if not checksum:
            raise SHUDRuntimeError(
                "FORCING_CHECKSUM_MISSING",
                f"Direct-grid forcing package manifest entry is missing checksum: {relative_path}",
            )
        entries.append(
            {
                "role": str(file_entry.get("role") or ""),
                "relative_path": relative_path,
                "uri": str(file_entry.get("uri") or relative_path),
                "checksum": checksum,
            }
        )
    return entries


def _normalize_package_manifest_file_relative_path(
    file_entry: Mapping[str, Any],
    *,
    forcing_uri: str,
    object_store_prefix: str,
) -> str:
    relative_path = str(file_entry.get("relative_path") or "").strip()
    if not relative_path:
        relative_path = _derive_package_manifest_file_relative_path(
            file_entry,
            forcing_uri=forcing_uri,
            object_store_prefix=object_store_prefix,
        )
    candidate = PurePosixPath(relative_path)
    if not relative_path or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SHUDRuntimeError(
            "FORCING_FILE_PATH_INVALID",
            f"Direct-grid forcing package manifest relative_path is unsafe: {relative_path}",
        )
    return candidate.as_posix()


def _derive_package_manifest_file_relative_path(
    file_entry: Mapping[str, Any],
    *,
    forcing_uri: str,
    object_store_prefix: str,
) -> str:
    uri = str(file_entry.get("uri") or "").strip()
    if not uri:
        raise SHUDRuntimeError(
            "FORCING_CHECKSUM_MISSING",
            "Direct-grid forcing package manifest file entry is missing relative_path and uri.",
        )
    forcing_root = _object_key(forcing_uri, object_store_prefix).rstrip("/")
    file_key = _object_key(uri, object_store_prefix)
    if not forcing_root or file_key == forcing_root or not file_key.startswith(f"{forcing_root}/"):
        raise SHUDRuntimeError(
            "FORCING_FILE_PATH_INVALID",
            "Direct-grid forcing package manifest entry without relative_path must have a uri under forcing_uri: "
            f"{uri}",
        )
    return file_key[len(forcing_root) + 1 :]


def _direct_grid_sensitive_member_limit(relative_path: str) -> _DirectGridSensitiveMemberLimit | None:
    if relative_path == "shud/qhh.tsd.forc":
        return _DirectGridSensitiveMemberLimit(
            max_bytes=MAX_DIRECT_GRID_TSD_FORC_BYTES,
            error_code="DIRECT_GRID_TSD_FORC_TOO_LARGE",
            message="Direct-grid SHUD forcing station file exceeds the staging read cap",
        )
    if _is_direct_grid_station_csv_relative_path(relative_path):
        return _DirectGridSensitiveMemberLimit(
            max_bytes=MAX_DIRECT_GRID_FORCING_CSV_BYTES,
            error_code="DIRECT_GRID_FORCING_CSV_TOO_LARGE",
            message="Direct-grid SHUD forcing CSV exceeds the staging read cap",
        )
    return None


def _resolve_staged_forcing_path(model_input_dir: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate == Path(".") or candidate.is_absolute() or ".." in candidate.parts:
        raise SHUDRuntimeError(
            "FORCING_FILE_PATH_INVALID",
            f"Forcing checksum relative_path escapes model input directory: {relative_path}",
        )
    return model_input_dir / candidate


def _staged_relative_posix(model_input_dir: Path, staged_path: Path) -> str:
    try:
        relative = _absolute_lexical_path(staged_path).relative_to(_absolute_lexical_path(model_input_dir))
    except ValueError as error:
        raise SHUDRuntimeError(
            "FORCING_FILE_PATH_INVALID",
            f"Forcing checksum staged path escapes model input directory: {staged_path}",
        ) from error
    return PurePosixPath(*relative.parts).as_posix()


def _is_direct_grid_station_csv_relative_path(relative_path: str) -> bool:
    candidate = PurePosixPath(relative_path)
    return (
        len(candidate.parts) == 2
        and candidate.parts[0] == "shud"
        and candidate.parts[1].endswith(".csv")
    )


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
