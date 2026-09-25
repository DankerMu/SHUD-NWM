"""Test-only oracle for #2424 D1: the pre-rewrite ``_per_source_latest_cycles``.

``PRE_2424_PER_SOURCE_LATEST_CYCLES_SQL`` is the statement master ``64f47adee``
executed, verbatim, with ``_segment_rows_source_sql()`` expanded inline and the
two filter slots left as ``{scenario_filter}`` / ``{identity_filter}``. It is
frozen here as a literal on purpose: the equality oracles (the seeded real-DB
test and the node-27 regression probe) must compare the new statement with what
production DID run, not with whatever the shared source template says today.

It scans the segment's fact rows and joins ``hydro.hydro_run`` to take
``MAX(cycle_time)`` per scenario. It is never executed by production code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

PRE_2424_PER_SOURCE_LATEST_CYCLES_SQL = """
            SELECT
                h.scenario_id,
                MAX(h.cycle_time) AS cycle_time
            FROM (
SELECT rt.run_key, rt.river_network_version_key, rt.valid_time, rt.value, rt.unit_e
FROM hydro.river_timeseries rt
JOIN hydro.hydro_run h ON h.run_key = rt.run_key
WHERE rt.basin_version_key IS NOT NULL
  AND rt.basin_version_key IS NOT DISTINCT FROM (
      SELECT basin_version_key FROM core.basin_version
      WHERE basin_version_id = %(basin_version_id)s
  )
  AND rt.river_segment_key = (
      SELECT river_segment_key FROM core.river_segment
      WHERE river_segment_id = %(river_segment_id)s
        AND river_network_version_id = %(river_network_version_id)s
  )
  AND rt.river_network_version_key IS NOT NULL
  AND rt.river_network_version_key IS NOT DISTINCT FROM (
      SELECT river_network_version_key FROM core.river_network_version
      WHERE river_network_version_id = %(river_network_version_id)s
  )
  AND rt.variable_e = 'q_down'::hydro.river_variable
) rt
            JOIN hydro.hydro_run h ON h.run_key = rt.run_key
            WHERE h.run_type = 'forecast'
              AND h.cycle_time IS NOT NULL
              {scenario_filter}
              {identity_filter}
            GROUP BY h.scenario_id
            ORDER BY h.scenario_id
            """


def pre_2424_statement(*, scenario_filter: Any, identity_filter: Any) -> str:
    """The old statement text for one pair of ``_ScenarioFilter`` fragments."""
    return PRE_2424_PER_SOURCE_LATEST_CYCLES_SQL.format(
        scenario_filter=scenario_filter.sql,
        identity_filter=identity_filter.sql,
    )


def pre_2424_parameters(
    *,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str,
    scenario_filter: Any,
    identity_filter: Any,
) -> dict[str, Any]:
    return {
        "basin_version_id": basin_version_id,
        "river_segment_id": segment_id,
        "river_network_version_id": river_network_version_id,
        **scenario_filter.params,
        **identity_filter.params,
    }


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def pre_2424_latest_cycles(
    cursor: Any,
    *,
    basin_version_id: str,
    segment_id: str,
    river_network_version_id: str,
    scenario_filter: Any,
    identity_filter: Any,
) -> dict[str, datetime]:
    """Execute the old statement and shape its rows exactly as the old method did.

    ``cursor`` must be a ``RealDictCursor``; ``segment_id`` is the TIMESERIES
    segment id (``_timeseries_segment_id`` already applied), as the store passes it.
    """
    cursor.execute(
        pre_2424_statement(scenario_filter=scenario_filter, identity_filter=identity_filter),
        pre_2424_parameters(
            basin_version_id=basin_version_id,
            segment_id=segment_id,
            river_network_version_id=river_network_version_id,
            scenario_filter=scenario_filter,
            identity_filter=identity_filter,
        ),
    )
    return {
        str(row["scenario_id"]): _utc(row["cycle_time"])
        for row in cursor.fetchall()
        if row.get("scenario_id") and row.get("cycle_time") is not None
    }
