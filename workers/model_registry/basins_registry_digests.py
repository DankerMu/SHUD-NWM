"""Basins registry idempotency digests: river-segment and output-river-segment
digest rows, property normalization and canonical single-part line WKT (#2490
split of ``basins_registry_import``).

``workers.model_registry.basins_registry_import`` stays the stable import path
and re-exports every name defined here.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .basins_registry_support import ImportSources, _json_dict, _sha256_json

_OUTPUT_BACKFILL_INJECTED_KEYS = frozenset(
    {"geometry_source", "geometry_source_segment_count", "geometry_source_length_m", "Type"}
)


def _output_river_segment_digest(rows: list[dict[str, Any]]) -> str:
    """Hash output-river rows for idempotency checking.

    Filters out fields injected by ``_backfill_output_segment_geometry`` after
    the initial write (``geometry_source``, ``geometry_source_segment_count``,
    ``geometry_source_length_m``). Incoming rows (from
    ``_output_river_segment_rows``) lack these provenance keys; existing rows
    (re-read from DB after a prior bootstrap + backfill) carry them. Without
    this filter, every re-ingest after a successful bootstrap would trigger
    ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` on ``output_river_segment``.
    """
    payload = [
        {
            "river_segment_id": str(row["river_segment_id"]),
            "segment_order": None if row["segment_order"] is None else int(row["segment_order"]),
            "properties": _normalize_properties_for_digest(
                {k: v for k, v in (row["properties"] or {}).items() if k not in _OUTPUT_BACKFILL_INJECTED_KEYS}
            ),
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
    # PR 2 (feat-reach-geom-from-river-shp) writes each reach as a
    # single-part LineString WKT, but
    # ``INSERT ... ST_Multi(ST_GeomFromText(...))`` (see ``_ensure_river_segments``)
    # wraps it into a single-part MultiLineString to satisfy the
    # ``geometry(MultiLineString, 4490)`` column type. The DB round-trip therefore
    # returns ``MULTILINESTRING((...))`` while the incoming side still carries
    # ``LINESTRING(...)``. Hashing the raw WKT verbatim would make the two sides
    # diverge and break the ``BASINS_REGISTRY_CHECKSUM_CONFLICT`` idempotency
    # guard on every re-ingest. Canonicalising to the bare coordinate sequence
    # (numerics normalised via ``format(x, ".12g")``, ``-0`` collapsed to ``0``)
    # makes both shapes hash identically while still flagging genuine geometry
    # drift.
    return {
        "river_segment_id": str(river_segment_id),
        "segment_order": None if segment_order is None else int(segment_order),
        "downstream_segment_id": None if downstream_segment_id is None else str(downstream_segment_id),
        "length_m": None if length_m is None else float(length_m),
        "geometry": _canonical_singlepart_line_coordinates(str(geom_wkt or "")),
        "properties": _normalize_properties_for_digest(properties),
    }


def _normalize_properties_for_digest(properties: dict[str, Any]) -> dict[str, Any]:
    """Stabilise properties for digest hashing across PG JSONB round-trips.

    PR 2 carries SHUD reach physical parameters from ``gis/river.shp`` (Slope,
    Length, BankSlope, Width, ...) into ``properties_json``. PostgreSQL JSONB
    stores numbers as ``numeric`` and re-emits them in canonical form, which
    psycopg2 then decodes with ``json.loads``. The result is that a Python
    ``float(5550.0)`` written into JSONB may come back as ``int(5550)``
    (PG strips trailing zeros from the JSON text representation). Without
    normalisation the SHA-256 of the incoming dict vs the re-read dict
    diverges, breaking idempotent re-import (``BASINS_REGISTRY_CHECKSUM_CONFLICT``
    on ``river_segment``). We coerce every numeric leaf to ``float`` so both
    sides hash identically; booleans are preserved (``bool`` is a subclass of
    ``int`` in Python so we filter them first).
    """

    def _coerce(value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, dict):
            return {str(k): _coerce(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_coerce(item) for item in value]
        return value

    return {str(key): _coerce(value) for key, value in properties.items()}


def _normalize_wkt(value: str) -> str:
    compact_commas = re.sub(r"\s*,\s*", ",", value.strip())
    return re.sub(r"\s+", " ", compact_commas)


def _canonical_singlepart_line_coordinates(value: str) -> list[list[str]]:
    """Normalize a single-part linestring WKT into a canonical coordinate list.

    Both ``LINESTRING(...)`` and single-part ``MULTILINESTRING((...))`` collapse
    to the same coordinate sequence so the incoming parser form (LineString)
    and the PostgreSQL/PostGIS round-tripped storage form (MultiLineString,
    written via ``ST_Multi(ST_GeomFromText(...))``) hash to the same digest.

    Numeric tokens are normalised via ``format(x, ".12g")`` so equivalent text
    representations (``100`` / ``100.0`` / ``1e2``) produce identical output.
    Negative zero is collapsed to positive zero.

    Raises ``ValueError`` on a multi-part ``MULTILINESTRING``: the PR 2
    ``gis/river.shp`` parser contract guarantees a single-part reach geometry,
    so multi-part inputs are an invariant violation and must NOT be silently
    folded into one canonical key.
    """

    text = _normalize_wkt(value)
    line_match = re.fullmatch(r"LINESTRING\s*\((.*)\)", text, flags=re.IGNORECASE)
    multi_match = re.fullmatch(r"MULTILINESTRING\s*\(\s*\((.*)\)\s*\)", text, flags=re.IGNORECASE)
    if line_match is not None:
        body = line_match.group(1)
    elif multi_match is not None:
        body = multi_match.group(1)
        if re.search(r"\)\s*,\s*\(", body):
            raise ValueError("expected a single-part MultiLineString")
    else:
        raise ValueError(f"unsupported river geometry WKT: {text[:64]}")
    coordinates: list[list[str]] = []
    for raw_point in body.split(","):
        ordinates = raw_point.split()
        if len(ordinates) != 2:
            raise ValueError("expected a two-dimensional coordinate")
        point: list[str] = []
        for token in ordinates:
            number = float(token)
            if not math.isfinite(number):
                raise ValueError("non-finite coordinate")
            if number == 0:
                number = 0.0  # collapse -0 to +0
            point.append(format(number, ".12g"))
        coordinates.append(point)
    if len(coordinates) < 2:
        raise ValueError("river geometry must contain at least two points")
    return coordinates
