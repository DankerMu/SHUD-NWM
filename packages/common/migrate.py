from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as PsycopgConnection

from packages.common.redaction import REDACTION_MARKER, redact_database_dsn

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
SCHEMA_MIGRATIONS_TABLE = "public.schema_migrations"

_B97C16E2_RETIREMENT = (
    "removed from db/migrations by commit b97c16e2 (frequency pipeline retirement) after it had been "
    "applied; the ledger row is true history and stays (#2048)"
)
# Ledger versions that are applied on node-27 but, by decision, have no file on disk. The runner's
# ledger/disk check (`ledger_disk_mismatches`) passes these and refuses every other ledger row
# without a file. Their objects were converged by 000062-000064. A name here must never come back
# to disk: the runner would skip it as applied.
RETIRED_LEDGER_VERSIONS: Mapping[str, str] = MappingProxyType(
    {
        "000007_flood.sql": _B97C16E2_RETIREMENT,
        "000015_flood_return_period_identity_indexes.sql": _B97C16E2_RETIREMENT,
        "000017_return_period_max_over_window_identity.sql": _B97C16E2_RETIREMENT,
        "000020_valid_time_discovery_indexes.sql": _B97C16E2_RETIREMENT,
        "000031_search_discovery_return_period_performance.sql": (
            "renamed by commit b97c16e2 to 000031_search_discovery_performance.sql after it had been "
            "applied; the successor was applied again under its new name, so both rows stay (#2048)"
        ),
        "000034_return_period_run_quality_materialization.sql": _B97C16E2_RETIREMENT,
        "000036_run_product_quality_explicit_source.sql": _B97C16E2_RETIREMENT,
    }
)
_MIGRATION_PREFIX_CHARS = 6

