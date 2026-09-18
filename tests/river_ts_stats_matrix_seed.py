"""Seeding for the #2451 condition cross product: shape x stats x branch x chunk.

``design.md`` ("How the selection is made") gates a candidate on EVERY cell of
predicate shape x statistics state x store branch x chunk compression state.
This module builds the database side of that: one scenario per
(branch, chunk state, statistics state), each with its own day, its own segment
block and its own pair of runs, so the cells never contaminate each other.

Why each scenario needs its OWN day and its OWN segments
-------------------------------------------------------

* Its own day, 21 days apart — the narrow hypertable is chunked at 1 day and the
  legacy one at the 7-day default, and a forecast read spans
  ``[cycle_time, cycle_time + 7 days]``. 21 days apart puts every scenario's
  chunk outside every other scenario's read window, so one measurement touches
  one chunk and the statistics state of a cell is the statistics state of the
  node it measures.
* Its own ``cycle_time`` — ``_per_source_latest_cycles`` and
  ``_resolve_run_identity`` (``packages/common/forecast_store.py:743``, ``:650``)
  resolve by ``cycle_time`` x ``scenario_id`` x ``model_id``, NOT by segment.
  A shared cycle time would make the ``issue_time=latest`` shape of one scenario
  resolve another scenario's runs.
* Its own segment block — ``_per_source_latest_cycles`` IS segment-scoped, so a
  shared segment pool would make "latest" for a scenario see every other
  scenario's runs through that segment.

The statistics states, in the shape production actually has
----------------------------------------------------------

``absent``: the chunk is never analysed. ``stale``: the decoy run's rows are
inserted, the chunk is analysed, and THEN the target run's rows are inserted and
the chunk is not re-analysed — so the target ``run_key`` is absent from the
column's MCV list, which is what ``design.md`` F9b says production's staleness
is. Merely adding rows for runs already present does not reproduce it, and the
test module asserts the MCV precondition rather than assuming it.

``compressed`` x ``stale`` is expected to be UNCONSTRUCTIBLE on the deployed
TimescaleDB 2.10.2: it needs an INSERT into a compressed chunk of a table
carrying a unique constraint, which landed in 2.11. It is attempted inside a
savepoint and the server's own error is recorded as the cell's construction
status. That cell also has no production analogue — production's stale chunk is
the write frontier, which is never compressed (design.md F8/F11).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import Any

from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORCING_VERSION_ID,
    MODEL_ID,
    RIVER_NETWORK_VERSION_ID,
    SOURCE_ID,
)

#: Seeding scale. Production's no-statistics chunk carried 32018 segments x 6
#: hourly steps and the bad plan removed 192096 rows to return 12. The RATIO is
#: what matters, not the absolute size: with 1000 segments the bad plan reads one
#: run's 24000 chunk rows to return 24 — a per-node ratio of ~999 against D11's
#: bound of 10 — while the good plan reads 24 and removes none. Post-ANALYZE the
#: two estimates are 24 against 24000, which is not a tie either. Tuned down from
#: the single-scenario fixture's 3000 because there are now eight scenarios.
SEGMENTS_PER_SCENARIO = 1000
#: Hourly steps per run, chosen so 00:00..23:00 lands entirely inside ONE narrow
#: 1-day chunk (``create_hypertable(..., chunk_time_interval => interval '1 day')``).
STEP_COUNT = 24
#: Well clear of ``seed_issue_126_data``'s 2026-05-03, so no constant window of
#: this module's can reach the it126 chunk.
BASE_DAY = datetime(2026, 6, 1, tzinfo=UTC)
#: > 7 days (the forecast read's window AND the legacy chunk interval), so each
#: scenario's chunk is invisible to every other scenario's measurement.
DAY_STRIDE = timedelta(days=21)
#: ``seed_issue_126_data`` already owns low ``segment_order`` values; the offset
#: keeps each scenario's block disjoint from it and from every other scenario.
SEGMENT_ORDER_BASE = 100_000

#: The target run and the decoy sit on the SAME ``cycle_time`` under DIFFERENT
#: scenarios, which is production's shape: design.md F8's ``latest`` failure
#: removed 384 192 rows — two runs' worth — because
#: ``_resolve_run_identity`` (``packages/common/forecast_store.py:650``) resolves
#: one run per scenario and pushes them as ``rt.run_key = ANY(...)``. A
#: single-element array would make the ``latest`` cell a re-spelling of the
#: run-bound one rather than the second shape design.md requires. ``source_id``
#: stays ``SOURCE_ID`` for both: it is FK-constrained to ``met.data_source``
#: (``db/migrations/000006_hydro.sql:8``) and ``scenarios=["GFS", "IFS"]``
#: already selects both runs through it, while ``scenario_id`` is free TEXT.
TARGET_SCENARIO_ID = "forecast_gfs_deterministic"
DECOY_SCENARIO_ID = "forecast_ifs_deterministic"
#: What a cell's read must return, per shape: the run-bound read is pinned to one
#: run, the `latest` read reports one run per selected scenario.
EXPECTED_SERIES_BY_SHAPE = {"run_bound": 1, "latest": 2}

BRANCHES = ("narrow", "legacy")
CHUNK_STATES = ("uncompressed", "compressed")
STATISTICS_STATES = ("absent", "stale")
SHAPES = ("run_bound", "latest")

#: Cells that are expected NOT to be constructible on TimescaleDB 2.10.2 (see the
#: module docstring). They are still attempted; they are only excused from the
#: "every required cell must be measured" gate.
EXPECTED_UNCONSTRUCTIBLE = frozenset(
    (branch, "compressed", "stale") for branch in BRANCHES
)


@dataclass(frozen=True)
class Scenario:
    """One (branch, chunk state, statistics state) corner of the cross product."""

    index: int
    branch: str
    chunk_state: str
    statistics: str

    @property
    def key(self) -> str:
        return f"{self.branch}/{self.chunk_state}/{self.statistics}"

    @property
    def triple(self) -> tuple[str, str, str]:
        return (self.branch, self.chunk_state, self.statistics)

    @property
    def compressed(self) -> bool:
        return self.chunk_state == "compressed"

    @property
    def day(self) -> datetime:
        return BASE_DAY + DAY_STRIDE * self.index

    @property
    def issue_time(self) -> str:
        return self.day.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def hypertable_name(self) -> str:
        return "river_timeseries" if self.branch == "narrow" else "river_timeseries_legacy"

    @property
    def hypertable(self) -> str:
        return f"hydro.{self.hypertable_name}"

    @property
    def segment_prefix(self) -> str:
        # `_timeseries_segment_id` only rewrites ids containing `_reach_`, so a
        # `_shud_riv_` id passes through `forecast_series` unchanged.
        return f"it2451_s{self.index}_shud_riv_"

    @property
    def target_segment_id(self) -> str:
        return f"{self.segment_prefix}000001"

    @property
    def target_run_id(self) -> str:
        return f"it2451_s{self.index}_target"

    @property
    def decoy_run_id(self) -> str:
        return f"it2451_s{self.index}_decoy"

    @property
    def order_lo(self) -> int:
        return SEGMENT_ORDER_BASE + self.index * SEGMENTS_PER_SCENARIO + 1

    @property
    def order_hi(self) -> int:
        return SEGMENT_ORDER_BASE + (self.index + 1) * SEGMENTS_PER_SCENARIO


SCENARIOS: tuple[Scenario, ...] = tuple(
    Scenario(index, branch, chunk_state, statistics)
    for index, (branch, chunk_state, statistics) in enumerate(product(BRANCHES, CHUNK_STATES, STATISTICS_STATES))
)


# ---------------------------------------------------------------------------
# Small SQL helpers
# ---------------------------------------------------------------------------


def execute(connection: Any, statement: str, parameters: Any = None) -> int:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return int(cursor.rowcount)


def fetch_all(connection: Any, statement: str, parameters: Any = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        return [dict(row) for row in cursor.fetchall()]


def fetch_one(connection: Any, statement: str, parameters: Any = None) -> dict[str, Any] | None:
    rows = fetch_all(connection, statement, parameters)
    return rows[0] if rows else None


def guarded_call(connection: Any, name: str, call: Any) -> Any:
    """Run ``call()``, reporting a server refusal instead of raising.

    Used for the operations whose availability is a property of the SERVER, not
    of this fixture — ``ALTER TABLE ... SET (autovacuum_enabled = ...)`` on a
    chunk, ``compress_chunk``, ``ANALYZE``, the INSERT into a compressed chunk —
    so a refusal lands in the evidence as the cell's recorded construction status
    rather than as an opaque seed failure.

    Inside a transaction a savepoint keeps the surrounding transaction usable
    after the refusal. In autocommit there is no transaction block to protect,
    and ``SAVEPOINT`` would itself be the error ("SAVEPOINT can only be used in
    transaction blocks"), so it is not issued.
    """
    if getattr(connection, "autocommit", False):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 - the error text IS the evidence
            return f"{type(error).__name__}: {error}"
    with connection.cursor() as cursor:
        cursor.execute(f"SAVEPOINT {name}")
        try:
            outcome = call()
        except Exception as error:  # noqa: BLE001
            cursor.execute(f"ROLLBACK TO SAVEPOINT {name}")
            cursor.execute(f"RELEASE SAVEPOINT {name}")
            return f"{type(error).__name__}: {error}"
        cursor.execute(f"RELEASE SAVEPOINT {name}")
    return outcome


def guarded(connection: Any, name: str, statement: str, parameters: Any = None) -> str:
    outcome = guarded_call(connection, name, lambda: execute(connection, statement, parameters))
    return "ok" if isinstance(outcome, int) else str(outcome)


def disable_autovacuum(connection: Any, relation: str) -> str:
    """Turn autovacuum off for ``relation``.

    A fresh chunk has ``reltuples = 0``, so its autoanalyze threshold is
    ``50 + 0.1 * 0 = 50`` modified rows — the seed trips it the instant a worker
    visits this database, which would erase the very state being measured.
    """
    return guarded(connection, "disable_autovacuum", f"ALTER TABLE {relation} SET (autovacuum_enabled = false)")


def analyze_relation(connection: Any, relation: str) -> str:
    return guarded(connection, "analyze_relation", f"ANALYZE {relation}")


def compress_chunk(connection: Any, relation: str) -> str:
    return guarded(
        connection,
        "compress_chunk",
        "SELECT compress_chunk(%(relation)s::regclass)",
        {"relation": relation},
    )


# ---------------------------------------------------------------------------
# Chunk resolution
# ---------------------------------------------------------------------------


def hypertable_chunks(connection: Any, hypertable_name: str) -> list[dict[str, Any]]:
    return fetch_all(
        connection,
        """
        SELECT chunk_schema, chunk_name, range_start, range_end, is_compressed,
               quote_ident(chunk_schema) || '.' || quote_ident(chunk_name) AS qualified
        FROM timescaledb_information.chunks
        WHERE hypertable_schema = 'hydro' AND hypertable_name = %(hypertable_name)s
        ORDER BY range_start
        """,
        {"hypertable_name": hypertable_name},
    )


def scenario_chunk(connection: Any, scenario: Scenario) -> dict[str, Any]:
    """The ONE chunk of ``scenario``'s hypertable covering its seeded day."""
    chunks = hypertable_chunks(connection, scenario.hypertable_name)
    covering = [chunk for chunk in chunks if chunk["range_start"] <= scenario.day < chunk["range_end"]]
    if len(covering) != 1:
        raise AssertionError(
            f"{scenario.key}: expected exactly one {scenario.hypertable} chunk covering "
            f"{scenario.day.isoformat()}; got "
            f"{[(chunk['qualified'], str(chunk['range_start']), str(chunk['range_end'])) for chunk in chunks]}"
        )
    chunk = dict(covering[0])
    window_end = scenario.day + timedelta(hours=STEP_COUNT - 1)
    if window_end >= chunk["range_end"]:
        raise AssertionError(
            f"{scenario.key}: the seeded window {scenario.day.isoformat()}..{window_end.isoformat()} spills past "
            f"chunk {chunk['qualified']} (range_end={chunk['range_end']}); the measurement would span two chunks"
        )
    return chunk


def compressed_relation(connection: Any, chunk: dict[str, Any]) -> dict[str, Any] | None:
    """The ``compress_hyper_*`` relation backing ``chunk``, if it has one.

    Read from ``_timescaledb_catalog.chunk`` because the compressed relation is
    where the PLANNER's statistics live for a compressed chunk: an "absent
    statistics" precondition asserted on the user chunk would be vacuous.
    The plan-side mapping is structural and needs no catalog (see
    ``tests/river_ts_plan_criteria.extract_cell_nodes``); this one does.
    """
    outcome = guarded_call(
        connection,
        "compressed_relation",
        lambda: fetch_all(
            connection,
            """
            SELECT cc.schema_name AS chunk_schema, cc.table_name AS chunk_name,
                   quote_ident(cc.schema_name) || '.' || quote_ident(cc.table_name) AS qualified
            FROM _timescaledb_catalog.chunk ch
            JOIN _timescaledb_catalog.chunk cc ON cc.id = ch.compressed_chunk_id
            WHERE ch.schema_name = %(chunk_schema)s AND ch.table_name = %(chunk_name)s
            """,
            {"chunk_schema": chunk["chunk_schema"], "chunk_name": chunk["chunk_name"]},
        ),
    )
    if isinstance(outcome, str):
        return {"error": outcome}
    return dict(outcome[0]) if outcome else None


# ---------------------------------------------------------------------------
# Statistics state
# ---------------------------------------------------------------------------


def relation_statistics(connection: Any, chunk_schema: str, chunk_name: str) -> dict[str, Any]:
    """Both statistics oracles for one relation.

    ``pg_stats`` is the load-bearing one: it is catalog state, written
    synchronously by ANALYZE. (``pg_stats`` and not ``pg_statistic`` itself: the
    base catalog has no public SELECT grant, so a non-superuser integration role
    would get a permission error instead of a clean precondition failure.)
    ``pg_stat_all_tables`` is what the node-27 receipt quoted and is recorded
    too, but its collector can lag, so it is never the sole gate.
    """
    execute(connection, "SELECT pg_stat_clear_snapshot()")
    row = fetch_one(
        connection,
        """
        SELECT
            (SELECT count(*) FROM pg_stats
              WHERE schemaname = %(chunk_schema)s AND tablename = %(chunk_name)s) AS pg_statistic_rows,
            c.reltuples, c.relpages, c.reloptions,
            s.last_analyze, s.last_autoanalyze, s.n_live_tup, s.n_mod_since_analyze
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
        WHERE n.nspname = %(chunk_schema)s AND c.relname = %(chunk_name)s
        """,
        {"chunk_schema": chunk_schema, "chunk_name": chunk_name},
    )
    if row is None:
        raise AssertionError(f"relation {chunk_schema}.{chunk_name} vanished from pg_class")
    return row


def column_statistics(connection: Any, chunk_schema: str, chunk_name: str, columns: tuple[str, ...]) -> dict[str, Any]:
    """``most_common_vals`` per column, as recorded text plus a parsed member set.

    ``design.md`` F9b: the staleness that matters is that the target ``run_key``
    is ABSENT from this list, not that many rows changed. The parse is a plain
    brace/comma split, which is exact for the integer surrogate keys this fixture
    checks; the raw text is kept beside it so a surprising value is still legible.
    """
    rows = fetch_all(
        connection,
        """
        SELECT attname, n_distinct, most_common_vals::text AS most_common_vals
        FROM pg_stats
        WHERE schemaname = %(chunk_schema)s AND tablename = %(chunk_name)s
          AND attname = ANY(%(columns)s)
        """,
        {"chunk_schema": chunk_schema, "chunk_name": chunk_name, "columns": list(columns)},
    )
    parsed: dict[str, Any] = {}
    for row in rows:
        raw = row["most_common_vals"]
        members = _parse_array_literal(raw)
        parsed[str(row["attname"])] = {
            "n_distinct": row["n_distinct"],
            "most_common_vals": raw,
            "members": members,
        }
    return parsed


def _parse_array_literal(raw: Any) -> list[str]:
    text = str(raw or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return []
    body = text[1:-1]
    if not body:
        return []
    return [item.strip().strip('"') for item in body.split(",")]


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_matrix(write_connection: Any, autocommit_connection: Any) -> dict[str, Any]:
    """Seed every scenario of the cross product, in two deliberate phases.

    PHASE 1 runs in ``write_connection``'s single open transaction — rows,
    chunks, and each chunk's ``autovacuum_enabled = false`` land together, so
    autovacuum cannot observe (and therefore cannot analyse) a new chunk before
    the opt-out is durable. The phase is committed here.

    PHASE 2 runs in ``autocommit_connection`` and does the things that must not
    be inside a transaction block: ``compress_chunk``, the pre-write ``ANALYZE``
    of the stale scenarios, and the post-``ANALYZE`` target write. Every call
    site of ``compress_chunk`` in this repository is in autocommit
    (``scripts/node27_timeseries_compression.py:596-620``,
    ``tests/test_river_identity_normalization_integration.py:600``), and a
    fixture must not be the first place that assumes it is transaction-safe.
    Phase 2 is safe from autovacuum because phase 1's per-chunk opt-out is
    already committed; the only relation created in phase 2 is each compressed
    chunk, whose opt-out follows its creation immediately and whose statistics
    state is asserted by the caller rather than assumed.
    """
    report: dict[str, Any] = {
        "segments_per_scenario": SEGMENTS_PER_SCENARIO,
        "step_count": STEP_COUNT,
        "base_day": BASE_DAY.isoformat(),
        "day_stride_days": DAY_STRIDE.days,
        "hypertable_autovacuum_disable": {
            hypertable: disable_autovacuum(write_connection, hypertable)
            for hypertable in ("hydro.river_timeseries", "hydro.river_timeseries_legacy")
        },
        "scenarios": {},
    }
    for scenario in SCENARIOS:
        report["scenarios"][scenario.key] = _seed_phase1(write_connection, scenario)
    write_connection.commit()
    for scenario in SCENARIOS:
        _seed_phase2(autocommit_connection, scenario, report["scenarios"][scenario.key])
    return report


def _new_construction(scenario: Scenario) -> dict[str, Any]:
    return {
        "key": scenario.key,
        "index": scenario.index,
        "branch": scenario.branch,
        "chunk_state": scenario.chunk_state,
        "statistics": scenario.statistics,
        "day": scenario.day.isoformat(),
        "target_run_id": scenario.target_run_id,
        "decoy_run_id": scenario.decoy_run_id,
        "target_segment_id": scenario.target_segment_id,
        "steps": [],
    }


def _seed_phase1(connection: Any, scenario: Scenario) -> dict[str, Any]:
    """Rows, chunk and the autovacuum opt-out, in one savepoint-isolated unit.

    A scenario that cannot be built rolls back to its own savepoint and is
    recorded as ``unavailable`` — it is DATA about a condition that could not be
    constructed, and it must not take the other seven with it.
    """
    construction = _new_construction(scenario)
    with connection.cursor() as cursor:
        cursor.execute("SAVEPOINT seed_scenario")
    try:
        _step(construction, "segments", _insert_segments(connection, scenario))
        _step(construction, "runs", _insert_runs(connection, scenario))
        _step(construction, "decoy_facts", _insert_facts(connection, scenario, scenario.decoy_run_id))
        if scenario.statistics == "absent":
            # Both runs land before compression so the absent-compressed cell
            # measures a fully compressed chunk, not a half-compressed one.
            _step(construction, "target_facts", _insert_facts(connection, scenario, scenario.target_run_id))
        chunk = scenario_chunk(connection, scenario)
        construction["chunk"] = chunk
        _step(construction, "chunk_autovacuum_disable", disable_autovacuum(connection, chunk["qualified"]))
    except Exception as error:  # noqa: BLE001 - a scenario that cannot be built is DATA
        with connection.cursor() as cursor:
            cursor.execute("ROLLBACK TO SAVEPOINT seed_scenario")
        construction["status"] = "unavailable"
        construction["error"] = f"{type(error).__name__}: {error}"
        construction.pop("chunk", None)
    else:
        construction["status"] = "seeded"
        construction["error"] = None
    with connection.cursor() as cursor:
        cursor.execute("RELEASE SAVEPOINT seed_scenario")
    return construction


def _seed_phase2(connection: Any, scenario: Scenario, construction: dict[str, Any]) -> None:
    """Compression, the pre-write ANALYZE and the post-ANALYZE target write."""
    if construction.get("status") != "seeded":
        return
    try:
        chunk = dict(construction["chunk"])
        if scenario.compressed:
            _step(construction, "compress_chunk", compress_chunk(connection, chunk["qualified"]))
            # Re-read: `is_compressed` above was measured BEFORE the call, and
            # whether the chunk really is compressed is a gated precondition of
            # the compressed cells rather than an assumption.
            chunk = scenario_chunk(connection, scenario)
            construction["chunk"] = chunk
            compressed = compressed_relation(connection, chunk)
            construction["compressed_relation"] = compressed
            if compressed and "qualified" in compressed:
                _step(
                    construction,
                    "compressed_autovacuum_disable",
                    disable_autovacuum(connection, compressed["qualified"]),
                )

        construction["statistics_relation"] = _statistics_relation(construction, chunk)

        if scenario.statistics == "stale":
            # ANALYZE FIRST, then write the target run: design.md F9b's shape,
            # where the target `run_key` is absent from the MCV list.
            _step(construction, "analyze_chunk", analyze_relation(connection, chunk["qualified"]))
            statistics_relation = construction["statistics_relation"]
            if statistics_relation["qualified"] != chunk["qualified"]:
                _step(
                    construction,
                    "analyze_statistics_relation",
                    analyze_relation(connection, statistics_relation["qualified"]),
                )
            _step(
                construction,
                "target_facts",
                _insert_facts_guarded(connection, scenario, scenario.target_run_id, construction),
            )
    except Exception as error:  # noqa: BLE001
        construction["status"] = "unavailable"
        construction["error"] = f"{type(error).__name__}: {error}"


def _step(construction: dict[str, Any], name: str, outcome: Any) -> Any:
    construction["steps"].append({"step": name, "outcome": outcome})
    return outcome


def _statistics_relation(construction: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    """Where the PLANNER reads this cell's statistics from."""
    compressed = construction.get("compressed_relation")
    if isinstance(compressed, dict) and "qualified" in compressed:
        return {
            "chunk_schema": compressed["chunk_schema"],
            "chunk_name": compressed["chunk_name"],
            "qualified": compressed["qualified"],
            "role": "compressed_relation",
        }
    return {
        "chunk_schema": chunk["chunk_schema"],
        "chunk_name": chunk["chunk_name"],
        "qualified": chunk["qualified"],
        "role": "chunk",
    }


def _insert_facts_guarded(connection: Any, scenario: Scenario, run_id: str, construction: dict[str, Any]) -> Any:
    """Insert the target run's rows, recording a server refusal as construction data.

    This is where ``compressed`` x ``stale`` is expected to stop on TimescaleDB
    2.10.2 (INSERT into a compressed chunk of a table with a unique constraint).
    The server's own message is what the evidence carries; the caller decides
    whether that cell was required.
    """
    outcome = guarded_call(connection, "target_facts", lambda: _insert_facts(connection, scenario, run_id))
    if isinstance(outcome, str):
        construction["target_facts_error"] = outcome
    return outcome


def _insert_segments(connection: Any, scenario: Scenario) -> int:
    # Segment ids are built with `to_char`, never `format('%s', ...)`: a bare `%`
    # in a statement carrying a parameter mapping is a psycopg2 interpolation
    # error, not a literal.
    rows = execute(
        connection,
        """
        INSERT INTO core.river_segment (
            river_segment_id, river_network_version_id, segment_order, length_m, geom
        )
        SELECT
            %(prefix)s || to_char(ordinal, 'FM000000'),
            %(river_network_version_id)s,
            %(order_lo)s + ordinal - 1,
            1000.0,
            ST_Multi(
                ST_SetSRID(
                    ST_MakeLine(
                        ST_MakePoint(110.0 + ordinal * 0.0001, 30.0 + %(index)s * 0.01),
                        ST_MakePoint(110.0 + ordinal * 0.0001, 30.001 + %(index)s * 0.01)
                    ),
                    4490
                )
            )
        FROM generate_series(1, %(segment_count)s) AS ordinals(ordinal)
        """,
        {
            "prefix": scenario.segment_prefix,
            "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            "order_lo": scenario.order_lo,
            "index": scenario.index,
            "segment_count": SEGMENTS_PER_SCENARIO,
        },
    )
    if rows != SEGMENTS_PER_SCENARIO:
        raise AssertionError(f"{scenario.key}: seeded {rows} of {SEGMENTS_PER_SCENARIO} river segments")
    return rows


def _insert_runs(connection: Any, scenario: Scenario) -> list[dict[str, Any]]:
    """The target run and one decoy, whose rows are the chunk neighbours.

    The decoy exists so ``run_key`` is NOT single-valued in the chunk — that is
    the production shape and it is what gives the bad plan something to filter
    out. Same ``cycle_time``, different ``scenario_id``, so the run-bound shape
    reads ONE run (pinned by ``run_id``) while ``issue_time=latest`` resolves
    BOTH and pushes a two-element ``rt.run_key = ANY(...)``, as design.md F8
    measured in production.
    """
    seeded: list[dict[str, Any]] = []
    for run_id, cycle_time, scenario_id in (
        (scenario.target_run_id, scenario.day, TARGET_SCENARIO_ID),
        (scenario.decoy_run_id, scenario.day, DECOY_SCENARIO_ID),
    ):
        execute(
            connection,
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id,
                forcing_version_id, source_id, cycle_time, start_time, end_time,
                status, timeseries_store, run_manifest_uri, output_uri, log_uri
            )
            VALUES (
                %(run_id)s, 'forecast', %(scenario_id)s, %(model_id)s, %(basin_version_id)s,
                %(forcing_version_id)s, %(source_id)s, %(cycle_time)s, %(cycle_time)s, %(end_time)s,
                'parsed', %(timeseries_store)s, %(manifest_uri)s, %(output_uri)s, %(log_uri)s
            )
            """,
            {
                "run_id": run_id,
                "scenario_id": scenario_id,
                "model_id": MODEL_ID,
                "basin_version_id": BASIN_VERSION_ID,
                "forcing_version_id": FORCING_VERSION_ID,
                "source_id": SOURCE_ID,
                "cycle_time": cycle_time,
                "end_time": cycle_time + timedelta(hours=STEP_COUNT - 1),
                "timeseries_store": scenario.branch,
                "manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
                "output_uri": f"s3://nhms/runs/{run_id}/output/",
                "log_uri": f"s3://nhms/runs/{run_id}/logs/",
            },
        )
        seeded.append({"run_id": run_id, "cycle_time": cycle_time.isoformat(), "scenario_id": scenario_id})
    return seeded


_NARROW_FACTS_SQL = """
INSERT INTO hydro.river_timeseries (
    run_key, basin_version_key, river_network_version_key, river_segment_key,
    valid_time, lead_time_hours, variable_e, value, unit_e, quality_flag_e
)
SELECT
    h.run_key,
    bv.basin_version_key,
    rnv.river_network_version_key,
    rs.river_segment_key,
    %(day)s::timestamptz + make_interval(hours => step),
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
 AND rs.segment_order BETWEEN %(order_lo)s AND %(order_hi)s
CROSS JOIN generate_series(0, %(last_step)s) AS hours(step)
WHERE h.run_id = %(run_id)s
  AND bv.basin_version_id = %(basin_version_id)s
  AND rnv.river_network_version_id = %(river_network_version_id)s
"""

#: The legacy table keeps BOTH identity layers (000050 added the surrogate keys
#: beside the text columns and 000059 renamed the table without dropping either),
#: and `render_river_ts_sql(..., "legacy")` binds the text ones. A legacy row
#: missing them would make the legacy branch return nothing and the cell vacuous.
_LEGACY_FACTS_SQL = """
INSERT INTO hydro.river_timeseries_legacy (
    run_id, basin_version_id, river_network_version_id, river_segment_id,
    valid_time, lead_time_hours, variable, value, unit, quality_flag,
    run_key, basin_version_key, river_network_version_key, river_segment_key,
    variable_e, unit_e, quality_flag_e
)
SELECT
    h.run_id,
    %(basin_version_id)s,
    %(river_network_version_id)s,
    rs.river_segment_id,
    %(day)s::timestamptz + make_interval(hours => step),
    step,
    'q_down',
    rs.river_segment_key + step,
    'm3/s',
    'ok',
    h.run_key,
    bv.basin_version_key,
    rnv.river_network_version_key,
    rs.river_segment_key,
    'q_down'::hydro.river_variable,
    'm3/s'::hydro.river_unit,
    'ok'::hydro.river_quality_flag
FROM hydro.hydro_run h
CROSS JOIN core.basin_version bv
CROSS JOIN core.river_network_version rnv
JOIN core.river_segment rs
  ON rs.river_network_version_id = %(river_network_version_id)s
 AND rs.segment_order BETWEEN %(order_lo)s AND %(order_hi)s
CROSS JOIN generate_series(0, %(last_step)s) AS hours(step)
WHERE h.run_id = %(run_id)s
  AND bv.basin_version_id = %(basin_version_id)s
  AND rnv.river_network_version_id = %(river_network_version_id)s
"""


def _insert_facts(connection: Any, scenario: Scenario, run_id: str) -> int:
    statement = _NARROW_FACTS_SQL if scenario.branch == "narrow" else _LEGACY_FACTS_SQL
    rows = execute(
        connection,
        statement,
        {
            "day": scenario.day,
            "river_network_version_id": RIVER_NETWORK_VERSION_ID,
            "basin_version_id": BASIN_VERSION_ID,
            "order_lo": scenario.order_lo,
            "order_hi": scenario.order_hi,
            "last_step": STEP_COUNT - 1,
            "run_id": run_id,
        },
    )
    expected = SEGMENTS_PER_SCENARIO * STEP_COUNT
    if rows != expected:
        raise AssertionError(
            f"{scenario.key}: seeded {rows} {scenario.branch} fact rows for {run_id}, expected {expected}; "
            "an authority row (run / basin_version / river_network_version / river_segment) is missing"
        )
    return rows
