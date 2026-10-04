"""#2716 on a real PostgreSQL: the DB tile-cache write-privilege probe.

``tests/test_mvt_db_cache_write_privilege.py`` scripts the probe's answer on a
session double, so it proves what the write path does WITH a verdict but only
reads ``mvt._DB_TILE_CACHE_WRITABLE_SQL`` as text. Here the exact module
constant is executed by real login roles against real catalogs, which is the
only place two of its claims can be checked:

* VERDICT. Each privilege / catalog state yields the documented boolean -- a
  real ``bool``, not NULL.
* NO PG ERROR. No state makes the statement raise. That is the point of the
  probe (it replaces a rejected ``INSERT`` per cold miss), and a role without
  USAGE on schema ``map`` is the state where a naive ``to_regclass`` would.

Plus the production mode end to end: ``build_raw_tile_response`` on an engine
logged in as a SELECT-only role sends one probe, no cache-table write and
nothing PostgreSQL rejects; a writable role is the control showing the same
assertions do see a write.

Roles are cluster-global, so each test creates uniquely named ones and drops
them (after ``DROP OWNED``) before its throwaway database goes away.

Run against a disposable database (never production), e.g. a local
``timescale/timescaledb-ha:pg15-latest`` container:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... \\
        uv run pytest -q -rs tests/test_mvt_db_cache_write_probe_integration.py

SILENT-SKIP TRAP: without both variables every case skips; read the passed count.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

import services.tiles.mvt as mvt
from packages.common.sqlalchemy_url import sqlalchemy_url
from tests.integration_helpers import apply_migrations_from_zero

pytestmark = pytest.mark.integration

_CACHE_TABLE_WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE)\s+map\.tile_(layer|cache)\b", re.IGNORECASE)
_USAGE = "GRANT USAGE ON SCHEMA map TO {role}"

#: (case id, grants, expected verdict) on the migrated catalog (both tables present).
PRIVILEGE_CASES: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    # The production display role.
    ("select_only", (_USAGE, "GRANT SELECT ON map.tile_cache, map.tile_layer TO {role}"), False),
    # The write is INSERT ... ON CONFLICT DO UPDATE: INSERT alone is not enough.
    ("insert_only", (_USAGE, "GRANT SELECT, INSERT ON map.tile_cache, map.tile_layer TO {role}"), False),
    ("insert_update", (_USAGE, "GRANT INSERT, UPDATE ON map.tile_cache, map.tile_layer TO {role}"), True),
    (
        "cache_rw_layer_ro",
        (_USAGE, "GRANT INSERT, UPDATE ON map.tile_cache TO {role}", "GRANT SELECT ON map.tile_layer TO {role}"),
        False,
    ),
    (
        "layer_rw_cache_ro",
        (_USAGE, "GRANT INSERT, UPDATE ON map.tile_layer TO {role}", "GRANT SELECT ON map.tile_cache TO {role}"),
        False,
    ),
    # Table privileges without schema USAGE: unusable, and the state in which
    # `to_regclass('map....')` itself raises "permission denied for schema map".
    ("no_schema_usage", ("GRANT INSERT, UPDATE ON map.tile_cache, map.tile_layer TO {role}",), False),
)


class _Roles:
    """Login roles for one throwaway database, and engines logged in as them."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._prefix = f"it2716_{uuid.uuid4().hex[:8]}"
        self._passwords: dict[str, str] = {}
        self._engines: list[Engine] = []

    def admin(
        self, statement: str, params: tuple[Any, ...] | None = None, *, role: str | None = None
    ) -> list[tuple[Any, ...]]:
        """Run one statement as the harness superuser; ``{role}`` is quoted in."""
        connection = psycopg2.connect(self._database_url)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                query = sql.SQL(statement)
                cursor.execute(query.format(role=sql.Identifier(role)) if role is not None else query, params)
                return cursor.fetchall() if cursor.description is not None else []
        finally:
            connection.close()

    def create(self, case_id: str, grants: tuple[str, ...] = ()) -> str:
        role = f"{self._prefix}_{case_id}"
        password = uuid.uuid4().hex
        # Registered before CREATE so a half-made role is still dropped.
        self._passwords[role] = password
        self.admin(
            "CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s",
            (password,),
            role=role,
        )
        for grant in grants:
            self.admin(grant, role=role)
        return role

    def engine(self, role: str) -> Engine:
        url = make_url(sqlalchemy_url(self._database_url)).set(username=role, password=self._passwords[role])
        engine = create_engine(url, future=True)
        self._engines.append(engine)
        return engine

    def drop_all(self) -> None:
        for engine in self._engines:
            engine.dispose()
        for role in self._passwords:
            # Grants live in this database; DROP ROLE refuses while they exist.
            if self.admin("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
                self.admin("DROP OWNED BY {role}", role=role)
                self.admin("DROP ROLE {role}", role=role)


@pytest.fixture()
def roles(throwaway_database_url: str) -> Iterator[_Roles]:
    # Depends on the database fixture, so this teardown runs before the drop.
    harness = _Roles(throwaway_database_url)
    try:
        yield harness
    finally:
        harness.drop_all()


@pytest.fixture(autouse=True)
def _fresh_probe_verdicts() -> Iterator[None]:
    mvt.reset_db_tile_cache_write_probe()
    yield
    mvt.reset_db_tile_cache_write_probe()


def _probe(engine: Engine) -> Any:
    """The module constant, executed the way ``_probe_db_tile_cache_writable`` does."""
    with Session(engine) as session:
        row = session.execute(text(mvt._DB_TILE_CACHE_WRITABLE_SQL)).mappings().first()
    assert row is not None
    return row["writable"]


def _check(failures: list[str], case_id: str, engine: Engine, expected: bool) -> None:
    try:
        verdict = _probe(engine)
    except Exception as error:  # noqa: BLE001 - "never raises" is the claim under test
        failures.append(f"{case_id}: the probe raised {type(error).__name__}: {error}")
        return
    if verdict is not expected:
        failures.append(f"{case_id}: the probe returned {verdict!r}, expected {expected!r}")


def test_probe_verdict_per_privilege_state(throwaway_database_url: str, roles: _Roles) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    failures: list[str] = []
    for case_id, grants, expected in PRIVILEGE_CASES:
        role = roles.create(case_id, grants)
        ((has_usage,),) = roles.admin("SELECT has_schema_privilege(%s, 'map', 'USAGE')", (role,))
        # Guards the seed: schema `map` must not hand USAGE to PUBLIC.
        assert has_usage is (_USAGE in grants), case_id
        _check(failures, case_id, roles.engine(role), expected)
    assert not failures, "\n".join(failures)


def test_probe_verdict_per_catalog_state(roles: _Roles) -> None:
    """Missing schema / tables: built up by hand so each state is exactly what it says."""
    assert roles.admin("SELECT to_regnamespace('map') IS NULL") == [(True,)]
    role = roles.create("catalog")
    engine = roles.engine(role)
    failures: list[str] = []

    _check(failures, "schema map absent", engine, False)

    roles.admin("CREATE SCHEMA map")
    roles.admin(_USAGE, role=role)
    _check(failures, "schema map present, no tables", engine, False)

    roles.admin("CREATE TABLE map.tile_layer (layer_id TEXT PRIMARY KEY)")
    roles.admin("GRANT INSERT, UPDATE ON map.tile_layer TO {role}", role=role)
    _check(failures, "map.tile_cache absent, map.tile_layer writable", engine, False)

    roles.admin("DROP TABLE map.tile_layer")
    roles.admin("CREATE TABLE map.tile_cache (cache_key TEXT PRIMARY KEY)")
    roles.admin("GRANT SELECT ON map.tile_cache TO {role}", role=role)
    _check(failures, "only map.tile_cache present, read-only", engine, False)

    roles.admin("GRANT INSERT, UPDATE ON map.tile_cache TO {role}", role=role)
    _check(failures, "only map.tile_cache present, writable", engine, True)

    assert not failures, "\n".join(failures)


def _tile(x: int) -> mvt.TileInput:
    return mvt.TileInput(
        layer_id="river-network",
        source_id="basins_demo_v1",
        source_version="generation-a",
        valid_time=None,
        z=6,
        x=x,
        y=20,
    )


def _serve_misses(engine: Engine, xs: tuple[int, ...]) -> tuple[list[mvt.TileResponse], list[str], list[str]]:
    """One session per tile (the per-request shape); returns responses, statements, DB errors."""
    statements: list[str] = []
    errors: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    def record_error(context: Any) -> None:
        errors.append(str(context.original_exception))

    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "handle_error", record_error)
    responses = []
    for x in xs:
        with Session(engine) as session:
            responses.append(mvt.build_raw_tile_response(session, _tile(x), f"pbf-{x}".encode()))
    return responses, statements, errors


