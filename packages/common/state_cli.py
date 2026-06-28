from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.manifest_index import (
    MAX_MANIFEST_INDEX_BYTES,
    MAX_MANIFEST_INDEX_ENTRIES,
    SAFE_IDENTIFIER_RE,
    ManifestValidationError,
    load_manifest_entry,
    resolve_task_id,
    validate_manifest_index_entry_count,
)
from packages.common.object_store import LocalObjectStore
from packages.common.safe_fs import SafeFilesystemError, read_bytes_limited_no_follow
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
    StateManager,
    StateManagerError,
)
from packages.common.state_qc import cfg_ic_header_minute_index
from workers.data_adapters.base import cycle_id_for, parse_cycle_time


@dataclass(frozen=True)
class StateRunContext:
    run_id: str
    model_id: str
    end_time: datetime
    output_uri: str | None
    source_id: str | None = None
    cycle_time: datetime | None = None
    model_package_version: str | None = None
    model_package_checksum: str | None = None


@dataclass(frozen=True)
class StateCheckpoint:
    valid_time: datetime
    ic_file: Path
    original_shud_filename: str
    lead_hours: int | None = None


class StateRunRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @classmethod
    def from_env(cls) -> StateRunRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise StateManagerError("DATABASE_URL is required for state save operations.")
        return cls(database_url)

    def load_run_context(self, run_id: str) -> StateRunContext:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError as error:
            raise StateManagerError("psycopg2 is required for state save operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT
                        h.run_id,
                        h.model_id,
                        h.end_time,
                        h.output_uri,
                        h.source_id,
                        h.cycle_time,
                        mi.model_package_uri,
                        mi.resource_profile
                    FROM hydro.hydro_run h
                    LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                row = cursor.fetchone()
            if row is None:
                raise StateManagerError(f"hydro_run not found: {run_id}")
            return StateRunContext(
                run_id=str(row["run_id"]),
                model_id=str(row["model_id"]),
                end_time=_ensure_utc(row["end_time"]),
                output_uri=row.get("output_uri"),
                source_id=_optional_str(row.get("source_id")),
                cycle_time=_ensure_utc(row["cycle_time"]) if row.get("cycle_time") is not None else None,
                model_package_version=_optional_str(row.get("model_package_uri")),
                model_package_checksum=_package_checksum_from_resource_profile(row.get("resource_profile")),
            )
        except psycopg2.Error as error:
            raise StateManagerError(f"Failed to load hydro_run {run_id}: {error}") from error
        finally:
            if connection is not None:
                connection.close()


def save_state_for_run(
    run_id: str,
    *,
    manager: StateManager | None = None,
    repository: StateRunRepository | None = None,
    run_context: StateRunContext | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root or os.getenv("WORKSPACE_ROOT", ".")).expanduser().resolve()
    object_root = Path(os.getenv("OBJECT_STORE_ROOT", str(workspace))).expanduser().resolve()
    object_prefix = os.getenv("OBJECT_STORE_PREFIX", "")
    state_object_store = LocalObjectStore(object_root, object_prefix)
    state_manager = manager or _state_manager_from_env_for_save(state_object_store)
    if run_context is not None:
        run = run_context
        if run.run_id != run_id:
            raise StateManagerError(f"State save run context mismatch: {run.run_id} != {run_id}")
    else:
        run_repository = repository or _state_run_repository_from_env_for_save()
        run = run_repository.load_run_context(run_id)
    checkpoints = _find_state_checkpoints(run, workspace, state_manager.object_store)
    if not checkpoints:
        ic_file = _find_ic_file(run, workspace, state_manager.object_store)
        checkpoints = [
            StateCheckpoint(
                valid_time=run.end_time,
                ic_file=ic_file,
                original_shud_filename=ic_file.name,
                lead_hours=_lead_hours_from_run_valid_time(run, run.end_time),
            )
        ]
    # The native SHUD end-of-segment restart artifact is ``*.cfg.ic.update``; the
    # canonical object key is ``state.cfg.ic`` (state_manager._state_object_key). Record
    # the original SHUD filename and key the snapshot at end_time == T_{N+1} so the saved
    # interim state is valid at the next cycle's init time (M24 §2 Lane 2).
    saved = []
    for checkpoint in checkpoints:
        ic_file_path = _normalized_checkpoint_ic_file(checkpoint)
        result = state_manager.save_state_snapshot(
            model_id=run.model_id,
            run_id=run.run_id,
            valid_time=checkpoint.valid_time,
            ic_file_path=ic_file_path,
            source_id=run.source_id,
            cycle_id=_state_cycle_id(run),
            lead_hours=checkpoint.lead_hours,
            model_package_version=run.model_package_version,
            model_package_checksum=run.model_package_checksum,
            original_shud_filename=checkpoint.original_shud_filename,
        )
        qc_passed = state_manager.run_qc(result.state_id)
        saved.append(
            {
                "state_id": result.state_id,
                "status": result.status,
                "qc_passed": qc_passed,
                "state_uri": result.snapshot.state_uri,
                "checksum": result.snapshot.checksum,
                "valid_time": _format_time(result.snapshot.valid_time),
                "source_id": result.snapshot.source_id,
                "cycle_id": result.snapshot.cycle_id,
                "lead_hours": result.snapshot.lead_hours,
                "model_package_version": result.snapshot.model_package_version,
                "model_package_checksum": result.snapshot.model_package_checksum,
                "original_shud_filename": result.snapshot.original_shud_filename,
            }
        )
    first = saved[0]
    return {
        "run_id": run.run_id,
        "state_id": first["state_id"],
        "status": first["status"],
        "qc_passed": first["qc_passed"],
        "state_uri": first["state_uri"],
        "checksum": first["checksum"],
        "valid_time": first["valid_time"],
        "checkpoints": saved,
    }


def _normalized_checkpoint_ic_file(checkpoint: StateCheckpoint) -> Path:
    """Return an IC file whose header minute-time is absolute at valid_time."""

    content = checkpoint.ic_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines:
        return checkpoint.ic_file
    header = lines[0].split()
    minute_index = cfg_ic_header_minute_index(header)
    if minute_index is None:
        return checkpoint.ic_file
    expected_minute = _ensure_utc(checkpoint.valid_time).timestamp() / 60.0
    try:
        observed_minute = float(header[minute_index])
    except ValueError:
        return checkpoint.ic_file
    if round(observed_minute) == round(expected_minute):
        return checkpoint.ic_file
    normalized = checkpoint.ic_file.with_name(f".{checkpoint.ic_file.name}.normalized")
    header[minute_index] = f"{expected_minute:.6f}"
    lines[0] = "\t".join(header)
    normalized.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return normalized


def resolve_run_id(run_id: str | None, manifest_index: str | None, task_id: int | None) -> str:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = _load_state_save_manifest_entry(manifest_index, resolved_task_id)
        return str(entry["run_id"])
    if not run_id:
        raise ManifestValidationError(
            "Explicit state save requires --run-id.",
            {"missing_fields": ["run_id"]},
        )
    return run_id


def resolve_run_context(
    run_id: str | None,
    manifest_index: str | None,
    task_id: int | None,
) -> tuple[str, StateRunContext | None]:
    if manifest_index is not None:
        resolved_task_id = resolve_task_id(task_id)
        entry = _load_state_save_manifest_entry(manifest_index, resolved_task_id)
        resolved_run_id = str(entry["run_id"])
        if _db_free_state_save_enabled():
            return resolved_run_id, _state_run_context_from_manifest_entry(entry)
        return resolved_run_id, None
    resolved_run_id = resolve_run_id(run_id, manifest_index, task_id)
    if _db_free_state_save_enabled():
        return resolved_run_id, _state_run_context_from_env(resolved_run_id)
    return resolved_run_id, None


def _state_manager_from_env_for_save(object_store: LocalObjectStore) -> StateManager:
    if _db_free_state_save_enabled():
        return StateManager(
            repository=FileStateSnapshotIndexRepository.from_env(create_missing=True),
            object_store=object_store,
        )
    return StateManager(
        repository=PsycopgStateSnapshotRepository.from_env(),
        object_store=object_store,
    )


def _state_run_repository_from_env_for_save() -> StateRunRepository:
    if _db_free_state_save_enabled():
        raise StateManagerError(
            "DB-free state save requires run context from --manifest-index or NHMS_* runtime env; "
            "StateRunRepository.from_env() is not allowed."
        )
    return StateRunRepository.from_env()


def _db_free_state_save_enabled() -> bool:
    return _env_flag("NHMS_SCHEDULER_DB_FREE_REQUIRED") and os.getenv(
        "NHMS_SCHEDULER_STATE_INDEX_BACKEND", ""
    ).strip().lower() == "file"


def _load_state_save_manifest_entry(manifest_index: str, task_id: int) -> dict[str, Any]:
    if _db_free_state_save_enabled():
        return load_manifest_entry(manifest_index, task_id)
    try:
        return load_manifest_entry(manifest_index, task_id)
    except ManifestValidationError as strict_error:
        legacy = _load_legacy_state_save_manifest_entry(manifest_index, task_id)
        if legacy is not None:
            return legacy
        raise strict_error


def _load_legacy_state_save_manifest_entry(manifest_index: str, task_id: int) -> dict[str, Any] | None:
    try:
        raw = read_bytes_limited_no_follow(Path(manifest_index), max_bytes=MAX_MANIFEST_INDEX_BYTES)
        if len(raw) > MAX_MANIFEST_INDEX_BYTES:
            raise ManifestValidationError(
                "Manifest index file exceeds size limit",
                {"manifest_index_path": manifest_index, "size_limit": MAX_MANIFEST_INDEX_BYTES},
            )
        data = json.loads(raw.decode("utf-8"))
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestValidationError(
            f"Unable to safely read legacy state-save manifest index: {error}",
            {"manifest_index_path": manifest_index, "task_id": task_id, "error": str(error)},
        ) from error
    if not isinstance(data, list):
        return None
    validate_manifest_index_entry_count(len(data), max_entries=MAX_MANIFEST_INDEX_ENTRIES)
    if task_id < 0 or task_id >= len(data):
        return None
    entry = data[task_id]
    if not isinstance(entry, Mapping):
        return None
    result = dict(entry)
    if set(result).difference({"task_id", "run_id"}):
        return None
    try:
        stored_task_id = int(result.get("task_id"))
    except (TypeError, ValueError):
        return None
    run_id = str(result.get("run_id") or "")
    if stored_task_id != task_id or not run_id or SAFE_IDENTIFIER_RE.fullmatch(run_id) is None:
        return None
    return {"task_id": stored_task_id, "run_id": run_id}


def _state_run_context_from_manifest_entry(entry: Mapping[str, Any]) -> StateRunContext:
    assembly = entry.get("model_run_assembly") if isinstance(entry.get("model_run_assembly"), Mapping) else {}
    identity = assembly.get("identity") if isinstance(assembly.get("identity"), Mapping) else {}
    outputs = assembly.get("outputs") if isinstance(assembly.get("outputs"), Mapping) else {}
    model = assembly.get("model") if isinstance(assembly.get("model"), Mapping) else {}
    resource_profile = entry.get("resource_profile") if isinstance(entry.get("resource_profile"), Mapping) else {}
    run_id = str(entry["run_id"])
    end_time_value = (
        identity.get("end_time")
        or entry.get("end_time")
        or os.getenv("NHMS_END_TIME")
    )
    if end_time_value in (None, ""):
        raise StateManagerError("DB-free state save manifest entry is missing end_time.")
    model_id = str(entry.get("model_id") or identity.get("model_id") or "")
    if not model_id:
        raise StateManagerError("DB-free state save manifest entry is missing model_id.")
    source_id = _optional_str(entry.get("source_id") or identity.get("source_id"))
    cycle_time_value = entry.get("cycle_time") or identity.get("cycle_time")
    return StateRunContext(
        run_id=run_id,
        model_id=model_id,
        end_time=_parse_time_flexible(end_time_value),
        output_uri=_optional_str(entry.get("output_uri") or outputs.get("output_uri")),
        source_id=source_id,
        cycle_time=_parse_time_flexible(cycle_time_value) if cycle_time_value not in (None, "") else None,
        model_package_version=_optional_str(
            entry.get("model_package_uri")
            or identity.get("model_package_uri")
            or model.get("model_package_uri")
        ),
        model_package_checksum=_optional_str(
            entry.get("model_package_checksum")
            or entry.get("package_checksum")
            or identity.get("model_package_checksum")
            or model.get("model_package_checksum")
            or resource_profile.get("package_checksum")
        ),
    )


def _state_run_context_from_env(run_id: str) -> StateRunContext:
    model_id = os.getenv("NHMS_MODEL_ID", "").strip()
    end_time = os.getenv("NHMS_END_TIME", "").strip()
    if not model_id or not end_time:
        raise StateManagerError("DB-free state save requires NHMS_MODEL_ID and NHMS_END_TIME.")
    cycle_time = os.getenv("NHMS_CYCLE_TIME", "").strip()
    return StateRunContext(
        run_id=run_id,
        model_id=model_id,
        end_time=_parse_time_flexible(end_time),
        output_uri=None,
        source_id=_optional_str(os.getenv("NHMS_SOURCE_ID")),
        cycle_time=_parse_time_flexible(cycle_time) if cycle_time else None,
        model_package_version=_optional_str(os.getenv("NHMS_MODEL_PACKAGE_URI")),
        model_package_checksum=_optional_str(os.getenv("NHMS_MODEL_PACKAGE_CHECKSUM")),
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _find_ic_file(run: StateRunContext, workspace_root: Path, object_store: LocalObjectStore) -> Path:
    # Prefer the native SHUD end-of-segment restart artifact ``*.cfg.ic.update`` (the
    # interim T_{N+1} state written by the restart cadence); fall back to ``*.cfg.ic``.
    update_candidates: list[Path] = []
    ic_candidates: list[Path] = []

    def _collect(root: Path) -> None:
        update_candidates.extend(sorted(p for p in root.rglob("*.cfg.ic.update") if p.is_file()))
        ic_candidates.extend(
            sorted(p for p in root.rglob("*.cfg.ic") if p.is_file() and not p.name.endswith(".cfg.ic.update"))
        )

    workspace_output = workspace_root / "runs" / run.run_id / "output"
    if workspace_output.exists():
        _collect(workspace_output)

    if run.output_uri:
        output_path = _resolve_run_output_path(run, object_store)
        if output_path.is_file():
            if output_path.name.endswith(".cfg.ic.update"):
                update_candidates.append(output_path)
            elif output_path.name.endswith(".cfg.ic"):
                ic_candidates.append(output_path)
        elif output_path.is_dir():
            _collect(output_path)

    if update_candidates:
        return update_candidates[0]
    if ic_candidates:
        return ic_candidates[0]
    raise StateManagerError(f"No .cfg.ic / .cfg.ic.update state file found for run {run.run_id}.")


def _find_state_checkpoints(
    run: StateRunContext,
    workspace_root: Path,
    object_store: LocalObjectStore,
) -> list[StateCheckpoint]:
    manifests: list[Path] = []

    workspace_manifest = (
        workspace_root / "runs" / run.run_id / "output" / "state_checkpoints" / "state_checkpoints.json"
    )
    if workspace_manifest.exists():
        manifests.append(workspace_manifest)

    if run.output_uri:
        output_path = _resolve_run_output_path(run, object_store)
        if output_path.is_dir():
            object_manifest = output_path / "state_checkpoints" / "state_checkpoints.json"
            if object_manifest.exists() and object_manifest not in manifests:
                manifests.append(object_manifest)

    for manifest_path in manifests:
        checkpoints = _load_state_checkpoint_manifest(manifest_path)
        if checkpoints:
            return checkpoints
    return []


def _load_state_checkpoint_manifest(manifest_path: Path) -> list[StateCheckpoint]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateManagerError(f"Invalid state checkpoint manifest {manifest_path}: {error}") from error
    raw_checkpoints = payload.get("checkpoints") if isinstance(payload, dict) else None
    if not isinstance(raw_checkpoints, Sequence) or isinstance(raw_checkpoints, str | bytes):
        return []
    output_root = manifest_path.parent.parent
    checkpoints: list[StateCheckpoint] = []
    for raw in raw_checkpoints:
        if not isinstance(raw, dict):
            continue
        relative_path = str(raw.get("relative_path") or "").strip()
        valid_time = raw.get("valid_time")
        if not relative_path or not valid_time:
            continue
        path = (output_root / relative_path).resolve(strict=False)
        try:
            path.relative_to(output_root.resolve(strict=False))
        except ValueError as error:
            raise StateManagerError(f"State checkpoint path escapes output directory: {relative_path}") from error
        if not path.is_file():
            continue
        checkpoints.append(
            StateCheckpoint(
                valid_time=_ensure_utc(_parse_time(str(valid_time))),
                ic_file=path,
                original_shud_filename=str(raw.get("checkpoint_filename") or path.name),
                lead_hours=_optional_int(raw.get("lead_hours")),
            )
        )
    checkpoints.sort(key=lambda item: item.valid_time)
    return checkpoints


def _resolve_run_output_path(run: StateRunContext, object_store: LocalObjectStore) -> Path:
    """Resolve ``hydro_run.output_uri`` for either a run output directory or file."""

    if not run.output_uri:
        raise StateManagerError(f"hydro_run {run.run_id} has no output_uri.")
    try:
        key = object_store.normalize_key(run.output_uri)
    except ValueError as error:
        raise StateManagerError(f"Invalid output_uri for run {run.run_id}: {error}") from error

    parts = Path(key).parts
    expected_prefix = ("runs", run.run_id, "output")
    if parts[: len(expected_prefix)] != expected_prefix:
        raise StateManagerError(
            f"output_uri for run {run.run_id} must be under runs/{run.run_id}/output/: {run.output_uri}"
        )

    if len(parts) > len(expected_prefix):
        try:
            return object_store.resolve_path(run.output_uri)
        except ValueError as error:
            raise StateManagerError(f"Invalid output object for run {run.run_id}: {error}") from error

    output_path = object_store.root.joinpath(*parts)
    try:
        output_path.relative_to(object_store.root)
    except ValueError as error:
        raise StateManagerError(
            f"output_uri escapes object store root for run {run.run_id}: {run.output_uri}"
        ) from error
    return output_path


def _click_main(argv: Sequence[str] | None = None) -> int:
    import click

    @click.group()
    def cli() -> None:
        pass

    @cli.command("save")
    @click.option("--run-id")
    @click.option("--manifest-index")
    @click.option("--task-id", type=int, default=None)
    def save(run_id: str | None, manifest_index: str | None, task_id: int | None) -> None:
        try:
            resolved_run_id, run_context = resolve_run_context(run_id, manifest_index, task_id)
            result = (
                save_state_for_run(resolved_run_id, run_context=run_context)
                if run_context is not None
                else save_state_for_run(resolved_run_id)
            )
            click.echo(
                json.dumps(
                    result,
                    sort_keys=True,
                )
            )
        except ManifestValidationError as error:
            click.echo(f"{error.error_code}: {error.message}", err=True)
            raise SystemExit(1) from error
        except StateManagerError as error:
            click.echo(str(error), err=True)
            raise SystemExit(1) from error

    cli.main(args=list(argv) if argv is not None else None, standalone_mode=True)
    return 0


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nhms-state")
    subparsers = parser.add_subparsers(dest="command", required=True)
    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--run-id")
    save_parser.add_argument("--manifest-index")
    save_parser.add_argument("--task-id", type=int, default=None)
    args = parser.parse_args(argv)

    if args.command == "save":
        try:
            resolved_run_id, run_context = resolve_run_context(args.run_id, args.manifest_index, args.task_id)
            result = (
                save_state_for_run(resolved_run_id, run_context=run_context)
                if run_context is not None
                else save_state_for_run(resolved_run_id)
            )
            print(json.dumps(result, sort_keys=True))
        except ManifestValidationError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except StateManagerError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import click  # noqa: F401
    except ImportError:
        return _argparse_main(argv)
    return _click_main(argv)


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _ensure_utc(parsed)


def _parse_time_flexible(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    text = str(value)
    try:
        return _ensure_utc(parse_cycle_time(text))
    except ValueError:
        return _parse_time(text)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _state_cycle_id(run: StateRunContext) -> str | None:
    if run.source_id is None or run.cycle_time is None:
        return None
    return cycle_id_for(run.source_id, run.cycle_time)


def _lead_hours_from_run_valid_time(run: StateRunContext, valid_time: datetime) -> int | None:
    if run.cycle_time is None:
        return None
    elapsed_seconds = (_ensure_utc(valid_time) - _ensure_utc(run.cycle_time)).total_seconds()
    if elapsed_seconds < 0:
        return None
    return int(round(elapsed_seconds / 3600.0))


def _package_checksum_from_resource_profile(value: Any) -> str | None:
    profile: Any = value
    if isinstance(value, str):
        try:
            profile = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(profile, dict):
        return None
    return _optional_str(profile.get("package_checksum"))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
