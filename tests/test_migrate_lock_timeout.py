"""Unit seam for the migration runner's session guard and lock-failure diagnostics (#2277),
and for its ledger/disk check (#2048).

`packages.common.migrate.main()` is driven with a fake psycopg2 connection that
emulates the session GUCs, the `public.schema_migrations` ledger and
`pg_stat_activity`, and records every SQL text it is sent so ordering ("before the
first statement") and absence ("no ledger SQL") are list assertions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest

from packages.common import migrate

DATABASE_URL = "postgresql://nhms:s3cretpw@db/nhms"
_VALID_DURATION = re.compile(r"^\d+(ms|s|min|h|d)?$")


class FakeLockNotAvailable(psycopg2.errors.LockNotAvailable):
    pgcode = "55P03"


class FakeQueryCanceled(psycopg2.errors.QueryCanceled):
    pgcode = "57014"


class FakeInvalidParameterValue(psycopg2.errors.InvalidParameterValue):
    pgcode = "22023"


class FakeUndefinedTable(psycopg2.errors.UndefinedTable):
    pgcode = "42P01"


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: Any = None
        self._all: list[Any] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        conn = self.connection
        text = " ".join(sql.split())
        conn.executed.append((text, params))
        self._one, self._all = None, []
        if text.startswith("SELECT set_config"):
            name, value = params  # type: ignore[misc]
            if not _VALID_DURATION.match(value):
                raise FakeInvalidParameterValue(f'invalid value for parameter "{name}": "{value}"')
            conn.settings[name] = (value, "session")
            self._one = (value,)
        elif text == "SELECT current_setting('lock_timeout')":
            self._one = (conn.settings["lock_timeout"][0],)
        elif text.startswith("SELECT current_setting(%s)"):
            self._one = conn.settings[params[0]]  # type: ignore[index]
        elif "FROM pg_stat_activity" in text:
            if conn.diagnostic_error is not None:
                raise conn.diagnostic_error
            self._all = list(conn.holders)
        elif text.startswith("CREATE TABLE IF NOT EXISTS public.schema_migrations"):
            pass
        elif text.startswith("SELECT version FROM public.schema_migrations"):
            if conn.ledger_read_error is not None:
                raise conn.ledger_read_error
            self._all = [(version,) for version in sorted(conn.ledger)]
        elif text.startswith("SELECT 1 FROM public.schema_migrations"):
            if conn.ledger_check_error is not None:
                raise conn.ledger_check_error
            self._one = (1,) if params[0] in conn.ledger else None  # type: ignore[index]
        elif text.startswith("INSERT INTO public.schema_migrations"):
            conn.ledger.add(params[0])  # type: ignore[index]
        else:
            error = conn.statement_errors.get(text)
            if error is not None:
                raise error

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._all


class FakeConnection:
    def __init__(
        self,
        *,
        lock_timeout: tuple[str, str] = ("0", "default"),
        statement_timeout: tuple[str, str] = ("0", "default"),
    ) -> None:
        self.settings = {"lock_timeout": lock_timeout, "statement_timeout": statement_timeout}
        self.executed: list[tuple[str, Any]] = []
        self.ledger: set[str] = set()
        self.holders: list[tuple[Any, ...]] = []
        self.statement_errors: dict[str, Exception] = {}
        self.diagnostic_error: Exception | None = None
        self.ledger_check_error: Exception | None = None
        self.ledger_read_error: Exception | None = None
        self.autocommit = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True

    def sql_texts(self) -> list[str]:
        return [text for text, _params in self.executed]

    def index_of(self, prefix: str) -> int:
        return next(i for i, text in enumerate(self.sql_texts()) if text.startswith(prefix))

    def set_config_calls(self) -> list[tuple[str, str]]:
        return [params for text, params in self.executed if text.startswith("SELECT set_config")]


@pytest.fixture
def migrations_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", directory)
    monkeypatch.setattr(migrate, "load_dotenv", lambda: None)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.delenv("NHMS_MIGRATE_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", raising=False)
    return directory


def _install(monkeypatch: pytest.MonkeyPatch, connection: FakeConnection) -> None:
    monkeypatch.setattr(migrate.psycopg2, "connect", lambda _url: connection)


def _write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Requirement: bound lock waits by default
# ---------------------------------------------------------------------------


def test_default_session_gets_5s_lock_timeout_before_first_statement(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    connection = FakeConnection()
    _install(monkeypatch, connection)

    migrate.main()

    out = capsys.readouterr().out
    assert connection.set_config_calls() == [("lock_timeout", "5s")]
    assert connection.index_of("SELECT set_config") < connection.index_of("CREATE TABLE IF NOT EXISTS")
    assert connection.settings["statement_timeout"] == ("0", "default")
    assert "Migration session lock_timeout=5s (source: session)" in out
    assert "Migration session statement_timeout=0 (source: default)" in out
    assert out.index("lock_timeout=5s") < out.index("Applied migration: 000001_a.sql")
    assert connection.ledger == {"000001_a.sql"}
    assert connection.closed


def test_pgoptions_session_values_are_kept(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = FakeConnection(lock_timeout=("30s", "client"), statement_timeout=("120s", "client"))
    _install(monkeypatch, connection)

    migrate.main()

    out = capsys.readouterr().out
    assert connection.set_config_calls() == []
    assert "Migration session lock_timeout=30s (source: client)" in out
    assert "Migration session statement_timeout=120s (source: client)" in out


def test_explicit_environment_values_win(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "200ms")
    monkeypatch.setenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", "10min")
    connection = FakeConnection(lock_timeout=("30s", "client"), statement_timeout=("120s", "client"))
    _install(monkeypatch, connection)

    migrate.main()

    out = capsys.readouterr().out
    assert connection.set_config_calls() == [("lock_timeout", "200ms"), ("statement_timeout", "10min")]
    assert connection.index_of("SELECT set_config") < connection.index_of("CREATE TABLE IF NOT EXISTS")
    assert "Migration session lock_timeout=200ms (source: session)" in out
    assert "Migration session statement_timeout=10min (source: session)" in out


def test_explicit_zero_environment_value_is_applied(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "0")
    connection = FakeConnection(lock_timeout=("30s", "client"))
    _install(monkeypatch, connection)

    migrate.main()

    assert connection.set_config_calls() == [("lock_timeout", "0")]
    assert "Migration session lock_timeout=0 (source: session)" in capsys.readouterr().out


def test_empty_environment_value_counts_as_unset(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "")
    monkeypatch.setenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", "  ")
    connection = FakeConnection()
    _install(monkeypatch, connection)

    migrate.main()

    assert connection.set_config_calls() == [("lock_timeout", "5s")]


@pytest.mark.parametrize("variable", ["NHMS_MIGRATE_LOCK_TIMEOUT", "NHMS_MIGRATE_STATEMENT_TIMEOUT"])
def test_invalid_value_fails_closed_before_any_statement(
    variable: str,
    migrations_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    monkeypatch.setenv(variable, "five-seconds")
    connection = FakeConnection()
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert variable in out
    texts = connection.sql_texts()
    assert not [t for t in texts if "schema_migrations" in t or t.startswith("CREATE TABLE")]
    assert connection.ledger == set()
    assert connection.closed


def test_library_apply_migration_never_touches_session_settings(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_LOCK_TIMEOUT", "200ms")
    migration = _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int); CREATE TABLE b (id int);")
    connection = FakeConnection(lock_timeout=("7s", "session"))

    migrate.ensure_schema_migrations_table(connection)  # type: ignore[arg-type]
    migrate.apply_migration(connection, migration)  # type: ignore[arg-type]

    assert connection.set_config_calls() == []
    assert not [t for t in connection.sql_texts() if "timeout" in t]
    assert connection.settings["lock_timeout"] == ("7s", "session")


# ---------------------------------------------------------------------------
# Requirement: actionable lock-timeout failure, no ledger row
# ---------------------------------------------------------------------------

_HOLDERS = [
    (4242, "nhms", "it-aborted-holder", "idle in transaction (aborted)", "Client", 812.25, "LOCK TABLE t"),
    (4243, "nhms_ingest_rw", "it-plain-holder", "idle in transaction", None, 3.0, "SELECT 1"),
]


def _lock_failure_connection(migrations_dir: Path, *, error: Exception | None = None) -> FakeConnection:
    _write(
        migrations_dir,
        "000001_blocked.sql",
        "CREATE TABLE early (id int);\nALTER TABLE scratch ADD COLUMN c int;\n",
    )
    _write(migrations_dir, "000002_after.sql", "CREATE TABLE later (id int);")
    connection = FakeConnection()
    connection.holders = list(_HOLDERS)
    connection.statement_errors["ALTER TABLE scratch ADD COLUMN c int;"] = error or FakeLockNotAvailable(
        "canceling statement due to lock timeout"
    )
    return connection


def test_lock_timeout_prints_holders_and_exits_without_ledger_row(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _lock_failure_connection(migrations_dir)
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "Failed migration: 000001_blocked.sql" in out
    assert "canceling statement due to lock timeout" in out
    assert "lock_timeout=5s" in out
    for pid, _user, app, state, *_rest in _HOLDERS:
        assert f"pid={pid}" in out
        assert f"application_name={app}" in out
        assert f"state={state}" in out
    assert "xact_age=812.2s" in out or "xact_age=812.3s" in out
    assert "already committed" in out and "autocommit" in out
    assert "000001_blocked.sql" in out and "NOT recorded" in out
    assert connection.ledger == set()
    assert not [t for t in connection.sql_texts() if t.startswith("INSERT")]
    assert "CREATE TABLE later (id int);" not in connection.sql_texts()
    diagnostic = next(t for t in connection.sql_texts() if "pg_stat_activity" in t)
    assert "pid <> pg_backend_pid()" in diagnostic
    assert "datname = current_database()" in diagnostic
    assert "xact_start IS NOT NULL" in diagnostic
    assert "ORDER BY xact_start" in diagnostic
    assert "LIMIT 20" in diagnostic
    # No retry, no cancel/terminate of other sessions.
    assert not [t for t in connection.sql_texts() if "pg_cancel_backend" in t or "pg_terminate_backend" in t]
    assert connection.sql_texts().count("ALTER TABLE scratch ADD COLUMN c int;") == 1
    assert connection.closed


def test_lock_timeout_on_ledger_check_exits_one_with_diagnostic(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    connection = FakeConnection()
    connection.holders = list(_HOLDERS)
    connection.ledger_check_error = FakeLockNotAvailable("canceling statement due to lock timeout")
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "pid=4242" in out
    assert connection.ledger == set()


def test_statement_timeout_names_effective_statement_timeout(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NHMS_MIGRATE_STATEMENT_TIMEOUT", "10min")
    connection = _lock_failure_connection(
        migrations_dir, error=FakeQueryCanceled("canceling statement due to statement timeout")
    )
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "canceling statement due to statement timeout" in out
    assert "effective statement_timeout=10min" in out
    assert connection.ledger == set()


def test_diagnostic_query_failure_keeps_original_error_and_exit_code(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _lock_failure_connection(migrations_dir)
    connection.diagnostic_error = RuntimeError("pg_stat_activity unavailable")
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "canceling statement due to lock timeout" in out
    assert [line for line in out.splitlines() if "diagnostic unavailable" in line] == [
        "diagnostic unavailable: RuntimeError"
    ]
    assert connection.ledger == set()


def test_other_database_errors_keep_the_plain_failure_output(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _lock_failure_connection(migrations_dir, error=FakeUndefinedTable('relation "scratch" does not exist'))
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "Failed migration: 000001_blocked.sql" in out
    assert 'relation "scratch" does not exist' in out
    assert not [t for t in connection.sql_texts() if "pg_stat_activity" in t]
    assert connection.ledger == set()


def test_secrets_never_reach_migration_output(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = _lock_failure_connection(
        migrations_dir, error=FakeLockNotAvailable(f"canceling statement due to lock timeout via {DATABASE_URL}")
    )
    connection.holders = [
        (5001, "nhms", "psql", "idle in transaction", None, 1.0, "ALTER ROLE x PASSWORD 'secret'"),
    ]
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit):
        migrate.main()

    out = capsys.readouterr().out
    assert "pid=5001" in out
    assert "s3cretpw" not in out
    assert "secret" not in out
    assert DATABASE_URL not in out


def test_password_literal_spanning_the_truncation_boundary_is_not_leaked(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "longsecretvalue"
    prefix = "x" * 100 + " PASSWORD '"
    query = f"{prefix}{secret}' VALID UNTIL 'infinity'"
    assert len(prefix) < 120 < len(prefix) + len(secret)
    connection = _lock_failure_connection(migrations_dir)
    connection.holders = [(5002, "nhms", "psql", "active", None, 1.0, query)]
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit):
        migrate.main()

    out = capsys.readouterr().out
    assert "pid=5002" in out
    leaked = [secret[i : i + 4] for i in range(len(secret) - 3) if secret[i : i + 4] in out]
    assert leaked == []


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("CREATE ROLE r LOGIN PASSWORD 'it''s-hidden' VALID UNTIL 'x'", "hidden"),
        ("alter role r encrypted password 'unterminated-hidden", "hidden"),
        ("ALTER ROLE r PASSWORD E'esc\\'hidden'", "hidden"),
        ("ALTER ROLE r PASSWORD $p$dollar-hidden$p$", "hidden"),
    ],
)
def test_sql_password_literal_forms_are_redacted(text: str, secret: str) -> None:
    redacted = migrate.redact_migration_text(text, DATABASE_URL)

    assert secret not in redacted
    assert "[redacted]" in redacted


# ---------------------------------------------------------------------------
# Requirement: refuse a ledger/disk mismatch before applying anything (#2048)
# ---------------------------------------------------------------------------


def test_ledger_check_names_every_unrecorded_missing_version() -> None:
    problems = migrate.ledger_disk_mismatches(
        ["000001_a.sql", "000009_gone.sql", "000010_also_gone.sql"], ["000001_a.sql"]
    )

    assert len(problems) == 2
    assert "000009_gone.sql" in problems[0]
    assert "000010_also_gone.sql" in problems[1]


def test_ledger_check_names_both_files_of_a_duplicated_prefix() -> None:
    problems = migrate.ledger_disk_mismatches([], ["000001_a.sql", "000002_b.sql", "000002_c.sql"])

    assert len(problems) == 1
    assert "000002_b.sql" in problems[0] and "000002_c.sql" in problems[0]


def test_ledger_check_refuses_a_retired_name_back_on_disk() -> None:
    problems = migrate.ledger_disk_mismatches(
        ["000007_flood.sql"], ["000006_hydro.sql", "000007_flood.sql"]
    )

    assert len(problems) == 1
    assert "000007_flood.sql" in problems[0]


def test_ledger_check_passes_the_production_shaped_ledger_and_an_empty_one() -> None:
    """node-27's ledger: every file applied plus the 7 retired rows, both `000031` rows included."""
    on_disk = [path.name for path in sorted(migrate.MIGRATIONS_DIR.glob("*.sql"))]
    production_ledger = [*on_disk, *migrate.RETIRED_LEDGER_VERSIONS]
    assert "000031_search_discovery_performance.sql" in on_disk
    assert "000031_search_discovery_return_period_performance.sql" in production_ledger

    assert migrate.ledger_disk_mismatches(production_ledger, on_disk) == []
    assert migrate.ledger_disk_mismatches([], on_disk) == []


