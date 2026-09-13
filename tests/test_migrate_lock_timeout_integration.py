"""Real-PostgreSQL falsifier for the migration runner lock guard (#2277).

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_migrate_lock_timeout_integration.py

Every test runs in a per-test throwaway database (never a live one).
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest

from packages.common import migrate

pytestmark = pytest.mark.integration

PROBE_VERSION = "999901_it2277_lock_probe.sql"
HOLDER_APPLICATION_NAME = "it2277-holder"


def _ledger_versions(database_url: str) -> set[str]:
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
            if not cursor.fetchone()[0]:
                return set()
            cursor.execute("SELECT version FROM public.schema_migrations")
            return {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()


def _runner_env(monkeypatch: pytest.MonkeyPatch, database_url: str, migrations_dir: Path) -> None:
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", migrations_dir)
    monkeypatch.setattr(migrate, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("NHMS_MIGRATE_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", raising=False)
    monkeypatch.delenv("PGOPTIONS", raising=False)


def test_blocked_alter_prints_holder_and_leaves_ledger_clean(
    throwaway_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = psycopg2.connect(throwaway_database_url)
    setup.autocommit = True
    with setup.cursor() as cursor:
        cursor.execute("CREATE SCHEMA it2277")
        cursor.execute("CREATE TABLE it2277.lock_probe (id integer)")
    setup.close()

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / PROBE_VERSION).write_text(
        "ALTER TABLE it2277.lock_probe ADD COLUMN probe_col integer;\n", encoding="utf-8"
    )
    _runner_env(monkeypatch, throwaway_database_url, migrations_dir)
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "200ms")

    holder = psycopg2.connect(throwaway_database_url, application_name=HOLDER_APPLICATION_NAME)
    try:
        holder_pid = holder.get_backend_pid()
        with holder.cursor() as cursor:
            cursor.execute("LOCK TABLE it2277.lock_probe IN ACCESS EXCLUSIVE MODE")

        started = time.monotonic()
        with pytest.raises(SystemExit) as raised:
            migrate.main()
        elapsed = time.monotonic() - started
        out = capsys.readouterr().out

        assert raised.value.code == 1
        assert elapsed < 10
        assert "Migration session lock_timeout=200ms (source: session)" in out
        assert f"Failed migration: {PROBE_VERSION}" in out
        assert "lock timeout" in out
        assert f"pid={holder_pid}" in out
        assert f"application_name={HOLDER_APPLICATION_NAME}" in out
        assert "state=idle in transaction " in out
        password = urlsplit(throwaway_database_url).password
        if password:
            assert password not in out
        assert PROBE_VERSION not in _ledger_versions(throwaway_database_url)

        # A failed statement inside a savepoint leaves the session `idle in transaction (aborted)`
        # while the outer transaction keeps xact_start and its lock, so it must still be listed.
        # (A top-level abort releases every lock and clears xact_start: nothing to list there.)
        with holder.cursor() as cursor:
            cursor.execute("SAVEPOINT it2277")
            with pytest.raises(psycopg2.Error):
                cursor.execute("SELECT 1 / 0")
        with pytest.raises(SystemExit) as raised_again:
            migrate.main()
        out = capsys.readouterr().out
        assert raised_again.value.code == 1
        assert f"pid={holder_pid}" in out
        assert "state=idle in transaction (aborted)" in out
        assert PROBE_VERSION not in _ledger_versions(throwaway_database_url)

        holder.rollback()
    finally:
        holder.close()

    migrate.main()
    out = capsys.readouterr().out
    assert f"Applied migration: {PROBE_VERSION}" in out
    assert PROBE_VERSION in _ledger_versions(throwaway_database_url)


def test_default_guard_and_pgoptions_precedence_on_a_real_session(
    throwaway_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _runner_env(monkeypatch, throwaway_database_url, migrations_dir)

    migrate.main()
    out = capsys.readouterr().out
    assert "Migration session lock_timeout=5s (source: session)" in out
    assert "Migration session statement_timeout=0 (source: default)" in out

    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=30s -c statement_timeout=120s")
    migrate.main()
    out = capsys.readouterr().out
    assert "Migration session lock_timeout=30s (source: client)" in out
    assert "Migration session statement_timeout=2min (source: client)" in out


def test_invalid_environment_value_is_rejected_by_postgres(
    throwaway_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / PROBE_VERSION).write_text("CREATE TABLE it2277_never (id integer);\n", encoding="utf-8")
    _runner_env(monkeypatch, throwaway_database_url, migrations_dir)
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "five-seconds")

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    assert raised.value.code == 1
    assert "NHMS_MIGRATE_LOCK_TIMEOUT" in capsys.readouterr().out
    assert _ledger_versions(throwaway_database_url) == set()


def test_apply_migration_leaves_caller_session_settings_unchanged(
    throwaway_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "200ms")
    monkeypatch.setenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", "10min")
    migration = tmp_path / PROBE_VERSION
    migration.write_text("CREATE TABLE it2277_library (id integer);\n", encoding="utf-8")

    connection = psycopg2.connect(throwaway_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            untouched = cursor.fetchone()
        migrate.ensure_schema_migrations_table(connection)
        migrate.apply_migration(connection, migration)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            assert cursor.fetchone() == untouched

            cursor.execute("SET lock_timeout = '7s'")
        migration.write_text("CREATE TABLE it2277_library_two (id integer);\n", encoding="utf-8")
        migrate.apply_migration(connection, migration)
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            assert cursor.fetchone() == ("7s", untouched[1])
    finally:
        connection.close()
