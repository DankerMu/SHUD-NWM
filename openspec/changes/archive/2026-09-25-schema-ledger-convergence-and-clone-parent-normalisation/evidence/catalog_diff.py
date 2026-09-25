"""#2048 audit: ledger (fresh DB from db/migrations) vs node-27 production catalog.

    cd <checkout> && NHMS_INTEGRATION_DATABASE_URL=<nhms dsn> PROD_DSNFILE=<ro dsn file> \
        PYTHONPATH=. .venv/bin/python <change>/evidence/catalog_diff.py > out.json

Fresh side: a throwaway ``nhms_it_<uuid>`` database built with the tests'
``apply_migrations_from_zero`` and dropped afterwards. Production side: read-only
catalog queries over the ``nhms_display_ro`` DSN. Nothing is written to production.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, os.getcwd())
from tests.conftest import (  # noqa: E402
    _create_database,
    _database_url_with_name,
    _drop_database,
    _integration_database_name,
    _integration_database_url,
)
from tests.integration_helpers import apply_migrations_from_zero  # noqa: E402

SCHEMAS = ("core", "met", "hydro", "map", "ops", "flood", "public")

QUERIES = {
    "schema": """
        SELECT nspname FROM pg_namespace WHERE nspname = ANY(%(s)s)
    """,
    "relation": """
        SELECT n.nspname || '.' || c.relname || ' kind=' || c.relkind::text
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND c.relkind IN ('r','p','v','m','S','f')
    """,
    "index": """
        SELECT schemaname || '.' || indexname || ' :: ' || indexdef
        FROM pg_indexes WHERE schemaname = ANY(%(s)s)
    """,
    "constraint": """
        SELECT n.nspname || '.' || cl.relname || '.' || co.conname || ' :: ' || pg_get_constraintdef(co.oid)
        FROM pg_constraint co JOIN pg_class cl ON cl.oid = co.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace
        WHERE n.nspname = ANY(%(s)s)
    """,
    "column": """
        SELECT table_schema || '.' || table_name || '.' || column_name || ' ' || data_type
               || ' udt=' || udt_schema || '.' || udt_name
               || ' null=' || is_nullable || ' default=' || coalesce(column_default, '<none>')
               || ' generated=' || coalesce(generation_expression, '<none>')
        FROM information_schema.columns WHERE table_schema = ANY(%(s)s)
    """,
    "enum": """
        SELECT n.nspname || '.' || t.typname || ' = ' ||
               string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = ANY(%(s)s) GROUP BY n.nspname, t.typname
    """,
    "function": """
        SELECT n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ') md5='
               || md5(pg_get_functiondef(p.oid))
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = ANY(%(s)s) AND p.prokind IN ('f','p')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
    """,
    "trigger": """
        SELECT n.nspname || '.' || c.relname || '.' || t.tgname || ' :: ' || pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = ANY(%(s)s) AND NOT t.tgisinternal
    """,
    "hypertable": """
        SELECT hypertable_schema || '.' || hypertable_name FROM timescaledb_information.hypertables
    """,
    "extension": """
        SELECT extname FROM pg_extension
    """,
}


def snapshot(dsn: str) -> dict[str, list[str]]:
    connection = psycopg2.connect(dsn)
    connection.set_session(readonly=True, autocommit=True)
    try:
        out: dict[str, list[str]] = {}
        with connection.cursor() as cursor:
            for name, sql in QUERIES.items():
                cursor.execute(sql, {"s": list(SCHEMAS)})
                out[name] = sorted(row[0] for row in cursor.fetchall())
        return out
    finally:
        connection.close()


def main() -> None:
    prod_dsn = Path(os.environ["PROD_DSNFILE"]).read_text(encoding="utf-8").strip()
    base_url = _integration_database_url()
    db_name = _integration_database_name()
    admin_url = _database_url_with_name(base_url, "postgres")
    _create_database(admin_url, db_name)
    try:
        fresh_url = _database_url_with_name(base_url, db_name)
        apply_migrations_from_zero(fresh_url)
        fresh = snapshot(fresh_url)
    finally:
        _drop_database(admin_url, db_name)
    prod = snapshot(prod_dsn)
    report = {}
    for name in QUERIES:
        f, p = set(fresh[name]), set(prod[name])
        report[name] = {
            "fresh_count": len(f),
            "prod_count": len(p),
            "ledger_only": sorted(f - p),
            "prod_only": sorted(p - f),
        }
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
