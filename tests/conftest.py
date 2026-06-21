from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2 import sql

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests that require explicitly configured external services")
    config.addinivalue_line("markers", "e2e: end-to-end pipeline tests; opt-in via NHMS_RUN_E2E=1 (node-22)")
    config.addinivalue_line("markers", "real_disk: tests that require node-27 DATABASE_URL and OBJECT_STORE_ROOT")
    config.addinivalue_line("markers", "grib: real GRIB2 decode tests; opt-in via NHMS_RUN_GRIB=1 (node-22)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    skip_reason = _integration_skip_reason()
    skip_integration = pytest.mark.skip(reason=skip_reason or "integration tests are explicitly enabled")
    e2e_skip_reason = _opt_in_skip_reason("e2e", "NHMS_RUN_E2E")
    grib_skip_reason = _opt_in_skip_reason("grib", "NHMS_RUN_GRIB")
    skip_e2e = pytest.mark.skip(reason=e2e_skip_reason or "e2e tests are explicitly enabled")
    skip_grib = pytest.mark.skip(reason=grib_skip_reason or "grib tests are explicitly enabled")
    for item in items:
        if "integration" in item.keywords and skip_reason:
            item.add_marker(skip_integration)
        if "e2e" in item.keywords and e2e_skip_reason:
            item.add_marker(skip_e2e)
        if "grib" in item.keywords and grib_skip_reason:
            item.add_marker(skip_grib)


@pytest.fixture(scope="session")
def integration_database_url() -> Iterator[str]:
    skip_reason = _integration_skip_reason()
    if skip_reason:
        pytest.skip(skip_reason)
    base_url = _integration_database_url()

    db_name = _integration_database_name()
    admin_url = _database_url_with_name(base_url, "postgres")
    target_url = _database_url_with_name(base_url, db_name)
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


def _opt_in_skip_reason(marker: str, env_var: str) -> str | None:
    if _env_flag(env_var):
        return None
    return (
        f"{marker} tests require explicit opt-in with {env_var}=1; "
        f"run on node-22 (outside production windows) via "
        f'`NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run pytest -m "e2e or grib"`'
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _database_url_with_name(database_url: str, database_name: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment))


def _integration_database_name() -> str:
    return f"nhms_it_{uuid.uuid4().hex}"


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
