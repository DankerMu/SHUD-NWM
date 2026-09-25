"""Real-PostgreSQL falsifier for the #2048 schema-ledger convergence (000062-000064 + the runner's ledger check).

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_schema_ledger_convergence_integration.py

Production is not a fresh database, so a fresh-database run of the three
migrations proves almost nothing about the apply that matters. Every scenario
here therefore first rebuilds node-27's measured drift (design Context of
`schema-ledger-convergence-and-clone-parent-normalisation`) in a per-test
throwaway database: a populated-shape `flood` schema with its hypertable, the six
`hydro.hydro_run` partial indexes carrying their pre-b97c16e2 predicates, and the
seven retired ledger rows. Then the REAL runner, `packages.common.migrate.main()`,
is pointed at it. The oracle for "converged" is a database built from
db/migrations (the session database), not a hand-written expectation.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import psycopg2.errors
import pytest

from packages.common import migrate
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORECAST_RUN_ID,
    MODEL_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

_CONVERGENCE_MIGRATIONS = (
    "000062_hydro_run_status_frequency_done_convergence.sql",
    "000063_hydro_run_partial_index_predicate_convergence.sql",
    "000064_drop_retired_flood_schema.sql",
)
_FLOOD_DROP_MIGRATION = _CONVERGENCE_MIGRATIONS[2]
_INDEX_REBUILD_MIGRATION = _CONVERGENCE_MIGRATIONS[1]

# node-27's `enum_range(NULL::hydro.run_status)`, measured 2026-09-25.
_PRODUCTION_RUN_STATUS_LABELS = (
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "parsed",
    "frequency_done",
    "published",
    "failed",
    "cancelled",
    "superseded",
)

_CANDIDATE_COLUMNS = "(LOWER(source_id), run_type, basin_version_id, cycle_time DESC, run_id DESC)"
# The six partial indexes with the predicates production carried before 000063
# (design Context, "Live hydro_run predicates").
_STALE_HYDRO_RUN_INDEXES = {
    "hydro_run_latest_ready_run_idx": (
        "(cycle_time DESC, run_id DESC) WHERE status IN ('frequency_done', 'published')"
    ),
    "hydro_run_qhh_latest_candidate_idx": (
        f"{_CANDIDATE_COLUMNS} WHERE cycle_time IS NOT NULL AND status IN ('frequency_done', 'published')"
    ),
    "hydro_run_qhh_latest_candidate_parsed_idx": (
        f"{_CANDIDATE_COLUMNS} WHERE cycle_time IS NOT NULL AND status IN ('parsed', 'frequency_done', 'published')"
    ),
    "hydro_run_display_product_basin_status_idx": (
        "(basin_version_id, status) WHERE status IN ('parsed', 'frequency_done', 'published')"
    ),
    "hydro_run_display_ready_candidate_idx": (
        f"{_CANDIDATE_COLUMNS} WHERE cycle_time IS NOT NULL "
        "AND status IN ('succeeded', 'parsed', 'frequency_done', 'published')"
    ),
    "hydro_run_display_ready_basin_status_idx": (
        "(basin_version_id, status) WHERE status IN ('succeeded', 'parsed', 'frequency_done', 'published')"
    ),
}

# A faithful minimal shape of the retired flood schema (git show b97c16e2^ of
# 000007 / 000034 / 000036): the three tables, the hypertable, the foreign keys
# into hydro.hydro_run and core.model_instance, the ON DELETE CASCADE, and the
# jsonb_typeof CHECKs the node-27 roles audit used to trust.
_FLOOD_TABLES = ("flood_frequency_curve", "return_period_result", "run_product_quality")
_FLOOD_DDL = (
    "CREATE SCHEMA flood",
    """
    CREATE TABLE flood.flood_frequency_curve (
      curve_id TEXT PRIMARY KEY,
      model_id TEXT NOT NULL REFERENCES core.model_instance(model_id),
      river_segment_id TEXT NOT NULL,
      q2 DOUBLE PRECISION
    )
    """,
    """
    CREATE TABLE flood.return_period_result (
      run_id TEXT NOT NULL REFERENCES hydro.hydro_run(run_id),
      river_segment_id TEXT NOT NULL,
      valid_time TIMESTAMPTZ NOT NULL,
      q_value DOUBLE PRECISION NOT NULL,
      PRIMARY KEY (run_id, river_segment_id, valid_time)
    )
    """,
    "SELECT create_hypertable('flood.return_period_result', 'valid_time')",
    """
    CREATE TABLE flood.run_product_quality (
      run_id TEXT PRIMARY KEY REFERENCES hydro.hydro_run(run_id) ON DELETE CASCADE,
      unavailable_products JSONB NOT NULL DEFAULT '[]'::jsonb,
      residual_blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
      CONSTRAINT run_product_quality_unavailable_products_array_chk
        CHECK (jsonb_typeof(unavailable_products) = 'array'),
      CONSTRAINT run_product_quality_residual_blockers_array_chk
        CHECK (jsonb_typeof(residual_blockers) = 'array')
    )
    """,
)
# Production's grant shape around the retired schema (db/roles/node27_write_roles.sql
# default-privileges block): the tables belong to the runtime owner, and the
# migration role carries a default ACL row IN SCHEMA flood. 000064's DROP SCHEMA
# has no CASCADE, so it must get past that pg_default_acl row (an AUTO dependency
# on the namespace) exactly as it will on node-27. The role comes from the role
# block `apply_migrations_from_zero` runs for 000059's owner handover.
_FLOOD_GRANT_SHAPE = (
    *(f"ALTER TABLE flood.{table} OWNER TO nhms_ingest_rw" for table in _FLOOD_TABLES),
    """
    DO $grants$
    BEGIN
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA flood '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nhms_ingest_rw',
        current_user);
    END
    $grants$
    """,
)
_FLOOD_DEFAULT_ACL_COUNT_SQL = """
    SELECT count(*)
    FROM pg_default_acl d
    JOIN pg_namespace n ON n.oid = d.defaclnamespace
    WHERE n.nspname = 'flood'
