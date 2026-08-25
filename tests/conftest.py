from __future__ import annotations

import gc
import os
import pathlib
import sys
import uuid
from collections.abc import Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2 import sql

from apps.api.display_cache import clear_display_catalog_cache, stop_display_catalog_warmer

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def build_canonical_nc(
    tmp_path: pathlib.Path,
    *,
    source: str,
    cycle_iso: str,
    variable: str,
    latitudes: Sequence[float],
    longitudes: Sequence[float],
) -> pathlib.Path:
    """Write a small ``NETCDF4`` fixture matching the canonical rectilinear shape.

    Layout: dims ``(latitude, longitude)``, coord vars ``latitude`` /
    ``longitude`` matching SUB-4 ``_build_cells`` expectations, one data var
    named after ``variable`` filled with deterministic zero values. The output
    path is
    ``tmp_path/{source}/{cycle_iso}/{variable}.nc`` — parents are created on
    demand.

    Used by :file:`tests/test_grid_stability_verification.py` (SUB-7 / #904)
    to author per-cycle / per-variable / per-backend fixtures under
    ``tmp_path`` without committing any NetCDF binaries to the repo.
    """
    import xarray as xr

    lat_list = [float(value) for value in latitudes]
    lon_list = [float(value) for value in longitudes]
    y_count, x_count = len(lat_list), len(lon_list)
    zeros = [[0.0] * x_count for _ in range(y_count)]
    dataset = xr.Dataset(
        data_vars={variable: (("latitude", "longitude"), zeros)},
        coords={
            "latitude": ("latitude", lat_list),
            "longitude": ("longitude", lon_list),
        },
    )
    target = tmp_path / source / cycle_iso / f"{variable}.nc"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        dataset.to_netcdf(target, engine="netcdf4", format="NETCDF4")
    finally:
        dataset.close()
    return target


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: tests that require explicitly configured external services")
    config.addinivalue_line("markers", "e2e: end-to-end pipeline tests; opt-in via NHMS_RUN_E2E=1 (node-27 oracle)")
    config.addinivalue_line("markers", "real_disk: tests that require node-27 DATABASE_URL and OBJECT_STORE_ROOT")
    config.addinivalue_line("markers", "grib: real GRIB2 decode tests; opt-in via NHMS_RUN_GRIB=1 (node-27 oracle)")
    config.addinivalue_line(
        "markers",
        "timescaledb_210: compression-semantics tests whose only valid oracle is a node-27 "
        "throwaway DB (TimescaleDB 2.10.2); CI's pg15-latest does not prove them",
    )


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


def pytest_unconfigure(config: pytest.Config) -> None:
    """Release the selector meta-guard suite's parse cache before shutdown.

    ``tests/test_select_ci_tests.py`` memoizes its AST parses; carrying those
    trees into CPython's finalization costs far more than dropping them here —
    measured on the run lane, 8.4 s of pure exit time (unconfigure at 12.96 s,
    atexit at 21.38 s) against 0.11-0.23 s for the clear below.

    This must be a hook, not a fixture in that module: ``--collect-only`` runs
    no fixtures yet still caches the ~188 trees collection touches, and an
    ``atexit`` callback fires too late — ~4.5 s of the finalization happens
    before ``atexit``, so it recovers only ~2 s of it.
    """
    del config
    module = sys.modules.get("tests.test_select_ci_tests")
    if module is not None:
        module._PARSE_CACHE.clear()
        gc.collect()


@pytest.fixture(autouse=True)
def _stop_display_catalog_warmer() -> Iterator[None]:
    """每个用例后停掉 display-catalog 预热线程，禁止它活到下一个用例。

    任何构造出 display_readonly app 的用例都会作为副作用启动名为
    ``display-catalog-warmer`` 的 daemon 线程；它消费进程全局的
    `time.monotonic`，会污染后续 monkeypatch 了全局时钟的用例。什么都没启动
    时代价是一次 Event 检查 + 一次 dict 清空。

    stop 失败（join 超时）会响在**当前**用例上，且在那个线程退出前每个后续
    teardown 都会继续响——这是设计上的级联，不是 stop 坏了。
    """
    yield
    stopped = stop_display_catalog_warmer()
    assert stopped, (
        "display-catalog-warmer thread did not stop within the join timeout; "
        "it is likely stuck inside a display_cache._replay_targets ASGI replay "
        "(httpx per-path timeout is 120s). Every subsequent test teardown will "
        "keep failing until that thread exits — investigate the thread, not "
        "stop_display_catalog_warmer()."
    )
    clear_display_catalog_cache()


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


@pytest.fixture()
def throwaway_database_url() -> Iterator[str]:
    """Per-TEST throwaway database, created and dropped around each test.

    ``integration_database_url`` is session-scoped, which is right for tests
    that only read or that seed disjoint rows. It is wrong for tests that
    mutate SCHEMA (issue #1339's cutover swaps the primary key, drops a foreign
    key and rewrites the compression settings) or that re-seed the same
    identities: those leak into every later test in the session and produce
    failures that look like product bugs.

    Same credentials, same server, same never-touch-a-live-database guarantee
    as the session fixture — only the lifetime differs.
    """
    skip_reason = _integration_skip_reason()
    if skip_reason:
        pytest.skip(skip_reason)
    base_url = _integration_database_url()
    db_name = _integration_database_name()
    admin_url = _database_url_with_name(base_url, "postgres")
    _create_database(admin_url, db_name)
    try:
        yield _database_url_with_name(base_url, db_name)
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
        f"run on the node-27 oracle (outside production windows) — see "
        f"docs/runbooks/ci-test-routing.md"
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
