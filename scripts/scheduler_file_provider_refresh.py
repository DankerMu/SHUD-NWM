#!/usr/bin/env python
"""Refresh all expiring node-22 scheduler file providers without a database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.provider_atomic import ProviderAtomicError, provider_destination_lock
from packages.common.safe_fs import (
    SafeFilesystemError,
    atomic_write_bytes_no_follow,
    ensure_directory_no_follow,
    read_bytes_limited_no_follow,
    verify_directory_no_follow,
)
from packages.common.state_manager import (
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


class RefreshError(RuntimeError):
    def __init__(self, reason: str, *, outcome: str = "failed", phase: str = "precommit") -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASONS else "provider_invalid"
        self.outcome = outcome if outcome in OUTCOMES else "failed"
        self.phase = phase


@dataclass(frozen=True)
class RefreshConfig:
    basins_root: Path
    registry_uri: str
    readiness_uri: str
    state_uri: str
    object_store_root: Path
    object_store_prefix: str
    workspace_root: Path
    receipt_root: Path
    emergency_root: Path
    refresh_lock: Path

    @classmethod
    def from_env(cls) -> RefreshConfig:
        if any(
            os.getenv(name) not in (None, "")
            for name in (
                "DATABASE_URL",
                "PIPELINE_DATABASE_URL",
                "PGHOST",
                "PGPORT",
                "PGDATABASE",
                "PGUSER",
                "PGSERVICE",
                "PGSERVICEFILE",
            )
        ):
            raise RefreshError("configuration_invalid")
        return cls(
            basins_root=_absolute_env_path("NHMS_BASINS_ROOT"),
            registry_uri=_required_env("NHMS_SCHEDULER_REGISTRY_MANIFEST"),
            readiness_uri=_required_env("NHMS_SCHEDULER_CANONICAL_READINESS_INDEX"),
            state_uri=_required_env("NHMS_SCHEDULER_STATE_INDEX"),
            object_store_root=_absolute_env_path("OBJECT_STORE_ROOT"),
            object_store_prefix=_required_env("OBJECT_STORE_PREFIX"),
            workspace_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_WORK_ROOT"),
            receipt_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT"),
            emergency_root=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_EMERGENCY_ROOT"),
            refresh_lock=_absolute_env_path("NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK"),
        )


def refresh_scheduler_file_providers(config: RefreshConfig, *, dry_run: bool) -> dict[str, Any]:
    started = datetime.now(UTC)
    run_id = f"refresh_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}"
    _preflight_config(config)
    run_workspace = config.workspace_root / run_id
    ensure_directory_no_follow(run_workspace, containment_root=config.workspace_root)
    run_workspace_identity = os.lstat(run_workspace)
    emergency_path, emergency_fd = _reserve_emergency_slot(config.emergency_root, run_id)
    receipt: dict[str, Any]
    committed: list[dict[str, Any]] = []
    orphan_paths: list[str] = []
    try:
        with provider_destination_lock(config.refresh_lock, blocking=False):
            registry_preimage = capture_scheduler_provider_preimage(
                config.registry_uri,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                max_bytes=MAX_REGISTRY_MANIFEST_BYTES,
            )
            registry_before = _read_provider_header(
                Path(config.registry_uri),
                containment_root=config.object_store_root,
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
            )
            _enforce_workspace_bounds(run_workspace)
            orphan_paths = sorted(
                f"package:{hashlib.sha256(str(item.get('manifest_uri') or '').encode()).hexdigest()[:32]}"
                for item in registry_result.get("packages", [])
                if isinstance(item, Mapping) and item.get("status") == "published"
            )
            if len(orphan_paths) > MAX_ORPHANS:
                raise RefreshError("orphan_limit_exceeded")
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
        _discard_emergency_slot(emergency_path, emergency_fd)
        emergency_fd = -1
    except (OSError, SafeFilesystemError, ValueError):
        if committed or receipt.get("outcome") == "replace_uncertain":
            if committed:
                receipt = {
                    **receipt,
                    "outcome": "published_receipt_failed",
                    "reason": "primary_receipt_failed",
                    "phase": "receipt",
                }
            try:
                _finalize_emergency_slot(emergency_fd, receipt)
                emergency_fd = -1
            except (OSError, SafeFilesystemError, ValueError) as error:
                raise RefreshError("receipt_channels_failed", outcome="replace_uncertain", phase="receipt") from error
        else:
            _discard_emergency_slot(emergency_path, emergency_fd)
            emergency_fd = -1
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
    _validate_receipt(receipt)
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
            object_store_root=config.object_store_root,
            object_store_prefix=config.object_store_prefix,
            max_bytes=MAX_READINESS_INDEX_BYTES,
        )
        if current.sha256 != expected:
            raise RefreshError("emergency_record_invalid")
    _publish_primary_receipt(config.receipt_root, receipt)
    return receipt


def _preflight_config(config: RefreshConfig) -> None:
    for directory in (config.basins_root, config.object_store_root):
        verify_directory_no_follow(directory)
    _ensure_private_directory(config.refresh_lock.parent)
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
            provider_path.relative_to(config.object_store_root)
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


def _reserve_emergency_slot(root: Path, run_id: str) -> tuple[Path, int]:
    ensure_directory_no_follow(root)
    path = root / f"{run_id}.reserved.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.fchmod(fd, 0o600)
    os.fsync(fd)
    return path, fd


def _finalize_emergency_slot(fd: int, receipt: Mapping[str, Any]) -> None:
    content = _receipt_bytes(receipt)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard_emergency_slot(path: Path, fd: int) -> None:
    try:
        opened = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RefreshError("receipt_channels_failed", outcome="replace_uncertain", phase="receipt")
        path.unlink()
    finally:
        os.close(fd)


def _publish_primary_receipt(root: Path, receipt: Mapping[str, Any]) -> None:
    _validate_receipt(receipt)
    content = _receipt_bytes(receipt)
    history = root / "history"
    ensure_directory_no_follow(history, containment_root=root)
    run_id = str(receipt["run_id"])
    atomic_write_bytes_no_follow(history / f"{run_id}.json", content, containment_root=root, mode=0o600)
    atomic_write_bytes_no_follow(root / "latest.json", content, containment_root=root, mode=0o600)
    files = sorted(
        (item for item in history.iterdir() if item.is_file() and not item.is_symlink()),
        key=lambda item: item.name,
        reverse=True,
    )
    for obsolete in files[MAX_HISTORY:]:
        obsolete.unlink()


def _receipt(
    *,
    run_id: str,
    started: datetime,
    outcome: str,
    reason: str,
    phase: str,
    providers: Sequence[Mapping[str, Any]],
    orphan_paths: Sequence[str] = (),
) -> dict[str, Any]:
    evidence = list(orphan_paths[:MAX_ORPHAN_EVIDENCE])
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
            "total": len(orphan_paths),
            "truncated": len(orphan_paths) > len(evidence),
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


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("receipt_schema_invalid")
    if receipt.get("outcome") not in OUTCOMES or receipt.get("reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if receipt.get("operation_outcome") not in OUTCOMES or receipt.get("operation_reason") not in REASONS:
        raise ValueError("receipt_enum_invalid")
    if len(str(receipt.get("reason"))) > 64 or receipt.get("database_free") is not True:
        raise ValueError("receipt_contract_invalid")
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
                digest = str(value)
                if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                    raise ValueError("receipt_provider_invalid")
        if not isinstance(provider.get("entry_count"), int) or int(provider["entry_count"]) < 0:
            raise ValueError("receipt_provider_invalid")
    if len(names) != len(set(names)):
        raise ValueError("receipt_provider_invalid")
    if receipt.get("outcome") in {"dry_run", "published"} and names != ["registry", "readiness", "state"]:
        raise ValueError("receipt_provider_invalid")
    if not isinstance(residues, list) or len(residues) > MAX_RESIDUES:
        raise ValueError("receipt_residue_limit")
    if any(Path(str(item)).is_absolute() or ".." in Path(str(item)).parts for item in residues):
        raise ValueError("receipt_residue_unsafe")
    if not isinstance(orphans, Mapping):
        raise ValueError("receipt_orphan_invalid")
    orphan_items = orphans.get("items")
    orphan_total = orphans.get("total")
    if (
        not isinstance(orphan_items, list)
        or len(orphan_items) > MAX_ORPHAN_EVIDENCE
        or not isinstance(orphan_total, int)
        or orphan_total > MAX_ORPHANS
        or orphan_total < len(orphan_items)
        or orphans.get("truncated") is not (orphan_total > len(orphan_items))
    ):
        raise ValueError("receipt_orphan_limit")
    _validate_value_bounds(receipt)


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
    total_bytes = 0
    total_entries = 0
    base_depth = len(root.parts)
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if len(current_path.parts) - base_depth > MAX_WORKSPACE_DEPTH:
            raise RefreshError("workspace_limit_exceeded")
        total_entries += len(directories) + len(files)
        if total_entries > MAX_WORKSPACE_ENTRIES:
            raise RefreshError("workspace_limit_exceeded")
        for name in directories + files:
            metadata = os.lstat(current_path / name)
            if stat.S_ISLNK(metadata.st_mode):
                raise RefreshError("workspace_limit_exceeded")
            if stat.S_ISREG(metadata.st_mode):
                total_bytes += metadata.st_size
                if total_bytes > MAX_WORKSPACE_BYTES:
                    raise RefreshError("workspace_limit_exceeded")


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
                "packages": [
                    {
                        "status": item.get("status"),
                        "orphan_id": hashlib.sha256(
                            str(item.get("manifest_uri") or "").encode("utf-8")
                        ).hexdigest()[:32],
                    }
                    for item in orphan_items[:MAX_ORPHANS]
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recover-emergency", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = RefreshConfig.from_env()
        receipt = (
            reconstruct_primary_receipt(config, args.recover_emergency)
            if args.recover_emergency is not None
            else refresh_scheduler_file_providers(config, dry_run=args.dry_run)
        )
    except RefreshError as error:
        print(json.dumps({"status": error.outcome, "reason": error.reason}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["outcome"] in {"dry_run", "published"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
