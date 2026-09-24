"""Registry imports lock the parent basin_version row first (#2491).

``bootstrap-qhh-production`` opens with ``_lock_qhh_basin_scope`` (advisory
lock + ``core.basin_version ... FOR UPDATE``) and only then reaches
``import_basin_into_registry_core``, whose
``_refresh_parent_version_materialization`` ``UPDATE``s
``core.river_network_version``: basin_version -> river_network_version. The
generic import entered the same function with no basin_version lock, so its
first parent row lock was that rnv ``UPDATE`` and its second the
``UPDATE core.basin_version`` right after it: river_network_version ->
basin_version. For an existing basin the two formed an ABBA pair and one
session died with ``deadlock detected``. After #2491 the function takes
``FOR NO KEY UPDATE`` on the basin_version row before anything else, so the
generic import queues behind the bootstrap holding nothing.

Session A is the bootstrap's parent-lock prefix: the REAL
``_lock_qhh_basin_scope`` (advisory lock + ``FOR UPDATE``), then the rnv
``UPDATE`` the bootstrap's own ``import_basin_into_registry_core`` call issues
first; session B is the REAL
``import_basin_into_registry_core`` re-importing a basin the REAL generic import
seeded, so B completes as ``already_imported`` rather than tripping
``CHECKSUM_CONFLICT``. Same bounded framework as
``tests/test_river_segment_lock_order_integration.py``: ``PGOPTIONS`` sets a
``lock_timeout`` far above the server's ``deadlock_timeout``, every wait polls
``pg_stat_activity`` with a deadline, every join is bounded and the ``finally``
blocks cancel the background backend. The module imports only symbols that
exist before the fix, so it runs unchanged against the pre-change source for
the red leg.

File name carries ``integration`` so ``.github/workflows/ci.yml``'s ``database``
filter opens the real-DB lane.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from psycopg2.extras import RealDictCursor

from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection
from workers.model_registry.basins_registry_import import (
    import_basin_into_registry_core,
    import_basins_registry,
    prepare_basins_import_sources,
)

pytestmark = pytest.mark.integration

# Far above the server's deadlock_timeout (1 s default), so a real deadlock is
# reported as `deadlock detected`, never masked as a lock timeout.
_PGOPTIONS = "-c lock_timeout=20000 -c statement_timeout=60000"
_POLL_SECONDS = 15.0
_JOIN_SECONDS = 45.0


class _Background(threading.Thread):
    """Run one session's work off the main thread; keep its result or its error."""

    def __init__(self, work: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._work = work
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._work()
        except BaseException as error:  # noqa: BLE001 - surfaced by the test body
            self.error = error


def _connect(database_url: str, *, autocommit: bool = False) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = autocommit
    return connection


def _pid(connection: Any) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid() AS pid")
        pid = int(cursor.fetchone()["pid"])
    if not connection.autocommit:
        connection.commit()
    return pid


def _is_lock_waiting(observer: Any, pid: int) -> bool:
    with observer.cursor() as cursor:
        cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,))
        row = cursor.fetchone()
    return row is not None and row["wait_event_type"] == "Lock"


def _wait_for_lock_wait(observer: Any, background: _Background, pid: int) -> None:
    deadline = time.monotonic() + _POLL_SECONDS
    while time.monotonic() < deadline:
        if not background.is_alive():
            raise AssertionError(
                f"session B finished before it waited on a lock: result={background.result!r} "
                f"error={background.error!r}"
            )
        if _is_lock_waiting(observer, pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"session B never waited on a lock within {_POLL_SECONDS}s")


def _stop(observer: Any, background: _Background, pid: int) -> None:
    """Never leave a blocked backend or a live thread behind a failed test."""
    if background.is_alive():
        with observer.cursor() as cursor:
            cursor.execute("SELECT pg_cancel_backend(%s)", (pid,))
    background.join(timeout=_JOIN_SECONDS)
    assert not background.is_alive(), "session B did not stop"


def _registry_fixture(tmp_path: Path, basin_slug: str) -> tuple[Path, Path]:
    """Inventory + package manifest of one valid Basins model, as the registry suites build it.

    Imported in the function body on purpose: the #1913 registry-partition oracle
    freezes the set of suites that import the helper at module scope
    (tests/test_select_ci_tests.py), and a body import does not make this file
    one of them.
    """
    from tests.basins_registry_import_helpers import _write_registry_fixture

    _, _, inventory_path, manifest_path, _model_id = _write_registry_fixture(tmp_path, basin_slug=basin_slug)
    return inventory_path, manifest_path


