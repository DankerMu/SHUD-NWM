from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

from packages.common.rollback_execution_binding import (
    ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION,
    RollbackExecutionBindingError,
    archive_completed_rollback_execution_binding,
    binding_id_for,
    read_rollback_execution_binding,
    rollback_execution_artifact_root,
    seal_rollback_python_source_tree,
    write_rollback_execution_binding,
)
from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
    _public_evidence,
    _validated_git_writer_generation,
)
from services.orchestrator.scheduler_state import _format_utc
from workers.data_adapters.base import parse_cycle_time

HISTORICAL_NODE22_DB_PORT = 55433
MIGRATION_RECEIPT_SCHEMA_VERSION = "nhms.scheduler.file_orchestration_migration.v1"
HISTORICAL_NODE22_DB_HOSTS = {
    "127.0.0.1",
    "::1",
    "localhost",
    "node22",
    "node-22",
    "10.0.2.100",
    "210.77.77.22",
}
MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION = 100_000
HISTORICAL_MIGRATION_ROW_LIMITS = {
    "forecast_cycles": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "hydro_runs": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "pipeline_jobs": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
    "pipeline_events": MAX_HISTORICAL_MIGRATION_ROWS_PER_RELATION,
}
EXPORT_FETCHMANY_BATCH_SIZE = 1_000
ROLLBACK_WRITER_GIT_TIMEOUT_SECONDS = 10
ROLLBACK_WRITER_GIT_OUTPUT_LIMIT_BYTES = 16_384
ROLLBACK_RUNTIME_COPY_CHUNK_BYTES = 1024 * 1024
ROLLBACK_EXECUTION_LOCK_NAME = ".reconcile-inventory-rollback-execution.lock"

_FAILED_STATUSES = {"failed", "submission_failed", "partially_failed", "permanently_failed", "cancelled"}


def prepare_file_journal_rollback(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
    scheduler_state: str,
    active_scheduler_processes: int,
    checked_at: datetime,
    checked_by: str,
    target_writer_generation: str,
) -> dict[str, Any]:
    """Produce the durable receipt required before launching an old writer."""

    target_writer_generation = _validated_git_writer_generation(
        target_writer_generation,
        field="target_writer_generation",
        invalid_reason="file_journal_rollback_target_writer_generation_invalid",
    )
    with _rollback_execution_lock(journal_root):
        config, lease_identity = _rollback_file_lease_config(
            journal_root=journal_root,
            workspace_root=workspace_root,
            lock_path=lock_path,
            scheduler_lock_backend=scheduler_lock_backend,
            lock_ttl_seconds=lock_ttl_seconds,
        )
        lease, heartbeat, pass_id = _acquire_rollback_file_lease(config, operation="prepare")
        repository = FileOrchestrationJournalRepository(journal_root)
        try:
            receipt = repository._prepare_reconcile_inventory_rollback_under_scheduler_lease(
                scheduler_lease_identity=lease_identity,
                scheduler_lease_guard=lambda: _rollback_lease_is_held(
                    lease,
                    heartbeat,
                    pass_id=pass_id,
                ),
                scheduler_state=scheduler_state,
                active_scheduler_processes=active_scheduler_processes,
                checked_at=checked_at,
                checked_by=checked_by,
                target_writer_generation=target_writer_generation,
            )
            _publish_prepared_rollback_execution_binding(
                config=config,
                receipt=receipt,
            )
            return receipt
        finally:
            try:
                heartbeat.stop()
            finally:
                lease.release(pass_id=pass_id)


def _publish_prepared_rollback_execution_binding(
    *,
    config: Any,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    prepared: dict[str, Any] = {
        "schema_version": ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION,
        "binding_id": "",
        "status": "prepared",
        "preparation_receipt_id": receipt["receipt_id"],
        "journal_root_identity": dict(receipt["journal_root_identity"]),
        "scheduler_lease_identity": dict(receipt["scheduler_lease_identity"]),
        "workspace_root": str(config.workspace_root),
        "lock_path": str(config.lock_path),
        "target_writer_generation": receipt["preflight"]["target_writer_generation"],
        "target_python_runtime": None,
        "target_python_source_root": None,
        "writer_repository_root": None,
        "created_at": receipt["prepared_at"],
        "updated_at": receipt["prepared_at"],
    }
    prepared["binding_id"] = binding_id_for(prepared)
    try:
        existing = read_rollback_execution_binding(
            config.workspace_root,
            require_artifacts=False,
        )
        if existing is not None and existing["status"] == "completed":
            archive_completed_rollback_execution_binding(config.workspace_root, existing)
            existing = None
        if existing is not None:
            if existing == prepared or (
                existing["status"] == "active"
                and _rollback_binding_matches_preparation(existing, prepared)
            ):
                return existing
            raise RollbackExecutionBindingError("rollback preparation binding conflicts")
        return write_rollback_execution_binding(config.workspace_root, prepared)
    except RollbackExecutionBindingError as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_preparation_authority_unavailable",
            field="rollback_execution_binding",
        ) from error


