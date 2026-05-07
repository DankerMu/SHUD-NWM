from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
SCHEMA_MIGRATIONS_TABLE = "public.schema_migrations"


def main() -> None:
    """Apply pending SQL migrations from db/migrations in filename order."""
    load_dotenv()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to run migrations.")

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied = 0
    skipped = 0

    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS public.schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

        for migration_file in migration_files:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM public.schema_migrations WHERE version = %s", (migration_file.name,))
                already_applied = cursor.fetchone() is not None

            if already_applied:
                skipped += 1
                continue

            with connection.cursor() as cursor:
                cursor.execute(migration_file.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO public.schema_migrations (version) VALUES (%s)", (migration_file.name,))
            applied += 1
            print(f"Applied migration: {migration_file.name}")

    print(f"Migrations complete: {applied} applied, {skipped} skipped.")


if __name__ == "__main__":
    main()