def _parent_rows(database_url: str, basin_version_id: str, river_network_version_id: str) -> dict[str, Any]:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT bv.source_uri AS bv_source_uri, bv.checksum AS bv_checksum,
                   rnv.segment_count, rnv.source_uri AS rnv_source_uri, rnv.checksum AS rnv_checksum
            FROM core.basin_version bv
            JOIN core.river_network_version rnv ON rnv.basin_version_id = bv.basin_version_id
            WHERE bv.basin_version_id = %s AND rnv.river_network_version_id = %s
            """,
            (basin_version_id, river_network_version_id),
        )
        return dict(cursor.fetchone())


def test_generic_reimport_queues_behind_a_bootstrap_holding_the_basin_version(
    throwaway_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A holds the basin_version scope, B re-imports, A then UPDATEs the network.

    Before #2491 B's first parent write was its rnv ``UPDATE``, so B blocked on
    the basin_version row while holding the network row and A's rnv ``UPDATE``
    closed the cycle: `deadlock detected`. After it, B blocks on the
    basin_version row before touching any parent, so A's rnv ``UPDATE`` returns
    while B is still waiting (the discriminant: "B waits" alone is true of both
    builds), A commits, and B's re-import then completes with zero new rows.
    """
    # Body import, same reason as `_registry_fixture`: keep this module out of
    # the selector's module-scope import closures. Exists before #2491.
    from workers.model_registry.qhh_bootstrap_registry import _lock_qhh_basin_scope

    url = throwaway_database_url
    apply_migrations_from_zero(url)
    inventory_path, manifest_path = _registry_fixture(tmp_path, "basin-2491")
    seeded = import_basins_registry(
        inventory_path=inventory_path,
        package_manifest_path=manifest_path,
        database_url=url,
        trusted_internal=True,
    )
    assert seeded["status"] == "imported", seeded
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    basin_version_id = sources.ids["basin_version_id"]
    network_id = sources.ids["river_network_version_id"]
    parents_before = _parent_rows(url, basin_version_id, network_id)

    monkeypatch.setenv("PGOPTIONS", _PGOPTIONS)
    observer = _connect(url, autocommit=True)
    session_a = _connect(url)
    session_b = _connect(url)
    try:
        b_pid = _pid(session_b)

        def reimport_in_b() -> dict[str, int]:
            with session_b.cursor() as cursor:
                row_counts = import_basin_into_registry_core(cursor, sources)
            session_b.commit()
            return row_counts

        background = _Background(reimport_in_b)
        try:
            with session_a.cursor() as cursor_a:
                # (1) A: the bootstrap's real basin-version scope lock.
                _lock_qhh_basin_scope(cursor_a, basin_version_id)
                assert cursor_a.fetchone() is not None, "the scope lock must have found (and locked) the row"
                # (2) B: the generic re-import, until it waits on a lock.
                background.start()
                _wait_for_lock_wait(observer, background, b_pid)
                # (3) A: the rnv UPDATE its own delegated import issues first.
                try:
                    cursor_a.execute(
                        "UPDATE core.river_network_version SET segment_count = segment_count "
                        "WHERE river_network_version_id = %s",
                        (network_id,),
                    )
                except psycopg2.Error as error:
                    raise AssertionError(
                        f"session A's rnv UPDATE failed while B waited (pgcode={error.pgcode}): {error}"
                    ) from error
                assert background.error is None, f"session B failed while A held the scope: {background.error}"
                assert background.is_alive() and _is_lock_waiting(observer, b_pid), (
                    "A's rnv UPDATE returned but B is no longer waiting on the basin_version row"
                )
            # (4) A commits; B proceeds.
            session_a.commit()
            background.join(timeout=_JOIN_SECONDS)
        finally:
            _stop(observer, background, b_pid)
    finally:
        for connection in (session_a, session_b, observer):
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            connection.close()

    if isinstance(background.error, psycopg2.Error):
        raise AssertionError(
            f"session B failed after A committed (pgcode={background.error.pgcode}): {background.error}"
        )
    assert background.error is None, f"session B failed after A committed: {background.error}"
    assert set(background.result.values()) == {0}, background.result
    assert _parent_rows(url, basin_version_id, network_id) == parents_before


def test_first_import_takes_no_basin_version_lock_and_imports(
    throwaway_database_url: str,
    tmp_path: Path,
) -> None:
    """A basin_version row that does not exist yet locks nothing; the import proceeds."""
    url = throwaway_database_url
    apply_migrations_from_zero(url)
    inventory_path, manifest_path = _registry_fixture(tmp_path, "basin-2491-new")
    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)
    connection = _connect(url)
    try:
        with connection.cursor() as cursor:
            row_counts = import_basin_into_registry_core(cursor, sources)
        connection.commit()
    finally:
        connection.close()
    assert row_counts["basin"] == 1
    assert row_counts["basin_version"] == 1
    assert row_counts["model_instance"] == 1
