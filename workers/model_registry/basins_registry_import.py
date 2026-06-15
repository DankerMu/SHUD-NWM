from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.common.auth_policy import (
    PolicyDecision,
    require_policy_evidence,
    trusted_internal_policy_decision,
)

from .basins_geometry import (
    SHAPEFILE_REQUIRED_SUFFIXES,
    SHUD_CANONICAL_SUFFIXES,
    BasinsGeometryError,
    ParsedBasinsGeometry,
    TrustedBasinsRoot,
    _merge_polyline_parts,
    parse_basins_geometry,
    safe_basins_file_sha256,
    trusted_basins_root,
)

BASINS_REGISTRY_IMPORT_SCHEMA_VERSION = "basins.registry_import.v1"
RIVER_SEGMENT_INSERT_PAGE_SIZE = 1000
PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID = "unknown"


class BasinsRegistryImportError(RuntimeError):
    """Raised when a Basins package cannot be imported into the registry."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        model_id: str | None = None,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.model_id = model_id
        self.path = path
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.model_id is not None:
            payload["model_id"] = self.model_id
        if self.path is not None:
            payload["path"] = self.path
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class ImportSources:
    inventory: dict[str, Any]
    manifest: dict[str, Any]
    model: dict[str, Any]
    input_dir: TrustedBasinsRoot
    source_root: Path
    ids: dict[str, str]
    geometry: ParsedBasinsGeometry
    manifest_checksums: dict[str, str]


def import_basins_registry(
    *,
    inventory_path: str | Path,
    package_manifest_path: str | Path,
    database_url: str | None = None,
    output_path: str | Path | None = None,
    policy_decision: PolicyDecision | None = None,
    preflight_policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
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


def _prepare_sources(
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    *,
    inventory_raw_checksum: str | None = None,
) -> ImportSources:
    model_id = _required_str(manifest, "model_id", "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID")
    model = _find_inventory_model(inventory, model_id)
    if manifest.get("schema_version") != "basins.package.v1":
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest schema_version must be basins.package.v1.",
            model_id=model_id,
        )
    if (
        manifest.get("package_checksum") in (None, "")
        or manifest.get("model_package_uri") in (None, "")
        or manifest.get("manifest_uri") in (None, "")
    ):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest must include package_checksum, model_package_uri, and manifest_uri.",
            model_id=model_id,
        )
    if model.get("status") != "valid" or model.get("default_import_eligible") is not True:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_IMPORTABLE",
            "Basins model is not importable from this inventory.",
            model_id=model_id,
            path=str(model.get("source_path") or ""),
        )
    if manifest.get("basin_slug") not in (None, model.get("basin_slug")):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest basin_slug does not match inventory.",
            model_id=model_id,
        )

    inventory_root = _inventory_root(inventory, model_id)
    inventory_relative_root = _recorded_relative_inventory_root(inventory)
    source_root = _source_root(inventory_root, inventory_relative_root, model, model_id)
    input_dir = _input_dir(inventory_root, inventory_relative_root, source_root, model, model_id)
    _validate_manifest_source_identity(
        inventory,
        manifest,
        model,
        source_root,
        model_id,
        inventory_raw_checksum=inventory_raw_checksum,
    )
    _verify_model_id_matches_canonical_identity(model, model_id)
    required_files = model.get("required_files")
    if not isinstance(required_files, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing required_files.",
            model_id=model_id,
        )
    expected_checksums = _expected_manifest_checksums(manifest, model, model_id)
    _verify_inventory_checksums_match_manifest(expected_checksums, model, model_id)
    try:
        geometry = parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name=_required_model_str(model, "shud_input_name", model_id),
            required_files=required_files,
            expected_checksums=expected_checksums,
        )
    except BasinsGeometryError as error:
        _raise_geometry_import_error(error, model_id)
    _validate_manifest_included_files(manifest, model, input_dir, model_id, expected_checksums=expected_checksums)
    return ImportSources(
        inventory=inventory,
        manifest=manifest,
        model=model,
        input_dir=input_dir,
        source_root=source_root,
        ids=_registry_ids(model, model_id),
        geometry=geometry,
        manifest_checksums=expected_checksums,
    )


def _import_prepared_sources(
    sources: ImportSources,
    database_url: str,
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
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
            row_counts = {
                "basin": _ensure_basin(cursor, sources),
                "basin_version": _ensure_basin_version(cursor, sources),
                "river_network_version": _ensure_river_network(cursor, sources),
                "river_segment": _ensure_river_segments(cursor, sources),
                "output_river_segment": _ensure_output_river_segments(cursor, sources),
                "mesh_version": _ensure_mesh(cursor, sources),
                "model_instance": _ensure_model_instance(cursor, sources),
            }
            # NOTE: output-river reach rows are left NULL-geom here on purpose --
            # display geometry is a separate concern (see _ensure_output_river_segments).
            # The seeding orchestrators stitch it on after import:
            # qhh -> qhh_production_bootstrap, generic -> node27 autopipe
            # (_backfill_output_segment_geometry, the single shared SQL).
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


def _require_import_policy(
    model_id: str,
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
) -> PolicyDecision:
    if trusted_internal:
        policy_decision = trusted_internal_policy_decision(
            "models.switch_version",
            target_type="model_registry",
            target_id=model_id,
            actor_id="trusted-internal:basins-registry-import",
            roles=("sys_admin",),
        )
    decision = require_policy_evidence(
        policy_decision,
        action_id="models.switch_version",
        target_type="model_registry",
        target_id=model_id,
    )
    if decision.decision != "allow":
        raise BasinsRegistryImportError(
            decision.reason_code,
            decision.reason,
            model_id=model_id,
            details={"policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )
    return decision


def _require_public_import_preflight_policy(
    *,
    policy_decision: PolicyDecision | None = None,
    trusted_internal: bool = False,
) -> None:
    if trusted_internal:
        return
    decision = require_policy_evidence(
        policy_decision,
        action_id="models.switch_version",
        target_type="model_registry",
        target_id=PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
    )
    if decision.decision != "allow":
        raise BasinsRegistryImportError(
            decision.reason_code,
            decision.reason,
            model_id=PUBLIC_REGISTRY_IMPORT_UNKNOWN_TARGET_ID,
            details={"policy_decision": decision.to_dict(), "no_mutation_expected": True},
        )


def _ensure_basin(cursor: Any, sources: ImportSources) -> int:
    basin_id = sources.ids["basin_id"]
    existing = _fetch_optional(cursor, "SELECT basin_id FROM core.basin WHERE basin_id = %s", (basin_id,))
    if existing is not None:
        return 0
    cursor.execute(
        """
        INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
        VALUES (%s, %s, %s, %s)
        """,
        (
            basin_id,
            _basin_name(sources.model),
            "Basins",
            "Imported from Basins discovery inventory and package manifest.",
        ),
    )
    return 1


def _ensure_basin_version(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    source_uri = sources.geometry.domain_source_uri
    checksum = sources.geometry.domain_checksum
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_id, source_uri, checksum
        FROM core.basin_version
        WHERE basin_version_id = %s
        """,
        (ids["basin_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_id"] == ids["basin_id"]
            and existing["source_uri"] == source_uri
            and existing["checksum"] == checksum,
            "basin_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
        )
        VALUES (%s, %s, %s, ST_GeomFromText(%s, 4490), false, %s, %s)
        """,
        (
            ids["basin_version_id"],
            ids["basin_id"],
            _version_label(sources),
            sources.geometry.domain_wkt,
            source_uri,
            checksum,
        ),
    )
    return 1


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


def _ensure_river_segments(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    existing = _fetch_optional(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
        """,
        (ids["river_network_version_id"],),
    )
    existing_count = int(existing["count"]) if existing is not None else 0
    if existing_count:
        _require_existing(existing_count == sources.geometry.segment_count, "river_segment", ids["model_id"])
        _require_existing(
            _existing_river_segment_digest(cursor, ids["river_network_version_id"])
            == _incoming_river_segment_digest(sources),
            "river_segment",
            ids["model_id"],
        )
        return 0
    try:
        from psycopg2.extras import Json, execute_values
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
            model_id=ids["model_id"],
        ) from error
    inserted = 0
    for chunk in _chunks(sources.geometry.river_segments, RIVER_SEGMENT_INSERT_PAGE_SIZE):
        rows = [
            (
                segment.river_segment_id,
                ids["river_network_version_id"],
                segment.segment_order,
                segment.downstream_segment_id,
                segment.length_m,
                segment.geom_wkt,
                Json(
                    {
                        **segment.properties,
                        "basin_slug": sources.model.get("basin_slug"),
                        "shud_input_name": sources.model.get("shud_input_name"),
                    }
                ),
            )
            for segment in chunk
        ]
        execute_values(
            cursor,
            """
            INSERT INTO core.river_segment (
                river_segment_id, river_network_version_id, segment_order, downstream_segment_id,
                length_m, geom, properties_json
            )
            VALUES %s
            """,
            rows,
            template="(%s, %s, %s, %s, %s, ST_GeomFromText(%s, 4490), %s)",
            page_size=RIVER_SEGMENT_INSERT_PAGE_SIZE,
        )
        inserted += len(rows)
    return inserted