def _rollback_binding_matches_preparation(
    binding: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> bool:
    fields = (
        "preparation_receipt_id",
        "journal_root_identity",
        "scheduler_lease_identity",
        "workspace_root",
        "lock_path",
        "target_writer_generation",
    )
    return all(binding.get(field) == prepared.get(field) for field in fields)


def require_file_journal_rollback_prepared(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    receipt_id: str,
    actual_writer_generation: str,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Old-writer launch gate for the supported rollback path."""

    _config, lease_identity = _rollback_file_lease_config(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    return repository._require_reconcile_inventory_rollback_prepared(
        receipt_id=receipt_id,
        scheduler_lease_identity=lease_identity,
        actual_writer_generation=actual_writer_generation,
    )


def launch_file_journal_rollback_writer(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    receipt_id: str,
    writer_repository_root: str | Path,
    writer_args: Iterable[str],
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Validate the bound receipt and only then cross the old-writer exec boundary."""

    arguments = _validated_production_writer_arguments(writer_args)
    config, _lease_identity = _rollback_file_lease_config(
        journal_root=journal_root,
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_lock_backend=scheduler_lock_backend,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    with _rollback_execution_lock(journal_root) as execution_lock_fd:
        repository_root, actual_writer_generation = _resolve_clean_writer_generation(writer_repository_root)
        writer_runtime, resolved_runtime, runtime_identity = _resolve_target_writer_runtime(repository_root)
        receipt = require_file_journal_rollback_prepared(
            journal_root=journal_root,
            workspace_root=workspace_root,
            receipt_id=receipt_id,
            actual_writer_generation=actual_writer_generation,
            lock_path=lock_path,
            scheduler_lock_backend=scheduler_lock_backend,
            lock_ttl_seconds=lock_ttl_seconds,
        )
        if receipt["preflight"]["dry_run"] is not False:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_not_prepared",
                field="reconcile_inventory_migration",
            )
        try:
            existing_binding = read_rollback_execution_binding(
                config.workspace_root,
                require_artifacts=False,
            )
            if existing_binding is not None and (
                existing_binding["status"] == "active"
                or (
                    existing_binding["status"] == "rolling_forward"
                    and existing_binding.get("target_python_runtime") is not None
                )
            ):
                existing_binding = read_rollback_execution_binding(
                    config.workspace_root,
                    required=True,
                    require_artifacts=True,
                )
        except RollbackExecutionBindingError as error:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_execution_binding_invalid",
                field="rollback_execution_binding",
            ) from error
        with ExitStack() as retained:
            if existing_binding is None or existing_binding["status"] == "completed":
                raise FileOrchestrationJournalError(
                    "file_journal_rollback_execution_binding_conflict",
                    field="rollback_execution_binding",
                )
            if existing_binding is not None and existing_binding["status"] == "prepared":
                _require_matching_prepared_rollback_binding(
                    existing_binding,
                    receipt=receipt,
                    config=config,
                    actual_writer_generation=actual_writer_generation,
                )
            if existing_binding["status"] == "prepared":
                retention_root = _prepare_rollback_retention_root(
                    config.workspace_root,
                    receipt_id=receipt["receipt_id"],
                    generation=actual_writer_generation,
                )
                snapshot_root = retained.enter_context(
                    _materialize_commit_snapshot(
                        repository_root,
                        actual_writer_generation,
                        snapshot_root=retention_root / "source",
                    )
                )
                bound_runtime = retained.enter_context(
                    _materialize_bound_runtime(
                        writer_runtime,
                        resolved_runtime=resolved_runtime,
                        runtime_identity=runtime_identity,
                        bundle_root=retention_root / "runtime",
                    )
                )
                rechecked_root, rechecked_generation = _resolve_clean_writer_generation(repository_root)
                rechecked_runtime, rechecked_resolved_runtime, rechecked_runtime_identity = (
                    _resolve_target_writer_runtime(repository_root)
                )
                if (
                    rechecked_root != repository_root
                    or rechecked_generation != actual_writer_generation
                    or rechecked_runtime != writer_runtime
                    or rechecked_resolved_runtime != resolved_runtime
                    or rechecked_runtime_identity != runtime_identity
                ):
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_writer_generation_changed",
                        field="writer_repository_root",
                    )
                os.chmod(retention_root, 0o500)
                created_at = _format_utc(datetime.now(UTC))
                binding: dict[str, Any] = {
                    "schema_version": ROLLBACK_EXECUTION_BINDING_SCHEMA_VERSION,
                    "binding_id": "",
                    "status": "active",
                    "preparation_receipt_id": receipt["receipt_id"],
                    "journal_root_identity": dict(receipt["journal_root_identity"]),
                    "scheduler_lease_identity": dict(receipt["scheduler_lease_identity"]),
                    "workspace_root": str(config.workspace_root),
                    "lock_path": str(config.lock_path),
                    "target_writer_generation": actual_writer_generation,
                    "target_python_runtime": str(bound_runtime.path),
                    "target_python_source_root": str(snapshot_root),
                    "writer_repository_root": str(repository_root),
                    "created_at": created_at,
                    "updated_at": created_at,
                }
                binding["binding_id"] = binding_id_for(binding)
                try:
                    binding = write_rollback_execution_binding(config.workspace_root, binding)
                except RollbackExecutionBindingError as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollback_execution_binding_unavailable",
                        field="rollback_execution_binding",
                    ) from error
            else:
                binding = _require_matching_active_rollback_binding(
                    existing_binding,
                    receipt=receipt,
                    config=config,
                    repository_root=repository_root,
                    actual_writer_generation=actual_writer_generation,
                )
                snapshot_root = Path(binding["target_python_source_root"])
            controlled_arguments = (
                *arguments,
                "--workspace-root",
                str(config.workspace_root),
                "--lock-path",
                str(config.lock_path),
            )
            command = (
                binding["target_python_runtime"],
                "-m",
                "services.orchestrator.cli",
                *controlled_arguments,
            )
            completed = _run_rollback_writer(
                command,
                cwd=snapshot_root,
                check=False,
                env=_rollback_writer_environment(
                    config=config,
                    journal_root=journal_root,
                    target_python_runtime=Path(binding["target_python_runtime"]),
                    target_python_source_root=Path(binding["target_python_source_root"]),
                ),
                pass_fds=(execution_lock_fd,),
            )
    return {
        "preparation_receipt_id": receipt["receipt_id"],
        "actual_writer_generation": actual_writer_generation,
        "writer_repository_root": str(repository_root),
        "rollback_execution_binding_id": binding["binding_id"],
        "rollback_execution_binding_status": binding["status"],
        "target_python_runtime": binding["target_python_runtime"],
        "target_python_source_root": binding["target_python_source_root"],
        "target_python_runtime_retention": "retained_fail_closed_until_operator_cleanup",
        "dry_run": False,
        "writer_exit_code": int(completed.returncode),
    }


def _validated_production_writer_arguments(writer_args: Iterable[str]) -> tuple[str, ...]:
    if isinstance(writer_args, str | bytes):
        arguments: tuple[Any, ...] = ()
    else:
        arguments = tuple(writer_args)
    if arguments[:1] == ("--",):
        arguments = arguments[1:]
    invalid_mode = any(
        argument in {"--plan", "--dry-run"}
        or argument.startswith("--plan=")
        or argument.startswith("--dry-run=")
        or argument in {"-h", "--help", "--version"}
        or (argument.startswith("--") and "--help".startswith(argument))
        or (argument.startswith("--") and "--version".startswith(argument))
        or argument in {"--workspace-root", "--lock-path"}
        or argument.startswith("--workspace-root=")
        or argument.startswith("--lock-path=")
        for argument in arguments
        if isinstance(argument, str)
    )
    if (
        not arguments
        or arguments[0] != "plan-production"
        or arguments.count("--submit") != 1
        or invalid_mode
        or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in arguments)
    ):
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_command_invalid",
            field="writer_args",
        )
    return arguments


def _require_matching_active_rollback_binding(
    binding: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    config: Any,
    repository_root: Path,
    actual_writer_generation: str,
) -> dict[str, Any]:
    expected = {
        "preparation_receipt_id": receipt["receipt_id"],
        "journal_root_identity": receipt["journal_root_identity"],
        "scheduler_lease_identity": receipt["scheduler_lease_identity"],
        "workspace_root": str(config.workspace_root),
        "lock_path": str(config.lock_path),
        "target_writer_generation": actual_writer_generation,
        "writer_repository_root": str(repository_root),
    }
    if binding.get("status") != "active" or any(binding.get(key) != value for key, value in expected.items()):
        raise FileOrchestrationJournalError(
            "file_journal_rollback_execution_binding_conflict",
            field="rollback_execution_binding",
        )
    return dict(binding)


