"""Lifecycle support methods of ``PsycopgModelRegistryStore``: the
prior-state / rollback-history reads and the lifecycle / activation audit
writes (#2617 split of ``packages.common.model_registry``).

A plain mixin: no fields and no dunders. ``PsycopgModelRegistryStore`` in
``packages.common.model_registry`` is the only class that composes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packages.common.auth_policy import PolicyDecision, audit_record, redact_audit_payload
from packages.common.model_registry_contracts import (
    ModelLifecycleOperation,
    _classify_forcing_mapping_mode,
    _json_mapping,
)
from packages.common.model_registry_preflight_rules import _canonical_lifecycle_state, _model_audit_reference
from packages.common.model_registry_public import (
    REDACTED_REASON,
    _sanitize_audit_uri,
)


class _LifecycleSupportMixin:
    """Prior-state reads and audit writes."""

    def _fetch_model_lifecycle_row(self, cursor: Any, model_id: str, *, for_update: bool) -> dict[str, Any] | None:
        lock_clause = "FOR UPDATE" if for_update else ""
        return self._fetch_optional(
            cursor,
            f"""
            SELECT
                mi.*,
                COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
                b.basin_id,
                b.basin_name,
                bv.checksum AS basin_checksum,
                rnv.segment_count,
                rnv.checksum AS river_network_checksum,
                mv.mesh_uri,
                mv.checksum AS mesh_checksum,
                mv.properties_json AS mesh_properties_json
            FROM core.model_instance mi
            JOIN core.basin_version bv
              ON bv.basin_version_id = mi.basin_version_id
            JOIN core.basin b
              ON b.basin_id = bv.basin_id
            JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = mi.river_network_version_id
            JOIN core.mesh_version mv
              ON mv.mesh_version_id = mi.mesh_version_id
            WHERE mi.model_id = %s
            {lock_clause}
            """,
            (model_id,),
        )

    def _fetch_active_model_for_scope(
        self,
        cursor: Any,
        basin_version_id: str,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        lock_clause = "FOR UPDATE" if for_update else ""
        return self._fetch_optional(
            cursor,
            f"""
            SELECT
                mi.*,
                COALESCE(mi.lifecycle_state, CASE WHEN mi.active_flag THEN 'active' ELSE 'inactive' END)
                    AS lifecycle_state,
                b.basin_id,
                b.basin_name,
                bv.checksum AS basin_checksum,
                rnv.segment_count,
                rnv.checksum AS river_network_checksum,
                mv.mesh_uri,
                mv.checksum AS mesh_checksum,
                mv.properties_json AS mesh_properties_json
            FROM core.model_instance mi
            JOIN core.basin_version bv
              ON bv.basin_version_id = mi.basin_version_id
            JOIN core.basin b
              ON b.basin_id = bv.basin_id
            JOIN core.river_network_version rnv
              ON rnv.river_network_version_id = mi.river_network_version_id
            JOIN core.mesh_version mv
              ON mv.mesh_version_id = mi.mesh_version_id
            WHERE mi.basin_version_id = %s
              AND mi.active_flag = true
              AND COALESCE(mi.lifecycle_state, 'active') = 'active'
            ORDER BY mi.created_at DESC, mi.model_id
            LIMIT 1
            {lock_clause}
            """,
            (basin_version_id,),
        )

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,
        *,
        current_model: Mapping[str, Any],
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        if previous_model_id is None:
            return None
        row = self._fetch_optional(
            cursor,
            """
            SELECT log_id, action, entity_id, details, created_at
            FROM ops.audit_log
            WHERE entity_type = 'model_instance'
              AND action IN ('models.activate', 'models.switch_version', 'models.rollback_version')
              AND details->>'operation' IN ('activate', 'switch_version', 'rollback_version')
              AND details->>'outcome' IN ('allowed', 'rollback')
              AND details->>'basin_version_id' = %s
              AND (
                entity_id = %s
                OR details->'updated_model'->>'model_id' = %s
              )
            ORDER BY created_at DESC, log_id DESC
            LIMIT 1
            """,
            (
                str(current_model["basin_version_id"]),
                str(current_model["model_id"]),
                str(current_model["model_id"]),
            ),
        )
        if row is None:
            return None
        details = _json_mapping(row.get("details"))
        previous_ref = _json_mapping(details.get("previous_model"))
        new_state = _json_mapping(details.get("new_state"))
        updated_ref = _json_mapping(details.get("updated_model"))
        made_current_active = (
            str(row.get("entity_id")) == str(current_model["model_id"])
            or str(updated_ref.get("model_id")) == str(current_model["model_id"])
        )
        trusted = (
            made_current_active
            and str(previous_ref.get("model_id")) == str(previous_model_id)
            and str(details.get("basin_version_id")) == str(current_model.get("basin_version_id"))
            and bool(new_state.get("active")) is True
            and str(new_state.get("lifecycle_state")) == "active"
            and bool(current_model.get("active_flag")) is True
            and _canonical_lifecycle_state(current_model) == "active"
        )
        row["trusted"] = trusted
        row["prior_audit_log_id"] = row.get("log_id")
        row["matched_previous_model_id"] = previous_ref.get("model_id")
        if not trusted:
            row["stale_reason"] = "latest_current_epoch_previous_mismatch"
        return row

    def _fetch_direct_grid_activation_history(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        current_active: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return direct-grid activation evidence for a basin scope, or None.

        §3.1 (Epic #961) legacy-reactivation guard predicate. History is
        APPEND-ONLY, derived from either

          * the scope's currently-active model classifying as direct-grid, OR
          * an ``ops.audit_log`` record of a successful activation-class
            transition (``action IN ('models.activate',
            'models.switch_version', 'models.rollback_version')``, ``details
            ->> 'outcome' IN ('allowed', 'rollback')``) whose resulting-active
            model (``details -> 'updated_model' ->> 'model_id'`` joined to
            ``core.model_instance``) classifies as direct-grid.

        Returns ``None`` when neither source is armed. Later
        ``deactivate`` / ``deprecate`` / ``supersede`` cannot erase the
        audit-log arm — the history predicate is one-way.
        """
        if current_active is not None:
            if _classify_forcing_mapping_mode(current_active) == "direct_grid":
                return {
                    "source": "current_active",
                    "model_id": str(current_active.get("model_id")),
                    "basin_version_id": basin_version_id,
                }
        cursor.execute(
            """
            SELECT
                al.log_id,
                al.details -> 'updated_model' ->> 'model_id' AS updated_model_id,
                mi.resource_profile AS updated_resource_profile
            FROM ops.audit_log al
            JOIN core.model_instance mi
              ON mi.model_id = (al.details -> 'updated_model' ->> 'model_id')
            WHERE al.entity_type = 'model_instance'
              AND al.action IN (
                'models.activate',
                'models.switch_version',
                'models.rollback_version'
              )
              AND al.details ->> 'outcome' IN ('allowed', 'rollback')
              AND al.details ->> 'basin_version_id' = %s
            ORDER BY al.created_at ASC, al.log_id ASC
            """,
            (basin_version_id,),
        )
        rows = cursor.fetchall()
        for row in rows:
            classification = _classify_forcing_mapping_mode(
                {"resource_profile": row.get("updated_resource_profile")}
            )
            if classification == "direct_grid":
                return {
                    "source": "audit_log",
                    "log_id": row.get("log_id"),
                    "model_id": row.get("updated_model_id"),
                    "basin_version_id": basin_version_id,
                }
        return None

    def _fetch_idempotent_rollback_retry_history(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        current_active: Mapping[str, Any] | None,
        previous_model_id: str | None,
    ) -> dict[str, Any] | None:
        if current_active is None or previous_model_id is None:
            return None
        if str(current_active.get("model_id")) != str(previous_model_id):
            return None
        if str(current_active.get("basin_version_id")) != str(model.get("basin_version_id")):
            return None
        if not bool(current_active.get("active_flag")) or _canonical_lifecycle_state(current_active) != "active":
            return None
        if bool(model.get("active_flag")) or _canonical_lifecycle_state(model) not in {"inactive", "superseded"}:
            return None
        row = self._fetch_optional(
            cursor,
            """
            SELECT log_id, action, entity_id, details, created_at
            FROM ops.audit_log
            WHERE entity_type = 'model_instance'
              AND entity_id = %s
              AND action = 'models.rollback_version'
              AND details->>'operation' = 'rollback_version'
              AND details->>'outcome' = 'rollback'
              AND details->>'basin_version_id' = %s
              AND details->'previous_model'->>'model_id' = %s
              AND details->'updated_model'->>'model_id' = %s
            ORDER BY created_at DESC, log_id DESC
            LIMIT 1
            """,
            (
                str(model["model_id"]),
                str(model["basin_version_id"]),
                str(model["model_id"]),
                str(previous_model_id),
            ),
        )
        if row is None:
            return None
        row["trusted"] = True
        row["prior_audit_log_id"] = row.get("log_id")
        row["matched_previous_model_id"] = previous_model_id
        return row

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: PolicyDecision,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        details = audit_record(
            policy_decision,
            request_id=request_id,
            previous_state={
                "active": bool(model.get("active_flag")),
                "lifecycle_state": model.get("lifecycle_state"),
            },
            new_state={
                "active": bool(updated.get("active_flag")),
                "lifecycle_state": updated.get("lifecycle_state"),
            },
            payload={
                "operation": operation,
                "outcome": outcome,
                "basin_id": model.get("basin_id"),
                "basin_version_id": model.get("basin_version_id"),
                "river_network_version_id": model.get("river_network_version_id"),
                "mesh_version_id": model.get("mesh_version_id"),
                "model_package_uri": _sanitize_audit_uri(model.get("model_package_uri")),
                "reason": REDACTED_REASON if reason else None,
                "preflight": preflight,
                "previous_model": _model_audit_reference(previous_model),
                "updated_model": _model_audit_reference(updated),
                "prior_audit_log_id": preflight.get("prior_audit_log_id"),
            },
        )
        details.update(
            {
                "operation": operation,
                "outcome": outcome,
                "basin_id": model.get("basin_id"),
                "basin_version_id": model.get("basin_version_id"),
                "river_network_version_id": model.get("river_network_version_id"),
                "mesh_version_id": model.get("mesh_version_id"),
                "model_package_uri": _sanitize_audit_uri(model.get("model_package_uri")),
                "previous_model": _model_audit_reference(previous_model),
                "updated_model": _model_audit_reference(updated),
                "prior_audit_log_id": preflight.get("prior_audit_log_id"),
                "preflight": preflight,
                "reason": REDACTED_REASON if reason else None,
            }
        )
        details = redact_audit_payload(details)
        cursor.execute(
            """
            INSERT INTO ops.audit_log (
                actor,
                actor_role,
                action,
                entity_type,
                entity_id,
                details
            )
            VALUES (%s, %s, %s, 'model_instance', %s, %s)
            RETURNING log_id
            """,
            (
                policy_decision.actor_id,
                ",".join(policy_decision.roles),
                policy_decision.action_id,
                model["model_id"],
                self._json(details),
            ),
        )
        return int(cursor.fetchone()["log_id"])