LOCK_TIMEOUT_ENV = "NHMS_MIGRATE_LOCK_TIMEOUT"
STATEMENT_TIMEOUT_ENV = "NHMS_MIGRATE_STATEMENT_TIMEOUT"
DEFAULT_LOCK_TIMEOUT = "5s"
LOCK_NOT_AVAILABLE = "55P03"
QUERY_CANCELED = "57014"
_LOCK_HOLDER_LIMIT = 20
_QUERY_HEAD_CHARS = 120
# PostgreSQL's 55P03 message names no relation, so list every other session in this database
# holding an open transaction; `idle in transaction (aborted)` sessions still hold their locks.
_LOCK_HOLDER_SQL = f"""
    SELECT pid, usename, application_name, state, wait_event_type,
           extract(epoch FROM now() - xact_start)::float8 AS xact_age_seconds,
           query
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
      AND datname = current_database()
      AND xact_start IS NOT NULL
    ORDER BY xact_start
    LIMIT {_LOCK_HOLDER_LIMIT}
"""
# `PASSWORD '<literal>'` (also E'..' and $tag$..$tag$); the literal body is scanned separately.
_SQL_PASSWORD_RE = re.compile(r"\bPASSWORD\s*(?:E(?=')|(?=\$))?['$]", re.IGNORECASE)


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL on top-level semicolons while preserving dollar-quoted blocks."""
    statements: list[str] = []
    start = 0
    index = 0
    dollar_quote: str | None = None
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue

        if dollar_quote is not None:
            if sql.startswith(dollar_quote, index):
                index += len(dollar_quote)
                dollar_quote = None
                continue
            index += 1
            continue

        if in_single_quote:
            if char == "'" and next_char == "'":
                index += 2
                continue
            if char == "'":
                in_single_quote = False
            index += 1
            continue

        if in_double_quote:
            if char == '"' and next_char == '"':
                index += 2
                continue
            if char == '"':
                in_double_quote = False
            index += 1
            continue

        if char == "-" and next_char == "-":
            in_line_comment = True
            index += 2
            continue

        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue

        if char == "'":
            in_single_quote = True
            index += 1
            continue

        if char == '"':
            in_double_quote = True
            index += 1
            continue

        if char == "$":
            tag_end = sql.find("$", index + 1)
            if tag_end != -1:
                tag = sql[index : tag_end + 1]
                tag_body = tag[1:-1]
                if tag_body == "" or tag_body.replace("_", "").isalnum():
                    dollar_quote = tag
                    index = tag_end + 1
                    continue

        if char == ";":
            statement = sql[start : index + 1].strip()
            if statement:
                statements.append(statement)
            start = index + 1

        index += 1

    trailing_statement = sql[start:].strip()
    if trailing_statement:
        statements.append(trailing_statement)

    return statements


def ensure_schema_migrations_table(connection: PsycopgConnection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


def migration_has_been_applied(connection: PsycopgConnection, version: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT 1 FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = %s", (version,))
        return cursor.fetchone() is not None


def read_ledger_versions(connection: PsycopgConnection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version")
        return [str(version) for (version,) in cursor.fetchall()]


def ledger_disk_mismatches(ledger_versions: Iterable[str], migration_names: Iterable[str]) -> list[str]:
    """Every way the ledger and the migration files disagree, one line per offending name.

    The runner only walks files, so without this a deleted or renamed applied migration
    passes silently (#2048). An empty list means the runner may apply:

    - R1: a ledger version with no file is refused unless it is in ``RETIRED_LEDGER_VERSIONS``;
    - R2: two files sharing a 6-digit prefix are refused;
    - R3: a file named like a retired version is refused (the runner would skip it as applied).
    """
    ledger = set(ledger_versions)
    names = sorted(set(migration_names))
    on_disk = set(names)
    problems = [
        f"ledger version {version} has no migration file and is not a recorded retirement"
        for version in sorted(ledger - on_disk)
        if version not in RETIRED_LEDGER_VERSIONS
    ]
    by_prefix: dict[str, list[str]] = {}
    for name in names:
        by_prefix.setdefault(name[:_MIGRATION_PREFIX_CHARS], []).append(name)
    problems.extend(
        f"migration prefix {prefix} is shared by {', '.join(shared)}"
        for prefix, shared in sorted(by_prefix.items())
        if len(shared) > 1
    )
    problems.extend(
        f"migration file {name} reuses the name of a retired ledger version"
        for name in names
        if name in RETIRED_LEDGER_VERSIONS
    )
    return problems


def record_migration(connection: PsycopgConnection, version: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
            (version,),
        )


def apply_migration(connection: PsycopgConnection, migration_file: Path) -> None:
    sql = migration_file.read_text(encoding="utf-8")
    statements = split_sql_statements(sql)
    for statement in statements:
        with connection.cursor() as cursor:
            cursor.execute(statement)
    record_migration(connection, migration_file.name)


def _sql_password_literal_end(text: str, start: int) -> int:
    """Return the index just past the literal opening at ``start`` (or len(text) if unterminated)."""
    if text[start] == "$":
        tag_end = text.find("$", start + 1)
        if tag_end == -1:
            return len(text)
        close = text.find(text[start : tag_end + 1], tag_end + 1)
        return len(text) if close == -1 else close + tag_end + 1 - start
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            # Over-redacting a standard-string backslash is safe; under-redacting E'' is not.
            index += 2
            continue
        if char == "'":
            if text.startswith("''", index):
                index += 2
                continue
            return index + 1
        index += 1
    return len(text)


def _redact_sql_password_literals(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _SQL_PASSWORD_RE.finditer(text):
        if match.start() < cursor:
            continue
        literal_start = match.end() - 1
        parts.append(text[cursor:literal_start])
        parts.append(f"'{REDACTION_MARKER}'")
        cursor = _sql_password_literal_end(text, literal_start)
    parts.append(text[cursor:])
    return "".join(parts)


def redact_migration_text(text: object, database_url: str | None) -> str:
    """Redact SQL password literals and DSN secrets from text the runner prints."""
    return redact_database_dsn(_redact_sql_password_literals(str(text)), database_url)


class MigrationSessionConfigError(RuntimeError):
    """An NHMS_MIGRATE_* timeout value was rejected by PostgreSQL."""


def _set_session_timeout(cursor: Any, name: str, value: str, env_name: str) -> None:
    try:
        cursor.execute("SELECT set_config(%s, %s, false)", (name, value))
    except psycopg2.Error as error:
        raise MigrationSessionConfigError(f"Invalid {env_name}={value!r}: {str(error).strip()}") from error


def _session_setting_with_source(cursor: Any, name: str) -> tuple[str, str]:
    cursor.execute(
        "SELECT current_setting(%s), (SELECT source FROM pg_settings WHERE name = %s)",
        (name, name),
    )
    value, source = cursor.fetchone()
    return str(value), str(source)


def configure_migration_session(connection: PsycopgConnection) -> tuple[str, str]:
    """Bound lock waits on the runner's own session; return effective (lock_timeout, statement_timeout).

    Precedence for ``lock_timeout``: ``NHMS_MIGRATE_LOCK_TIMEOUT`` > a non-zero value already in
    effect on the session (PGOPTIONS / role / database) > ``5s``. ``statement_timeout`` is changed
    only when ``NHMS_MIGRATE_STATEMENT_TIMEOUT`` is set. An empty environment value counts as unset.
    Library helpers never call this: connections owned by other callers keep their settings.
    """
    lock_env = os.getenv(LOCK_TIMEOUT_ENV, "").strip()
    statement_env = os.getenv(STATEMENT_TIMEOUT_ENV, "").strip()
    with connection.cursor() as cursor:
        if lock_env:
            _set_session_timeout(cursor, "lock_timeout", lock_env, LOCK_TIMEOUT_ENV)
        else:
            cursor.execute("SELECT current_setting('lock_timeout')")
            (current,) = cursor.fetchone()
            if str(current) == "0":
                cursor.execute("SELECT set_config(%s, %s, false)", ("lock_timeout", DEFAULT_LOCK_TIMEOUT))
        if statement_env:
            _set_session_timeout(cursor, "statement_timeout", statement_env, STATEMENT_TIMEOUT_ENV)
        lock_value, lock_source = _session_setting_with_source(cursor, "lock_timeout")
        statement_value, statement_source = _session_setting_with_source(cursor, "statement_timeout")
    print(f"Migration session lock_timeout={lock_value} (source: {lock_source})")
    print(f"Migration session statement_timeout={statement_value} (source: {statement_source})")
    return lock_value, statement_value


def _print_lock_holders(connection: PsycopgConnection, database_url: str) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(_LOCK_HOLDER_SQL)
            rows = cursor.fetchall()
    except Exception as error:  # the original error and exit code must survive
        print(f"diagnostic unavailable: {type(error).__name__}")
        return
    print(f"Sessions in this database with an open transaction (oldest first, at most {_LOCK_HOLDER_LIMIT}):")
    if not rows:
        print("  (none observed)")
    for pid, usename, application_name, state, wait_event_type, xact_age_seconds, query in rows:
        query_head = redact_migration_text(query or "", database_url)[:_QUERY_HEAD_CHARS]
        age = "unknown" if xact_age_seconds is None else f"{float(xact_age_seconds):.1f}s"
        print(
            f"  pid={pid} usename={redact_migration_text(usename, database_url)} "
            f"application_name={redact_migration_text(application_name, database_url)} "
            f"state={state} wait_event_type={wait_event_type} xact_age={age} "
            f"query={query_head!r}"
        )


def _report_migration_failure(
    connection: PsycopgConnection,
    database_url: str,
    migration_name: str | None,
    error: psycopg2.Error,
    effective: tuple[str, str],
) -> None:
    target = migration_name or f"{SCHEMA_MIGRATIONS_TABLE} ledger check"
    print(f"Failed migration: {target}")
    print(redact_migration_text(error, database_url).strip())
    pgcode = getattr(error, "pgcode", None)
    if pgcode not in {LOCK_NOT_AVAILABLE, QUERY_CANCELED}:
        return
    lock_timeout, statement_timeout = effective
    if pgcode == LOCK_NOT_AVAILABLE:
        print(f"Lock wait exceeded effective lock_timeout={lock_timeout}.")
        _print_lock_holders(connection, database_url)
    else:
        print(f"Statement canceled; effective statement_timeout={statement_timeout}.")
    if migration_name is not None:
        print(
            f"Statements of {migration_name} executed before the failure are already committed "
            "(autocommit); its ledger row was NOT recorded. Rerunning skips ledger-recorded files only: "
            "inspect the catalog (including INVALID indexes left by CREATE INDEX CONCURRENTLY) before retry."
        )


def main() -> None:
    """Apply pending SQL migrations from db/migrations in filename order."""
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations.")

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied = 0
    skipped = 0

    connection = psycopg2.connect(database_url)
    connection.autocommit = True

    try:
        try:
            effective = configure_migration_session(connection)
        except MigrationSessionConfigError as error:
            print(f"Migration session configuration failed: {redact_migration_text(error, database_url)}")
            raise SystemExit(1) from error

        current: str | None = None
        try:
            ensure_schema_migrations_table(connection)
            # Before the first apply, inside this handler: a lock/statement timeout on the ledger
            # read is reported as the ledger check (`current` is None) with the same diagnostics.
            mismatches = ledger_disk_mismatches(
                read_ledger_versions(connection), (path.name for path in migration_files)
            )
            if mismatches:
                print("Migration ledger/disk mismatch; nothing was applied:")
                for mismatch in mismatches:
                    print(f"  {redact_migration_text(mismatch, database_url)}")
                raise SystemExit(1)

            for migration_file in migration_files:
                current = None
                if migration_has_been_applied(connection, migration_file.name):
                    skipped += 1
                    print(f"Skipped migration: {migration_file.name}")
                    continue

                current = migration_file.name
                apply_migration(connection, migration_file)
                applied += 1
                print(f"Applied migration: {migration_file.name}")
        except psycopg2.Error as error:
            _report_migration_failure(connection, database_url, current, error, effective)
            raise SystemExit(1) from error
    finally:
        connection.close()

    total = len(migration_files)
    print(f"Migrations complete: {applied} applied, {skipped} skipped, {total} total.")


if __name__ == "__main__":
    main()
