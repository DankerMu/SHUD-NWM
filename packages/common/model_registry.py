from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from packages.common.auth_policy import (
    PolicyDecision,
    audit_record,
    redact_audit_payload,
    require_policy_evidence,
    trusted_internal_policy_decision,
)
from packages.common.forecast_store import QHH_LATEST_READY_RUN_STATUSES
from workers.forcing_producer.direct_grid_contract import (
    DIRECT_GRID_MODE,
    DIRECT_GRID_SECTION_KEYS,
    DirectGridContractError,
    load_forcing_mapping_contract_from_manifest,
)


class ModelRegistryError(RuntimeError):
    """Base class for model registry failures."""


class DuplicateResourceError(ModelRegistryError):
    """Raised when a registry resource already exists."""


class MissingResourceError(ModelRegistryError):
    """Raised when a requested registry resource does not exist."""


class InvalidReferenceError(ModelRegistryError):
    """Raised when a payload references a missing or mismatched resource."""


class InvalidPayloadError(ModelRegistryError):
    """Raised when a payload is structurally invalid."""


class ModelLifecycleAuditPersistenceError(ModelRegistryError):
    """Raised when lifecycle audit persistence fails after a prepared mutation."""

    def __init__(self, result: Mapping[str, Any], cause: BaseException) -> None:
        super().__init__("Model lifecycle audit evidence could not be persisted.")
        self.result = dict(result)
        self.__cause__ = cause


ModelLifecycleState = Literal["inactive", "active", "deprecated", "superseded"]
ModelLifecycleOperation = Literal[
    "activate",
    "deactivate",
    "switch_version",
    "rollback_version",
    "supersede",
    "deprecate",
]

MODEL_LIFECYCLE_STATES: tuple[ModelLifecycleState, ...] = ("inactive", "active", "deprecated", "superseded")
MODEL_LIFECYCLE_ACTIONS: dict[str, str] = {
    "activate": "models.activate",
    "deactivate": "models.deactivate",
    "switch_version": "models.switch_version",
    "rollback_version": "models.rollback_version",
    "supersede": "models.supersede",
    "deprecate": "models.deactivate",
}


SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES = 10_000
SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS = 3
RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES = SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES
RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS = SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS
RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES = 50_000
RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES = 1_000_000
RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES = 250_000


class RiverSegmentGeoJsonBudgetError(ModelRegistryError):
    """Raised when a river segment GeoJSON response exceeds the server serialization budget."""

    def __init__(
        self,
        *,
        limit_type: str,
        max_bytes: int,
        serialized_bytes: int,
        scope: str,
    ) -> None:
        super().__init__("River segment GeoJSON payload budget exceeded.")
        self.limit_type = limit_type
        self.max_bytes = max_bytes
        self.serialized_bytes = serialized_bytes
        self.scope = scope


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ModelRegistryError("DATABASE_URL is required for model registry operations.")
    return database_url


