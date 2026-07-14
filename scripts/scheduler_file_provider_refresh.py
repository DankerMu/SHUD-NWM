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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from packages.common.libpq_env import LIBPQ_CONNECTION_ENV_KEYS
from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock
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
    load_canonical_readiness_entries_for_renewal,
    publish_canonical_readiness_index,
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
    }
)
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
        )


@dataclass
class EmergencySlot:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int


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
    orphan_paths: list[str] = []
    orphan_total = 0
    orphan_discovered_total = 0
    orphan_attempted_total = 0
    try:
        with provider_destination_lock(config.refresh_lock, blocking=False):
            registry_preimage = capture_scheduler_provider_preimage(
                config.registry_uri,
                object_store_root=config.provider_store_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            # Registry renewal is rebuilt independently from Basins.  The
            # header is evidence only; the preimage captured first remains the
            # commit CAS, so a concurrent registry generation cannot be lost.
            registry_before = _read_provider_header(
                Path(config.registry_uri),
                containment_root=config.provider_store_root,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            readiness_entries, readiness_before, readiness_preimage = (
                load_canonical_readiness_entries_for_renewal(
                    config.readiness_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                )
            )
            state_repository = FileStateSnapshotIndexRepository(
                index_uri=config.state_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
            )
            state_entries, state_before, state_preimage = state_repository.validated_entries_for_renewal()
            registry_result = publish_all_basin_scheduler_registry(
                basins_root=config.basins_root,
                registry_manifest=config.registry_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                work_dir=run_workspace / "registry",
                dry_run=dry_run,
                expected_preimage=registry_preimage,
                precommit_validator=_registry_precommit_gate,
                resource_validator=_enforce_workspace_bounds,
                workspace_budget=workspace_budget,
                max_contexts=MAX_ORPHANS,
            )
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
                _provider_evidence(
                    "readiness", {**readiness_preimage.to_dict(), **readiness_before}, readiness_before
                ),
                _provider_evidence("state", {**state_preimage.to_dict(), **state_before}, state_before),
            ]
            if dry_run:
                for provider in provider_evidence:
                    provider["after_sha256"] = provider["before_sha256"]
                    provider["after_schema_version"] = provider["before_schema_version"]
                    provider["after_generated_at"] = provider["before_generated_at"]
                    provider["after_payload_checksum"] = provider["before_payload_checksum"]
            if not dry_run:
                committed.append(provider_evidence[0])
                readiness_result = publish_canonical_readiness_index(
                    readiness_entries,
                    config.readiness_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    expected_preimage=readiness_preimage,
                    verify_external_references=True,
                )
                provider_evidence[1] = _provider_evidence(
                    "readiness", {**readiness_preimage.to_dict(), **readiness_before}, readiness_result
                )
                committed.append(provider_evidence[1])
                state_result = publish_state_snapshot_index(
                    state_entries,
                    config.state_uri,
                    object_store_root=config.object_store_root,
                    object_store_prefix=config.object_store_prefix,
                    expected_preimage=state_preimage,
                )
                provider_evidence[2] = _provider_evidence(
                    "state", {**state_preimage.to_dict(), **state_before}, state_result
                )
                committed.append(provider_evidence[2])
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
            )
    except ProviderAtomicError as error:
        receipt = _receipt(
            run_id=run_id,
            started=started,
            outcome="already_running" if error.reason == "provider_already_running" else "failed",
            reason="refresh_already_running" if error.reason == "provider_already_running" else "provider_invalid",
            phase=error.phase,
            providers=committed,
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
        if reason == "provider_preimage_changed":
            reason = "provider_preimage_changed"
        elif reason == "provider_restored_previous":
            outcome, reason = "restored_previous", "provider_postread_failed"
        elif reason == "provider_replace_uncertain":
            outcome, reason = "replace_uncertain", "provider_replace_uncertain"
        elif reason not in REASONS:
            reason = "provider_invalid"
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
        )
    except Exception:
        receipt = _receipt(
            run_id=run_id,
            started=started,
            outcome="failed",
            reason="provider_invalid",
            phase="precommit",
            providers=committed,
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
            if committed:
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
    for provider in receipt.get("providers", []):
        uri = {
            "registry": config.registry_uri,
            "readiness": config.readiness_uri,
            "state": config.state_uri,
        }.get(provider.get("name"))
        expected = provider.get("after_sha256")
        if uri is None or not expected:
            raise RefreshError("emergency_record_invalid")
        current = capture_scheduler_provider_preimage(
            uri,
            object_store_root=config.provider_store_root,
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
    provider_contract = (
        ("registry", config.registry_uri, MAX_REGISTRY_MANIFEST_BYTES),
        ("readiness", config.readiness_uri, MAX_READINESS_INDEX_BYTES),
        ("state", config.state_uri, MAX_STATE_SNAPSHOT_INDEX_BYTES),
    )
    providers = receipt.get("providers")
    if not isinstance(providers, list) or len(providers) != len(provider_contract):
        raise RefreshError("emergency_record_invalid", phase="receipt")
    for provider, (name, uri, max_bytes) in zip(providers, provider_contract, strict=True):
        if provider.get("name") != name:
            raise RefreshError("emergency_record_invalid", phase="receipt")
        try:
            current = capture_scheduler_provider_preimage(
                uri,
                object_store_root=config.provider_store_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=max_bytes,
            )
        except SchedulerFileProviderError as error:
            raise RefreshError("emergency_record_invalid", phase="receipt") from error
        if not current.exists or current.sha256 != provider.get("after_sha256"):
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
    if not config.object_store_prefix.startswith("s3://"):
        raise RefreshError("configuration_invalid")


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
            latest = _validate_receipt(
                json.loads(
                    read_bytes_limited_no_follow(
                        latest_path,
                        max_bytes=MAX_RECEIPT_BYTES,
                        containment_root=root,
                    )
                )
            )
            replace_latest = _receipt_order(canonical) >= _receipt_order(latest)
        except FileNotFoundError:
            pass
        if replace_latest:
            atomic_write_bytes_no_follow(latest_path, content, containment_root=root, mode=0o600)
        files: list[tuple[tuple[datetime, str], Path]] = []
        for item in history.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            try:
                historical = _validate_receipt(
                    json.loads(
                        read_bytes_limited_no_follow(
                            item,
                            max_bytes=MAX_RECEIPT_BYTES,
                            containment_root=root,
                        )
                    )
                )
                order = _receipt_order(historical)
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
) -> dict[str, Any]:
    evidence = list(orphan_paths[:MAX_ORPHAN_EVIDENCE])
    total = len(orphan_paths) if orphan_total is None else orphan_total
    discovered_total = total if orphan_discovered_total is None else orphan_discovered_total
    attempted_total = total if orphan_attempted_total is None else orphan_attempted_total
    return {
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
            result_map.get("model_count")
            or result_map.get("entry_count")
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
    if not isinstance(receipt, Mapping) or set(receipt) != RECEIPT_KEYS:
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
        if name not in {"registry", "readiness", "state"}:
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
    if receipt.get("outcome") in {"dry_run", "published"} and names != ["registry", "readiness", "state"]:
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
    _validate_value_bounds(receipt)
    return json.loads(json.dumps(receipt, ensure_ascii=True))


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


def _registry_precommit_gate(workspace: Path, packages: Sequence[Mapping[str, Any]]) -> None:
    orphan_items = [item for item in packages if item.get("status") == "published"]
    try:
        _enforce_workspace_bounds(workspace)
        if len(orphan_items) > MAX_ORPHANS:
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