def _ensure_output_river_segments(cursor: Any, sources: ImportSources) -> int:
    """Seed the `.sp.riv` SHUD output/product river layer.

    SHUD discharge output is keyed on the coarser `.sp.riv` reach topology
    (``output_segment_count`` rows), not the finer ``seg.shp``/`.sp.rivseg`
    display geometry the generic import already records. These rows are tagged
    ``properties_json->>'shud_output_river'='true'`` and carry deterministic ids
    ``{model_id}_shud_riv_{index:06d}`` so the forecast output verifier and the
    output parser select the correct column/row count. Geometry is left NULL
    here: display geometry is a separate concern and the verifier/parser only
    need the row identities, ordering, and count (geom column is nullable).
    """

    ids = sources.ids
    output_segment_count = sources.geometry.output_segment_count
    incoming = _output_river_segment_rows(sources)
    existing = _fetch_optional(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
        """,
        (ids["river_network_version_id"],),
    )
    existing_count = int(existing["count"]) if existing is not None else 0
    if existing_count:
        _require_existing(existing_count == output_segment_count, "output_river_segment", ids["model_id"])
        _require_existing(
            _existing_output_river_segment_digest(cursor, ids["river_network_version_id"])
            == _output_river_segment_digest(incoming),
            "output_river_segment",
            ids["model_id"],
        )
        return 0
    if not incoming:
        return 0
    try:
        from psycopg2.extras import Json, execute_values
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
            model_id=ids["model_id"],
        ) from error
    inserted = 0
    for chunk in _chunks(incoming, RIVER_SEGMENT_INSERT_PAGE_SIZE):
        rows = [
            (
                row["river_segment_id"],
                ids["river_network_version_id"],
                row["segment_order"],
                Json(row["properties"]),
            )
            for row in chunk
        ]
        execute_values(
            cursor,
            """
            INSERT INTO core.river_segment (
                river_segment_id, river_network_version_id, segment_order, properties_json
            )
            VALUES %s
            """,
            rows,
            template="(%s, %s, %s, %s)",
            page_size=RIVER_SEGMENT_INSERT_PAGE_SIZE,
        )
        inserted += len(rows)
    return inserted


def _backfill_output_segment_geometry(
    cursor: Any,
    river_network_version_id: str,
    *,
    only_missing: bool = False,
    record_geometry_source: bool = True,
) -> int:
    """Stitch SHUD output-river (`.sp.riv`) display geometry from the finer GIS
    river segments onto the ``shud_output_river='true'`` reach rows.

    ``_ensure_output_river_segments`` deliberately seeds those reach rows with a
    NULL geom. Without this backfill the national / single-run MVT JOINs the
    reach rows but renders nothing — the live display can neither draw nor click
    those reaches (the heihe symptom). Every finer GIS segment is grouped by
    ``source_raw_segment_id`` (the SHUD reach index) and the group's parts are
    greedy-stitched (``_merge_polyline_parts``) into ONE continuous LineString
    per reach, matched onto the reach row via ``shud_riv_index``.

    Why greedy in Python, not ``ST_LineMerge`` in SQL: a reach's finer segments
    routinely meet only at coincident vertices that ST_LineMerge refuses to
    linearise (degree-3 nodes, an endpoint touching another part's interior,
    retraced overlaps), so it returns a MULTILINESTRING; keeping just the longest
    part then DROPS the rest of the channel and the reach renders broken (the
    node-27 heihe/qhh breakage: ~8-12% of reaches, up to ~57% of length lost).
    The even older ``ST_MakeLine(... ORDER BY segment_order)`` had the opposite
    failure: it linked every point in storage order, drawing a cross-ridge
    straight "jump" wherever record order != flow order. ``_merge_polyline_parts``
    chains parts by NEAREST endpoints (reversing as needed): parts that touch
    join with a zero-length link (continuous, no jump), a genuine source gap
    joins with the shortest bridge (faithful, never a storage-order artifact),
    and nothing is dropped. This is the SAME stitch the parser uses
    (``basins_geometry``), so the two paths stay in lock-step by construction.

    ``length_m`` is the SUM of the reach's finer segments (its true channel
    length). ``record_geometry_source`` additionally stamps provenance into
    ``properties_json``; the node-27 autopipe passes False so the output-river
    idempotency digest (which digests properties_json) stays stable, while the
    qhh bootstrap keeps it True. With ``only_missing`` only NULL-geom reaches are
    updated, so re-importing an already-correct basin updates zero rows -- and a
    basin whose reaches already hold geometry from an EARLIER backfill is NOT
    re-stitched without first resetting the affected reach geom to NULL (the
    autopipe also short-circuits already-seeded basins). New basins seed with
    NULL geom and are stitched normally.
    """
    from psycopg2.extras import execute_values

    def _cell(row: Any, key: str, index: int) -> Any:
        # Tolerate both plain (tuple) and RealDict cursors across callers/tests.
        return row[key] if isinstance(row, dict) else row[index]

    # Output reaches still needing geometry (honour only_missing). The index is
    # matched as text, and a non-numeric shud_riv_index is filtered out here, so
    # a malformed sibling row can never abort the batch on an integer cast.
    cursor.execute(
        """
        SELECT river_segment_id, properties_json->>'shud_riv_index' AS shud_riv_index
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
          AND (properties_json->>'shud_riv_index') ~ '^[0-9]+$'
          AND (NOT %s OR geom IS NULL)
        """,
        (river_network_version_id, only_missing),
    )
    reaches_by_index: dict[str, list[str]] = {}
    for row in cursor.fetchall():
        reaches_by_index.setdefault(_cell(row, "shud_riv_index", 1), []).append(
            _cell(row, "river_segment_id", 0)
        )
    if not reaches_by_index:
        return 0

    # Finer GIS segments grouped by SHUD reach index, each as an ordered point list.
    cursor.execute(
        """
        SELECT
            properties_json->>'source_raw_segment_id' AS shud_riv_index,
            (SELECT array_agg(ARRAY[ST_X(dp.geom), ST_Y(dp.geom)] ORDER BY dp.path)
             FROM ST_DumpPoints(geom) AS dp) AS points,
            length_m
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND geom IS NOT NULL
          AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
          AND properties_json ? 'source_raw_segment_id'
          AND (properties_json->>'source_raw_segment_id') ~ '^[0-9]+$'
        """,
        (river_network_version_id,),
    )
    parts_by_index: dict[str, list[list[tuple[float, float]]]] = {}
    length_by_index: dict[str, float] = {}
    count_by_index: dict[str, int] = {}
    for row in cursor.fetchall():
        index = _cell(row, "shud_riv_index", 0)
        points = _cell(row, "points", 1)
        length_m = _cell(row, "length_m", 2)
        if not points:
            continue
        part = [(float(x), float(y)) for x, y in points if x is not None and y is not None]
        if len(part) < 2:
            continue
        parts_by_index.setdefault(index, []).append(part)
        length_by_index[index] = length_by_index.get(index, 0.0) + (float(length_m) if length_m is not None else 0.0)
        count_by_index[index] = count_by_index.get(index, 0) + 1

    # Greedy-stitch each reach's parts into ONE continuous LineString (no dropped
    # part, no storage-order jump). length_m stays the SUM of the finer segments.
    updates: list[tuple[str, str, float | None, str]] = []
    for index, reach_ids in reaches_by_index.items():
        parts = parts_by_index.get(index)
        if not parts:
            continue
        merged = _merge_polyline_parts(parts)
        if len(merged) < 2:
            continue
        wkt = "LINESTRING(" + ",".join(f"{x!r} {y!r}" for x, y in merged) + ")"
        total_length = length_by_index.get(index)
        if record_geometry_source:
            provenance = json.dumps(
                {
                    "geometry_source": "gis_rivseg_iRiv",
                    "geometry_source_segment_count": count_by_index.get(index, len(parts)),
                    "geometry_source_length_m": total_length,
                }
            )
        else:
            provenance = "{}"
        for reach_id in reach_ids:
            updates.append((reach_id, wkt, total_length, provenance))
    if not updates:
        return 0

    # ST_Length(geom) > 0 drops a degenerate (coincident-vertex) line so the reach
    # is left NULL rather than written unrenderable. execute_values pages the batch
    # (default 100 rows/page) and cursor.rowcount would then report only the LAST
    # page, undercounting; RETURNING + fetch=True concatenates every page's updated
    # rows so the returned count is accurate and paging-safe for any basin size.
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
                ST_GeomFromText(value.wkt, 4490) AS geom,
                value.length_m::double precision AS length_m,
                value.provenance::jsonb AS provenance
            FROM (VALUES %s) AS value(river_segment_id, wkt, length_m, provenance)
        ) AS source
        WHERE target.river_segment_id = source.river_segment_id
          AND ST_Length(source.geom) > 0
        RETURNING target.river_segment_id
        """,
        updates,
        template="(%s, %s, %s, %s)",
        fetch=True,
    )
    return len(updated_rows)


