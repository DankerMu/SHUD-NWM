from __future__ import annotations

import hashlib as hashlib
import json
import math as math
import os
import re as re
import stat as stat
from collections.abc import Iterator as Iterator
from contextlib import contextmanager
from dataclasses import dataclass as dataclass
from pathlib import Path
from typing import Any

from packages.common.auth_policy import PolicyDecision, require_policy_evidence, trusted_internal_policy_decision

from .basins_geometry import SHAPEFILE_REQUIRED_SUFFIXES as SHAPEFILE_REQUIRED_SUFFIXES
from .basins_geometry import SHUD_CANONICAL_SUFFIXES as SHUD_CANONICAL_SUFFIXES
from .basins_geometry import BasinsGeometryError as BasinsGeometryError
from .basins_geometry import CrosswalkRow as CrosswalkRow
from .basins_geometry import ParsedBasinsGeometry as ParsedBasinsGeometry
from .basins_geometry import TrustedBasinsRoot as TrustedBasinsRoot
from .basins_geometry import parse_basins_geometry as parse_basins_geometry
from .basins_geometry import parse_seg_shp_crosswalk as parse_seg_shp_crosswalk
from .basins_geometry import safe_basins_file_sha256 as safe_basins_file_sha256
from .basins_geometry import trusted_basins_root as trusted_basins_root
from .basins_package import SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS as SUPPORTED_BASINS_PACKAGE_SCHEMA_VERSIONS
from .basins_registry_digests import _OUTPUT_BACKFILL_INJECTED_KEYS as _OUTPUT_BACKFILL_INJECTED_KEYS
from .basins_registry_digests import _canonical_singlepart_line_coordinates as _canonical_singlepart_line_coordinates
from .basins_registry_digests import _existing_output_river_segment_digest as _existing_output_river_segment_digest
from .basins_registry_digests import _existing_river_segment_digest as _existing_river_segment_digest
from .basins_registry_digests import _incoming_river_segment_digest as _incoming_river_segment_digest
from .basins_registry_digests import _normalize_properties_for_digest as _normalize_properties_for_digest
from .basins_registry_digests import _normalize_wkt as _normalize_wkt
from .basins_registry_digests import _output_river_segment_digest as _output_river_segment_digest
from .basins_registry_digests import _river_segment_digest_row as _river_segment_digest_row
from .basins_registry_preflight import _expected_manifest_checksums as _expected_manifest_checksums
from .basins_registry_preflight import _find_inventory_model as _find_inventory_model
from .basins_registry_preflight import _input_dir as _input_dir
from .basins_registry_preflight import _inventory_root as _inventory_root
from .basins_registry_preflight import _prepare_sources, _require_import_policy, _require_public_import_preflight_policy
from .basins_registry_preflight import _registry_ids as _registry_ids
from .basins_registry_preflight import _source_root as _source_root
from .basins_registry_preflight import _validate_manifest_included_files as _validate_manifest_included_files
from .basins_registry_preflight import _validate_manifest_source_identity as _validate_manifest_source_identity
from .basins_registry_preflight import (
    _verify_inventory_checksums_match_manifest as _verify_inventory_checksums_match_manifest,
)
from .basins_registry_preflight import (
    _verify_model_id_matches_canonical_identity as _verify_model_id_matches_canonical_identity,
)
from .basins_registry_rows import _build_river_segment_crosswalk_rows as _build_river_segment_crosswalk_rows
from .basins_registry_rows import _crosswalk_rows_for_sources as _crosswalk_rows_for_sources
from .basins_registry_rows import (
    _delete_legacy_seg_rows,
    _ensure_basin,
    _ensure_basin_version,
    _ensure_mesh,
    _ensure_model_instance,
    _ensure_output_river_segments,
    _ensure_river_segment_crosswalk,
    _ensure_river_segments,
    _resource_profile,
)
from .basins_registry_rows import _output_river_segment_rows as _output_river_segment_rows
from .basins_registry_support import (
    BASINS_REGISTRY_IMPORT_SCHEMA_VERSION,
    BasinsRegistryImportError,
    ImportSources,
    _fetch_optional,
    _json,
    _json_dict,
    _mesh_uri,
    _read_json_document,
    _read_json_object,
    _require_existing,
    _required_str,
    _sha256_bytes,
    _source_checksum,
    _version_label,
    _write_report,
)
from .basins_registry_support import (
    PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID as PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
)
from .basins_registry_support import RIVER_SEGMENT_INSERT_PAGE_SIZE as RIVER_SEGMENT_INSERT_PAGE_SIZE
from .basins_registry_support import _basin_name as _basin_name
from .basins_registry_support import _chunks as _chunks
from .basins_registry_support import _ensure_under_root as _ensure_under_root
from .basins_registry_support import _normalize_relative as _normalize_relative
from .basins_registry_support import _raise_geometry_import_error as _raise_geometry_import_error
from .basins_registry_support import _recorded_path_matches_expected as _recorded_path_matches_expected
from .basins_registry_support import _recorded_relative_inventory_root as _recorded_relative_inventory_root
from .basins_registry_support import _reject_directory_symlink as _reject_directory_symlink
from .basins_registry_support import _required_mapping_str as _required_mapping_str
from .basins_registry_support import _required_model_str as _required_model_str
from .basins_registry_support import _safe_path_relative as _safe_path_relative
from .basins_registry_support import _safe_relative as _safe_relative
from .basins_registry_support import _sha256_json as _sha256_json
from .basins_registry_support import _slug_id as _slug_id