"""
_ORPHAN_DEFAULT_ACL_COUNT_SQL = """
    SELECT count(*)
    FROM pg_default_acl d
    LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
    WHERE d.defaclnamespace <> 0 AND n.oid IS NULL
"""

# One row per flood table, each satisfying that table's foreign key.
_FLOOD_ROW_INSERTS = {
    "flood_frequency_curve": (
        "INSERT INTO flood.flood_frequency_curve (curve_id, model_id, river_segment_id) VALUES (%s, %s, %s)",
        ("it2048_curve", MODEL_ID, "it2048_seg"),
    ),
    "return_period_result": (
        "INSERT INTO flood.return_period_result (run_id, river_segment_id, valid_time, q_value) "
        "VALUES (%s, %s, '2026-05-03T01:00:00Z', 1.0)",
        (FORECAST_RUN_ID, "it2048_seg"),
    ),
    "run_product_quality": (
        "INSERT INTO flood.run_product_quality (run_id) VALUES (%s)",
        (FORECAST_RUN_ID,),
    ),
}


def _execute(database_url: str, *statements: str | tuple[str, tuple[object, ...]]) -> None:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                if isinstance(statement, tuple):
                    cursor.execute(*statement)
                else:
                    cursor.execute(statement)
    finally:
        connection.close()


def _fetchall(database_url: str, sql: str, params: tuple[object, ...] | None = None) -> list[tuple[object, ...]]:
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        connection.close()


def _hydro_run_indexes(database_url: str) -> dict[str, tuple[str, bool]]:
    rows = _fetchall(
        database_url,
        """
        SELECT c.relname, pg_get_indexdef(c.oid), i.indisvalid
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'hydro' AND c.relname = ANY(%s)
        """,
        (list(_STALE_HYDRO_RUN_INDEXES),),
    )
    return {str(name): (str(definition), bool(valid)) for name, definition, valid in rows}


def _run_status_labels(database_url: str) -> tuple[str, ...]:
    rows = _fetchall(database_url, "SELECT unnest(enum_range(NULL::hydro.run_status))::text")
    return tuple(str(label) for (label,) in rows)


def _ledger(database_url: str) -> set[str]:
    return {str(version) for (version,) in _fetchall(database_url, "SELECT version FROM public.schema_migrations")}


def _flood_relations(database_url: str) -> set[str]:
    rows = _fetchall(
        database_url,
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'flood' AND c.relkind IN ('r', 'p')
        """,
    )
    return {str(name) for (name,) in rows}


