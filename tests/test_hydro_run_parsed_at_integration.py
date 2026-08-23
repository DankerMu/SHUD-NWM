"""Real-database falsifier for the ``parsed_at`` write point (#1789).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_hydro_run_parsed_at_integration.py

Why this test exists, and why a production tick observation cannot replace it:

The obvious way to add ``parsed_at`` is to fold it into ``mark_run_parsed``'s
existing ``SET status = 'parsed', ...`` UPDATE. That UPDATE is gated on
``PARSE_READY_RUN_STATUSES``, which does NOT include ``'published'`` -- and a
re-parse of an already-published run is exactly the population recompute
detection serves. Under that half-fix the re-parse matches zero rows, silently:
no error, no log line, ``parsed_at`` frozen. Its product mtime then exceeds the
timestamp forever, so the pipeline re-ingests the run every tick, rc=0, for as
long as the run exists.

Nothing in a node-27 tick receipt falsifies that shape. "No new handoff was
attempted" passes vacuously whenever no published run happens to be re-parsed
inside the observation window, which is not something the observer controls.
This test controls it: it re-parses a published run on purpose and asserts the
three properties that half-fix violates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

import scripts.node27_autopipeline as autopipe
from packages.common.object_store import LocalObjectStore
from tests.integration_helpers import apply_migrations_from_zero
from workers.output_parser.parser import (
    OutputParser,
    OutputParserConfig,
    PsycopgOutputParserRepository,
)

pytestmark = pytest.mark.integration

_RUN_ID = "run_parsed_at_published_reparse"
_INIT_STATE_ID = "state-a"
_START_TIME = datetime(2026, 6, 1, tzinfo=UTC)
_SEGMENTS = 2
_HOURS = 2
_OBJECT_STORE_PREFIX = "s3://nhms"


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _run_row(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status::text AS status, parsed_at FROM hydro.hydro_run WHERE run_id = %s",
            (_RUN_ID,),
        )
        row = cursor.fetchone()
    assert row is not None
    return dict(row)


def _execute(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def _seed_authority(connection: Any, *, output_uri: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO core.basin VALUES ('b1', 'B1', NULL, NULL, now()) ON CONFLICT DO NOTHING")
        cursor.execute(
            """
            INSERT INTO core.basin_version
                (basin_version_id, basin_id, version_label, geom, active_flag)
            VALUES ('bv1', 'b1', 'v1',
                    ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((0 0,0 1,1 1,0 0)))'), 4490), true)
            ON CONFLICT DO NOTHING
            """
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version
                (river_network_version_id, basin_version_id, version_label, segment_count)
            VALUES ('rnv1', 'bv1', 'v1', %s) ON CONFLICT DO NOTHING
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.river_segment
                (river_segment_id, river_network_version_id, segment_order, properties_json)
            SELECT 'seg-' || g, 'rnv1', g, '{"shud_output_river": "true"}'::jsonb
            FROM generate_series(1, %s) g
            ON CONFLICT DO NOTHING
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance
                (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                 calibration_version_id, shud_code_version, model_package_uri)
            VALUES ('m1', 'bv1', 'rnv1', 'mv1', 'cal1', '1.0', 's3://x')
            ON CONFLICT DO NOTHING
            """
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id, init_state_id,
                 cycle_time, start_time, end_time, status, run_manifest_uri, output_uri)
            VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', %s, %s, %s, %s, 'succeeded', 's3://m', %s)
            ON CONFLICT DO NOTHING
            """,
            (
                _RUN_ID,
                _INIT_STATE_ID,
                _START_TIME,
                _START_TIME,
                _START_TIME + timedelta(hours=_HOURS),
                output_uri,
            ),
        )


def _write_products(root: Path) -> LocalObjectStore:
    """The manifest the criterion reads plus the ``.rivqdown`` the parser reads."""
    store = LocalObjectStore(root, _OBJECT_STORE_PREFIX)
    store.write_bytes_atomic(
        f"runs/{_RUN_ID}/input/manifest.json",
        json.dumps(
            {"identity": {"run_id": _RUN_ID}, "initial_state": {"state_id": _INIT_STATE_ID}}
        ).encode("utf-8"),
    )
    header = ",".join(["time", *(f"seg-{index}" for index in range(1, _SEGMENTS + 1))])
    lines = [header]
    for hour in range(_HOURS):
        timestamp = (_START_TIME + timedelta(hours=hour)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(",".join([timestamp, *(str(86400 * (hour + 1) * n) for n in range(1, _SEGMENTS + 1))]))
    store.write_bytes_atomic(
        f"runs/{_RUN_ID}/output/demo.rivqdown",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )
    return store


def _parse(database_url: str, root: Path, store: LocalObjectStore) -> Any:
    parser = OutputParser(
        config=OutputParserConfig(
            object_store_root=root,
            object_store_prefix=_OBJECT_STORE_PREFIX,
            batch_size=64,
        ),
        repository=PsycopgOutputParserRepository(database_url=database_url),
        object_store=store,
    )
    return parser.parse_run(_RUN_ID)


@pytest.fixture()
def published_run(throwaway_database_url: str, tmp_path: Path) -> Any:
    """A published run whose ``parsed_at`` is older than its object-store product.

    The stale timestamp is written directly rather than waited for: the
    criterion compares ``product_mtime`` against ``parsed_at``, and a test that
    depended on wall-clock ordering within a few milliseconds would be a
    flake, not an oracle.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    root = tmp_path / "object-store"
    _seed_authority(connection, output_uri=f"{_OBJECT_STORE_PREFIX}/runs/{_RUN_ID}/output/")
    store = _write_products(root)

    _parse(throwaway_database_url, root, store)
    assert _run_row(connection)["status"] == "parsed"

    _execute(
        connection,
        """
        UPDATE hydro.hydro_run
        SET status = 'published', parsed_at = parsed_at - interval '1 day'
        WHERE run_id = %s
        """,
        (_RUN_ID,),
    )
    try:
        yield connection, root, store
    finally:
        connection.close()


