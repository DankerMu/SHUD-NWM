"""Single owner of the node-27 lifecycle hypertable set (issue #1985, D7).

During the ``timeseries-narrow-store-expand-contract`` transition each detail
hypertable exists twice for a while: the canonical name carries the narrow
key/enum table and ``<name>_legacy`` carries the renamed text table until
retention has drained it and the contract migration drops it.  Every lifecycle
tool must govern BOTH, under the same lag and the same retention window, and
must converge back to the canonical table alone when the sibling disappears —
without a second code change.

The rule is therefore a discovery, not a list::

    governed = {canonical} ∪ {canonical_legacy | it exists in
                              timescaledb_information.hypertables}

re-evaluated on every invocation (no cached table list).  Eight call sites
consume this module:

* ``scripts/node27_timeseries_compression.py`` — chunk selection, per-table totals
* ``scripts/node27_timeseries_retention.py`` — chunk selection, ``legacy_chunks``
* ``scripts/node27_timeseries_compression_supervisor.py`` — ``validate_current_d3``
* ``scripts/node27_timeseries_compression_capture.py`` — catalog/selection/size probes
* ``scripts/node27_timeseries_compression_live_evidence.py`` — replay validation
* ``scripts/node27_autopipeline.py`` — the statistics guard's candidate query
* ``packages/common/node27_cold_governance_collection.py`` — working-set collection
* ``scripts/node27_resource_governance.py`` — the audit's policy-missing checks

Four sites deliberately do NOT consume the discovery set; each is documented
where it stands.  Three of them read :data:`CANONICAL_HYPERTABLES` instead:

- ``scripts/node27_timeseries_compression.py`` — its ``HYPERTABLES`` constant,
  the fallback for receipt paths that never reached the catalog
- ``scripts/node27_timeseries_retention.py`` — its ``TARGET_HYPERTABLES``
  constant, the documentary canonical-pair allowlist
- ``scripts/node27_timeseries_compression_capture.py`` — the write-guard
  preflight's ``guards_both_targets`` check, over a tuple set frozen for I7

The fourth site does not even use the constant:

- ``scripts/node27_timeseries_compression_capture.py`` — the ownership probe
  labelled ``/* capture:role */`` and marked "Canonical-only on purpose", which
  casts bare ``schema.table`` literals to ``regclass``.  That ERRORS on a
  relation that does not exist, and ownership of a renamed sibling is the same
  role fact as ownership of the table it was renamed from.

Pins here are by NAME, not by line: the line pin this roster used to carry went
stale inside a single PR.

The candidate list is a LITERAL, never an identifier discovered at runtime and
formatted back into SQL: chunk/settings queries simply carry all four
``(schema, table)`` tuples and the catalog returns rows only for the ones that
exist.  That keeps discovery free of string interpolation and makes "legacy
gone" a no-op rather than a branch.

Per-table expectations follow catalog state, not table names (task 3.1's
three-state rule):

* canonical table WITH a sibling → key-shaped settings, sibling text-shaped
* canonical table WITHOUT a sibling → that table's entry in
  :data:`NO_SIBLING_SHAPE`, a per-canonical constant (text-shaped for both
  tables today, so today's live catalog is asserted exactly as before)

The no-sibling case is deliberately a CONSTANT and not "always text-shaped".
Sibling presence alone cannot answer the question, because the catalog looks
the same before the expand migration creates the sibling and after the
contract migration drops it — but the physical table is text-shaped in the
first case and key-shaped in the second.  So each contract PR (tasks 6.2 for
river, 8.2 for forcing) flips ITS OWN entry in :data:`NO_SIBLING_SHAPE` to the
key shape and deploys that flip BEFORE the ``DROP``; skip the flip and
``validate_current_d3`` goes red the instant the sibling disappears (round-1
review reproduced exactly that).  One constant per contract, no change to the
discovery logic.  Both key shapes are encoded here now (river per design D4,
forcing per design D9), so I7 and I12 flip by migration alone.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "CANDIDATE_HYPERTABLES",
    "CANONICAL_HYPERTABLES",
    "CANONICAL_KEYS",
    "DISCOVERY_SQL",
    "LEGACY_SUFFIX",
    "NO_SIBLING_SHAPE",
    "candidate_in_list_sql",
    "candidate_tuple_list_sql",
    "compression_settings_expectation",
    "discovery_set",
    "expected_hypertable_flags",
    "is_legacy",
    "legacy_chunk_counts",
    "legacy_sibling",
    "present_from_rows",
    "qualified",
]

#: The two D3 detail hypertables, in catalog order (``hypertable_schema``,
#: ``hypertable_name``).  Ordering is the tie-break every consumer's
#: ``ORDER BY`` reproduces and the order of the compression receipt's
#: ``per_table_totals``; do not reorder.
CANONICAL_HYPERTABLES: tuple[tuple[str, str], ...] = (
    ("hydro", "river_timeseries"),
    ("met", "forcing_station_timeseries"),
)

LEGACY_SUFFIX = "_legacy"


def qualified(schema: str, name: str) -> str:
    """``schema.table`` — the key shape used by every receipt and catalog probe."""

    return f"{schema}.{name}"


def legacy_sibling(schema: str, name: str) -> tuple[str, str]:
    """The transitional sibling name for a canonical hypertable (D2)."""

    return (schema, f"{name}{LEGACY_SUFFIX}")


def is_legacy(name: str) -> bool:
    return name.endswith(LEGACY_SUFFIX)


CANONICAL_KEYS: tuple[str, ...] = tuple(qualified(*item) for item in CANONICAL_HYPERTABLES)

#: Canonical tables plus their possible siblings, in catalog order.  This is
#: what goes into SQL ``IN`` lists: names that do not exist match nothing.
CANDIDATE_HYPERTABLES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [*CANONICAL_HYPERTABLES, *(legacy_sibling(*item) for item in CANONICAL_HYPERTABLES)]
    )
)

CANDIDATE_KEYS: tuple[str, ...] = tuple(qualified(*item) for item in CANDIDATE_HYPERTABLES)


def candidate_in_list_sql(indent: str = "    ") -> str:
    """Row-per-line ``(schema, table)`` tuples for a block-formatted ``IN`` list."""

    return ",\n".join(f"{indent}('{schema}', '{name}')" for schema, name in CANDIDATE_HYPERTABLES)


def candidate_tuple_list_sql() -> str:
    """Compact parenthesised tuple list for single-line ``--command`` SQL."""

    body = ",".join(f"('{schema}','{name}')" for schema, name in CANDIDATE_HYPERTABLES)
    return f"({body})"


#: Existence + chunk-count probe.  ``num_chunks`` is what the retention receipt's
#: ``legacy_chunks`` reports and what the I9 / I14 contract gate reads as zero,
#: so it must be the table's TOTAL remaining chunks, not a filtered subset.
DISCOVERY_SQL = f"""
SELECT hypertable_schema, hypertable_name, num_chunks, compression_enabled
FROM timescaledb_information.hypertables
WHERE (hypertable_schema, hypertable_name) IN (
{candidate_in_list_sql()}
)
ORDER BY hypertable_schema, hypertable_name
"""


def _row_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    return (str(row["hypertable_schema"]), str(row["hypertable_name"]))


def present_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Candidate hypertables the catalog actually reports, in catalog order."""

    observed = {_row_identity(row) for row in rows}
    return tuple(item for item in CANDIDATE_HYPERTABLES if item in observed)


