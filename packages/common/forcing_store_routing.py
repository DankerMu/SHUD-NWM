"""What both forcing fact-table writers share (#1991, task 7.3).

``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`` created the
narrow key/enum ``met.forcing_station_timeseries``, and both writers
(``workers/forcing_producer/store.py`` on node-22's own database,
``packages/common/forcing_domain_handoff_apply.py`` across the node-22 ->
node-27 handoff) write only that table. This module owns what they must not
spell twice:

* the lookup that resolves a forcing version's surrogate key — one row,
  primary-key lookup on ``met.forcing_version``;
* the narrow INSERT's column list and per-row template;
* a cursor-row accessor that works for both cursor factories.

It holds no SQL naming the fact table, so it stays outside the forcing template
census's discovery set (``tests/forcing_ts_template_registry`` counts
schema-qualified mentions of ``met.forcing_station_timeseries``).

The module name is historical: until #1993's first cut it also routed between
the narrow table and the wide table 000061 renamed aside, off a per-version
store column that no code reads any more.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: One row, primary-key lookup: the surrogate key the narrow statements
#: predicate on.
FORCING_VERSION_ROUTING_SQL = """
SELECT forcing_version_key
FROM met.forcing_version
WHERE forcing_version_id = %s
"""

#: The narrow fact table's column list, in the order both writers bind it.
#: Spelled once because the two writers must not drift: they write the same rows
#: from the same producer, one on node-22's own database and one across the
#: node-22 -> node-27 handoff.
FORCING_NARROW_INSERT_COLUMNS: tuple[str, ...] = (
    "forcing_version_key",
    "station_key",
    "valid_time",
    "variable_e",
    "value",
    "unit_e",
    "quality_flag_e",
    "native_resolution",
)

#: ``execute_values`` per-row template for that INSERT. Spelled out rather than
#: left to psycopg2's default ``(%s, %s, …)`` because the three enum columns need
#: an explicit cast: psycopg2 sends a Python ``str`` as ``text`` and Postgres has
#: no implicit ``text -> enum`` coercion in an INSERT source list. The casts
#: double as the vocabulary gate — a variable/unit/quality_flag literal outside
#: ``000061_forcing_station_timeseries_narrow_expand.sql``'s enums fails the whole
#: batch here rather than landing as free text nobody ever notices.
FORCING_NARROW_INSERT_TEMPLATE = (
    "(%s, %s, %s, %s::met.forcing_variable, %s, %s::met.forcing_unit, %s::met.forcing_quality_flag, %s)"
)


def row_value(row: Any, key: str, index: int) -> Any:
    """One field out of a cursor row, whatever cursor factory produced it.

    The two writers do not control their cursor factory the same way --
    ``workers/forcing_producer/store.py`` opens a plain tuple cursor, while
    ``packages/common/forcing_domain_handoff_apply.py`` runs on whatever cursor
    its caller's connection hands out, which may be a ``RealDictCursor``.
    Reading ``row[0]`` off a dict row raises ``KeyError: 0``; reading
    ``row["x"]`` off a tuple raises ``TypeError``. Neither diagnostic names the
    cursor.
    """
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]