def import_basins_registry(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
    database_url: str | None = None,
    output_path: str | Path | None = None,
    policy_decision: PolicyDecision | None = None,
    preflight_policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
    seed_output_river_segments: bool = True,
    backfill_output_segment_geometry: bool = True,
) -> dict[str, Any]:
    _require_public_import_preflight_policy(
        policy_decision=preflight_policy_decision if preflight_policy_decision is not None else policy_decision,
        trusted_internal=trusted_internal,
    )
    manifest = _read_json_object(
        package_manifest_path,
        error_code="BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
        not_found_code="BASINS_REGISTRY_PACKAGE_MANIFEST_NOT_FOUND",
    )
    model_id = _required_str(manifest, "model_id", "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID")
    _require_import_policy(
        model_id,
        policy_decision=policy_decision,
        trusted_internal=trusted_internal,
    )
    resolved_database_url = database_url or os.getenv("DATABASE_URL", "").strip()
    if not resolved_database_url:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_DATABASE_URL_MISSING",
            "DATABASE_URL or --database-url is required for Basins registry import.",
            model_id=model_id,
        )
    inventory, inventory_bytes = _read_json_document(
        inventory_path,
        error_code="BASINS_REGISTRY_INVENTORY_INVALID",
        not_found_code="BASINS_REGISTRY_INVENTORY_NOT_FOUND",
    )
    sources = _prepare_sources(inventory, manifest, inventory_raw_checksum=_sha256_bytes(inventory_bytes))
    report = _import_prepared_sources(
        sources,
        resolved_database_url,
        policy_decision=policy_decision,
        trusted_internal=trusted_internal,
        seed_output_river_segments=seed_output_river_segments,
        backfill_output_segment_geometry=backfill_output_segment_geometry,
    )
    if output_path is not None:
        _write_report(output_path, report)
    return report


def prepare_basins_import_sources(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
) -> ImportSources:
    inventory, inventory_bytes = _read_json_document(
        inventory_path,
        error_code="BASINS_REGISTRY_INVENTORY_INVALID",
        not_found_code="BASINS_REGISTRY_INVENTORY_NOT_FOUND",
    )
    manifest = _read_json_object(
        package_manifest_path,
        error_code="BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
        not_found_code="BASINS_REGISTRY_PACKAGE_MANIFEST_NOT_FOUND",
    )
    return _prepare_sources(inventory, manifest, inventory_raw_checksum=_sha256_bytes(inventory_bytes))


def prepare_relocated_basins_import_sources_after_package_verification(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
    verified_package_checksum: str,
) -> ImportSources:
    """Prepare a byte-verified source snapshot relocated from an immutable package manifest.

    Scheduler repair workspaces are deliberately run-scoped while immutable
    package versions are root-independent.  Reusing an existing package must
    therefore tolerate only the recorded absolute source paths and whole-
    inventory checksum changing.  The selected model identity, package file
    checksums, canonical required files, and parsed geometry remain fully
    validated by ``_prepare_sources``.  The caller must first complete the
    package publisher's full content and stored-object verification and pass
    its verified package checksum here.
    """

    inventory, inventory_bytes = _read_json_document(
        inventory_path,
        error_code="BASINS_REGISTRY_INVENTORY_INVALID",
        not_found_code="BASINS_REGISTRY_INVENTORY_NOT_FOUND",
    )
    manifest = _read_json_object(
        package_manifest_path,
        error_code="BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
        not_found_code="BASINS_REGISTRY_PACKAGE_MANIFEST_NOT_FOUND",
    )
    if not verified_package_checksum or manifest.get("package_checksum") != verified_package_checksum:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Relocated Basins sources require a matching verified package checksum.",
            model_id=str(manifest.get("model_id") or "") or None,
            details={"fields": ["package_checksum"]},
        )
    return _prepare_sources(
        inventory,
        manifest,
        inventory_raw_checksum=_sha256_bytes(inventory_bytes),
        allow_source_relocation=True,
    )