def _output_river_segment_rows(sources: ImportSources) -> list[dict[str, Any]]:
    ids = sources.ids
    project_name = str(sources.model.get("shud_input_name") or "")
    offset = sources.geometry.segment_count
    rows: list[dict[str, Any]] = []
    for index in range(1, sources.geometry.output_segment_count + 1):
        rows.append(
            {
                "river_segment_id": f"{ids['model_id']}_shud_riv_{index:06d}",
                "segment_order": offset + index,
                "properties": {
                    "seed": "basins_registry_import",
                    "model_id": ids["model_id"],
                    "basin_id": ids["basin_id"],
                    "basin_version_id": ids["basin_version_id"],
                    "basin_slug": sources.model.get("basin_slug"),
                    "shud_input_name": sources.model.get("shud_input_name"),
                    "shud_output_river": True,
                    "shud_riv_index": index,
                    "source": f"{project_name}.sp.riv",
                    "output_identity": f"{project_name}.sp.riv:{index}",
                },
            }
        )
    return rows


def _output_river_segment_digest(rows: list[dict[str, Any]]) -> str:
    payload = [
        {
            "river_segment_id": str(row["river_segment_id"]),
            "segment_order": None if row["segment_order"] is None else int(row["segment_order"]),
            "properties": row["properties"],
        }
        for row in sorted(rows, key=lambda item: str(item["river_segment_id"]))
    ]
    return _sha256_json(payload)


