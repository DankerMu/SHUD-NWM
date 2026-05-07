from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as PsycopgConnection

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
SCHEMA_MIGRATIONS_TABLE = "public.schema_migrations"


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
        ensure_schema_migrations_table(connection)

        for migration_file in migration_files:
            if migration_has_been_applied(connection, migration_file.name):
                skipped += 1
                print(f"Skipped migration: {migration_file.name}")
                continue

            try:
                apply_migration(connection, migration_file)
            except psycopg2.Error as error:
                print(f"Failed migration: {migration_file.name}")
                print(error)
                raise SystemExit(1) from error

            applied += 1
            print(f"Applied migration: {migration_file.name}")
    finally:
        connection.close()

    total = len(migration_files)
    print(f"Migrations complete: {applied} applied, {skipped} skipped, {total} total.")


if __name__ == "__main__":
    main()
