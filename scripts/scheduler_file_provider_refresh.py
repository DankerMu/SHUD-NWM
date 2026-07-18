#!/usr/bin/env python
"""Refresh all expiring node-22 scheduler file providers without a database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import jsonschema

from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    provider_destination_lock,
)
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    open_directory_no_follow,
    read_bytes_limited_no_follow,
    verify_directory_no_follow,
)
from packages.common.state_manager import (
    MAX_STATE_SNAPSHOT_INDEX_BYTES,
    FileStateSnapshotIndexRepository,
    StateManagerError,
    publish_state_snapshot_index,
)
from scripts.publish_scheduler_file_registry import (
    SchedulerRegistryPublishError,
    publish_all_basin_scheduler_registry,
)
from services.orchestrator.scheduler_file_providers import (
    MAX_READINESS_INDEX_BYTES,
    MAX_REGISTRY_MANIFEST_BYTES,
    SchedulerFileProviderError,
    capture_scheduler_provider_preimage,
    derive_catalog_bound_readiness_entries,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
    validate_catalog_bound_readiness_entries,
)

SCHEMA_VERSION = "nhms.scheduler.file_provider_refresh_receipt.v1"
OUTCOMES = frozenset(
    {
        "dry_run",
        "published",
        "already_running",
        "failed",
        "replace_uncertain",
        "restored_previous",
        "published_receipt_failed",
    }
)
REASONS = frozenset(
    {
        "success",
        "dry_run_complete",
        "refresh_already_running",
        "configuration_invalid",
        "provider_invalid",
        "provider_preimage_changed",
        "provider_replace_failed",
        "provider_replace_uncertain",
        "provider_postread_failed",
        "workspace_limit_exceeded",
        "orphan_limit_exceeded",
        "primary_receipt_failed",
        "receipt_channels_failed",
        "emergency_record_invalid",
        # #1080 registry cutover gate refusal tokens.  Emitted only before any
        # canonical provider replacement; previous canonical bytes stay intact.
        "registry_cutover_undeclared",
        "registry_cutover_removal_refused",
        "registry_cutover_declaration_invalid",
    }
)
REGISTRY_CUTOVER_REFUSAL_REASONS = frozenset(
    {
        "registry_cutover_undeclared",
        "registry_cutover_removal_refused",
        "registry_cutover_declaration_invalid",
    }
)
CUTOVER_SCHEMA_VERSION = "nhms.scheduler.registry_package_cutover.v1"
REGISTRY_MANIFEST_SCHEMA_VERSION = "nhms.scheduler.file_model_registry.v1"
CUTOVER_TRANSITION_MODES = frozenset({"replace"})
CUTOVER_DECLARATION_ENV = "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH"
MAX_CUTOVER_DECLARATION_BYTES = 256 * 1024
CUTOVER_PAST_TOLERANCE = timedelta(hours=24)
CUTOVER_FUTURE_TOLERANCE = timedelta(hours=168)
# Aligned to the 00:00/12:00 UTC compute cycle cadence.
CUTOVER_CYCLE_HOURS = frozenset({0, 12})
# Registry rows compared byte-for-byte on these top-level fields to decide
# "unchanged" vs "package_changed".  Deviations in ANY of these fields escalate.
#
# The whitelist is the deliberate union of the identity fields the spec names
# (see openspec design D7): the three URI/checksum fields plus every documented
# identity field emitted by scheduler_registry_row_from_sources.  It stays a
# tuple so the classifier iterates in declaration order and the identical
# tuple also drives the regression test that guards the whitelist itself.
REGISTRY_MODEL_IDENTITY_FIELDS = (
    "model_package_uri",
    "manifest_uri",
    "package_checksum",
    "basin_version_id",
    "river_network_version_id",
    "shud_code_version",
    "segment_count",
    "output_segment_count",
    "lifecycle_state",
)
# Nested identity fields; classified by (top_level_field, nested_path) pairs.
# Every path is a tuple of successive Mapping keys.  A missing top-level or
# intermediate mapping in either row counts as inequality (escalates to
# package_changed) so drift in a rebuilt resource profile cannot ride through
# silently.
REGISTRY_MODEL_NESTED_IDENTITY_FIELDS = (
    ("resource_profile", ("source_inventory_checksum",)),
)
# Runtime enforcement of the model_id regex must match the schema
# (see schemas/scheduler_file_provider_refresh_receipt.schema.json:204 and
# schemas/scheduler_registry_package_cutover.schema.json:16) so
# `_validate_receipt` and `jsonschema.Draft202012Validator` reject the same
# corpus.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
MAX_MODEL_ID_LENGTH = 128
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_COLLECTION_ITEMS = 256
MAX_STRING_LENGTH = 512
MAX_RESIDUES = 64
MAX_HISTORY = 32
MAX_WORKSPACE_BYTES = 64 * 1024**3
MAX_WORKSPACE_ENTRIES = 250_000
MAX_WORKSPACE_DEPTH = 32
MAX_ORPHANS = 4096
MAX_ORPHAN_EVIDENCE = 256
RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "started_at",
        "finished_at",
        "outcome",
        "reason",
        "operation_outcome",
        "operation_reason",
        "phase",
        "database_free",
        "providers",
        "orphans",
        "residues",
    }
)
# Optional top-level key emitted whenever the registry-cutover gate ran.  The
# schema (allOf conditional) requires it on dry_run/published/refusal outcomes.
RECEIPT_OPTIONAL_KEYS = frozenset({"registry_classification"})


class RefreshError(RuntimeError):
    def __init__(self, reason: str, *, outcome: str = "failed", phase: str = "precommit") -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASONS else "provider_invalid"
        self.outcome = outcome if outcome in OUTCOMES else "failed"
        self.phase = phase


class _WorkspaceBudget:
    """Streaming, pre-side-effect accounting for one private run workspace."""

    def __init__(self, root: Path, *, max_bytes: int, max_entries: int, max_depth: int) -> None:
        self.root = root.expanduser().absolute()
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.total_bytes = 0
        self.total_entries = 0
        self._file_sizes: dict[Path, int] = {}
        self.rescan()

    def rescan(self) -> None:
        self.total_bytes = 0
        self.total_entries = 0
        self._file_sizes = {}
        try:
            metadata = os.lstat(self.root)
        except OSError as error:
            raise RefreshError("workspace_limit_exceeded") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        self._scan_directory(self.root, depth=0)

    def _scan_directory(self, directory: Path, *, depth: int) -> None:
        if depth > self.max_depth:
            raise RefreshError("workspace_limit_exceeded")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RefreshError("workspace_limit_exceeded")
                    self.total_entries += 1
                    if self.total_entries > self.max_entries:
                        raise RefreshError("workspace_limit_exceeded")
                    path = Path(entry.path).absolute()
                    if stat.S_ISDIR(metadata.st_mode):
                        self._scan_directory(path, depth=depth + 1)
                    elif stat.S_ISREG(metadata.st_mode):
                        self.total_bytes += metadata.st_size
                        if self.total_bytes > self.max_bytes:
                            raise RefreshError("workspace_limit_exceeded")
                    else:
                        raise RefreshError("workspace_limit_exceeded")
        except OSError as error:
            raise RefreshError("workspace_limit_exceeded") from error

    def _relative(self, path: Path) -> Path:
        candidate = path.expanduser().absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise RefreshError("workspace_limit_exceeded") from error
        if ".." in relative.parts:
            raise RefreshError("workspace_limit_exceeded")
        return relative

    def ensure_directory(self, path: Path) -> None:
        relative = self._relative(path)
        current = self.root
        for depth, part in enumerate(relative.parts, start=1):
            current = current / part
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                self._reserve(entries=1, byte_delta=0, parent_depth=depth)
                try:
                    os.mkdir(current, 0o700)
                except OSError as error:
                    self.rescan()
                    raise RefreshError("workspace_limit_exceeded") from error
            else:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise RefreshError("workspace_limit_exceeded")

    def write_json(self, path: Path, payload: Mapping[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self.write_bytes(path, content)

    def write_bytes(self, path: Path, content: bytes) -> None:
        target = path.expanduser().absolute()
        self.ensure_directory(target.parent)
        self._reserve_file(target, len(content))
        try:
            target.write_bytes(content)
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        self.verify_external_write(target)

    def reserve_external_write(self, path: Path, size: int) -> None:
        target = path.expanduser().absolute()
        self.ensure_directory(target.parent)
        self._reserve_file(target, size)

    def finalize_external_write(self, path: Path, size: int) -> None:
        target = path.expanduser().absolute()
        reserved = self._file_sizes.get(target)
        if reserved is None:
            self.reserve_external_write(target, size)
            return
        if size < 0 or size > reserved:
            raise RefreshError("workspace_limit_exceeded")
        self._reserve(entries=0, byte_delta=size - reserved, parent_depth=0)
        self._file_sizes[target] = size

    def verify_external_write(self, path: Path) -> None:
        target = path.expanduser().absolute()
        try:
            metadata = os.lstat(target)
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        expected = self._file_sizes.get(target)
        if (
            expected is None
            or stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != expected
        ):
            self.rescan()
            raise RefreshError("workspace_limit_exceeded")

    def _reserve_file(self, target: Path, size: int) -> None:
        if size < 0:
            raise RefreshError("workspace_limit_exceeded")
        relative = self._relative(target)
        try:
            metadata = os.lstat(target)
        except FileNotFoundError:
            old_size = 0
            entry_delta = 1
        else:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RefreshError("workspace_limit_exceeded")
            old_size = self._file_sizes.get(target, metadata.st_size)
            entry_delta = 0
        self._reserve(
            entries=entry_delta,
            byte_delta=size - old_size,
            parent_depth=max(len(relative.parts) - 1, 0),
        )
        self._file_sizes[target] = size

    def _reserve(self, *, entries: int, byte_delta: int, parent_depth: int) -> None:
        next_entries = self.total_entries + entries
        next_bytes = self.total_bytes + byte_delta
        if (
            parent_depth > self.max_depth
            or next_entries > self.max_entries
            or next_bytes > self.max_bytes
            or next_entries < 0
            or next_bytes < 0
        ):
            raise RefreshError("workspace_limit_exceeded")
        self.total_entries = next_entries
        self.total_bytes = next_bytes

    def copy_tree(self, source: Path, target: Path) -> None:
        source_metadata = os.lstat(source)
        if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(source_metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        if target.exists() or target.is_symlink():
            raise RefreshError("workspace_limit_exceeded")
        self.ensure_directory(target)
        self._copy_directory_contents(source, target)

    def copy_file(self, source: Path, target: Path) -> None:
        metadata = os.lstat(source)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RefreshError("workspace_limit_exceeded")
        if target.exists() or target.is_symlink():
            raise RefreshError("workspace_limit_exceeded")
        self.ensure_directory(target.parent)
        self._copy_regular_file(source, target, metadata)

    def _copy_directory_contents(self, source: Path, target: Path) -> None:
        try:
            with os.scandir(source) as entries:
                for entry in entries:
                    source_path = Path(entry.path)
                    target_path = target / entry.name
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise RefreshError("workspace_limit_exceeded")
                    if stat.S_ISDIR(metadata.st_mode):
                        self.ensure_directory(target_path)
                        self._copy_directory_contents(source_path, target_path)
                    elif stat.S_ISREG(metadata.st_mode):
                        self._copy_regular_file(source_path, target_path, metadata)
                    else:
                        raise RefreshError("workspace_limit_exceeded")
        except OSError as error:
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error

    def _copy_regular_file(self, source: Path, target: Path, metadata: os.stat_result) -> None:
        self._reserve_file(target, metadata.st_size)
        try:
            with source.open("rb") as source_handle:
                opened = os.fstat(source_handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                ):
                    raise OSError("workspace source changed before copy")
                with target.open("xb") as target_handle:
                    remaining = metadata.st_size
                    while remaining:
                        chunk = source_handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise OSError("workspace source shortened during copy")
                        target_handle.write(chunk)
                        remaining -= len(chunk)
                    if source_handle.read(1):
                        raise OSError("workspace source grew during copy")
                shutil.copystat(source, target, follow_symlinks=False)
        except OSError as error:
            try:
                target.unlink()
            except OSError:
                pass
            self.rescan()
            raise RefreshError("workspace_limit_exceeded") from error
        self.verify_external_write(target)


@dataclass(frozen=True)
class RefreshConfig:
    basins_root: Path
    registry_uri: str
    readiness_uri: str
    state_uri: str
    object_store_root: Path
    provider_store_root: Path
    object_store_prefix: str
    workspace_root: Path
    receipt_root: Path
    emergency_root: Path
    refresh_lock: Path
    worker_registry_uri: str | None = None

    @classmethod
    def from_env(cls) -> RefreshConfig:
        if any(os.getenv(name) not in (None, "") for name in LIBPQ_CONNECTION_ENV_KEYS):
            raise RefreshError("configuration_invalid")
        return cls(
            basins_root=_absolute_env_path("NHMS_BASINS_ROOT"),
            registry_uri=_required_env("NHMS_SCHEDULER_REGISTRY_MANIFEST"),
            readiness_uri=_required_env("NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"),
            state_uri=_required_env("NHMS_SCHEDULER_STATE_INDEX"),
            object_store_root=_absolute_env_path("OBJECT_STORE_ROOT"),
            provider_store_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_STORE_ROOT"),
            object_store_prefix=_required_env("OBJECT_STORE_PREFIX"),
            workspace_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT"),
            receipt_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT"),
            emergency_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT"),
            refresh_lock=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK"),
            worker_registry_uri=_required_env("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST"),
        )


@dataclass
class EmergencySlot:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int


@dataclass(frozen=True)
class _ProviderRollbackRecord:
    name: str
    path: Path
    containment_root: Path
    max_bytes: int
    previous: bytes | None
    committed: ProviderPreimage


def _provider_failure_reason(error: Exception) -> str | None:
    reason = getattr(error, "reason", None) or getattr(error, "error_code", None)
    details = getattr(error, "details", {})
    if isinstance(details, Mapping) and details.get("provider_reason"):
        reason = details["provider_reason"]
    return str(reason) if reason not in (None, "") else None


def _tracked_provider_publish(
    *,
    name: str,
    path: Path,
    containment_root: Path,
    max_bytes: int,
    previous_preimage: ProviderPreimage,
    previous: bytes | None,
    rollback_stack: list[_ProviderRollbackRecord],
    uncertainty: list[bool],
    publisher: Callable[[Callable[[ProviderPreimage], None]], dict[str, Any]],
) -> dict[str, Any]:
    commit_token: ProviderPreimage | None = None

    def observe_commit(value: ProviderPreimage) -> None:
        nonlocal commit_token
        observed = ProviderPreimage.from_value(value)
        if not observed.exists or observed.sha256 is None:
            uncertainty[0] = True
            raise RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit")
        if commit_token is not None and commit_token != observed:
            uncertainty[0] = True
            raise RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit")
        commit_token = observed

    try:
        result = publisher(observe_commit)
    except Exception as error:
        # A typed expected-preimage conflict means this lane never committed.
        # The current generation belongs to the concurrent authoritative writer
        # and must never be enrolled in this transaction's rollback stack.
        if _provider_failure_reason(error) == "provider_preimage_changed":
            raise
        try:
            current = capture_scheduler_provider_preimage(
                path,
                object_store_root=containment_root,
                max_bytes=max_bytes,
            )
        except (OSError, SafeFilesystemError, ProviderAtomicError, SchedulerFileProviderError) as capture_error:
            uncertainty[0] = True
            raise RefreshError(
                "provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit"
            ) from capture_error
        if commit_token is not None and current == commit_token:
            rollback_stack.append(
                _ProviderRollbackRecord(name, path, containment_root, max_bytes, previous, current)
            )
        elif current != previous_preimage:
            # Without an exact postimage token ownership is unknowable.  A
            # superseding generation is preserved and the transaction reports
            # uncertainty instead of guessing that it owns those bytes.
            uncertainty[0] = True
        raise
    if commit_token is None:
        uncertainty[0] = True
        raise RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit")
    try:
        current = capture_scheduler_provider_preimage(
            path,
            object_store_root=containment_root,
            max_bytes=max_bytes,
        )
    except (OSError, SafeFilesystemError, ProviderAtomicError, SchedulerFileProviderError) as capture_error:
        uncertainty[0] = True
        raise RefreshError(
            "provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit"
        ) from capture_error
    if current != commit_token:
        uncertainty[0] = True
        raise RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit")
    rollback_stack.append(
        _ProviderRollbackRecord(name, path, containment_root, max_bytes, previous, commit_token)
    )
    return result


def refresh_scheduler_file_providers(config: RefreshConfig, *, dry_run: bool) -> dict[str, Any]:
    started = datetime.now(UTC)
    run_id = f"refresh_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    _preflight_config(config)
    run_workspace = config.workspace_root / run_id
    ensure_directory_no_follow(run_workspace, containment_root=config.workspace_root)
    run_workspace_identity = os.lstat(run_workspace)
    workspace_budget = _WorkspaceBudget(
        run_workspace,
        max_bytes=MAX_WORKSPACE_BYTES,
        max_entries=MAX_WORKSPACE_ENTRIES,
        max_depth=MAX_WORKSPACE_DEPTH,
    )
    try:
        emergency_slot = _reserve_emergency_slot(config.emergency_root, run_id)
    except (OSError, SafeFilesystemError) as error:
        try:
            _cleanup_run_workspace(run_workspace, run_workspace_identity, containment_root=config.workspace_root)
        except (OSError, SafeFilesystemError, RefreshError):
            pass
        raise RefreshError("primary_receipt_failed", phase="receipt") from error
    receipt: dict[str, Any]
    committed: list[dict[str, Any]] = []
    rollback_stack: list[_ProviderRollbackRecord] = []
    transaction_uncertainty = [False]
    orphan_paths: list[str] = []
    orphan_total = 0
    orphan_discovered_total = 0
    orphan_attempted_total = 0
    # Populated by the registry precommit gate; must be included on every
    # dry_run/published/refusal receipt per #1080 spec.
    registry_classification: dict[str, Any] | None = None
    cutover_declaration_env = os.getenv(CUTOVER_DECLARATION_ENV, "").strip() or None

    def rollback_receipt_if_needed(*, preserve_failure: bool = False) -> dict[str, Any] | None:
        if not rollback_stack:
            return None
        restored = _rollback_provider_transaction(rollback_stack)
        if restored and not transaction_uncertainty[0]:
            rollback_stack.clear()
            committed.clear()
            if preserve_failure:
                return None
            return _receipt(
                run_id=run_id,
                started=started,
                outcome="restored_previous",
                reason="provider_postread_failed",
                phase="postcommit",
                providers=[],
                orphan_paths=orphan_paths,
                orphan_total=orphan_total,
                orphan_discovered_total=orphan_discovered_total,
                orphan_attempted_total=orphan_attempted_total,
                registry_classification=registry_classification,
            )
        return _receipt(
            run_id=run_id,
            started=started,
            outcome="replace_uncertain",
            reason="provider_replace_uncertain",
            phase="postcommit",
            providers=committed,
            orphan_paths=orphan_paths,
            orphan_total=orphan_total,
            orphan_discovered_total=orphan_discovered_total,
            orphan_attempted_total=orphan_attempted_total,
            registry_classification=registry_classification,
        )

    try:
        with provider_destination_lock(config.refresh_lock, blocking=False):
            registry_preimage = capture_scheduler_provider_preimage(
                config.registry_uri,
                object_store_root=config.provider_store_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            registry_previous = (
                read_bytes_limited_no_follow(
                    Path(config.registry_uri),
                    max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                    containment_root=config.provider_store_root,
                )
                if config.worker_registry_uri is not None and registry_preimage.exists
                else None
            )
            if (
                registry_previous is not None
                and hashlib.sha256(registry_previous).hexdigest() != registry_preimage.sha256
            ):
                raise RefreshError("provider_preimage_changed")
            # Registry renewal is rebuilt independently from Basins.  The
            # header is evidence only; the preimage captured first remains the
            # commit CAS, so a concurrent registry generation cannot be lost.
            registry_before = _read_provider_header(
                Path(config.registry_uri),
                containment_root=config.provider_store_root,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            worker_registry_preimage: ProviderPreimage | None = None
            worker_registry_before: dict[str, Any] = {}
            worker_registry_previous: bytes | None = None
            worker_registry_result: dict[str, Any] | None = None
            worker_registry_committed: ProviderPreimage | None = None
            if config.worker_registry_uri is not None:
                worker_registry_preimage = capture_scheduler_provider_preimage(
                    config.worker_registry_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                )
                if worker_registry_preimage.exists:
                    worker_registry_previous = read_bytes_limited_no_follow(
                        Path(config.worker_registry_uri),
                        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                        containment_root=config.object_store_root,
                    )
                    if hashlib.sha256(worker_registry_previous).hexdigest() != worker_registry_preimage.sha256:
                        raise RefreshError("provider_preimage_changed")
                    worker_registry_before = _read_provider_header(
                        Path(config.worker_registry_uri),
                        containment_root=config.object_store_root,
                        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                    )
                if dry_run and worker_registry_preimage.sha256 != registry_preimage.sha256:
                    raise RefreshError("provider_invalid")
            readiness_preimage = capture_scheduler_provider_preimage(
                config.readiness_uri,
                object_store_root=config.provider_store_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=MAX_READINESS_INDEX_BYTES,
            )
            readiness_previous = (
                read_bytes_limited_no_follow(
                    Path(config.readiness_uri),
                    max_bytes=MAX_READINESS_INDEX_BYTES,
                    containment_root=config.provider_store_root,
                )
                if config.worker_registry_uri is not None and readiness_preimage.exists
                else None
            )
            if (
                readiness_previous is not None
                and hashlib.sha256(readiness_previous).hexdigest() != readiness_preimage.sha256
            ):
                raise RefreshError("provider_preimage_changed")
            readiness_before = _read_provider_header(
                Path(config.readiness_uri),
                containment_root=config.provider_store_root,
                max_bytes=MAX_READINESS_INDEX_BYTES,
            )
            state_repository = FileStateSnapshotIndexRepository(
                index_uri=config.state_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
            )
            state_entries, state_before, state_preimage = state_repository.validated_entries_for_renewal()
            state_previous = (
                read_bytes_limited_no_follow(
                    Path(config.state_uri),
                    max_bytes=MAX_STATE_SNAPSHOT_INDEX_BYTES,
                    containment_root=config.provider_store_root,
                )
                if config.worker_registry_uri is not None and state_preimage.exists
                else None
            )
            if state_previous is not None and hashlib.sha256(state_previous).hexdigest() != state_preimage.sha256:
                raise RefreshError("provider_preimage_changed")
            readiness_entries: list[dict[str, Any]] = []
            readiness_derivation: dict[str, Any] = {}
            registry_generated_at = datetime.now(UTC)
            # Snapshot the previous canonical registry once, inside the
            # destination lock, so classification sees the exact bytes the
            # canonical writer is about to replace.  The loader now returns
            # (sha256, models, raw_bytes) from the same read, so
            # previous_registry_sha256_snapshot and
            # previous_registry_bytes_snapshot are guaranteed to come from
            # the same page cache read (finding C-F2).
            try:
                previous_canonical = _load_previous_canonical_registry(
                    config.registry_uri,
                    containment_root=config.provider_store_root,
                )
            except RefreshError:
                raise
            if previous_canonical is None:
                previous_registry_sha256_snapshot: str | None = None
                previous_registry_bytes_snapshot: bytes | None = None
                previous_models_snapshot: list[dict[str, Any]] = []
            else:
                (
                    previous_registry_sha256_snapshot,
                    previous_models_snapshot,
                    previous_registry_bytes_snapshot,
                ) = previous_canonical

            def _classification_sink(payload: dict[str, Any]) -> None:
                nonlocal registry_classification
                registry_classification = payload

            def precommit_provider_generation(
                workspace: Path,
                packages: Sequence[Mapping[str, Any]],
                registry_models: Sequence[Mapping[str, Any]],
            ) -> None:
                nonlocal readiness_entries, readiness_derivation
                _registry_precommit_gate(
                    workspace,
                    packages,
                    registry_models,
                    previous_registry_bytes=previous_registry_bytes_snapshot,
                    previous_registry_sha256=previous_registry_sha256_snapshot,
                    prospective_generated_at=registry_generated_at,
                    cutover_declaration_env=cutover_declaration_env,
                    dry_run=dry_run,
                    classification_sink=_classification_sink,
                )
                readiness_entries, readiness_derivation = derive_catalog_bound_readiness_entries(
                    registry_models,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                )
                validate_catalog_bound_readiness_entries(
                    readiness_entries,
                    registry_models,
                    destination_uri=config.readiness_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                )
                if not dry_run and config.worker_registry_uri is not None:
                    nonlocal worker_registry_result, worker_registry_committed
                    worker_registry_result = _tracked_provider_publish(
                        name="registry_worker_mirror",
                        path=Path(config.worker_registry_uri),
                        containment_root=config.object_store_root,
                        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                        previous_preimage=worker_registry_preimage or ProviderPreimage(False),
                        previous=worker_registry_previous,
                        rollback_stack=rollback_stack,
                        uncertainty=transaction_uncertainty,
                        publisher=lambda observe_commit: publish_scheduler_registry_manifest(
                            registry_models,
                            config.worker_registry_uri or "",
                            object_store_root=config.object_store_root,
                            object_store_prefix=config.object_store_prefix,
                            generated_at=registry_generated_at,
                            expected_preimage=worker_registry_preimage,
                            commit_observer=observe_commit,
                        ),
                    )
                    worker_registry_committed = rollback_stack[-1].committed

            # R2-A1: the runner installs the cutover gate itself upstream via
            # `precommit_provider_generation`; audit that fact on the publisher
            # summary/manifest receipt so operators reading either side see
            # the same gate mode and declaration-present bit.
            runner_cutover_gate_audit = {
                "mode": "enforced",
                "declaration_env": CUTOVER_DECLARATION_ENV,
                "declaration_present": _cutover_declaration_env_resolves_to_file(
                    cutover_declaration_env
                ),
            }

            def publish_registry(
                commit_observer: Callable[[ProviderPreimage], None] | None = None,
            ) -> dict[str, Any]:
                if _env_flag("NHMS_SCHEDULER_REQUIRE_DIRECT_GRID"):
                    if not previous_models_snapshot:
                        raise RefreshError("provider_invalid")
                    workspace = run_workspace / "registry"
                    workspace.mkdir(parents=True, exist_ok=True)
                    precommit_provider_generation(workspace, [], previous_models_snapshot)
                    if dry_run:
                        return {
                            "status": "dry_run",
                            "selected_model_count": len(previous_models_snapshot),
                            "packages": [],
                            "registry": {"model_count": len(previous_models_snapshot)},
                        }
                    registry_receipt = publish_scheduler_registry_manifest(
                        previous_models_snapshot,
                        config.registry_uri,
                        object_store_root=config.object_store_root,
                        object_store_prefix=config.object_store_prefix,
                        generated_at=registry_generated_at,
                        expected_preimage=registry_preimage,
                        commit_observer=commit_observer,
                        require_direct_grid=True,
                    )
                    return {
                        "status": "published",
                        "selected_model_count": len(previous_models_snapshot),
                        "packages": [],
                        "registry": registry_receipt,
                    }
                return publish_all_basin_scheduler_registry(
                    basins_root=config.basins_root,
                    registry_manifest=config.registry_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    work_dir=run_workspace / "registry",
                    dry_run=dry_run,
                    expected_preimage=registry_preimage,
                    registry_generated_at=registry_generated_at,
                    registry_commit_observer=commit_observer,
                    precommit_validator=precommit_provider_generation,
                    resource_validator=_enforce_workspace_bounds,
                    workspace_budget=workspace_budget,
                    max_contexts=MAX_ORPHANS,
                    cutover_gate=runner_cutover_gate_audit,
                )

            if not dry_run and config.worker_registry_uri is not None:
                registry_result = _tracked_provider_publish(
                    name="registry",
                    path=Path(config.registry_uri),
                    containment_root=config.provider_store_root,
                    max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
                    previous_preimage=registry_preimage,
                    previous=registry_previous,
                    rollback_stack=rollback_stack,
                    uncertainty=transaction_uncertainty,
                    publisher=lambda observe_commit: publish_registry(observe_commit),
                )
            else:
                registry_result = publish_registry()
            if not readiness_entries or readiness_derivation.get("status") != "ready":
                raise RefreshError("provider_invalid")
            _enforce_workspace_bounds(run_workspace)
            orphan_paths = sorted(
                f"package:{hashlib.sha256(str(item.get('manifest_uri') or '').encode()).hexdigest()[:32]}"
                for item in registry_result.get("packages", [])
                if isinstance(item, Mapping) and item.get("status") == "published"
            )
            if len(orphan_paths) > MAX_ORPHANS:
                raise RefreshError("orphan_limit_exceeded")
            orphan_total = len(orphan_paths)
            orphan_discovered_total = int(registry_result.get("selected_model_count") or 0)
            orphan_attempted_total = len(registry_result.get("packages") or [])
            registry_publish_evidence = dict(registry_result.get("registry") or {})
            registry_publish_evidence.setdefault(
                "model_count", int(registry_result.get("selected_model_count") or 0)
            )
            provider_evidence = [
                _provider_evidence(
                    "registry", {**registry_preimage.to_dict(), **registry_before}, registry_publish_evidence
                ),
            ]
            if config.worker_registry_uri is not None:
                worker_evidence = _provider_evidence(
                    "registry_worker_mirror",
                    {**(worker_registry_preimage or ProviderPreimage(False)).to_dict(), **worker_registry_before},
                    worker_registry_result,
                )
                provider_evidence.append(worker_evidence)
                if not dry_run and worker_evidence["after_sha256"] != provider_evidence[0]["after_sha256"]:
                    raise RefreshError("provider_invalid", phase="postcommit")
            provider_evidence.extend(
                [
                    _provider_evidence(
                        "readiness", {**readiness_preimage.to_dict(), **readiness_before}, readiness_derivation
                    ),
                    _provider_evidence("state", {**state_preimage.to_dict(), **state_before}, state_before),
                ]
            )
            if dry_run:
                for provider in provider_evidence:
                    provider["after_sha256"] = provider["before_sha256"]
                    provider["after_schema_version"] = provider["before_schema_version"]
                    provider["after_generated_at"] = provider["before_generated_at"]
                    provider["after_payload_checksum"] = provider["before_payload_checksum"]
            if not dry_run:
                committed.append(provider_evidence[0])
                provider_offset = 1
                if config.worker_registry_uri is not None:
                    committed.append(provider_evidence[1])
                    provider_offset = 2

                def publish_readiness(
                    commit_observer: Callable[[ProviderPreimage], None] | None = None,
                ) -> dict[str, Any]:
                    return publish_canonical_readiness_index(
                        readiness_entries,
                        config.readiness_uri,
                        object_store_root=config.object_store_root,
                        object_store_prefix=config.object_store_prefix,
                        expected_preimage=readiness_preimage,
                        verify_external_references=True,
                        commit_observer=commit_observer,
                    )

                readiness_result = (
                    _tracked_provider_publish(
                        name="readiness",
                        path=Path(config.readiness_uri),
                        containment_root=config.provider_store_root,
                        max_bytes=MAX_READINESS_INDEX_BYTES,
                        previous_preimage=readiness_preimage,
                        previous=readiness_previous,
                        rollback_stack=rollback_stack,
                        uncertainty=transaction_uncertainty,
                        publisher=lambda observe_commit: publish_readiness(observe_commit),
                    )
                    if config.worker_registry_uri is not None
                    else publish_readiness()
                )
                provider_evidence[provider_offset] = _provider_evidence(
                    "readiness", {**readiness_preimage.to_dict(), **readiness_before}, readiness_result
                )
                committed.append(provider_evidence[provider_offset])

                def publish_state(
                    commit_observer: Callable[[ProviderPreimage], None] | None = None,
                ) -> dict[str, Any]:
                    return publish_state_snapshot_index(
                        state_entries,
                        config.state_uri,
                        object_store_root=config.object_store_root,
                        object_store_prefix=config.object_store_prefix,
                        expected_preimage=state_preimage,
                        commit_observer=commit_observer,
                    )

                state_result = (
                    _tracked_provider_publish(
                        name="state",
                        path=Path(config.state_uri),
                        containment_root=config.provider_store_root,
                        max_bytes=MAX_STATE_SNAPSHOT_INDEX_BYTES,
                        previous_preimage=state_preimage,
                        previous=state_previous,
                        rollback_stack=rollback_stack,
                        uncertainty=transaction_uncertainty,
                        publisher=lambda observe_commit: publish_state(observe_commit),
                    )
                    if config.worker_registry_uri is not None
                    else publish_state()
                )
                provider_evidence[provider_offset + 1] = _provider_evidence(
                    "state", {**state_preimage.to_dict(), **state_before}, state_result
                )
                committed.append(provider_evidence[provider_offset + 1])
            receipt = _receipt(
                run_id=run_id,
                started=started,
                outcome="dry_run" if dry_run else "published",
                reason="dry_run_complete" if dry_run else "success",
                phase="complete",
                providers=provider_evidence,
                orphan_paths=orphan_paths,
                orphan_total=orphan_total,
                orphan_discovered_total=orphan_discovered_total,
                orphan_attempted_total=orphan_attempted_total,
                registry_classification=registry_classification,
            )
    except ProviderAtomicError as error:
        rollback_receipt = rollback_receipt_if_needed(
            preserve_failure=error.reason == "provider_preimage_changed"
        )
        receipt = rollback_receipt or _receipt(
            run_id=run_id,
            started=started,
            outcome="already_running" if error.reason == "provider_already_running" else "failed",
            reason=(
                "refresh_already_running"
                if error.reason == "provider_already_running"
                else error.reason
                if error.reason == "provider_preimage_changed"
                else "provider_invalid"
            ),
            phase=error.phase,
            providers=committed,
            registry_classification=registry_classification,
        )
    except (RefreshError, SchedulerRegistryPublishError, SchedulerFileProviderError, StateManagerError) as error:
        reason = getattr(error, "reason", None) or getattr(error, "error_code", None) or "provider_invalid"
        details = getattr(error, "details", {})
        if isinstance(details, Mapping):
            provider_reason = details.get("provider_reason")
            if provider_reason:
                reason = str(provider_reason)
            packages = details.get("packages")
            if isinstance(packages, Sequence) and not isinstance(packages, str | bytes | bytearray):
                orphan_paths = sorted(
                    f"package:{str(item.get('orphan_id') or '')}"
                    for item in packages
                    if isinstance(item, Mapping) and item.get("status") == "published"
                )
            created_total = details.get("created_total")
            if isinstance(created_total, int) and not isinstance(created_total, bool):
                orphan_total = created_total
            else:
                orphan_total = len(orphan_paths)
            discovered_total = details.get("context_total", details.get("discovered_total", 0))
            attempted_total = details.get("attempted_total", 0)
            if isinstance(discovered_total, int) and not isinstance(discovered_total, bool):
                orphan_discovered_total = discovered_total
            if isinstance(attempted_total, int) and not isinstance(attempted_total, bool):
                orphan_attempted_total = attempted_total
        evidence = getattr(error, "evidence", {})
        evidence_phase = evidence.get("phase") if isinstance(evidence, Mapping) else None
        detail_phase = details.get("provider_phase") if isinstance(details, Mapping) else None
        phase = str(getattr(error, "phase", None) or evidence_phase or detail_phase or "precommit")
        outcome = str(getattr(error, "outcome", "failed"))
        rollback_receipt = rollback_receipt_if_needed(
            preserve_failure=reason == "provider_preimage_changed"
        )
        if rollback_receipt is not None:
            receipt = rollback_receipt
        elif reason == "provider_preimage_changed":
            reason = "provider_preimage_changed"
        if rollback_receipt is None and reason == "provider_restored_previous":
            outcome, reason = "restored_previous", "provider_postread_failed"
        elif rollback_receipt is None and reason == "provider_replace_uncertain":
            outcome, reason = "replace_uncertain", "provider_replace_uncertain"
        elif rollback_receipt is None and reason not in REASONS:
            reason = "provider_invalid"
        if rollback_receipt is None:
            receipt = _receipt(
                run_id=run_id,
                started=started,
                outcome=outcome,
                reason=reason,
                phase=phase,
                providers=committed,
                orphan_paths=orphan_paths,
                orphan_total=orphan_total,
                orphan_discovered_total=orphan_discovered_total,
                orphan_attempted_total=orphan_attempted_total,
                registry_classification=registry_classification,
            )
    except Exception:
        rollback_receipt = rollback_receipt_if_needed()
        receipt = rollback_receipt or _receipt(
            run_id=run_id,
            started=started,
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=committed,
            registry_classification=registry_classification,
        )

    try:
        _cleanup_run_workspace(run_workspace, run_workspace_identity, containment_root=config.workspace_root)
    except (OSError, SafeFilesystemError, RefreshError):
        receipt["residues"] = [run_id]

    try:
        _publish_primary_receipt(config.receipt_root, receipt)
        _discard_emergency_slot(emergency_slot)
        emergency_slot = None
    except (OSError, SafeFilesystemError, ValueError, ProviderAtomicError):
        if committed or receipt.get("outcome") == "replace_uncertain":
            if committed and receipt.get("outcome") == "published":
                receipt = {
                    **receipt,
                    "outcome": "published_receipt_failed",
                    "reason": "primary_receipt_failed",
                    "operation_outcome": "published_receipt_failed",
                    "operation_reason": "primary_receipt_failed",
                    "phase": "receipt",
                }
            try:
                _finalize_emergency_slot(emergency_slot, receipt)
                emergency_slot = None
            except (OSError, SafeFilesystemError, ValueError) as error:
                raise RefreshError("receipt_channels_failed", outcome="replace_uncertain", phase="receipt") from error
        else:
            _discard_emergency_slot(emergency_slot)
            emergency_slot = None
            raise RefreshError("primary_receipt_failed", phase="receipt")
    return receipt


def reconstruct_primary_receipt(config: RefreshConfig, emergency_path: Path) -> dict[str, Any]:
    try:
        content = read_bytes_limited_no_follow(
            emergency_path,
            max_bytes=MAX_RECEIPT_BYTES,
            containment_root=config.emergency_root,
        )
        receipt = json.loads(content)
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("emergency_record_invalid") from error
    try:
        receipt = _validate_receipt(receipt)
    except (TypeError, ValueError) as error:
        raise RefreshError("emergency_record_invalid") from error
    if receipt.get("outcome") != "published_receipt_failed":
        raise RefreshError("emergency_record_invalid")
    provider_uris = {
        "registry": (config.registry_uri, config.provider_store_root),
        "readiness": (config.readiness_uri, config.provider_store_root),
        "state": (config.state_uri, config.provider_store_root),
    }
    if config.worker_registry_uri is not None:
        provider_uris["registry_worker_mirror"] = (
            config.worker_registry_uri,
            config.object_store_root,
        )
    for provider in receipt.get("providers", []):
        binding = provider_uris.get(provider.get("name"))
        expected = provider.get("after_sha256")
        if binding is None or not expected:
            raise RefreshError("emergency_record_invalid")
        uri, containment_root = binding
        current = capture_scheduler_provider_preimage(
            uri,
            object_store_root=containment_root,
            object_store_prefix=config.object_store_prefix,
            max_bytes=MAX_READINESS_INDEX_BYTES,
        )
        if current.sha256 != expected:
            raise RefreshError("emergency_record_invalid")
    _publish_primary_receipt(config.receipt_root, receipt)
    return receipt


def validate_current_receipt(config: RefreshConfig, receipt_path: Path) -> dict[str, Any]:
    try:
        content = read_bytes_limited_no_follow(
            receipt_path,
            max_bytes=MAX_RECEIPT_BYTES,
            containment_root=config.receipt_root,
        )
        receipt = _validate_receipt(json.loads(content))
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RefreshError("emergency_record_invalid", phase="receipt") from error
    if receipt.get("outcome") != "published":
        raise RefreshError("emergency_record_invalid", phase="receipt")
    provider_contract = [("registry", config.registry_uri, config.provider_store_root, MAX_REGISTRY_MANIFEST_BYTES)]
    if config.worker_registry_uri is not None:
        provider_contract.append(
            (
                "registry_worker_mirror",
                config.worker_registry_uri,
                config.object_store_root,
                MAX_REGISTRY_MANIFEST_BYTES,
            )
        )
    provider_contract.extend(
        [
            ("readiness", config.readiness_uri, config.provider_store_root, MAX_READINESS_INDEX_BYTES),
            ("state", config.state_uri, config.provider_store_root, MAX_STATE_SNAPSHOT_INDEX_BYTES),
        ]
    )
    providers = receipt.get("providers")
    if not isinstance(providers, list) or len(providers) != len(provider_contract):
        raise RefreshError("emergency_record_invalid", phase="receipt")
    for provider, (name, uri, containment_root, max_bytes) in zip(providers, provider_contract, strict=True):
        if provider.get("name") != name:
            raise RefreshError("emergency_record_invalid", phase="receipt")
        try:
            current = capture_scheduler_provider_preimage(
                uri,
                object_store_root=containment_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=max_bytes,
            )
        except SchedulerFileProviderError as error:
            raise RefreshError("emergency_record_invalid", phase="receipt") from error
        if not current.exists or current.sha256 != provider.get("after_sha256"):
            raise RefreshError("emergency_record_invalid", phase="receipt")
    if config.worker_registry_uri is not None:
        registry_evidence = providers[0]
        mirror_evidence = providers[1]
        if (
            registry_evidence.get("after_sha256") != mirror_evidence.get("after_sha256")
            or registry_evidence.get("entry_count") != mirror_evidence.get("entry_count")
        ):
            raise RefreshError("emergency_record_invalid", phase="receipt")
    return receipt


def _preflight_config(config: RefreshConfig) -> None:
    for directory in (config.basins_root, config.object_store_root, config.provider_store_root):
        verify_directory_no_follow(directory)
    _ensure_private_directory(config.refresh_lock.parent)
    lock_parent = os.lstat(config.refresh_lock.parent)
    if lock_parent.st_uid != os.geteuid() or stat.S_IMODE(lock_parent.st_mode) & 0o077:
        raise RefreshError("configuration_invalid")
    for directory in (
        config.workspace_root,
        config.receipt_root,
        config.emergency_root,
    ):
        _ensure_private_directory(directory)
        metadata = os.lstat(directory)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RefreshError("configuration_invalid")
    for uri in (config.registry_uri, config.readiness_uri, config.state_uri):
        provider_path = Path(uri).expanduser()
        if not provider_path.is_absolute():
            raise RefreshError("configuration_invalid")
        try:
            provider_path.relative_to(config.provider_store_root)
        except ValueError as error:
            raise RefreshError("configuration_invalid") from error
    if config.worker_registry_uri is not None:
        worker_registry_path = Path(config.worker_registry_uri).expanduser()
        if not worker_registry_path.is_absolute() or worker_registry_path == Path(config.registry_uri):
            raise RefreshError("configuration_invalid")
        try:
            worker_registry_path.relative_to(config.object_store_root)
        except ValueError as error:
            raise RefreshError("configuration_invalid") from error
    if not config.object_store_prefix.startswith("s3://"):
        raise RefreshError("configuration_invalid")


def _restore_worker_registry_mirror(
    config: RefreshConfig,
    *,
    previous: bytes | None,
    expected_current: ProviderPreimage,
) -> None:
    if config.worker_registry_uri is None:
        raise RefreshError("provider_invalid", outcome="replace_uncertain", phase="postcommit")
    _restore_provider_path(
        Path(config.worker_registry_uri),
        containment_root=config.object_store_root,
        max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
        previous=previous,
        expected_current=expected_current,
    )


def _restore_provider_path(
    path: Path,
    *,
    containment_root: Path,
    max_bytes: int,
    previous: bytes | None,
    expected_current: ProviderPreimage,
) -> None:
    try:
        if previous is not None:
            atomic_replace_provider_bytes(
                path,
                previous,
                containment_root=containment_root,
                max_bytes=max_bytes,
                expected_preimage=expected_current,
            )
            return
        with provider_destination_lock(path, containment_root=containment_root, blocking=False):
            current = capture_scheduler_provider_preimage(
                path,
                object_store_root=containment_root,
                max_bytes=max_bytes,
            )
            if current != expected_current:
                raise ProviderAtomicError("provider_preimage_changed", phase="postcommit")
            parent_fd = open_directory_no_follow(path.parent, containment_root=containment_root)
            try:
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except (OSError, SafeFilesystemError, ProviderAtomicError, SchedulerFileProviderError) as error:
        raise RefreshError("provider_replace_uncertain", outcome="replace_uncertain", phase="postcommit") from error


def _rollback_provider_transaction(records: Sequence[_ProviderRollbackRecord]) -> bool:
    uncertain = False
    for record in reversed(records):
        try:
            _restore_provider_path(
                record.path,
                containment_root=record.containment_root,
                max_bytes=record.max_bytes,
                previous=record.previous,
                expected_current=record.committed,
            )
            restored = capture_scheduler_provider_preimage(
                record.path,
                object_store_root=record.containment_root,
                max_bytes=record.max_bytes,
            )
            expected_sha = hashlib.sha256(record.previous).hexdigest() if record.previous is not None else None
            if restored.exists != (record.previous is not None) or restored.sha256 != expected_sha:
                uncertain = True
        except (OSError, RefreshError, ProviderAtomicError, SchedulerFileProviderError):
            uncertain = True
    return not uncertain


def _ensure_private_directory(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        ensure_directory_no_follow(path)
        os.chmod(path, 0o700, follow_symlinks=False)
    else:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RefreshError("configuration_invalid")
    verify_directory_no_follow(path)


def _absolute_env_path(name: str) -> Path:
    value = Path(_required_env(name)).expanduser()
    if not value.is_absolute():
        raise RefreshError("configuration_invalid")
    return value


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RefreshError("configuration_invalid")
    return value


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _reserve_emergency_slot(root: Path, run_id: str) -> EmergencySlot:
    ensure_directory_no_follow(root)
    path = root / f"{run_id}.reserved.json"
    parent_fd = open_directory_no_follow(root)
    fd = -1
    try:
        fd = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("emergency slot is not regular")
        os.fsync(fd)
        os.fsync(parent_fd)
        return EmergencySlot(path, parent_fd, fd, opened.st_dev, opened.st_ino)
    except Exception:
        if fd >= 0:
            try:
                current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    os.unlink(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass
            os.close(fd)
        os.close(parent_fd)
        raise


def _finalize_emergency_slot(slot: EmergencySlot, receipt: Mapping[str, Any]) -> None:
    content = _receipt_bytes(_validate_receipt(receipt))
    completed = False
    try:
        _verify_emergency_slot(slot)
        os.fchmod(slot.file_fd, 0o600)
        os.ftruncate(slot.file_fd, 0)
        os.lseek(slot.file_fd, 0, os.SEEK_SET)
        remaining = memoryview(content)
        while remaining:
            written = os.write(slot.file_fd, remaining)
            if written <= 0:
                raise OSError("emergency receipt write made no progress")
            remaining = remaining[written:]
        os.fsync(slot.file_fd)
        if os.fstat(slot.file_fd).st_size != len(content):
            raise OSError("emergency receipt size mismatch")
        os.lseek(slot.file_fd, 0, os.SEEK_SET)
        verified = bytearray()
        while len(verified) < len(content):
            chunk = os.read(slot.file_fd, len(content) - len(verified))
            if not chunk:
                break
            verified.extend(chunk)
        if bytes(verified) != content:
            raise OSError("emergency receipt digest mismatch")
        os.fsync(slot.parent_fd)
        completed = True
    finally:
        if not completed:
            try:
                _verify_emergency_slot(slot)
                os.unlink(slot.path.name, dir_fd=slot.parent_fd)
                os.fsync(slot.parent_fd)
            except (OSError, RefreshError):
                pass
        os.close(slot.file_fd)
        os.close(slot.parent_fd)


def _verify_emergency_slot(slot: EmergencySlot) -> None:
    opened = os.fstat(slot.file_fd)
    current = os.stat(slot.path.name, dir_fd=slot.parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (slot.device, slot.inode)
        or (current.st_dev, current.st_ino) != (slot.device, slot.inode)
    ):
        raise RefreshError("receipt_channels_failed", outcome="replace_uncertain", phase="receipt")


def _discard_emergency_slot(slot: EmergencySlot) -> None:
    try:
        _verify_emergency_slot(slot)
        os.unlink(slot.path.name, dir_fd=slot.parent_fd)
        os.fsync(slot.parent_fd)
    finally:
        os.close(slot.file_fd)
        os.close(slot.parent_fd)


def _lenient_receipt_order(payload: Any) -> tuple[datetime, str] | None:
    """Extract ``(started_at, run_id)`` from an untrusted receipt payload.

    Used only when reading an existing ``latest.json`` for the monotonic-order
    comparison and history rotation.  Legacy pre-#1080 receipts on disk lack
    the ``registry_classification`` field required by ``_validate_receipt``;
    running the strict validator on them would brick the first post-#1080
    refresh (see finding C-A2).  This lenient reader accepts any payload
    whose ``started_at`` and ``run_id`` parse cleanly; anything malformed
    returns ``None`` and lets the caller default to "replace".
    """
    if not isinstance(payload, Mapping):
        return None
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        started = _parse_receipt_datetime(payload.get("started_at"))
    except ValueError:
        return None
    return started, run_id


def _publish_primary_receipt(root: Path, receipt: Mapping[str, Any]) -> None:
    canonical = _validate_receipt(receipt)
    content = _receipt_bytes(canonical)
    with provider_destination_lock(root / "receipt-publication", containment_root=root):
        history = root / "history"
        ensure_directory_no_follow(history, containment_root=root)
        run_id = str(canonical["run_id"])
        atomic_write_bytes_no_follow(history / f"{run_id}.json", content, containment_root=root, mode=0o600)
        latest_path = root / "latest.json"
        replace_latest = True
        try:
            existing_bytes = read_bytes_limited_no_follow(
                latest_path,
                max_bytes=MAX_RECEIPT_BYTES,
                containment_root=root,
            )
        except FileNotFoundError:
            existing_bytes = None
        if existing_bytes is not None:
            # Read the existing latest.json leniently — legacy pre-#1080
            # receipts lack `registry_classification` and would otherwise
            # brick this write (finding C-A2).  Validation is a publish-time
            # invariant we hold for receipts THIS process writes, not a
            # gate on the previous generation's shape.
            try:
                existing_payload = json.loads(existing_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                existing_payload = None
            existing_order = _lenient_receipt_order(existing_payload)
            if existing_order is not None:
                replace_latest = _receipt_order(canonical) >= existing_order
        if replace_latest:
            atomic_write_bytes_no_follow(latest_path, content, containment_root=root, mode=0o600)
        files: list[tuple[tuple[datetime, str], Path]] = []
        for item in history.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            try:
                historical_bytes = read_bytes_limited_no_follow(
                    item,
                    max_bytes=MAX_RECEIPT_BYTES,
                    containment_root=root,
                )
                historical_payload = json.loads(historical_bytes)
                lenient = _lenient_receipt_order(historical_payload)
                if lenient is None:
                    order = (datetime.min.replace(tzinfo=UTC), item.name)
                else:
                    order = lenient
            except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                order = (datetime.min.replace(tzinfo=UTC), item.name)
            files.append((order, item))
        files.sort(key=lambda pair: pair[0], reverse=True)
        for _order, obsolete in files[MAX_HISTORY:]:
            try:
                obsolete.unlink()
            except FileNotFoundError:
                pass


def _receipt_order(receipt: Mapping[str, Any]) -> tuple[datetime, str]:
    return _parse_receipt_datetime(receipt["started_at"]), str(receipt["run_id"])


def _receipt(
    *,
    run_id: str,
    started: datetime,
    outcome: str,
    reason: str,
    phase: str,
    providers: Sequence[Mapping[str, Any]],
    orphan_paths: Sequence[str] = (),
    orphan_total: int | None = None,
    orphan_discovered_total: int | None = None,
    orphan_attempted_total: int | None = None,
    registry_classification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = list(orphan_paths[:MAX_ORPHAN_EVIDENCE])
    total = len(orphan_paths) if orphan_total is None else orphan_total
    discovered_total = total if orphan_discovered_total is None else orphan_discovered_total
    attempted_total = total if orphan_attempted_total is None else orphan_attempted_total
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "outcome": outcome,
        "reason": reason,
        "operation_outcome": outcome,
        "operation_reason": reason,
        "phase": phase,
        "database_free": True,
        "providers": [dict(item) for item in providers[:MAX_COLLECTION_ITEMS]],
        "orphans": {
            "items": evidence,
            "total": total,
            "discovered_total": discovered_total,
            "attempted_total": attempted_total,
            "created_total": total,
            "truncated": total > len(evidence),
        },
        "residues": [],
    }
    if registry_classification is not None:
        # deep-copy through json to freeze the payload against later mutation.
        receipt["registry_classification"] = json.loads(json.dumps(registry_classification))
    return receipt


def _provider_evidence(name: str, before: Mapping[str, Any], result: Any) -> dict[str, Any]:
    result_map = result if isinstance(result, Mapping) else {}
    checksum = str(result_map.get("content_sha256") or result_map.get("checksum") or "")
    return {
        "name": name,
        "before_sha256": before.get("sha256"),
        "before_inode": before.get("inode"),
        "before_schema_version": before.get("schema_version"),
        "before_generated_at": before.get("generated_at"),
        "before_payload_checksum": before.get("checksum"),
        "after_sha256": checksum.removeprefix("sha256:") or before.get("sha256"),
        "after_schema_version": result_map.get("schema_version") or before.get("schema_version"),
        "after_generated_at": result_map.get("generated_at") or before.get("generated_at"),
        "after_payload_checksum": result_map.get("checksum") or before.get("checksum"),
        "entry_count": int(
            result_map.get("entry_count")
            or result_map.get("model_count")
            or result_map.get("selected_model_count")
            or 0
        ),
    }


def _read_provider_header(path: Path, *, containment_root: Path, max_bytes: int) -> dict[str, Any]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=max_bytes, containment_root=containment_root)
        if len(content) > max_bytes:
            raise RefreshError("provider_invalid")
        payload = json.loads(content)
    except (OSError, SafeFilesystemError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("provider_invalid") from error
    if not isinstance(payload, Mapping):
        raise RefreshError("provider_invalid")
    return {
        "schema_version": payload.get("schema_version"),
        "generated_at": payload.get("generated_at"),
        "checksum": payload.get("checksum"),
    }


def _validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ValueError("receipt_shape_invalid")
    keys = set(receipt)
    if not RECEIPT_KEYS <= keys or (keys - RECEIPT_KEYS) - RECEIPT_OPTIONAL_KEYS:
        raise ValueError("receipt_shape_invalid")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("receipt_schema_invalid")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or len(run_id) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
        raise ValueError("receipt_run_id_invalid")
    started = _parse_receipt_datetime(receipt.get("started_at"))
    finished = _parse_receipt_datetime(receipt.get("finished_at"))
    if finished < started:
        raise ValueError("receipt_time_invalid")
    if receipt.get("outcome") not in OUTCOMES or receipt.get("reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if receipt.get("operation_outcome") not in OUTCOMES or receipt.get("operation_reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if (
        receipt.get("operation_outcome") != receipt.get("outcome")
        or receipt.get("operation_reason") != receipt.get("reason")
        or len(str(receipt.get("reason"))) > 64
        or receipt.get("database_free") is not True
    ):
        raise ValueError("receipt_contract_invalid")
    phase = receipt.get("phase")
    if not isinstance(phase, str) or not phase or len(phase) > 64:
        raise ValueError("receipt_phase_invalid")
    providers = receipt.get("providers")
    residues = receipt.get("residues")
    orphans = receipt.get("orphans")
    if not isinstance(providers, list) or len(providers) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_collection_limit")
    provider_keys = {
        "name",
        "before_sha256",
        "before_inode",
        "before_schema_version",
        "before_generated_at",
        "before_payload_checksum",
        "after_sha256",
        "after_schema_version",
        "after_generated_at",
        "after_payload_checksum",
        "entry_count",
    }
    names: list[str] = []
    for provider in providers:
        if not isinstance(provider, Mapping) or set(provider) != provider_keys:
            raise ValueError("receipt_provider_invalid")
        name = str(provider.get("name") or "")
        names.append(name)
        if name not in {"registry", "registry_worker_mirror", "readiness", "state"}:
            raise ValueError("receipt_provider_invalid")
        for field in ("before_sha256", "after_sha256"):
            value = provider.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("receipt_provider_invalid")
                digest = value
                if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                    raise ValueError("receipt_provider_invalid")
        before_inode = provider.get("before_inode")
        if before_inode is not None and (
            not isinstance(before_inode, int) or isinstance(before_inode, bool) or before_inode < 0
        ):
            raise ValueError("receipt_provider_invalid")
        for field in ("before_schema_version", "after_schema_version"):
            value = provider.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError("receipt_provider_invalid")
        for field in ("before_generated_at", "after_generated_at"):
            value = provider.get(field)
            if value is not None:
                _parse_receipt_datetime(value)
        for field in ("before_payload_checksum", "after_payload_checksum"):
            value = provider.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError("receipt_provider_invalid")
        if (
            not isinstance(provider.get("entry_count"), int)
            or isinstance(provider.get("entry_count"), bool)
            or int(provider["entry_count"]) < 0
        ):
            raise ValueError("receipt_provider_invalid")
    if len(names) != len(set(names)):
        raise ValueError("receipt_provider_invalid")
    if receipt.get("outcome") in {"dry_run", "published"} and names not in (
        ["registry", "readiness", "state"],
        ["registry", "registry_worker_mirror", "readiness", "state"],
    ):
        raise ValueError("receipt_provider_invalid")
    if names == ["registry", "registry_worker_mirror", "readiness", "state"]:
        registry_provider = providers[0]
        mirror_provider = providers[1]
        if receipt.get("outcome") in {"dry_run", "published"} and (
            registry_provider.get("after_sha256") != mirror_provider.get("after_sha256")
            or registry_provider.get("entry_count") != mirror_provider.get("entry_count")
        ):
            raise ValueError("receipt_provider_invalid")
    if not isinstance(residues, list) or len(residues) > MAX_RESIDUES:
        raise ValueError("receipt_residue_limit")
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > MAX_STRING_LENGTH
        or Path(item).is_absolute()
        or ".." in Path(item).parts
        for item in residues
    ):
        raise ValueError("receipt_residue_unsafe")
    if not isinstance(orphans, Mapping) or set(orphans) != {
        "items",
        "total",
        "discovered_total",
        "attempted_total",
        "created_total",
        "truncated",
    }:
        raise ValueError("receipt_orphan_invalid")
    orphan_items = orphans.get("items")
    orphan_total = orphans.get("total")
    discovered_total = orphans.get("discovered_total")
    attempted_total = orphans.get("attempted_total")
    created_total = orphans.get("created_total")
    if (
        not isinstance(orphan_items, list)
        or len(orphan_items) > MAX_ORPHAN_EVIDENCE
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"package:[0-9a-f]{32}", item) is None
            for item in orphan_items
        )
        or not isinstance(orphan_total, int)
        or isinstance(orphan_total, bool)
        or orphan_total > MAX_ORPHANS
        or orphan_total < 0
        or orphan_total < len(orphan_items)
        or not isinstance(discovered_total, int)
        or isinstance(discovered_total, bool)
        or discovered_total < 0
        or not isinstance(attempted_total, int)
        or isinstance(attempted_total, bool)
        or attempted_total < 0
        or not isinstance(created_total, int)
        or isinstance(created_total, bool)
        or created_total != orphan_total
        or discovered_total < attempted_total
        or attempted_total < created_total
        or attempted_total > MAX_ORPHANS
        or not isinstance(orphans.get("truncated"), bool)
        or orphans.get("truncated") is not (orphan_total > len(orphan_items))
    ):
        raise ValueError("receipt_orphan_limit")
    _validate_registry_classification_field(receipt)
    _validate_value_bounds(receipt)
    return json.loads(json.dumps(receipt, ensure_ascii=True))


_CLASSIFICATION_GROUP_KEYS = frozenset({"items", "total", "truncated"})


def _validate_registry_classification_field(receipt: Mapping[str, Any]) -> None:
    classification = receipt.get("registry_classification")
    outcome = receipt.get("outcome")
    reason = receipt.get("reason")
    requires_classification = (
        outcome in {"dry_run", "published"}
        or reason in REGISTRY_CUTOVER_REFUSAL_REASONS
    )
    if classification is None:
        if requires_classification:
            raise ValueError("receipt_classification_required")
        return
    if not isinstance(classification, Mapping):
        raise ValueError("receipt_classification_invalid")
    required = {
        "previous_registry_sha256",
        "new_registry_sha256",
        # R2-N1: partition counts are required on every classified receipt so
        # reconciliation is enforceable as equality, not just non-negative.
        "previous_model_count",
        "prospective_model_count",
        "added",
        "unchanged",
        "removed",
        "package_changed",
        "refused",
        "declared_cutovers",
    }
    if set(classification) != required:
        raise ValueError("receipt_classification_invalid")
    for hash_field in ("previous_registry_sha256", "new_registry_sha256"):
        value = classification.get(hash_field)
        if value is not None:
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError("receipt_classification_invalid")
    previous_count = classification.get("previous_model_count")
    if previous_count is not None:
        if (
            not isinstance(previous_count, int)
            or isinstance(previous_count, bool)
            or previous_count < 0
        ):
            raise ValueError("receipt_classification_invalid")
    prospective_count = classification.get("prospective_model_count")
    if (
        not isinstance(prospective_count, int)
        or isinstance(prospective_count, bool)
        or prospective_count < 0
    ):
        raise ValueError("receipt_classification_invalid")
    for group_name in ("added", "unchanged", "removed"):
        group = classification.get(group_name)
        if not isinstance(group, Mapping) or set(group) != _CLASSIFICATION_GROUP_KEYS:
            raise ValueError("receipt_classification_invalid")
        items = group.get("items")
        if not isinstance(items, list) or len(items) > MAX_COLLECTION_ITEMS:
            raise ValueError("receipt_classification_invalid")
        for item in items:
            if (
                not isinstance(item, str)
                or not item
                or len(item) > MAX_MODEL_ID_LENGTH
                or MODEL_ID_PATTERN.fullmatch(item) is None
            ):
                # Same corpus as the schema (see MODEL_ID_PATTERN). Runtime
                # and jsonschema.Draft202012Validator must accept/reject the
                # same set of items or fixtures drift.
                raise ValueError("receipt_classification_invalid")
        _validate_group_totals(group, items)
    package_changed = classification.get("package_changed")
    _validate_object_group(
        package_changed,
        required_keys={"model_id", "old_checksum", "new_checksum"},
        optional_keys=set(),
    )
    refused = classification.get("refused")
    _validate_object_group(
        refused,
        required_keys={"model_id", "reason"},
        optional_keys={"old_checksum", "new_checksum"},
        reason_enum=REGISTRY_CUTOVER_REFUSAL_REASONS,
    )
    declared = classification.get("declared_cutovers")
    _validate_object_group(
        declared,
        required_keys={
            "model_id",
            "old_checksum",
            "new_checksum",
            "effective_cycle_utc",
            "transition_mode",
        },
        optional_keys=set(),
    )
    _enforce_registry_classification_reconciliation(
        classification, outcome=outcome, reason=reason
    )


def _enforce_registry_classification_reconciliation(
    classification: Mapping[str, Any],
    *,
    outcome: Any,
    reason: Any,
) -> None:
    """Cross-check every classification total against the reconciliation formulas.

    Governing invariants from spec.md:397-403:

    * ``unchanged + package_changed + removed == previous_count`` when the
      previous canonical registry existed.
    * ``added + unchanged + package_changed == prospective_count``.
    * ``declared_cutovers`` model_ids are a subset of ``package_changed``
      model_ids.
    * ``refused`` covers every ``removed`` entry, every ``package_changed``
      entry not in ``declared_cutovers``, and every ``declaration_invalid``
      entry (including synthetic ``__declaration__`` markers).
    * ``dry_run``: reconciliation runs in id-only mode; ``package_changed``
      may legitimately be zero because prospective rows carry only ids.
    * ``published``: ``refused.total == 0`` (a non-zero refusal would have
      raised before commit).
    * refusal outcomes: ``refused.total >= 1``.
    """
    def _total(group_name: str) -> int:
        group = classification.get(group_name)
        if not isinstance(group, Mapping):
            raise ValueError("receipt_classification_invalid")
        total = group.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ValueError("receipt_classification_invalid")
        return total

    def _items(group_name: str) -> list[Any]:
        group = classification.get(group_name)
        if not isinstance(group, Mapping):
            raise ValueError("receipt_classification_invalid")
        items = group.get("items")
        if not isinstance(items, list):
            raise ValueError("receipt_classification_invalid")
        return items

    added_total = _total("added")
    unchanged_total = _total("unchanged")
    removed_total = _total("removed")
    package_changed_total = _total("package_changed")
    refused_total = _total("refused")
    declared_total = _total("declared_cutovers")

    package_changed_ids = {
        item.get("model_id")
        for item in _items("package_changed")
        if isinstance(item, Mapping)
    }
    declared_ids = {
        item.get("model_id")
        for item in _items("declared_cutovers")
        if isinstance(item, Mapping)
    }
    if not declared_ids <= package_changed_ids:
        # `declared_cutovers ⊆ package_changed` per spec — any declared row
        # must also be listed in `package_changed`.
        raise ValueError("receipt_classification_invalid")

    prospective_count = classification.get("prospective_model_count")
    if (
        not isinstance(prospective_count, int)
        or isinstance(prospective_count, bool)
        or prospective_count < 0
    ):
        raise ValueError("receipt_classification_invalid")

    if outcome == "dry_run":
        # dry_run classification is id-only; prospective rows have no
        # checksum, so package_changed/refused stay empty by construction.
        if package_changed_total != 0 or refused_total != 0 or declared_total != 0:
            raise ValueError("receipt_classification_invalid")
        # Even in dry_run the added+unchanged+package_changed equality must
        # bind to the pinned prospective_model_count (package_changed is 0
        # here so this reduces to added+unchanged == prospective_count).
        if added_total + unchanged_total + package_changed_total != prospective_count:
            raise ValueError("receipt_classification_invalid")
        return

    previous_count = classification.get("previous_model_count")
    previous_sha = classification.get("previous_registry_sha256")
    # R2-N1: enforce EQUALITY, not just non-negative bounds.  The pinned
    # counts (`previous_model_count` / `prospective_model_count`) come from
    # `_classify_registry`'s own len(previous_by_id)/len(prospective_by_id)
    # so any drop or gain in the bucket totals fails validation on-disk.
    if previous_sha is None:
        # A missing previous canonical registry MUST also carry a null
        # previous_model_count (bootstrap semantics).  A non-null count with
        # no previous SHA is contradictory shape.
        if previous_count is not None:
            raise ValueError("receipt_classification_invalid")
    else:
        if (
            not isinstance(previous_count, int)
            or isinstance(previous_count, bool)
            or previous_count < 0
        ):
            raise ValueError("receipt_classification_invalid")
        if unchanged_total + package_changed_total + removed_total != previous_count:
            raise ValueError("receipt_classification_invalid")

    if added_total + unchanged_total + package_changed_total != prospective_count:
        raise ValueError("receipt_classification_invalid")

    # refused equals every removed entry + every package_changed entry not
    # in declared_cutovers + every entry rejected by declaration_invalid.
    # The declaration_invalid slice is unbounded (may include the synthetic
    # `__declaration__` marker) so we assert only the lower bound.
    expected_min_refused = removed_total + max(
        package_changed_total - declared_total, 0
    )
    if refused_total < expected_min_refused:
        raise ValueError("receipt_classification_invalid")

    if outcome == "published":
        # A publish that emits any refused entry contradicts the gate
        # contract (the gate would have raised before commit).
        if refused_total != 0:
            raise ValueError("receipt_classification_invalid")

    if reason in REGISTRY_CUTOVER_REFUSAL_REASONS:
        if refused_total < 1:
            raise ValueError("receipt_classification_invalid")


def _validate_group_totals(group: Mapping[str, Any], items: Sequence[Any]) -> None:
    total = group.get("total")
    truncated = group.get("truncated")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("receipt_classification_invalid")
    if total < len(items) or not isinstance(truncated, bool):
        raise ValueError("receipt_classification_invalid")
    if truncated is not (total > len(items)):
        raise ValueError("receipt_classification_invalid")


def _validate_object_group(
    group: Any,
    *,
    required_keys: set[str],
    optional_keys: set[str],
    reason_enum: frozenset[str] | None = None,
) -> None:
    if not isinstance(group, Mapping) or set(group) != _CLASSIFICATION_GROUP_KEYS:
        raise ValueError("receipt_classification_invalid")
    items = group.get("items")
    if not isinstance(items, list) or len(items) > MAX_COLLECTION_ITEMS:
        raise ValueError("receipt_classification_invalid")
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("receipt_classification_invalid")
        keys = set(item)
        if not required_keys <= keys or (keys - required_keys) - optional_keys:
            raise ValueError("receipt_classification_invalid")
        for name, value in item.items():
            if name == "model_id":
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value) > MAX_MODEL_ID_LENGTH
                    or MODEL_ID_PATTERN.fullmatch(value) is None
                ):
                    # Same corpus as the schema (see MODEL_ID_PATTERN).
                    raise ValueError("receipt_classification_invalid")
            elif name in {"old_checksum", "new_checksum"} and value is not None:
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                ):
                    raise ValueError("receipt_classification_invalid")
            elif name == "reason":
                if reason_enum is not None and value not in reason_enum:
                    raise ValueError("receipt_classification_invalid")
            elif name == "transition_mode":
                if value not in CUTOVER_TRANSITION_MODES:
                    raise ValueError("receipt_classification_invalid")
            elif isinstance(value, str):
                if len(value) > MAX_STRING_LENGTH:
                    raise ValueError("receipt_classification_invalid")
    _validate_group_totals(group, items)


def _parse_receipt_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("receipt_time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("receipt_time_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("receipt_time_invalid")
    return parsed.astimezone(UTC)


def _validate_value_bounds(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and len(item) > MAX_STRING_LENGTH:
            raise ValueError("receipt_string_limit")
        if isinstance(item, Mapping):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError("receipt_collection_limit")
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            if len(item) > MAX_COLLECTION_ITEMS:
                raise ValueError("receipt_collection_limit")
            stack.extend(item)


def _receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    content = json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=True).encode() + b"\n"
    if len(content) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt_size_limit")
    return content


def _enforce_workspace_bounds(root: Path) -> None:
    _WorkspaceBudget(
        root,
        max_bytes=MAX_WORKSPACE_BYTES,
        max_entries=MAX_WORKSPACE_ENTRIES,
        max_depth=MAX_WORKSPACE_DEPTH,
    )


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise RefreshError("provider_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cutover_declaration_env_resolves_to_file(env_value: str | None) -> bool:
    """Return True when the cutover declaration env points at a readable file.

    R2-A1 audit helper: only records "did the operator stage a file the runner
    could open" — schema validity is proven separately by
    ``_load_cutover_declaration``.  Missing env, symlinks, non-regular files,
    and permission errors collapse to ``False`` so the audit fact never
    overclaims presence.
    """
    if not env_value:
        return False
    path = Path(env_value).expanduser()
    try:
        stat_result = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(stat_result.st_mode):
        return False
    return os.access(str(path), os.R_OK)


def _load_previous_canonical_registry(
    registry_uri: str, *, containment_root: Path
) -> tuple[str, list[dict[str, Any]], bytes] | None:
    """Return (sha256, models, raw_bytes) for the current canonical manifest.

    Missing file is legitimate first publication and returns ``None``.  Any
    other read/parse failure is a hard refusal condition and propagates.
    Returned ``raw_bytes`` lets the caller hand the exact bytes that were
    classified to downstream code without a second read (see finding C-F2).
    """
    path = Path(registry_uri)
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            containment_root=containment_root,
        )
    except FileNotFoundError:
        return None
    except (OSError, SafeFilesystemError) as error:
        raise RefreshError("provider_invalid") from error
    # Sentinel-plus-one: read_bytes_limited_no_follow returns max_bytes+1
    # bytes when the file is oversize; enforce the explicit cap here so the
    # loader is symmetric with _read_provider_header/provider_atomic.
    if len(content) > MAX_REGISTRY_MANIFEST_BYTES:
        raise RefreshError("provider_invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("provider_invalid") from error
    if not isinstance(payload, Mapping):
        raise RefreshError("provider_invalid")
    models = payload.get("models")
    if not isinstance(models, list):
        raise RefreshError("provider_invalid")
    normalized: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping):
            raise RefreshError("provider_invalid")
        normalized.append(dict(model))
    return hashlib.sha256(content).hexdigest(), normalized, content


def _prospective_registry_content(
    registry_models: Sequence[Mapping[str, Any]], *, generated_at: datetime
) -> tuple[bytes, str]:
    """Return the exact canonical bytes and SHA-256 that
    ``publish_scheduler_registry_manifest`` will commit.

    Mirrors the payload shape in
    ``services/orchestrator/scheduler_file_providers.publish_scheduler_registry_manifest``
    so the receipt's `new_registry_sha256` matches the actual on-disk hash.
    """
    payload: dict[str, Any] = {
        "schema_version": REGISTRY_MANIFEST_SCHEMA_VERSION,
        "generated_at": _iso_utc(generated_at),
        "models": [dict(model) for model in registry_models],
    }
    canonical_without_checksum = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    payload["checksum"] = f"sha256:{hashlib.sha256(canonical_without_checksum).hexdigest()}"
    pretty = json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8") + b"\n"
    return pretty, hashlib.sha256(pretty).hexdigest()


def _prospective_registry_generation(
    registry_models: Sequence[Mapping[str, Any]], *, generated_at: datetime
) -> str:
    """Stable identifier for one prospective registry publication.

    Operators observe this value on a refused refresh receipt and file the
    matching cutover declaration.  The generation is a pure content hash of
    the sorted-by-model_id model list (``generated_at`` is intentionally
    excluded from the preimage): the value is byte-for-byte deterministic
    across any wall-clock interval, so the operator's refuse -> declare ->
    retry loop always sees the same generation string as long as the model
    set has not itself drifted.

    ``generated_at`` is still accepted so callers stay symmetric with
    ``_prospective_registry_content``, but the parameter is unused.  Keeping
    the signature stable avoids touching every call site.
    """
    del generated_at  # unused — kept for signature stability
    normalized = [
        {key: value for key, value in dict(model).items()}
        for model in registry_models
    ]
    normalized.sort(key=lambda model: str(model.get("model_id") or ""))
    preimage = json.dumps(
        {"models": normalized},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(preimage).hexdigest()
    return f"manifest-{digest[:12]}"


def _load_cutover_declaration(
    env_path: str | None, *, now: datetime
) -> dict[str, Any] | None:
    """Read/validate a cutover declaration file; return the parsed payload.

    Absent env or empty value returns ``None`` (no declaration).  Any file
    validation failure raises RefreshError(registry_cutover_declaration_invalid).
    """
    if not env_path:
        return None
    path = Path(env_path).expanduser()
    if not path.is_absolute():
        raise RefreshError("registry_cutover_declaration_invalid")
    parent = path.parent
    try:
        content = read_bytes_limited_no_follow(
            path,
            max_bytes=MAX_CUTOVER_DECLARATION_BYTES,
            containment_root=parent,
        )
    except (OSError, SafeFilesystemError) as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    # Sentinel-plus-one: enforce the byte cap explicitly (finding C-F4).
    if len(content) > MAX_CUTOVER_DECLARATION_BYTES:
        raise RefreshError("registry_cutover_declaration_invalid")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    try:
        # Module-level validator (finding C-F3) avoids the per-call
        # metaschema resolution jsonschema.validate performs.
        _CUTOVER_DECLARATION_VALIDATOR.validate(payload)
    except jsonschema.ValidationError as error:
        raise RefreshError("registry_cutover_declaration_invalid") from error
    if not isinstance(payload, dict):
        raise RefreshError("registry_cutover_declaration_invalid")
    entries = payload.get("entries") or []
    seen: set[str] = set()
    for entry in entries:
        model_id = str(entry.get("model_id") or "")
        if model_id in seen:
            raise RefreshError("registry_cutover_declaration_invalid")
        seen.add(model_id)
        try:
            cycle = datetime.fromisoformat(str(entry["effective_cycle_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as error:
            raise RefreshError("registry_cutover_declaration_invalid") from error
        if cycle.tzinfo is None:
            raise RefreshError("registry_cutover_declaration_invalid")
        cycle = cycle.astimezone(UTC)
        if (
            cycle.minute != 0
            or cycle.second != 0
            or cycle.microsecond != 0
            or cycle.hour not in CUTOVER_CYCLE_HOURS
        ):
            raise RefreshError("registry_cutover_declaration_invalid")
        if cycle < now - CUTOVER_PAST_TOLERANCE or cycle > now + CUTOVER_FUTURE_TOLERANCE:
            raise RefreshError("registry_cutover_declaration_invalid")
        if str(entry.get("transition_mode")) not in CUTOVER_TRANSITION_MODES:
            raise RefreshError("registry_cutover_declaration_invalid")
    return payload


# jsonschema is loaded from the vendored file at import time so the gate does
# not touch the filesystem per call.
_CUTOVER_DECLARATION_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "scheduler_registry_package_cutover.schema.json"
)
try:
    _CUTOVER_DECLARATION_SCHEMA = json.loads(
        _CUTOVER_DECLARATION_SCHEMA_PATH.read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError) as _cutover_schema_load_error:  # pragma: no cover
    raise RuntimeError(
        f"cutover declaration schema unavailable: {_cutover_schema_load_error}"
    ) from _cutover_schema_load_error
# Module-level validator: jsonschema.validate() re-resolves the metaschema on
# every call and builds a fresh validator instance.  We hold the validator
# once (finding C-F3) so the gate pays the metaschema hop only at import.
# R2-B6 (round-2 review): attach a FormatChecker with a registered
# ``date-time`` check (the default Draft202012Validator FORMAT_CHECKER only
# ships date-time when ``rfc3339-validator`` is installed).  The check
# mirrors the consumer at ``services/orchestrator/scheduler_generation.py``
# so publisher and consumer accept and reject the same set of values.
_CUTOVER_DECLARATION_FORMAT_CHECKER = jsonschema.FormatChecker()


@_CUTOVER_DECLARATION_FORMAT_CHECKER.checks("date-time", raises=(TypeError, ValueError))
def _cutover_datetime_format_check(value: Any) -> bool:  # pragma: no cover - trivial
    """Return True when ``value`` parses as an aware RFC 3339 date-time."""
    if not isinstance(value, str):
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive datetime not permitted")
    return True


_CUTOVER_DECLARATION_VALIDATOR = jsonschema.Draft202012Validator(
    _CUTOVER_DECLARATION_SCHEMA,
    format_checker=_CUTOVER_DECLARATION_FORMAT_CHECKER,
)


@dataclass
class _RegistryClassification:
    """In-flight classification decision produced by ``_registry_precommit_gate``.

    Populated before the gate raises so the exception handler can still emit
    ``registry_classification`` on a refusal receipt.
    """

    previous_registry_sha256: str | None = None
    new_registry_sha256: str | None = None
    # R2-N1: carry the exact previous/prospective row counts on the
    # classification so ``_enforce_registry_classification_reconciliation``
    # can enforce EQUALITY (not just non-negative bounds) against the
    # partition counts.  A validated on-disk receipt then catches a stale
    # classification whose totals silently drop or gain a row.
    previous_model_count: int | None = None
    prospective_model_count: int = 0
    added: list[str] = dataclass_field(default_factory=list)
    unchanged: list[str] = dataclass_field(default_factory=list)
    removed: list[str] = dataclass_field(default_factory=list)
    package_changed: list[dict[str, str]] = dataclass_field(default_factory=list)
    refused: list[dict[str, Any]] = dataclass_field(default_factory=list)
    declared_cutovers: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def to_receipt(self) -> dict[str, Any]:
        def id_group(values: Sequence[str]) -> dict[str, Any]:
            total = len(values)
            items = list(values)[:MAX_COLLECTION_ITEMS]
            return {"items": items, "total": total, "truncated": total > len(items)}

        def obj_group(values: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            total = len(values)
            items = [dict(item) for item in values[:MAX_COLLECTION_ITEMS]]
            return {"items": items, "total": total, "truncated": total > len(items)}

        return {
            "previous_registry_sha256": self.previous_registry_sha256,
            "new_registry_sha256": self.new_registry_sha256,
            "previous_model_count": self.previous_model_count,
            "prospective_model_count": int(self.prospective_model_count),
            "added": id_group(sorted(self.added)),
            "unchanged": id_group(sorted(self.unchanged)),
            "removed": id_group(sorted(self.removed)),
            "package_changed": obj_group(
                sorted(self.package_changed, key=lambda item: item["model_id"])
            ),
            "refused": obj_group(sorted(self.refused, key=lambda item: item["model_id"])),
            "declared_cutovers": obj_group(
                sorted(self.declared_cutovers, key=lambda item: item["model_id"])
            ),
        }


_MISSING_IDENTITY = object()


def _extract_nested_identity(row: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Traverse ``row`` by ``path`` returning ``_MISSING_IDENTITY`` on any gap.

    Missing top-level or intermediate mapping is materially different from a
    JSON ``null`` value; conflating the two would let a rebuilt profile drop
    its ``source_inventory_checksum`` silently and stay ``unchanged``.
    """
    current: Any = row
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING_IDENTITY
        current = current[key]
    return current


