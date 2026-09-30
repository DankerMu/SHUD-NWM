"""Name the PostgreSQL DBAPI explicitly on every SQLAlchemy engine URL (#2632).

SQLAlchemy 2.1 changed the default DBAPI for a plain ``postgresql://`` URL from
psycopg2 to psycopg (v3). The project ships only ``psycopg2-binary`` and its raw
DBAPI code (``psycopg2.connect``, ``RealDictCursor``, ``psycopg2.errors``,
``copy_expert``) is psycopg2-only, so every engine keeps psycopg2 by naming it.

Environment DSNs such as ``DATABASE_URL=postgresql://...`` stay as they are;
the rewrite happens here, at engine creation. ``tests/test_sqlalchemy_driver_explicit.py``
guards that every ``create_engine`` call routes through :func:`sqlalchemy_url`.
"""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url

_PLAIN_POSTGRESQL = "postgresql"
_EXPLICIT_PSYCOPG2 = "postgresql+psycopg2"


def sqlalchemy_url(url: str | URL) -> URL:
    """Return ``url`` as a :class:`URL` whose PostgreSQL driver is explicit.

    Only the bare ``postgresql`` scheme is rewritten, to ``postgresql+psycopg2``.
    A URL that already names a driver (``+psycopg2``, ``+psycopg``) and any
    non-PostgreSQL URL (``sqlite://``) come back unchanged. ``postgres://`` is
    deliberately left alone: SQLAlchemy rejects that scheme, and it keeps doing
    so. Parsing goes through ``make_url``, so a malformed DSN raises exactly as
    ``create_engine`` would, and credentials and query strings are carried on
    the returned object rather than re-serialised into a string.
    """
    parsed = make_url(url)
    if parsed.drivername == _PLAIN_POSTGRESQL:
        return parsed.set(drivername=_EXPLICIT_PSYCOPG2)
    return parsed