def test_read_only_role_serves_misses_from_the_file_cache_without_a_rejected_write(
    throwaway_database_url: str, roles: _Roles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    role = roles.create("display_ro", PRIVILEGE_CASES[0][1])

    responses, statements, errors = _serve_misses(roles.engine(role), (50, 51, 52))

    assert [response.cache_status for response in responses] == ["miss", "miss", "miss"]
    # Asked once for the engine, not once per miss (and not once per session).
    assert len([statement for statement in statements if "has_table_privilege" in statement]) == 1
    assert [statement for statement in statements if _CACHE_TABLE_WRITE.search(statement)] == []
    # Nothing this engine sent was rejected: no ERROR line in the PostgreSQL log.
    assert errors == []
    assert roles.admin("SELECT count(*) FROM map.tile_cache") == [(0,)]
    assert roles.admin("SELECT count(*) FROM map.tile_layer WHERE layer_id = 'river-network'") == [(0,)]
    assert sorted(path.read_bytes() for path in tmp_path.rglob("*.pbf")) == [b"pbf-50", b"pbf-51", b"pbf-52"]


def test_writable_role_still_writes_the_db_cache(
    throwaway_database_url: str, roles: _Roles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: the same observations do see a DB write."""
    apply_migrations_from_zero(throwaway_database_url)
    monkeypatch.setenv(mvt.MVT_FILE_CACHE_DIR_ENV, str(tmp_path))
    role = roles.create(
        "display_rw", (_USAGE, "GRANT SELECT, INSERT, UPDATE ON map.tile_cache, map.tile_layer TO {role}")
    )

    responses, statements, errors = _serve_misses(roles.engine(role), (50, 51))

    assert [response.cache_status for response in responses] == ["miss", "miss"]
    assert len([statement for statement in statements if "has_table_privilege" in statement]) == 1
    assert errors == []
    assert roles.admin("SELECT count(*) FROM map.tile_cache") == [(2,)]
    assert roles.admin("SELECT count(*) FROM map.tile_layer WHERE layer_id = 'river-network'") == [(1,)]
    # The DB write succeeded, so the file cache was never the fallback.
    assert list(tmp_path.rglob("*.pbf")) == []
