"""packages/common/sqlalchemy_url.py (#2632, design D1).

Every PostgreSQL engine names psycopg2 explicitly, so SQLAlchemy 2.1's switch of
the plain ``postgresql://`` default to psycopg (v3) cannot change the driver.
Engines connect lazily, so ``create_engine`` here needs no database: it only
resolves the dialect and imports its DBAPI.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError, NoSuchModuleError

from packages.common.sqlalchemy_url import sqlalchemy_url

PLAIN = "postgresql://nhms:pw@127.0.0.1:55432/nhms"


def test_plain_postgresql_url_names_psycopg2() -> None:
    url = sqlalchemy_url(PLAIN)

    assert isinstance(url, URL)
    assert url.drivername == "postgresql+psycopg2"
    assert url.render_as_string(hide_password=False) == "postgresql+psycopg2://nhms:pw@127.0.0.1:55432/nhms"


def test_plain_postgresql_engine_uses_the_psycopg2_dbapi() -> None:
    engine = create_engine(sqlalchemy_url(PLAIN), future=True)
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg2"
        assert engine.dialect.dbapi.__name__ == "psycopg2"
    finally:
        engine.dispose()


def test_url_object_input_is_normalised_the_same_way() -> None:
    url = sqlalchemy_url(URL.create("postgresql", username="u", host="h", database="d"))

    assert url.drivername == "postgresql+psycopg2"
    assert (url.username, url.host, url.database) == ("u", "h", "d")


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql+psycopg2://nhms:pw@127.0.0.1:55432/nhms",
        # The caller chose psycopg (v3); the helper does not override that choice.
        "postgresql+psycopg://nhms:pw@127.0.0.1:55432/nhms",
        "sqlite://",
        "sqlite:////tmp/x.sqlite3",
    ],
)
def test_explicit_driver_and_non_postgresql_urls_are_unchanged(raw: str) -> None:
    url = sqlalchemy_url(raw)

    assert url.render_as_string(hide_password=False) == raw
    assert url.drivername == raw.split(":", 1)[0]


def test_postgres_scheme_is_left_alone_and_still_rejected() -> None:
    url = sqlalchemy_url("postgres://nhms:pw@127.0.0.1:55432/nhms")

    assert url.drivername == "postgres"
    # Same rejection create_engine gives the raw string today.
    with pytest.raises(NoSuchModuleError):
        create_engine(url)
    with pytest.raises(NoSuchModuleError):
        create_engine("postgres://nhms:pw@127.0.0.1:55432/nhms")


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        ("not a url", ArgumentError),
        ("postgresql://nhms:secret@[::1/nhms", ValueError),
        ("postgresql://nhms:secret@bad::host/nhms", ValueError),
    ],
)
def test_malformed_url_raises_like_create_engine(bad: str, expected: type[Exception]) -> None:
    """A bad DSN fails synchronously with the class create_engine gives it today
    (scheduler_runtime records only that class name)."""
    with pytest.raises(expected) as direct:
        create_engine(bad)
    with pytest.raises(expected) as normalised:
        sqlalchemy_url(bad)

    assert type(normalised.value) is type(direct.value)


def test_special_character_password_and_query_string_survive() -> None:
    # `@`, `:`, `/`, `%`, `#` and `?` in the password, percent-encoded as libpq
    # and SQLAlchemy both require; the options query carries an encoded space.
    raw = (
        "postgresql://nhms_display_ro:p%40ss%3Aw%2Frd%25x%23y%3Fz@db.example:5432/nhms"
        "?options=-c%20statement_timeout%3D10000&application_name=operator-override"
    )

    url = sqlalchemy_url(raw)

    assert url.drivername == "postgresql+psycopg2"
    assert url.username == "nhms_display_ro"
    assert url.password == "p@ss:w/rd%x#y?z"
    assert (url.host, url.port, url.database) == ("db.example", 5432, "nhms")
    assert url.query == {
        "options": "-c statement_timeout=10000",
        "application_name": "operator-override",
    }
    # The engine hands psycopg2 the decoded credentials and query options.
    engine = create_engine(url)
    try:
        _args, kwargs = engine.dialect.create_connect_args(engine.url)
    finally:
        engine.dispose()
    assert kwargs["password"] == "p@ss:w/rd%x#y?z"
    assert kwargs["options"] == "-c statement_timeout=10000"
