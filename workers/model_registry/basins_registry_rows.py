"""Basins registry row writers: basin / basin-version / river-segment /
crosswalk / output-segment / mesh / model-instance ``_ensure_*`` helpers and
their row builders (#2490 split of ``basins_registry_import``). The caller
``import_basin_into_registry_core`` stays in the facade and resolves every
helper through the facade's module globals.

``workers.model_registry.basins_registry_import`` stays the stable import path
and re-exports every name defined here.
"""

from __future__ import annotations

import os
from typing import Any

from .basins_geometry import BasinsGeometryError, CrosswalkRow, parse_seg_shp_crosswalk
from .basins_registry_digests import (
    _existing_output_river_segment_digest,
    _existing_river_segment_digest,
    _incoming_river_segment_digest,
    _output_river_segment_digest,
)
from .basins_registry_support import (
    RIVER_SEGMENT_INSERT_PAGE_SIZE,
    BasinsRegistryImportError,
    ImportSources,
    _basin_name,
    _chunks,
    _fetch_optional,
    _json,
    _json_dict,
    _mesh_uri,
    _raise_geometry_import_error,
    _require_existing,
    _source_checksum,
    _version_label,
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


def _ensure_river_segments(cursor: Any, sources: ImportSources) -> int:
    ids = sources.ids
    # Legacy seg-level rows from the pre-PR-2 ingestion contract (IDs of
    # the form ``<model>_seg_*``) are removed by ``_delete_legacy_seg_rows``
    # at the top of the per-basin transaction (see ``_import_prepared_sources``).
    # The purge MUST run before ``_ensure_basin_version`` /
    # ``_ensure_river_network`` so the new PR-2 checksums can be written
    # via the INSERT path; running it here would be too late.
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
            # PR 2: the parser now emits single-part LINESTRING WKT
            # (one row per reach from gis/river.shp). ST_Multi wraps it
            # into a single-part MultiLineString so the
            # geometry(MultiLineString, 4490) column type is preserved
            # without a schema change.
            template="(%s, %s, %s, %s, %s, ST_Multi(ST_GeomFromText(%s, 4490)), %s)",
            page_size=RIVER_SEGMENT_INSERT_PAGE_SIZE,
        )
        inserted += len(rows)
    return inserted


def _delete_legacy_seg_rows(cursor: Any, ids: dict[str, str]) -> bool:
    """Remove pre-PR-2 ``<model>_seg_*`` rows for this basin before reach insert.

    The crosswalk table has a FOREIGN KEY on
    ``(river_segment_id, river_network_version_id)`` referencing
    ``core.river_segment`` (db/migrations/000004_core.sql:64-65), so the
    delete order is strict: crosswalk children first, then the
    ``river_segment`` parents. Both are scoped by the per-basin
    ``river_network_version_id`` so other basins' rows stay untouched.

    When legacy rows are actually present we also refresh the sibling
    ``river_network_version`` / ``basin_version`` metadata
    (``segment_count`` / ``source_uri`` / ``checksum``) in place so the
    subsequent ``_ensure_river_network`` / ``_ensure_basin_version`` calls see
    the new PR-2 reach-level values and take their idempotent no-op path
    instead of raising ``BASINS_REGISTRY_CHECKSUM_CONFLICT``. Pre-PR-2 state
    had ``segment_count = seg.shp record count`` and a checksum derived from
    seg.shp; PR 2 derives both from ``river.shp`` and the counts/digests no
    longer match.

    We refresh rather than DELETE the parent rows because in production
    ``core.model_instance`` and ``core.mesh_version`` FK-reference
    ``core.basin_version`` / ``core.river_network_version`` without
    ``ON DELETE CASCADE`` (see ``db/migrations/000004_core.sql``): a DELETE
    would violate those FKs as soon as the basin has any registered models.
    Freshly-imported basins with no legacy rows short-circuit out of this
    branch and idempotent re-ingest (PR 2 -> PR 2) keeps its existing
    ``_ensure_*`` no-op path.
    """

    rnv_id = ids["river_network_version_id"]
    model_id = ids["model_id"]
    legacy_pattern = f"{model_id}_seg_%"
    legacy_present = _fetch_optional(
        cursor,
        """
        SELECT 1 AS present
        FROM core.river_segment
        WHERE river_network_version_id = %s
          AND river_segment_id LIKE %s
        LIMIT 1
        """,
        (rnv_id, legacy_pattern),
    )
    if legacy_present is None:
        return False
    cursor.execute(
        """
        DELETE FROM core.river_segment_crosswalk
        WHERE river_network_version_id = %s
          AND river_segment_id LIKE %s
        """,
        (rnv_id, legacy_pattern),
    )
    cursor.execute(
        """
        DELETE FROM core.river_segment
        WHERE river_network_version_id = %s
          AND river_segment_id LIKE %s
        """,
        (rnv_id, legacy_pattern),
    )
    return True


