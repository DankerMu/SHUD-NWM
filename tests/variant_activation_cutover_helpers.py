"""Shared harness of the variant activation cutover suite (#3165).

Split out of ``tests/test_variant_activation_cutover.py``; its partitions
(``tests/test_variant_activation_cutover*.py``) import from here. Not a test
module: pytest does not collect it.

* ``_HarnessStore`` is a :class:`PsycopgModelRegistryStore` whose DB-touching
  helpers are in-memory, so the real ``model_lifecycle_operation`` flow
  (preflight, hook dispatch, transition, audit) runs unchanged.
* ``variant_direct_grid_profile`` and ``two_models`` are pytest fixtures. A
  partition that requests ``two_models`` must import BOTH names, because pytest
  resolves a fixture's own dependencies by name in the requesting module.
* ``_ManifestPublisherStub`` records the post-commit manifest publisher calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from packages.common.auth_policy import trusted_internal_policy_decision
from packages.common.model_registry import (
    ModelLifecycleOperation,
    PostCommitPublishContext,
    PsycopgModelRegistryStore,
)

# --- test harness ----------------------------------------------------------


BASIN_VERSION_ID = "basin_v01"
BASIN_ID = "basin_a"


def _model_row(
    *,
    model_id: str,
    active_flag: bool = False,
    lifecycle_state: str = "inactive",
    resource_profile: dict[str, Any] | None = None,
    basin_version_id: str = BASIN_VERSION_ID,
    basin_id: str = BASIN_ID,
) -> dict[str, Any]:
    """Return a model row shaped like ``_fetch_model_lifecycle_row`` output.

    Includes every field ``_activation_safety_evidence`` and
    ``_build_model_operation_preflight`` inspect so a preflight over this
    row lands ``status='ready'`` for activation-class operations.
    """
    profile = {
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "package_checksum": f"sha256:{model_id}-package",
        "copied_root_status": "verified",
        "package_checksum_verified": True,
        **(resource_profile or {}),
    }
    return {
        "model_id": model_id,
        "model_name": model_id,
        "basin_id": basin_id,
        "basin_name": basin_id.upper(),
        "basin_version_id": basin_version_id,
        "river_network_version_id": f"{basin_id}_rivnet_v01",
        "mesh_version_id": f"{basin_id}_mesh_v01",
        "calibration_version_id": f"{basin_id}_cal_v01",
        "shud_code_version": "2.0",
        "mesh_uri": f"s3://nhms/models/{model_id}/mesh.sp.mesh",
        "mesh_checksum": f"sha256:{model_id}-mesh",
        "model_package_uri": f"s3://nhms/models/{model_id}/package/",
        "package_checksum": f"sha256:{model_id}-package",
        "manifest_uri": f"s3://nhms/models/{model_id}/manifest.json",
        "source_inventory_checksum": None,
        "basin_slug": basin_id.replace("_", "-"),
        "shud_input_name": basin_id,
        "segment_count": 1,
        "basin_checksum": f"sha256:{basin_id}-basin",
        "river_network_checksum": f"sha256:{basin_id}-rivnet",
        "mesh_properties_json": {},
        "active_flag": active_flag,
        "lifecycle_state": lifecycle_state,
        "resource_profile": profile,
        "created_at": "2026-05-07T00:00:00Z",
    }


class _RecordingCursor:
    """Fake cursor that records every SQL statement passed through it.

    Used as the transaction cursor for the harness store; the harness
    overrides every ``_fetch_*`` / ``_update_*`` / ``_insert_*`` helper
    so no statements should reach this cursor. Recording still catches
    an accidental raw ``cursor.execute`` regression.
    """

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self._pending: dict[str, Any] | None = None

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:  # pragma: no cover - regression guard
        self.statements.append((statement, tuple(parameters)))

    def fetchone(self) -> dict[str, Any] | None:  # pragma: no cover - regression guard
        result = self._pending
        self._pending = None
        return result


class _FakeTransaction:
    """Context manager wrapper around a :class:`_RecordingCursor`.

    Tracks enter/exit so the tests can assert the swap and audit happen
    in ONE transaction (single-transaction reuse, §2.1 spec scenario
    "Cutover reuses the existing single-transaction activation
    lifecycle").
    """

    def __init__(self, harness: _HarnessStore) -> None:
        self._harness = harness

    def __enter__(self) -> _RecordingCursor:
        cursor = _RecordingCursor()
        self._harness._transactions.append({"cursor": cursor, "committed": None})
        self._harness._current_cursor = cursor
        return cursor

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: Any) -> bool:
        state = self._harness._transactions[-1]
        state["committed"] = exc_type is None
        self._harness._current_cursor = None
        return False


class _HarnessStore(PsycopgModelRegistryStore):
    """In-memory PsycopgModelRegistryStore for §2.1 hook-contract tests.

    Overrides the DB-touching helpers so the real
    ``model_lifecycle_operation`` — preflight, hook dispatch, transition,
    audit — runs unchanged over ``self._models`` and ``self.audit_rows``.
    """

    def __init__(self, models: list[Mapping[str, Any]]) -> None:
        super().__init__("postgresql://harness")
        # Frozen dataclass: bypass with ``object.__setattr__``.
        object.__setattr__(self, "_models", {row["model_id"]: dict(row) for row in models})
        object.__setattr__(self, "audit_rows", [])
        object.__setattr__(self, "_transactions", [])
        object.__setattr__(self, "_current_cursor", None)
        object.__setattr__(self, "_state_updates", [])  # (model_id, lifecycle_state, active_flag)

    # ---- transaction plumbing --------------------------------------------

    def _transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    # ---- read helpers (all cursor arg unused for in-memory backend) ------

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:  # noqa: ARG002
        # basin_version_id is presumed to exist for the harness.
        return None

    def _fetch_model_lifecycle_row(
        self, cursor: Any, model_id: str, *, for_update: bool  # noqa: ARG002
    ) -> dict[str, Any] | None:
        row = self._models.get(model_id)
        return dict(row) if row is not None else None

    def _fetch_active_model_for_scope(
        self, cursor: Any, basin_version_id: str, *, for_update: bool  # noqa: ARG002
    ) -> dict[str, Any] | None:
        for row in self._models.values():
            if (
                row["basin_version_id"] == basin_version_id
                and bool(row.get("active_flag"))
                and str(row.get("lifecycle_state") or "active") == "active"
            ):
                return dict(row)
        return None

    def _fetch_trustworthy_rollback_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        current_model: Mapping[str, Any],  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_idempotent_rollback_retry_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
        previous_model_id: str | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        return None

    def _fetch_direct_grid_activation_history(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        basin_version_id: str,  # noqa: ARG002
        current_active: Mapping[str, Any] | None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        # SUB-5/6/7 hook + manifest tests do not exercise the §3.1
        # legacy-reactivation guard; keep the guard disarmed here so
        # this harness stays scoped to the SUB-5/6/7 contracts. The
        # dedicated §3.1 test file (`test_legacy_reactivation_guard.py`)
        # overrides this method with a real history predicate.
        return None

    # ---- write helpers ---------------------------------------------------

    def _update_model_lifecycle_state(
        self, cursor: Any, model_id: str, lifecycle_state: str  # noqa: ARG002
    ) -> dict[str, Any]:
        row = self._models[model_id]
        row["lifecycle_state"] = lifecycle_state
        row["active_flag"] = lifecycle_state == "active"
        self._state_updates.append((model_id, lifecycle_state, row["active_flag"]))
        return dict(row)

    def _insert_model_lifecycle_audit(
        self,
        cursor: Any,  # noqa: ARG002
        *,
        model: Mapping[str, Any],
        updated: Mapping[str, Any],
        operation: ModelLifecycleOperation,
        outcome: str,
        policy_decision: Any,
        request_id: str | None,
        preflight: Mapping[str, Any],
        previous_model: Mapping[str, Any] | None,
        reason: str | None,
    ) -> int:
        entry = {
            "action": policy_decision.action_id,
            "actor": policy_decision.actor_id,
            "entity_type": "model_instance",
            "entity_id": model["model_id"],
            "operation": operation,
            "outcome": outcome,
            "basin_version_id": model.get("basin_version_id"),
            "request_id": request_id,
            "reason": reason,
            "preflight_status": preflight.get("status"),
            "updated_model_id": updated["model_id"],
            "previous_model_id": previous_model["model_id"] if previous_model else None,
        }
        self.audit_rows.append(entry)
        return len(self.audit_rows)


# --- fixtures --------------------------------------------------------------


def _decision(action_id: str, target_id: str) -> Any:
    return trusted_internal_policy_decision(
        action_id,
        target_type="model_instance",
        target_id=target_id,
        actor_id="test:harness",
        roles=("sys_admin",),
    )


@pytest.fixture
def variant_direct_grid_profile() -> dict[str, Any]:
    """A direct-grid ``resource_profile`` extras dict for the target model.

    Locks the source-scope extraction path used by the hook context.
    """
    return {
        "canonical_grid_key": "canonical_key_grid_a_v1",
        "direct_grid_forcing": {
            "forcing_mapping_mode": "direct_grid",
            "applicable_source_ids": ["gfs", "IFS"],
            "grid_id": "grid_a",
        },
    }


@pytest.fixture
def two_models(variant_direct_grid_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """A legacy IDW active baseline + an inactive direct-grid variant."""

    return [
        _model_row(
            model_id="legacy_m0",
            active_flag=True,
            lifecycle_state="active",
        ),
        _model_row(
            model_id="direct_grid_m1",
            active_flag=False,
            lifecycle_state="inactive",
            resource_profile=variant_direct_grid_profile,
        ),
    ]


# --- post-commit manifest publisher stub ---------------------------------


class _ManifestPublisherStub:
    """Recording stub for the post-commit manifest publisher.

    Captures every :class:`PostCommitPublishContext` the seam hands over,
    plus a call counter for the ``exactly-once`` assertion. A single
    stub instance can be reused across a parametrized test because
    each parametrization instantiates a fresh store + stub pair.
    """

    def __init__(self) -> None:
        self.contexts: list[PostCommitPublishContext] = []

    @property
    def call_count(self) -> int:
        return len(self.contexts)

    def __call__(self, ctx: PostCommitPublishContext) -> None:
        self.contexts.append(ctx)
