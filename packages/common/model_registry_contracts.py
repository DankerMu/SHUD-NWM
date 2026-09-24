"""Model registry contracts: error types, lifecycle literals and constants,
river-segment payload budgets, id/geometry helpers, the cold-start approval /
activation / post-commit context dataclasses with their default no-op hooks,
and the lifecycle helpers used by ``model_lifecycle_operation`` (#2617 split of
``packages.common.model_registry``).

``packages.common.model_registry`` stays the stable import path and re-exports
every name defined here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

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

# #1729: ``core.basin.basin_group`` of synthetic evidence fixtures. ``core.basin``
# has no ``active_flag``, so public basin / model discovery excludes this group
# instead (NULL and every other group stay visible).
EVIDENCE_ONLY_BASIN_GROUP = "evidence-only"


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


# Epic #982 SUB-6 (§3.3) — state clone index publisher seam. Runs on the
# post-commit tail BEFORE the manifest publisher; a raise here holds
# back the manifest re-publish so node-22 (DB-free) never observes
# ``M1``-active in the manifest without ``M1``'s successor checkpoint on
# the file state index (D7 fact anchor A-i).
PostCommitStateIndexPublisher = Callable[[PostCommitPublishContext], None]


def _default_no_op_state_index_publisher(  # noqa: ARG001
    ctx: PostCommitPublishContext,
) -> None:
    """Default state-index publisher: no-op.

    Preserves prior lifecycle behavior byte-for-byte until the production
    wiring registers the real
    :func:`packages.common.state_clone_index_publisher.build_default_state_index_publisher`
    bridge. Tests bind a recording / raising stub via
    :meth:`PsycopgModelRegistryStore.register_post_commit_state_index_publisher`.
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


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}