def _import_prepared_sources(
    sources: ImportSources,
    database_url: str,
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
    seed_output_river_segments: bool = True,
    backfill_output_segment_geometry: bool = True,
) -> dict[str, Any]:
    if trusted_internal:
        policy_decision = trusted_internal_policy_decision(
            "models.switch_version",
            target_type="model_registry",
            target_id=sources.ids["model_id"],
            actor_id="trusted-internal:basins-registry-import",
            roles=("sys_admin",),
        )
    decision = require_policy_evidence(
        policy_decision,
        action_id="models.switch_version",
        target_type="model_registry",
        target_id=sources.ids["model_id"],
    )
    if decision.decision != "allow":
        raise BasinsRegistryImportError(
            decision.reason_code,
            decision.reason,
            model_id=sources.ids["model_id"],
            details={"policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )
    try:
        with _transaction(database_url) as cursor:
            row_counts = import_basin_into_registry_core(
                cursor,
                sources,
                seed_output_river_segments=seed_output_river_segments,
                backfill_output_segment_geometry=backfill_output_segment_geometry,
            )
            # NOTE: output-river reach rows are left NULL-geom by
            # ``_ensure_output_river_segments`` on purpose -- display geometry
            # is a separate concern from the row identities the verifier/parser
            # need. The seeding orchestrators stitch it on after import:
            # qhh -> qhh_production_bootstrap (inside this same transaction
            # via _backfill_output_segment_geometry), generic -> node27
            # autopipe (same shared SQL helper).
    except BasinsRegistryImportError:
        raise
    except Exception as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_DATABASE_ERROR",
            f"Basins registry import database operation failed: {error.__class__.__name__}",
            model_id=str(sources.manifest.get("model_id") or ""),
        ) from error

    status = "already_imported" if all(count == 0 for count in row_counts.values()) else "imported"
    return {
        "schema_version": BASINS_REGISTRY_IMPORT_SCHEMA_VERSION,
        "status": status,
        "model_id": sources.ids["model_id"],
        "basin_id": sources.ids["basin_id"],
        "basin_version_id": sources.ids["basin_version_id"],
        "river_network_version_id": sources.ids["river_network_version_id"],
        "mesh_version_id": sources.ids["mesh_version_id"],
        "active": False
        if row_counts["model_instance"] == 1
        else _model_active_state(database_url, sources.ids["model_id"]),
        "segment_count": sources.geometry.segment_count,
        "output_segment_count": sources.geometry.output_segment_count,
        "row_counts": row_counts,
        "model_package_uri": sources.manifest["model_package_uri"],
        "manifest_uri": sources.manifest["manifest_uri"],
        "package_checksum": sources.manifest["package_checksum"],
        "auth_policy_decision": decision.to_dict(),
    }


def import_basin_into_registry_core(
    cursor: Any,
    sources: ImportSources,
    *,
    seed_output_river_segments: bool = True,
    backfill_output_segment_geometry: bool = True,
) -> dict[str, int]:
    """Single source of truth for the PR 2 per-basin write sequence.

    The order is fixed to satisfy ``core`` foreign keys and to keep Path C's
    ``core.river_segment_crosswalk`` populated for every importer that touches
    the registry core (generic registry CLI, QHH production bootstrap, future
    basins). It must run inside an already-opened transaction so the per-basin
    atomicity guarantee is preserved by the caller (the existing
    ``with _transaction(database_url) as cursor`` block in
    ``_import_prepared_sources`` and the equivalent
    ``with _transaction(database_url) as cursor`` block in
    ``_bootstrap_database`` for QHH).

    Sequence rationale:

    * ``_lock_basin_version`` MUST run before every other step (#2491): it is
      the first parent row lock, so every caller takes ``basin_version ->
      river_network_version`` (see ``_lock_river_network_version``).
    * ``_delete_legacy_seg_rows`` MUST run next: pre-PR-2 imports left
      ``<model>_seg_*`` rows under ``basin_version`` + ``river_network_version``
      parents whose ``segment_count`` / ``checksum`` were derived from
      ``gis/seg.shp``. PR 2 derives both from ``gis/river.shp`` and the values
      diverge, so leaving the legacy parent metadata in place would trip
      ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` on ``basin_version`` /
      ``river_network_version`` before any segment-level work could run.
      Freshly bootstrapped basins (no legacy rows) short-circuit out of the
      helper, and idempotent PR 2 -> PR 2 re-ingest still takes the existing
      ``_ensure_*`` no-op path.
    * ``_ensure_river_segment_crosswalk`` MUST run after
      ``_ensure_river_segments``: crosswalk rows reference
      ``(river_segment_id, river_network_version_id)`` via a FK declared at
      ``db/migrations/000004_core.sql:64-65``.
    * ``_backfill_output_segment_geometry`` runs last so the
      ``shud_output_river='true'`` rows seeded by
      ``_ensure_output_river_segments`` with NULL geom get their display
      LineString copied off the PR 2 reach-level
      ``core.river_segment.geom`` (matched on ``iRiv`` -> ``shud_riv_index``).

    The QHH production bootstrap owns its own per-row seeding for the SHUD
    output river layer (``_seed_output_segment_rows`` writes QHH-specific
    ``properties_json`` keys not present in the generic registry payload, then
    its own ``_backfill_output_segment_geometry`` call records provenance) and
    cannot share the generic ``_ensure_output_river_segments`` digest contract
    -- the two write the same ``shud_riv_*`` row identities with divergent
    properties_json, so a second QHH bootstrap would otherwise trip
    ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` on ``output_river_segment``. The
    ``seed_output_river_segments`` / ``backfill_output_segment_geometry``
    toggles let the QHH path delegate the registry-core write while keeping
    its bespoke output seeding intact.
    """

    _lock_basin_version(cursor, sources.ids["basin_version_id"])
    _delete_legacy_seg_rows(cursor, sources.ids)
    # Unconditional: ``_refresh_parent_version_materialization`` does its own
    # per-row probe and skips when a row is absent (fresh basin) or already
    # matches (same-version re-ingest). Always running it covers three cases
    # in one path: (a) pre-PR-2 -> PR-2 swap after seg-row purge,
    # (b) re-ingest of a previously-bootstrapped basin under a new
    # ``package_version`` (mesh_uri / model_package_uri / properties_json
    # would otherwise diverge from existing rows and trip CHECKSUM_CONFLICT),
    # (c) fresh basin first-time import (helper short-circuits).
    _refresh_parent_version_materialization(cursor, sources)
    row_counts: dict[str, int] = {
        "basin": _ensure_basin(cursor, sources),
        "basin_version": _ensure_basin_version(cursor, sources),
        "river_network_version": _ensure_river_network(cursor, sources),
        "river_segment": _ensure_river_segments(cursor, sources),
    }
    if seed_output_river_segments:
        row_counts["output_river_segment"] = _ensure_output_river_segments(cursor, sources)
    # FK-order critical: see helper docstring.
    row_counts["river_segment_crosswalk"] = _ensure_river_segment_crosswalk(cursor, sources)
    row_counts["mesh_version"] = _ensure_mesh(cursor, sources)
    row_counts["model_instance"] = _ensure_model_instance(cursor, sources)
    if backfill_output_segment_geometry:
        _backfill_output_segment_geometry(
            cursor,
            sources.ids["river_network_version_id"],
            only_missing=True,
        )
    return row_counts


def _ensure_river_network(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    source_uri = sources.geometry.river_network_source_uri
    checksum = sources.geometry.river_network_checksum
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id, segment_count, source_uri, checksum
        FROM core.river_network_version
        WHERE river_network_version_id = %s
        """,
        (ids["river_network_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and int(existing["segment_count"]) == sources.geometry.segment_count
            and existing["source_uri"] == source_uri
            and existing["checksum"] == checksum,
            "river_network_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.river_network_version (
            river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            ids["river_network_version_id"],
            ids["basin_version_id"],
            _version_label(sources),
            sources.geometry.segment_count,
            source_uri,
            checksum,
        ),
    )
    return 1


def _refresh_parent_version_materialization(cursor: Any, sources: ImportSources) -> None:
    """Refresh parent metadata (``basin_version`` / ``river_network_version`` /
    ``mesh_version`` / ``model_instance``) in place so the subsequent
    ``_ensure_*`` idempotency checks see the values the current import would
    have INSERTed and take their no-op path instead of raising
    ``BASINS_REGISTRY_CHECKSUM_CONFLICT``.

    Called unconditionally on every import. Each per-row UPDATE is gated by a
    presence probe so the helper is a no-op on first-time imports (rows
    absent) and a safe re-stamp on same-version re-ingest (values identical).
    The cases that actually need it:

    * Pre-PR-2 -> PR-2 swap: ``_delete_legacy_seg_rows`` purged ``<model>_seg_*``
      rows; parent ``segment_count`` / ``checksum`` derived from seg.shp now
      diverge from the river.shp-derived values the new import will write.
    * Re-ingesting any basin (bootstrapped or generic) under a different
      ``package_version``: ``mesh_version.mesh_uri`` and
      ``model_instance.model_package_uri`` still reference the old version,
      and the ``properties_json.package_checksum`` digests diverge. Without
      the in-place refresh below, ``_ensure_mesh`` / ``_ensure_model_instance``
      would raise CHECKSUM_CONFLICT before any reach-level work could land.
    """

    ids = sources.ids
    rnv_present = _fetch_optional(
        cursor,
        "SELECT 1 AS present FROM core.river_network_version WHERE river_network_version_id = %s",
        (ids["river_network_version_id"],),
    )
    if rnv_present is not None:
        cursor.execute(
            """
            UPDATE core.river_network_version
            SET segment_count = %s,
                source_uri = %s,
                checksum = %s
            WHERE river_network_version_id = %s
            """,
            (
                sources.geometry.segment_count,
                sources.geometry.river_network_source_uri,
                sources.geometry.river_network_checksum,
                ids["river_network_version_id"],
            ),
        )
    basin_version_present = _fetch_optional(
        cursor,
        "SELECT 1 AS present FROM core.basin_version WHERE basin_version_id = %s",
        (ids["basin_version_id"],),
    )
    if basin_version_present is not None:
        cursor.execute(
            """
            UPDATE core.basin_version
            SET source_uri = %s,
                checksum = %s
            WHERE basin_version_id = %s
            """,
            (
                sources.geometry.domain_source_uri,
                sources.geometry.domain_checksum,
                ids["basin_version_id"],
            ),
        )
    mesh_version_existing = _fetch_optional(
        cursor,
        """
        SELECT properties_json
        FROM core.mesh_version
        WHERE mesh_version_id = %s
        """,
        (ids["mesh_version_id"],),
    )
    if mesh_version_existing is not None:
        existing_mesh_checksum = _json_dict(mesh_version_existing.get("properties_json")).get(
            "package_checksum"
        )
        new_package_checksum = sources.manifest.get("package_checksum")
        if (
            existing_mesh_checksum is not None
            and new_package_checksum is not None
            and existing_mesh_checksum != new_package_checksum
        ):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_CHECKSUM_CONFLICT",
                "mesh_version.properties_json.package_checksum drifted from incoming manifest "
                "package_checksum without a corresponding mesh_version_id change; refusing to "
                "silently overwrite (use a new package_version to legitimately re-ingest).",
                model_id=ids["model_id"],
                details={"resource": "mesh_version"},
            )
        mesh_uri = _mesh_uri(sources)
        mesh_checksum = _source_checksum(
            sources, f"{sources.model['shud_input_name']}.sp.mesh"
        )
        mesh_properties = {
            "basin_slug": sources.model.get("basin_slug"),
            "shud_input_name": sources.model.get("shud_input_name"),
            "manifest_uri": sources.manifest.get("manifest_uri"),
            "package_checksum": sources.manifest.get("package_checksum"),
            "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
            "source_path": sources.model.get("source_path"),
            "resolved_source_path": sources.model.get("resolved_source_path"),
        }
        cursor.execute(
            """
            UPDATE core.mesh_version
            SET mesh_uri = %s,
                checksum = %s,
                properties_json = %s
            WHERE mesh_version_id = %s
            """,
            (
                mesh_uri,
                mesh_checksum,
                _json(mesh_properties),
                ids["mesh_version_id"],
            ),
        )
    model_instance_existing = _fetch_optional(
        cursor,
        """
        SELECT resource_profile
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (ids["model_id"],),
    )
    if model_instance_existing is not None:
        existing_model_checksum = _json_dict(model_instance_existing.get("resource_profile")).get(
            "package_checksum"
        )
        new_package_checksum = sources.manifest.get("package_checksum")
        if (
            existing_model_checksum is not None
            and new_package_checksum is not None
            and existing_model_checksum != new_package_checksum
        ):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_CHECKSUM_CONFLICT",
                "model_instance.resource_profile.package_checksum drifted from incoming manifest "
                "package_checksum without a corresponding model_id change; refusing to silently "
                "overwrite (use a new package_version to legitimately re-ingest).",
                model_id=ids["model_id"],
                details={"resource": "model_instance"},
            )
        cursor.execute(
            """
            UPDATE core.model_instance
            SET model_package_uri = %s,
                resource_profile = %s
            WHERE model_id = %s
            """,
            (
                sources.manifest["model_package_uri"],
                _json(_resource_profile(sources)),
                ids["model_id"],
            ),
        )


