"""Provider publish/rollback transactions and the receipt recovery entrypoints.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from packages.common.provider_atomic import (
    ProviderAtomicError,
    ProviderPreimage,
    atomic_replace_provider_bytes,
    provider_destination_lock,
)
from packages.common.safe_fs import SafeFilesystemError, open_directory_no_follow, read_bytes_limited_no_follow
from packages.common.state_manager import MAX_STATE_SNAPSHOT_INDEX_BYTES
from scripts.scheduler_refresh.config import RefreshConfig, _provider_failure_reason, _ProviderRollbackRecord
from scripts.scheduler_refresh.constants import MAX_RECEIPT_BYTES, RefreshError
from scripts.scheduler_refresh.receipt import _publish_primary_receipt
from scripts.scheduler_refresh.receipt_validation import _validate_receipt
from services.orchestrator.scheduler_file_providers import (
    MAX_READINESS_INDEX_BYTES,
    MAX_REGISTRY_MANIFEST_BYTES,
    SchedulerFileProviderError,
    capture_scheduler_provider_preimage,
)


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
