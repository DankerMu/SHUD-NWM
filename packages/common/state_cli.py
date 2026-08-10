from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.manifest_index import (
    MAX_MANIFEST_INDEX_BYTES,
    MAX_MANIFEST_INDEX_ENTRIES,
    SAFE_IDENTIFIER_RE,
    ManifestValidationError,
    load_manifest_entry,
    resolve_task_id,
    validate_manifest_index_entry_count,
)
from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    read_bytes_limited_no_follow,
    stat_no_follow,
)
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    PsycopgStateSnapshotRepository,
    StateManager,
    StateManagerError,
)
from packages.common.state_qc import (
    MAX_STATE_IC_BYTES,
    cfg_ic_header_minute_index,
    normalize_state_negative_residuals,
)
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


MAX_STATE_CHECKPOINT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES = 10_000

# #1325 publish-side admission reasons. Each leads the ``StateManagerError``
# message so the Slurm task's stderr is machine-greppable: the compute-node CLI
# is DB-free, so the token plus the nonzero exit IS the evidence plane.
STATE_SAVE_SOURCE_OUTPUT_MISSING = "STATE_SAVE_SOURCE_OUTPUT_MISSING"
STATE_SAVE_SOURCE_MANIFEST_MISSING = "STATE_SAVE_SOURCE_MANIFEST_MISSING"
STATE_SAVE_SOURCE_PROVENANCE_MISSING = "STATE_SAVE_SOURCE_PROVENANCE_MISSING"
STATE_SAVE_SOURCE_PROVENANCE_MISMATCH = "STATE_SAVE_SOURCE_PROVENANCE_MISMATCH"
STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE = "STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE"
STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH = "STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH"
STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED = "STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED"
STATE_SAVE_SOURCE_FINAL_IC_MISSING = "STATE_SAVE_SOURCE_FINAL_IC_MISSING"

_REQUIRED_PROVENANCE_KEYS = (
    "run_id",
    "generated_at",
    "slurm_job_id",
    "array_task_id",
    "requested_checkpoint_hours",
)


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
    _db_free_state_save_enabled()
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
    # #1325: admission runs BEFORE any artifact selection. Nothing below may
    # search the tree — only artifacts the verified manifest names and
    # checksums are publishable, so a killed/cleaned run can no longer mint a
    # successor state that looks healthy downstream.
    source = _admit_state_publish_source(run, workspace, state_manager.object_store)
    if source.final_ic is None:
        checkpoints = [
            _checkpoint_with_header_time(checkpoint, run)
            for checkpoint in _load_state_checkpoint_manifest(source.manifest_path)
        ]
        if not checkpoints:
            # The gate verified a non-empty declared set, so an empty parse means
            # the tree changed under us between the hash and this read. Answer
            # with the typed reason rather than an IndexError off ``saved[0]``.
            raise StateManagerError(
                f"{STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE}: manifest {source.manifest_path} declared checkpoint "
                "artifacts that no longer resolve at publish time."
            )
    else:
        ic_file = source.output_root / str(source.final_ic["relative_path"])
        checkpoints = [
            StateCheckpoint(
                valid_time=run.end_time,
                ic_file=ic_file,
                original_shud_filename=str(source.final_ic.get("original_shud_filename") or ic_file.name),
                lead_hours=_lead_hours_from_run_valid_time(run, run.end_time),
            )
        ]
    # The native SHUD end-of-segment restart artifact is ``*.cfg.ic.update``; the
    # canonical object key is ``state.cfg.ic`` (state_manager._state_object_key). Record
    # the original SHUD filename and key the snapshot at end_time == T_{N+1} so the saved
    # interim state is valid at the next cycle's init time (M24 §2 Lane 2).
    saved = []
    for checkpoint in checkpoints:
        ic_file_path, normalization_evidence = _normalized_checkpoint_ic_file(checkpoint)
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
                "state_normalization": normalization_evidence,
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


