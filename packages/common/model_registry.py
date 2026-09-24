"""Model registry store (stable import path).

``PsycopgModelRegistryStore`` is composed here from the plain mixins in the
``packages.common.model_registry_*`` owner modules (#2617 physical split); the
dataclass fields, ``__post_init__``, ``from_env``, ``_transaction`` and the
psycopg2 connection (``_PsycopgTransaction``) stay in this module. Every
module-level name the owner modules define is re-exported here by identity.
"""

from __future__ import annotations

import json
import os
import re as re
from collections.abc import Callable as Callable
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath as PurePosixPath
from typing import Any
from typing import Literal as Literal
from urllib.parse import urlsplit as urlsplit
from urllib.parse import urlunsplit as urlunsplit
from uuid import uuid4 as uuid4

from packages.common.auth_policy import PolicyDecision as PolicyDecision
from packages.common.auth_policy import audit_record as audit_record
from packages.common.auth_policy import redact_audit_payload as redact_audit_payload
from packages.common.auth_policy import require_policy_evidence as require_policy_evidence
from packages.common.auth_policy import trusted_internal_policy_decision as trusted_internal_policy_decision
from packages.common.forecast_store import QHH_LATEST_READY_RUN_STATUSES as QHH_LATEST_READY_RUN_STATUSES
from packages.common.model_registry_catalog import _RegistryCatalogMixin
from packages.common.model_registry_contracts import MODEL_LIFECYCLE_ACTIONS as MODEL_LIFECYCLE_ACTIONS
from packages.common.model_registry_contracts import MODEL_LIFECYCLE_STATES as MODEL_LIFECYCLE_STATES
from packages.common.model_registry_contracts import (
    PRE_ACTIVATION_HOOK_MOUNT_POINTS,
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    ModelRegistryError,
    _default_no_op_hook,
    _default_no_op_manifest_publisher,
    _default_no_op_state_index_publisher,
)
from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES as RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
)
from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS as RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
)
from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES as RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
)
from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES as RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
)
from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES as RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
)
from packages.common.model_registry_contracts import (
    SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES as SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES,
)
from packages.common.model_registry_contracts import (
    SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS as SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
)
from packages.common.model_registry_contracts import ColdStartApprovalInput as ColdStartApprovalInput
from packages.common.model_registry_contracts import ForcingMappingClassification as ForcingMappingClassification
from packages.common.model_registry_contracts import MissingResourceError as MissingResourceError
from packages.common.model_registry_contracts import ModelActivationContext as ModelActivationContext
from packages.common.model_registry_contracts import (
    ModelLifecycleAuditPersistenceError as ModelLifecycleAuditPersistenceError,
)
from packages.common.model_registry_contracts import ModelLifecycleOperation as ModelLifecycleOperation
from packages.common.model_registry_contracts import ModelLifecycleState as ModelLifecycleState
from packages.common.model_registry_contracts import PostCommitManifestPublisher as PostCommitManifestPublisher
from packages.common.model_registry_contracts import PostCommitPublishContext as PostCommitPublishContext
from packages.common.model_registry_contracts import PostCommitStateIndexPublisher as PostCommitStateIndexPublisher
from packages.common.model_registry_contracts import PreActivationHook as PreActivationHook
from packages.common.model_registry_contracts import RiverSegmentGeoJsonBudgetError as RiverSegmentGeoJsonBudgetError
from packages.common.model_registry_contracts import (
    _build_activation_result_approval_block as _build_activation_result_approval_block,
)
from packages.common.model_registry_contracts import _classify_forcing_mapping_mode as _classify_forcing_mapping_mode
from packages.common.model_registry_contracts import _declares_direct_grid_intent as _declares_direct_grid_intent
from packages.common.model_registry_contracts import _escape_like as _escape_like
from packages.common.model_registry_contracts import _extract_source_scope as _extract_source_scope
from packages.common.model_registry_contracts import _format_point as _format_point
from packages.common.model_registry_contracts import _json_mapping as _json_mapping
from packages.common.model_registry_contracts import _linestring_to_wkt as _linestring_to_wkt
from packages.common.model_registry_contracts import _multipolygon_to_wkt as _multipolygon_to_wkt
from packages.common.model_registry_contracts import (
    _should_publish_manifest_after_commit as _should_publish_manifest_after_commit,
)
from packages.common.model_registry_contracts import _would_be_already_current as _would_be_already_current
from packages.common.model_registry_contracts import build_versioned_id as build_versioned_id
from packages.common.model_registry_contracts import geometry_to_wkt as geometry_to_wkt
from packages.common.model_registry_lifecycle import _ModelLifecycleMixin
from packages.common.model_registry_lifecycle_support import _LifecycleSupportMixin
from packages.common.model_registry_preflight_rules import _activation_safety_evidence as _activation_safety_evidence
from packages.common.model_registry_preflight_rules import (
    _apply_idempotent_rollback_preflight as _apply_idempotent_rollback_preflight,
)
from packages.common.model_registry_preflight_rules import _canonical_lifecycle_state as _canonical_lifecycle_state
from packages.common.model_registry_preflight_rules import _copied_root_status as _copied_root_status
from packages.common.model_registry_preflight_rules import _has_unsafe_source_root as _has_unsafe_source_root
from packages.common.model_registry_preflight_rules import _is_unsafe_source_value as _is_unsafe_source_value
from packages.common.model_registry_preflight_rules import _iter_source_evidence_values as _iter_source_evidence_values
from packages.common.model_registry_preflight_rules import (
    _lifecycle_audit_persistence_failure_result as _lifecycle_audit_persistence_failure_result,
)
from packages.common.model_registry_preflight_rules import _model_audit_reference as _model_audit_reference
from packages.common.model_registry_preflight_rules import _object_uri_prefix_status as _object_uri_prefix_status
from packages.common.model_registry_preflight_rules import (
    _package_checksum_verification_status as _package_checksum_verification_status,
)
from packages.common.model_registry_preflight_rules import _preflight_blocker as _preflight_blocker
from packages.common.model_registry_preflight_rules import _preflight_warning as _preflight_warning
from packages.common.model_registry_preflight_rules import (
    _rollback_history_preflight_reference as _rollback_history_preflight_reference,
)
from packages.common.model_registry_preflight_rules import _transition_blocker as _transition_blocker
from packages.common.model_registry_public import BASINS_AUDIT_LINEAGE_KEYS as BASINS_AUDIT_LINEAGE_KEYS
from packages.common.model_registry_public import BASINS_AUDIT_LINEAGE_URI_KEYS as BASINS_AUDIT_LINEAGE_URI_KEYS
from packages.common.model_registry_public import MODEL_ASSET_LINEAGE_KEYS as MODEL_ASSET_LINEAGE_KEYS
from packages.common.model_registry_public import MODEL_ASSET_URI_KEYS as MODEL_ASSET_URI_KEYS
from packages.common.model_registry_public import MODEL_ASSET_URI_OR_PATH_KEYS as MODEL_ASSET_URI_OR_PATH_KEYS
from packages.common.model_registry_public import PUBLIC_JSON_SANITIZE_MAX_DEPTH as PUBLIC_JSON_SANITIZE_MAX_DEPTH
from packages.common.model_registry_public import PUBLIC_JSON_SANITIZE_MAX_NODES as PUBLIC_JSON_SANITIZE_MAX_NODES
from packages.common.model_registry_public import PUBLIC_SENSITIVE_DIGEST_KEYS as PUBLIC_SENSITIVE_DIGEST_KEYS
from packages.common.model_registry_public import PUBLIC_SENSITIVE_PATH_KEYS as PUBLIC_SENSITIVE_PATH_KEYS
from packages.common.model_registry_public import REDACTED_REASON as REDACTED_REASON
from packages.common.model_registry_public import SUPPORTED_OBJECT_URI_SCHEMES as SUPPORTED_OBJECT_URI_SCHEMES
from packages.common.model_registry_public import _basin_version_public_projection as _basin_version_public_projection
from packages.common.model_registry_public import _basins_lineage_details as _basins_lineage_details
from packages.common.model_registry_public import (
    _enforce_river_segment_serialized_budget as _enforce_river_segment_serialized_budget,
)
from packages.common.model_registry_public import _first_non_empty as _first_non_empty
from packages.common.model_registry_public import (
    _is_public_sensitive_path_or_file_uri as _is_public_sensitive_path_or_file_uri,
)
from packages.common.model_registry_public import _is_sensitive_public_json_key as _is_sensitive_public_json_key
from packages.common.model_registry_public import _is_sensitive_public_path_key as _is_sensitive_public_path_key
from packages.common.model_registry_public import _is_uri_like as _is_uri_like
from packages.common.model_registry_public import _model_asset_detail as _model_asset_detail
from packages.common.model_registry_public import _model_public_projection as _model_public_projection
from packages.common.model_registry_public import _river_segment_detail as _river_segment_detail
from packages.common.model_registry_public import _sanitize_audit_uri as _sanitize_audit_uri
from packages.common.model_registry_public import _sanitize_public_json_value as _sanitize_public_json_value
from packages.common.model_registry_public import (
    sanitize_basin_version_list_payload as sanitize_basin_version_list_payload,
)
from packages.common.model_registry_public import sanitize_model_detail_payload as sanitize_model_detail_payload
from packages.common.model_registry_public import sanitize_model_list_payload as sanitize_model_list_payload
from packages.common.model_registry_river_segments import _RiverSegmentReadMixin
from workers.forcing_producer.direct_grid_contract import DIRECT_GRID_MODE as DIRECT_GRID_MODE
from workers.forcing_producer.direct_grid_contract import DIRECT_GRID_SECTION_KEYS as DIRECT_GRID_SECTION_KEYS
from workers.forcing_producer.direct_grid_contract import DirectGridContractError as DirectGridContractError
from workers.forcing_producer.direct_grid_contract import (
    load_forcing_mapping_contract_from_manifest as load_forcing_mapping_contract_from_manifest,
)


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ModelRegistryError("DATABASE_URL is required for model registry operations.")
    return database_url


