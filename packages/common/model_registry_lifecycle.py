"""Model lifecycle methods of ``PsycopgModelRegistryStore``: operation
preflight, the activate / deactivate / switch / rollback / supersede /
deprecate transaction, and the state transition itself (#2617 split of
``packages.common.model_registry``).

A plain mixin: no fields and no dunders. ``PsycopgModelRegistryStore`` in
``packages.common.model_registry`` is the only class that composes it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from packages.common.auth_policy import (
    PolicyDecision,
    redact_audit_payload,
    require_policy_evidence,
    trusted_internal_policy_decision,
)
from packages.common.model_registry_contracts import (
    MODEL_LIFECYCLE_ACTIONS,
    ColdStartApprovalInput,
    InvalidPayloadError,
    MissingResourceError,
    ModelActivationContext,
    ModelLifecycleAuditPersistenceError,
    ModelLifecycleOperation,
    ModelLifecycleState,
    ModelRegistryError,
    PostCommitPublishContext,
    _build_activation_result_approval_block,
    _classify_forcing_mapping_mode,
    _extract_source_scope,
    _json_mapping,
    _should_publish_manifest_after_commit,
    _would_be_already_current,
)
from packages.common.model_registry_preflight_rules import (
    _activation_safety_evidence,
    _apply_idempotent_rollback_preflight,
    _canonical_lifecycle_state,
    _lifecycle_audit_persistence_failure_result,
    _object_uri_prefix_status,
    _preflight_blocker,
    _rollback_history_preflight_reference,
    _transition_blocker,
)
from packages.common.model_registry_public import (
    REDACTED_REASON,
    _first_non_empty,
    _model_public_projection,
    _sanitize_audit_uri,
)


class _ModelLifecycleMixin:
    """Lifecycle preflight, operation transaction and state transition."""

    def preflight_model_operation(
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: PolicyDecision | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if operation not in MODEL_LIFECYCLE_ACTIONS:
            raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")
        action_id = MODEL_LIFECYCLE_ACTIONS[operation]
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_instance",
            target_id=model_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)
        request_id = request_id or str(uuid4())
        with self._transaction() as cursor:
            model = self._fetch_model_lifecycle_row(cursor, model_id, for_update=False)
            if model is None:
                raise MissingResourceError(f"model_id not found: {model_id}")
            active = self._fetch_active_model_for_scope(cursor, str(model["basin_version_id"]), for_update=False)
            previous = (
                self._fetch_model_lifecycle_row(cursor, previous_model_id, for_update=False)
                if previous_model_id is not None
                else None
            )
            if previous_model_id is not None and previous is None:
                raise MissingResourceError(f"model_id not found: {previous_model_id}")
            history = self._fetch_trustworthy_rollback_history(
                cursor,
                current_model=model,
                previous_model_id=previous_model_id,
            )
            idempotent_rollback_history = (
                self._fetch_idempotent_rollback_retry_history(
                    cursor,
                    model=model,
                    current_active=active,
                    previous_model_id=previous_model_id,
                )
                if operation == "rollback_version"
                else None
            )
            # §3.1 (Epic #961) legacy-reactivation guard: fetch direct-grid
            # activation history for the scope only when the operation is
            # activation-class (the only path the guard scopes to).
            direct_grid_history = (
                self._fetch_direct_grid_activation_history(
                    cursor,
                    basin_version_id=str(model["basin_version_id"]),
                    current_active=active,
                )
                if operation in {"activate", "switch_version", "rollback_version"}
                else None
            )
        preflight = self._build_model_operation_preflight(
            model=model,
            current_active=active,
            operation=operation,
            action_id=action_id,
            actor_id=decision.actor_id,
            request_id=request_id,
            previous_model=previous,
            rollback_history=history,
            override_missing_active=override_missing_active,
            reason=reason,
            actor_roles=decision.roles,
            direct_grid_history=direct_grid_history,
        )
        if idempotent_rollback_history is not None:
            _apply_idempotent_rollback_preflight(preflight, idempotent_rollback_history)
        return preflight

    def model_lifecycle_operation(
        self,
        model_id: str,
        *,
        operation: ModelLifecycleOperation,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
        request_id: str | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        cold_start_approval: ColdStartApprovalInput | None = None,
    ) -> dict[str, Any]:
        if operation not in MODEL_LIFECYCLE_ACTIONS:
            raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")
        action_id = MODEL_LIFECYCLE_ACTIONS[operation]
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                action_id,
                target_type="model_instance",
                target_id=model_id,
                actor_id="trusted-internal:model-registry",
                roles=("sys_admin",),
            )
            if operation == "deactivate":
                override_missing_active = True
                reason = reason or "trusted internal legacy deactivation"
        request_id = request_id or str(uuid4())
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_instance",
            target_id=model_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)

        # §2.2 (Epic #961) post-commit manifest re-publish trigger.
        # ``publish_context`` is set inside the transaction ONLY on the
        # committed successful-transition path. Blocked/already-current/
        # hook-aborted paths either early-return before setting it or
        # roll the transaction back before setting it, so the publisher
        # never fires on a non-committed transaction. The publisher is
        # invoked AFTER the ``with self._transaction()`` block exits
        # successfully (below the try) so the manifest reflects the
        # committed DB state, matching the tasks.md §2.2 "uniform
        # post-commit tail" contract.
        publish_context: PostCommitPublishContext | None = None

        # SUB-5 task 3.2: hoisted so the outside-tx state-clone refusal
        # handler can read the target model / basin scope after the
        # lifecycle transaction has rolled back.
        activation_context: ModelActivationContext | None = None

        # Local import to avoid a module-level cycle: ``state_clone_hook``
        # imports :class:`ColdStartApprovalInput` and
        # :class:`ModelActivationContext` from this module, so importing
        # the hook at module scope would deadlock. The specific exception
        # type is stable and light — resolving it once per call is fine.
        from packages.common.state_clone_hook import StateCloneCutoverRefusedError

        try:
            with self._transaction() as cursor:
                unlocked_model = self._fetch_model_lifecycle_row(cursor, model_id, for_update=False)
                if unlocked_model is None:
                    raise MissingResourceError(f"model_id not found: {model_id}")
                self._lock_basin_version_scope(cursor, str(unlocked_model["basin_version_id"]))
                unlocked_current_active = self._fetch_active_model_for_scope(
                    cursor,
                    str(unlocked_model["basin_version_id"]),
                    for_update=False,
                )
                unlocked_previous = (
                    self._fetch_model_lifecycle_row(cursor, previous_model_id, for_update=False)
                    if previous_model_id is not None
                    else None
                )
                if previous_model_id is not None and unlocked_previous is None:
                    raise MissingResourceError(f"model_id not found: {previous_model_id}")
                lock_ids = {
                    str(unlocked_model["model_id"]),
                    *(
                        [str(unlocked_current_active["model_id"])]
                        if unlocked_current_active is not None
                        else []
                    ),
                    *([str(unlocked_previous["model_id"])] if unlocked_previous is not None else []),
                }
                locked_rows: dict[str, dict[str, Any]] = {}
                for locked_model_id in sorted(lock_ids):
                    locked = self._fetch_model_lifecycle_row(cursor, locked_model_id, for_update=True)
                    if locked is None:
                        raise MissingResourceError(f"model_id not found: {locked_model_id}")
                    locked_rows[locked_model_id] = locked
                model = locked_rows[str(unlocked_model["model_id"])]
                current_active = (
                    locked_rows.get(str(unlocked_current_active["model_id"]))
                    if unlocked_current_active is not None
                    else None
                )
                previous = (
                    locked_rows.get(str(unlocked_previous["model_id"])) if unlocked_previous is not None else None
                )
                rollback_history = self._fetch_trustworthy_rollback_history(
                    cursor,
                    current_model=model,
                    previous_model_id=previous_model_id,
                )
                idempotent_rollback_history = (
                    self._fetch_idempotent_rollback_retry_history(
                        cursor,
                        model=model,
                        current_active=current_active,
                        previous_model_id=previous_model_id,
                    )
                    if operation == "rollback_version"
                    else None
                )
                # §3.1 (Epic #961) legacy-reactivation guard: fetch direct-grid
                # activation history for the scope only when the operation is
                # activation-class (the only path the guard scopes to).
                direct_grid_history = (
                    self._fetch_direct_grid_activation_history(
                        cursor,
                        basin_version_id=str(model["basin_version_id"]),
                        current_active=current_active,
                    )
                    if operation in {"activate", "switch_version", "rollback_version"}
                    else None
                )
                preflight = self._build_model_operation_preflight(
                    model=model,
                    current_active=current_active,
                    operation=operation,
                    action_id=action_id,
                    actor_id=decision.actor_id,
                    request_id=request_id,
                    previous_model=previous,
                    rollback_history=rollback_history,
                    override_missing_active=override_missing_active,
                    reason=reason,
                    actor_roles=decision.roles,
                    direct_grid_history=direct_grid_history,
                )
                if idempotent_rollback_history is not None:
                    _apply_idempotent_rollback_preflight(preflight, idempotent_rollback_history)
                    return {
                        "status": "already_current",
                        "operation": operation,
                        "model": _model_public_projection(current_active),
                        "previous_model": _model_public_projection(model),
                        "preflight": preflight,
                        "audit_reference": None,
                    }
                if preflight["status"] == "blocked":
                    try:
                        audit_id = self._insert_model_lifecycle_audit(
                            cursor,
                            model=model,
                            updated=model,
                            operation=operation,
                            outcome="blocked",
                            policy_decision=decision,
                            request_id=request_id,
                            preflight=preflight,
                            previous_model=current_active,
                            reason=reason,
                        )
                    except Exception as audit_error:
                        raise ModelLifecycleAuditPersistenceError(
                            _lifecycle_audit_persistence_failure_result(
                                model=model,
                                current_active=current_active,
                                operation=operation,
                                preflight=preflight,
                            ),
                            audit_error,
                        ) from audit_error
                    return {
                        "status": "blocked",
                        "operation": operation,
                        "model": _model_public_projection(model),
                        "preflight": preflight,
                        "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": audit_id},
                    }

                # §2.1: run the ordered pre-activation hook chain inside
                # the same transaction, BEFORE the supersede+activate
                # swap, so a raising hook rolls back both the swap and
                # the audit-row insert (fail-closed, D7). Hooks fire only
                # for the three activation-class operations that produce
                # a real swap; ``deactivate``/``supersede``/``deprecate``
                # skip hooks because no clone/flip target exists.
                #
                # Additional gate: skip when the target is already
                # current — that path returns ``already_current`` from
                # ``_apply_model_lifecycle_transition`` with no state
                # mutation (mirrors the check at
                # ``_apply_model_lifecycle_transition:2266-2268``), so
                # there is nothing for a clone/flip hook to act on.
                # The ``rollback_version`` already-current path is the
                # idempotent-rollback-retry short-circuit above, which
                # returned before reaching this point.
                if operation in {"activate", "switch_version", "rollback_version"} and not _would_be_already_current(
                    model, operation
                ):
                    hook_target = previous if operation == "rollback_version" else model
                    if hook_target is None:
                        # rollback_version without a previous model is a
                        # preflight blocker (ROLLBACK_HISTORY_MISSING),
                        # so this branch is unreachable. Guard for the
                        # invariant so a future preflight refactor cannot
                        # silently pass None into a hook.
                        raise InvalidPayloadError(
                            "rollback_version reached the hook dispatch site with no previous_model."
                        )
                    activation_context = ModelActivationContext(
                        basin_version_id=str(hook_target["basin_version_id"]),
                        previous_active_model=(dict(current_active) if current_active is not None else None),
                        target_model=dict(hook_target),
                        source_scope=_extract_source_scope(hook_target),
                        cold_start_approval=cold_start_approval,
                    )
                    self._dispatch_pre_activation_hooks(cursor, activation_context)

                transition = self._apply_model_lifecycle_transition(
                    cursor,
                    model=model,
                    current_active=current_active,
                    operation=operation,
                    previous_model=previous,
                )
                try:
                    audit_id = self._insert_model_lifecycle_audit(
                        cursor,
                        model=model,
                        updated=transition["model"],
                        operation=operation,
                        outcome=transition["outcome"],
                        policy_decision=decision,
                        request_id=request_id,
                        preflight=preflight,
                        previous_model=transition.get("previous_model"),
                        reason=reason,
                    )
                except Exception as audit_error:
                    raise ModelLifecycleAuditPersistenceError(
                        _lifecycle_audit_persistence_failure_result(
                            model=model,
                            current_active=current_active,
                            operation=operation,
                            preflight=preflight,
                        ),
                        audit_error,
                    ) from audit_error
                # §2.2: stage the post-commit manifest re-publish
                # AFTER audit persistence has succeeded but BEFORE the
                # ``with self._transaction()`` block exits, so a raised
                # audit-persistence error rolls back the transaction and
                # skips publish (``publish_context`` stays None). The
                # actual publisher call runs below the try/except, after
                # the transaction commits.
                if _should_publish_manifest_after_commit(
                    operation=operation,
                    transition_outcome=transition["outcome"],
                    model=model,
                    current_active_before=current_active,
                ):
                    publish_context = PostCommitPublishContext(
                        basin_version_id=str(model["basin_version_id"]),
                        target_model_id=str(transition["model"]["model_id"]),
                        source_scope=_extract_source_scope(transition["model"]),
                        operation_type=operation,
                    )
                result: dict[str, Any] = {
                    "status": transition["outcome"],
                    "operation": operation,
                    "model": _model_public_projection(transition["model"]),
                    "previous_model": (
                        _model_public_projection(transition["previous_model"])
                        if transition.get("previous_model")
                        else None
                    ),
                    "preflight": preflight,
                    "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": audit_id},
                }
                # SUB-5 task 3.2: if an explicit cold-start approval
                # covered any source actually in scope, surface the
                # approval + spin-up-distortion-announcement obligation
                # marker on the activation result so the operator sees
                # the same obligation clause that landed on the audit
                # record inside the hook.
                approval_block = _build_activation_result_approval_block(
                    activation_context
                )
                if approval_block is not None:
                    result["cold_start_approval"] = approval_block
        except ModelLifecycleAuditPersistenceError as error:
            return error.result
        except StateCloneCutoverRefusedError as refusal:
            # SUB-5 task 3.2: the state-clone hook refused a source
            # without an approval covering it. The lifecycle transaction
            # has already rolled back on the raised exception (via the
            # ``_PsycopgTransaction.__exit__`` path), so the refusal
            # audit record is written on a FRESH transaction below —
            # PostgreSQL does not support autonomous transactions, so an
            # in-tx write would have been discarded along with the
            # supersede + activate swap.
            if activation_context is None:  # pragma: no cover - defensive
                # A refusal can only be raised from
                # ``_dispatch_pre_activation_hooks``, which runs after
                # ``activation_context`` is bound. Guard defensively so
                # a future refactor cannot silently strip the invariant.
                raise
            return self._record_state_clone_refusal_audit(
                activation_context=activation_context,
                refusal=refusal,
                policy_decision=decision,
                request_id=request_id,
                operation=operation,
                preflight=preflight,
            )

        # §2.2 + SUB-6 (§3.3) post-commit tail: fire publishers ONLY on
        # a committed dispatch-set-changing transition. All non-
        # committing paths (preflight-blocked / already-current / hook-
        # aborted / audit-persistence-failed) either early-returned
        # above or left ``publish_context`` as None because the trigger
        # was set only right before the successful return.
        #
        # Ordering invariant (D7 fact anchor A-i): the state-index
        # publisher runs FIRST. A raise here holds back the manifest re-
        # publish — the previous manifest remains the compute-plane
        # authority (node-22, DB-free) so node-22 never observes
        # ``M1``-active in the manifest without ``M1``'s successor
        # checkpoint on the file state index. The manifest publisher
        # runs SECOND only after the state-index publisher returns
        # without raising.
        if publish_context is not None:
            self._dispatch_post_commit_state_index_publish(publish_context)
            self._dispatch_post_commit_manifest_publish(publish_context)
        return result

    def _build_model_operation_preflight(
        self,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        operation: ModelLifecycleOperation,
        action_id: str,
        actor_id: str,
        request_id: str,
        previous_model: Mapping[str, Any] | None,
        rollback_history: Mapping[str, Any] | None,
        override_missing_active: bool,
        reason: str | None,
        actor_roles: Sequence[str],
        direct_grid_history: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        restored_model = previous_model if operation == "rollback_version" else model
        resource_profile = _json_mapping(restored_model.get("resource_profile")) if restored_model else {}
        mesh_properties = _json_mapping(restored_model.get("mesh_properties_json")) if restored_model else {}
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        lifecycle_state = _canonical_lifecycle_state(model)
        activation_class_operation = operation in {"activate", "switch_version", "rollback_version"}

        copied_root = "missing"
        package_checksum = None
        if restored_model is not None:
            activation_blockers, activation_warnings, copied_root, package_checksum = _activation_safety_evidence(
                restored_model,
                activation_class_operation=activation_class_operation,
            )
            blockers.extend(activation_blockers)
            warnings.extend(activation_warnings)

        current_active_id = str(current_active["model_id"]) if current_active else None
        if invalid_transition := _transition_blocker(
            operation=operation,
            lifecycle_state=lifecycle_state,
            model_id=str(model["model_id"]),
            current_active_id=current_active_id,
        ):
            blockers.append(invalid_transition)
        if operation in {"activate", "switch_version"} and current_active_id == model["model_id"]:
            warnings.append({"code": "ALREADY_CURRENT", "message": "Model is already the active model for this scope."})
        if operation == "switch_version" and current_active_id is None:
            blockers.append(
                _preflight_blocker("SWITCH_REQUIRES_CURRENT_ACTIVE", "Version switch requires current active model.")
            )
        removes_current_active = (
            bool(model.get("active_flag"))
            and current_active_id == model["model_id"]
            and operation in {"deactivate", "supersede", "deprecate"}
        )
        operation_supports_missing_active_override = operation == "deactivate"
        if removes_current_active and (not override_missing_active or not operation_supports_missing_active_override):
            blockers.append(
                _preflight_blocker(
                    "MISSING_ACTIVE_RISK",
                    "Operation would leave this basin version without an active model.",
                )
            )
        if (
            operation == "deactivate"
            and removes_current_active
            and override_missing_active
            and not str(reason or "").strip()
        ):
            blockers.append(_preflight_blocker("OVERRIDE_REASON_REQUIRED", "Override requires a non-empty reason."))
        if (
            operation == "deactivate"
            and removes_current_active
            and override_missing_active
            and actor_roles
            and "sys_admin" not in actor_roles
        ):
            blockers.append(
                _preflight_blocker("OVERRIDE_REQUIRES_SYS_ADMIN", "Missing-active override requires sys_admin.")
            )
        if operation == "rollback_version":
            if current_active_id != model["model_id"]:
                blockers.append(
                    _preflight_blocker("ROLLBACK_CURRENT_STALE", "Rollback target is not the current active model.")
                )
            if previous_model is None:
                blockers.append(_preflight_blocker("ROLLBACK_HISTORY_MISSING", "Rollback requires a prior model id."))
            elif previous_model.get("basin_version_id") != model.get("basin_version_id"):
                blockers.append(_preflight_blocker("ROLLBACK_SCOPE_MISMATCH", "Rollback model scope does not match."))
            elif _canonical_lifecycle_state(previous_model) not in {"inactive", "superseded"}:
                blockers.append(
                    _preflight_blocker(
                        "INVALID_TRANSITION",
                        f"rollback_version is not allowed from previous {_canonical_lifecycle_state(previous_model)}.",
                    )
                )
            if rollback_history is None:
                blockers.append(
                    _preflight_blocker("ROLLBACK_HISTORY_MISSING", "No trustworthy prior active audit history exists.")
                )
            elif not bool(rollback_history.get("trusted")):
                blockers.append(
                    _preflight_blocker(
                        "ROLLBACK_CURRENT_STALE",
                        "Rollback history is stale for the current active epoch.",
                    )
                )

        # §3.1 (Epic #961) legacy-reactivation guard. Fail-closed, no
        # override: once a basin has direct-grid activation history the
        # only permitted activation-class target is another direct-grid
        # variant (fix-forward, direct → direct'). The predicate classifies
        # ``restored_model`` — the row that would become ``active`` after
        # commit — so ``rollback_version`` is judged on the RESTORED
        # previous model, not on the addressed currently-active model.
        # A target that declares ``forcing_mapping_mode='direct_grid'`` but
        # fails the parser is refused with a DISTINCT blocker code so an
        # operator can tell a broken fix-forward candidate from a genuine
        # legacy target.
        if (
            activation_class_operation
            and direct_grid_history is not None
            and restored_model is not None
        ):
            target_classification = _classify_forcing_mapping_mode(restored_model)
            if target_classification == "invalid_direct_grid":
                blockers.append(
                    _preflight_blocker(
                        "DIRECT_GRID_CONTRACT_INVALID",
                        "Target declares direct-grid forcing but its contract failed the "
                        "parser; the legacy-reactivation guard refuses broken fix-forward "
                        "candidates on a basin with direct-grid activation history.",
                    )
                )
            elif target_classification == "legacy":
                blockers.append(
                    _preflight_blocker(
                        "LEGACY_REACTIVATION_BLOCKED",
                        "Legacy-mapping reactivation is refused for a basin that has "
                        "direct-grid activation history (fail-closed, no override).",
                    )
                )

        status = "blocked" if blockers else "ready"
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "request_id": request_id,
            "operation": operation,
            "action_id": action_id,
            "actor_id": actor_id,
            "roles": list(actor_roles),
            "status": status,
            "basin_id": model.get("basin_id"),
            "basin_version_id": model.get("basin_version_id"),
            "model_id": model.get("model_id"),
            "current_active_model_id": current_active_id,
            "previous_model_id": previous_model.get("model_id") if previous_model else None,
            "restored_model_id": restored_model.get("model_id") if restored_model else None,
            "prior_audit_log_id": rollback_history.get("prior_audit_log_id") if rollback_history else None,
            "rollback_history": _rollback_history_preflight_reference(rollback_history),
            "river_network_version_id": restored_model.get("river_network_version_id") if restored_model else None,
            "mesh_version_id": restored_model.get("mesh_version_id") if restored_model else None,
            "lineage": redact_audit_payload(
                {
                    "package_checksum": _first_non_empty(
                        resource_profile.get("package_checksum"),
                        restored_model.get("package_checksum") if restored_model else None,
                    ),
                    "source_inventory_checksum": resource_profile.get("source_inventory_checksum"),
                    "mesh_checksum": restored_model.get("mesh_checksum") if restored_model else None,
                    "river_network_checksum": restored_model.get("river_network_checksum") if restored_model else None,
                    "basin_checksum": restored_model.get("basin_checksum") if restored_model else None,
                    "object_uri": (
                        _sanitize_audit_uri(restored_model.get("model_package_uri")) if restored_model else None
                    ),
                    "manifest_uri": _sanitize_audit_uri(resource_profile.get("manifest_uri"))
                    if resource_profile.get("manifest_uri")
                    else None,
                    "copied_root_status": copied_root,
                    "mesh_properties": mesh_properties,
                }
            ),
            "object_uri_prefix": {
                "status": (
                    _object_uri_prefix_status(restored_model.get("model_package_uri"))
                    if restored_model
                    else "missing"
                ),
                "uri": _sanitize_audit_uri(restored_model.get("model_package_uri")) if restored_model else None,
            },
            "impact": {
                "downstream_surfaces": ["forecast-routing", "model-assets-api", "operator-audit"],
                "segment_count": int(model["segment_count"]) if model.get("segment_count") is not None else None,
                "active_scope": {
                    "basin_id": model.get("basin_id"),
                    "basin_version_id": model.get("basin_version_id"),
                },
            },
            "blockers": blockers,
            "warnings": warnings,
            "override_missing_active": bool(override_missing_active),
            "reason": REDACTED_REASON,
        }

    def _apply_model_lifecycle_transition(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        operation: ModelLifecycleOperation,
        previous_model: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        lifecycle_state = str(model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive"))
        if operation in {"activate", "switch_version"}:
            if lifecycle_state == "active" and bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
                raise InvalidPayloadError(f"Invalid {operation} transition from {lifecycle_state}.")
            if current_active and current_active["model_id"] != model["model_id"]:
                self._update_model_lifecycle_state(cursor, str(current_active["model_id"]), "superseded")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "active")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "deactivate":
            if lifecycle_state == "inactive" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "inactive")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "supersede":
            if lifecycle_state == "superseded" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"active", "inactive", "deprecated"}:
                raise InvalidPayloadError(f"Invalid supersede transition from {lifecycle_state}.")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "superseded")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "deprecate":
            if lifecycle_state == "deprecated" and not bool(model.get("active_flag")):
                return {"outcome": "already_current", "model": dict(model), "previous_model": current_active}
            if lifecycle_state not in {"inactive", "superseded"}:
                raise InvalidPayloadError(f"Invalid deprecate transition from {lifecycle_state}.")
            updated = self._update_model_lifecycle_state(cursor, str(model["model_id"]), "deprecated")
            return {"outcome": "allowed", "model": updated, "previous_model": current_active}
        if operation == "rollback_version":
            if previous_model is None:
                raise InvalidPayloadError("previous_model_id is required for rollback_version.")
            previous_state = _canonical_lifecycle_state(previous_model)
            if previous_state not in {"inactive", "superseded"}:
                raise InvalidPayloadError(f"Invalid rollback_version transition from previous {previous_state}.")
            self._update_model_lifecycle_state(cursor, str(model["model_id"]), "superseded")
            updated = self._update_model_lifecycle_state(cursor, str(previous_model["model_id"]), "active")
            return {"outcome": "rollback", "model": updated, "previous_model": model}
        raise InvalidPayloadError(f"Unsupported model lifecycle operation: {operation}")

    def _update_model_lifecycle_state(
        self,
        cursor: Any,
        model_id: str,
        lifecycle_state: ModelLifecycleState,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            WITH updated AS (
                UPDATE core.model_instance
                SET lifecycle_state = %s,
                    active_flag = %s
                WHERE model_id = %s
                RETURNING *
            )
            SELECT
                u.*,
                COALESCE(u.lifecycle_state, CASE WHEN u.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
                b.basin_id,
                b.basin_name,
                bv.checksum AS basin_checksum,
                rnv.segment_count,
                rnv.checksum AS river_network_checksum,
                mv.mesh_uri,
                mv.checksum AS mesh_checksum,
                mv.properties_json AS mesh_properties_json
            FROM updated u
            JOIN core.basin_version bv
              ON bv.basin_version_id = u.basin_version_id
            JOIN core.basin b
              ON b.basin_id = bv.basin_id
            JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = u.river_network_version_id
            JOIN core.mesh_version mv
              ON mv.mesh_version_id = u.mesh_version_id
            """,
            (lifecycle_state, lifecycle_state == "active", model_id),
        )
        return dict(cursor.fetchone())