def test_every_retired_ledger_version_names_the_retiring_change() -> None:
    assert len(migrate.RETIRED_LEDGER_VERSIONS) == 7
    for version, reason in migrate.RETIRED_LEDGER_VERSIONS.items():
        assert re.fullmatch(r"\d{6}_[a-z0-9_]+\.sql", version), version
        assert "b97c16e2" in reason and "#2048" in reason, version


def test_ledger_check_runs_after_session_and_table_before_first_apply(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    connection = FakeConnection()
    _install(monkeypatch, connection)

    migrate.main()

    ledger_read = connection.index_of("SELECT version FROM public.schema_migrations")
    assert connection.index_of("SELECT set_config") < connection.index_of("CREATE TABLE IF NOT EXISTS")
    assert connection.index_of("CREATE TABLE IF NOT EXISTS") < ledger_read
    assert ledger_read < connection.index_of("SELECT 1 FROM public.schema_migrations")
    assert ledger_read < connection.sql_texts().index("CREATE TABLE a (id int);")
    assert connection.ledger == {"000001_a.sql"}


def test_unrecorded_ledger_version_refuses_before_any_apply(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    _write(migrations_dir, "000002_b.sql", "CREATE TABLE b (id int);")
    connection = FakeConnection()
    connection.ledger = {"000001_a.sql", "000050_unrecorded.sql"}
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "nothing was applied" in out
    assert "000050_unrecorded.sql" in out
    assert "CREATE TABLE b (id int);" not in connection.sql_texts()
    assert not [t for t in connection.sql_texts() if t.startswith(("INSERT", "SELECT 1 FROM"))]
    assert connection.ledger == {"000001_a.sql", "000050_unrecorded.sql"}
    assert "s3cretpw" not in out
    assert connection.closed


def test_duplicate_prefix_on_disk_refuses_before_any_apply(
    migrations_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    _write(migrations_dir, "000001_b.sql", "CREATE TABLE b (id int);")
    connection = FakeConnection()
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "000001_a.sql" in out and "000001_b.sql" in out
    assert not [t for t in connection.sql_texts() if t.startswith("CREATE TABLE a") or t.startswith("CREATE TABLE b")]
    assert connection.ledger == set()


@pytest.mark.parametrize(
    ("error", "diagnostic"),
    [
        (
            FakeLockNotAvailable("canceling statement due to lock timeout"),
            "Lock wait exceeded effective lock_timeout=5s.",
        ),
        (
            FakeQueryCanceled("canceling statement due to statement timeout"),
            "Statement canceled; effective statement_timeout=0.",
        ),
    ],
)
def test_timeout_on_the_ledger_read_is_reported_as_the_ledger_check(
    error: Exception,
    diagnostic: str,
    migrations_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(migrations_dir, "000001_a.sql", "CREATE TABLE a (id int);")
    connection = FakeConnection()
    connection.holders = list(_HOLDERS)
    connection.ledger_read_error = error
    _install(monkeypatch, connection)

    with pytest.raises(SystemExit) as raised:
        migrate.main()

    out = capsys.readouterr().out
    assert raised.value.code == 1
    assert "Failed migration: public.schema_migrations ledger check" in out
    assert diagnostic in out
    if isinstance(error, FakeLockNotAvailable):
        assert "pid=4242" in out and "pid=4243" in out
    # No migration was being applied, so there is no per-file partial-commit notice.
    assert "NOT recorded" not in out
    assert not [t for t in connection.sql_texts() if t.startswith(("INSERT", "SELECT 1 FROM", "CREATE TABLE a"))]
    assert connection.ledger == set()
    assert connection.closed
