"""#2451: does a MISSING chunk statistic pick ``river_ts_run_discovery_key_idx``?

What this module tries to falsify
---------------------------------

The node-27 measurement archived under
``openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/``
is correlation only. On the newest narrow chunk — the ONE chunk of the three
newest whose ``pg_stat_all_tables.last_analyze`` and ``last_autoanalyze`` are
both NULL — the run-bound forecast-series statement read
``river_ts_run_discovery_key_idx`` and demoted ``river_segment_key`` into a
``Filter`` (``Rows Removed by Filter: 192096`` against 12 returned rows). Every
analysed chunk in the very same plan used ``river_timeseries_narrow_pkey``.

The hypothesis under test, stated so it can lose:

    On a narrow chunk with NO statistics, the run-bound forecast-series
    statement picks an index that does NOT carry ``river_segment_key``, pushing
    it into a ``Filter`` whose ``Rows Removed by Filter / Actual Rows`` ratio
    breaks the D11 bound; after ``ANALYZE hydro.river_timeseries`` the same
    statement picks an index that carries ``river_segment_key`` in its
    ``Index Cond`` and respects the bound.

A negative result is a fully acceptable outcome. Both halves are asserted, both
failure messages name the index the planner actually chose, and the full
before/after plan extract is written out when ``NHMS_STATS_PROOF_OUTPUT`` names
a path, so a refutation is readable rather than a bare ``assert False``.

A HYPOTHESISED mechanism, recorded so the run can contradict it
---------------------------------------------------------------

``db/migrations/000059_river_timeseries_narrow_expand.sql:23-35`` gives the
narrow table three indexes. The run-bound statement binds ``run_key``,
``basin_version_key``, ``river_network_version_key``, ``river_segment_key``,
``variable_e`` and a ``valid_time`` range. That is FOUR equalities plus a range
against ``river_ts_run_discovery_key_idx`` and THREE plus a range against
``river_timeseries_narrow_pkey``. With no column statistics PostgreSQL gives
every equality the same default selectivity, so the index with more bound
columns has the smaller estimate — and ``river_segment_key`` is not one of its
columns. After ANALYZE, ``basin_version_key`` and ``river_network_version_key``
are single-valued while ``river_segment_key`` has thousands of distinct values,
which reverses the ranking decisively.

That is a hypothesis about the planner, not an assertion of this test. Both
default estimates are small enough to clamp to one row, in which case the
pre-ANALYZE choice is a COST TIE broken by the order the chunk's indexes were
created rather than by selectivity at all. ``Plan Rows``, ``Startup Cost`` and
``Total Cost`` are therefore recorded per node and the chunk's index OIDs are
dumped alongside them: if the chosen pre-ANALYZE node reports ``Plan Rows: 1``,
the paragraph above is falsified by the measurement and the evidence says so.
The POST-ANALYZE half is unaffected either way — 24 estimated rows against
72000 is not a tie.

Ways this test could go GREEN for the wrong reason — named, not hidden
----------------------------------------------------------------------

1. ``ANALYZE`` refreshes ``reltuples``/``relpages`` as well as column
   statistics. ``estimate_rel_size`` already reads the chunk's true block count
   before ANALYZE, so the size delta is small, but this design cannot separate
   the two contributions. Residual, not mitigated.
2. A ``Seq Scan`` on the no-statistics chunk would also demote
   ``river_segment_key`` to a ``Filter`` and satisfy the first half — for "no
   index at all" rather than "the wrong index". ``Node Type`` and ``Index Name``
   are recorded per node so the evidence distinguishes the two.
3. The seeded fixture has one basin version and one river-network version, which
   is what makes the discovery index look maximally selective without
   statistics. Production's newest chunk has the same shape; a chunk holding
   several basin versions would not, and the mechanism would differ there.
4. Buffer-cache warmth CANNOT explain the index choice — the planner does not
   consult the buffer cache — but it does move ``Shared Hit Blocks``. Each
   EXPLAIN is therefore run twice and the second (warm) plan is the one kept, so
   the block counts of the two measurements are comparable.
5. If the three candidate index paths cost the SAME without statistics, the
   winner is whichever path was added first, which follows index creation order,
   not selectivity. The first half would then be green for an ordering reason
   that statistics merely happen to override. ``Plan Rows`` / ``Total Cost`` per
   node and ``chunk_indexes`` (name + OID, in creation order) are in the
   evidence precisely so that reading is available to whoever archives it.

Deviation from the task's suggested scaffolding, recorded deliberately:
``tests/test_display_coverage_residual_debt_integration._prepared_database``
stops at migration ``000058`` (PRE-expand), which is the wrong catalog for a
narrow-table index question. This module uses the full
``apply_migrations_from_zero`` + ``seed_issue_126_data`` pair instead — the same
combination ``tests/test_real_database_integration.py:227-228`` already uses —
and imports only ``_connect`` from the residual-debt module.

Run on node-27 against a throwaway database:

    mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp
    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        NHMS_STATS_PROOF_OUTPUT=/home/nwm/tmp/2451-stats-proof.json \\
        uv run pytest -q tests/test_river_timeseries_stats_index_choice_integration.py
"""