def _require_matching_prepared_rollback_binding(
    binding: Mapping[str, Any],
    *,
    receipt: Mapping[str, Any],
    config: Any,
    actual_writer_generation: str,
) -> None:
    expected = {
        "preparation_receipt_id": receipt["receipt_id"],
        "journal_root_identity": receipt["journal_root_identity"],
        "scheduler_lease_identity": receipt["scheduler_lease_identity"],
        "workspace_root": str(config.workspace_root),
        "lock_path": str(config.lock_path),
        "target_writer_generation": actual_writer_generation,
        "target_python_runtime": None,
        "target_python_source_root": None,
        "writer_repository_root": None,
    }
    if binding.get("status") != "prepared" or any(
        binding.get(key) != value for key, value in expected.items()
    ):
        raise FileOrchestrationJournalError(
            "file_journal_rollback_execution_binding_conflict",
            field="rollback_execution_binding",
        )


def _prepare_rollback_retention_root(
    workspace_root: Path,
    *,
    receipt_id: str,
    generation: str,
) -> Path:
    try:
        workspace = Path(workspace_root).resolve(strict=True)
        retention_root = rollback_execution_artifact_root(
            workspace,
            receipt_id,
            generation,
        )
        container = retention_root.parent
        ensure_directory_no_follow(container, containment_root=workspace)
        os.chmod(container, 0o700)
        container_metadata = os.stat(container, follow_symlinks=False)
        if (
            not stat.S_ISDIR(container_metadata.st_mode)
            or container_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(container_metadata.st_mode) & 0o077
        ):
            raise OSError("rollback retention container is unsafe")
        try:
            os.mkdir(retention_root, mode=0o700)
        except FileExistsError:
            metadata = os.stat(retention_root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o222
            ):
                raise OSError("rollback retention generation is incomplete or unsafe")
        return retention_root
    except (OSError, SafeFilesystemError, RollbackExecutionBindingError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_execution_retention_unavailable",
            field="rollback_execution_binding",
        ) from error


def _resolve_target_writer_runtime(
    repository_root: Path,
) -> tuple[Path, Path, tuple[int, int]]:
    runtime = repository_root / ".venv" / "bin" / "python"
    try:
        resolved_runtime = runtime.resolve(strict=True)
        runtime_stat = runtime.stat()
    except (OSError, RuntimeError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_runtime_unavailable",
            field="writer_repository_root",
        ) from error
    if not resolved_runtime.is_file() or not os.access(runtime, os.X_OK):
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_runtime_unavailable",
            field="writer_repository_root",
        )
    return runtime.absolute(), resolved_runtime, (runtime_stat.st_dev, runtime_stat.st_ino)


@dataclass
class _BoundRuntime:
    path: Path


@contextmanager
def _materialize_bound_runtime(
    writer_runtime: Path,
    *,
    resolved_runtime: Path,
    runtime_identity: tuple[int, int],
    bundle_root: Path,
) -> Iterator[_BoundRuntime]:
    venv_root = writer_runtime.parent.parent
    bundle_bin = bundle_root / "bin"
    bound_path = bundle_bin / "python"
    if bundle_root.exists():
        try:
            metadata = bound_path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o222
                or stat.S_IMODE(metadata.st_mode) & 0o111 == 0
            ):
                raise OSError("retained runtime is invalid")
        except OSError as error:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_writer_runtime_unavailable",
                field="writer_repository_root",
            ) from error
        yield _BoundRuntime(path=bound_path)
        return
    source_fd: int | None = None
    try:
        source_fd = os.open(
            resolved_runtime,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or (source_stat.st_dev, source_stat.st_ino) != runtime_identity:
            raise OSError("target runtime changed before binding")
        bundle_root.mkdir(mode=0o700)
        bundle_bin.mkdir(mode=0o700)
        source_config = venv_root / "pyvenv.cfg"
        source_config_stat = source_config.stat(follow_symlinks=False)
        if not stat.S_ISREG(source_config_stat.st_mode):
            raise OSError("target venv is missing pyvenv.cfg")
        config_lines = source_config.read_text(encoding="utf-8").splitlines()
        retained_config = [
            line for line in config_lines
            if not line.lower().startswith(("home =", "executable =", "command ="))
        ]
        retained_config.insert(0, f"home = {bundle_bin}")
        (bundle_root / "pyvenv.cfg").write_text("\n".join(retained_config) + "\n", encoding="utf-8")
        _materialize_bound_runtime_libraries(
            bundle_root=bundle_root,
            venv_root=venv_root,
            resolved_runtime=resolved_runtime,
        )
        _copy_open_runtime_snapshot(
            source_fd=source_fd,
            bound_path=bound_path,
            runtime_identity=runtime_identity,
        )
        _seal_bound_runtime_tree(bundle_root)
        os.chmod(bundle_root / "pyvenv.cfg", 0o400)
        os.chmod(bundle_bin, 0o500)
        os.chmod(bundle_root, 0o500)
    except OSError as error:
        _cleanup_bound_runtime_bundle(bundle_root)
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_runtime_unavailable",
            field="writer_repository_root",
        ) from error
    finally:
        if source_fd is not None:
            os.close(source_fd)
    bound_runtime = _BoundRuntime(path=bound_path)
    yield bound_runtime


def _copy_open_runtime_snapshot(
    *,
    source_fd: int,
    bound_path: Path,
    runtime_identity: tuple[int, int],
) -> None:
    bound_fd: int | None = None
    try:
        bound_fd = os.open(
            bound_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o500,
        )
        while chunk := os.read(source_fd, ROLLBACK_RUNTIME_COPY_CHUNK_BYTES):
            pending = memoryview(chunk)
            while pending:
                written = os.write(bound_fd, pending)
                if written <= 0:
                    raise OSError("target runtime snapshot write made no progress")
                pending = pending[written:]
        os.fchmod(bound_fd, 0o500)
        os.fsync(bound_fd)

        source_stat = os.fstat(source_fd)
        bound_stat = os.fstat(bound_fd)
        bound_path_stat = bound_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or (source_stat.st_dev, source_stat.st_ino) != runtime_identity
            or not stat.S_ISREG(bound_stat.st_mode)
            or not stat.S_ISREG(bound_path_stat.st_mode)
            or (bound_stat.st_dev, bound_stat.st_ino)
            != (bound_path_stat.st_dev, bound_path_stat.st_ino)
            or stat.S_IMODE(bound_stat.st_mode) & 0o111 == 0
        ):
            raise OSError("target runtime snapshot verification failed")
    finally:
        if bound_fd is not None:
            os.close(bound_fd)


