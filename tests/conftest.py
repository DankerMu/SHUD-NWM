from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2 import sql

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests that require explicitly configured external services")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    skip_reason = _integration_skip_reason()
    skip_integration = pytest.mark.skip(reason=skip_reason or "integration tests are explicitly enabled")
    for item in items:
        if "integration" in item.keywords and skip_reason:
            item.add_marker(skip_integration)


@pytest.fixture(scope="session")
def integration_database_url() -> Iterator[str]:
    skip_reason = _integration_skip_reason()
    if skip_reason:
        pytest.skip(skip_reason)
    base_url = _integration_database_url()

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
    integration_url = os.getenv("NHMS_INTEGRATION_DATABASE_URL", "").strip()
    if integration_url:
        return integration_url
    if _env_flag("NHMS_ALLOW_DATABASE_URL_INTEGRATION"):
        return os.getenv("DATABASE_URL", "").strip()
    return ""


def _integration_skip_reason() -> str | None:
    if not _env_flag("NHMS_RUN_INTEGRATION"):
        return (
            "integration tests require explicit opt-in with NHMS_RUN_INTEGRATION=1; "
            "run `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q -m integration` "
            "against PostgreSQL/PostGIS/TimescaleDB"
        )
    if not _integration_database_url():
        return (
            "integration tests require NHMS_INTEGRATION_DATABASE_URL; generic DATABASE_URL is ignored unless "
            "NHMS_ALLOW_DATABASE_URL_INTEGRATION=1 is also set for compatibility"
        )
    return None


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _database_url_with_name(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment))


def _create_database(admin_url: str, database_name: str) -> None:
    connection = psycopg2.connect(admin_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
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
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
    finally:
        connection.close()
