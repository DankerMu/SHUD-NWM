from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import InitVar, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore, sha256_bytes
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
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
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
        for directory in (input_dir, output_dir, log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        (input_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

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
        self._stage_artifact(manifest["model"]["model_package_uri"], input_dir)
        self._stage_artifact(manifest["forcing"]["forcing_uri"], input_dir)
        self._stage_initial_state(manifest, input_dir)

        for suffix in (".mesh", ".para", ".calib", ".tsd.forc"):
            matches = [path for path in input_dir.rglob(f"*{suffix}") if path.is_file() and path.stat().st_size > 0]
            if not matches:
                raise SHUDRuntimeError("WORKSPACE_INCOMPLETE", f"Missing required staged file: *{suffix}")
        if _initial_state_uri(manifest):
            matches = [path for path in input_dir.rglob("*.cfg.ic") if path.is_file() and path.stat().st_size > 0]
            if not matches:
                raise SHUDRuntimeError(
                    "INIT_STATE_INCOMPLETE",
                    "Initial state URI did not stage a non-empty *.cfg.ic file.",
                )

    def generate_cfg_para(self, manifest: dict[str, Any], input_dir: Path, output_dir: Path) -> Path:
        template_path = _first_file(input_dir, "*.cfg.para") or _first_file(input_dir, "*.para")
        if template_path is None:
            raise SHUDRuntimeError("CFG_TEMPLATE_MISSING", "No .para template found in staged model package.")

        output_path = input_dir / f"{_project_name(manifest)}.cfg.para"
        content = template_path.read_text(encoding="utf-8")
        content = "\n".join(line for line in content.splitlines() if ".cfg.ic" not in line)

        init_mode = _init_mode(manifest)
        replacements = {
            "START_TIME": _format_time(_parse_time(manifest["start_time"])),
            "END_TIME": _format_time(_parse_time(manifest["end_time"])),
            "OUTPUT_DIR": str(output_dir),
            "MODEL_OUTPUT_INTERVAL": str(_output_interval_minutes(manifest, self.config.output_interval_minutes)),
            "INIT_MODE": init_mode,
            "SEGMENT_COUNT": str(_segment_count(manifest)),
        }
        for key, value in replacements.items():
            content = _replace_or_append(content, key, value)
        output_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        return output_path

    def run_shud(
        self,
        manifest: dict[str, Any],
        cfg_path: Path,
        workspace: Path,
        output_dir: Path,
        log_dir: Path,
    ) -> None:
        command = _runtime_command(self.config.shud_executable, cfg_path)
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
            (log_dir / "shud_stdout.log").write_text(_subprocess_output_text(error.stdout), encoding="utf-8")
            (log_dir / "shud_stderr.log").write_text(_subprocess_output_text(error.stderr), encoding="utf-8")
            message = f"shud_omp timed out after {self.config.timeout_seconds}s"
            raise SHUDRuntimeError("SHUD_TIMEOUT", message) from error

        (log_dir / "shud_stdout.log").write_text(completed.stdout or "", encoding="utf-8")
        (log_dir / "shud_stderr.log").write_text(completed.stderr or "", encoding="utf-8")
        if completed.returncode != 0:
            detail = _last_lines(completed.stderr or "", 50)
            raise SHUDRuntimeError(
                f"SHUD_EXIT_{completed.returncode}",
                f"shud_omp exited with code {completed.returncode}: {detail}",
            )
        output_dir.mkdir(parents=True, exist_ok=True)

    def verify_output(self, manifest: dict[str, Any], output_dir: Path) -> Path:
        files = sorted([*output_dir.glob("*.rivqdown"), *output_dir.glob("*.rivqdown.csv")])
        if not files:
            raise SHUDRuntimeError(
                "OUTPUT_MISSING",
                "Output verification failed: .rivqdown file not found",
            )
        rivqdown = files[0]
        lines = [line.strip() for line in rivqdown.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise SHUDRuntimeError("OUTPUT_EMPTY", "Output verification failed: .rivqdown file is empty")

        expected_columns = _segment_count(manifest) + 1
        header_tokens = _split_row(lines[0])
        has_header = any(not _is_number(token) for token in header_tokens)
        data_lines = lines[1:] if has_header else lines
        expected_timesteps = _expected_timesteps(manifest, self.config.output_interval_minutes)
        if len(data_lines) != expected_timesteps:
            raise SHUDRuntimeError(
                "OUTPUT_ROW_COUNT_MISMATCH",
                f"Output verification failed: expected {expected_timesteps} data rows, found {len(data_lines)}",
            )
        for line_number, line in enumerate(data_lines, start=2 if has_header else 1):
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
        (input_dir / "manifest.json").write_text(content, encoding="utf-8")
        run_manifest_uri = _run_manifest_uri(manifest)
        self.object_store.write_bytes_atomic(run_manifest_uri, content.encode("utf-8"))

    def _stage_initial_state(self, manifest: dict[str, Any], input_dir: Path) -> None:
        if not _initial_state_id(manifest) and not _initial_state_uri(manifest):
            _set_cold_start_initial_state(manifest, quality=_initial_state_quality(manifest) or "cold_start_no_state")
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

        matches = sorted(path for path in input_dir.rglob("*.cfg.ic") if path.is_file() and path.stat().st_size > 0)
        if not matches:
            return None, None, "Initial state URI did not stage a non-empty *.cfg.ic file."

        staged_path = matches[0]
        try:
            return staged_path, sha256_bytes(staged_path.read_bytes()), None
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
        for path in input_dir.rglob("*.cfg.ic"):
            if path.is_file():
                path.unlink()

    def _sync_init_state_id(self, manifest: dict[str, Any]) -> None:
        update_init_state = getattr(self.repository, "update_init_state", None)
        if callable(update_init_state):
            update_init_state(manifest["run_id"], _initial_state_id(manifest))

    def _stage_artifact(self, uri: str, destination: Path) -> None:
        source = self._object_store_path(uri)
        if not source.exists():
            raise SHUDRuntimeError("ARTIFACT_NOT_FOUND", f"Object storage artifact not found: {uri}")

        if source.is_dir():
            for file in source.rglob("*"):
                if file.is_file():
                    relative = file.relative_to(source)
                    target = destination / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
            return

        if tarfile.is_tarfile(source):
            with tarfile.open(source) as archive:
                archive.extractall(destination, filter="data")
            return

        target = destination / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def _upload_directory(self, directory: Path, key_prefix: str) -> None:
        for file in directory.rglob("*"):
            if not file.is_file():
                continue
            relative = file.relative_to(directory)
            key = f"{key_prefix.rstrip('/')}/{relative.as_posix()}"
            content = file.read_bytes()
            for attempt in range(self.config.upload_retries):
                try:
                    target = self.object_store.resolve_path(key)
                    if file.resolve() == target.resolve():
                        break
                    self.object_store.write_bytes_atomic(key, content)
                    break
                except Exception as error:
                    if attempt + 1 >= self.config.upload_retries:
                        raise SHUDRuntimeError("UPLOAD_FAILED", f"Failed to upload {key}: {error}") from error
                    time.sleep(0.1 * (2**attempt))

    def _object_store_path(self, uri_or_key: str) -> Path:
        key = _object_key(uri_or_key, self.config.object_store_prefix)
        path = (Path(self.config.object_store_root) / key).resolve()
        try:
            path.relative_to(Path(self.config.object_store_root))
        except ValueError as error:
            message = f"Object key escapes object store root: {uri_or_key}"
            raise SHUDRuntimeError("INVALID_OBJECT_KEY", message) from error
        return path

    def _directory_uri(self, key_prefix: str) -> str:
        prefix = self.config.object_store_prefix.rstrip("/")
        if not prefix:
            return key_prefix.rstrip("/") + "/"
        return f"{prefix}/{key_prefix.strip('/')}/"

    def _write_failure_log(self, log_dir: Path, error: SHUDRuntimeError) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "runtime_error.log").write_text(f"{error.error_code}: {error.message}\n", encoding="utf-8")


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


def _runtime_command(shud_executable: str, cfg_path: Path) -> list[str]:
    executable = str(shud_executable)
    args = [str(cfg_path)]
    if executable.endswith(".py"):
        return [sys.executable, executable, *args]
    return [executable, *args]


def _replace_or_append(content: str, key: str, value: str) -> str:
    patterns = (
        f"{{{{{key}}}}}",
        f"${{{key}}}",
    )
    for pattern in patterns:
        if pattern in content:
            content = content.replace(pattern, value)

    lines = content.splitlines()
    replaced = False
    prefix = f"{key} ="
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix) or stripped.startswith(f"{key}="):
            lines[index] = f"{key} = {value}"
            replaced = True
    if not replaced:
        lines.append(f"{key} = {value}")
    return "\n".join(lines)


def _first_file(directory: Path, pattern: str) -> Path | None:
    matches = sorted(path for path in directory.rglob(pattern) if path.is_file())
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
