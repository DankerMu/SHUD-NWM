from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests that require explicitly configured external services")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    database_url = _integration_database_url()
    skip_integration = pytest.mark.skip(
        reason=(
            "integration test requires NHMS_INTEGRATION_DATABASE_URL or DATABASE_URL; "
            "run explicitly with `uv run pytest -q -m integration` against PostgreSQL/PostGIS/TimescaleDB"
        )
    )
    for item in items:
        if "integration" in item.keywords and not database_url:
            item.add_marker(skip_integration)


@pytest.fixture(scope="session")
def integration_database_url() -> Iterator[str]:
    base_url = _integration_database_url()
    if not base_url:
        pytest.skip("NHMS_INTEGRATION_DATABASE_URL or DATABASE_URL is required for integration tests")

    db_name = f"nhms_it_{os.getpid()}"
    admin_url = _database_url_with_name(base_url, "postgres")
    target_url = _database_url_with_name(base_url, db_name)
    _drop_database(admin_url, db_name)
    _create_database(admin_url, db_name)
    try:
        yield target_url
    finally:
        _drop_database(admin_url, db_name)


def _integration_database_url() -> str:
    return os.getenv("NHMS_INTEGRATION_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()


def _database_url_with_name(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment))


def _create_database(admin_url: str, database_name: str) -> None:
    connection = psycopg2.connect(admin_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        connection.close()


def _drop_database(admin_url: str, database_name: str) -> None:
    connection = psycopg2.connect(admin_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        connection.close()