def _existing_output_river_segment_digest(cursor: Any, river_network_version_id: str) -> str:
    cursor.execute(
        """
        SELECT river_segment_id, segment_order, properties_json
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
        ORDER BY river_segment_id
        """,
        (river_network_version_id,),
    )
    rows = [
        {
            "river_segment_id": str(row["river_segment_id"]),
            "segment_order": None if row["segment_order"] is None else int(row["segment_order"]),
            "properties": _json_dict(row["properties_json"]),
        }
        for row in cursor.fetchall()
    ]
    return _output_river_segment_digest(rows)


def _ensure_mesh(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    mesh_uri = _mesh_uri(sources)
    checksum = _source_checksum(sources, f"{sources.model['shud_input_name']}.sp.mesh")
    properties = {
        "basin_slug": sources.model.get("basin_slug"),
        "shud_input_name": sources.model.get("shud_input_name"),
        "manifest_uri": sources.manifest.get("manifest_uri"),
        "package_checksum": sources.manifest.get("package_checksum"),
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_path": sources.model.get("source_path"),
        "resolved_source_path": sources.model.get("resolved_source_path"),
    }
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id, mesh_uri, checksum, properties_json
        FROM core.mesh_version
        WHERE mesh_version_id = %s
        """,
        (ids["mesh_version_id"],),
    )
    if existing is not None:
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and existing["mesh_uri"] == mesh_uri
            and existing["checksum"] == checksum
            and _json_dict(existing["properties_json"]).get("package_checksum")
            == sources.manifest.get("package_checksum"),
            "mesh_version",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.mesh_version (
            mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            ids["mesh_version_id"],
            ids["basin_version_id"],
            _version_label(sources),
            mesh_uri,
            checksum,
            _json(properties),
        ),
    )
    return 1


def _ensure_model_instance(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    resource_profile = _resource_profile(sources)
    existing = _fetch_optional(
        cursor,
        """
        SELECT basin_version_id,
               river_network_version_id,
               mesh_version_id,
               model_package_uri,
               active_flag,
               resource_profile
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (ids["model_id"],),
    )
    if existing is not None:
        existing_profile = _json_dict(existing["resource_profile"])
        _require_existing(
            existing["basin_version_id"] == ids["basin_version_id"]
            and existing["river_network_version_id"] == ids["river_network_version_id"]
            and existing["mesh_version_id"] == ids["mesh_version_id"]
            and existing["model_package_uri"] == sources.manifest["model_package_uri"]
            and existing_profile.get("package_checksum") == sources.manifest.get("package_checksum")
            and existing_profile.get("source_inventory_checksum") == sources.manifest.get("source_inventory_checksum"),
            "model_instance",
            ids["model_id"],
        )
        return 0
    cursor.execute(
        """
        INSERT INTO core.model_instance (
            model_id, basin_version_id, river_network_version_id, mesh_version_id,
            calibration_version_id, shud_code_version, model_package_uri, active_flag, resource_profile
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, false, %s)
        """,
        (
            ids["model_id"],
            ids["basin_version_id"],
            ids["river_network_version_id"],
            ids["mesh_version_id"],
            f"{ids['model_id']}_calib_{_version_label(sources)}",
            "basins-shud",
            sources.manifest["model_package_uri"],
            _json(resource_profile),
        ),
    )
    return 1


