"""The refresh runner: the full provider refresh transaction.

Split out of ``scripts/scheduler_file_provider_refresh.py`` by #1099; the
historical module remains the executable entrypoint and attribute facade.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from packages.common.provider_atomic import ProviderAtomicError, ProviderPreimage, provider_destination_lock
from packages.common.safe_fs import SafeFilesystemError, ensure_directory_no_follow, read_bytes_limited_no_follow
from packages.common.state_manager import (
    MAX_STATE_SNAPSHOT_INDEX_BYTES,
    FileStateSnapshotIndexRepository,
    StateManagerError,
    publish_state_snapshot_index,
)
from scripts.publish_scheduler_file_registry import SchedulerRegistryPublishError, publish_all_basin_scheduler_registry
from scripts.scheduler_refresh.config import (
    RefreshConfig,
    _cleanup_run_workspace,
    _enforce_workspace_bounds,
    _env_flag,
    _preflight_config,
    _ProviderRollbackRecord,
    _WorkspaceBudget,
)
from scripts.scheduler_refresh.constants import (
    CALIBRATION_OVERRIDE_REFUSAL_REASON,
    CUTOVER_DECLARATION_ENV,
    MAX_ORPHANS,
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_DEPTH,
    MAX_WORKSPACE_ENTRIES,
    REASONS,
    RefreshError,
)
from scripts.scheduler_refresh.cutover_declaration import (
    _cutover_declaration_env_resolves_to_file,
    _load_previous_canonical_registry,
)
from scripts.scheduler_refresh.precommit_gate import _registry_precommit_gate
from scripts.scheduler_refresh.providers import _rollback_provider_transaction, _tracked_provider_publish
from scripts.scheduler_refresh.receipt import (
    _discard_emergency_slot,
    _finalize_emergency_slot,
    _provider_evidence,
    _publish_primary_receipt,
    _read_provider_header,
    _receipt,
    _reserve_emergency_slot,
)
from scripts.scheduler_refresh.receipt_validation import (
    _calibration_overrides_audit_from_error,
    _calibration_overrides_audit_from_summary,
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
from workers.model_registry.basins_calibration_overrides import CalibrationOverrideError


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
    # Bound only once the registry publish path constructs the audit block;
    # receipts built before that (lock contention, provider-preimage failures)
    # pass None and omit the key.
    runner_cutover_gate_audit: dict[str, Any] | None = None
    # #1832: what the declared calibration overrides did on this run.  Stays
    # None until the publisher either returns a summary that ran them or raises.
    calibration_overrides_audit: dict[str, Any] | None = None
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
                cutover_gate=runner_cutover_gate_audit,
            )
        providers = committed
        if restored:
            # #2297: the rollback was verified (every record's path is back to
            # `record.previous` by sha256) but another lane's outcome is unknown,
            # so the receipt stays `replace_uncertain`.  Its after_* must still
            # describe the bytes on disk, not the generation that was published
            # and rolled back -- the same substitution the dry-run lane makes.
            # `entry_count` keeps the attempted generation's count, as dry-run.
            restored_names = {record.name for record in rollback_stack}
            providers = [
                {
                    **provider,
                    "after_sha256": provider["before_sha256"],
                    "after_schema_version": provider["before_schema_version"],
                    "after_generated_at": provider["before_generated_at"],
                    "after_payload_checksum": provider["before_payload_checksum"],
                }
                if provider.get("name") in restored_names
                else provider
                for provider in committed
            ]
        return _receipt(
            run_id=run_id,
            started=started,
            outcome="replace_uncertain",
            reason="provider_replace_uncertain",
            phase="postcommit",
            providers=providers,
            orphan_paths=orphan_paths,
            orphan_total=orphan_total,
            orphan_discovered_total=orphan_discovered_total,
            orphan_attempted_total=orphan_attempted_total,
            registry_classification=registry_classification,
            cutover_gate=runner_cutover_gate_audit,
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

            # #1433: bulk publish drops models it cannot publish; when such a
            # model is already canonical the gate sees a removal and has to say
            # why the row went missing.  The publisher feeds those rows here
            # before it calls the precommit callback below.
            skipped_models: dict[str, Mapping[str, Any]] = {}

            def _skipped_model_sink(rows: Mapping[str, Mapping[str, Any]]) -> None:
                skipped_models.update(rows)

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
                    skipped_models=skipped_models,
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
                    calibration_overrides_path=config.calibration_overrides_path,
                    cutover_gate=runner_cutover_gate_audit,
                    skipped_model_sink=_skipped_model_sink,
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
            calibration_overrides_audit = _calibration_overrides_audit_from_summary(
                registry_result, declaration_path=config.calibration_overrides_path
            )
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
                # #1926: under dry_run the mirror publisher never runs, so
                # `worker_registry_result` is still None and the shared
                # `_provider_evidence` coalescing chain would bottom its
                # `entry_count` out at 0 -- which `_validate_receipt`'s
                # registry/mirror equality check then rejects as
                # `receipt_provider_invalid`, folded by the caller to
                # `primary_receipt_failed`.  The mirror is a byte-copy of the
                # canonical registry, so its prospective count IS the
                # registry's: take it from the registry evidence just built,
                # never from the before-image and never from a literal.  The
                # `not dry_run` lane keeps passing the real publisher result
                # unchanged, including the postcommit sha equality check below.
                worker_registry_evidence: Any = (
                    {"entry_count": provider_evidence[0]["entry_count"]}
                    if dry_run
                    else worker_registry_result
                )
                worker_evidence = _provider_evidence(
                    "registry_worker_mirror",
                    {**(worker_registry_preimage or ProviderPreimage(False)).to_dict(), **worker_registry_before},
                    worker_registry_evidence,
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
                cutover_gate=runner_cutover_gate_audit,
                calibration_overrides=calibration_overrides_audit,
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
            cutover_gate=runner_cutover_gate_audit,
        )
    except (
        RefreshError,
        SchedulerRegistryPublishError,
        SchedulerFileProviderError,
        StateManagerError,
        # #1832 round-2 C1: `CalibrationOverrideError` is a bare `RuntimeError`
        # subclass and was in none of these tuples, so it landed on the generic
        # handler below and lost its error code, message and offending entry.
        CalibrationOverrideError,
    ) as error:
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
        if isinstance(error, CalibrationOverrideError):
            # The raw `CALIBRATION_OVERRIDE_*` code is not a receipt reason, and
            # the clamp below would otherwise reset it to `provider_invalid`.
            # It travels in the block instead, with the offending entry.
            calibration_overrides_audit = _calibration_overrides_audit_from_error(
                error, declaration_path=config.calibration_overrides_path
            )
            reason = CALIBRATION_OVERRIDE_REFUSAL_REASON
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
                cutover_gate=runner_cutover_gate_audit,
                calibration_overrides=calibration_overrides_audit,
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
            cutover_gate=runner_cutover_gate_audit,
            calibration_overrides=calibration_overrides_audit,
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