def _rows_have_identical_identity(
    row: Mapping[str, Any], previous_row: Mapping[str, Any]
) -> bool:
    """Return True when ``row`` and ``previous_row`` match on every identity field.

    Compares the flat ``REGISTRY_MODEL_IDENTITY_FIELDS`` plus every nested
    ``(top_level, nested_path)`` pair in ``REGISTRY_MODEL_NESTED_IDENTITY_FIELDS``.
    Any deviation escalates the caller to ``package_changed``.
    """
    for field_name in REGISTRY_MODEL_IDENTITY_FIELDS:
        if row.get(field_name) != previous_row.get(field_name):
            return False
    for top_level, nested_path in REGISTRY_MODEL_NESTED_IDENTITY_FIELDS:
        if _extract_nested_identity(row, (top_level, *nested_path)) != (
            _extract_nested_identity(previous_row, (top_level, *nested_path))
        ):
            return False
    return True


def _classify_registry(
    *,
    previous: Sequence[Mapping[str, Any]] | None,
    prospective: Sequence[Mapping[str, Any]],
    previous_sha256: str | None,
    new_sha256: str | None,
    generation: str,
    declaration: Mapping[str, Any] | None,
    dry_run: bool,
) -> tuple[_RegistryClassification, str | None]:
    """Return (classification, refusal_reason).

    Refusal semantics only apply to real publishes; ``dry_run`` reports the
    id-only shape without failing so operators can preview additions.
    """
    result = _RegistryClassification(
        previous_registry_sha256=previous_sha256,
        new_registry_sha256=None if dry_run else new_sha256,
    )
    prospective_by_id: dict[str, Mapping[str, Any]] = {}
    for row in prospective:
        model_id = str(row.get("model_id") or "")
        if not model_id:
            raise RefreshError("provider_invalid")
        if model_id in prospective_by_id:
            # A duplicate model_id in the prospective set is a data-shape bug
            # upstream; refuse before any canonical replacement.
            raise RefreshError("provider_invalid")
        prospective_by_id[model_id] = row
    previous_by_id: dict[str, Mapping[str, Any]] = {}
    if previous is not None:
        for row in previous:
            model_id = str(row.get("model_id") or "")
            if not model_id:
                raise RefreshError("provider_invalid")
            if model_id in previous_by_id:
                raise RefreshError("provider_invalid")
            previous_by_id[model_id] = row
    # R2-N1: pin the exact partition counts here (not derived from the sum of
    # bucket totals) so the receipt validator can enforce EQUALITY against a
    # tampered classification.
    result.prospective_model_count = len(prospective_by_id)
    result.previous_model_count = (
        None if previous is None else len(previous_by_id)
    )

    # In dry-run mode prospective rows carry only id/basin_id (no checksum),
    # so checksum-based drift cannot be observed and we do a lenient id-only
    # classification.  Operators preview additions this way.
    if dry_run:
        for model_id in prospective_by_id:
            if model_id in previous_by_id:
                result.unchanged.append(model_id)
            else:
                result.added.append(model_id)
        # dry_run does not evaluate removals or refuse; the strict gate only
        # runs on real publish.
        return result, None

    for model_id, row in prospective_by_id.items():
        if model_id not in previous_by_id:
            result.added.append(model_id)
            continue
        previous_row = previous_by_id[model_id]
        if _rows_have_identical_identity(row, previous_row):
            result.unchanged.append(model_id)
        else:
            result.package_changed.append(
                {
                    "model_id": model_id,
                    "old_checksum": str(previous_row.get("package_checksum") or ""),
                    "new_checksum": str(row.get("package_checksum") or ""),
                }
            )
    for model_id in previous_by_id:
        if model_id not in prospective_by_id:
            result.removed.append(model_id)

    declaration_entries: dict[str, Mapping[str, Any]] = {}
    declaration_generation: str | None = None
    if declaration is not None:
        declaration_generation = str(declaration.get("generation") or "")
        for entry in declaration.get("entries") or []:
            declaration_entries[str(entry.get("model_id") or "")] = entry

    # Declaration must bind to this exact prospective generation.  A stale or
    # rebuilt registry cannot accidentally activate a formerly-approved
    # transition.
    generation_matches = (
        declaration is None or declaration_generation == generation
    )

    def refuse_declaration(model_id: str, entry: Mapping[str, Any] | None) -> None:
        result.refused.append(
            {
                "model_id": model_id,
                "old_checksum": str((entry or {}).get("old_checksum") or "") or None,
                "new_checksum": str((entry or {}).get("new_checksum") or "") or None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )

    # 1. Any declaration entry whose model_id is not part of the prospective
    #    registry is invalid — an operator cannot cutover an unknown model.
    unknown_declaration_ids = [
        model_id
        for model_id in declaration_entries
        if model_id not in prospective_by_id
    ]
    declaration_invalid = bool(unknown_declaration_ids) or not generation_matches
    for model_id in unknown_declaration_ids:
        refuse_declaration(model_id, declaration_entries[model_id])
    if not generation_matches:
        # Attach a synthetic marker so operators see the generation mismatch
        # without leaking either canonical string publicly.
        result.refused.append(
            {
                "model_id": "__declaration__",
                "old_checksum": None,
                "new_checksum": None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )

    # 2. For each package_changed row: look for a matching declaration entry
    #    with correct old/new checksums.  Accept it when everything aligns,
    #    otherwise record the specific invalidity mode.
    undeclared: list[Mapping[str, Any]] = []
    for changed in result.package_changed:
        model_id = changed["model_id"]
        entry = declaration_entries.get(model_id)
        if entry is None:
            undeclared.append(changed)
            continue
        old_matches = str(entry.get("old_checksum")) == changed["old_checksum"]
        new_matches = str(entry.get("new_checksum")) == changed["new_checksum"]
        if declaration_invalid or not old_matches or not new_matches:
            declaration_invalid = True
            refuse_declaration(model_id, entry)
            continue
        result.declared_cutovers.append(
            {
                "model_id": model_id,
                "old_checksum": changed["old_checksum"],
                "new_checksum": changed["new_checksum"],
                "effective_cycle_utc": str(entry.get("effective_cycle_utc") or ""),
                "transition_mode": str(entry.get("transition_mode") or ""),
            }
        )

    # 3. Any package_changed row not covered by a valid declaration is
    #    undeclared drift.
    for changed in undeclared:
        result.refused.append(
            {
                "model_id": changed["model_id"],
                "old_checksum": changed["old_checksum"],
                "new_checksum": changed["new_checksum"],
                "reason": "registry_cutover_undeclared",
            }
        )

    # 4. Removals are refused — deliberate decommission is out of scope for
    #    #1080 and must go through a separate declared workflow.
    for model_id in result.removed:
        result.refused.append(
            {
                "model_id": model_id,
                "old_checksum": str(previous_by_id[model_id].get("package_checksum") or "") or None,
                "new_checksum": None,
                "reason": "registry_cutover_removal_refused",
            }
        )

    # Decide the single refusal reason to raise (declaration-invalid takes
    # priority so operators see the schema problem first, then removal, then
    # undeclared drift).
    if declaration_invalid:
        return result, "registry_cutover_declaration_invalid"
    if result.removed:
        return result, "registry_cutover_removal_refused"
    if undeclared:
        return result, "registry_cutover_undeclared"
    return result, None


def _registry_precommit_gate(
    workspace: Path,
    packages: Sequence[Mapping[str, Any]],
    registry_models: Sequence[Mapping[str, Any]],
    *,
    previous_registry_bytes: bytes | None,
    previous_registry_sha256: str | None,
    prospective_generated_at: datetime,
    cutover_declaration_env: str | None,
    dry_run: bool,
    classification_sink: Callable[[dict[str, Any]], None],
    now: datetime | None = None,
) -> None:
    """Precommit gate.

    Order: classification/declaration first (semantic refusals should fail
    fast without paying for workspace enumeration), then workspace/orphan
    bounds.  Classification is delivered to ``classification_sink`` even on
    refusal so the receipt path can attach the payload.
    """
    orphan_items = [item for item in packages if item.get("status") == "published"]

    previous_models: list[dict[str, Any]] | None
    if previous_registry_bytes is None:
        previous_models = None
    else:
        try:
            previous_payload = json.loads(previous_registry_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry could not be parsed.",
                details={"provider_reason": "provider_invalid", "provider_phase": "precommit"},
            ) from error
        raw_models = previous_payload.get("models") if isinstance(previous_payload, Mapping) else None
        if not isinstance(raw_models, list):
            raise SchedulerRegistryPublishError(
                "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
                "Previous canonical registry has no model list.",
                details={"provider_reason": "provider_invalid", "provider_phase": "precommit"},
            )
        previous_models = [dict(row) for row in raw_models]

    prospective_generation = _prospective_registry_generation(
        registry_models, generated_at=prospective_generated_at
    )
    _, new_sha = _prospective_registry_content(
        registry_models, generated_at=prospective_generated_at
    )

    declaration: dict[str, Any] | None = None
    reference_now = now or datetime.now(UTC)
    declaration_load_error: RefreshError | None = None
    try:
        declaration = _load_cutover_declaration(cutover_declaration_env, now=reference_now)
    except RefreshError as error:
        declaration_load_error = error

    classification, refusal_reason = _classify_registry(
        previous=previous_models,
        prospective=registry_models,
        previous_sha256=previous_registry_sha256,
        new_sha256=new_sha,
        generation=prospective_generation,
        declaration=declaration,
        dry_run=dry_run,
    )
    if declaration_load_error is not None:
        # Surface the file-level load failure without needing the operator to
        # inspect provider evidence; treat it as declaration-invalid.
        classification.refused.append(
            {
                "model_id": "__declaration__",
                "old_checksum": None,
                "new_checksum": None,
                "reason": "registry_cutover_declaration_invalid",
            }
        )
        refusal_reason = "registry_cutover_declaration_invalid"

    classification_sink(classification.to_receipt())

    if refusal_reason is not None:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
            "Registry cutover gate refused canonical replacement before commit.",
            details={
                "provider_reason": refusal_reason,
                "provider_phase": "precommit",
                "discovered_total": len(packages),
                "attempted_total": len(packages),
                "created_total": len(orphan_items),
                "packages": [
                    {
                        "status": item.get("status"),
                        "orphan_id": hashlib.sha256(
                            str(item.get("manifest_uri") or "").encode("utf-8")
                        ).hexdigest()[:32],
                    }
                    for item in orphan_items[:MAX_ORPHAN_EVIDENCE]
                ],
            },
        )

    try:
        _enforce_workspace_bounds(workspace)
        if len(orphan_items) > MAX_ORPHANS:
            raise RefreshError("orphan_limit_exceeded")
        if not registry_models or len(registry_models) > MAX_ORPHANS:
            raise RefreshError("orphan_limit_exceeded")
    except RefreshError as error:
        raise SchedulerRegistryPublishError(
            "SCHEDULER_REGISTRY_REFRESH_PRECOMMIT_FAILED",
            "Refresh bounds failed before canonical registry replacement.",
            details={
                "provider_reason": error.reason,
                "provider_phase": "precommit",
                "discovered_total": len(packages),
                "attempted_total": len(packages),
                "created_total": len(orphan_items),
                "packages": [
                    {
                        "status": item.get("status"),
                        "orphan_id": hashlib.sha256(
                            str(item.get("manifest_uri") or "").encode("utf-8")
                        ).hexdigest()[:32],
                    }
                    for item in orphan_items[:MAX_ORPHAN_EVIDENCE]
                ],
            },
        ) from error


def _cleanup_run_workspace(path: Path, identity: os.stat_result, *, containment_root: Path) -> None:
    path.relative_to(containment_root)
    current = os.lstat(path)
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise RefreshError("workspace_limit_exceeded")
    _enforce_workspace_bounds(path)
    shutil.rmtree(path)


def _apply_environment_file(path: Path) -> Callable[[], None]:
    try:
        content = read_bytes_limited_no_follow(path, max_bytes=64 * 1024, containment_root=path.parent)
        text_content = content.decode("utf-8")
    except (OSError, SafeFilesystemError, UnicodeDecodeError) as error:
        raise RefreshError("configuration_invalid") from error
    parsed: dict[str, str] = {}
    for raw_line in text_content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RefreshError("configuration_invalid")
        name, value = line.split("=", 1)
        if (
            re.fullmatch(r"[A-Z][A-Z0-9_]*", name) is None
            or "\x00" in value
            or name in parsed
        ):
            raise RefreshError("configuration_invalid")
        parsed[name] = value
    previous = {name: os.environ.get(name) for name in parsed}
    os.environ.update(parsed)

    def restore() -> None:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    return restore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--recover-emergency", type=Path)
    operation.add_argument("--validate-current-receipt", type=Path)
    parser.add_argument("--env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        restore_environment = _apply_environment_file(args.env_file) if args.env_file is not None else None
        try:
            config = RefreshConfig.from_env()
        finally:
            if restore_environment is not None:
                restore_environment()
        if args.validate_current_receipt is not None:
            receipt = validate_current_receipt(config, args.validate_current_receipt)
        elif args.recover_emergency is not None:
            receipt = reconstruct_primary_receipt(config, args.recover_emergency)
        else:
            receipt = refresh_scheduler_file_providers(config, dry_run=args.dry_run)
    except RefreshError as error:
        print(json.dumps({"status": error.outcome, "reason": error.reason}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["outcome"] in {"dry_run", "published"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