from __future__ import annotations

import inspect
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.forecast_store import PsycopgForecastStore
from packages.common.node27_pgdata_workload_plan import evaluate_explain_json_plan
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORCING_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)
from tests.test_display_coverage_residual_debt_integration import _connect

pytestmark = pytest.mark.integration

#: Seeding scale. Production's no-statistics chunk carried 32018 segments x 6
#: hourly steps and the bad plan removed 192096 rows to return 12. The ratio is
#: what matters, not the absolute size: with 3000 segments the bad plan reads one
#: run's 72000 chunk rows to return 24, a ratio of ~3000 against D11's bound of
#: 10, while the good plan reads 24 and removes none. Tuned down from
#: production so the whole module (migrations included) stays well inside two
#: minutes, and seeded with one ``INSERT ... SELECT`` over ``generate_series``
#: rather than row by row.
_SEGMENT_COUNT = 3000
#: Hourly steps per run, chosen so 00:00..23:00 lands entirely inside ONE
#: 1-day chunk (``create_hypertable(..., chunk_time_interval => interval '1 day')``).
_STEP_COUNT = 24
#: Two runs, so ``run_key`` is not single-valued: the decoy run's rows are the
#: chunk neighbours the bad plan has to filter out, which is the production shape.
_RUN_IDS = ("it2451_run_target", "it2451_run_decoy")
_TARGET_RUN_ID = _RUN_IDS[0]

#: A day of its own, distinct from ``seed_issue_126_data``'s 2026-05-03, so the
#: constant ``valid_time`` bounds exclude the it126 chunk by chunk pruning and
#: the measurement is about one chunk only.
_CYCLE_TIME = datetime(2026, 6, 1, tzinfo=UTC)
_ISSUE_TIME = "2026-06-01T00:00:00Z"

_SEGMENT_PREFIX = "it2451_shud_riv_"
_TARGET_SEGMENT_ID = f"{_SEGMENT_PREFIX}000001"

#: The #2417 slot this whole issue is about, as it renders into the NARROW
#: branch of ``_SEGMENT_ROWS_SOURCE_SQL`` (``packages/common/forecast_store.py:73-77``;
#: the ``rt.run_id`` aid above it is deleted for narrow by ``river_ts_render``).
_RUN_PUSHDOWN_MARKER = "rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)"

#: D11's own ceiling, read off the gate rather than copied, so this test goes red
#: if ``packages/common/node27_pgdata_workload_plan.py`` moves the bound.
_FILTER_RATIO_LIMIT: int = inspect.signature(evaluate_explain_json_plan).parameters["filter_ratio_limit"].default