def _lock_basin_version(cursor: Any, basin_version_id: str) -> None:
    """Take the parent ``core.basin_version`` row lock (#2491).

    ``import_basin_into_registry_core`` calls this before any other statement,
    so the generic import locks ``basin_version`` before its
    ``_refresh_parent_version_materialization`` ``UPDATE``s the network row --
    the order ``qhh_bootstrap_registry.py::_lock_qhh_basin_scope`` (``FOR
    UPDATE``) already takes. Without it the generic import went network ->
    basin_version and deadlocked against a concurrent bootstrap of the same
    existing basin (ABBA). ``FOR NO KEY UPDATE`` is the lock the later
    non-key ``UPDATE core.basin_version`` takes anyway; inside the bootstrap's
    transaction, which already holds ``FOR UPDATE``, it is a no-op.

    A missing row (first import) locks nothing and returns nothing; the
    concurrent first-import unique-key race is out of scope.
    """
    cursor.execute(
        """
        SELECT 1
        FROM core.basin_version
        WHERE basin_version_id = %s
        FOR NO KEY UPDATE
        """,
        (basin_version_id,),
    )


def _lock_river_network_version(cursor: Any, river_network_version_id: str) -> None:
    """Take the parent ``core.river_network_version`` row lock (#2157).

    Parent-lock order (#2491): ``basin_version -> river_network_version ->
    river_segment``. ``import_basin_into_registry_core`` takes
    ``_lock_basin_version`` first, then this row (its rnv ``UPDATE``), then the
    segment rows; the QHH bootstrap takes the same basin_version row first in
    ``_lock_qhh_basin_scope``.

    Lock-order invariant: every production path that rewrites EXISTING
    ``core.river_segment`` rows in place -- the ``UPDATE`` in
    ``_backfill_output_segment_geometry`` and the ``ON CONFLICT DO UPDATE``
    upsert in ``qhh_production_bootstrap.py::_seed_output_segment_rows`` (via
    both of its entry points) -- takes this lock BEFORE its first statement
    that touches those rows, so all in-place writers acquire parent then
    children. The import transaction (``_refresh_parent_version_materialization``
    ``UPDATE``s the parent first) already follows that order; before this lock
    the backfill-only transactions went children -> parent (the
    ``geometry_generation`` bump) and deadlocked against it (ABBA).

    ``FOR NO KEY UPDATE`` is the lock a non-key ``UPDATE`` of the parent takes,
    so it queues behind (and blocks) the import's parent ``UPDATE`` and the
    generation bump, while FK checks from row-level segment ``INSERT``s (``FOR
    KEY SHARE``) do not conflict with it. Re-taking it inside a transaction
    that already holds it (``_import_basin`` -> ``import_basin_into_registry_core``
    -> backfill) is a no-op, never a self-deadlock.

    Boundary: the invariant covers in-place rewrites of existing rows only.
    Row-level ``INSERT`` of new segments (``_ensure_river_segments``,
    ``db/seeds/seed_demo.py``'s ``ON CONFLICT DO NOTHING``) and
    ``_delete_legacy_seg_rows`` are outside it: they never lock a row either
    in-place writer targets. The same boundary holds for the parent-lock
    order: ``_delete_legacy_seg_rows`` DELETEs ``river_segment`` rows before
    the rnv ``UPDATE`` (after the basin_version lock), and that row-level
    DELETE is not part of the ``basin_version -> river_network_version``
    invariant.

    Precondition: the parent row already exists. The statement does not fetch
    or assert the row -- on a missing row it simply locks nothing -- so every
    caller runs after ``_ensure_river_network`` or against a network that a
    ``core.model_instance`` row already references.
    """
    cursor.execute(
        """
        SELECT 1
        FROM core.river_network_version
        WHERE river_network_version_id = %s
        FOR NO KEY UPDATE
        """,
        (river_network_version_id,),
    )


