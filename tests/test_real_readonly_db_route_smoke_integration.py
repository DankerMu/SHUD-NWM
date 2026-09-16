"""Real-PostgreSQL regression for the readonly-DB validation connection URL.

``_bounded_database_url`` rebuilds the display DSN with the lane's bounded libpq
``options``. Every other check on it is a substring assertion over the emitted URL, and
every application route consumes that URL through SQLAlchemy, whose ``make_url`` decodes
query values with ``unquote_plus``. Both of those turn ``+`` back into a space, so a
form-encoded options value passed the whole suite while libpq — which percent-decodes
only — refused the connection outright on every live server (issue #2413)::

    FATAL:  unrecognized configuration parameter "+statement_timeout"

This test hands the rebuilt URL straight to ``psycopg2.connect``, the libpq-direct
consumer, and reads the resulting GUCs back from the server. It deliberately does not
catch ``psycopg2.Error``: the lane does not swallow it either, so a refused connection
must surface as the test error with its original class and message.
"""

from __future__ import annotations

import psycopg2
import pytest

from services.production_closure.readonly_db_route_smoke import _bounded_database_url
from services.production_closure.readonly_db_types import (
    VALIDATION_IDLE_TIMEOUT_MS,
    VALIDATION_LOCK_TIMEOUT_MS,
    VALIDATION_STATEMENT_TIMEOUT_MS,
)

pytestmark = pytest.mark.integration

BOUNDED_TIMEOUT_SETTINGS = (
    ("statement_timeout", VALIDATION_STATEMENT_TIMEOUT_MS),
    ("lock_timeout", VALIDATION_LOCK_TIMEOUT_MS),
    ("idle_in_transaction_session_timeout", VALIDATION_IDLE_TIMEOUT_MS),
)


def test_bounded_url_opens_a_libpq_connection_carrying_the_configured_timeouts(
    integration_database_url: str,
) -> None:
    """The bounded URL connects through libpq and applies all three lane timeouts."""

    connection = psycopg2.connect(_bounded_database_url(integration_database_url))
    try:
        with connection.cursor() as cursor:
            for name, expected_ms in BOUNDED_TIMEOUT_SETTINGS:
                # ``pg_settings.setting`` carries the GUC's base unit, which is ``ms``
                # for all three, so it compares directly against the constants. ``SHOW``
                # would render ``'10s'`` / ``'2s'`` and fail on the *fixed* code.
                cursor.execute("SELECT setting FROM pg_settings WHERE name = %s", (name,))
                row = cursor.fetchone()
                assert row is not None, f"{name} is absent from pg_settings"
                assert int(row[0]) == expected_ms, f"{name} is {row[0]}ms, expected {expected_ms}ms"
    finally:
        connection.close()