def _normalized_checkpoint_ic_file(checkpoint: StateCheckpoint) -> tuple[Path, dict[str, Any]]:
    """Return a canonical IC with exact time and bounded physical floors."""

    content = _read_limited_text_no_follow(
        checkpoint.ic_file,
        max_bytes=MAX_STATE_IC_BYTES,
        label="state checkpoint IC file",
    )
    normalization = normalize_state_negative_residuals(content)
    if not normalization.accepted:
        raise StateManagerError(f"State checkpoint residual normalization rejected: {normalization.reason}")
    lines = normalization.content.splitlines()
    if not lines:
        return checkpoint.ic_file, normalization.evidence()
    header = lines[0].split()
    minute_index = cfg_ic_header_minute_index(header)
    if minute_index is None:
        if normalization.normalized_value_count == 0:
            return checkpoint.ic_file, normalization.evidence()
        normalized = checkpoint.ic_file.with_name(f".{checkpoint.ic_file.name}.normalized")
        atomic_write_bytes_no_follow(normalized, ("\n".join(lines) + "\n").encode("utf-8"))
        return normalized, normalization.evidence()
    expected_minute = _ensure_utc(checkpoint.valid_time).timestamp() / 60.0
    try:
        observed_minute = float(header[minute_index])
    except ValueError:
        observed_minute = expected_minute
    header_changed = round(observed_minute) != round(expected_minute)
    if not header_changed and normalization.normalized_value_count == 0:
        return checkpoint.ic_file, normalization.evidence()
    normalized = checkpoint.ic_file.with_name(f".{checkpoint.ic_file.name}.normalized")
    if header_changed:
        header[minute_index] = f"{expected_minute:.6f}"
        lines[0] = "\t".join(header)
    atomic_write_bytes_no_follow(normalized, ("\n".join(lines) + "\n").encode("utf-8"))
    return normalized, normalization.evidence()


def _checkpoint_with_header_time(checkpoint: StateCheckpoint, run: StateRunContext) -> StateCheckpoint:
    """Trust the checkpoint IC header when it proves a different model time."""

    observed_minute = _checkpoint_header_minute(checkpoint.ic_file)
    if observed_minute is None or run.cycle_time is None:
        return checkpoint
    inferred_valid_time = _valid_time_from_header_minute(observed_minute, run)
    if inferred_valid_time is None:
        return checkpoint
    lead_hours = _lead_hours_from_run_valid_time(run, inferred_valid_time)
    if lead_hours is None:
        return checkpoint
    if _ensure_utc(checkpoint.valid_time) == inferred_valid_time and checkpoint.lead_hours == lead_hours:
        return checkpoint
    return StateCheckpoint(
        valid_time=inferred_valid_time,
        ic_file=checkpoint.ic_file,
        original_shud_filename=checkpoint.original_shud_filename,
        lead_hours=lead_hours,
    )


def _checkpoint_header_minute(path: Path) -> float | None:
    content = _read_limited_text_no_follow(
        path,
        max_bytes=MAX_STATE_IC_BYTES,
        label="state checkpoint IC file",
    )
    lines = content.splitlines()
    if not lines:
        return None
    header = lines[0].split()
    minute_index = cfg_ic_header_minute_index(header)
    if minute_index is None:
        return None
    try:
        return float(header[minute_index])
    except ValueError:
        return None