def test_reparsing_a_published_run_bumps_parsed_at_and_clears_the_handoff(
    published_run: Any, throwaway_database_url: str
) -> None:
    connection, root, store = published_run

    before = _run_row(connection)
    assert before["status"] == "published"
    stale_parsed_at = before["parsed_at"]
    assert stale_parsed_at is not None

    # Non-vacuity: with the stale timestamp in place the run IS judged
    # recomputed, so the assertion after the re-parse is measuring the
    # re-parse and not a criterion that returns everything.
    assert (
        autopipe._already_ingested_runs(throwaway_database_url, [_RUN_ID], object_store_root=root)
        == set()
    )

    _parse(throwaway_database_url, root, store)

    after = _run_row(connection)
    assert after["parsed_at"] > stale_parsed_at, (
        "the parse timestamp did not advance: mark_run_parsed's status UPDATE excludes "
        "'published', so a status-gated parsed_at write matches zero rows here and the run "
        "is re-ingested on every tick forever"
    )
    assert after["status"] == "published", "a re-parse must not downgrade a published run"
    assert autopipe._already_ingested_runs(
        throwaway_database_url, [_RUN_ID], object_store_root=root
    ) == {_RUN_ID}


def test_the_completeness_criterion_reads_no_fact_row_for_a_published_run(
    published_run: Any, throwaway_database_url: str
) -> None:
    """Authority state first (#1674), now with the fact table out of the picture.

    Deleting every fact row of a published run must change nothing: on node-27
    those rows are routinely invisible (NULL-key legacy rows inside compressed
    chunks, retention-dropped chunks), and treating that as incomplete is the
    per-cycle handoff storm this criterion was rewritten to end.
    """
    connection, root, _store = published_run
    _execute(connection, "DELETE FROM hydro.river_timeseries WHERE run_id = %s", (_RUN_ID,))
    _execute(connection, "UPDATE hydro.hydro_run SET parsed_at = now() WHERE run_id = %s", (_RUN_ID,))

    assert autopipe._already_ingested_runs(
        throwaway_database_url, [_RUN_ID], object_store_root=root
    ) == {_RUN_ID}

    # ...and it stays complete even with NO parse timestamp at all, which is
    # the legacy cohort's permanent state after backfill.
    _execute(connection, "UPDATE hydro.hydro_run SET parsed_at = NULL WHERE run_id = %s", (_RUN_ID,))
    assert autopipe._already_ingested_runs(
        throwaway_database_url, [_RUN_ID], object_store_root=root
    ) == {_RUN_ID}


def test_a_parsed_run_without_a_parse_timestamp_stays_incomplete(
    published_run: Any, throwaway_database_url: str
) -> None:
    """The other half of the gate, and the only half that can still say "no".

    ``h.status = 'published' OR h.parsed_at IS NOT NULL``: for a run at
    ``'parsed'`` the second disjunct is the whole test. A NULL parse timestamp
    means the parser chain did not finish, so the run must stay out of the
    already-ingested set and keep being retried. Asserted against a real
    database because a fake that feeds ``completeness_rows`` straight in never
    evaluates the predicate at all.
    """
    connection, root, _store = published_run
    _execute(
        connection,
        "UPDATE hydro.hydro_run SET status = 'parsed', parsed_at = NULL WHERE run_id = %s",
        (_RUN_ID,),
    )

    assert (
        autopipe._already_ingested_runs(throwaway_database_url, [_RUN_ID], object_store_root=root)
        == set()
    )

    _execute(connection, "UPDATE hydro.hydro_run SET parsed_at = now() WHERE run_id = %s", (_RUN_ID,))

    assert autopipe._already_ingested_runs(
        throwaway_database_url, [_RUN_ID], object_store_root=root
    ) == {_RUN_ID}
