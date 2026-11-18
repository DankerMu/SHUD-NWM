"""Model registry contracts: error types, lifecycle literals and constants,
river-segment payload budgets, id/geometry helpers, the post-commit publish
context dataclass with its default no-op publishers, and the lifecycle helpers
used by ``model_lifecycle_operation`` (#2617 split of
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

from packages.common.contracts.direct_grid import (
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
class PostCommitPublishContext:
    """Context payload delivered to the post-commit manifest publisher.

    §2.2 (Epic #961, `source-specific-model-variant-routing`) wires
    `services.orchestrator.scheduler_file_providers.publish_scheduler_registry_manifest`
    as the uniform post-commit tail of every successful dispatch-set-changing
    lifecycle transition. This frozen container carries the committed
    state a publisher needs to reconcile the scheduler manifest with the
    DB. The publisher runs AFTER the lifecycle transaction commits — a
    rolled-back transaction (for example an audit persistence failure)
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


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}