def _escape_like(value: str) -> str:
    """Escape LIKE/ILIKE wildcards so user search input matches literally.

    The backslash is the ESCAPE character; %/_ are the only LIKE metacharacters.
    Values remain bound as parameters, so this only prevents the search term from
    silently widening the pattern (e.g. a literal '%').
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_versioned_id(prefix: str, version_label: str | None, explicit_id: str | None = None) -> str:
    """Build a conservative ID from a prefix and version label when an explicit ID is absent."""
    if explicit_id:
        return explicit_id
    if not version_label:
        raise InvalidPayloadError("version_label is required when an explicit id is not provided.")

    label = re.sub(r"[^a-zA-Z0-9]+", "_", version_label.strip()).strip("_").lower()
    if not label:
        raise InvalidPayloadError("version_label must contain at least one alphanumeric character.")
    if not label.startswith("v"):
        label = f"v{label}"
    return f"{prefix}_{label}"


def geometry_to_wkt(geom: Mapping[str, Any] | str, expected_type: str) -> str:
    """Convert simple GeoJSON geometry or WKT into WKT for PostGIS insertion."""
    if isinstance(geom, str):
        candidate = geom.strip()
        if not candidate.upper().startswith(expected_type.upper()):
            raise InvalidPayloadError(f"geom must be a {expected_type} geometry.")
        return candidate

    geom_type = str(geom.get("type", ""))
    if geom_type != expected_type:
        raise InvalidPayloadError(f"geom.type must be {expected_type}.")

    coordinates = geom.get("coordinates")
    if geom_type == "MultiPolygon":
        return _multipolygon_to_wkt(coordinates)
    if geom_type == "LineString":
        return _linestring_to_wkt(coordinates)
    raise InvalidPayloadError(f"Unsupported geometry type: {geom_type}.")


def _linestring_to_wkt(coordinates: Any) -> str:
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise InvalidPayloadError("LineString coordinates must contain at least two points.")
    return "LINESTRING(" + ", ".join(_format_point(point) for point in coordinates) + ")"


def _multipolygon_to_wkt(coordinates: Any) -> str:
    if not isinstance(coordinates, list) or not coordinates:
        raise InvalidPayloadError("MultiPolygon coordinates must contain at least one polygon.")

    polygons: list[str] = []
    for polygon in coordinates:
        if not isinstance(polygon, list) or not polygon:
            raise InvalidPayloadError("Each MultiPolygon polygon must contain at least one ring.")
        rings = []
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise InvalidPayloadError("Each MultiPolygon ring must contain at least four points.")
            rings.append("(" + ", ".join(_format_point(point) for point in ring) + ")")
        polygons.append("(" + ", ".join(rings) + ")")
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"


def _format_point(point: Any) -> str:
    if not isinstance(point, list | tuple) or len(point) < 2:
        raise InvalidPayloadError("Geometry point must contain longitude and latitude.")
    return f"{float(point[0]):.12g} {float(point[1]):.12g}"


@dataclass(frozen=True)
class ColdStartApprovalInput:
    """Explicit cold-start approval carried on an activation request.

    Epic #982 SUB-5 (``mapping-variant-state-compatibility`` task 3.2):
    an operator with sys_admin authority may override a fingerprint
    refusal by naming the exact ``covered_source_ids`` they authorize
    to cold-start alongside a human-legible ``reason``. The approval
    is unconditional grant for the named sources — the state-clone hook
    skips the fingerprint gate for each covered source and records the
    approval in ``ops.audit_log`` in the SAME transaction as the
    supersede + activate swap, so no covered cutover can ship without
    the audit obligation.

    The approval carries the spin-up-distortion public-announcement
    obligation (docs §11.3 clause 3) via the marker literal on the audit
    record and the activation result; that literal is written by the
    hook and by :meth:`PsycopgModelRegistryStore.model_lifecycle_operation`
    respectively and is imported from ``packages.common.state_clone_hook``
    so both sites stay in lockstep.

    Fields:
        approver: Human-legible identity of the operator granting the
            approval — surfaces on the audit record.
        reason: Free-text justification the operator supplied. Stored
            verbatim; never redacted (approvals are auditable evidence,
            not sensitive payload).
        covered_source_ids: Exact tuple of ``source_id``s this approval
            covers. A source outside this tuple is NOT covered — the
            fingerprint gate still runs and can still refuse. Approvals
            never widen beyond their named sources.
    """

    approver: str
    reason: str
    covered_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ModelActivationContext:
    """Context payload delivered to every pre-activation hook.

    §2.1 (Epic #961, `source-specific-model-variant-routing`) defines the
    ordered pre-activation extension-point contract. Hooks receive this
    frozen container inside the lifecycle transaction, before the
    supersede+activate swap runs; a raising hook aborts the whole
    transaction.

    Fields:
        basin_version_id: The scope (`core.model_instance.basin_version_id`)
            whose active model is being swapped.
        previous_active_model: The row that will be superseded (or ``None``
            when no prior active model exists for the scope).
        target_model: The row that will become active as a result of the
            transition. For ``rollback_version`` this is the RESTORED
            ``previous_model``, not the addressed model, matching the
            preflight's own ``restored_model`` semantics (D5).
        source_scope: Normalized ``applicable_source_ids`` extracted from
            ``target_model.resource_profile.direct_grid_forcing`` when the
            target carries a direct-grid contract; ``None`` for a legacy
            IDW model. Kept as a tuple so hooks cannot mutate the scope.
        cold_start_approval: Optional explicit cold-start approval
            (SUB-5 task 3.2). When present, the state-clone hook skips
            the fingerprint gate for the sources named in
            ``cold_start_approval.covered_source_ids`` and records the
            approval on the activation cursor. ``None`` means no
            approval — every source in ``source_scope`` runs the
            fingerprint gate unchanged.
    """

    basin_version_id: str
    previous_active_model: Mapping[str, Any] | None
    target_model: Mapping[str, Any]
    source_scope: tuple[str, ...] | None
    cold_start_approval: ColdStartApprovalInput | None = None


PreActivationHook = Callable[[Any, ModelActivationContext], None]


# Ordered mount points reserved for later changes in this Epic.
# Order is declaration order in this tuple: ``state_clone`` (Change 5,
# fingerprint-gated state clone) runs before ``station_flag_flip``
# (Change 8, `met.met_station.active_flag` atomic flip). Both mount here
# WITHOUT re-opening the lifecycle transaction (D7).
PRE_ACTIVATION_HOOK_MOUNT_POINTS: tuple[str, ...] = ("state_clone", "station_flag_flip")


def _default_no_op_hook(cursor: Any, ctx: ModelActivationContext) -> None:  # noqa: ARG001
    """Default hook: no-op. Preserves prior lifecycle behavior byte-for-byte."""
    return None


@dataclass(frozen=True)
class PostCommitPublishContext:
    """Context payload delivered to the post-commit manifest publisher.

    §2.2 (Epic #961, `source-specific-model-variant-routing`) wires
    `services.orchestrator.scheduler_file_providers.publish_scheduler_registry_manifest`
    as the uniform post-commit tail of every successful dispatch-set-changing
    lifecycle transition. This frozen container carries the committed
    state a publisher needs to reconcile the scheduler manifest with the
    DB. The publisher runs AFTER the lifecycle transaction commits — a
    rolled-back transaction (raising hook, audit persistence failure)
    never surfaces here, so the manifest can never diverge from the
    committed DB state.

    Fields:
        basin_version_id: The scope whose active model changed.
        target_model_id: The model row the transition landed on. For
            ``activate``/``switch_version`` this is the newly-active
            model; for ``rollback_version`` it is the restored previous
            model; for ``deactivate`` it is the model that was flipped
            inactive (the operation's target).
        source_scope: Normalized ``applicable_source_ids`` extracted from
            ``target_model.resource_profile.direct_grid_forcing`` when
            the target carries a direct-grid contract; ``None`` for a
            legacy IDW model. Kept as a tuple so callbacks cannot mutate
            the scope.
        operation_type: The lifecycle operation that triggered the
            publish — one of ``activate``, ``switch_version``,
            ``rollback_version``, or ``deactivate`` (only when the
            deactivate removed the currently-active model via the
            sys_admin missing-active override).
    """

    basin_version_id: str
    target_model_id: str
    source_scope: tuple[str, ...] | None
    operation_type: ModelLifecycleOperation


PostCommitManifestPublisher = Callable[[PostCommitPublishContext], None]


def _default_no_op_manifest_publisher(ctx: PostCommitPublishContext) -> None:  # noqa: ARG001
    """Default publisher: no-op.

    Preserves prior lifecycle behavior byte-for-byte until the production
    wiring registers the real
    ``publish_scheduler_registry_manifest`` bridge. Tests can register a
    recording stub via
    :meth:`PsycopgModelRegistryStore.register_post_commit_manifest_publisher`.
    """
    return None


def _should_publish_manifest_after_commit(
    *,
    operation: ModelLifecycleOperation,
    transition_outcome: str,
    model: Mapping[str, Any],
    current_active_before: Mapping[str, Any] | None,
) -> bool:
    """Predicate: does this committed transition change the dispatch set?

    Dispatch-set-changing operations that MUST re-publish the manifest:
      * ``activate`` / ``switch_version`` / ``rollback_version`` whose
        transition landed on the ``allowed`` or ``rollback`` outcome
        (already-current returns ``already_current`` and no re-publish).
      * ``deactivate`` when the model being deactivated WAS the currently
        active model at the start of the transaction and the transition
        landed on ``allowed`` — the §11.2 step-4 pause-production lever,
        committed via the sys_admin missing-active override.

    Non-dispatch-set-changing operations that MUST NOT re-publish:
      * ``supersede`` / ``deprecate`` addressed at the currently-active
        model are preflight-blocked by ``MISSING_ACTIVE_RISK``
        (`_build_model_operation_preflight:2312-2324`), never reach the
        transition, and therefore never reach this predicate. If a
        future preflight change ever admits such an operation, this
        predicate leaves them un-published unless explicitly added.
      * A ``deactivate`` on a non-current model (e.g. an already
        superseded/deprecated row) does not change the active dispatch
        target and therefore returns False.
      * Any transition with outcome ``already_current`` — no state
        changed, nothing to re-publish.
    """
    if operation in {"activate", "switch_version", "rollback_version"}:
        return transition_outcome in {"allowed", "rollback"}
    if operation == "deactivate":
        return (
            transition_outcome == "allowed"
            and current_active_before is not None
            and str(current_active_before.get("model_id")) == str(model.get("model_id"))
        )
    return False


def _build_activation_result_approval_block(
    activation_context: ModelActivationContext | None,
) -> dict[str, Any] | None:
    """Return the ``cold_start_approval`` result block, or ``None``.

    SUB-5 task 3.2: when the activation carries an explicit approval
    covering at least one source in scope, the activation result must
    surface the approver + reason + covered sources plus the
    spin-up-distortion-announcement obligation marker (docs §11.3
    clause 3) so the caller sees the same obligation clause that
    landed on the audit record inside the hook.

    Returns ``None`` — i.e. the activation result carries NO obligation
    marker — whenever the state-clone hook would have skipped BEFORE
    reaching its per-source approval-consumption loop. The hook takes a
    skip path (``no_previous_active_model`` or ``target_not_direct_grid``)
    when ``previous_active_model is None`` OR ``source_scope is None``,
    and in both cases no ``record_approval`` audit call fires. Mirroring
    those short-circuits here keeps the result-side marker in lockstep
    with the audit-record side: no marker on the result unless the hook
    actually consumed the approval on the audit stream (fold-at-intro
    from Epic #982 SUB-5 round-1 correctness review).

    Also returns ``None`` when the approval was absent OR when none of
    the covered sources intersect the actual ``source_scope`` (a stray
    approval that covers no in-scope source records no obligation on
    the result — the hook never fires ``record_approval`` in that case
    either, so the two sites stay symmetric).
    """
    if activation_context is None:
        return None
    approval = activation_context.cold_start_approval
    if approval is None:
        return None
    # Symmetry with the state-clone hook's applicability predicates
    # (packages/common/state_clone_hook.py::_hook): the hook records a
    # skip WITHOUT invoking ``record_approval`` when the previous active
    # model is absent (fresh basin) or the target is not direct-grid.
    # Emitting the marker here in either case would attach an obligation
    # clause to the activation result with no matching audit row.
    if activation_context.previous_active_model is None:
        return None
    if activation_context.source_scope is None:
        return None
    scope = activation_context.source_scope
    covered_in_scope = tuple(
        source_id for source_id in scope if source_id in approval.covered_source_ids
    )
    if not covered_in_scope:
        return None
    # Local import mirrors :meth:`_record_state_clone_refusal_audit` — the
    # marker literal lives with the hook so both sites stay in lockstep.
    from packages.common.state_clone_hook import (
        STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER,
    )

    return {
        "approver": approval.approver,
        "reason": approval.reason,
        "covered_source_ids": list(covered_in_scope),
        "spin_up_distortion_announcement_obligation": (
            STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER
        ),
    }


def _extract_source_scope(target_model: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Read ``applicable_source_ids`` from the target's direct-grid contract.

    Returns the ids as a tuple (immutable), preserving declaration order.
    Returns ``None`` when the target is a legacy IDW model with no
    ``direct_grid_forcing`` block.
    """
    resource_profile = _json_mapping(target_model.get("resource_profile"))
    direct_grid = resource_profile.get("direct_grid_forcing")
    if not isinstance(direct_grid, Mapping):
        return None
    source_ids = direct_grid.get("applicable_source_ids")
    if not isinstance(source_ids, (list, tuple)):
        return None
    return tuple(str(source_id) for source_id in source_ids)


ForcingMappingClassification = Literal["direct_grid", "invalid_direct_grid", "legacy"]


def _classify_forcing_mapping_mode(
    model: Mapping[str, Any] | None,
) -> ForcingMappingClassification:
    """Classify a model as direct-grid, invalid-direct-grid, or legacy.

    This classifier delegates to
    ``workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest``
    — the single source of truth for direct-grid recognition. Any section
    key recognized by the parser (``direct_grid_forcing``,
    ``direct_grid_contract``, ``forcing_mapping_contract``, or root-level
    ``forcing_mapping_mode='direct_grid'``) is treated symmetrically: the
    classifier never short-circuits on a specific key name.

    Return value:

    - ``"direct_grid"``: the parser returns a valid contract from the
      resource profile.
    - ``"invalid_direct_grid"``: the resource profile DECLARED direct-grid
      intent (via root-level ``forcing_mapping_mode='direct_grid'`` or via
      a ``forcing_mapping_mode='direct_grid'`` field inside any recognized
      section) but the parser raised ``DirectGridContractError``. This
      fail-closed classification stays distinct from ``"legacy"`` so the
      caller can emit a distinct blocker code (an operator can tell "broken
      fix-forward candidate" from "genuine legacy target").
    - ``"legacy"``: the parser did not confirm direct-grid — either it
      returned ``None`` (no direct-grid section, or mode is IDW) or it
      raised on a resource profile that never declared direct-grid intent
      (e.g. a truly malformed non-direct manifest). Fail-closed default.
    """
    if model is None:
        return "legacy"
    resource_profile = _json_mapping(model.get("resource_profile"))
    try:
        contract = load_forcing_mapping_contract_from_manifest(resource_profile)
    except DirectGridContractError:
        # Parser rejected the manifest. Only classify as "invalid_direct_grid"
        # when the profile explicitly declared direct-grid intent — otherwise
        # a broken non-direct manifest would spuriously get the fix-forward
        # blocker code.
        if _declares_direct_grid_intent(resource_profile):
            return "invalid_direct_grid"
        return "legacy"
    if contract is None:
        # Parser judged non-direct (e.g. IDW mode or no recognized section).
        return "legacy"
    return "direct_grid"


def _declares_direct_grid_intent(resource_profile: Mapping[str, Any]) -> bool:
    """Return True if the resource profile declares ``forcing_mapping_mode='direct_grid'``.

    Intent is declared when either:

    * the root-level ``forcing_mapping_mode`` equals ``'direct_grid'``, or
    * any recognized direct-grid section
      (``direct_grid_forcing`` / ``direct_grid_contract`` /
      ``forcing_mapping_contract``) is a mapping whose
      ``forcing_mapping_mode`` equals ``'direct_grid'``.

    Mirrors the section keys enumerated by
    ``workers.forcing_producer.direct_grid_contract.DIRECT_GRID_SECTION_KEYS``
    so the classifier stays symmetric with the parser without duplicating
    parse semantics.
    """
    if resource_profile.get("forcing_mapping_mode") == DIRECT_GRID_MODE:
        return True
    for key in DIRECT_GRID_SECTION_KEYS:
        section = resource_profile.get(key)
        if isinstance(section, Mapping) and section.get("forcing_mapping_mode") == DIRECT_GRID_MODE:
            return True
    return False


def _would_be_already_current(
    model: Mapping[str, Any],
    operation: ModelLifecycleOperation,
) -> bool:
    """Mirror ``_apply_model_lifecycle_transition``'s already-current check.

    Kept in sync with the swap logic (§2.1 hooks skip when no swap will
    occur). For ``activate``/``switch_version`` an already-active target
    short-circuits at ``_apply_model_lifecycle_transition:2266-2268``.
    For ``rollback_version``, the idempotent-rollback-retry short-circuit
    upstream in ``model_lifecycle_operation`` handles the already-current
    return path, so this predicate stays False for rollback (any rollback
    that reaches the hook site is a real swap).
    """
    if operation not in {"activate", "switch_version"}:
        return False
    lifecycle_state = str(
        model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive")
    )
    return lifecycle_state == "active" and bool(model.get("active_flag"))


@dataclass(frozen=True)
class PsycopgModelRegistryStore:
    database_url: str
    audit_actor: str = "nhms-api"
    audit_actor_role: str = "model-registry"

    def __post_init__(self) -> None:
        # Seed the ordered pre-activation hook chain (§2.1). Each reserved
        # mount point starts as a no-op so existing lifecycle behavior is
        # preserved byte-for-byte until Change 5 (state clone) or Change 8
        # (station flag flip) registers a real hook. Bypasses the frozen
        # constraint via ``object.__setattr__`` because hook membership is
        # instance-scoped runtime state, not part of dataclass identity.
        object.__setattr__(
            self,
            "_pre_activation_hooks",
            {name: _default_no_op_hook for name in PRE_ACTIVATION_HOOK_MOUNT_POINTS},
        )
        # Seed the post-commit manifest publisher (§2.2). Starts as a
        # no-op so existing lifecycle behavior is byte-for-byte preserved
        # until production wiring registers the real
        # ``publish_scheduler_registry_manifest`` bridge. Same
        # frozen-dataclass rationale as the pre-activation hook chain.
        object.__setattr__(
            self,
            "_post_commit_manifest_publisher",
            _default_no_op_manifest_publisher,
        )

    def register_pre_activation_hook(self, mount_point: str, hook: PreActivationHook) -> None:
        """Register a hook at a reserved mount point (§2.1).

        Raises ``InvalidPayloadError`` for an unknown mount point so a
        typo cannot silently install a hook that never fires.
        """
        if mount_point not in PRE_ACTIVATION_HOOK_MOUNT_POINTS:
            raise InvalidPayloadError(
                f"Unknown pre-activation mount point: {mount_point!r}; "
                f"valid mount points are {PRE_ACTIVATION_HOOK_MOUNT_POINTS}"
            )
        # ``_pre_activation_hooks`` is a mutable dict on the instance
        # (seeded in ``__post_init__``); mutating it is compatible with
        # the ``frozen=True`` dataclass because we mutate the dict, not
        # reassign the field.
        self._pre_activation_hooks[mount_point] = hook

    def _dispatch_pre_activation_hooks(
        self,
        cursor: Any,
        activation_context: ModelActivationContext,
    ) -> None:
        """Run the ordered hook chain inside the lifecycle transaction.

        Iterates ``PRE_ACTIVATION_HOOK_MOUNT_POINTS`` in declared order;
        a raising hook propagates so the whole transaction rolls back
        (fail-closed, no "activated but hooks not run" intermediate
        state — D7).
        """
        # Defensive lookup: instances constructed via unusual paths that
        # skip ``__post_init__`` still get the default chain.
        hooks: Mapping[str, PreActivationHook] = getattr(
            self, "_pre_activation_hooks", {}
        )
        for mount_point in PRE_ACTIVATION_HOOK_MOUNT_POINTS:
            hook = hooks.get(mount_point, _default_no_op_hook)
            hook(cursor, activation_context)

    def register_post_commit_manifest_publisher(
        self, publisher: PostCommitManifestPublisher
    ) -> None:
        """Register the post-commit manifest publisher (§2.2).

        Overrides the default no-op with a real callable that receives
        every committed dispatch-set-changing transition. Production
        wiring binds this to
        ``services.orchestrator.scheduler_file_providers.publish_scheduler_registry_manifest``;
        tests bind a recording stub. Only one publisher is supported —
        the post-commit tail is a single seam, not an ordered chain
        (contrast with the pre-activation hook chain).
        """
        object.__setattr__(self, "_post_commit_manifest_publisher", publisher)

    def _dispatch_post_commit_manifest_publish(
        self, publish_context: PostCommitPublishContext
    ) -> None:
        """Invoke the post-commit manifest publisher.

        Called AFTER the lifecycle transaction commits, so the manifest
        reflects the committed DB state and never fires on a rolled-back
        transaction (preflight-blocked / hook-aborted / audit-persistence
        failure). A raising publisher propagates — the transition is
        already committed, so surfacing the failure to the caller is the
        correct behavior; the operator sees the divergence between DB
        and manifest immediately instead of silently.
        """
        publisher: PostCommitManifestPublisher = getattr(
            self, "_post_commit_manifest_publisher", _default_no_op_manifest_publisher
        )
        publisher(publish_context)

    @classmethod
    def from_env(cls) -> PsycopgModelRegistryStore:
        return cls(default_database_url())

    def create_basin_with_version(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="basins",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        basin_version = dict(payload["basin_version"])
        basin_version_id = build_versioned_id(
            str(payload["basin_id"]),
            basin_version.get("version_label"),
            basin_version.get("basin_version_id"),
        )
        geom_wkt = geometry_to_wkt(basin_version["geom"], "MultiPolygon")
        with self._transaction() as cursor:
            if self._exists(cursor, "core.basin", "basin_id", payload["basin_id"]):
                raise DuplicateResourceError(f"basin_id already exists: {payload['basin_id']}")
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (
                    payload["basin_id"],
                    payload["basin_name"],
                    payload.get("basin_group"),
                    payload.get("description"),
                ),
            )
            basin = dict(cursor.fetchone())
            basin_version_row = self._insert_basin_version(
                cursor,
                basin_id=payload["basin_id"],
                basin_version_id=basin_version_id,
                payload=basin_version,
                geom_wkt=geom_wkt,
            )
        return {"basin": basin, "basin_version": basin_version_row}

    def create_basin_version(
        self,
        basin_id: str,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id=basin_id,
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        basin_version_id = build_versioned_id(basin_id, payload.get("version_label"), payload.get("basin_version_id"))
        geom_wkt = geometry_to_wkt(payload["geom"], "MultiPolygon")
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin", "basin_id", basin_id):
                raise MissingResourceError(f"basin_id not found: {basin_id}")
            return self._insert_basin_version(
                cursor,
                basin_id=basin_id,
                basin_version_id=basin_version_id,
                payload=payload,
                geom_wkt=geom_wkt,
            )

    def list_basins(
        self, *, limit: int, offset: int, has_display_product: bool = False
    ) -> list[dict[str, Any]]:
        # When has_display_product is true, restrict to basins that have at least
        # one run that the latest-product candidate query could surface. We align
        # discovery with availability on the three run-level dimensions the
        # candidate query also filters on (forecast_store latest-product):
        #   - status ∈ QHH_LATEST_READY_RUN_STATUSES (single source of truth)
        #   - run_type = 'forecast'
        #   - cycle_time IS NOT NULL
        # The source (GFS/IFS) and run_id dimensions are intentionally NOT pushed
        # down here: discovery is source-agnostic (a basin with any forecast run
        # should be discoverable); the concrete source/run_id is resolved later by
        # the latest-product query. So this stays a superset on source but is exact
        # on status/run_type/cycle_time.
        display_filter = ""
        parameters: tuple[Any, ...] = (limit, offset)
        if has_display_product:
            display_filter = """
                WHERE EXISTS (
                    SELECT 1
                    FROM core.basin_version bv
                    JOIN hydro.hydro_run hr
                        ON hr.basin_version_id = bv.basin_version_id
                    WHERE bv.basin_id = core.basin.basin_id
                      AND hr.status = ANY(%s::hydro.run_status[])
                      AND hr.run_type = 'forecast'
                      AND hr.cycle_time IS NOT NULL
                )
                """
            parameters = (list(QHH_LATEST_READY_RUN_STATUSES), limit, offset)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT basin_id, basin_name, basin_group, description, created_at
                FROM core.basin
                {display_filter}
                ORDER BY basin_name, basin_id
                LIMIT %s OFFSET %s
                """,
                parameters,
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_basin_versions(self, *, basin_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin", "basin_id", basin_id):
                raise MissingResourceError(f"basin_id not found: {basin_id}")
            cursor.execute(
                """
                SELECT
                    basin_version_id,
                    basin_id,
                    version_label,
                    ST_AsGeoJSON(geom)::json AS geom,
                    active_flag,
                    valid_from,
                    valid_to,
                    source_uri,
                    checksum,
                    created_at
                FROM core.basin_version
                WHERE basin_id = %s
                ORDER BY active_flag DESC, created_at DESC, basin_version_id
                LIMIT %s OFFSET %s
                """,
                (basin_id, limit, offset),
            )
            return [_basin_version_public_projection(row) for row in cursor.fetchall()]

    def create_river_network(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="river-networks",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        segments = list(payload.get("segments") or [])
        segment_count = int(payload.get("segment_count") if payload.get("segment_count") is not None else len(segments))
        if segment_count != len(segments):
            raise InvalidPayloadError("segment_count must equal the number of supplied river segments.")

        river_network_version_id = build_versioned_id(
            f"{payload['basin_version_id']}_rivnet",
            payload.get("version_label"),
            payload.get("river_network_version_id"),
        )
        # PR 2 (feat-reach-geom-from-river-shp): the reach-source contract
        # writes one single-part LineString per reach; SQL-side ST_Multi (see
        # the INSERT template below) wraps it into the column's required
        # MultiLineString shape.
        segment_rows = [
            (
                segment["river_segment_id"],
                river_network_version_id,
                segment.get("segment_order"),
                segment.get("downstream_segment_id"),
                segment.get("length_m"),
                geometry_to_wkt(segment["geom"], "LineString"),
                self._json(segment.get("properties_json") or {}),
            )
            for segment in segments
        ]

        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id,
                    basin_version_id,
                    version_label,
                    segment_count,
                    source_uri,
                    checksum
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    river_network_version_id,
                    payload["basin_version_id"],
                    payload["version_label"],
                    segment_count,
                    payload.get("source_uri"),
                    payload.get("checksum"),
                ),
            )
            network = dict(cursor.fetchone())
            if segment_rows:
                self._execute_values(
                    cursor,
                    """
                    INSERT INTO core.river_segment (
                        river_segment_id,
                        river_network_version_id,
                        segment_order,
                        downstream_segment_id,
                        length_m,
                        geom,
                        properties_json
                    )
                    VALUES %s
                    """,
                    segment_rows,
                    # geom is geometry(MultiLineString, 4490) (000036). ST_Multi wraps a
                    # LineString payload into a single-part MultiLineString so the legacy
                    # LineString write contract still inserts; a MultiLineString WKT passes
                    # through unchanged.
                    template="(%s, %s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
                )
        return {"river_network_version": network, "segment_count": segment_count}

    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None = None,
        search: str | None = None,
        stream_order_min: int | None = None,
        stream_order_max: int | None = None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        # PR 2 Path C (feat-reach-geom-from-river-shp / spec
        # "River segment map query returns segment-level features sliced
        # from parent reach polyline"): when this basin/RNV has crosswalk
        # rows from gis/seg.shp, return segment-level features whose
        # geometry is the result of ST_LineSubstring against the parent
        # reach polyline. DB row granularity stays reach (1 row per
        # .sp.riv reach); the segment-level identifier is derived from
        # the crosswalk external_id so the frontend
        # promoteId='river_segment_id' contract is preserved verbatim
        # (OQ2: M11MapLibreSurface.tsx hover/popup/colour/forecast paths
        # all key on segment-level river_segment_id).
        #
        # Dispatch granularity is per-RNV: a basin that mixes Path C and
        # legacy RNVs must NOT classify the whole basin as Path C (would
        # send the legacy RNV down the slice path and emit an empty
        # FeatureCollection for those reaches). We probe each RNV
        # individually; if any has crosswalk rows we go through the
        # slice path which then gates per-reach by RNV crosswalk presence.
        candidate_rnvs = self._list_river_segment_rnv_ids(
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
        rnv_has_crosswalk = {
            rnv_id: self._has_segment_crosswalk_for_rnv(rnv_id)
            for rnv_id in candidate_rnvs
        }
        if any(rnv_has_crosswalk.values()):
            return self._list_river_segments_segment_slice(
                basin_version_id=basin_version_id,
                river_network_version_id=river_network_version_id,
                search=search,
                stream_order_min=stream_order_min,
                stream_order_max=stream_order_max,
                limit=limit,
                offset=offset,
                rnv_has_crosswalk=rnv_has_crosswalk,
            )
        filters = ["rnv.basin_version_id = %s"]
        params: list[Any] = [basin_version_id]
        if river_network_version_id is not None:
            filters.append("rnv.river_network_version_id = %s")
            params.append(river_network_version_id)

        # search: parameterised ILIKE over the segment identifier and the human
        # readable name stored in properties_json. Escapes %/_ so caller input is
        # treated literally and never widens the LIKE pattern (no injection face).
        normalized_search = search.strip() if search is not None else ""
        if normalized_search:
            like_pattern = f"%{_escape_like(normalized_search)}%"
            filters.append(
                "(rs.river_segment_id ILIKE %s ESCAPE '\\' "
                "OR COALESCE(rs.properties_json->>'name', '') ILIKE %s ESCAPE '\\' "
                "OR COALESCE(rs.properties_json->>'segment_name', '') ILIKE %s ESCAPE '\\')"
            )
            params.extend([like_pattern, like_pattern, like_pattern])

        # stream_order filter lands on core.river_segment.segment_order. The column
        # is nullable, so rows without a populated order are excluded from a filtered
        # subset (the correct "filter by stream order" semantic) rather than erroring.
        if stream_order_min is not None:
            filters.append("rs.segment_order >= %s")
            params.append(stream_order_min)
        if stream_order_max is not None:
            filters.append("rs.segment_order <= %s")
            params.append(stream_order_max)

        where_clause = " AND ".join(filters)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                WITH matching AS (
                    SELECT
                        rs.river_segment_id,
                        rs.segment_order,
                        rs.geom,
                        CASE
                            WHEN COALESCE(rs.properties_json->>'shud_output_river', 'false') = 'true' THEN 0
                            ELSE 1
                        END AS display_priority
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE {where_clause}
                ),
                ordered_renderable AS (
                    SELECT
                        river_segment_id,
                        SUM(ST_NPoints(geom)) OVER (
                            ORDER BY display_priority, COALESCE(segment_order, 2147483647), river_segment_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS running_coordinate_count
                    FROM matching
                    WHERE geom IS NOT NULL
                      AND ST_NPoints(geom) BETWEEN 2 AND %s
                      AND ST_NDims(geom) <= %s
                )
                SELECT
                    (SELECT COUNT(*) FROM matching) AS total,
                    COUNT(*) FILTER (WHERE running_coordinate_count <= %s) AS feature_total
                FROM ordered_renderable
                """,
                tuple([
                    *params,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
                    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                ]),
            )
            counts = cursor.fetchone()
            total = int(counts["total"])
            feature_total = int(counts["feature_total"])
            cursor.execute(
                f"""
                WITH ordered_renderable AS (
                    SELECT
                        rs.river_segment_id,
                        rs.river_network_version_id,
                        rnv.basin_version_id,
                        rs.segment_order,
                        rs.downstream_segment_id,
                        rs.length_m,
                        rs.properties_json,
                        rs.geom,
                        CASE
                            WHEN COALESCE(rs.properties_json->>'shud_output_river', 'false') = 'true' THEN 0
                            ELSE 1
                        END AS display_priority,
                        ST_NPoints(rs.geom) AS coordinate_count,
                        SUM(ST_NPoints(rs.geom)) OVER (
                            ORDER BY
                                CASE
                                    WHEN COALESCE(rs.properties_json->>'shud_output_river', 'false') = 'true' THEN 0
                                    ELSE 1
                                END,
                                COALESCE(rs.segment_order, 2147483647),
                                rs.river_segment_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                        ) AS running_coordinate_count
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE {where_clause}
                      AND rs.geom IS NOT NULL
                      AND ST_NPoints(rs.geom) BETWEEN 2 AND %s
                      AND ST_NDims(rs.geom) <= %s
                ),
                renderable AS (
                    SELECT *
                    FROM ordered_renderable
                    WHERE running_coordinate_count <= %s
                )
                SELECT
                    river_segment_id,
                    river_network_version_id,
                    basin_version_id,
                    segment_order,
                    downstream_segment_id,
                    length_m,
                    properties_json,
                    ST_AsGeoJSON(geom)::json AS geometry
                FROM renderable
                ORDER BY display_priority, COALESCE(segment_order, 2147483647), river_segment_id
                LIMIT %s OFFSET %s
                """,
                tuple([
                    *params,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
                    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
                    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                    limit,
                    offset,
                ]),
            )
            rows = [dict(row) for row in cursor.fetchall()]

        features = []
        for row in rows:
            properties_json = row.get("properties_json") or {}
            if isinstance(properties_json, str):
                try:
                    properties_json = json.loads(properties_json)
                except json.JSONDecodeError:
                    properties_json = {}
            properties = dict(properties_json) if isinstance(properties_json, Mapping) else {}
            stream_order = row.get("segment_order")
            name = properties.get("name") or properties.get("segment_name") or row["river_segment_id"]
            properties.update(
                {
                    "segment_id": str(row["river_segment_id"]),
                    "river_segment_id": str(row["river_segment_id"]),
                    "basin_version_id": str(row["basin_version_id"]),
                    "river_network_version_id": str(row["river_network_version_id"]),
                    "name": str(name),
                    "stream_order": int(stream_order) if stream_order is not None else 1,
                    "segment_order": int(stream_order) if stream_order is not None else None,
                    "downstream_segment_id": row.get("downstream_segment_id"),
                    "length_m": float(row["length_m"]) if row.get("length_m") is not None else None,
                }
            )
            features.append(
                {
                    "type": "Feature",
                    "properties": properties,
                    "geometry": row["geometry"],
                }
            )

        collection = {
            "type": "FeatureCollection",
            "features": features,
            "total": total,
            "feature_total": feature_total,
            "limit": limit,
            "offset": offset,
        }
        _enforce_river_segment_serialized_budget(
            collection,
            max_bytes=RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
            scope="collection",
        )
        return collection

    def _list_river_segment_rnv_ids(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
    ) -> list[str]:
        """Collect distinct river_network_version_id values for this basin/RNV scope.

        Returns the set of RNV ids whose reaches would be returned by the
        legacy reach-level query. Used to drive the per-RNV crosswalk probe
        in ``list_river_segments`` so a mixed-RNV basin (one Path C, one
        legacy) is dispatched correctly.
        """

        params: list[Any] = [basin_version_id]
        rnv_filter = ""
        if river_network_version_id is not None:
            rnv_filter = " AND rnv.river_network_version_id = %s"
            params.append(river_network_version_id)
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT rs.river_network_version_id
                FROM core.river_segment rs
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = rs.river_network_version_id
                WHERE rnv.basin_version_id = %s
                  {rnv_filter}
                """,
                tuple(params),
            )
            rows = cursor.fetchall() or []
        result: list[str] = []
        for row in rows:
            if isinstance(row, Mapping):
                value = row.get("river_network_version_id")
            else:
                try:
                    value = row[0]
                except (IndexError, KeyError, TypeError):
                    value = None
            if value is None:
                continue
            result.append(value)
        return result

    def _has_segment_crosswalk_for_rnv(self, river_network_version_id: Any) -> bool:
        """Detect whether this RNV has PR-2-style crosswalk rows.

        Used to switch ``list_river_segments`` between the legacy
        reach-level path (no crosswalk -> emit existing rows verbatim) and
        the Path C segment-slice path (crosswalk present -> emit segments
        sliced from parent reach polylines via ST_LineSubstring).

        Probes by ``river_network_version_id`` (NOT ``basin_version_id``)
        because the slice path also operates per-RNV; classifying a whole
        basin would dispatch mixed-RNV basins through the wrong code path
        and produce empty FeatureCollections for legacy RNVs.

        Real DB errors propagate; callers must not silently fall back to
        the legacy path on probe failure -- that would break the frontend
        ``promoteId='river_segment_id'`` contract.
        """

        with self._transaction() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM core.river_segment_crosswalk rsc
                    WHERE rsc.river_network_version_id = %s
                      AND rsc.source = 'basins_seg_shp'
                ) AS exists
                """,
                (river_network_version_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return False
        if isinstance(row, Mapping):
            return bool(row.get("exists", False))
        try:
            return bool(row[0])
        except (KeyError, IndexError, TypeError):
            return False

    def _list_river_segments_segment_slice(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        search: str | None,
        stream_order_min: int | None,
        stream_order_max: int | None,
        limit: int,
        offset: int,
        rnv_has_crosswalk: Mapping[Any, bool] | None = None,
    ) -> dict[str, Any]:
        """Return segment-level features sliced from parent reach polylines.

        Path C (spec D7): for each crosswalk row in segment_order under its
        parent reach, compute cumulative ``length_m`` proportions to derive
        ``start_fraction``/``end_fraction``, then call PostGIS
        ``ST_LineSubstring(reach_geom, start_fraction, end_fraction)`` to
        carve a sub-polyline out of the parent reach. The last segment in
        each reach saturates its ``end_fraction`` to ``1.0`` to absorb the
        residual between the sum of ``sp.rivseg`` segment lengths and the
        reach ``Length`` (floating-point + R-side preprocessing drift,
        ≈ 0.02 m on qhh).

        Length-less ``seg.shp`` (qhh's seg.shp has no Length field, so
        ``properties_json.length_m`` is ``None``) falls back to equal-length
        partitioning: each segment occupies ``1/N`` of the parent reach
        polyline, where ``N`` is the number of crosswalk rows for that
        reach.

        Output identity: ``river_segment_id = "<model>_seg_<iRiv>_<iEle>"``
        is derived from the crosswalk ``external_id`` so the frontend
        ``promoteId='river_segment_id'`` contract (verified in OQ2) keeps
        working. The DB-level ``<model>_reach_<iRiv:06d>`` ID is not
        exposed in this response.

        ``rnv_has_crosswalk`` is the per-RNV probe map computed in
        ``list_river_segments``. For reaches whose RNV has no crosswalk we
        emit the reach-level feature verbatim (legacy shape) so a mixed
        basin renders both planes without an empty FeatureCollection.
        """

        crosswalk_map: dict[Any, bool] = dict(rnv_has_crosswalk or {})

        rnv_filter = ""
        params: list[Any] = [basin_version_id]
        if river_network_version_id is not None:
            rnv_filter = " AND rnv.river_network_version_id = %s"
            params.append(river_network_version_id)

        normalized_search = search.strip() if search is not None else ""
        like_pattern = (
            f"%{_escape_like(normalized_search)}%" if normalized_search else None
        )
        with self._transaction() as cursor:
            # Pull all reaches for this basin -- one row per reach (PR 2
            # row granularity), with the geom kept in DB for the slice
            # query below. We need both geom and length to:
            # (a) compute the slice fractions per segment in this reach,
            # (b) feed ST_LineSubstring with a stable reach geom.
            # We also fetch reach-level columns so a per-reach RNV-without-
            # crosswalk gate can emit the legacy reach-level feature.
            cursor.execute(
                f"""
                SELECT
                    rs.river_segment_id AS reach_segment_id,
                    rs.river_network_version_id,
                    rnv.basin_version_id,
                    rs.segment_order,
                    rs.downstream_segment_id,
                    rs.length_m,
                    rs.properties_json,
                    ST_AsBinary(rs.geom) AS geom_wkb,
                    ST_AsGeoJSON(rs.geom)::json AS geometry
                FROM core.river_segment rs
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = rs.river_network_version_id
                WHERE rnv.basin_version_id = %s
                  AND COALESCE(rs.properties_json->>'shud_output_river', 'false') <> 'true'
                  AND rs.geom IS NOT NULL
                  {rnv_filter}
                """,
                tuple(params),
            )
            reach_rows = [dict(row) for row in cursor.fetchall()]
            if not reach_rows:
                empty: dict[str, Any] = {
                    "type": "FeatureCollection",
                    "features": [],
                    "total": 0,
                    "feature_total": 0,
                    "limit": limit,
                    "offset": offset,
                }
                _enforce_river_segment_serialized_budget(
                    empty,
                    max_bytes=RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
                    scope="collection",
                )
                return empty
            reach_by_id = {row["reach_segment_id"]: row for row in reach_rows}
            # Per-RNV gate: any RNV whose crosswalk probe returned False is
            # rendered via the legacy reach-level feature shape. We pull
            # crosswalk rows only for the RNVs that flagged true (and any
            # RNVs we did not probe — defaulting to "look it up"), so a
            # mixed-RNV basin never feeds the slice query a legacy RNV.
            rnv_ids = sorted(
                {
                    row["river_network_version_id"]
                    for row in reach_rows
                    if crosswalk_map.get(row["river_network_version_id"], True)
                }
            )

            # Pull all crosswalk rows for these RNV ids, ordered by parent
            # reach + segment_order. Sorting by segment_order in SQL keeps
            # the per-reach grouping below deterministic without us having
            # to re-sort in Python.
            if rnv_ids:
                cursor.execute(
                    """
                    SELECT
                        river_segment_id AS reach_segment_id,
                        external_id,
                        properties_json
                    FROM core.river_segment_crosswalk
                    WHERE river_network_version_id = ANY(%s)
                      AND source = 'basins_seg_shp'
                    ORDER BY river_segment_id,
                             COALESCE((properties_json->>'segment_order')::int, 2147483647),
                             external_id
                    """,
                    (rnv_ids,),
                )
                crosswalk_rows = [dict(row) for row in cursor.fetchall()]
            else:
                crosswalk_rows = []

            # Group crosswalk rows by parent reach_id and compute cumulative
            # length proportions for each segment. The last segment's
            # end_fraction is forced to 1.0 to saturate floating-point
            # drift between sum(length_m) and the parent reach Length.
            grouped: dict[str, list[dict[str, Any]]] = {}
            for crosswalk_row in crosswalk_rows:
                reach_id = crosswalk_row["reach_segment_id"]
                grouped.setdefault(reach_id, []).append(crosswalk_row)

            slice_requests: list[dict[str, Any]] = []
            for reach_id, members in grouped.items():
                if reach_id not in reach_by_id:
                    # The crosswalk insert path is FK-protected, but a
                    # cross-RNV reach reference still warrants skipping
                    # rather than crashing the whole endpoint.
                    continue
                lengths: list[float | None] = []
                for member in members:
                    props = member.get("properties_json") or {}
                    if isinstance(props, str):
                        try:
                            props = json.loads(props)
                        except json.JSONDecodeError:
                            props = {}
                    raw_length = props.get("length_m") if isinstance(props, Mapping) else None
                    try:
                        lengths.append(None if raw_length is None else float(raw_length))
                    except (TypeError, ValueError):
                        lengths.append(None)
                non_null_lengths = [length for length in lengths if length is not None and length > 0]
                if non_null_lengths and len(non_null_lengths) == len(members):
                    total_length = sum(non_null_lengths)
                    cumulative = 0.0
                    fractions: list[tuple[float, float]] = []
                    for index, length in enumerate(non_null_lengths):
                        start_fraction = cumulative / total_length
                        cumulative += length
                        end_fraction = cumulative / total_length
                        if index == len(non_null_lengths) - 1:
                            end_fraction = 1.0
                        # Clamp into [0.0, 1.0] to defend against any
                        # cumulative drift that would otherwise hand
                        # ST_LineSubstring a value > 1 (would error).
                        start_fraction = max(0.0, min(1.0, start_fraction))
                        end_fraction = max(start_fraction, min(1.0, end_fraction))
                        fractions.append((start_fraction, end_fraction))
                else:
                    # length_m=None fallback: equal partition. Each segment
                    # occupies 1/N of the parent reach polyline (the qhh
                    # seg.shp has no Length field; see fixture README).
                    member_count = len(members)
                    fractions = []
                    for index in range(member_count):
                        start_fraction = index / member_count
                        end_fraction = (
                            1.0 if index == member_count - 1 else (index + 1) / member_count
                        )
                        fractions.append((start_fraction, end_fraction))
                for member, (start_fraction, end_fraction) in zip(members, fractions, strict=True):
                    slice_requests.append(
                        {
                            "reach_segment_id": reach_id,
                            "external_id": member["external_id"],
                            "properties_json": member.get("properties_json") or {},
                            "start_fraction": start_fraction,
                            "end_fraction": end_fraction,
                            "geom_wkb": reach_by_id[reach_id]["geom_wkb"],
                            "river_network_version_id": reach_by_id[reach_id][
                                "river_network_version_id"
                            ],
                            "basin_version_id": reach_by_id[reach_id]["basin_version_id"],
                        }
                    )

            # Build per-segment features. We dispatch each ST_LineSubstring
            # call individually because the fractions vary per row;
            # qhh-scale basins (~3.7k segments) handle this in batches by
            # the API layer. Optimisation to a single SQL UNNEST is in
            # scope for follow-up (PR 6 perf check); the unit-test
            # correctness is what this PR pins down.
            slice_features: list[dict[str, Any]] = []
            model_id_pattern = re.compile(r"^(?P<model>.+)_reach_(?P<index>\d+)$")
            for slice_request in slice_requests:
                cursor.execute(
                    """
                    SELECT ST_AsGeoJSON(
                        ST_LineSubstring(
                            ST_GeomFromWKB(%s, 4490),
                            %s,
                            %s
                        )
                    )::json AS geometry
                    """,
                    (
                        slice_request["geom_wkb"],
                        slice_request["start_fraction"],
                        slice_request["end_fraction"],
                    ),
                )
                geometry_row = cursor.fetchone()
                if geometry_row is None or geometry_row["geometry"] is None:
                    continue
                props_in = slice_request["properties_json"]
                if isinstance(props_in, str):
                    try:
                        props_in = json.loads(props_in)
                    except json.JSONDecodeError:
                        props_in = {}
                if not isinstance(props_in, Mapping):
                    props_in = {}
                iriv = props_in.get("iRiv")
                iele = props_in.get("iEle")
                if iriv is None or iele is None:
                    parts = str(slice_request["external_id"]).split(":")
                    if len(parts) == 2:
                        try:
                            iriv = int(parts[0])
                            iele = int(parts[1])
                        except (TypeError, ValueError):
                            iriv = iele = None
                if iriv is None or iele is None:
                    # Skip rows that cannot produce a stable segment ID
                    # rather than emit "None"/"None" placeholders that
                    # collide under MapLibre promoteId='river_segment_id'.
                    continue
                model_match = model_id_pattern.match(slice_request["reach_segment_id"])
                model_id = model_match.group("model") if model_match else slice_request["reach_segment_id"]
                segment_river_id = f"{model_id}_seg_{iriv}_{iele}"
                if like_pattern is not None:
                    haystack = segment_river_id.lower()
                    if normalized_search.lower() not in haystack:
                        continue
                segment_order = props_in.get("segment_order")
                try:
                    segment_order_int = (
                        int(segment_order) if segment_order is not None else None
                    )
                except (TypeError, ValueError):
                    segment_order_int = None
                if stream_order_min is not None and (
                    segment_order_int is None or segment_order_int < stream_order_min
                ):
                    continue
                if stream_order_max is not None and (
                    segment_order_int is None or segment_order_int > stream_order_max
                ):
                    continue
                segment_length = props_in.get("length_m") if isinstance(props_in, Mapping) else None
                try:
                    segment_length_value = (
                        None if segment_length is None else float(segment_length)
                    )
                except (TypeError, ValueError):
                    segment_length_value = None
                slice_features.append(
                    {
                        "type": "Feature",
                        "id": segment_river_id,
                        "geometry": geometry_row["geometry"],
                        "properties": {
                            "segment_id": segment_river_id,
                            "river_segment_id": segment_river_id,
                            "basin_version_id": str(slice_request["basin_version_id"]),
                            "river_network_version_id": str(
                                slice_request["river_network_version_id"]
                            ),
                            "name": segment_river_id,
                            "stream_order": segment_order_int
                            if segment_order_int is not None
                            else 1,
                            "segment_order": segment_order_int,
                            "length_m": segment_length_value,
                            "iRiv": iriv,
                            "iEle": iele,
                            "reach_segment_id": str(slice_request["reach_segment_id"]),
                        },
                    }
                )

            # Per-RNV fallback: for reaches whose RNV has no crosswalk we
            # emit the legacy reach-level feature shape so the frontend
            # never sees an empty layer for that RNV in a mixed basin.
            for reach_row in reach_rows:
                if crosswalk_map.get(reach_row["river_network_version_id"], True):
                    continue
                legacy_feature = self._reach_row_to_legacy_feature(
                    reach_row,
                    like_pattern=like_pattern,
                    normalized_search=normalized_search,
                    stream_order_min=stream_order_min,
                    stream_order_max=stream_order_max,
                )
                if legacy_feature is not None:
                    slice_features.append(legacy_feature)

        # Total counts every renderable feature (slice + legacy fallback);
        # Path C does no coordinate-budget filtering so total equals the
        # full renderable count. ``feature_total`` mirrors the legacy
        # semantic (renderable count independent of pagination) so callers
        # using ``feature_total == total`` as a "no truncation" check stay
        # correct. ``features`` carries the paginated slice. limit/offset
        # are applied in Python because we already had to materialise the
        # full list to do per-reach grouping + fraction computation.
        total = len(slice_features)
        paged = slice_features[offset : offset + limit]
        feature_total = total
        collection: dict[str, Any] = {
            "type": "FeatureCollection",
            "features": paged,
            "total": total,
            "feature_total": feature_total,
            "limit": limit,
            "offset": offset,
        }
        _enforce_river_segment_serialized_budget(
            collection,
            max_bytes=RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
            scope="collection",
        )
        return collection

    def _reach_row_to_legacy_feature(
        self,
        reach_row: Mapping[str, Any],
        *,
        like_pattern: str | None,
        normalized_search: str,
        stream_order_min: int | None,
        stream_order_max: int | None,
    ) -> dict[str, Any] | None:
        """Render a reach row as a legacy reach-level GeoJSON feature.

        Used by the segment-slice path to fall back per-RNV when an RNV
        has no crosswalk; produces the exact same feature shape as the
        legacy ``list_river_segments`` reach query so a mixed-RNV basin
        renders both planes consistently. Returns ``None`` when the row
        is filtered out by search or stream_order constraints.
        """

        properties_json = reach_row.get("properties_json") or {}
        if isinstance(properties_json, str):
            try:
                properties_json = json.loads(properties_json)
            except json.JSONDecodeError:
                properties_json = {}
        properties = (
            dict(properties_json) if isinstance(properties_json, Mapping) else {}
        )
        river_segment_id = str(reach_row["reach_segment_id"])
        stream_order = reach_row.get("segment_order")
        try:
            stream_order_int = (
                int(stream_order) if stream_order is not None else None
            )
        except (TypeError, ValueError):
            stream_order_int = None
        if stream_order_min is not None and (
            stream_order_int is None or stream_order_int < stream_order_min
        ):
            return None
        if stream_order_max is not None and (
            stream_order_int is None or stream_order_int > stream_order_max
        ):
            return None
        name = (
            properties.get("name")
            or properties.get("segment_name")
            or river_segment_id
        )
        if like_pattern is not None:
            needle = normalized_search.lower()
            if (
                needle not in river_segment_id.lower()
                and needle not in str(name).lower()
            ):
                return None
        length_m = reach_row.get("length_m")
        properties.update(
            {
                "segment_id": river_segment_id,
                "river_segment_id": river_segment_id,
                "basin_version_id": str(reach_row["basin_version_id"]),
                "river_network_version_id": str(
                    reach_row["river_network_version_id"]
                ),
                "name": str(name),
                "stream_order": stream_order_int if stream_order_int is not None else 1,
                "segment_order": stream_order_int,
                "downstream_segment_id": reach_row.get("downstream_segment_id"),
                "length_m": float(length_m) if length_m is not None else None,
            }
        )
        return {
            "type": "Feature",
            "properties": properties,
            "geometry": reach_row.get("geometry"),
        }

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                """
                WITH selected AS (
                    SELECT
                        rs.river_segment_id,
                        rs.river_network_version_id,
                        rs.segment_order,
                        rs.downstream_segment_id,
                        rs.length_m,
                        rs.geom,
                        rs.properties_json,
                        rs.created_at,
                        ST_NPoints(rs.geom) AS coordinate_count,
                        ST_NDims(rs.geom) AS coordinate_dimensions
                    FROM core.river_segment rs
                    JOIN core.river_network_version rnv
                      ON rnv.river_network_version_id = rs.river_network_version_id
                    WHERE rnv.basin_version_id = %s
                      AND rs.river_segment_id = %s
                      AND rs.river_network_version_id = %s
                )
                SELECT
                    river_segment_id,
                    river_network_version_id,
                    segment_order,
                    downstream_segment_id,
                    length_m,
                    ST_AsGeoJSON(geom)::json AS geom,
                    properties_json,
                    created_at
                FROM selected
                WHERE geom IS NOT NULL
                  AND coordinate_count BETWEEN 2 AND %s
                  AND coordinate_dimensions <= %s
                """,
                (
                    basin_version_id,
                    segment_id,
                    river_network_version_id,
                    SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES,
                    SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
                ),
            )
        if row is None:
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        detail = _river_segment_detail(row)
        _enforce_river_segment_serialized_budget(
            detail,
            max_bytes=RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
            scope="detail",
        )
        return detail

    def create_mesh_version(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="mesh-versions",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        mesh_version_id = build_versioned_id(
            f"{payload['basin_version_id']}_mesh",
            payload.get("version_label"),
            payload.get("mesh_version_id"),
        )
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id,
                    basin_version_id,
                    version_label,
                    mesh_uri,
                    checksum,
                    properties_json
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    mesh_version_id,
                    payload["basin_version_id"],
                    payload["version_label"],
                    payload["mesh_uri"],
                    payload.get("checksum"),
                    self._json(payload.get("properties_json") or {}),
                ),
            )
            return dict(cursor.fetchone())

    def create_model(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="models",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        if bool(payload.get("active_flag", False)):
            raise InvalidPayloadError(
                "active_flag=true is not accepted when creating models; use a lifecycle activate operation."
            )
        with self._transaction() as cursor:
            if not self._exists(cursor, "core.basin_version", "basin_version_id", payload["basin_version_id"]):
                raise InvalidReferenceError(f"basin_version_id does not exist: {payload['basin_version_id']}")
            network = self._fetch_optional(
                cursor,
                """
                SELECT basin_version_id
                FROM core.river_network_version
                WHERE river_network_version_id = %s
                """,
                (payload["river_network_version_id"],),
            )
            if network is None:
                raise InvalidReferenceError(
                    f"river_network_version_id does not exist: {payload['river_network_version_id']}"
                )
            if network["basin_version_id"] != payload["basin_version_id"]:
                raise InvalidReferenceError("river_network_version_id does not belong to basin_version_id.")
            mesh = self._fetch_optional(
                cursor,
                """
                SELECT basin_version_id
                FROM core.mesh_version
                WHERE mesh_version_id = %s
                """,
                (payload["mesh_version_id"],),
            )
            if mesh is None:
                raise InvalidReferenceError(f"mesh_version_id does not exist: {payload['mesh_version_id']}")
            if mesh["basin_version_id"] != payload["basin_version_id"]:
                raise InvalidReferenceError("mesh_version_id does not belong to basin_version_id.")

            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    rshud_code_version,
                    autoshud_code_version,
                    container_image,
                    model_package_uri,
                    active_flag,
                    lifecycle_state,
                    resource_profile
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    payload["model_id"],
                    payload["basin_version_id"],
                    payload["river_network_version_id"],
                    payload["mesh_version_id"],
                    payload["calibration_version_id"],
                    payload["shud_code_version"],
                    payload.get("rshud_code_version"),
                    payload.get("autoshud_code_version"),
                    payload.get("container_image"),
                    payload["model_package_uri"],
                    False,
                    "inactive",
                    self._json(payload.get("resource_profile") or {}),
                ),
            )
            return dict(cursor.fetchone())

    def set_model_active(
        self,
        model_id: str,
        active: bool,
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.model_lifecycle_operation(
            model_id,
            operation="activate" if active else "deactivate",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
            request_id=request_id,
        )
        return result["model"]

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

        # §2.2 post-commit tail: fire the manifest publisher ONLY on a
        # committed dispatch-set-changing transition. All non-committing
        # paths (preflight-blocked / already-current / hook-aborted /
        # audit-persistence-failed) either early-returned above or left
        # ``publish_context`` as None because the trigger was set only
        # right before the successful return.
        if publish_context is not None:
            self._dispatch_post_commit_manifest_publish(publish_context)
        return result

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if basin_version_id is not None:
            clauses.append("basin_version_id = %s")
            parameters.append(basin_version_id)
        if active is not None:
            clauses.append("active_flag = %s")
            parameters.append(active)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        # JOIN basin_version + basin so each row carries basin_id / basin_name —
        # parity with get_model_internal. OpenAPI ModelInstance schema declares
        # basin_id/basin_name (nullable), and the frontend builds basinVersionToBasinId
        # from model rows; without basin_id the map stays empty and single-run hydro
        # MVT popups (whose feature properties don't self-describe basin_id) fall back
        # to null → "请选择流域" placeholder.
        # Filter clauses must be requalified — `basin_version_id` and `active_flag`
        # exist on BOTH `core.basin_version` and `core.model_instance`, so the
        # unqualified WHERE form raises 'column reference is ambiguous' once the
        # JOIN is in place. The mechanical rewrite below is load-bearing, not
        # defensive: do NOT remove it.
        join_where = where.replace("basin_version_id", "mi.basin_version_id").replace(
            "active_flag", "mi.active_flag"
        )
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) AS total FROM core.model_instance mi {join_where}",
                tuple(parameters),
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                f"""
                SELECT mi.*, b.basin_id, b.basin_name
                FROM core.model_instance mi
                JOIN core.basin_version bv ON bv.basin_version_id = mi.basin_version_id
                JOIN core.basin b ON b.basin_id = bv.basin_id
                {join_where}
                ORDER BY mi.created_at DESC, mi.model_id
                LIMIT %s OFFSET %s
                """,
                tuple([*parameters, limit, offset]),
            )
            items = [_model_public_projection(row) for row in cursor.fetchall()]
        return {"total": total, "items": items, "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        row = self.get_model_internal(model_id)
        if row is None:
            raise MissingResourceError(f"model_id not found: {model_id}")
        return _model_asset_detail(row)

    def get_model_internal(self, model_id: str) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                """
                SELECT
                    mi.*,
                    b.basin_id,
                    b.basin_name,
                    rnv.segment_count,
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
                LEFT JOIN core.mesh_version mv
                  ON mv.mesh_version_id = mi.mesh_version_id
                WHERE mi.model_id = %s
                """,
                (model_id,),
            )
        if row is None:
            raise MissingResourceError(f"model_id not found: {model_id}")
        return dict(row)

    def create_crosswalk_entries(
        self,
        payload: Mapping[str, Any],
        *,
        policy_decision: PolicyDecision | None = None,
        trusted_internal: bool = False,
    ) -> dict[str, Any]:
        self._require_m17_registry_admin_write_policy(
            target_id="river-segment-crosswalks",
            policy_decision=policy_decision,
            trusted_internal=trusted_internal,
        )
        entries = list(payload.get("entries") or [])
        if not entries:
            raise InvalidPayloadError("entries must not be empty.")
        rows = [
            (
                payload["river_network_version_id"],
                entry["river_segment_id"],
                entry["source"],
                entry["external_id"],
                self._json(entry.get("properties_json") or {}),
            )
            for entry in entries
        ]
        with self._transaction() as cursor:
            inserted = self._execute_values(
                cursor,
                """
                INSERT INTO core.river_segment_crosswalk (
                    river_network_version_id,
                    river_segment_id,
                    source,
                    external_id,
                    properties_json
                )
                VALUES %s
                ON CONFLICT (river_network_version_id, source, external_id)
                DO UPDATE SET river_segment_id = EXCLUDED.river_segment_id,
                              properties_json = EXCLUDED.properties_json
                RETURNING river_network_version_id, river_segment_id, source, external_id, properties_json
                """,
                rows,
                fetch=True,
            )
        items = [dict(row) for row in inserted]
        return {"count": len(items), "items": items}

    def _insert_basin_version(
        self,
        cursor: Any,
        *,
        basin_id: str,
        basin_version_id: str,
        payload: Mapping[str, Any],
        geom_wkt: str,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            INSERT INTO core.basin_version (
                basin_version_id,
                basin_id,
                version_label,
                geom,
                active_flag,
                valid_from,
                valid_to,
                source_uri,
                checksum
            )
            VALUES (%s, %s, %s, ST_GeomFromText(%s, 4490), %s, %s, %s, %s, %s)
            RETURNING
                basin_version_id,
                basin_id,
                version_label,
                ST_AsGeoJSON(geom)::json AS geom,
                active_flag,
                valid_from,
                valid_to,
                source_uri,
                checksum,
                created_at
            """,
            (
                basin_version_id,
                basin_id,
                payload["version_label"],
                geom_wkt,
                bool(payload.get("active_flag", False)),
                payload.get("valid_from"),
                payload.get("valid_to"),
                payload.get("source_uri"),
                payload.get("checksum"),
            ),
        )
        return dict(cursor.fetchone())

    def _exists(self, cursor: Any, table: str, column: str, value: str) -> bool:
        cursor.execute(f"SELECT 1 FROM {table} WHERE {column} = %s", (value,))
        return cursor.fetchone() is not None

    def _fetch_optional(self, cursor: Any, statement: str, parameters: Sequence[Any]) -> dict[str, Any] | None:
        cursor.execute(statement, tuple(parameters))
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def _json(self, value: Mapping[str, Any]) -> Any:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error
        return Json(dict(value))

    def _execute_values(
        self,
        cursor: Any,
        statement: str,
        rows: Sequence[Sequence[Any]],
        *,
        template: str | None = None,
        fetch: bool = False,
    ) -> list[Any]:
        try:
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error
        result = execute_values(cursor, statement, rows, template=template, page_size=1000, fetch=fetch)
        return list(result or [])

    def _require_m17_registry_admin_write_policy(
        self,
        *,
        target_id: str,
        policy_decision: PolicyDecision | None,
        trusted_internal: bool,
    ) -> PolicyDecision:
        # M17 has no finer-grained create action ids for registry-admin writes.
        # Until M18 lifecycle actions land, route and direct writes must present
        # the canonical models.switch_version decision for their route target.
        action_id = "models.switch_version"
        if trusted_internal:
            policy_decision = trusted_internal_policy_decision(
                action_id,
                target_type="model_registry",
                target_id=target_id,
                actor_id="trusted-internal:model-registry",
                roles=("sys_admin",),
            )
        decision = require_policy_evidence(
            policy_decision,
            action_id=action_id,
            target_type="model_registry",
            target_id=target_id,
        )
        if decision.decision != "allow":
            raise ModelRegistryError(decision.reason)
        return decision

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

    def _lock_basin_version_scope(self, cursor: Any, basin_version_id: str) -> None:
        cursor.execute(
            """
            SELECT basin_version_id
            FROM core.basin_version
            WHERE basin_version_id = %s
            FOR UPDATE
            """,
            (basin_version_id,),
        )
        if cursor.fetchone() is None:
            raise InvalidReferenceError(f"basin_version_id does not exist: {basin_version_id}")

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

    def _insert_model_activation_audit(
        self,
        cursor: Any,
        *,
        current: Mapping[str, Any],
        updated: Mapping[str, Any],
        active: bool,
        policy_decision: PolicyDecision,
        request_id: str | None,
    ) -> None:
        details = audit_record(
            policy_decision,
            request_id=request_id,
            previous_state={"active": bool(current["active_flag"])},
            new_state={"active": bool(active)},
            payload={
                "basin_version_id": updated["basin_version_id"],
                "river_network_version_id": updated["river_network_version_id"],
                "mesh_version_id": updated["mesh_version_id"],
                "model_package_uri": _sanitize_audit_uri(updated["model_package_uri"]),
            },
        )
        details.update(
            {
            "previous_active": bool(current["active_flag"]),
            "active": bool(active),
            "basin_version_id": updated["basin_version_id"],
            "river_network_version_id": updated["river_network_version_id"],
            "mesh_version_id": updated["mesh_version_id"],
            "model_package_uri": _sanitize_audit_uri(updated["model_package_uri"]),
            }
        )
        basins_lineage = _basins_lineage_details(updated.get("resource_profile"))
        if basins_lineage:
            details["basins_lineage"] = basins_lineage
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
            """,
            (
                policy_decision.actor_id,
                ",".join(policy_decision.roles),
                policy_decision.action_id,
                updated["model_id"],
                self._json(details),
            ),
        )

    def _record_state_clone_refusal_audit(
        self,
        *,
        activation_context: ModelActivationContext,
        refusal: Any,
        policy_decision: PolicyDecision,
        request_id: str | None,
        operation: ModelLifecycleOperation,
        preflight: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the state-clone refusal audit record on a fresh tx.

        SUB-5 task 3.2: the pre-activation clone hook refused a source
        with no ``cold_start_approval`` covering it, so the lifecycle
        transaction rolled back. This helper opens a fresh transaction
        and writes an ``ops.audit_log`` row whose ``action`` is the
        stable code ``state_clone_cold_start_approval_required`` and
        whose ``details`` name the blocked ``(basin_version_id,
        source_id)`` scope and the refusal cause; the row survives the
        prior rollback because it is written on a NEW transaction (not
        an autonomous sub-transaction, which PostgreSQL does not
        support).

        The returned result mirrors the shape of the ``blocked``
        activation return so downstream callers key uniformly off
        ``status`` and ``audit_reference``; the stable error code is
        also surfaced under ``error.code`` for API consumers.

        ``refusal`` is typed loosely as ``Any`` to avoid a module-level
        import cycle from ``state_clone_hook`` — the caller resolves the
        exception type locally and passes the instance in.
        """
        from packages.common.state_clone import (
            STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
        )

        target_model = activation_context.target_model
        target_model_id = str(target_model.get("model_id"))
        details = {
            "basin_version_id": activation_context.basin_version_id,
            "source_id": getattr(refusal, "source_id", None),
            "refusal_scope": getattr(refusal, "refusal_scope", None),
            "refusal_code": getattr(
                refusal, "refusal_code", STATE_CLONE_COLD_START_APPROVAL_REQUIRED
            ),
            "target_model_id": target_model_id,
            "operation": operation,
            "request_id": request_id,
        }
        details = redact_audit_payload(details)

        with self._transaction() as cursor:
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
                    STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
                    target_model_id,
                    self._json(details),
                ),
            )
            audit_id = int(cursor.fetchone()["log_id"])

        return {
            "status": "refused",
            "operation": operation,
            "model": _model_public_projection(target_model),
            "preflight": preflight,
            "error": {
                "code": STATE_CLONE_COLD_START_APPROVAL_REQUIRED,
                "message": (
                    f"state clone refused for source_id="
                    f"{getattr(refusal, 'source_id', None)!r} "
                    f"scope={getattr(refusal, 'refusal_scope', None)!r}; "
                    "explicit cold-start approval required "
                    "(docs §11.3 clause 2)."
                ),
                "details": {
                    "basin_version_id": activation_context.basin_version_id,
                    "source_id": getattr(refusal, "source_id", None),
                    "refusal_scope": getattr(refusal, "refusal_scope", None),
                },
            },
            "audit_reference": {
                "entity_type": "model_instance",
                "entity_id": target_model_id,
                "log_id": audit_id,
            },
        }

    def _transaction(self) -> Any:
        return _PsycopgTransaction(self.database_url)


def sanitize_model_list_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["items"] = [_model_public_projection(item) for item in list(result.get("items") or [])]
    return result


def sanitize_model_detail_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _model_asset_detail(payload)


def sanitize_basin_version_list_payload(payload: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_basin_version_public_projection(item) for item in payload]


BASINS_AUDIT_LINEAGE_KEYS = (
    "basin_slug",
    "shud_input_name",
    "manifest_uri",
    "package_checksum",
    "source_inventory_checksum",
)
BASINS_AUDIT_LINEAGE_URI_KEYS = frozenset({"manifest_uri"})


def _sanitize_audit_uri(value: Any) -> str | None:
    if value in (None, ""):
        return None
    parsed = urlsplit(str(value))
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_uri_like(value: Any) -> bool:
    parsed = urlsplit(str(value))
    return bool(parsed.scheme or parsed.netloc)


def _basins_lineage_details(resource_profile: Any) -> dict[str, Any]:
    if isinstance(resource_profile, str):
        try:
            resource_profile = json.loads(resource_profile)
        except json.JSONDecodeError:
            return {}
    if not isinstance(resource_profile, Mapping):
        return {}
    details: dict[str, Any] = {}
    for key in BASINS_AUDIT_LINEAGE_KEYS:
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        details[key] = _sanitize_audit_uri(value) if key in BASINS_AUDIT_LINEAGE_URI_KEYS else value
    return details


MODEL_ASSET_LINEAGE_KEYS = (
    "manifest_uri",
    "source_inventory_checksum",
    "basin_slug",
    "shud_input_name",
    "package_checksum",
    "source_path",
    "resolved_source_path",
    "source_uri",
    "source_is_symlink",
)
MODEL_ASSET_URI_KEYS = frozenset(
    {
        "manifest_uri",
        "mesh_uri",
        "model_package_uri",
        "source_uri",
    }
)
MODEL_ASSET_URI_OR_PATH_KEYS = frozenset({"source_path", "resolved_source_path"})
PUBLIC_SENSITIVE_PATH_KEYS = frozenset(
    {
        "artifact_path",
        "copied_root",
        "copied_root_uri",
        "local_path",
        "local_root",
        "package_path",
        "path",
        "resolved_source_path",
        "root",
        "source_path",
        "source_root",
        "source_uri",
        "uri",
        "url",
    }
)
PUBLIC_SENSITIVE_DIGEST_KEYS = frozenset(
    {
        "checksum",
        "digest",
        "hash",
        "md5",
        "package_checksum",
        "sha",
        "sha1",
        "sha224",
        "sha256",
        "sha384",
        "sha512",
        "source_inventory_checksum",
        "stored_manifest_package_checksum",
    }
)
REDACTED_REASON = "[redacted]"
SUPPORTED_OBJECT_URI_SCHEMES = frozenset({"s3", "az", "gs", "https", "http", "integration", "memory"})
PUBLIC_JSON_SANITIZE_MAX_DEPTH = 24
PUBLIC_JSON_SANITIZE_MAX_NODES = 5000


def _model_asset_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    resource_profile = _json_mapping(detail.get("resource_profile"))
    mesh_properties = _json_mapping(detail.pop("mesh_properties_json", None))
    detail["resource_profile"] = _sanitize_public_json_value(resource_profile)
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )

    for key in MODEL_ASSET_LINEAGE_KEYS:
        detail[key] = _first_non_empty(resource_profile.get(key), mesh_properties.get(key), detail.get(key))
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])

    model_name = _first_non_empty(
        resource_profile.get("model_name"),
        resource_profile.get("shud_input_name"),
        detail.get("model_name"),
        detail.get("model_id"),
    )
    detail["model_name"] = str(model_name) if model_name is not None else None
    detail["segment_count"] = int(detail["segment_count"]) if detail.get("segment_count") is not None else None
    for key in (
        "package_checksum",
        "source_inventory_checksum",
        "mesh_checksum",
        "basin_checksum",
        "river_network_checksum",
    ):
        if key in detail:
            detail[key] = None
    return detail


def _river_segment_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["river_segment_id"] = str(detail["river_segment_id"])
    detail["river_network_version_id"] = str(detail["river_network_version_id"])
    detail["segment_order"] = int(detail["segment_order"]) if detail.get("segment_order") is not None else None
    detail["length_m"] = float(detail["length_m"]) if detail.get("length_m") is not None else None
    detail["properties_json"] = _json_mapping(detail.get("properties_json"))
    return detail


def _enforce_river_segment_serialized_budget(payload: Mapping[str, Any], *, max_bytes: int, scope: str) -> None:
    serialized_bytes = len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8"))
    if serialized_bytes > max_bytes:
        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=max_bytes,
            serialized_bytes=serialized_bytes,
            scope=scope,
        )


def _model_public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["resource_profile"] = _sanitize_public_json_value(_json_mapping(detail.get("resource_profile")))
    detail["lifecycle_state"] = str(
        detail.get("lifecycle_state") or ("active" if detail.get("active_flag") else "inactive")
    )
    for key in MODEL_ASSET_URI_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in MODEL_ASSET_URI_OR_PATH_KEYS:
        if detail.get(key) not in (None, ""):
            detail[key] = _sanitize_public_json_value(detail[key])
    for key in (
        "package_checksum",
        "source_inventory_checksum",
        "mesh_checksum",
        "basin_checksum",
        "river_network_checksum",
    ):
        if key in detail:
            detail[key] = None
    return detail


def _basin_version_public_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    if "source_uri" in detail:
        detail["source_uri"] = None
    if "checksum" in detail:
        detail["checksum"] = None
    return detail


def _sanitize_public_json_value(
    value: Any,
    *,
    _depth: int = 0,
    _state: dict[str, Any] | None = None,
) -> Any:
    state = _state or {"nodes": 0, "seen": set()}
    state["nodes"] += 1
    if _depth > PUBLIC_JSON_SANITIZE_MAX_DEPTH or state["nodes"] > PUBLIC_JSON_SANITIZE_MAX_NODES:
        return None

    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in state["seen"]:
            return None
        state["seen"].add(object_id)
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            if state["nodes"] >= PUBLIC_JSON_SANITIZE_MAX_NODES:
                break
            if _is_sensitive_public_json_key(key):
                sanitized[key] = None
            elif (
                _is_sensitive_public_path_key(key)
                and isinstance(child, str)
                and _is_public_sensitive_path_or_file_uri(child)
            ):
                sanitized[key] = None
            else:
                sanitized[key] = _sanitize_public_json_value(child, _depth=_depth + 1, _state=state)
        state["seen"].remove(object_id)
        return sanitized
    if isinstance(value, list | tuple):
        object_id = id(value)
        if object_id in state["seen"]:
            return None
        state["seen"].add(object_id)
        sanitized_list = []
        for child in value:
            if state["nodes"] >= PUBLIC_JSON_SANITIZE_MAX_NODES:
                break
            sanitized_list.append(_sanitize_public_json_value(child, _depth=_depth + 1, _state=state))
        state["seen"].remove(object_id)
        return sanitized_list
    if isinstance(value, str):
        if _is_public_sensitive_path_or_file_uri(value):
            return None
        if _is_uri_like(value):
            return _sanitize_audit_uri(value)
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    return None
    return value


def _is_sensitive_public_json_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in PUBLIC_SENSITIVE_DIGEST_KEYS:
        return True
    return (
        lowered.endswith("_checksum")
        or lowered.endswith("checksum")
        or lowered.endswith("_hash")
        or lowered.endswith("_digest")
    )


def _is_sensitive_public_path_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in PUBLIC_SENSITIVE_PATH_KEYS or lowered.endswith("_path") or lowered.endswith("_root")


def _is_public_sensitive_path_or_file_uri(value: str) -> bool:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return False
    if scheme == "file":
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", value):
        return True
    if value.startswith("\\\\"):
        return True
    normalized = (parsed.path if parsed.scheme else value).replace("\\", "/")
    if not parsed.scheme and not parsed.netloc and normalized.startswith("/"):
        return True
    if normalized.startswith("/volume/"):
        return True
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"/", ""}]
    for index, part in enumerate(parts):
        if part == "data" and index + 1 < len(parts) and parts[index + 1] == "Basins":
            return True
        if part == "Basins" and index > 0 and parts[index - 1] == "data":
            return True
    return False


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _preflight_blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _lifecycle_audit_persistence_failure_result(
    *,
    model: Mapping[str, Any],
    current_active: Mapping[str, Any] | None,
    operation: ModelLifecycleOperation,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blocked_preflight = dict(preflight)
    blocked_preflight["status"] = "blocked"
    blocked_preflight["blockers"] = [
        *list(preflight.get("blockers") or []),
        _preflight_blocker(
            "LIFECYCLE_AUDIT_PERSISTENCE_FAILED",
            "Lifecycle audit evidence could not be persisted; mutation was rolled back.",
        ),
    ]
    return {
        "status": "blocked",
        "operation": operation,
        "model": _model_public_projection(model),
        "previous_model": _model_public_projection(current_active) if current_active is not None else None,
        "preflight": blocked_preflight,
        "audit_reference": None,
    }


def _preflight_warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _apply_idempotent_rollback_preflight(preflight: dict[str, Any], history: Mapping[str, Any]) -> None:
    preflight["status"] = "ready"
    preflight["blockers"] = []
    preflight["warnings"] = [
        *list(preflight.get("warnings") or []),
        {
            "code": "ROLLBACK_ALREADY_CURRENT",
            "message": "Rollback retry is already reflected by the current active model.",
        },
    ]
    preflight["prior_audit_log_id"] = history.get("prior_audit_log_id")
    preflight["rollback_history"] = _rollback_history_preflight_reference(history)


def _activation_safety_evidence(
    model: Mapping[str, Any],
    *,
    activation_class_operation: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str, Any]:
    resource_profile = _json_mapping(model.get("resource_profile"))
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if model.get("river_network_version_id") and model.get("basin_version_id") is None:
        blockers.append(_preflight_blocker("LINEAGE_MISSING_BASIN_VERSION", "Model lineage is missing basin version."))
    if model.get("mesh_version_id") in (None, ""):
        blockers.append(_preflight_blocker("LINEAGE_MISSING_MESH_VERSION", "Model lineage is missing mesh version."))
    if model.get("model_package_uri") in (None, ""):
        blockers.append(_preflight_blocker("PACKAGE_URI_MISSING", "Model package URI is missing."))
    package_checksum = _first_non_empty(resource_profile.get("package_checksum"), model.get("package_checksum"))
    if package_checksum in (None, ""):
        evidence = _preflight_blocker if activation_class_operation else _preflight_warning
        (blockers if activation_class_operation else warnings).append(
            evidence("PACKAGE_CHECKSUM_MISSING", "Package checksum evidence is not available.")
        )
    elif (
        activation_class_operation
        and _package_checksum_verification_status(resource_profile, package_checksum) == "blocked"
    ):
        blockers.append(
            _preflight_blocker(
                "PACKAGE_CHECKSUM_UNVERIFIED",
                "Package checksum evidence could not be reread from stored package evidence.",
            )
        )
    if _object_uri_prefix_status(model.get("model_package_uri")) == "invalid":
        blockers.append(_preflight_blocker("OBJECT_URI_PREFIX_INVALID", "Model package URI prefix is not supported."))
    copied_root = _copied_root_status(resource_profile)
    if copied_root == "unsafe":
        blockers.append(_preflight_blocker("COPIED_ROOT_UNSAFE", "Copied-root source evidence is unsafe."))
    elif copied_root == "missing":
        warnings.append(_preflight_warning("COPIED_ROOT_EVIDENCE_MISSING", "Copied-root evidence is not available."))
    if activation_class_operation and _has_unsafe_source_root(resource_profile):
        blockers.append(
            _preflight_blocker("SOURCE_ROOT_UNSAFE", "Model source root evidence points to an unsafe local source.")
        )
    return blockers, warnings, copied_root, package_checksum


def _object_uri_prefix_status(value: Any) -> str:
    if value in (None, ""):
        return "missing"
    parsed = urlsplit(str(value))
    if parsed.scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return "valid"
    return "invalid"


def _copied_root_status(resource_profile: Mapping[str, Any]) -> str:
    copied_root = _first_non_empty(
        resource_profile.get("copied_root"),
        resource_profile.get("copied_root_uri"),
        resource_profile.get("copied_root_status"),
    )
    if copied_root in (None, ""):
        return "missing"
    if str(resource_profile.get("source_is_symlink", "")).lower() == "true":
        return "unsafe"
    text = str(copied_root)
    if text.lower() in {"unsafe", "symlink", "raw", "local"}:
        return "unsafe"
    if text.lower() in {"present", "safe", "copied", "verified"}:
        return "present"
    if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
        return "unsafe"
    return "present"


def _canonical_lifecycle_state(model: Mapping[str, Any]) -> ModelLifecycleState:
    state = str(model.get("lifecycle_state") or ("active" if model.get("active_flag") else "inactive"))
    if state in MODEL_LIFECYCLE_STATES:
        return state  # type: ignore[return-value]
    return "active" if model.get("active_flag") else "inactive"


def _transition_blocker(
    *,
    operation: ModelLifecycleOperation,
    lifecycle_state: ModelLifecycleState,
    model_id: str,
    current_active_id: str | None,
) -> dict[str, str] | None:
    if operation == "activate":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"activate is not allowed from {lifecycle_state}.")
    elif operation == "switch_version":
        if lifecycle_state == "active" and current_active_id == model_id:
            return None
        if lifecycle_state not in {"inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"switch_version is not allowed from {lifecycle_state}.")
    elif operation == "deactivate":
        if lifecycle_state not in {"active", "inactive"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deactivate is not allowed from {lifecycle_state}.")
    elif operation == "supersede":
        if lifecycle_state not in {"active", "inactive", "deprecated", "superseded"}:
            return _preflight_blocker("INVALID_TRANSITION", f"supersede is not allowed from {lifecycle_state}.")
    elif operation == "deprecate":
        if lifecycle_state not in {"inactive", "superseded", "deprecated"}:
            return _preflight_blocker("INVALID_TRANSITION", f"deprecate is not allowed from {lifecycle_state}.")
    return None


def _package_checksum_verification_status(resource_profile: Mapping[str, Any], package_checksum: Any) -> str:
    verification_fields = (
        resource_profile.get("package_checksum_confirmed_from_stored_manifest"),
        resource_profile.get("package_checksum_verified"),
        resource_profile.get("checksum_reread_verified"),
    )
    if any(value is True for value in verification_fields):
        return "verified"
    if any(value is False for value in verification_fields):
        return "blocked"
    for key in (
        "package_checksum_reread_status",
        "package_checksum_reconstruction_status",
        "checksum_reread_status",
    ):
        value = resource_profile.get(key)
        if value in (None, ""):
            continue
        if str(value).lower() in {"verified", "ready", "ok", "confirmed"}:
            return "verified"
        if str(value).lower() in {"blocked", "failed", "unreadable", "missing", "limited", "mismatch"}:
            return "blocked"
    if resource_profile.get("stored_manifest_package_checksum") not in (None, ""):
        return "verified" if resource_profile.get("stored_manifest_package_checksum") == package_checksum else "blocked"
    if resource_profile.get("manifest_uri") not in (None, ""):
        return "verified"
    return "blocked"


def _has_unsafe_source_root(resource_profile: Mapping[str, Any]) -> bool:
    for value in _iter_source_evidence_values(resource_profile):
        if _is_unsafe_source_value(value):
            return True
    return False


def _iter_source_evidence_values(value: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"source_path", "resolved_source_path", "source_uri", "root", "source_root"}:
                values.append(child)
            if isinstance(child, (Mapping, list, tuple)):
                values.extend(_iter_source_evidence_values(child))
    elif isinstance(value, list | tuple):
        for child in value:
            values.extend(_iter_source_evidence_values(child))
    return values


def _is_unsafe_source_value(value: Any) -> bool:
    if value in (None, ""):
        return False
    text = str(value)
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        return True
    if scheme in SUPPORTED_OBJECT_URI_SCHEMES:
        return False
    path_text = parsed.path if scheme else text
    normalized = path_text.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return True
    if normalized.startswith("/volume/"):
        return True
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"/", ""}]
    for index, part in enumerate(parts):
        if part == "data" and index + 1 < len(parts) and parts[index + 1] == "Basins":
            return True
        if part == "Basins" and index > 0 and parts[index - 1] == "data":
            return True
    return False


def _model_audit_reference(model: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "model_id": model.get("model_id"),
        "basin_version_id": model.get("basin_version_id"),
        "lifecycle_state": model.get("lifecycle_state"),
        "active_flag": bool(model.get("active_flag")),
    }


def _rollback_history_preflight_reference(history: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if history is None:
        return None
    return {
        "prior_audit_log_id": history.get("prior_audit_log_id") or history.get("log_id"),
        "action": history.get("action"),
        "entity_id": history.get("entity_id"),
        "trusted": bool(history.get("trusted")),
        "matched_previous_model_id": history.get("matched_previous_model_id"),
        "stale_reason": history.get("stale_reason"),
    }


class _PsycopgTransaction:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: Any | None = None

    def __enter__(self) -> Any:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error

        self.psycopg2 = psycopg2
        self.connection = psycopg2.connect(self.database_url)
        self.connection.autocommit = False
        register_default_json(loads=json.loads, conn_or_curs=self.connection)
        register_default_jsonb(loads=json.loads, conn_or_curs=self.connection)
        return self.connection.cursor(cursor_factory=RealDictCursor)

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: Any) -> bool:
        if self.connection is None:
            return False
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
                if isinstance(exc, ModelRegistryError):
                    return False
                self._raise_mapped_database_error(exc)
        finally:
            self.connection.close()
        return False

    def _raise_mapped_database_error(self, exc: BaseException | None) -> None:
        if exc is None:
            return
        error_code = getattr(exc, "pgcode", None)
        if error_code == "23505":
            raise DuplicateResourceError(str(exc)) from exc
        if error_code in {"23503", "22P02"}:
            raise InvalidReferenceError(str(exc)) from exc
        if error_code in {"XX000", "22023"}:
            raise InvalidPayloadError(str(exc)) from exc
        raise ModelRegistryError(f"Model registry database operation failed: {exc}") from exc