def _resource_profile(sources: ImportSources) -> dict[str, Any]:
    return {
        "scheduler": "slurm",
        "partition": "standard",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": int(os.getenv("NHMS_BASINS_DEFAULT_CPUS", "4")),
        "memory_mb": int(os.getenv("NHMS_BASINS_DEFAULT_MEMORY_MB", "8192")),
        "walltime_minutes": int(os.getenv("NHMS_BASINS_DEFAULT_WALLTIME_MINUTES", "720")),
        "lineage": "basins_registry_import",
        "basin_slug": sources.model.get("basin_slug"),
        "shud_input_name": sources.model.get("shud_input_name"),
        "manifest_uri": sources.manifest.get("manifest_uri"),
        "package_checksum": sources.manifest.get("package_checksum"),
        "source_inventory_checksum": sources.manifest.get("source_inventory_checksum"),
        "source_inventory_schema_version": sources.manifest.get("source_inventory_schema_version"),
        "source_path": sources.model.get("source_path"),
        "resolved_source_path": sources.model.get("resolved_source_path"),
        "source_is_symlink": bool(sources.model.get("source_is_symlink", False)),
        "segment_count": sources.geometry.segment_count,
        "output_segment_count": sources.geometry.output_segment_count,
        "shud_evidence_counts": sources.geometry.evidence_counts,
    }


def _require_existing(condition: bool, resource: str, model_id: str) -> None:
    if not condition:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            f"Existing {resource} row does not match incoming Basins package/source checksums.",
            model_id=model_id,
            details={"resource": resource},
        )


def _existing_river_segment_digest(cursor: Any, river_network_version_id: str) -> str:
    cursor.execute(
        """
        SELECT river_segment_id,
               segment_order,
               downstream_segment_id,
               length_m,
               ST_AsText(geom) AS geom_wkt,
               properties_json
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
        ORDER BY COALESCE(segment_order, 2147483647), river_segment_id
        """,
        (river_network_version_id,),
    )
    rows = [
        _river_segment_digest_row(
            river_segment_id=row["river_segment_id"],
            segment_order=row["segment_order"],
            downstream_segment_id=row["downstream_segment_id"],
            length_m=row["length_m"],
            geom_wkt=row["geom_wkt"],
            properties=_json_dict(row["properties_json"]),
        )
        for row in cursor.fetchall()
    ]
    return _sha256_json(rows)


def _incoming_river_segment_digest(sources: ImportSources) -> str:
    rows = [
        _river_segment_digest_row(
            river_segment_id=segment.river_segment_id,
            segment_order=segment.segment_order,
            downstream_segment_id=segment.downstream_segment_id,
            length_m=segment.length_m,
            geom_wkt=segment.geom_wkt,
            properties={
                **segment.properties,
                "basin_slug": sources.model.get("basin_slug"),
                "shud_input_name": sources.model.get("shud_input_name"),
            },
        )
        for segment in sorted(
            sources.geometry.river_segments,
            key=lambda item: (
                item.segment_order if item.segment_order is not None else 2147483647,
                item.river_segment_id,
            ),
        )
    ]
    return _sha256_json(rows)


def _river_segment_digest_row(
    *,
    river_segment_id: Any,
    segment_order: Any,
    downstream_segment_id: Any,
    length_m: Any,
    geom_wkt: Any,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return {
        "river_segment_id": str(river_segment_id),
        "segment_order": None if segment_order is None else int(segment_order),
        "downstream_segment_id": None if downstream_segment_id is None else str(downstream_segment_id),
        "length_m": None if length_m is None else float(length_m),
        "geom_wkt": _normalize_wkt(str(geom_wkt or "")),
        "properties": properties,
    }


def _normalize_wkt(value: str) -> str:
    compact_commas = re.sub(r"\s*,\s*", ",", value.strip())
    return re.sub(r"\s+", " ", compact_commas)


def _find_inventory_model(inventory: dict[str, Any], model_id: str) -> dict[str, Any]:
    models = inventory.get("models")
    if not isinstance(models, list):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory JSON must contain a models array.",
            model_id=model_id,
        )
    matches = [model for model in models if isinstance(model, dict) and model.get("model_id") == model_id]
    if len(matches) > 1:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_ID_DUPLICATE",
            "Basins inventory contains duplicate records for model_id.",
            model_id=model_id,
        )
    if not matches:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_MODEL_NOT_FOUND",
            "Basins model_id was not found in inventory.",
            model_id=model_id,
        )
    return matches[0]