def discovery_set(rows: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    """The governed set: canonical tables plus every sibling that exists.

    A canonical table missing from the catalog stays in the set on purpose:
    silently dropping it would turn "the expand migration renamed the table and
    never created the new one" into a quiet no-op tick.

    Keeping it is NOT by itself a refusal, and callers must not assume one.
    :func:`compression_settings_expectation` and :func:`expected_hypertable_flags`
    raise on a missing canonical (so the supervisor and the live-evidence
    validator fail closed), but the compression runner and the governance
    collection never call those, so each performs its own
    :func:`present_from_rows` check: the runner refuses the tick
    (``CompressionConfigError``) and governance reports
    ``projection_status = "working_set_unavailable"``. The retention runner
    consumes discovery only to count ``_legacy`` chunks, where a missing
    canonical changes nothing.
    """

    present = set(present_from_rows(rows))
    return tuple(
        item
        for item in CANDIDATE_HYPERTABLES
        if item in CANONICAL_HYPERTABLES or item in present
    )


def legacy_chunk_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int] | None:
    """``{"<schema>.<table>_legacy": <total chunks>}`` or ``None``.

    ``None`` — not an empty mapping — when no sibling exists, so a receipt
    written on today's catalog carries no ``legacy_chunks`` key at all and stays
    byte-comparable with the archived receipts.
    """

    counts = {
        qualified(*_row_identity(row)): int(row.get("num_chunks") or 0)
        for row in rows
        if _row_identity(row) in CANDIDATE_HYPERTABLES and is_legacy(str(row["hypertable_name"]))
    }
    return counts or None


# ---------------------------------------------------------------------------
# Per-table expectations (the three-state rule)
# ---------------------------------------------------------------------------