def _flood_schema_exists(database_url: str) -> bool:
    return bool(_fetchall(database_url, "SELECT 1 FROM pg_namespace WHERE nspname = 'flood'"))


def _flood_row_count(database_url: str, table: str) -> int:
    return int(_fetchall(database_url, f"SELECT count(*) FROM flood.{table}")[0][0])


def _flood_chunks(database_url: str) -> list[str]:
    rows = _fetchall(
        database_url,
        """
        SELECT chunk_schema || '.' || chunk_name
        FROM timescaledb_information.chunks
        WHERE hypertable_schema = 'flood'
        """,
    )
    return [str(name) for (name,) in rows]


def _build_production_drift(database_url: str) -> None:
    """node-27 as measured: the ledger through 000061, plus everything b97c16e2 left behind.

    `frequency_done` is put back by hand, not by 000062: production has it from
    the original CREATE TYPE and no 000062 ledger row, so the runner must APPLY
    000062 there as an IF NOT EXISTS no-op. Its own transaction, before the
    stale predicates name it.
    """
    apply_migrations_from_zero(database_url, through="000061")
    _execute(database_url, "ALTER TYPE hydro.run_status ADD VALUE 'frequency_done' AFTER 'parsed'")
    seed_issue_126_data(database_url)
    assert _fetchall(database_url, "SELECT 1 FROM pg_roles WHERE rolname = 'nhms_ingest_rw'"), (
        "the harness no longer creates nhms_ingest_rw; the flood grant shape needs it"
    )
    stale_indexes: list[str] = []
    for name, tail in _STALE_HYDRO_RUN_INDEXES.items():
        stale_indexes.append(f"DROP INDEX hydro.{name}")
        stale_indexes.append(f"CREATE INDEX {name} ON hydro.hydro_run {tail}")
    _execute(
        database_url,
        *_FLOOD_DDL,
        *_FLOOD_GRANT_SHAPE,
        *stale_indexes,
        # Production's hypertable has had rows; an emptied chunk must go with it.
        _FLOOD_ROW_INSERTS["return_period_result"],
        "DELETE FROM flood.return_period_result",
        (
            "INSERT INTO public.schema_migrations (version) SELECT unnest(%s::text[])",
            (list(migrate.RETIRED_LEDGER_VERSIONS),),
        ),
    )