def _inventory_root(inventory: dict[str, Any], model_id: str) -> Path:
    value = inventory.get("resolved_root")
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory is missing resolved_root.",
            model_id=model_id,
        )
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins inventory resolved_root does not exist.",
            model_id=model_id,
            path=str(path),
        )
    return path


def _source_root(
    inventory_root: Path,
    inventory_relative_root: Path | None,
    model: dict[str, Any],
    model_id: str,
) -> Path:
    relative = _safe_relative(_required_model_str(model, "root_relative_resolved_path", model_id), model_id)
    source_root = (inventory_root / relative).resolve()
    _ensure_under_root(source_root, inventory_root, model_id)
    recorded_value = _required_model_str(model, "resolved_source_path", model_id)
    if not _recorded_path_matches_expected(recorded_value, source_root, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory resolved_source_path does not match root_relative_resolved_path.",
            model_id=model_id,
            path=recorded_value,
        )
    source_path_value = _required_model_str(model, "source_path", model_id)
    if not _recorded_path_matches_expected(source_path_value, source_root, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory source_path does not match root_relative_resolved_path.",
            model_id=model_id,
            path=source_path_value,
        )
    if not source_root.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins source directory does not exist.",
            model_id=model_id,
            path=str(source_root),
        )
    return source_root


def _input_dir(
    inventory_root: Path,
    inventory_relative_root: Path | None,
    source_root: Path,
    model: dict[str, Any],
    model_id: str,
) -> TrustedBasinsRoot:
    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    expected = source_root / "input" / shud_input_name
    for role, path in (
        ("input", source_root / "input"),
        ("shud_input_name", expected),
    ):
        _reject_directory_symlink(path, role, model_id)
    gis_dir = expected / "gis"
    if gis_dir.exists() or gis_dir.is_symlink():
        _reject_directory_symlink(gis_dir, "gis", model_id)
    final_resolved = expected.resolve()
    _ensure_under_root(final_resolved, inventory_root, model_id)
    _ensure_under_root(final_resolved, source_root, model_id)
    recorded_value = _required_model_str(model, "input_dir", model_id)
    if not _recorded_path_matches_expected(recorded_value, final_resolved, inventory_root, inventory_relative_root):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory input_dir does not match canonical source root and shud_input_name.",
            model_id=model_id,
            path=recorded_value,
        )
    gis_dir_value = model.get("gis_dir")
    if isinstance(gis_dir_value, str) and gis_dir_value:
        gis_resolved = gis_dir.resolve()
        _ensure_under_root(gis_resolved, inventory_root, model_id)
        _ensure_under_root(gis_resolved, source_root, model_id)
        if not _recorded_path_matches_expected(gis_dir_value, gis_resolved, inventory_root, inventory_relative_root):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_SOURCE_MISMATCH",
                "Basins inventory gis_dir does not match canonical source root and shud_input_name.",
                model_id=model_id,
                path=gis_dir_value,
            )
    if not expected.is_dir():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISSING",
            "Basins input directory does not exist.",
            model_id=model_id,
            path=str(expected),
        )
    try:
        return trusted_basins_root(expected, role="shud_input_name")
    except BasinsGeometryError as error:
        _raise_geometry_import_error(error, model_id)
        raise AssertionError("unreachable") from error


def _registry_ids(model: dict[str, Any], model_id: str) -> dict[str, str]:
    suggested = model.get("suggested_ids")
    if not isinstance(suggested, dict):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing suggested_ids.",
            model_id=model_id,
        )
    ids = {
        "basin_id": _required_mapping_str(suggested, "basin_id", model_id),
        "basin_version_id": _required_mapping_str(suggested, "basin_version_id", model_id),
        "river_network_version_id": _required_mapping_str(suggested, "river_network_version_id", model_id),
        "mesh_version_id": _required_mapping_str(suggested, "mesh_version_id", model_id),
        "model_id": model_id,
    }
    if suggested.get("model_id") not in (None, model_id):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins suggested model_id does not match package manifest model_id.",
            model_id=model_id,
        )
    return ids


def _validate_manifest_source_identity(
    inventory: dict[str, Any],
    manifest: dict[str, Any],
    model: dict[str, Any],
    source_root: Path,
    model_id: str,
    inventory_raw_checksum: str | None,
) -> None:
    expected = {
        "basin_slug": model.get("basin_slug"),
        "shud_input_name": model.get("shud_input_name"),
        "source_path": model.get("source_path"),
        "resolved_source_path": str(source_root),
        "source_is_symlink": bool(model.get("source_is_symlink", False)),
        "source_inventory_schema_version": inventory.get("schema_version"),
    }
    missing = [key for key in expected if key not in manifest or manifest.get(key) in (None, "")]
    mismatches = [
        key
        for key, expected_value in expected.items()
        if key not in missing and manifest.get(key) != expected_value
    ]
    source_inventory_checksum = manifest.get("source_inventory_checksum")
    if not isinstance(source_inventory_checksum, str) or not source_inventory_checksum:
        missing.append("source_inventory_checksum")
    if missing or mismatches:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest source identity does not match selected inventory model.",
            model_id=model_id,
            details={"fields": sorted({*missing, *mismatches})},
        )
    actual_inventory_checksum = inventory_raw_checksum or _sha256_json(inventory)
    if source_inventory_checksum != actual_inventory_checksum:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest source inventory checksum does not match selected inventory.",
            model_id=model_id,
            details={
                "fields": ["source_inventory_checksum"],
                "expected": actual_inventory_checksum,
                "actual": source_inventory_checksum,
            },
        )


