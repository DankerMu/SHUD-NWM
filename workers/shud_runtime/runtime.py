from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore


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
    executable: str = "shud_omp"
    output_interval_minutes: int = 1440
    timeout_seconds: int = 3600
    upload_retries: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())

    @classmethod
    def from_env(cls) -> SHUDRuntimeConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            workspace_root=workspace_root,
            object_store_root=os.getenv("OBJECT_STORE_ROOT", workspace_root),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            executable=os.getenv("SHUD_EXECUTABLE", "shud_omp"),
            output_interval_minutes=int(os.getenv("MODEL_OUTPUT_INTERVAL", "1440")),
            timeout_seconds=int(os.getenv("SHUD_TIMEOUT_SECONDS", "3600")),
        )


@dataclass(frozen=True)
class SHUDExecutionResult:
    run_id: str
    status: str
    output_uri: str | None
    log_uri: str | None
    rivqdown_file: str | None


class NullHydroRunRepository:
    """Repository used by unit tests and local dry runs when no DB is injected."""

    def create_run(self, _manifest: dict[str, Any], _run_manifest_uri: str) -> dict[str, Any]:
        return {}

    def update_status(self, _run_id: str, _status: str, **_fields: Any) -> dict[str, Any]:
        return {}

    def mark_failed(self, _run_id: str, _error_code: str, _error_message: str, **_fields: Any) -> dict[str, Any]:
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
        return self._fetch_one(
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
            VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, 'created', %s)
            ON CONFLICT (run_id) DO UPDATE SET
                status = 'created',
                error_code = NULL,
                error_message = NULL,
                output_uri = NULL,
                log_uri = NULL,
                updated_at = now()
            RETURNING *
            """,
            (
                manifest["run_id"],
                manifest.get("run_type", "forecast"),
                manifest.get("scenario_id", "forecast_gfs_deterministic"),
                manifest["model"]["model_id"],
                manifest["model"]["basin_version_id"],
                manifest.get("forcing", {}).get("forcing_version_id"),
                manifest.get("source_id"),
                _parse_time_or_none(manifest.get("cycle_time")),
                _parse_time(manifest["start_time"]),
                _parse_time(manifest["end_time"]),
                run_manifest_uri,
            ),
        )

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

    def _fetch_one(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
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
                if row is None:
                    raise SHUDRuntimeError("HYDRO_RUN_NOT_FOUND", "hydro_run update did not return a row.")
                return dict(row)
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
    ) -> None:
        self.config = config
        self.repository = repository or NullHydroRunRepository()
        self.object_store = object_store or LocalObjectStore(config.object_store_root, config.object_store_prefix)

    @classmethod
    def from_env(cls) -> SHUDRuntime:
        config = SHUDRuntimeConfig.from_env()
        repository: Any
        if os.getenv("DATABASE_URL", "").strip():
            repository = PsycopgHydroRunRepository.from_env()
        else:
            repository = NullHydroRunRepository()
        return cls(config=config, repository=repository)

    def execute_manifest_path(self, manifest_path: str | Path) -> SHUDExecutionResult:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        return self.execute(manifest)

    def execute(self, manifest: dict[str, Any]) -> SHUDExecutionResult:
        run_id = manifest["run_id"]
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

        for suffix in (".mesh", ".para", ".calib", ".tsd.forc"):
            matches = [path for path in input_dir.rglob(f"*{suffix}") if path.is_file() and path.stat().st_size > 0]
            if not matches:
                raise SHUDRuntimeError("WORKSPACE_INCOMPLETE", f"Missing required staged file: *{suffix}")

    def generate_cfg_para(self, manifest: dict[str, Any], input_dir: Path, output_dir: Path) -> Path:
        template_path = _first_file(input_dir, "*.cfg.para") or _first_file(input_dir, "*.para")
        if template_path is None:
            raise SHUDRuntimeError("CFG_TEMPLATE_MISSING", "No .para template found in staged model package.")

        output_path = input_dir / f"{_project_name(manifest)}.cfg.para"
        content = template_path.read_text(encoding="utf-8")
        content = "\n".join(line for line in content.splitlines() if ".cfg.ic" not in line)

        replacements = {
            "START_TIME": _format_time(_parse_time(manifest["start_time"])),
            "END_TIME": _format_time(_parse_time(manifest["end_time"])),
            "OUTPUT_DIR": str(output_dir),
            "MODEL_OUTPUT_INTERVAL": str(_output_interval_minutes(manifest, self.config.output_interval_minutes)),
            "INIT_MODE": "cold-start",
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
        command = _runtime_command(manifest, self.config.executable, cfg_path)
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
            (log_dir / "shud_stdout.log").write_text(error.stdout or "", encoding="utf-8")
            (log_dir / "shud_stderr.log").write_text(error.stderr or "", encoding="utf-8")
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
        self._upload_directory(output_dir, f"runs/{run_id}/output")
        log_uri = self.upload_logs(run_id, log_dir)
        return {
            "output_uri": self._directory_uri(f"runs/{run_id}/output"),
            "log_uri": log_uri,
        }

    def upload_logs(self, run_id: str, log_dir: Path) -> str:
        self._upload_directory(log_dir, f"runs/{run_id}/logs")
        return self._directory_uri(f"runs/{run_id}/logs")

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


def _runtime_command(manifest: dict[str, Any], default_executable: str, cfg_path: Path) -> list[str]:
    runtime = manifest.get("runtime") or {}
    command = runtime.get("command")
    if command:
        return [str(part) for part in command]

    executable = str(runtime.get("executable") or default_executable)
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
    return str(manifest.get("model", {}).get("project_name") or manifest["model"]["model_id"])


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
        or f"runs/{manifest['run_id']}/input/manifest.json"
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


def _as_runtime_error(error: Exception) -> SHUDRuntimeError:
    if isinstance(error, SHUDRuntimeError):
        return error
    return SHUDRuntimeError("RUNTIME_ERROR", str(error))
