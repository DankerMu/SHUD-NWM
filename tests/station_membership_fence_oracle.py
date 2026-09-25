"""Test-only oracle for #2516 D3: the pre-change station legs, frozen from master.

``PRE_2516_LEGACY_STATION_LEG`` and ``PRE_2516_NARROW_STATION_LEG`` are the two
variants master ``64f47adee`` rendered for BOTH station legs --
``forecast_store._LATEST_PRODUCT_STATION_SOURCE_TEMPLATES`` and
``display_coverage._STATION_SAMPLE_ROWS_TEMPLATES``. The two templates were
byte-identical apart from indentation, so one dedented copy of each variant
stands for both.

design.md D3 (revised) changes exactly one thing: the narrow variant's
``interp_weight`` membership ``EXISTS`` gains ``OFFSET 0`` as its last clause, so
the planner keeps the probe a correlated SubPlan instead of pulling it up into a
semi-join, and ``(fst.variable_e)::text`` becomes an index-scan parameter of
``interp_weight_qhh_latest_membership_idx``. The legacy variant is unchanged.
Frozen as literals so the shape and equality tests compare the new legs with
what production DID run. Never executed by production code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from packages.common.forcing_ts_render import FORCING_TABLE_TOKEN, ForcingTemplatePair, render_forcing_ts_sql

#: The one line D3 adds, as the last clause inside the narrow membership EXISTS.
MEMBERSHIP_FENCE = "OFFSET 0"
#: The flattened opening of the membership EXISTS (both variants, both legs).
MEMBERSHIP_EXISTS_OPENING = "AND EXISTS ( SELECT 1 FROM met.interp_weight iw"

PRE_2516_LEGACY_STATION_LEG = f"""SELECT
    cr.run_id,
    cr.model_id,
    cr.display_start_time,
    cr.display_end_time,
    fst.forcing_version_id,
    fst.basin_version_id,
    LOWER(fst.source_id) AS station_source_id,
    fst.station_id,
    fst.variable,
    cr.expected_station_count,
    fst.valid_time,
    fst.unit,
    fst.quality_flag
FROM {FORCING_TABLE_TOKEN} fst
JOIN candidate_runs cr
  ON cr.forcing_version_id = fst.forcing_version_id
 AND fst.basin_version_id = cr.basin_version_id
 AND LOWER(fst.source_id) = LOWER(cr.source_id)
WHERE fst.variable = ANY(%(variables)s)
  AND fst.valid_time >= cr.display_start_time
  AND fst.valid_time <= cr.display_end_time
  AND (%(scan_forcing_version_id)s IS NULL
       OR fst.forcing_version_id = %(scan_forcing_version_id)s)
  AND (%(scan_basin_version_id)s IS NULL
       OR fst.basin_version_id = %(scan_basin_version_id)s)
  AND (%(scan_source_id_lower)s IS NULL
       OR LOWER(fst.source_id) = %(scan_source_id_lower)s)
  AND (%(scan_display_start)s IS NULL
       OR fst.valid_time >= %(scan_display_start)s)
  AND (%(scan_display_end)s IS NULL
       OR fst.valid_time <= %(scan_display_end)s)
  AND EXISTS (
      SELECT 1
      FROM met.interp_weight iw
      WHERE iw.model_id = cr.model_id
        AND iw.station_id = fst.station_id
        AND iw.variable = fst.variable
        AND LOWER(iw.source_id) = LOWER(cr.source_id)
  )
"""

PRE_2516_NARROW_STATION_LEG = f"""SELECT
    cr.run_id,
    cr.model_id,
    cr.display_start_time,
    cr.display_end_time,
    fv.forcing_version_id,
    ms.basin_version_id,
    LOWER(fv.source_id) AS station_source_id,
    ms.station_id,
    fst.variable_e::text AS variable,
    cr.expected_station_count,
    fst.valid_time,
    fst.unit_e::text AS unit,
    fst.quality_flag_e::text AS quality_flag
FROM {FORCING_TABLE_TOKEN} fst
JOIN met.forcing_version fv
  ON fv.forcing_version_key = fst.forcing_version_key
JOIN met.met_station ms
  ON ms.station_key = fst.station_key
JOIN candidate_runs cr
  ON cr.forcing_version_id = fv.forcing_version_id
 AND ms.basin_version_id = cr.basin_version_id
 AND LOWER(fv.source_id) = LOWER(cr.source_id)