def _valid_time_from_header_minute(observed_minute: float, run: StateRunContext) -> datetime | None:
    if run.cycle_time is None:
        return None
    cycle_time = _ensure_utc(run.cycle_time)
    end_time = _ensure_utc(run.end_time)
    rounded_minute = round(observed_minute)
    if 0 <= rounded_minute <= round((end_time - cycle_time).total_seconds() / 60):
        return cycle_time + timedelta(minutes=rounded_minute)
    try:
        inferred = datetime.fromtimestamp(observed_minute * 60, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if cycle_time <= inferred <= end_time:
        return inferred
    return None


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
        _require_db_free_state_index_destination()
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
    if not _env_flag("NHMS_SCHEDULER_DB_FREE_REQUIRED"):
        return False
    backend = os.getenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", "").strip().lower()
    if backend != "file":
        raise StateManagerError("DB-free state save requires NHMS_SCHEDULER_STATE_INDEX_BACKEND=file.")
    return True


def _require_db_free_state_index_destination() -> None:
    index_uri = os.getenv("NHMS_SCHEDULER_STATE_INDEX", "").strip()
    if not index_uri:
        raise StateManagerError("NHMS_SCHEDULER_STATE_INDEX is required for DB-free state save.")
    parsed = urlparse(index_uri)
    if parsed.scheme in {"s3", "published"}:
        return
    if parsed.scheme:
        raise StateManagerError("DB-free state save NHMS_SCHEDULER_STATE_INDEX uses an unsupported URI scheme.")
    destination = Path(index_uri).expanduser()
    destination = destination if destination.is_absolute() else Path.cwd() / destination
    destination = destination.resolve(strict=False)
    allowed_roots = _db_free_state_index_allowed_roots()
    if not allowed_roots:
        raise StateManagerError("DB-free state save requires an allowed root for NHMS_SCHEDULER_STATE_INDEX.")
    if not any(_path_is_relative_to(destination, root) for root in allowed_roots):
        raise StateManagerError("DB-free state save NHMS_SCHEDULER_STATE_INDEX local path is outside allowed roots.")


def _db_free_state_index_allowed_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    raw_values = [value for value in os.getenv("NHMS_SCHEDULER_ALLOWED_ROOTS", "").split(os.pathsep) if value.strip()]
    for value in raw_values:
        if value in (None, ""):
            continue
        root = Path(str(value)).expanduser()
        root = root if root.is_absolute() else Path.cwd() / root
        resolved = root.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
    cycle_time = _parse_time_flexible(cycle_time_value) if cycle_time_value not in (None, "") else None
    model_package_version = _optional_str(
        entry.get("model_package_uri")
        or identity.get("model_package_uri")
        or model.get("model_package_uri")
    )
    model_package_checksum = _optional_str(
        entry.get("model_package_checksum")
        or entry.get("package_checksum")
        or identity.get("model_package_checksum")
        or model.get("model_package_checksum")
        or resource_profile.get("package_checksum")
    )
    _require_db_free_state_save_lineage(
        {
            "source_id": source_id,
            "cycle_time": cycle_time,
            "model_package_uri": model_package_version,
            "model_package_checksum": model_package_checksum,
        },
        source="manifest entry",
    )
    return StateRunContext(
        run_id=run_id,
        model_id=model_id,
        end_time=_parse_time_flexible(end_time_value),
        output_uri=_optional_str(entry.get("output_uri") or outputs.get("output_uri")),
        source_id=source_id,
        cycle_time=cycle_time,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
    )


def _state_run_context_from_env(run_id: str) -> StateRunContext:
    model_id = os.getenv("NHMS_MODEL_ID", "").strip()
    end_time = os.getenv("NHMS_END_TIME", "").strip()
    if not model_id or not end_time:
        raise StateManagerError("DB-free state save requires NHMS_MODEL_ID and NHMS_END_TIME.")
    source_id = _optional_str(os.getenv("NHMS_SOURCE_ID"))
    cycle_time = os.getenv("NHMS_CYCLE_TIME", "").strip()
    parsed_cycle_time = _parse_time_flexible(cycle_time) if cycle_time else None
    model_package_version = _optional_str(os.getenv("NHMS_MODEL_PACKAGE_URI"))
    model_package_checksum = _optional_str(os.getenv("NHMS_MODEL_PACKAGE_CHECKSUM"))
    _require_db_free_state_save_lineage(
        {
            "source_id": source_id,
            "cycle_time": parsed_cycle_time,
            "model_package_uri": model_package_version,
            "model_package_checksum": model_package_checksum,
        },
        source="NHMS_* runtime env",
    )
    return StateRunContext(
        run_id=run_id,
        model_id=model_id,
        end_time=_parse_time_flexible(end_time),
        output_uri=None,
        source_id=source_id,
        cycle_time=parsed_cycle_time,
        model_package_version=model_package_version,
        model_package_checksum=model_package_checksum,
    )


def _require_db_free_state_save_lineage(fields: Mapping[str, Any], *, source: str) -> None:
    required = {
        "source_id": "NHMS_SOURCE_ID",
        "cycle_time": "NHMS_CYCLE_TIME",
        "model_package_uri": "NHMS_MODEL_PACKAGE_URI",
        "model_package_checksum": "NHMS_MODEL_PACKAGE_CHECKSUM",
    }
    missing = [env for key, env in required.items() if fields.get(key) in (None, "")]
    if missing:
        missing_text = ", ".join(missing)
        raise StateManagerError(f"DB-free state save {source} is missing required lineage fields: {missing_text}.")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class _VerifiedStateSource:
    """An output root whose solver-success witness passed G1-G5."""

    output_root: Path
    manifest_path: Path
    # Set only on the fallback lane (zero declared and zero requested
    # checkpoints); ``None`` means "publish the manifest's checkpoint entries".
    # Its ``relative_path`` is the NORMALIZED string G5 hashed, never the raw
    # manifest value: publish must open the byte-identical file the checksum was
    # taken over (a whitespace twin next to it is a different file).
    final_ic: dict[str, Any] | None


class _StateSourceRejection(Exception):
    """A per-root admission failure; later roots may still verify (design D3)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        # Kept as a named attribute so cross-root policy (the #1329 downgrade
        # guard) can key on the typed reason instead of re-parsing the message.
        self.reason = reason


def _admit_state_publish_source(
    run: StateRunContext,
    workspace_root: Path,
    object_store: LocalObjectStore,
) -> _VerifiedStateSource:
    """Verify the source tree before anything is selected for publish (#1325).

    Roots are evaluated in the existing probe order and the FIRST one that both
    passes G2-G5 AND yields a publishable artifact set wins (#1329 re-scope of
    "verified root"): a workspace tree left by a failed attempt legitimately
    coexists with the object-store tree of the successful solve, so a root that
    proves its identity but has nothing publishable yields to the next root
    instead of hard-rejecting. One cross-root exception preserves the
    no-downgrade invariant: once a root has fallen through with
    ``STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`` — the proof that this run's
    configuration requested checkpoint states — no LATER root may publish via
    the final-IC fallback lane; checkpoint-lane roots stay eligible. If no root
    publishes, the first existing root's reason is reported so the outcome is
    deterministic.
    """

    roots = _state_output_roots(run, workspace_root, object_store)
    if not roots:
        raise StateManagerError(
            f"{STATE_SAVE_SOURCE_OUTPUT_MISSING}: no output root exists for run {run.run_id}."
        )
    first_rejection: _StateSourceRejection | None = None
    uncaptured_rejection: _StateSourceRejection | None = None
    for output_root in roots:
        try:
            source = _verify_state_source_root(output_root, run)
        except _StateSourceRejection as rejection:
            if first_rejection is None:
                first_rejection = rejection
            if uncaptured_rejection is None and rejection.reason == STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED:
                uncaptured_rejection = rejection
            continue
        if source.final_ic is not None and uncaptured_rejection is not None:
            # Cross-root no-downgrade: an earlier root PROVED the run requested
            # checkpoint states, so a sibling's single end-time IC is not an
            # acceptable substitute. Answer with that earlier root's reason.
            raise StateManagerError(str(uncaptured_rejection))
        return source
    raise StateManagerError(str(first_rejection))


def _state_output_roots(
    run: StateRunContext,
    workspace_root: Path,
    object_store: LocalObjectStore,
) -> list[Path]:
    roots: list[Path] = []
    workspace_output = workspace_root / "runs" / run.run_id / "output"
    if workspace_output.is_dir():
        roots.append(workspace_output)
    if run.output_uri:
        output_path = _resolve_run_output_path(run, object_store)
        # Only DIRECTORY roots are publishable sources: a file-shaped
        # ``output_uri`` names no witness tree and is no longer searched.
        if output_path.is_dir() and not any(_same_directory(output_path, root) for root in roots):
            roots.append(output_path)
    return roots


def _same_directory(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _verify_state_source_root(output_root: Path, run: StateRunContext) -> _VerifiedStateSource:
    manifest_path = output_root / "state_checkpoints" / "state_checkpoints.json"
    if not manifest_path.exists():
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_MANIFEST_MISSING,
            f"no solver-success witness at {manifest_path}",
        )
    payload = _read_state_checkpoint_manifest_payload(manifest_path)
    provenance = _usable_provenance(payload)
    if provenance is None:
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_PROVENANCE_MISSING,
            f"manifest {manifest_path} carries no usable provenance block",
        )
    manifest_run_id = str(provenance["run_id"])
    if manifest_run_id != run.run_id:
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_PROVENANCE_MISMATCH,
            f"manifest {manifest_path} was written by run {manifest_run_id}, not {run.run_id}",
        )
    raw_checkpoints = payload.get("checkpoints")
    _verify_declared_checkpoints(output_root, manifest_path, raw_checkpoints)
    if raw_checkpoints:
        return _VerifiedStateSource(output_root=output_root, manifest_path=manifest_path, final_ic=None)
    requested_hours = list(provenance["requested_checkpoint_hours"])
    if requested_hours:
        # A tree that failed its own capture contract must not quietly downgrade
        # to publishing the final IC in place of the requested checkpoints. It
        # yields to a later CHECKPOINT-publishing root (#1329) but never to a
        # later fallback-lane one — the loop's cross-root downgrade guard.
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED,
            f"manifest {manifest_path} requested checkpoint hours {requested_hours} but captured none.",
        )
    final_ic = payload.get("final_ic")
    final_ic_path = str(final_ic.get("relative_path") or "").strip() if isinstance(final_ic, Mapping) else ""
    if not final_ic_path:
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_FINAL_IC_MISSING,
            f"manifest {manifest_path} names no final IC to publish.",
        )
    _verify_final_ic_artifact(output_root, manifest_path, final_ic, final_ic_path)
    return _VerifiedStateSource(
        output_root=output_root,
        manifest_path=manifest_path,
        final_ic={**final_ic, "relative_path": final_ic_path},
    )


def _usable_provenance(payload: Any) -> Mapping[str, Any] | None:
    """Return the provenance block only when it can carry the gate's decisions.

    ``requested_checkpoint_hours`` is load-bearing (it discriminates the
    zero-hour fallback lane from a total capture miss), so a provenance block
    without a usable one is a G3 violation rather than a defaulted empty list.
    """

    provenance = payload.get("provenance") if isinstance(payload, Mapping) else None
    if not isinstance(provenance, Mapping):
        return None
    if any(key not in provenance for key in _REQUIRED_PROVENANCE_KEYS):
        return None
    if not str(provenance.get("run_id") or "").strip():
        return None
    if not str(provenance.get("generated_at") or "").strip():
        return None
    requested = provenance.get("requested_checkpoint_hours")
    if not isinstance(requested, Sequence) or isinstance(requested, str | bytes):
        return None
    return provenance


def _verify_declared_checkpoints(output_root: Path, manifest_path: Path, raw_checkpoints: Any) -> None:
    """Judge integrity over the RAW ``checkpoints`` array.

    Every shape the loader silently drops is a violation here: a filtered view
    would let the publish shrink from N declared states to M<N without anyone
    downstream being able to tell.
    """

    if not isinstance(raw_checkpoints, Sequence) or isinstance(raw_checkpoints, str | bytes):
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE,
            f"manifest {manifest_path} field 'checkpoints' is not a list",
        )
    if len(raw_checkpoints) > MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES:
        raise StateManagerError(
            "State checkpoint manifest exceeds maximum entry count: "
            f"{len(raw_checkpoints)} > {MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES}"
        )
    unusable: list[str] = []
    drifted: list[str] = []
    for index, raw in enumerate(raw_checkpoints):
        if not isinstance(raw, Mapping):
            unusable.append(f"entry {index} (not an object)")
            continue
        relative_path = str(raw.get("relative_path") or "").strip()
        valid_time = raw.get("valid_time")
        if not relative_path or not valid_time:
            unusable.append(f"entry {index} (missing relative_path/valid_time)")
            continue
        if not _parseable_checkpoint_valid_time(valid_time):
            # Presence is not usability: an unparseable stamp is a malformed
            # declaration here, and letting it reach the loader turns a typed
            # reject into a bare ``ValueError`` out of `_parse_time`.
            unusable.append(f"entry {index} ({relative_path}, unparseable valid_time)")
            continue
        state = _declared_artifact_state(output_root, raw, relative_path)
        if state == "checksum_mismatch":
            drifted.append(f"entry {index} ({relative_path})")
        elif state != "present":
            unusable.append(f"entry {index} ({relative_path}, {state})")
    if unusable:
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE,
            f"manifest {manifest_path} declares unusable checkpoint artifacts: {', '.join(unusable)}",
        )
    if drifted:
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH,
            f"manifest {manifest_path} declares checkpoint artifacts whose content changed: {', '.join(drifted)}",
        )


def _parseable_checkpoint_valid_time(value: Any) -> bool:
    """Answer whether the loader could turn ``value`` into a UTC datetime.

    Deliberately runs the loader's OWN conversion rather than a lookalike: the
    gate's job is to reject exactly what would break downstream, so the two must
    not be able to disagree.
    """

    try:
        _ensure_utc(_parse_time(str(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return True


def _verify_final_ic_artifact(
    output_root: Path,
    manifest_path: Path,
    final_ic: Mapping[str, Any],
    relative_path: str,
) -> None:
    """Hash the final IC at ``relative_path`` — the same string publish will open."""

    state = _declared_artifact_state(output_root, final_ic, relative_path)
    if state == "checksum_mismatch":
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_ARTIFACT_CHECKSUM_MISMATCH,
            f"manifest {manifest_path} final_ic ({relative_path}) content no longer matches its declared checksum",
        )
    if state != "present":
        raise _StateSourceRejection(
            STATE_SAVE_SOURCE_MANIFEST_INCOMPLETE,
            f"manifest {manifest_path} declares an unusable final_ic ({relative_path}, {state})",
        )


def _declared_artifact_state(output_root: Path, entry: Mapping[str, Any], relative_path: str) -> str:
    """Return ``present`` / ``missing`` / ``checksum_absent`` / ``checksum_mismatch``.

    Path safety is judged BEFORE checksum presence and keeps its own hard
    errors: an unsafe declared path is a suspect manifest, not an incomplete
    one, and must never be folded into a fall-through reason.
    """

    relative_candidate = Path(relative_path)
    if relative_candidate.is_absolute() or ".." in relative_candidate.parts:
        raise StateManagerError(f"State checkpoint path escapes output directory: {relative_path}")
    path = output_root / relative_candidate
    try:
        entry_stat = stat_no_follow(path, containment_root=output_root)
    except FileNotFoundError:
        return "missing"
    except SafeFilesystemError as error:
        raise StateManagerError(f"State checkpoint path is unsafe: {relative_path}") from error
    if not stat.S_ISREG(entry_stat.st_mode):
        return "missing"
    declared_checksum = str(entry.get("checksum") or "").strip()
    if not declared_checksum:
        return "checksum_absent"
    content = _read_limited_bytes_no_follow(
        path,
        max_bytes=MAX_STATE_IC_BYTES,
        label="state checkpoint IC file",
    )
    return "present" if sha256_bytes(content) == declared_checksum else "checksum_mismatch"


def _read_state_checkpoint_manifest_payload(manifest_path: Path) -> Any:
    try:
        return json.loads(
            _read_limited_text_no_follow(
                manifest_path,
                max_bytes=MAX_STATE_CHECKPOINT_MANIFEST_BYTES,
                label="state checkpoint manifest",
            )
        )
    except (OSError, json.JSONDecodeError, StateManagerError) as error:
        raise StateManagerError(f"Invalid state checkpoint manifest {manifest_path}: {error}") from error


def _load_state_checkpoint_manifest(manifest_path: Path) -> list[StateCheckpoint]:
    payload = _read_state_checkpoint_manifest_payload(manifest_path)
    raw_checkpoints = payload.get("checkpoints") if isinstance(payload, dict) else None
    if not isinstance(raw_checkpoints, Sequence) or isinstance(raw_checkpoints, str | bytes):
        return []
    if len(raw_checkpoints) > MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES:
        raise StateManagerError(
            "State checkpoint manifest exceeds maximum entry count: "
            f"{len(raw_checkpoints)} > {MAX_STATE_CHECKPOINT_MANIFEST_ENTRIES}"
        )
    output_root = manifest_path.parent.parent
    checkpoints: list[StateCheckpoint] = []
    for raw in raw_checkpoints:
        if not isinstance(raw, dict):
            continue
        relative_path = str(raw.get("relative_path") or "").strip()
        valid_time = raw.get("valid_time")
        if not relative_path or not valid_time:
            continue
        relative_candidate = Path(relative_path)
        if relative_candidate.is_absolute() or ".." in relative_candidate.parts:
            raise StateManagerError(f"State checkpoint path escapes output directory: {relative_path}")
        path = output_root / relative_candidate
        try:
            entry_stat = stat_no_follow(path, containment_root=output_root)
        except FileNotFoundError:
            continue
        except SafeFilesystemError as error:
            raise StateManagerError(f"State checkpoint path is unsafe: {relative_path}") from error
        if not stat.S_ISREG(entry_stat.st_mode):
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


def _read_limited_bytes_no_follow(path: Path, *, max_bytes: int, label: str) -> bytes:
    try:
        raw = read_bytes_limited_no_follow(path, max_bytes=max_bytes)
        if len(raw) > max_bytes:
            raise StateManagerError(f"{label} exceeds size limit of {max_bytes} bytes: {path}")
        return raw
    except StateManagerError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise StateManagerError(f"Unable to safely read {label} {path}: {error}") from error


def _read_limited_text_no_follow(path: Path, *, max_bytes: int, label: str) -> str:
    raw = _read_limited_bytes_no_follow(path, max_bytes=max_bytes, label=label)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StateManagerError(f"Unable to safely read {label} {path}: {error}") from error


def _resolve_run_output_path(run: StateRunContext, object_store: LocalObjectStore) -> Path:
    """Resolve ``hydro_run.output_uri`` to its local path under the object store.

    Both key shapes still resolve (the ``runs/<run_id>/output`` prefix itself and
    a deeper object key), but the sole caller — `_state_output_roots` — keeps
    only directories. So the shape of what a deeper key points at decides its
    fate: a file-shaped key resolves and is then dropped by that directory
    filter (#1325 retired the file-shaped output root from the publish path),
    while a directory-shaped key passes the filter and is still probed as a
    source root.
    """

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
            if not _state_save_result_qc_passed(result):
                click.echo("STATE_SNAPSHOT_QC_FAILED: one or more saved checkpoints failed QC.", err=True)
                raise SystemExit(1)
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
            if not _state_save_result_qc_passed(result):
                print(
                    "STATE_SNAPSHOT_QC_FAILED: one or more saved checkpoints failed QC.",
                    file=sys.stderr,
                )
                return 1
        except ManifestValidationError as error:
            print(f"{error.error_code}: {error.message}", file=sys.stderr)
            return 1
        except StateManagerError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    parser.error(f"Unsupported command: {args.command}")
    return 2


def _state_save_result_qc_passed(result: Mapping[str, Any]) -> bool:
    if result.get("qc_passed") is False:
        return False
    checkpoints = result.get("checkpoints")
    if not isinstance(checkpoints, Sequence) or isinstance(checkpoints, (str, bytes)):
        return True
    return all(
        not isinstance(checkpoint, Mapping) or checkpoint.get("qc_passed") is not False
        for checkpoint in checkpoints
    )


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