# One tuple per ``timescaledb_information.compression_settings`` row:
# (attname, segmentby_column_index, orderby_column_index, orderby_asc,
#  orderby_nullsfirst).  Segmentby rows carry NULL orderby fields and vice
# versa, exactly as the catalog reports them.
_TEXT_SHAPE: dict[tuple[str, str], tuple[tuple[str, int | None, int | None, bool | None, bool | None], ...]] = {
    ("hydro", "river_timeseries"): (
        ("run_id", 1, None, None, None),
        ("river_network_version_id", 2, None, None, None),
        ("river_segment_id", 3, None, None, None),
        ("variable", None, 1, True, False),
        ("valid_time", None, 2, True, False),
    ),
    ("met", "forcing_station_timeseries"): (
        ("forcing_version_id", 1, None, None, None),
        ("station_id", 2, None, None, None),
        ("variable", None, 1, True, False),
        ("valid_time", None, 2, True, False),
    ),
}

# Narrow (key/enum) shapes, encoded now so I7 and I12 flip by migration alone:
# river per design D4, forcing per design D9.
_KEY_SHAPE: dict[tuple[str, str], tuple[tuple[str, int | None, int | None, bool | None, bool | None], ...]] = {
    ("hydro", "river_timeseries"): (
        ("run_key", 1, None, None, None),
        ("river_segment_key", 2, None, None, None),
        ("variable_e", None, 1, True, False),
        ("valid_time", None, 2, True, False),
    ),
    ("met", "forcing_station_timeseries"): (
        ("forcing_version_key", 1, None, None, None),
        ("station_key", 2, None, None, None),
        ("variable_e", None, 1, True, False),
        ("valid_time", None, 2, True, False),
    ),
}

# The shape a canonical table is asserted to have when NO ``_legacy`` sibling is
# in the catalog.  Both entries are the text shape TODAY, which is why the live
# node-27 catalog is asserted byte-identically to before #1985; each contract PR
# flips its own entry to ``_KEY_SHAPE`` and deploys that flip before its DROP
# (see the module docstring).  Never collapse this back to "text shape when no
# sibling": that reverts the canonical table's expectation after the contract.
NO_SIBLING_SHAPE: dict[
    tuple[str, str], tuple[tuple[str, int | None, int | None, bool | None, bool | None], ...]
] = {
    ("hydro", "river_timeseries"): _TEXT_SHAPE[("hydro", "river_timeseries")],
    ("met", "forcing_station_timeseries"): _TEXT_SHAPE[("met", "forcing_station_timeseries")],
}


ExpectationRow = tuple[str, str, str, int | None, int | None, bool | None, bool | None]


def _normalise_present(present: Iterable[str] | Iterable[Sequence[str]]) -> tuple[str, ...]:
    keys: list[str] = []
    for item in present:
        if isinstance(item, str):
            keys.append(item)
        else:
            schema, name = item
            keys.append(qualified(schema, name))
    unknown = [key for key in keys if key not in CANDIDATE_KEYS]
    if unknown:
        raise ValueError(f"unknown hypertable in catalog: {sorted(unknown)}")
    missing = [key for key in CANONICAL_KEYS if key not in keys]
    if missing:
        raise ValueError(f"catalog is missing a canonical hypertable: {missing}")
    return tuple(key for key in CANDIDATE_KEYS if key in set(keys))


def expected_hypertable_flags(
    present: Iterable[str] | Iterable[Sequence[str]],
) -> dict[str, bool]:
    """``{qualified name: True}`` — every governed table must be compression-enabled.

    Both tables are compressed under the same lag, so a sibling with compression
    off is drift, not an accepted transitional state.
    """

    return {key: True for key in _normalise_present(present)}


def compression_settings_expectation(
    present: Iterable[str] | Iterable[Sequence[str]],
) -> list[ExpectationRow]:
    """Expected ``compression_settings`` rows for the observed catalog.

    Rows come back in the catalog's own order — ``hypertable_schema``,
    ``hypertable_name``, ``segmentby_column_index NULLS LAST``,
    ``orderby_column_index NULLS LAST`` — so a consumer that orders its query
    the same way can compare the two lists element for element.
    """

    keys = _normalise_present(present)
    rows: list[ExpectationRow] = []
    for schema, name in CANDIDATE_HYPERTABLES:
        key = qualified(schema, name)
        if key not in keys:
            continue
        if is_legacy(name):
            # The renamed table keeps the text shape it was created with; the
            # expand migration never alters its compression settings.
            canonical = (schema, name[: -len(LEGACY_SUFFIX)])
            shape = _TEXT_SHAPE[canonical]
        elif qualified(*legacy_sibling(schema, name)) in keys:
            # Its sibling exists, so this table IS the post-expand narrow one.
            shape = _KEY_SHAPE[(schema, name)]
        else:
            # No sibling: the per-table constant decides, because the catalog
            # cannot distinguish "pre-expand" from "post-contract".
            shape = NO_SIBLING_SHAPE[(schema, name)]
        rows.extend((schema, name, *entry) for entry in shape)
    return rows