def _validate_manifest_included_files(
    manifest: dict[str, Any],
    model: dict[str, Any],
    input_dir: TrustedBasinsRoot,
    model_id: str,
    *,
    expected_checksums: dict[str, str],
) -> None:
    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    canonical_paths = [f"{shud_input_name}{suffix}" for suffix in SHUD_CANONICAL_SUFFIXES.values()]
    canonical_paths.extend(
        f"gis/{layer}.{suffix}"
        for layer in ("domain", "river", "seg")
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES
    )
    missing = [relative_path for relative_path in canonical_paths if relative_path not in expected_checksums]
    if missing:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest is missing canonical runtime/GIS package entries.",
            model_id=model_id,
            details={"missing_included_files": missing},
        )

    conflicts: list[str] = []
    for relative_path in canonical_paths:
        manifest_sha = expected_checksums[relative_path]
        try:
            actual_sha = safe_basins_file_sha256(input_dir.path / relative_path, input_dir)
        except BasinsGeometryError as error:
            _raise_geometry_import_error(error, model_id, relative_path=relative_path)
        if actual_sha != manifest_sha:
            conflicts.append(relative_path)
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )


def _expected_manifest_checksums(
    manifest: dict[str, Any],
    model: dict[str, Any],
    model_id: str,
) -> dict[str, str]:
    included_files = manifest.get("included_files")
    if not isinstance(included_files, list):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
            "Basins package manifest included_files must be an array.",
            model_id=model_id,
        )
    expected: dict[str, str] = {}
    conflicts: list[str] = []
    for entry in included_files:
        if not isinstance(entry, dict) or not isinstance(entry.get("relative_path"), str):
            raise BasinsRegistryImportError(
                "BASINS_REGISTRY_PACKAGE_MANIFEST_INVALID",
                "Basins package manifest included_files entries must include relative_path.",
                model_id=model_id,
            )
        relative_path = _normalize_relative(entry["relative_path"], model_id)
        manifest_sha = entry.get("sha256")
        if not isinstance(manifest_sha, str) or not manifest_sha:
            conflicts.append(relative_path)
            continue
        expected.setdefault(relative_path, manifest_sha)

    shud_input_name = _required_model_str(model, "shud_input_name", model_id)
    canonical_paths = [f"{shud_input_name}{suffix}" for suffix in SHUD_CANONICAL_SUFFIXES.values()]
    canonical_paths.extend(
        f"gis/{layer}.{suffix}"
        for layer in ("domain", "river", "seg")
        for suffix in SHAPEFILE_REQUIRED_SUFFIXES
    )
    missing = [relative_path for relative_path in canonical_paths if relative_path not in expected]
    if missing:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins package manifest is missing canonical runtime/GIS package entries.",
            model_id=model_id,
            details={"missing_included_files": missing},
        )
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )
    return {relative_path: expected[relative_path] for relative_path in canonical_paths}


def _verify_inventory_checksums_match_manifest(
    expected_checksums: dict[str, str],
    model: dict[str, Any],
    model_id: str,
) -> None:
    checksums = model.get("checksums")
    known_checksums = checksums if isinstance(checksums, dict) else {}
    conflicts = [
        relative_path
        for relative_path, manifest_sha in expected_checksums.items()
        if isinstance(known_checksums.get(relative_path), str)
        and known_checksums[relative_path]
        and known_checksums[relative_path] != manifest_sha
    ]
    if conflicts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_CHECKSUM_CONFLICT",
            "Basins package manifest file checksums do not match selected inventory/source.",
            model_id=model_id,
            details={"relative_paths": sorted(set(conflicts))},
        )


def _raise_geometry_import_error(
    error: BasinsGeometryError,
    model_id: str,
    *,
    relative_path: str | None = None,
) -> None:
    error_code = error.error_code
    details = dict(error.details)
    if relative_path and error.error_code == "BASINS_REGISTRY_SOURCE_MISSING" and relative_path.startswith("gis/"):
        error_code = "BASINS_REGISTRY_GIS_SIDECAR_MISSING"
        details["missing_sidecar"] = relative_path
    raise BasinsRegistryImportError(
        error_code,
        str(error),
        model_id=model_id,
        path=error.path,
        details=details,
    ) from error


def _verify_model_id_matches_canonical_identity(model: dict[str, Any], model_id: str) -> None:
    basin_slug = _required_model_str(model, "basin_slug", model_id)
    root_relative = model.get("root_relative_resolved_path") or model.get("root_relative_path")
    if not isinstance(root_relative, str) or not root_relative:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            "Basins inventory model record is missing root-relative source path.",
            model_id=model_id,
        )
    canonical_slug = _normalize_relative(root_relative, model_id)
    expected_model_id = f"basins_{_slug_id(canonical_slug)}_shud"
    suggested = model.get("suggested_ids")
    suggested_model_id = suggested.get("model_id") if isinstance(suggested, dict) else None
    if (
        basin_slug != canonical_slug
        or model.get("model_id") != expected_model_id
        or suggested_model_id != expected_model_id
    ):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SOURCE_MISMATCH",
            "Basins inventory model_id does not match canonical source identity.",
            model_id=model_id,
        )