def _backfill_output_segment_geometry(
    cursor: Any,
    river_network_version_id: str,
    *,
    only_missing: bool = False,
) -> int:
    """Backfill SHUD output-river (`.sp.riv`) display geometry onto the
    ``shud_output_river='true'`` reach rows that ``_ensure_output_river_segments``
    deliberately seeds with NULL geom (display geometry is a separate concern from
    the row identities the verifier/parser need).

    PR 2 reach-level source: ``core.river_segment`` holds one row per ``.sp.riv``
    reach (from ``gis/river.shp``) with the reach's single-part LineString in
    ``geom`` and ``iRiv``/``Type`` recorded in properties. The output reach row keyed
    ``{model_id}_shud_riv_{N:06d}`` carries ``shud_riv_index=N`` and lines up
    1:1 with the parser-loaded reach row keyed ``{model_id}_reach_{N:06d}``
    whose properties carry ``iRiv=N``. We copy ``geom`` + ``length_m`` + the
    source stream ``Type`` from the parent reach onto the output row in one SQL
    UPDATE. The stream class lets low-zoom MVT use real network topology instead
    of dropping a tile when every local q_down value is tied at zero.

    ``length_m`` reflects the reach's true channel length (the reach row's
    ``length_m`` from river.shp's ``Length``). Provenance is stamped into
    ``properties_json`` under the historical ``gis_rivseg_iRiv`` label so the
    qhh bootstrap idempotency comparison in
    ``_output_segment_idempotency_properties`` keeps matching. With
    ``only_missing`` updates NULL-geom reaches and existing output reaches that
    still lack a source ``Type``. A source without ``Type`` does not cause a
    geometry-complete target to be rewritten on every pass.

    Lock order (#2157): the FIRST statement is ``_lock_river_network_version``
    -- parent row before any segment row, the order every in-place
    ``core.river_segment`` writer follows (see that helper for the invariant
    and its boundary). It is taken before the candidate SELECT, not after the
    early exits, so candidates and source reaches are read under the lock: a
    snapshot taken before waiting on a concurrent import could otherwise
    overwrite geometry that import has just committed. Cost: a
    geometry-complete ``only_missing=True`` tick holds the parent lock until
    its transaction ends, yet still returns 0 and bumps nothing.
    """
    from psycopg2.extras import execute_values

    def _cell(row: Any, key: str, index: int) -> Any:
        # Tolerate both plain (tuple) and RealDict cursors across callers/tests.
        return row[key] if isinstance(row, dict) else row[index]

    _lock_river_network_version(cursor, river_network_version_id)

    # Output reaches still needing geometry (honour only_missing). The index is
    # matched as text, and a non-numeric shud_riv_index is filtered out here, so
    # a malformed sibling row can never abort the batch on an integer cast.
    cursor.execute(
        """
        SELECT river_segment_id,
               properties_json->>'shud_riv_index' AS shud_riv_index,
               geom IS NULL AS geom_missing
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
          AND (properties_json->>'shud_riv_index') ~ '^[0-9]+$'
          AND (NOT %s OR geom IS NULL OR NOT properties_json ? 'Type')
        """,
        (river_network_version_id, only_missing),
    )
    reaches_by_index: dict[str, list[tuple[str, bool]]] = {}
    for row in cursor.fetchall():
        reaches_by_index.setdefault(_cell(row, "shud_riv_index", 1), []).append(
            (_cell(row, "river_segment_id", 0), bool(_cell(row, "geom_missing", 2)))
        )
    if not reaches_by_index:
        return 0

    # Map each output row to the parser-loaded reach row whose ``iRiv`` matches
    # the output's ``shud_riv_index``. The reach row carries the single-part
    # LineString geom from gis/river.shp wrapped as MultiLineString at insert
    # time. We match on the text form of iRiv so a non-numeric value never
    # aborts the read. We explicitly exclude the output rows themselves
    # (``shud_output_river=true``) so a NULL-geom output sibling can never spoof
    # its own backfill source.
    cursor.execute(
        """
        SELECT
            properties_json->>'iRiv' AS shud_riv_index,
            ST_AsText(geom) AS geom_wkt,
            length_m,
            properties_json->'Type' AS stream_type
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND geom IS NOT NULL
          AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
          AND properties_json ? 'iRiv'
          AND (properties_json->>'iRiv') ~ '^[0-9]+$'
        """,
        (river_network_version_id,),
    )
    reach_geom_by_index: dict[str, tuple[str, float | None, Any]] = {}
    for row in cursor.fetchall():
        index = _cell(row, "shud_riv_index", 0)
        geom_wkt = _cell(row, "geom_wkt", 1)
        length_m = _cell(row, "length_m", 2)
        stream_type = _cell(row, "stream_type", 3)
        if not geom_wkt:
            continue
        reach_geom_by_index[index] = (
            str(geom_wkt),
            float(length_m) if length_m is not None else None,
            stream_type,
        )

    updates: list[tuple[str, str, float | None, str, str]] = []
    for index, reach_rows in reaches_by_index.items():
        reach_geom = reach_geom_by_index.get(index)
        if reach_geom is None:
            continue
        geom_wkt, total_length, stream_type = reach_geom
        provenance_payload = {
            "geometry_source": "gis_rivseg_iRiv",
            "geometry_source_segment_count": 1,
            "geometry_source_length_m": total_length,
        }
        if stream_type is not None:
            provenance_payload["Type"] = stream_type
        provenance = json.dumps(provenance_payload)
        for reach_id, geom_missing in reach_rows:
            if only_missing and not geom_missing and stream_type is None:
                continue
            updates.append((reach_id, geom_wkt, total_length, provenance, river_network_version_id))

    if not updates:
        return 0

    # ST_Length(geom) > 0 drops a degenerate (coincident-vertex) line so the reach
    # is left NULL rather than written unrenderable. execute_values pages the batch
    # (default 100 rows/page) and cursor.rowcount would then report only the LAST
    # page, undercounting; RETURNING + fetch=True concatenates every page's updated
    # rows so the returned count is accurate and paging-safe for any basin size.
    # #2158: the network id rides in the VALUES tuple (5th column) instead of a
    # second bind because execute_values accepts exactly one %s placeholder;
    # matching the full composite PK keeps a same-id row in another network
    # untouched.
    updated_rows = execute_values(
        cursor,
        """
        UPDATE core.river_segment AS target SET
            geom = source.geom,
            length_m = source.length_m,
            properties_json = target.properties_json || source.provenance
        FROM (
            SELECT
                value.river_segment_id::text AS river_segment_id,
                ST_Multi(ST_GeomFromText(value.wkt, 4490)) AS geom,
                value.length_m::double precision AS length_m,
                value.provenance::jsonb AS provenance,
                value.river_network_version_id::text AS river_network_version_id
            FROM (VALUES %s) AS value(river_segment_id, wkt, length_m, provenance, river_network_version_id)
        ) AS source
        WHERE target.river_segment_id = source.river_segment_id
          AND target.river_network_version_id = source.river_network_version_id
          AND ST_Length(source.geom) > 0
        RETURNING target.river_segment_id
        """,
        updates,
        template="(%s, %s, %s, %s, %s)",
        fetch=True,
    )
    if updated_rows:
        # #2031: the national display digests
        # (`services/tiles/mvt.py::national_discharge_source_version` /
        # `::national_river_network_source_version`) are computed from run rows
        # and the network's inventory metadata, none of which move when geometry
        # is rewritten UNDER an unchanged network version. This counter is that
        # missing signal, and it is bumped on the SAME cursor so it commits and
        # rolls back with the geometry it describes.
        #
        # Gated on `updated_rows` rather than on `updates`: `ST_Length > 0` can
        # empty the batch, and both early exits above return before this point.
        # Every bootstrap tick runs `only_missing=True` over already-complete
        # networks, so an unguarded bump would rotate every national tile cache
        # key on every tick for no data change at all.
        cursor.execute(
            """
            UPDATE core.river_network_version
            SET geometry_generation = geometry_generation + 1
            WHERE river_network_version_id = %s
            """,
            (river_network_version_id,),
        )
    return len(updated_rows)


def _model_active_state(database_url: str, model_id: str) -> bool:
    try:
        with _transaction(database_url) as cursor:
            row = _fetch_optional(
                cursor,
                "SELECT active_flag FROM core.model_instance WHERE model_id = %s",
                (model_id,),
            )
    except Exception:
        return False
    return bool(row and row["active_flag"])


@contextmanager
def _transaction(database_url: str) -> Any:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
        ) from error
    connection = psycopg2.connect(database_url)
    connection.autocommit = False
    register_default_json(loads=json.loads, conn_or_curs=connection)
    register_default_jsonb(loads=json.loads, conn_or_curs=connection)
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