_EXPLAIN_ROUNDS = 2


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _execute(connection: Any, statement: str, parameters: Any = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.rowcount


def _fetch_all(connection: Any, statement: str, parameters: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return [dict(row) for row in cursor.fetchall()]


def _fetch_one(connection: Any, statement: str, parameters: Any = None) -> dict[str, Any] | None:
    rows = _fetch_all(connection, statement, parameters)
    return rows[0] if rows else None


def _disable_autovacuum(connection: Any, relation: str) -> str:
    """Turn autovacuum off for ``relation``, reporting what happened.

    A fresh chunk has ``reltuples = 0``, so its autoanalyze threshold is
    ``50 + 0.1 * 0 = 50`` modified rows — the 144000-row seed trips it the
    instant a worker visits this database, which would erase the very state
    being measured. Disabling it keeps the no-statistics window deterministic
    instead of racing a background worker.

    Wrapped in a savepoint because TimescaleDB versions differ on which
    ``ALTER TABLE`` forms they accept on a chunk; the load-bearing guarantee is
    the ``pg_statistic`` precondition below, not this call, so a refusal is
    recorded in the evidence rather than failing the seed.
    """
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT disable_autovacuum")
        try:
            cursor.execute(f"ALTER TABLE {relation} SET (autovacuum_enabled = false)")
        except Exception as error:
            cursor.execute("ROLLBACK TO SAVEPOINT disable_autovacuum")
            return f"{type(error).__name__}: {error}"
        cursor.execute("RELEASE SAVEPOINT disable_autovacuum")
    return "ok"


def _seed_stats_probe_fixture(connection: Any) -> dict[str, Any]:
    """Seed one day-chunk of narrow facts at the scale the two plans differ at.

    Everything runs inside the caller's single transaction, so autovacuum cannot
    observe — and therefore cannot analyse — the new chunk before the chunk-level
    ``autovacuum_enabled = false`` is committed alongside it.
    """
    report: dict[str, Any] = {
        "segment_count": _SEGMENT_COUNT,
        "step_count": _STEP_COUNT,
        "run_ids": list(_RUN_IDS),
        "cycle_time": _CYCLE_TIME.isoformat(),
    }
    report["hypertable_autovacuum_disable"] = _disable_autovacuum(connection, "hydro.river_timeseries")

    # Segment ids are built with `to_char`, never `format('%s', ...)`: a bare `%`
    # in a statement carrying a parameter mapping is a psycopg2 interpolation
    # error, not a literal.
    segments = _execute(
        connection,
        """
        INSERT INTO core.river_segment (
            river_segment_id, river_network_version_id, segment_order, length_m, geom
        )
        SELECT
            %(prefix)s || to_char(ordinal, 'FM000000'),
            %(river_network_version_id)s,
            ordinal,
            1000.0,
            ST_Multi(
                ST_SetSRID(
                    ST_MakeLine(
                        ST_MakePoint(110.0 + ordinal * 0.0001, 30.0),
                        ST_MakePoint(110.0 + ordinal * 0.0001, 30.001)
                    ),
                    4490
                )
            )
        FROM generate_series(1, %(segment_count)s) AS ordinals(ordinal)
        """,
        {
            "prefix": _SEGMENT_PREFIX,
            "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            "segment_count": _SEGMENT_COUNT,
        },
    )
    assert segments == _SEGMENT_COUNT, f"seeded {segments} of {_SEGMENT_COUNT} river segments"

    for index, run_id in enumerate(_RUN_IDS):
        _execute(
            connection,
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time,
                status, timeseries_store, run_manifest_uri, output_uri, log_uri
            )
            VALUES (
                %(run_id)s, 'forecast', 'forecast_gfs_deterministic', %(model_id)s, %(basin_version_id)s,
                %(forcing_version_id)s, %(source_id)s, %(cycle_time)s, %(cycle_time)s, %(end_time)s,
                'parsed', 'narrow', %(manifest_uri)s, %(output_uri)s, %(log_uri)s
            )
            """,
            {
                "run_id": run_id,
                "model_id": MODEL_ID,
                "basin_version_id": BASIN_VERSION_ID,
                "forcing_version_id": FORCING_VERSION_ID,
                "source_id": SOURCE_ID,
                "cycle_time": _CYCLE_TIME,
                "end_time": _CYCLE_TIME + timedelta(hours=_STEP_COUNT - 1),
                "manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
                "output_uri": f"s3://nhms/runs/{run_id}/output/",
                "log_uri": f"s3://nhms/runs/{run_id}/logs/",
            },
        )
        report.setdefault("run_order", []).append({"position": index, "run_id": run_id})

    facts = _execute(
        connection,
        """
        INSERT INTO hydro.river_timeseries (
            run_key, basin_version_key, river_network_version_key, river_segment_key,
            valid_time, lead_time_hours, variable_e, value, unit_e, quality_flag_e
        )
        SELECT
            h.run_key,
            bv.basin_version_key,
            rnv.river_network_version_key,
            rs.river_segment_key,
            %(cycle_time)s::timestamptz + make_interval(hours => step),
            step,
            'q_down'::hydro.river_variable,
            rs.river_segment_key + step,
            'm3/s'::hydro.river_unit,
            'ok'::hydro.river_quality_flag
        FROM hydro.hydro_run h
        CROSS JOIN core.basin_version bv
        CROSS JOIN core.river_network_version rnv
        JOIN core.river_segment rs
          ON rs.river_network_version_id = %(river_network_version_id)s
         AND rs.segment_order BETWEEN 1 AND %(segment_count)s
         AND rs.river_segment_id = %(prefix)s || to_char(rs.segment_order, 'FM000000')
        CROSS JOIN generate_series(0, %(last_step)s) AS hours(step)
        WHERE h.run_id = ANY(%(run_ids)s)
          AND bv.basin_version_id = %(basin_version_id)s
          AND rnv.river_network_version_id = %(river_network_version_id)s
        """,
        {
            "cycle_time": _CYCLE_TIME,
            "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            "basin_version_id": BASIN_VERSION_ID,
            "segment_count": _SEGMENT_COUNT,
            "prefix": _SEGMENT_PREFIX,
            "last_step": _STEP_COUNT - 1,
            "run_ids": list(_RUN_IDS),
        },
    )
    expected_facts = _SEGMENT_COUNT * _STEP_COUNT * len(_RUN_IDS)
    assert facts == expected_facts, (
        f"seeded {facts} narrow fact rows, expected {expected_facts}; "
        "an authority row (run / basin_version / river_network_version / river_segment) is missing"
    )
    report["fact_rows"] = facts

    report["chunks"] = _narrow_chunks(connection)
    report["chunk_autovacuum_disable"] = {
        chunk["qualified"]: _disable_autovacuum(connection, chunk["qualified"]) for chunk in report["chunks"]
    }
    return report


def _narrow_chunks(connection: Any) -> list[dict[str, Any]]:
    """Every chunk of the NARROW hypertable (never ``river_timeseries_legacy``)."""
    return _fetch_all(
        connection,
        """
        SELECT chunk_schema, chunk_name, range_start, range_end, is_compressed,
               quote_ident(chunk_schema) || '.' || quote_ident(chunk_name) AS qualified
        FROM timescaledb_information.chunks
        WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
        ORDER BY range_start
        """,
    )


def _seeded_chunk(chunks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The ONE narrow chunk covering the seeded cycle day.

    ``seed_issue_126_data`` also writes four narrow rows, at 2026-05-03, so the
    hypertable legitimately holds a second chunk; the measurement must pick the
    seeded one rather than demand that it be alone. Chunk boundaries are
    resolved from ``timescaledb_information.chunks`` and the whole seeded window
    is asserted to fall inside them, so nothing here assumes the 1-day interval
    aligns on UTC midnight.
    """
    window_end = _CYCLE_TIME + timedelta(hours=_STEP_COUNT - 1)
    covering = [chunk for chunk in chunks if chunk["range_start"] <= _CYCLE_TIME < chunk["range_end"]]
    assert len(covering) == 1, (
        f"expected exactly one narrow chunk covering {_CYCLE_TIME.isoformat()}; "
        f"got {[(chunk['qualified'], str(chunk['range_start']), str(chunk['range_end'])) for chunk in chunks]}"
    )
    chunk = dict(covering[0])
    assert window_end < chunk["range_end"], (
        f"the seeded window {_CYCLE_TIME.isoformat()}..{window_end.isoformat()} spills past chunk "
        f"{chunk['qualified']} (range_end={chunk['range_end']}); the measurement would span two chunks"
    )
    assert not chunk["is_compressed"], (
        f"chunk {chunk['qualified']} is compressed; the scan would be a DecompressChunk node and neither "
        "measurement would be about index choice"
    )
    return chunk


def _chunk_indexes(connection: Any, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The chunk's indexes with their OIDs, in creation order.

    Recorded because a cost TIE between index paths is broken by the order the
    paths were added, which follows index OID order — see wrong-reason 5 in the
    module docstring.
    """
    return _fetch_all(
        connection,
        """
        SELECT i.relname AS index_name, i.oid AS index_oid, pg_get_indexdef(i.oid) AS definition
        FROM pg_index x
        JOIN pg_class c ON c.oid = x.indrelid
        JOIN pg_class i ON i.oid = x.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(chunk_schema)s AND c.relname = %(chunk_name)s
        ORDER BY i.oid
        """,
        {"chunk_schema": chunk["chunk_schema"], "chunk_name": chunk["chunk_name"]},
    )


def _chunk_statistics_state(connection: Any, chunk: Mapping[str, Any]) -> dict[str, Any]:
    """Both statistics oracles for one chunk.

    ``pg_stats`` is the load-bearing one: it is catalog state, written
    synchronously by ANALYZE. (``pg_stats`` and not ``pg_statistic`` itself:
    the base catalog has no public SELECT grant, so a non-superuser integration
    role would get a permission error instead of a clean precondition failure.)
    ``pg_stat_all_tables`` is what the node-27 receipt quoted and is asserted
    too, but its collector can lag on PostgreSQL 14 and older, so it is never
    the sole gate.
    """
    _execute(connection, "SELECT pg_stat_clear_snapshot()")
    row = _fetch_one(
        connection,
        """
        SELECT
            (SELECT count(*) FROM pg_stats
              WHERE schemaname = %(chunk_schema)s AND tablename = %(chunk_name)s) AS pg_statistic_rows,
            c.reltuples,
            c.relpages,
            c.reloptions,
            s.last_analyze,
            s.last_autoanalyze,
            s.n_live_tup
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
        WHERE n.nspname = %(chunk_schema)s AND c.relname = %(chunk_name)s
        """,
        {"chunk_schema": chunk["chunk_schema"], "chunk_name": chunk["chunk_name"]},
    )
    assert row is not None, f"chunk {chunk['qualified']} vanished from pg_class"
    return row


# ---------------------------------------------------------------------------
# Capturing the REAL statement
# ---------------------------------------------------------------------------


class _RecordingCursor:
    """Pass-through cursor that records every statement and parameter mapping.

    Same shape as the node-27 probe's ``_RecCursor``
    (``openspec/changes/timeseries-narrow-store-expand-contract/receipts/2026-09-17-i8-explain-gate/probe1987.py``),
    reproduced here rather than imported: that directory is archived evidence,
    not a library.
    """

    def __init__(self, cursor: Any, sink: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._sink = sink

    def execute(self, statement: Any, parameters: Any = None) -> None:
        if isinstance(parameters, Mapping):
            captured: Any = dict(parameters)
        elif parameters is None:
            captured = {}
        else:
            captured = list(parameters)
        self._sink.append({"sql": str(statement), "params": captured})
        self._cursor.execute(statement, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _RecordingForecastStore(PsycopgForecastStore):
    """The real store, driven over a caller-owned connection, with a tap on it."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        super().__init__("recording://issue-2451")
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_sink", sink)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        cursor = self._connection.cursor()
        try:
            yield _RecordingCursor(cursor, self._sink)
        finally:
            cursor.close()


def _capture_fact_statement(connection: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive the public run-bound read and return (fact statement, response)."""
    sink: list[dict[str, Any]] = []
    store = _RecordingForecastStore(connection, sink)
    response = store.forecast_series(
        basin_version_id=BASIN_VERSION_ID,
        segment_id=_TARGET_SEGMENT_ID,
        river_network_version_id=RIVER_NETWORK_VERSION_ID,
        issue_time=_ISSUE_TIME,
        variables=["q_down"],
        scenarios=["GFS"],
        include_analysis=False,
        run_types=["forecast"],
        run_id=_TARGET_RUN_ID,
        model_id=MODEL_ID,
    )
    fact_statements = [
        statement
        for statement in sink
        if "hydro.river_timeseries" in statement["sql"] and isinstance(statement["params"], dict)
    ]
    assert len(fact_statements) == 1, (
        f"expected exactly one captured statement against hydro.river_timeseries, got {len(fact_statements)}: "
        + " | ".join(" ".join(statement["sql"].split())[:120] for statement in fact_statements)
    )
    return fact_statements[0], response


# ---------------------------------------------------------------------------
# EXPLAIN extraction
# ---------------------------------------------------------------------------


def _walk_plan(node: Mapping[str, Any], depth: int = 0) -> Iterator[tuple[int, Mapping[str, Any]]]:
    yield depth, node
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping):
            yield from _walk_plan(child, depth + 1)


def _index_predicate(node: Mapping[str, Any]) -> str:
    """Everything the ACCESS METHOD evaluated, as opposed to a heap-level filter.

    A bitmap plan splits the answer over two nodes: ``Index Cond`` sits on the
    child ``Bitmap Index Scan`` (which carries no ``Relation Name``) while
    ``Filter`` and the buffer counters sit on the parent ``Bitmap Heap Scan``.
    Reading only the parent's own keys would lose the index predicate and report
    every bitmap plan as a demotion. ``Recheck Cond`` is included for the same
    reason D11's ``_node_predicate_raw`` includes it.
    """
    parts = [str(node.get(key)) for key in ("Index Cond", "Recheck Cond") if node.get(key)]
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping) and child.get("Node Type") == "Bitmap Index Scan" and child.get("Index Cond"):
            parts.append(str(child["Index Cond"]))
    return " ".join(parts)


def _index_names(node: Mapping[str, Any]) -> list[str]:
    names = [str(node["Index Name"])] if node.get("Index Name") else []
    for child in node.get("Plans") or []:
        if isinstance(child, Mapping) and child.get("Node Type") == "Bitmap Index Scan" and child.get("Index Name"):
            names.append(str(child["Index Name"]))
    return names


def _narrow_access_nodes(plan: Mapping[str, Any], narrow_relations: Sequence[str]) -> list[dict[str, Any]]:
    """Per-node extract for every node that reads a chunk of the NARROW table.

    Attribution is by relation name against the chunk list resolved from
    ``timescaledb_information.chunks``, not by index name: the legacy hypertable
    is read by the other branch of the same ``UNION ALL`` and must not be mixed
    in.
    """
    known = set(narrow_relations)
    extracts: list[dict[str, Any]] = []
    for depth, node in _walk_plan(plan):
        relation = node.get("Relation Name")
        if relation is None or str(relation) not in known:
            continue
        loops = int(node.get("Actual Loops") or 1) or 1
        actual_rows = int(node.get("Actual Rows") or 0)
        rows_removed = int(node.get("Rows Removed by Filter") or 0)
        predicate = _index_predicate(node)
        returned_total = actual_rows * loops
        removed_total = rows_removed * loops
        extracts.append(
            {
                "depth": depth,
                "node_type": node.get("Node Type"),
                "relation": str(relation),
                "index_names": _index_names(node),
                "index_cond": node.get("Index Cond"),
                "recheck_cond": node.get("Recheck Cond"),
                "access_predicate": predicate,
                "filter": node.get("Filter"),
                "rows_removed_by_filter": rows_removed,
                "actual_rows": actual_rows,
                "actual_loops": loops,
                "shared_hit_blocks": node.get("Shared Hit Blocks"),
                "shared_read_blocks": node.get("Shared Read Blocks"),
                # Estimates, not measurements: `Plan Rows == 1` on the
                # pre-ANALYZE node means the three index paths tied on cost and
                # the winner was decided by path order, not selectivity.
                "plan_rows": node.get("Plan Rows"),
                "startup_cost": node.get("Startup Cost"),
                "total_cost": node.get("Total Cost"),
                "segment_key_in_index_cond": "river_segment_key" in predicate,
                "segment_key_in_filter": "river_segment_key" in str(node.get("Filter") or ""),
                "returned_total": returned_total,
                "removed_total": removed_total,
                # D11's own comparison (node27_pgdata_workload_plan.py:514),
                # spelled as a product so a zero-row node is judged the same way
                # the gate judges it rather than dividing by zero.
                "breaks_d11_filter_ratio": removed_total > returned_total * _FILTER_RATIO_LIMIT,
                "filter_ratio": removed_total / max(returned_total, 1),
            }
        )
    return extracts


def _relation_census(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every scan node in the plan, chunk or not.

    The assertions are scoped to the seeded chunk, so this census is what makes a
    surprise legible: the ``UNION ALL``'s legacy branch, the empty hypertable
    parent PostgreSQL's inheritance expansion may keep, and the it126 chunk if
    chunk pruning ever failed to exclude it, all show up here.
    """
    return [
        {
            "depth": depth,
            "node_type": node.get("Node Type"),
            "relation": node.get("Relation Name"),
            "schema": node.get("Schema"),
            "index_names": _index_names(node),
            "actual_rows": node.get("Actual Rows"),
            "rows_removed_by_filter": node.get("Rows Removed by Filter"),
        }
        for depth, node in _walk_plan(plan)
        if node.get("Relation Name")
    ]


def _explain(connection: Any, statement: Mapping[str, Any], narrow_relations: Sequence[str]) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) the captured statement, warm.

    Two rounds, keeping the second: the planner never consults the buffer cache,
    so this cannot change which index is chosen — it only makes the two
    measurements' ``Shared Hit Blocks`` comparable.
    """
    plan: Mapping[str, Any] = {}
    root: Mapping[str, Any] = {}
    for _ in range(_EXPLAIN_ROUNDS):
        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement["sql"],
                statement["params"],
            )
            row = cursor.fetchone()
        payload = next(iter(row.values())) if isinstance(row, Mapping) else row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        root = payload[0]
        plan = root["Plan"]
    return {
        "execution_time_ms": root.get("Execution Time"),
        "planning_time_ms": root.get("Planning Time"),
        "root_shared_hit_blocks": plan.get("Shared Hit Blocks"),
        "root_shared_read_blocks": plan.get("Shared Read Blocks"),
        "root_actual_rows": plan.get("Actual Rows"),
        "narrow_nodes": _narrow_access_nodes(plan, narrow_relations),
        "relation_census": _relation_census(plan),
        "plan": plan,
    }


def _describe(nodes: Sequence[Mapping[str, Any]]) -> str:
    return " | ".join(
        f"{node['node_type']}({node['relation']}) index={node['index_names'] or None} "
        f"index_cond={node['index_cond']!r} filter={node['filter']!r} "
        f"removed={node['removed_total']} returned={node['returned_total']} "
        f"ratio={node['filter_ratio']:.1f}"
        for node in nodes
    ) or "<no node read a chunk of hydro.river_timeseries>"


@contextmanager
def _evidence_dump(evidence: dict[str, Any]) -> Iterator[None]:
    """Write the before/after extract to ``NHMS_STATS_PROOF_OUTPUT``, if set.

    In a ``finally`` so a REFUTATION is archived too — that is the outcome whose
    evidence is worth the most. Skipped, not failed, when the variable is unset,
    and a write error is recorded rather than raised so it cannot mask the
    assertion that was actually being reported.
    """
    try:
        yield
    finally:
        target = os.getenv("NHMS_STATS_PROOF_OUTPUT", "").strip()
        if not target:
            evidence["dump_path"] = None
        else:
            try:
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
                evidence["dump_path"] = str(path)
            except OSError as error:
                evidence["dump_path"] = f"{type(error).__name__}: {error}"


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


def test_missing_chunk_statistics_demote_river_segment_key_into_a_filter(
    throwaway_database_url: str,
) -> None:
    """#2451: the no-statistics chunk picks an index without ``river_segment_key``.

    Two measurements of the SAME captured statement against the SAME chunk, the
    only difference being ``ANALYZE hydro.river_timeseries``. Both halves of the
    hypothesis are asserted; a refutation names the index the planner chose.
    """
    apply_migrations_from_zero(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)

    evidence: dict[str, Any] = {
        "issue": 2451,
        "filter_ratio_limit": _FILTER_RATIO_LIMIT,
        "explain_rounds": _EXPLAIN_ROUNDS,
        "target_run_id": _TARGET_RUN_ID,
        "target_segment_id": _TARGET_SEGMENT_ID,
        "issue_time": _ISSUE_TIME,
    }
    with _evidence_dump(evidence):
        before, after = _measure_both_states(throwaway_database_url, evidence)

    demoted = [
        node
        for node in before["narrow_nodes"]
        if not node["segment_key_in_index_cond"] and node["breaks_d11_filter_ratio"]
    ]
    assert demoted, (
        "HYPOTHESIS REFUTED (first half): with NO statistics on the chunk, every node that read "
        "hydro.river_timeseries already carried river_segment_key in its Index Cond, or stayed inside D11's "
        f"filter-ratio bound of {_FILTER_RATIO_LIMIT}. Missing statistics do not by themselves demote "
        f"river_segment_key into a Filter.\nwithout statistics: {_describe(before['narrow_nodes'])}"
        f"\nwith statistics:    {_describe(after['narrow_nodes'])}"
        f"\nevidence: {evidence['dump_path']}"
    )

    still_demoted = [
        node
        for node in after["narrow_nodes"]
        if not node["segment_key_in_index_cond"] or node["breaks_d11_filter_ratio"]
    ]
    assert not still_demoted, (
        "HYPOTHESIS REFUTED (second half): ANALYZE hydro.river_timeseries did NOT restore an index that "
        "carries river_segment_key within D11's filter-ratio bound of "
        f"{_FILTER_RATIO_LIMIT}.\nwithout statistics: {_describe(before['narrow_nodes'])}"
        f"\nwith statistics:    {_describe(after['narrow_nodes'])}"
        f"\nevidence: {evidence['dump_path']}"
    )


def _measure_both_states(
    database_url: str,
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seed, assert the preconditions, and EXPLAIN the captured statement twice.

    Everything that needs a database lives here so the caller can hold the two
    plan extracts after every connection is closed — ``throwaway_database_url``
    drops the database in its teardown, which a live connection would block.
    """
    seed_connection = _connect(database_url)
    seed_connection.autocommit = False
    try:
        evidence["seed"] = _seed_stats_probe_fixture(seed_connection)
        seed_connection.commit()
    finally:
        seed_connection.close()

    connection = _connect(database_url)
    try:
        chunks = _narrow_chunks(connection)
        evidence["narrow_chunks"] = chunks
        chunk = _seeded_chunk(chunks)
        evidence["measured_chunk"] = chunk
        evidence["chunk_indexes"] = _chunk_indexes(connection, chunk)
        # Scoped to the seeded CHUNK, not to `river_timeseries`: PostgreSQL's
        # inheritance expansion can keep the empty hypertable parent in the
        # Append, and a 0-row parent scan would answer the Index Cond question
        # with noise. `relation_census` in the evidence still shows it.
        narrow_relations = [chunk["chunk_name"]]

        before_stats = _chunk_statistics_state(connection, chunk)
        evidence["chunk_statistics_before"] = before_stats
        # Precondition, asserted rather than assumed: if autovacuum got there
        # first the window measured below is not the no-statistics state.
        assert before_stats["pg_statistic_rows"] == 0, (
            f"precondition lost: chunk {chunk['qualified']} already carries "
            f"{before_stats['pg_statistic_rows']} pg_statistic rows before the first EXPLAIN "
            f"(reloptions={before_stats['reloptions']}, "
            f"autovacuum disable={evidence['seed']['chunk_autovacuum_disable']})"
        )
        assert before_stats["last_analyze"] is None and before_stats["last_autoanalyze"] is None, (
            f"precondition lost: chunk {chunk['qualified']} was already analyzed "
            f"(last_analyze={before_stats['last_analyze']}, last_autoanalyze={before_stats['last_autoanalyze']})"
        )

        statement, response = _capture_fact_statement(connection)
        evidence["statement_head"] = " ".join(statement["sql"].split())[:400]
        evidence["statement_params"] = statement["params"]
        # Non-vacuity on the capture: this must be the #2417 run-bound shape, not
        # some other read that happens to mention the table.
        assert _RUN_PUSHDOWN_MARKER in statement["sql"], (
            "the captured statement does not carry the #2417 bound-run pushdown; "
            f"head={evidence['statement_head']}"
        )
        returned_points = [point for series in response["series"] for point in series["points"]]
        evidence["response_point_count"] = len(returned_points)
        assert len(returned_points) == _STEP_COUNT, (
            f"the run-bound read returned {len(returned_points)} points, expected {_STEP_COUNT}; "
            "the plans below would be measuring the wrong rows"
        )

        before = _explain(connection, statement, narrow_relations)
        evidence["explain_without_statistics"] = before
        assert before["narrow_nodes"], (
            "no plan node read a chunk of hydro.river_timeseries without statistics; "
            "the comparison below would be vacuous"
        )

        _execute(connection, "ANALYZE hydro.river_timeseries")
        after_stats = _chunk_statistics_state(connection, chunk)
        evidence["chunk_statistics_after"] = after_stats
        # Separate assertion, separate message: if TimescaleDB does not recurse
        # ANALYZE into chunks on this server, that is its own finding and must
        # not surface as a mysterious refutation of the hypothesis.
        assert after_stats["pg_statistic_rows"] > 0, (
            f"ANALYZE hydro.river_timeseries did not populate statistics for chunk {chunk['qualified']}: "
            "either TimescaleDB did not recurse into chunks on this server, or the connected role does not "
            "own hydro.river_timeseries and PostgreSQL skipped the ANALYZE with a warning "
            "(000059 sets the owner to nhms_ingest_rw). Either way the second measurement below would be "
            "a repeat of the first, so this is reported as its own finding rather than as a refutation."
        )

        after = _explain(connection, statement, narrow_relations)
        evidence["explain_with_statistics"] = after
        assert after["narrow_nodes"], "no plan node read a chunk of hydro.river_timeseries after ANALYZE"
    finally:
        connection.close()
    return before, after