def _materialize_bound_runtime_libraries(
    *,
    bundle_root: Path,
    venv_root: Path,
    resolved_runtime: Path,
) -> None:
    bundle_lib = bundle_root / "lib"
    bundle_lib.mkdir(mode=0o700)
    source_roots = (resolved_runtime.parent.parent / "lib", venv_root / "lib")
    for source_root in source_roots:
        if not source_root.is_dir():
            continue
        for source_entry in source_root.iterdir():
            if source_entry.is_file() and source_entry.name.startswith("libpython"):
                shutil.copy2(source_entry, bundle_lib / source_entry.name, follow_symlinks=True)
                continue
            if not re.fullmatch(r"python\d+(?:\.\d+)?", source_entry.name):
                continue
            destination = bundle_lib / source_entry.name
            shutil.copytree(
                source_entry,
                destination,
                dirs_exist_ok=True,
                symlinks=False,
                copy_function=shutil.copy2,
            )
    os.chmod(bundle_lib, 0o500)


def _cleanup_bound_runtime_bundle(bundle_root: Path) -> None:
    if not bundle_root.exists():
        return
    os.chmod(bundle_root, 0o700)
    bundle_bin = bundle_root / "bin"
    if bundle_bin.exists():
        os.chmod(bundle_bin, 0o700)
    bundle_lib = bundle_root / "lib"
    if bundle_lib.exists():
        os.chmod(bundle_lib, 0o700)
    shutil.rmtree(bundle_root)


def _seal_bound_runtime_tree(bundle_root: Path) -> None:
    for directory, directory_names, file_names in os.walk(bundle_root, followlinks=False):
        current = Path(directory)
        for name in (*directory_names, *file_names):
            entry = current / name
            if entry.is_symlink():
                raise OSError("bound runtime cannot retain symlinks")
        for file_name in file_names:
            entry = current / file_name
            mode = 0o500 if entry == bundle_root / "bin" / "python" else 0o400
            os.chmod(entry, mode)
        for directory_name in directory_names:
            os.chmod(current / directory_name, 0o500)


def _rollback_writer_environment(
    *,
    config: Any,
    journal_root: str | Path,
    target_python_runtime: Path,
    target_python_source_root: Path,
) -> dict[str, str]:
    environment = {str(key): str(value) for key, value in os.environ.items()}
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(key, None)
    runtime_bin = str(target_python_runtime.parent)
    inherited_path = environment.get("PATH", "")
    environment.update(
        {
            "WORKSPACE_ROOT": str(config.workspace_root),
            "NHMS_SCHEDULER_JOURNAL_ROOT": str(Path(journal_root).expanduser().resolve()),
            "NHMS_SCHEDULER_LOCK_BACKEND": "file",
            "NHMS_SCHEDULER_LOCK_ROOT": str(Path(config.lock_path).parent),
            "NHMS_SCHEDULER_DB_FREE_REQUIRED": "true",
            "NHMS_TARGET_PYTHON_RUNTIME": str(target_python_runtime),
            "NHMS_TARGET_PYTHON_SOURCE_ROOT": str(target_python_source_root),
            "NHMS_PYTHON_VENV_BIN": runtime_bin,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": f"{runtime_bin}{os.pathsep}{inherited_path}" if inherited_path else runtime_bin,
        }
    )
    return environment


@contextmanager
def _materialize_commit_snapshot(
    repository_root: Path,
    generation: str,
    *,
    snapshot_root: Path,
) -> Iterator[Path]:
    if snapshot_root.exists():
        try:
            existing_root, existing_generation = _resolve_clean_writer_generation(snapshot_root)
        except FileOrchestrationJournalError as error:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_writer_snapshot_unavailable",
                field="writer_repository_root",
            ) from error
        if existing_root != snapshot_root.resolve() or existing_generation != generation:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_writer_snapshot_unavailable",
                field="writer_repository_root",
            )
        yield snapshot_root
        return
    try:
        temporary_root = Path(
            tempfile.mkdtemp(prefix=".nhms-rollback-source-tmp-", dir=snapshot_root.parent)
        )
    except OSError as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_snapshot_unavailable",
            field="writer_repository_root",
        ) from error
    temporary_snapshot = temporary_root / "repository"
    try:
        _run_bounded_git(
            temporary_root,
            "clone",
            "--no-local",
            "--no-checkout",
            "--quiet",
            str(repository_root),
            str(temporary_snapshot),
        )
        _run_bounded_git(temporary_snapshot, "checkout", "--detach", "--quiet", generation)
        snapshot_generation = _run_bounded_git(
            temporary_snapshot,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ).lower()
        snapshot_dirty = _run_bounded_git(
            temporary_snapshot,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if snapshot_generation != generation or snapshot_dirty:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_writer_snapshot_unavailable",
                field="writer_repository_root",
            )
        os.replace(temporary_snapshot, snapshot_root)
        seal_rollback_python_source_tree(snapshot_root)
    except (FileOrchestrationJournalError, OSError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_snapshot_unavailable",
            field="writer_repository_root",
        ) from error
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    yield snapshot_root


def _resolve_clean_writer_generation(
    writer_repository_root: str | Path,
) -> tuple[Path, str]:
    try:
        repository_root = Path(writer_repository_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        ) from error
    if not repository_root.is_dir():
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        )

    top_level = _run_bounded_git(repository_root, "rev-parse", "--show-toplevel")
    try:
        resolved_top_level = Path(top_level).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        ) from error
    if resolved_top_level != repository_root:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        )

    generation = _run_bounded_git(
        repository_root,
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    )
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", generation) is None:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        )
    dirty = _run_bounded_git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if dirty:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_dirty",
            field="writer_repository_root",
        )
    return repository_root, generation.lower()


def _run_bounded_git(repository_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository_root,
            check=False,
            capture_output=True,
            shell=False,
            text=False,
            timeout=ROLLBACK_WRITER_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        ) from error
    stdout = completed.stdout
    stderr = completed.stderr
    if (
        completed.returncode != 0
        or not isinstance(stdout, bytes)
        or not isinstance(stderr, bytes)
        or len(stdout) > ROLLBACK_WRITER_GIT_OUTPUT_LIMIT_BYTES
        or len(stderr) > ROLLBACK_WRITER_GIT_OUTPUT_LIMIT_BYTES
    ):
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        )
    try:
        value = stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        ) from error
    if "\n" in value or "\r" in value:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_writer_generation_unresolvable",
            field="writer_repository_root",
        )
    return value


