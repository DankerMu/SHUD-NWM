"""River-segment read methods of ``PsycopgModelRegistryStore``: the paged
GeoJSON collection (legacy and segment-slice paths) and the single-segment
detail, under the serialized payload budgets (#2617 split of
``packages.common.model_registry``).

A plain mixin: no fields and no dunders. ``PsycopgModelRegistryStore`` in
``packages.common.model_registry`` is the only class that composes it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_COORDINATES,
    RIVER_SEGMENT_COLLECTION_GEOMETRY_MAX_DIMENSIONS,
    RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
    RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
    SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES,
    SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
    MissingResourceError,
    _escape_like,
)
from packages.common.model_registry_public import _enforce_river_segment_serialized_budget, _river_segment_detail


class _RiverSegmentReadMixin:
    """River-segment GeoJSON reads under the payload budgets."""

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

        # search: parameterised LIKE/ILIKE over the segment identifier and the
        # human readable name stored in properties_json. Escapes %/_ so caller
        # input is treated literally and never widens the LIKE pattern (no
        # injection face).
        #
        # The id arm spells `lower(rs.river_segment_id)` and lowercases the
        # pattern instead of using ILIKE: migration 000052 rebuilt
        # `river_segment_id_trgm_idx` on that expression so equality lookups can
        # no longer select it (issue #1468 / ADR 0004), and only a query using
        # the same expression still gets the index. Semantically identical to
        # ILIKE on these ASCII slug ids, and `_escape_like` runs first so the
        # `\`/`%`/`_` escapes are untouched by `lower()`. The two name arms are
        # served by bare-column trigram indexes and stay on ILIKE.
        normalized_search = search.strip() if search is not None else ""
        if normalized_search:
            like_pattern = f"%{_escape_like(normalized_search)}%"
            filters.append(
                "(lower(rs.river_segment_id) LIKE %s ESCAPE '\\' "
                "OR COALESCE(rs.properties_json->>'name', '') ILIKE %s ESCAPE '\\' "
                "OR COALESCE(rs.properties_json->>'segment_name', '') ILIKE %s ESCAPE '\\')"
            )
            params.extend([like_pattern.lower(), like_pattern, like_pattern])

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
