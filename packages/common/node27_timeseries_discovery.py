"""The D3 lifecycle set, pinned exclusively by hypertable catalog membership."""

from collections.abc import Iterable

CANONICAL_HYPERTABLES = (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries"))
LEGACY_HYPERTABLES = tuple((schema, f"{name}_legacy") for schema, name in CANONICAL_HYPERTABLES)
CATALOG_QUERY = "SELECT hypertable_schema, hypertable_name FROM timescaledb_information.hypertables"


def discover_hypertables(rows: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Canonical names always exist in the contract; only catalog siblings extend it."""
    present = set(rows)
    return CANONICAL_HYPERTABLES + tuple(pair for pair in LEGACY_HYPERTABLES if pair in present)


def render_hypertable_pairs(pairs: Iterable[tuple[str, str]]) -> str:
    """Render only the closed lifecycle allowlist, never arbitrary SQL identifiers."""
    pairs = tuple(pairs)
    if any(pair not in CANONICAL_HYPERTABLES + LEGACY_HYPERTABLES for pair in pairs):
        raise ValueError("not a lifecycle hypertable")
    return ",".join(f"('{schema}','{name}')" for schema, name in pairs)


# Canonical names always appear; sibling names appear only when that exact
# hypertable exists. Ordinary leftover *_legacy tables never enter the set.
RUNTIME_HYPERTABLES_SQL = (
    "SELECT schema,name FROM (VALUES "
    + render_hypertable_pairs(CANONICAL_HYPERTABLES)
    + ") AS canonical(schema,name) UNION ALL "
    "SELECT hypertable_schema,hypertable_name FROM timescaledb_information.hypertables "
    "WHERE (hypertable_schema,hypertable_name) IN ("
    + render_hypertable_pairs(LEGACY_HYPERTABLES)
    + ")"
)