def _run_rollback_writer(
    command: tuple[str, ...],
    *,
    cwd: Path,
    check: bool,
    env: Mapping[str, str],
    pass_fds: tuple[int, ...],
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        shell=False,
        env=dict(env),
        pass_fds=pass_fds,
    )


@contextmanager
def _rollback_execution_lock(journal_root: str | Path) -> Iterator[int]:
    import fcntl

    root = Path(journal_root).expanduser().resolve()
    lock_path = root / ROLLBACK_EXECUTION_LOCK_NAME
    lock_fd: int | None = None
    try:
        ensure_directory_no_follow(root)
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        opened = os.fstat(lock_fd)
        current = os.stat(lock_path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("rollback execution lock changed")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FileOrchestrationJournalError(
                "file_journal_rollback_execution_active",
                field="reconcile_inventory_rollback_execution",
            ) from error
        current = os.stat(lock_path, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("rollback execution lock changed")
        yield lock_fd
    except FileOrchestrationJournalError:
        raise
    except (OSError, SafeFilesystemError) as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollback_execution_lock_unavailable",
            field="reconcile_inventory_rollback_execution",
        ) from error
    finally:
        if lock_fd is not None:
            # Do not explicitly LOCK_UN: the child inherits this open file
            # description, so a launcher crash keeps rollforward excluded until
            # the old writer itself exits.
            os.close(lock_fd)


def complete_file_journal_rollforward(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    preparation_receipt_id: str,
    lock_path: str | Path | None = None,
    scheduler_lock_backend: str = "file",
    lock_ttl_seconds: int = 60,
) -> dict[str, Any]:
    """Rebuild inventory and consume the rollback fence under the scheduler lease."""

    with _rollback_execution_lock(journal_root):
        config, lease_identity = _rollback_file_lease_config(
            journal_root=journal_root,
            workspace_root=workspace_root,
            lock_path=lock_path,
            scheduler_lock_backend=scheduler_lock_backend,
            lock_ttl_seconds=lock_ttl_seconds,
        )
        lease, heartbeat, pass_id = _acquire_rollback_file_lease(config, operation="rollforward")
        repository = FileOrchestrationJournalRepository(journal_root)
        try:
            try:
                binding = read_rollback_execution_binding(
                    config.workspace_root,
                    required=True,
                    require_artifacts=False,
                )
                if binding is not None and (
                    binding["status"] == "active"
                    or (
                        binding["status"] == "rolling_forward"
                        and binding.get("target_python_runtime") is not None
                    )
                ):
                    binding = read_rollback_execution_binding(
                        config.workspace_root,
                        required=True,
                        require_artifacts=True,
                    )
            except RollbackExecutionBindingError as error:
                raise FileOrchestrationJournalError(
                    "file_journal_rollforward_execution_binding_invalid",
                    field="rollback_execution_binding",
                ) from error
            assert binding is not None
            _require_matching_rollforward_binding(
                binding,
                preparation_receipt_id=preparation_receipt_id,
                lease_identity=lease_identity,
                config=config,
                journal_root=Path(journal_root).expanduser().resolve(),
            )
            if binding["status"] in {"prepared", "active"}:
                try:
                    unsettled = repository.query_rollback_unsettled_jobs()
                except FileOrchestrationJournalError as error:
                    if error.reason in {
                        "file_journal_rollback_receipt_invalid",
                        "file_journal_rollback_receipt_wrong_root",
                    }:
                        raise
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_quiescence_unavailable",
                        field="rollback_jobs",
                    ) from error
                except Exception as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_quiescence_unavailable",
                        field="rollback_jobs",
                    ) from error
                if unsettled:
                    raise FileOrchestrationJournalError(
                        "file_journal_rollforward_jobs_unsettled",
                        field="rollback_jobs",
                    )
                binding = _transition_rollback_execution_binding(
                    config.workspace_root,
                    binding,
                    status="rolling_forward",
                )
            completed = repository._complete_reconcile_inventory_rollforward_under_scheduler_lease(
                preparation_receipt_id=preparation_receipt_id,
                scheduler_lease_identity=lease_identity,
                scheduler_lease_guard=lambda: _rollback_lease_is_held(
                    lease,
                    heartbeat,
                    pass_id=pass_id,
                ),
            )
            completed_binding = _transition_rollback_execution_binding(
                config.workspace_root,
                binding,
                status="completed",
            )
            try:
                archive_completed_rollback_execution_binding(
                    config.workspace_root,
                    completed_binding,
                )
            except RollbackExecutionBindingError as error:
                raise FileOrchestrationJournalError(
                    "file_journal_rollforward_execution_binding_archive_unavailable",
                    field="rollback_execution_binding",
                ) from error
            return {
                **completed,
                "rollback_execution_binding_id": completed_binding["binding_id"],
                "rollback_execution_binding_status": completed_binding["status"],
            }
        finally:
            try:
                heartbeat.stop()
            finally:
                lease.release(pass_id=pass_id)


def _require_matching_rollforward_binding(
    binding: Mapping[str, Any],
    *,
    preparation_receipt_id: str,
    lease_identity: Mapping[str, Any],
    config: Any,
    journal_root: Path,
) -> None:
    try:
        journal_metadata = journal_root.stat(follow_symlinks=False)
    except OSError as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollforward_execution_binding_invalid",
            field="rollback_execution_binding",
        ) from error
    journal_identity = {
        "path_digest": hashlib.sha256(str(journal_root).encode("utf-8")).hexdigest(),
        "device": int(journal_metadata.st_dev),
        "inode": int(journal_metadata.st_ino),
    }
    expected = {
        "preparation_receipt_id": preparation_receipt_id,
        "journal_root_identity": journal_identity,
        "scheduler_lease_identity": dict(lease_identity),
        "workspace_root": str(config.workspace_root),
        "lock_path": str(config.lock_path),
    }
    if binding.get("status") not in {"prepared", "active", "rolling_forward", "completed"} or any(
        binding.get(key) != value for key, value in expected.items()
    ):
        raise FileOrchestrationJournalError(
            "file_journal_rollforward_execution_binding_conflict",
            field="rollback_execution_binding",
        )