def _normalize_relative(value: str, model_id: str) -> str:
    path = _safe_relative(value, model_id)
    return path.as_posix()


def _slug_id(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", value).strip("_").lower()
    return normalized or "unknown"


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: str | Path, *, error_code: str, not_found_code: str) -> dict[str, Any]:
    payload, _ = _read_json_document(path, error_code=error_code, not_found_code=not_found_code)
    return payload


def _read_json_document(
    path: str | Path,
    *,
    error_code: str,
    not_found_code: str,
) -> tuple[dict[str, Any], bytes]:
    source = Path(path).expanduser()
    try:
        content = source.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except FileNotFoundError as error:
        raise BasinsRegistryImportError(
            not_found_code,
            f"JSON file cannot be read: {source}",
            path=str(source),
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BasinsRegistryImportError(error_code, f"JSON file is invalid: {source}", path=str(source)) from error
    if not isinstance(payload, dict):
        raise BasinsRegistryImportError(error_code, "JSON file must contain an object.", path=str(source))
    return payload, content


def _write_report(output_path: str | Path, report: dict[str, Any]) -> None:
    output = Path(output_path).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_OUTPUT_WRITE_FAILED",
            f"Basins registry import report cannot be written: {output}",
            path=str(output),
        ) from error


def _required_str(payload: dict[str, Any], key: str, error_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(error_code, f"Required field is missing: {key}")
    return value


def _required_model_str(model: dict[str, Any], key: str, model_id: str) -> str:
    value = model.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            f"Basins inventory model record is missing {key}.",
            model_id=model_id,
        )
    return value


def _required_mapping_str(payload: dict[str, Any], key: str, model_id: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_INVENTORY_INVALID",
            f"Basins suggested_ids is missing {key}.",
            model_id=model_id,
        )
    return value


def _safe_relative(value: str, model_id: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins inventory contains an unsafe root-relative path.",
            model_id=model_id,
            path=value,
        )
    return path


def _recorded_relative_inventory_root(inventory: dict[str, Any]) -> Path | None:
    root = inventory.get("root")
    if not isinstance(root, str) or not root:
        return None
    root_path = Path(root).expanduser()
    if root_path.is_absolute():
        return None
    try:
        normalized = _safe_path_relative(root_path.as_posix())
    except ValueError:
        return None
    if normalized == Path("."):
        return None
    return normalized


def _recorded_path_matches_expected(
    value: str,
    expected_path: Path,
    inventory_root: Path,
    inventory_relative_root: Path | None,
) -> bool:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve() == expected_path
    try:
        normalized = _safe_path_relative(path.as_posix())
    except ValueError:
        return False
    expected_relative_paths: set[Path] = set()
    for base in (expected_path, inventory_root):
        try:
            expected_relative_paths.add(expected_path.relative_to(base))
        except ValueError:
            continue
    if inventory_relative_root is not None:
        try:
            expected_relative_paths.add(inventory_relative_root / expected_path.relative_to(inventory_root))
        except ValueError:
            pass
    return normalized in expected_relative_paths


def _safe_path_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(value)
    return path


def _ensure_under_root(path: Path, root: Path, model_id: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source path resolves outside the inventory root.",
            model_id=model_id,
            path=str(path),
        ) from error


def _reject_directory_symlink(path: Path, role: str, model_id: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source directory cannot be safely inspected.",
            model_id=model_id,
            path=str(path),
            details={"role": role},
        ) from error
    if stat.S_ISLNK(mode):
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PATH_UNSAFE",
            "Basins source directory component is a symlink.",
            model_id=model_id,
            path=str(path),
            details={"role": role},
        )


def _basin_name(model: dict[str, Any]) -> str:
    basin_slug = str(model.get("basin_slug") or "")
    return basin_slug.replace("_", " ").replace("/", " / ").replace("-", " ").title() or str(model.get("model_id"))


def _version_label(sources: ImportSources) -> str:
    version = str(sources.manifest.get("version") or "vbasins")
    return version


def _mesh_uri(sources: ImportSources) -> str:
    relative = f"{sources.model['shud_input_name']}.sp.mesh"
    for entry in sources.manifest.get("included_files") or []:
        if isinstance(entry, dict) and entry.get("relative_path") == relative and entry.get("object_uri"):
            return str(entry["object_uri"])
    return str(sources.manifest["model_package_uri"]).rstrip("/") + "/" + relative


def _source_checksum(sources: ImportSources, relative_path: str) -> str | None:
    checksum = sources.manifest_checksums.get(relative_path)
    if isinstance(checksum, str) and checksum:
        return checksum
    checksums = sources.model.get("checksums")
    if isinstance(checksums, dict) and isinstance(checksums.get(relative_path), str):
        return str(checksums[relative_path])
    return None


def _json(value: dict[str, Any]) -> Any:
    try:
        from psycopg2.extras import Json
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
        ) from error
    return Json(value)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _fetch_optional(cursor: Any, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


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
