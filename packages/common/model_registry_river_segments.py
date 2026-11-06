"""River-segment read method of ``PsycopgModelRegistryStore``: the single-segment
detail under its serialized payload budget (#2617 split of
``packages.common.model_registry``).

#2866 retired the HTTP list route (``GET /api/v1/basin-versions/{bv}/river-segments``)
and #2907 deleted the paged-collection store read behind it, with its legacy and
segment-slice helpers, its collection budgets and the derived segment-slice identifier
it produced (``docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md``). The detail read
below is the only river-segment read the store serves.

A plain mixin: no fields and no dunders. ``PsycopgModelRegistryStore`` in
``packages.common.model_registry`` is the only class that composes it.
"""

from __future__ import annotations

from typing import Any

from packages.common.model_registry_contracts import (
    RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
    SELECTED_SEGMENT_GEOMETRY_MAX_COORDINATES,
    SELECTED_SEGMENT_GEOMETRY_MAX_DIMENSIONS,
    MissingResourceError,
)
from packages.common.model_registry_public import _enforce_river_segment_serialized_budget, _river_segment_detail


class _RiverSegmentReadMixin:
    """River-segment detail read under the payload budget."""

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