def _transition_rollback_execution_binding(
    workspace_root: str | Path,
    binding: Mapping[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    current_status = str(binding.get("status") or "")
    if current_status == status:
        return dict(binding)
    allowed = {
        "prepared": "rolling_forward",
        "active": "rolling_forward",
        "rolling_forward": "completed",
    }
    if allowed.get(current_status) != status:
        raise FileOrchestrationJournalError(
            "file_journal_rollforward_execution_binding_conflict",
            field="rollback_execution_binding",
        )
    transitioned = {
        **dict(binding),
        "status": status,
        "updated_at": _format_utc(datetime.now(UTC)),
    }
    try:
        return write_rollback_execution_binding(workspace_root, transitioned)
    except RollbackExecutionBindingError as error:
        raise FileOrchestrationJournalError(
            "file_journal_rollforward_execution_binding_unavailable",
            field="rollback_execution_binding",
        ) from error


def _rollback_file_lease_config(
    *,
    journal_root: str | Path,
    workspace_root: str | Path,
    lock_path: str | Path | None,
    scheduler_lock_backend: str,
    lock_ttl_seconds: int,
) -> tuple[Any, dict[str, str]]:
    from services.orchestrator.scheduler import ProductionSchedulerConfig

    config = ProductionSchedulerConfig(
        workspace_root=workspace_root,
        lock_path=lock_path,
        scheduler_db_free_required=True,
        scheduler_lock_backend=scheduler_lock_backend,
        scheduler_journal_root=journal_root,
        lock_ttl_seconds=lock_ttl_seconds,
    )
    if config.scheduler_lock_backend != "file":
        raise FileOrchestrationJournalError(
            "file_journal_rollback_requires_file_scheduler_lease",
            field="scheduler_lock_backend",
        )
    workspace = Path(config.workspace_root)
    scheduler_lock = Path(config.lock_path)
    identity = {
        "backend": "file",
        "lock_path_digest": hashlib.sha256(str(scheduler_lock).encode("utf-8")).hexdigest(),
        "workspace_root_digest": hashlib.sha256(str(workspace).encode("utf-8")).hexdigest(),
    }
    return config, identity


def _acquire_rollback_file_lease(config: Any, *, operation: str) -> tuple[Any, Any, str]:
    from services.orchestrator.scheduler_lease import FileSchedulerLease, _LeaseHeartbeat

    pass_id = f"file-journal-{operation}-{uuid4().hex}"
    lease = FileSchedulerLease(
        Path(config.lock_path),
        ttl_seconds=config.lock_ttl_seconds,
        workspace_root=Path(config.workspace_root),
    )
    acquired = lease.acquire(pass_id=pass_id, started_at=datetime.now(UTC))
    if not acquired.get("acquired"):
        raise FileOrchestrationJournalError(
            "file_journal_scheduler_lease_contended",
            field="scheduler_lock",
        )
    heartbeat = _LeaseHeartbeat(
        lease,
        pass_id,
        max(1, config.lock_ttl_seconds // 3),
    )
    heartbeat.start()
    if not _rollback_lease_is_held(lease, heartbeat, pass_id=pass_id):
        heartbeat.stop()
        lease.release(pass_id=pass_id)
        raise FileOrchestrationJournalError(
            "file_journal_scheduler_lease_lost",
            field="scheduler_lock",
        )
    return lease, heartbeat, pass_id


def _rollback_lease_is_held(lease: Any, heartbeat: Any, *, pass_id: str) -> bool:
    return not heartbeat.lost and bool(lease.renew(pass_id=pass_id))


def import_historical_scheduler_state(
    *,
    journal_root: str | Path,
    forecast_cycles: Iterable[Mapping[str, Any]] = (),
    hydro_runs: Iterable[Mapping[str, Any]] = (),
    pipeline_jobs: Iterable[Mapping[str, Any]] = (),
    pipeline_events: Iterable[Mapping[str, Any]] = (),
    cutoff_time: datetime | None = None,
    source: str = "node22:55433",
) -> dict[str, Any]:
    cutoff = _ensure_utc(cutoff_time or datetime.now(UTC))
    cycles = _normalized_rows_limited("forecast_cycles", forecast_cycles)
    runs = _normalized_rows_limited("hydro_runs", hydro_runs)
    jobs = _normalized_rows_limited("pipeline_jobs", pipeline_jobs)
    events = _normalized_rows_limited("pipeline_events", pipeline_events)
    repository = FileOrchestrationJournalRepository(journal_root)
    imported_cycles: list[dict[str, Any]] = []
    imported_runs: list[dict[str, Any]] = []
    imported_jobs: list[dict[str, Any]] = []
    imported_events: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, str]] = []

    for row in cycles:
        skip_reason = _unsupported_forecast_cycle_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("forecast_cycles", row, skip_reason))
            continue
        repository.append_historical_forecast_cycle(row)
        imported_cycles.append(row)

    for row in runs:
        skip_reason = _unsupported_run_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("hydro_runs", row, skip_reason))
            continue
        repository.append_historical_hydro_run(row)
        imported_runs.append(row)

    for row in jobs:
        skip_reason = _unsupported_job_reason(row)
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("pipeline_jobs", row, skip_reason))
            continue
        repository.append_historical_pipeline_job(row)
        imported_jobs.append(row)

    imported_job_ids = {
        str(row.get("job_id"))
        for row in imported_jobs
        if row.get("job_id") not in (None, "")
    }
    imported_cycle_ids = {
        str(row.get("cycle_id"))
        for row in imported_cycles
        if row.get("cycle_id") not in (None, "")
    }
    for row in events:
        skip_reason = _unsupported_event_reason(
            row,
            imported_job_ids=imported_job_ids,
            imported_cycle_ids=imported_cycle_ids,
        )
        if skip_reason is not None:
            skipped_rows.append(_skipped_row("pipeline_events", row, skip_reason))
            continue
        written = repository.append_historical_pipeline_event(row)
        if written is None:
            skipped_rows.append(_skipped_row("pipeline_events", row, "unsupported_pipeline_event_target"))
            continue
        imported_events.append(row)

    replay_status = _migration_replay_status(repository, imported_jobs)
    receipt = {
        "schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION,
        "source": source,
        "cutoff_time": _format_utc(cutoff),
        "row_counts": {
            "forecast_cycles": len(cycles),
            "hydro_runs": len(runs),
            "pipeline_jobs": len(jobs),
            "pipeline_events": len(events),
        },
        "imported_row_counts": {
            "forecast_cycles": len(imported_cycles),
            "hydro_runs": len(imported_runs),
            "pipeline_jobs": len(imported_jobs),
            "pipeline_events": len(imported_events),
        },
        "skipped_rows": _skipped_rows_summary(skipped_rows),
        "checksums": {
            "forecast_cycles": _rows_checksum(cycles),
            "hydro_runs": _rows_checksum(runs),
            "pipeline_jobs": _rows_checksum(jobs),
            "pipeline_events": _rows_checksum(events),
        },
        "replay_status": replay_status,
        "stale_download_source_cycle_supersession": _download_source_cycle_supersession(imported_jobs, imported_events),
    }
    return receipt


