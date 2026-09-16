"""Real-PostgreSQL regression for the readonly-DB reachable-roles probe.

The reachable-roles statement in ``PsycopgReadonlyDbProbeAdapter._reachable_roles``
is only covered elsewhere by a fake cursor that records the SQL text and asserts on
substrings (``tests/test_readonly_db_validation_probes.py``). That double never hands
the statement to a PostgreSQL parser, so a reserved-word relation alias such as
``current_role`` passed every unit test while making the canonical deny-write
entrypoint fail on every live server (issue #2409).

These tests hand the statement to a real server. They deliberately do not catch
``psycopg2.Error``: the adapter does not swallow it either, so a parse failure must
surface as the test error with its original class and message.
"""

from __future__ import annotations

import pytest

from services.production_closure.readonly_db_probe_adapter import PsycopgReadonlyDbProbeAdapter

pytestmark = pytest.mark.integration


def test_reachable_role_privileges_executes_on_a_real_postgres(
    integration_database_url: str,
) -> None:
    """The public seam runs the probe end to end against a live server."""

    adapter = PsycopgReadonlyDbProbeAdapter(integration_database_url, ddl_suffix="issue-2409-reachable-roles")

    findings = adapter.reachable_role_privileges((), ())

    # The set itself is not asserted: the throwaway integration role legitimately has
    # no role memberships, while an operator role on a real cluster may have some.
    # What is under test is that the statement parses and executes at all.
    assert isinstance(findings, list)


def test_reachable_roles_statement_parses_for_every_membership_column_branch(
    integration_database_url: str,
) -> None:
    """Both f-string branches of the statement are parsed by a real server.

    ``_reachable_roles`` folds ``set_option`` / ``inherit_option`` into the statement
    only when the server's ``pg_auth_members`` actually has those columns (PG 16+).
    The branch that this server supports is taken from its own catalog; the constant
    fallback branch is valid on every supported version.
    """

    adapter = PsycopgReadonlyDbProbeAdapter(integration_database_url, ddl_suffix="issue-2409-reachable-roles")

    with adapter._connection() as connection:
        with connection.cursor() as cursor:
            detected_columns = adapter._pg_auth_members_columns(cursor)

            server_branch = adapter._reachable_roles(cursor, membership_columns=detected_columns)
            constant_branch = adapter._reachable_roles(cursor, membership_columns=set())

    assert isinstance(server_branch, list)
    assert isinstance(constant_branch, list)
