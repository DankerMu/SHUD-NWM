"""Apply SQL migrations for local qhh smoke without TimescaleDB.

This does not replace production migrations. It is a local-only compatibility
runner for machines where PostGIS is available but TimescaleDB is not.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2

from packages.common.migrate import (
    MIGRATIONS_DIR,
    apply_migration,
    ensure_schema_migrations_table,
    migration_has_been_applied,
    record_migration,
)


def _apply_smoke_extensions(connection: psycopg2.extensions.connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cursor.execute(
            """
            CREATE OR REPLACE FUNCTION public.create_hypertable(
                relation regclass,
                time_column_name name,
                if_not_exists boolean DEFAULT false
            )
            RETURNS TABLE(
                hypertable_id integer,
                schema_name name,
                table_name name,
                created boolean
            )
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RETURN QUERY SELECT 0, split_part(relation::text, '.', 1)::name,
                                    split_part(relation::text, '.', 2)::name, false;
            END
            $$;
            """
        )
    record_migration(connection, "000001_extensions.sql")


def main() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        ensure_schema_migrations_table(connection)
        if not migration_has_been_applied(connection, "000001_extensions.sql"):
            _apply_smoke_extensions(connection)
            print("Applied smoke migration: 000001_extensions.sql")
        else:
            print("Skipped migration: 000001_extensions.sql")

        for migration_file in sorted(Path(MIGRATIONS_DIR).glob("*.sql")):
            if migration_file.name == "000001_extensions.sql":
                continue
            if migration_has_been_applied(connection, migration_file.name):
                print(f"Skipped migration: {migration_file.name}")
                continue
            apply_migration(connection, migration_file)
            print(f"Applied migration: {migration_file.name}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