def _attribution_connect_kwargs(application_name: str | None) -> dict[str, str]:
    """libpq attribution kwargs for a connection this module opens (#1728).

    Empty when the caller injected no ``application_name``, so a store built
    without one reaches ``psycopg2.connect`` exactly as it did before.
    """
    if application_name is None:
        return {}
    return {"fallback_application_name": application_name}


@dataclass(frozen=True)
class PsycopgModelRegistryStore(
    _RegistryCatalogMixin,
    _RiverSegmentReadMixin,
    _ModelLifecycleMixin,
    _LifecycleSupportMixin,
):
    database_url: str
    audit_actor: str = "nhms-api"
    audit_actor_role: str = "model-registry"
    # #1728: the calling surface (route module) names the connection; the store
    # itself hard-codes nothing and defaults to the pre-#1728 unnamed behaviour.
    application_name: str | None = None

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
        # SUB-6 (§3.3): seed the post-commit state-index publisher. Runs
        # BEFORE the manifest publisher on the post-commit tail so a
        # raising index publisher holds back the manifest re-publish —
        # node-22 (DB-free) never observes ``M1``-active without ``M1``'s
        # successor checkpoint on the file state index (D7 fact anchor
        # A-i). Defaults to no-op so the byte-for-byte-preserved
        # lifecycle behavior stays intact until production wiring
        # registers the real publisher.
        object.__setattr__(
            self,
            "_post_commit_state_index_publisher",
            _default_no_op_state_index_publisher,
        )

    @classmethod
    def from_env(cls, *, application_name: str | None = None) -> PsycopgModelRegistryStore:
        return cls(default_database_url(), application_name=application_name)

    def _transaction(self) -> Any:
        return _PsycopgTransaction(self.database_url, application_name=self.application_name)


class _PsycopgTransaction:
    def __init__(self, database_url: str, *, application_name: str | None = None) -> None:
        self.database_url = database_url
        self.application_name = application_name
        self.connection: Any | None = None

    def __enter__(self) -> Any:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
        except ImportError as error:
            raise ModelRegistryError("psycopg2 is required for model registry operations.") from error

        self.psycopg2 = psycopg2
        self.connection = psycopg2.connect(
            self.database_url, **_attribution_connect_kwargs(self.application_name)
        )
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