def export_scheduler_state_from_postgres(
    *,
    database_url: str,
    journal_root: str | Path,
    allow_historical_node22: bool = False,
    cutoff_time: datetime | None = None,
) -> dict[str, Any]:
    _validate_historical_node22_database_url(database_url, allow_historical_node22=allow_historical_node22)
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError("psycopg is required to export historical scheduler state") from error

    cutoff = _ensure_utc(cutoff_time or datetime.now(UTC))
    with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10) as connection:
        forecast_cycles = _fetch_rows(
            connection,
            """
            SELECT cycle_id, source_id, cycle_time, issue_time, status, manifest_uri,
                   retry_count, error_code, error_message, created_at
            FROM met.forecast_cycle
            WHERE created_at <= %(cutoff)s OR cycle_time <= %(cutoff)s
            ORDER BY cycle_time ASC, cycle_id ASC
            """,
            {"cutoff": cutoff},
            relation="forecast_cycles",
        )
        hydro_runs = _fetch_rows(
            connection,
            """
            SELECT run_id, run_type, scenario_id, model_id, basin_version_id,
                   forcing_version_id, init_state_id, source_id, cycle_time,
                   start_time, end_time, status, run_manifest_uri, output_uri,
                   log_uri, slurm_job_id, error_code, error_message, created_at, updated_at
            FROM hydro.hydro_run
            WHERE created_at <= %(cutoff)s OR cycle_time <= %(cutoff)s
            ORDER BY cycle_time ASC, run_id ASC
            """,
            {"cutoff": cutoff},
            relation="hydro_runs",
        )
        pipeline_jobs = _fetch_rows(
            connection,
            """
            SELECT job_id, run_id, cycle_id, job_type, slurm_job_id, array_task_id,
                   model_id, status, stage, idempotency_key, candidate_id,
                   submitted_at, started_at, finished_at, exit_code, retry_count,
                   manual_retry_marker, error_code, error_message, log_uri,
                   created_at, updated_at
            FROM ops.pipeline_job
            WHERE created_at <= %(cutoff)s
               OR updated_at <= %(cutoff)s
               OR finished_at <= %(cutoff)s
            ORDER BY created_at ASC NULLS FIRST, job_id ASC
            """,
            {"cutoff": cutoff},
            relation="pipeline_jobs",
        )
        pipeline_events = _fetch_rows(
            connection,
            """
            SELECT event_id, entity_type, entity_id, event_type, status_from,
                   status_to, message, details, created_at
            FROM ops.pipeline_event
            WHERE created_at <= %(cutoff)s
            ORDER BY created_at ASC NULLS FIRST, event_id ASC
            """,
            {"cutoff": cutoff},
            relation="pipeline_events",
        )
    return import_historical_scheduler_state(
        journal_root=journal_root,
        forecast_cycles=forecast_cycles,
        hydro_runs=hydro_runs,
        pipeline_jobs=pipeline_jobs,
        pipeline_events=pipeline_events,
        cutoff_time=cutoff,
        source=_historical_source_label(database_url),
    )


def write_migration_receipt(
    receipt: Mapping[str, Any],
    receipt_path: str | Path,
    *,
    containment_root: str | Path | None = None,
) -> None:
    root = Path(containment_root) if containment_root is not None else None
    path = Path(receipt_path)
    if root is not None and not path.is_absolute():
        path = root / path
    content = (json.dumps(receipt, sort_keys=True, indent=2, default=_json_default) + "\n").encode("utf-8")
    try:
        if root is not None:
            ensure_directory_no_follow(root)
        atomic_write_bytes_no_follow(path, content, containment_root=root)
    except (OSError, SafeFilesystemError) as error:
        raise ValueError(f"failed to write migration receipt safely: {error}") from error


def _fetch_rows(connection: Any, sql: str, params: Mapping[str, Any], *, relation: str) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        fetchmany = getattr(cursor, "fetchmany", None)
        if callable(fetchmany):
            return _fetch_rows_limited(cursor, relation=relation)
        rows = [dict(row) for row in cursor.fetchall()]
        _validate_relation_row_count(relation, len(rows))
        return rows


def _fetch_rows_limited(cursor: Any, *, relation: str) -> list[dict[str, Any]]:
    limit = _relation_row_limit(relation)
    rows: list[dict[str, Any]] = []
    while True:
        remaining_probe_rows = limit + 1 - len(rows)
        if remaining_probe_rows <= 0:
            _raise_relation_row_limit(relation, limit)
        batch_size = min(EXPORT_FETCHMANY_BATCH_SIZE, remaining_probe_rows)
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return rows
        rows.extend(dict(row) for row in batch)
        if len(rows) > limit:
            _raise_relation_row_limit(relation, limit)


def _unsupported_forecast_cycle_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("cycle_id") in (None, ""):
        return None
    try:
        _source_cycle_from_cycle_id(str(row["cycle_id"]))
    except (FileOrchestrationJournalError, ValueError):
        return "unsupported_forecast_cycle_identity"
    return None


def _unsupported_run_reason(row: Mapping[str, Any]) -> str | None:
    run_id = row.get("run_id")
    if run_id in (None, ""):
        return None
    return None if _run_id_is_file_journal_supported(str(run_id)) else "unsupported_run_identity"


def _unsupported_job_reason(row: Mapping[str, Any]) -> str | None:
    run_id = row.get("run_id")
    if run_id in (None, ""):
        return None
    return None if _run_id_is_file_journal_supported(str(run_id)) else "unsupported_run_identity"


def _unsupported_event_reason(
    row: Mapping[str, Any],
    *,
    imported_job_ids: set[str],
    imported_cycle_ids: set[str],
) -> str | None:
    entity_type = str(row.get("entity_type") or "pipeline_job")
    entity_id = row.get("entity_id")
    if entity_type == "pipeline_job":
        if entity_id in (None, ""):
            return None
        return None if str(entity_id) in imported_job_ids else "unsupported_pipeline_event_target"
    if entity_type == "forecast_cycle":
        if entity_id in (None, ""):
            return None
        if str(entity_id) in imported_cycle_ids:
            return None
        try:
            _source_cycle_from_cycle_id(str(entity_id))
        except (FileOrchestrationJournalError, ValueError):
            return "unsupported_forecast_cycle_event_identity"
        return None
    return "unsupported_pipeline_event_entity_type"


def _run_id_is_file_journal_supported(run_id: str) -> bool:
    return run_id.startswith("fcst_") or run_id.startswith("cycle_")


def _source_cycle_from_cycle_id(cycle_id: str) -> tuple[str, datetime]:
    source, separator, cycle_stamp = cycle_id.rpartition("_")
    if not separator:
        raise ValueError(f"Cannot infer source/cycle from cycle_id: {cycle_id}")
    return source, parse_cycle_time(cycle_stamp)