def _ensure_river_segment_crosswalk(cursor: Any, sources: ImportSources) -> int:
    """Write ``core.river_segment_crosswalk`` rows from ``gis/seg.shp``.

    Spec "Segment-to-reach crosswalk is preserved from gis/seg.shp": one
    crosswalk row per ``seg.shp`` record, keyed by
    ``(river_network_version_id, river_segment_id, source='basins_seg_shp')``
    with ``external_id = "<iRiv>:<iEle>"`` and ``properties_json`` carrying
    ``iRiv``/``iEle``/``segment_order``/``length_m``.

    Idempotent re-ingest is supplied by the unique-constraint upsert
    (``ON CONFLICT ... DO UPDATE``), matching the existing
    ``PsycopgModelRegistryStore.create_crosswalk_entries`` behaviour. Pure
    function up-front: the parsed crosswalk rows are built without DB I/O
    via ``parse_seg_shp_crosswalk`` + ``_build_river_segment_crosswalk_rows``,
    and the FK against ``core.river_segment`` is satisfied because this
    helper always runs AFTER ``_ensure_river_segments`` inside the same
    transaction.
    """

    try:
        from psycopg2.extras import Json, execute_values
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_PSYCOPG_MISSING",
            "psycopg2 is required for Basins registry import.",
            model_id=sources.ids["model_id"],
        ) from error
    ids = sources.ids
    crosswalk_rows = _crosswalk_rows_for_sources(sources)
    if not crosswalk_rows:
        return 0
    # Idempotency: if the basin already has the exact crosswalk row count
    # under source='basins_seg_shp', re-ingest is a no-op (matches the
    # other _ensure_* helpers in this module and avoids inflating the
    # already_imported row_counts response). The ON CONFLICT upsert path
    # below would otherwise UPDATE existing rows and report a positive
    # row_count even when nothing changed.
    existing = _fetch_optional(
        cursor,
        """
        SELECT COUNT(*) AS count
        FROM core.river_segment_crosswalk
        WHERE river_network_version_id = %s
          AND source = 'basins_seg_shp'
        """,
        (ids["river_network_version_id"],),
    )
    existing_count = int(existing["count"]) if existing is not None else 0
    if existing_count == len(crosswalk_rows):
        return 0
    db_rows = [
        (
            row["river_network_version_id"],
            row["river_segment_id"],
            row["source"],
            row["external_id"],
            Json(row["properties_json"]),
        )
        for row in crosswalk_rows
    ]
    inserted = 0
    for chunk in _chunks(db_rows, RIVER_SEGMENT_INSERT_PAGE_SIZE):
        execute_values(
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
            DO UPDATE SET
                river_segment_id = EXCLUDED.river_segment_id,
                properties_json = EXCLUDED.properties_json
            """,
            chunk,
            page_size=RIVER_SEGMENT_INSERT_PAGE_SIZE,
        )
        inserted += len(chunk)
    return inserted


def _crosswalk_rows_for_sources(sources: ImportSources) -> list[dict[str, Any]]:
    """Parse ``gis/seg.shp`` and build crosswalk rows for this basin's reaches.

    Failures are raised as :class:`BasinsRegistryImportError` so the caller's
    per-basin transaction rolls back atomically (spec "Per-basin ingest is
    transactional"). ``BASINS_REGISTRY_CROSSWALK_REACH_MISSING`` surfaces
    when ``seg.shp`` references a reach Index that ``river.shp`` does not
    list -- typically a stale model package.
    """

    ids = sources.ids
    input_dir = sources.input_dir
    seg_shp = input_dir.path / "gis" / "seg.shp"
    if not seg_shp.is_file():
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SEG_SHP_MISSING",
            "Basins seg.shp is missing; crosswalk rows cannot be written.",
            model_id=ids["model_id"],
            path=str(seg_shp),
        )
    try:
        import shapefile
    except ImportError as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_SHAPEFILE_DEPENDENCY_MISSING",
            "pyshp is required for Basins seg.shp crosswalk parsing.",
            model_id=ids["model_id"],
            path=str(seg_shp),
        ) from error
    try:
        reader = shapefile.Reader(str(seg_shp))
    except Exception as error:
        raise BasinsRegistryImportError(
            "BASINS_REGISTRY_GIS_PARSE_FAILED",
            f"Basins seg.shp could not be opened: {error.__class__.__name__}",
            model_id=ids["model_id"],
            path=str(seg_shp),
        ) from error
    try:
        try:
            segments = parse_seg_shp_crosswalk(reader)
        except BasinsGeometryError as error:
            _raise_geometry_import_error(error, ids["model_id"])
            raise AssertionError("unreachable") from error
    finally:
        reader.close()
    reach_indices = {
        int(segment.properties.get("iRiv"))
        for segment in sources.geometry.river_segments
        if segment.properties.get("iRiv") is not None
    }
    try:
        return _build_river_segment_crosswalk_rows(
            ids["model_id"],
            ids["river_network_version_id"],
            segments,
            reach_indices,
        )
    except BasinsGeometryError as error:
        _raise_geometry_import_error(error, ids["model_id"])
        raise AssertionError("unreachable") from error


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


def _build_river_segment_crosswalk_rows(
    model_id: str,
    river_network_version_id: str,
    segments: list[CrosswalkRow],
    reach_indices: set[int],
) -> list[dict[str, Any]]:
    """Construct ``core.river_segment_crosswalk`` insert rows from seg.shp data.

    Pure constructor: no DB calls, no IO. The output dict shape matches the
    columns of ``core.river_segment_crosswalk`` plus the embedded
    ``river_network_version_id`` (which the writer in PR 2 will lift into the
    outer ``create_crosswalk_entries`` payload). Each row references the
    parent reach via ``river_segment_id = f"{model_id}_reach_{iRiv:06d}"``,
    matching the new reach-level naming convention (see D5 in the
    ``feat-reach-geom-from-river-shp`` design doc).

    ``reach_indices`` is the set of valid reach ``Index`` values produced from
    the basin's ``gis/river.shp``. Any segment whose ``iRiv`` is not in that
    set is a referential-integrity violation between ``seg.shp`` and
    ``river.shp`` and triggers a fail-fast error before any DB write.

    Raises:
        BasinsGeometryError: with code
            ``BASINS_REGISTRY_CROSSWALK_REACH_MISSING`` when any segment's
            ``iRiv`` is not present in ``reach_indices``. The error payload
            includes ``missing_iRiv`` (sorted list of all offending iRiv
            values) so the operator can correlate to the source seg.shp.
    """

    missing_iriv = sorted({segment.iRiv for segment in segments if segment.iRiv not in reach_indices})
    if missing_iriv:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_CROSSWALK_REACH_MISSING",
            "Basins seg.shp references reach Index values not present in river.shp.",
            details={"missing_iRiv": missing_iriv, "model_id": model_id},
        )
    rows_by_external_id: dict[str, dict[str, Any]] = {}
    for segment in segments:
        external_id = f"{segment.iRiv}:{segment.iEle}"
        candidate = {
            "river_network_version_id": river_network_version_id,
            "river_segment_id": f"{model_id}_reach_{segment.iRiv:06d}",
            "source": "basins_seg_shp",
            "external_id": external_id,
            "properties_json": {
                "iRiv": int(segment.iRiv),
                "iEle": int(segment.iEle),
                "segment_order": int(segment.segment_order),
                "length_m": (None if segment.length_m is None else float(segment.length_m)),
            },
        }
        existing = rows_by_external_id.get(external_id)
        if existing is None:
            rows_by_external_id[external_id] = candidate
            continue
        existing_properties = existing["properties_json"]
        candidate_properties = candidate["properties_json"]
        if existing_properties["length_m"] != candidate_properties["length_m"]:
            raise BasinsGeometryError(
                "BASINS_REGISTRY_CROSSWALK_DUPLICATE_CONFLICT",
                "Basins seg.shp repeats an (iRiv, iEle) identity with conflicting attributes.",
                details={
                    "model_id": model_id,
                    "external_id": external_id,
                    "existing_length_m": existing_properties["length_m"],
                    "duplicate_length_m": candidate_properties["length_m"],
                },
            )
        # The database key models one relation per (iRiv, iEle).  Source
        # shapefiles may repeat that same relation on multiple geometry rows;
        # preserve the first (lowest source segment_order) deterministically so
        # one execute_values page never targets the same ON CONFLICT key twice.
    return list(rows_by_external_id.values())


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