WHERE fst.variable_e = ANY(%(variables)s::met.forcing_variable[])
  AND fst.valid_time >= cr.display_start_time
  AND fst.valid_time <= cr.display_end_time
  AND (%(scan_forcing_version_id)s IS NULL
       OR fv.forcing_version_id = %(scan_forcing_version_id)s)
  AND (%(scan_basin_version_id)s IS NULL
       OR ms.basin_version_id = %(scan_basin_version_id)s)
  AND (%(scan_source_id_lower)s IS NULL
       OR LOWER(fv.source_id) = %(scan_source_id_lower)s)
  AND (%(scan_display_start)s IS NULL
       OR fst.valid_time >= %(scan_display_start)s)
  AND (%(scan_display_end)s IS NULL
       OR fst.valid_time <= %(scan_display_end)s)
  AND EXISTS (
      SELECT 1
      FROM met.interp_weight iw
      WHERE iw.model_id = cr.model_id
        AND iw.station_id = ms.station_id
        AND iw.variable = fst.variable_e::text
        AND LOWER(iw.source_id) = LOWER(cr.source_id)
  )
"""


def flat(sql: str) -> str:
    return " ".join(sql.split())


def membership_exists_body(sql: str) -> str:
    """The flattened body of the ``interp_weight`` membership ``EXISTS``, parentheses balanced."""
    text = flat(sql)
    assert text.count(MEMBERSHIP_EXISTS_OPENING) == 1, "expected exactly one membership EXISTS"
    start = text.index(MEMBERSHIP_EXISTS_OPENING) + len("AND EXISTS ")
    depth = 0
    for index in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return text[start + 1 : index].strip()
    raise AssertionError("unbalanced membership EXISTS")


def rendered_leg(template_pair: ForcingTemplatePair, store: str) -> str:
    return render_forcing_ts_sql(template_pair, store, entry="station_membership_fence_oracle").sql


def rendered_narrow_leg(template_pair: ForcingTemplatePair) -> str:
    return rendered_leg(template_pair, "narrow")


#: Master's pair, as both station templates were before D3.
PRE_2516_STATION_LEGS = ForcingTemplatePair(legacy=PRE_2516_LEGACY_STATION_LEG, narrow=PRE_2516_NARROW_STATION_LEG)


def pre_2516_leg(store: str = "narrow") -> str:
    return rendered_leg(PRE_2516_STATION_LEGS, store)


#: A one-row ``candidate_runs`` with the eight columns both legs read off ``cr``.
_CANDIDATE_RUNS = """
WITH candidate_runs AS (
    SELECT
        %(cr_run_id)s::text AS run_id,
        %(cr_model_id)s::text AS model_id,
        %(cr_display_start_time)s::timestamptz AS display_start_time,
        %(cr_display_end_time)s::timestamptz AS display_end_time,
        %(cr_forcing_version_id)s::text AS forcing_version_id,
        %(cr_basin_version_id)s::text AS basin_version_id,
        %(cr_source_id)s::text AS source_id,
        %(cr_expected_station_count)s::bigint AS expected_station_count
)
SELECT * FROM (
"""


def standalone_leg_statement(leg_sql: str) -> str:
    """One narrow leg under a literal candidate, in a total row order."""
    return _CANDIDATE_RUNS + leg_sql + ") AS leg\nORDER BY station_id, variable, valid_time, quality_flag, unit\n"


def standalone_leg_parameters(
    *,
    variables: list[str],
    run_id: str,
    model_id: str,
    forcing_version_id: str,
    basin_version_id: str,
    source_id: str,
    display_start_time: datetime,
    display_end_time: datetime,
    scan_pinned: bool,
) -> dict[str, Any]:
    return {
        "cr_run_id": run_id,
        "cr_model_id": model_id,
        "cr_display_start_time": display_start_time,
        "cr_display_end_time": display_end_time,
        "cr_forcing_version_id": forcing_version_id,
        "cr_basin_version_id": basin_version_id,
        "cr_source_id": source_id,
        "cr_expected_station_count": 2,
        "variables": list(variables),
        "scan_forcing_version_id": forcing_version_id if scan_pinned else None,
        "scan_basin_version_id": basin_version_id if scan_pinned else None,
        "scan_source_id_lower": source_id.lower() if scan_pinned else None,
        "scan_display_start": display_start_time if scan_pinned else None,
        "scan_display_end": display_end_time if scan_pinned else None,
    }