def _skipped_row(relation: str, row: Mapping[str, Any], reason: str) -> dict[str, str]:
    return {
        "relation": relation,
        "reason": reason,
        "identity": _migration_receipt_text(_skipped_row_identity(row)),
    }


def _skipped_row_identity(row: Mapping[str, Any]) -> str:
    for field in ("event_id", "job_id", "run_id", "cycle_id", "entity_id"):
        value = row.get(field)
        if value not in (None, ""):
            return f"{field}:{str(value)[:160]}"
    return "unknown"


def _skipped_rows_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_relation: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for row in rows:
        by_relation[row["relation"]] = by_relation.get(row["relation"], 0) + 1
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {
        "count": len(rows),
        "by_relation": by_relation,
        "by_reason": by_reason,
        "samples": [_migration_receipt_sample(row) for row in rows[:8]],
    }


def _validate_historical_node22_database_url(database_url: str, *, allow_historical_node22: bool) -> None:
    if not allow_historical_node22:
        raise ValueError("historical node-22 export requires --allow-historical-node22")
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("historical scheduler-state export requires a PostgreSQL URL")
    if parsed.query or parsed.fragment:
        raise ValueError("historical scheduler-state export does not allow libpq URL query parameters")
    if parsed.port != HISTORICAL_NODE22_DB_PORT:
        raise ValueError(f"historical scheduler-state export must target port {HISTORICAL_NODE22_DB_PORT}")
    if (parsed.hostname or "") not in HISTORICAL_NODE22_DB_HOSTS:
        raise ValueError("historical scheduler-state export must target the node-22 historical PostgreSQL host")


def _historical_source_label(database_url: str) -> str:
    parsed = urlparse(database_url)
    return f"{parsed.hostname or 'unknown'}:{parsed.port or HISTORICAL_NODE22_DB_PORT}"


def _normalized_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _normalize_json_value(value) for key, value in row.items() if value is not None}


def _normalized_rows_limited(relation: str, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    limit = _relation_row_limit(relation)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if len(normalized) >= limit:
            _raise_relation_row_limit(relation, limit)
        normalized.append(_normalized_mapping(row))
    return normalized


def _relation_row_limit(relation: str) -> int:
    try:
        return int(HISTORICAL_MIGRATION_ROW_LIMITS[relation])
    except KeyError as error:
        raise ValueError(f"historical migration row limit is not configured for relation {relation!r}") from error


def _validate_relation_row_count(relation: str, row_count: int) -> None:
    limit = _relation_row_limit(relation)
    if row_count > limit:
        _raise_relation_row_limit(relation, limit)


def _raise_relation_row_limit(relation: str, limit: int) -> None:
    raise ValueError(
        f"historical migration relation {relation!r} exceeds row limit {limit}; "
        "split the migration or raise the per-relation cap intentionally"
    )


def _migration_receipt_sample(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = _public_evidence(value)
    return dict(sanitized) if isinstance(sanitized, Mapping) else {}


def _migration_receipt_text(value: str) -> str:
    sanitized = _public_evidence(value)
    return sanitized if isinstance(sanitized, str) else str(sanitized)


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(_ensure_utc(value))
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_json_value(item) for item in value]
    return value


def _source_cycle_from_row(row: Mapping[str, Any]) -> tuple[str, datetime]:
    source_id = row.get("source_id")
    cycle_time = row.get("cycle_time")
    if source_id not in (None, "") and cycle_time not in (None, ""):
        return str(source_id), _coerce_datetime(cycle_time)
    cycle_id = str(row["cycle_id"])
    source, separator, cycle_stamp = cycle_id.rpartition("_")
    if not separator:
        raise ValueError(f"Cannot infer source/cycle from cycle_id: {cycle_id}")
    return source, parse_cycle_time(cycle_stamp)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    return parse_cycle_time(str(value))


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _rows_checksum(rows: list[Mapping[str, Any]]) -> str:
    content = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _format_utc(_ensure_utc(value))
    return str(value)


def _migration_replay_status(
    repository: FileOrchestrationJournalRepository,
    jobs: list[Mapping[str, Any]],
) -> dict[str, Any]:
    seen: set[tuple[str, str, str, str]] = set()
    blocked: list[dict[str, str]] = []
    checked = 0
    for job in jobs:
        if job.get("model_id") in (None, "") or job.get("run_id") in (None, "") or job.get("cycle_id") in (None, ""):
            continue
        source_id, cycle_time = _source_cycle_from_row(job)
        key = (source_id, _format_utc(cycle_time), str(job["model_id"]), str(job["run_id"]))
        if key in seen:
            continue
        seen.add(key)
        checked += 1
        state = repository.candidate_state(
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=str(job["model_id"]),
            run_id=str(job["run_id"]),
            forcing_version_id=f"forc_{source_id}_{cycle_time:%Y%m%d%H}_{job['model_id']}",
            candidate_id=f"migration:{source_id}:{cycle_time:%Y%m%d%H}:{job['model_id']}",
        )
        if isinstance(state, Mapping) and isinstance(state.get("file_journal"), Mapping):
            blocked.append(
                {
                    "run_id": str(job["run_id"]),
                    "reason": str(state["file_journal"].get("reason") or "file_journal_blocked"),
                }
            )
    return {
        "status": "ok" if not blocked else "blocked",
        "checked_candidate_states": checked,
        "blocked_count": len(blocked),
        "blocked_samples": blocked[:8],
    }


def _download_source_cycle_supersession(
    jobs: list[Mapping[str, Any]],
    events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    job_by_id = {str(job.get("job_id")): job for job in jobs if job.get("job_id") not in (None, "")}
    superseded: list[dict[str, Any]] = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), Mapping) else {}
        previous_job_id = details.get("previous_job_id")
        if previous_job_id in (None, ""):
            continue
        previous = job_by_id.get(str(previous_job_id))
        if not previous or previous.get("job_type") != "download_source_cycle":
            continue
        if str(previous.get("status") or "") not in _FAILED_STATUSES:
            continue
        if details.get("manual_retry_marker") is True or details.get("trigger") == "manual":
            superseded.append(
                _migration_receipt_sample(
                    {
                        "failed_job_id": str(previous_job_id),
                        "superseding_event_id": str(event.get("event_id") or ""),
                        "superseding_entity_id": str(event.get("entity_id") or ""),
                        "cycle_id": str(previous.get("cycle_id") or ""),
                        "prior_failure_reason": str(
                            details.get("prior_failure_reason")
                            or details.get("previous_error")
                            or previous.get("error_code")
                            or ""
                        ),
                    }
                )
            )
    return {"count": len(superseded), "samples": superseded[:8]}