def _run_runner(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    """`python -m packages.common.migrate` against ``database_url``, over the real db/migrations."""
    monkeypatch.setattr(migrate, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", database_url)
    for variable in ("NHMS_MIGRATE_LOCK_TIMEOUT", "NHMS_MIGRATE_STATEMENT_TIMEOUT", "PGOPTIONS"):
        monkeypatch.delenv(variable, raising=False)
    capsys.readouterr()
    try:
        migrate.main()
        code = 0
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    return code, capsys.readouterr().out


@pytest.fixture()
def fresh_catalog(integration_database_url: str) -> dict[str, object]:
    """The converged answer: what a database built from db/migrations holds."""
    apply_migrations_from_zero(integration_database_url)
    return {
        "indexes": _hydro_run_indexes(integration_database_url),
        "labels": _run_status_labels(integration_database_url),
    }


def test_runner_converges_a_production_shaped_database(
    throwaway_database_url: str,
    fresh_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = throwaway_database_url
    _build_production_drift(url)
    fresh_indexes = fresh_catalog["indexes"]
    assert isinstance(fresh_indexes, dict) and len(fresh_indexes) == 6
    # Premise: the drift is real, or the convergence assertions below prove nothing.
    stale = _hydro_run_indexes(url)
    assert all(stale[name][0] != fresh_indexes[name][0] for name in _STALE_HYDRO_RUN_INDEXES), stale
    assert _flood_relations(url) == set(_FLOOD_TABLES)
    assert _fetchall(url, _FLOOD_DEFAULT_ACL_COUNT_SQL) == [(1,)]
    assert _fetchall(
        url,
        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'flood' AND c.relkind = 'r'",
    ) == [("nhms_ingest_rw",)]
    chunks = _flood_chunks(url)
    assert chunks, "the emptied hypertable kept no chunk; the chunk-drop assertion would be vacuous"
    assert set(migrate.RETIRED_LEDGER_VERSIONS) <= _ledger(url)

    code, out = _run_runner(url, monkeypatch, capsys)

    assert code == 0, out
    assert f"Applied migration: {_CONVERGENCE_MIGRATIONS[0]}" in out
    assert f"Applied migration: {_INDEX_REBUILD_MIGRATION}" in out
    assert f"Applied migration: {_FLOOD_DROP_MIGRATION}" in out
    assert _hydro_run_indexes(url) == fresh_indexes
    assert all(valid for _definition, valid in fresh_indexes.values())
    assert not _flood_schema_exists(url)
    # The default ACL row went with the namespace, not left dangling.
    assert _fetchall(url, _ORPHAN_DEFAULT_ACL_COUNT_SQL) == [(0,)]
    assert _fetchall(url, "SELECT to_regclass(chunk) FROM unnest(%s::text[]) AS chunk", (chunks,)) == [
        (None,)
    ] * len(chunks)
    assert not _fetchall(url, "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_schema = 'flood'")
    assert _run_status_labels(url) == _PRODUCTION_RUN_STATUS_LABELS == fresh_catalog["labels"]
    ledger = _ledger(url)
    assert set(_CONVERGENCE_MIGRATIONS) <= ledger
    # The retired rows are true history and stay.
    assert set(migrate.RETIRED_LEDGER_VERSIONS) <= ledger


@pytest.mark.parametrize("populated_table", _FLOOD_TABLES)
def test_populated_flood_table_stops_the_drop_and_nothing_is_dropped(
    populated_table: str,
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = throwaway_database_url
    _build_production_drift(url)
    _execute(url, _FLOOD_ROW_INSERTS[populated_table])

    code, out = _run_runner(url, monkeypatch, capsys)

    assert code == 1, out
    assert f"Failed migration: {_FLOOD_DROP_MIGRATION}" in out
    assert f"flood.{populated_table}" in out and "holds rows" in out
    assert _flood_relations(url) == set(_FLOOD_TABLES)
    assert _flood_row_count(url, populated_table) == 1
    assert _FLOOD_DROP_MIGRATION not in _ledger(url)


def test_object_depending_on_a_flood_table_stops_the_drop_and_nothing_is_dropped(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No CASCADE: a view outside `flood` must abort the whole DO block, including the drops before it."""
    url = throwaway_database_url
    _build_production_drift(url)
    _execute(url, "CREATE VIEW public.it2048_flood_dependent AS SELECT run_id FROM flood.run_product_quality")

    code, out = _run_runner(url, monkeypatch, capsys)

    assert code == 1, out
    assert f"Failed migration: {_FLOOD_DROP_MIGRATION}" in out
    assert "depend" in out
    # `run_product_quality` is the LAST table the block drops, so the other two
    # surviving proves the drops are one transaction, not three.
    assert _flood_relations(url) == set(_FLOOD_TABLES)
    assert _flood_chunks(url)
    assert _fetchall(url, "SELECT to_regclass('public.it2048_flood_dependent') IS NOT NULL") == [(True,)]
    assert _FLOOD_DROP_MIGRATION not in _ledger(url)


def test_rerun_after_a_failed_concurrent_build_replaces_the_invalid_index(
    throwaway_database_url: str,
    fresh_catalog: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D2: a CREATE INDEX CONCURRENTLY that fails leaves an INVALID index and no ledger row; the rerun heals it."""
    url = throwaway_database_url
    _build_production_drift(url)
    code, out = _run_runner(url, monkeypatch, capsys)
    assert code == 0, out

    # A real failed concurrent build: the two seeded runs share a basin version,
    # so a UNIQUE build over it fails after the catalog entry exists.
    broken = "hydro_run_display_ready_basin_status_idx"
    assert _fetchall(url, "SELECT count(*) FROM hydro.hydro_run WHERE basin_version_id = %s", (BASIN_VERSION_ID,))[
        0
    ][0] >= 2
    _execute(url, f"DROP INDEX CONCURRENTLY hydro.{broken}")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _execute(url, f"CREATE UNIQUE INDEX CONCURRENTLY {broken} ON hydro.hydro_run (basin_version_id)")
    assert _hydro_run_indexes(url)[broken][1] is False
    _execute(url, ("DELETE FROM public.schema_migrations WHERE version = %s", (_INDEX_REBUILD_MIGRATION,)))

    code, out = _run_runner(url, monkeypatch, capsys)

    assert code == 0, out
    assert f"Applied migration: {_INDEX_REBUILD_MIGRATION}" in out
    assert _hydro_run_indexes(url) == fresh_catalog["indexes"]


def test_fresh_database_applies_the_convergence_migrations_and_reapplies_them_as_no_ops(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = throwaway_database_url
    apply_migrations_from_zero(url)

    def _catalog() -> tuple[object, ...]:
        return (_hydro_run_indexes(url), _run_status_labels(url), _flood_schema_exists(url))

    converged = _catalog()
    assert converged[1] == _PRODUCTION_RUN_STATUS_LABELS
    assert converged[2] is False
    assert all(valid for _definition, valid in converged[0].values())  # type: ignore[union-attr]

    connection = psycopg2.connect(url)
    connection.autocommit = True
    try:
        for name in _CONVERGENCE_MIGRATIONS:
            migrate.apply_migration(connection, migrate.MIGRATIONS_DIR / name)
    finally:
        connection.close()
    assert _catalog() == converged

    # The runner itself passes a fresh ledger's check and has nothing to do.
    code, out = _run_runner(url, monkeypatch, capsys)
    assert code == 0, out
    assert "0 applied" in out


def test_unrecorded_ledger_row_refuses_before_applying_anything(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D4 R1 on a real ledger: exit 1, every pending file left unapplied, no credential in the output."""
    url = throwaway_database_url
    apply_migrations_from_zero(url, through="000061")
    unrecorded = "000035_it2048_unrecorded_probe.sql"
    _execute(url, ("INSERT INTO public.schema_migrations (version) VALUES (%s)", (unrecorded,)))
    ledger_before = _ledger(url)

    code, out = _run_runner(url, monkeypatch, capsys)

    assert code == 1, out
    assert "nothing was applied" in out
    assert unrecorded in out
    assert _ledger(url) == ledger_before
    # 000062 is the next pending file; its one object is the enum label.
    assert "frequency_done" not in _run_status_labels(url)
    password = urlsplit(url).password
    if password:
        assert password not in out
    assert url not in out


def test_ledger_check_runs_in_a_read_only_session(throwaway_database_url: str) -> None:
    """The check can be run read-only against production (tasks 3.2 / 5.4): the reader only SELECTs."""
    url = throwaway_database_url
    apply_migrations_from_zero(url, through="000061")
    connection = psycopg2.connect(url)
    connection.set_session(readonly=True)
    try:
        versions = migrate.read_ledger_versions(connection)
    finally:
        connection.close()

    disk = [path.name for path in sorted(Path(migrate.MIGRATIONS_DIR).glob("*.sql"))]
    assert migrate.ledger_disk_mismatches([*versions, *migrate.RETIRED_LEDGER_VERSIONS], disk) == []
    assert migrate.ledger_disk_mismatches([*versions, "000035_it2048_unrecorded_probe.sql"], disk) != []
