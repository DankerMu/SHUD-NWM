"""node-27 production DB connections carry a component-level application_name.

Issue #1714: every application connection on the node-27 production database
reported an empty ``pg_stat_activity.application_name``, so an operator could
not tell the 10-minute ingest tick from a human psql session -- and cancelled a
production tick by mistake. Each registered component now ships a default
identity via libpq's ``fallback_application_name``.

Three layers are asserted here:

* T3 -- the kwarg actually reaches ``psycopg2.connect`` / ``create_engine`` on
  the real call path, and every OTHER connect parameter is unchanged.
* T4 -- an operator's explicit ``?application_name=`` in the DSN still wins,
  and neither DSN validator's verdict moves.
* T5 -- a static guard over the registered files, so a new unmarked connect
  site or a renamed identity fails instead of silently losing attribution.

Two further layers were added for the display unit:

* T3/T5c (#1728) -- a DEPLOYED-UNIT closure rooted at
  ``apps/api/route_registry.py``: every connect site import-reachable from a
  registered router must name a surface or be pinned ``UNREACHABLE``, plus
  behavioural rows that run each router's real dependency provider.

T5b (delegated connect surfaces / ``DELEGATED_CONNECT_CLOSURE``) and T5c's
function-level closure (#1726) live in
``tests/test_node27_connection_attribution_delegated.py``, which aliases this
module's import-graph walk and verdict vocabulary.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import pytest
from psycopg2.extras import RealDictCursor

from apps.api.routes import (
    best_available,
    data_sources,
    forecast,
    hydro_display,
    models,
    pipeline,
    state_snapshots,
)
from packages.common import display_coverage
from packages.common.best_available import BestAvailableManager
from packages.common.forecast_store import PsycopgForecastStore
from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.object_store_forcing import PsycopgStationLookup
from packages.common.state_manager import StateManager
from scripts import (
    node27_autopipeline,
    node27_cold_residency,
    node27_download_cycles,
    node27_ingest_run,
    node27_raw_retention,
    node27_refresh_coverage,
    node27_timeseries_compression,
    node27_timeseries_retention,
)
from workers.output_parser import parser as output_parser

REPO_ROOT = Path(__file__).resolve().parents[1]

DSN = "postgresql://u:p@127.0.0.1:55432/nhms"
DSN_WITH_OVERRIDE = "postgresql://u:p@127.0.0.1:55432/nhms?application_name=operator-override"

# The registered node-27 production components and their identities. This table
# is the single source of truth for both the behaviour tests and the static
# guard; changing an identity here without changing the code (or vice versa)
# fails T5.
REGISTERED_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("scripts/node27_autopipeline.py", "nhms-autopipe"),
    ("scripts/node27_ingest_run.py", "nhms-ingest-run"),
    ("workers/output_parser/parser.py", "nhms-output-parser"),
    ("scripts/node27_refresh_coverage.py", "nhms-refresh-coverage"),
    ("apps/api/routes/hydro_display.py", "nhms-display-api"),
    ("scripts/node27_timeseries_retention.py", "nhms-ts-retention"),
    ("scripts/node27_timeseries_compression.py", "nhms-ts-compression"),
    ("scripts/node27_raw_retention.py", "nhms-raw-retention"),
    ("scripts/node27_cold_residency.py", "nhms-ts-cold-residency"),
)


class _ConnectIntercepted(RuntimeError):
    """Raised by the fake connect so no test ever needs a live server."""


class _ConnectProbe:
    def __init__(self) -> None:
        self.args: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}
        self.called = False

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.args = args
        self.kwargs = kwargs
        self.called = True
        raise _ConnectIntercepted("intercepted")


def _probe_psycopg2_connect(monkeypatch: pytest.MonkeyPatch) -> _ConnectProbe:
    """Intercept psycopg2.connect on the module object itself.

    Retention / compression / the parser import psycopg2 lazily inside the
    function body, so patching the shared module attribute is the only patch
    point that covers every registered component.
    """
    probe = _ConnectProbe()
    monkeypatch.setattr(psycopg2, "connect", probe)
    return probe


def _invoke_autopipeline(dsn: str, tmp_path: Path) -> None:
    node27_autopipeline._basin_seeded(dsn, "basins_qhh")


def _invoke_ingest_run(dsn: str, tmp_path: Path) -> None:
    run_id = "fcst_gfs_2026062700_basins_qhh_shud"
    manifest_path = node27_ingest_run._manifest_path(tmp_path, run_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"source_id": "gfs"}), encoding="utf-8")
    node27_ingest_run.ingest_run(dsn, tmp_path, run_id)


def _invoke_output_parser(dsn: str, tmp_path: Path) -> None:
    output_parser.PsycopgOutputParserRepository(dsn)._connect()


def _invoke_refresh_coverage(dsn: str, tmp_path: Path) -> None:
    node27_refresh_coverage.main(["--run-id", "fcst_gfs_2026062700_basins_qhh_shud", "--database-url", dsn])


def _invoke_retention_fetch_chunks(dsn: str, tmp_path: Path) -> None:
    node27_timeseries_retention._default_fetch_chunks(SimpleNamespace(database_url=dsn), None)


def _invoke_retention_drop_chunk(dsn: str, tmp_path: Path) -> None:
    node27_timeseries_retention._default_drop_chunk(SimpleNamespace(database_url=dsn, lock_timeout_ms=1000), None)


def _invoke_compression_fetch_chunks(dsn: str, tmp_path: Path) -> None:
    node27_timeseries_compression._default_fetch_chunks(dsn)


def _invoke_compression_compress_chunk(dsn: str, tmp_path: Path) -> None:
    node27_timeseries_compression._default_compress_chunk(dsn, None, compress_timeout_ms=1000)


def _invoke_cold_residency_connect(dsn: str, tmp_path: Path) -> None:
    node27_cold_residency._connect_factory(dsn, node27_cold_residency._DEFAULT_STATEMENT_TIMEOUT_MS)()


# (case id, invoker, expected identity, expected OTHER connect kwargs).
# The fourth element is the invariant lock: introducing the attribution kwarg
# must not have moved cursor_factory / connect_timeout / options on any site.
PSYCOPG2_CASES: tuple[tuple[str, Any, str, dict[str, Any]], ...] = (
    ("autopipeline", _invoke_autopipeline, "nhms-autopipe", {}),
    ("ingest_run", _invoke_ingest_run, "nhms-ingest-run", {"cursor_factory": RealDictCursor}),
    (
        "output_parser",
        _invoke_output_parser,
        "nhms-output-parser",
        {"connect_timeout": 10, "options": "-c statement_timeout=60000"},
    ),
    ("refresh_coverage", _invoke_refresh_coverage, "nhms-refresh-coverage", {}),
    (
        "retention_fetch_chunks",
        _invoke_retention_fetch_chunks,
        "nhms-ts-retention",
        {"cursor_factory": psycopg2.extras.RealDictCursor},
    ),
    ("retention_drop_chunk", _invoke_retention_drop_chunk, "nhms-ts-retention", {}),
    (
        "compression_fetch_chunks",
        _invoke_compression_fetch_chunks,
        "nhms-ts-compression",
        {"connect_timeout": 10, "cursor_factory": psycopg2.extras.RealDictCursor},
    ),
    (
        "compression_compress_chunk",
        _invoke_compression_compress_chunk,
        "nhms-ts-compression",
        {"connect_timeout": 10},
    ),
    (
        "cold_residency_connect",
        _invoke_cold_residency_connect,
        "nhms-ts-cold-residency",
        {"connect_timeout": 10, "cursor_factory": psycopg2.extras.RealDictCursor},
    ),
)


# --------------------------------------------------------------------------- #
# T3 -- the default identity reaches the driver, other parameters unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("case_id", "invoke", "expected_name", "expected_other_kwargs"),
    PSYCOPG2_CASES,
    ids=[case[0] for case in PSYCOPG2_CASES],
)
def test_registered_component_connect_carries_default_application_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    invoke: Any,
    expected_name: str,
    expected_other_kwargs: dict[str, Any],
) -> None:
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        invoke(DSN, tmp_path)

    assert probe.called, f"{case_id} never reached psycopg2.connect"
    assert probe.args == (DSN,)
    assert probe.kwargs.pop("fallback_application_name") == expected_name
    assert probe.kwargs == expected_other_kwargs


@pytest.mark.parametrize(
    ("case_id", "invoke", "expected_name", "expected_other_kwargs"),
    PSYCOPG2_CASES,
    ids=[case[0] for case in PSYCOPG2_CASES],
)
def test_registered_component_conninfo_renders_fallback_application_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    invoke: Any,
    expected_name: str,
    expected_other_kwargs: dict[str, Any],
) -> None:
    """What libpq actually receives, not just what the caller passed."""
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        invoke(DSN, tmp_path)

    conninfo = psycopg2.extensions.make_dsn(
        probe.args[0], fallback_application_name=probe.kwargs["fallback_application_name"]
    )
    parsed = psycopg2.extensions.parse_dsn(conninfo)
    assert parsed["fallback_application_name"] == expected_name
    # No explicit application_name in the DSN -> libpq falls back to ours.
    assert "application_name" not in parsed
    assert parsed["host"] == "127.0.0.1"
    assert parsed["port"] == "55432"
    assert parsed["dbname"] == "nhms"


def test_display_api_engine_adds_identity_and_leaves_pool_parameters_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_create_engine(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "engine"

    monkeypatch.delenv("NHMS_DISPLAY_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", raising=False)
    monkeypatch.setattr(hydro_display, "create_engine", _fake_create_engine)

    # __wrapped__ bypasses the lru_cache so the probe never pollutes it.
    assert hydro_display._engine.__wrapped__(DSN) == "engine"

    assert captured["url"] == DSN
    assert captured["kwargs"] == {
        "future": True,
        "connect_args": {"fallback_application_name": "nhms-display-api"},
        "pool_pre_ping": True,
        "pool_size": 4,
        "max_overflow": 2,
        "pool_timeout": 10,
        "pool_recycle": 1800,
    }


def test_display_api_engine_cache_key_stays_database_url_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity is a constant, so it must not widen the lru_cache key."""
    calls: list[str] = []

    def _fake_create_engine(url: str, **kwargs: Any) -> str:
        calls.append(url)
        return f"engine:{len(calls)}"

    monkeypatch.setattr(hydro_display, "create_engine", _fake_create_engine)
    hydro_display._engine.cache_clear()
    try:
        first = hydro_display._engine(DSN)
        second = hydro_display._engine(DSN)
        other = hydro_display._engine(DSN_WITH_OVERRIDE)
    finally:
        hydro_display._engine.cache_clear()

    assert first is second
    assert other != first
    assert calls == [DSN, DSN_WITH_OVERRIDE]


# --------------------------------------------------------------------------- #
# T4 -- operator override wins; DSN validators unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("case_id", "invoke", "expected_name", "expected_other_kwargs"),
    PSYCOPG2_CASES,
    ids=[case[0] for case in PSYCOPG2_CASES],
)
def test_operator_application_name_in_dsn_beats_component_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    invoke: Any,
    expected_name: str,
    expected_other_kwargs: dict[str, Any],
) -> None:
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        invoke(DSN_WITH_OVERRIDE, tmp_path)

    conninfo = psycopg2.extensions.make_dsn(
        probe.args[0], fallback_application_name=probe.kwargs["fallback_application_name"]
    )
    parsed = psycopg2.extensions.parse_dsn(conninfo)
    # libpq: application_name is final once present; fallback only fills a hole.
    assert parsed["application_name"] == "operator-override"
    assert parsed["fallback_application_name"] == expected_name


@pytest.mark.parametrize(
    "module", [node27_autopipeline, node27_download_cycles], ids=["autopipeline", "download_cycles"]
)
@pytest.mark.parametrize(
    "query",
    ["application_name=x", "fallback_application_name=x", "application_name=x&fallback_application_name=y"],
)
def test_attribution_query_keys_pass_both_dsn_validators(module: Any, query: str) -> None:
    assert "application_name" in module.DATABASE_URL_ALLOWED_QUERY_KEYS
    assert "fallback_application_name" in module.DATABASE_URL_ALLOWED_QUERY_KEYS
    assert module._database_query_blockers(query) == []

    identity, blockers = module._database_preflight(
        f"postgresql://node27_writer:writer-secret@127.0.0.1:55432/nhms?{query}",
        {},
    )
    assert blockers == []
    assert identity["host"] == "127.0.0.1"
    assert identity["port"] == 55432


@pytest.mark.parametrize(
    "module", [node27_autopipeline, node27_download_cycles], ids=["autopipeline", "download_cycles"]
)
def test_query_key_outside_allowlist_still_refused(module: Any) -> None:
    blockers = module._database_query_blockers("host=evil")
    assert [blocker["code"] for blocker in blockers] == [module.DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN]

    _identity, preflight_blockers = module._database_preflight(
        "postgresql://node27_writer:writer-secret@127.0.0.1:55432/nhms?host=evil",
        {},
    )
    assert module.DATABASE_URL_QUERY_OVERRIDE_FORBIDDEN in {b["code"] for b in preflight_blockers}


def test_retention_measure_failure_text_stays_credential_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The new kwarg must not change the redacted failure text (#1213/#1664)."""
    secret_dsn = "postgresql://nwm_writer:top-secret-pw@127.0.0.1:55432/nhms"

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise psycopg2.OperationalError(
            f'connection to server failed: FATAL:  password authentication failed for user "nwm_writer" ({secret_dsn})'
        )

    monkeypatch.setattr(psycopg2, "connect", _boom)
    chunk = SimpleNamespace(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        chunk_schema="_timescaledb_internal",
        chunk_name="_hyper_1_1_chunk",
        qualified_name="_timescaledb_internal._hyper_1_1_chunk",
    )

    result = node27_timeseries_retention._default_measure_chunk_bytes(
        SimpleNamespace(database_url=secret_dsn), [chunk]
    )

    assert result == {"_timescaledb_internal._hyper_1_1_chunk": 0}
    stderr = capsys.readouterr().err
    assert "top-secret-pw" not in stderr
    assert secret_dsn not in stderr
    assert '"nwm_writer"' not in stderr
    assert "freed_bytes measurement failed" in stderr


# --------------------------------------------------------------------------- #
# T3b -- delegated connect surfaces reach the driver attributed too
#
# Round-1 review, both P1s: a registered component that delegates its connect
# into an imported helper was unattributed at runtime while the per-file AST
# guard stayed green. These run the real entrypoint, not the helper in
# isolation, so the wiring itself (main -> helper -> psycopg2.connect) is what
# is pinned.
# --------------------------------------------------------------------------- #
def _run_retention_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str = DSN) -> None:
    """``main()`` with no injected ``now`` -- the production CLI shape."""
    for key, value in {
        "DATABASE_URL": dsn,
        "NODE27_TIMESERIES_RETENTION_ARCHIVE_GATE": "disabled",
        "NODE27_TIMESERIES_RETENTION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_RETENTION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }.items():
        monkeypatch.setenv(key, value)
    # The watermark read is the first DB touch, so the probe aborts the tick.
    assert node27_timeseries_retention.main(argv=[]) != 0


def _run_compression_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str = DSN) -> None:
    """``main()`` with no injected ``now_utc`` -- the production CLI shape."""
    for key, value in {
        "DATABASE_URL": dsn,
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "604800",
        "NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS": "3600000",
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "5",
        "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS": "3900",
        "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS": "7842",
        "NODE27_COLD_RESIDENCY_STATEMENT_TIMEOUT_MS": "3600000",
        "NODE27_COLD_RESIDENCY_WRAPPER_WALL_SECONDS": "3901",
        "NODE27_COLD_RESIDENCY_SYSTEMD_WALL_SECONDS": "7842",
        "NODE27_TIMESERIES_COMPRESSION_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_TIMESERIES_COMPRESSION_LOCK_PATH": str(tmp_path / "runner.lock"),
    }.items():
        monkeypatch.setenv(key, value)
    assert node27_timeseries_compression.main([]) != 0


def _run_cold_residency_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str = DSN) -> None:
    for key, value in {
        "DATABASE_URL": dsn,
        "NODE27_COLD_RESIDENCY_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_COLD_RESIDENCY_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES": "100",
        "NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES": "1",
        "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID": "1005",
        "NODE27_COLD_RESIDENCY_CONTAINER_EXEC_GID": "1005",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        node27_cold_residency,
        "_observe_head",
        lambda *_args, **_kwargs: ("a" * 40, True, False),
    )
    assert node27_cold_residency.main([]) != 0


def _run_raw_retention_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str = DSN) -> None:
    """``main()`` with no ``--reference-time`` -- the production timer shape."""
    root = tmp_path / "object-store"
    (root / "raw").mkdir(parents=True)
    for key, value in {
        "NODE27_DISPLAY_WATERMARK_DATABASE_URL": dsn,
        "NODE27_RAW_RETENTION_OBJECT_STORE_ROOT": str(root),
        "NODE27_RAW_RETENTION_SOURCES": "gfs",
        "NODE27_RAW_RETENTION_DAYS": "14",
    }.items():
        monkeypatch.setenv(key, value)
    assert node27_raw_retention.main([]) != 0


# (case id, entrypoint runner, expected identity).
DELEGATED_WATERMARK_CASES: tuple[tuple[str, Any, str], ...] = (
    ("retention_main_watermark", _run_retention_main, "nhms-ts-retention"),
    ("compression_main_watermark", _run_compression_main, "nhms-ts-compression"),
    ("raw_retention_main_watermark", _run_raw_retention_main, "nhms-raw-retention"),
    ("cold_residency_main_watermark", _run_cold_residency_main, "nhms-ts-cold-residency"),
)


@pytest.mark.parametrize(
    ("case_id", "run_main", "expected_name"),
    DELEGATED_WATERMARK_CASES,
    ids=[case[0] for case in DELEGATED_WATERMARK_CASES],
)
def test_display_watermark_connection_is_attributed_on_every_tick(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_id: str,
    run_main: Any,
    expected_name: str,
) -> None:
    probe = _probe_psycopg2_connect(monkeypatch)

    run_main(monkeypatch, tmp_path)
    capsys.readouterr()

    assert probe.called, f"{case_id} never reached psycopg2.connect"
    assert probe.args == (DSN,)
    assert probe.kwargs.pop("fallback_application_name") == expected_name
    # The helper's own bounded-connect contract must survive the injection:
    # display_watermark.py passes connect_timeout=5 and nothing else.
    assert probe.kwargs == {"connect_timeout": 5}


@pytest.mark.parametrize(
    ("case_id", "run_main", "expected_name"),
    DELEGATED_WATERMARK_CASES,
    ids=[case[0] for case in DELEGATED_WATERMARK_CASES],
)
def test_display_watermark_connection_keeps_the_operator_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_id: str,
    run_main: Any,
    expected_name: str,
) -> None:
    """T4 over the DELEGATED path: the override must survive the extra hop.

    The direct-connect T4 above only covers PSYCOPG2_CASES. Here the identity is
    injected by a wrapper the component hands to ``fetch_display_watermark``, so
    an implementation that stamped ``application_name`` (or rewrote the DSN in
    ``args[0]``) instead of ``fallback_application_name`` would silently outrank
    the operator on exactly the ticks they are trying to label.
    """
    probe = _probe_psycopg2_connect(monkeypatch)

    run_main(monkeypatch, tmp_path, DSN_WITH_OVERRIDE)
    capsys.readouterr()

    assert probe.called, f"{case_id} never reached psycopg2.connect"
    # The wrapper must not touch args[0]: the operator's DSN reaches libpq verbatim.
    assert probe.args == (DSN_WITH_OVERRIDE,)
    assert probe.kwargs == {"fallback_application_name": expected_name, "connect_timeout": 5}

    conninfo = psycopg2.extensions.make_dsn(
        probe.args[0], fallback_application_name=probe.kwargs["fallback_application_name"]
    )
    parsed = psycopg2.extensions.parse_dsn(conninfo)
    assert parsed["application_name"] == "operator-override"
    assert parsed["fallback_application_name"] == expected_name


class _FakeCursor:
    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


class _FakeConnection:
    def cursor(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_refresh_coverage_all_attributes_every_worker_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--all`` runs on EVERY autopipe tick (node27_autopipe_cron.sh:229) and
    fans out up to 8 per-run connections inside ``display_coverage``. Those are
    the long-running backends an operator is most likely to cancel, so each one
    must carry the identity -- not just ``main()``'s own connection.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        calls.append((args, kwargs))
        return _FakeConnection()

    run_ids = [f"run-{index}" for index in range(6)]
    monkeypatch.setattr(psycopg2, "connect", _connect)
    monkeypatch.setattr(node27_refresh_coverage, "run_display_coverage_available", lambda _cursor: True)
    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)
    monkeypatch.setattr(display_coverage, "_refresh", lambda _connection, run_id: [run_id])

    assert node27_refresh_coverage.main(["--all", "--workers", "4", "--database-url", DSN]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["refreshed"] == len(run_ids)

    # main()'s own connection + one per run, all through the ThreadPoolExecutor.
    assert len(calls) == 1 + len(run_ids)
    for args, kwargs in calls:
        assert args == (DSN,)
        assert kwargs == {"fallback_application_name": "nhms-refresh-coverage"}


def test_refresh_coverage_worker_connections_keep_the_operator_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T4 over the fanned-out worker path (see the delegated watermark case).

    ``--all`` is also where an operator most plausibly sets their own
    ``?application_name=`` to tag a manual backfill: every one of the 1+N
    connections must keep that label while still carrying our default underneath.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        calls.append((args, kwargs))
        return _FakeConnection()

    run_ids = [f"run-{index}" for index in range(6)]
    monkeypatch.setattr(psycopg2, "connect", _connect)
    monkeypatch.setattr(node27_refresh_coverage, "run_display_coverage_available", lambda _cursor: True)
    monkeypatch.setattr(display_coverage, "_eligible_run_ids", lambda _connection: run_ids)
    monkeypatch.setattr(display_coverage, "_refresh", lambda _connection, run_id: [run_id])

    assert node27_refresh_coverage.main(["--all", "--workers", "4", "--database-url", DSN_WITH_OVERRIDE]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["refreshed"] == len(run_ids)

    assert len(calls) == 1 + len(run_ids)
    for args, kwargs in calls:
        # The DSN is forwarded verbatim -- neither main() nor the worker fan-out
        # may rewrite it to inject the identity.
        assert args == (DSN_WITH_OVERRIDE,)
        assert kwargs == {"fallback_application_name": "nhms-refresh-coverage"}
        parsed = psycopg2.extensions.parse_dsn(psycopg2.extensions.make_dsn(args[0], **kwargs))
        assert parsed["application_name"] == "operator-override"
        assert parsed["fallback_application_name"] == "nhms-refresh-coverage"


# --------------------------------------------------------------------------- #
# T5 -- static guard over every registered connect surface
# --------------------------------------------------------------------------- #
def _module_application_name(tree: ast.Module) -> str | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_APPLICATION_NAME":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


def _is_psycopg2_connect(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "connect"
        and isinstance(func.value, ast.Name)
        and func.value.id == "psycopg2"
    )


def _is_create_engine(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "create_engine"
    return isinstance(func, ast.Attribute) and func.attr == "create_engine"


def _references_identity_constant(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Name) and value.id == "_APPLICATION_NAME"


def _connect_site_marked(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "fallback_application_name":
            return _references_identity_constant(keyword.value)
    return False


def _create_engine_site_marked(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "connect_args" or not isinstance(keyword.value, ast.Dict):
            continue
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "fallback_application_name"
                and _references_identity_constant(value)
            ):
                return True
    return False


@pytest.mark.parametrize(
    ("relative_path", "expected_name"),
    REGISTERED_COMPONENTS,
    ids=[path for path, _ in REGISTERED_COMPONENTS],
)
def test_registered_file_declares_its_identity_constant(relative_path: str, expected_name: str) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    assert _module_application_name(tree) == expected_name, (
        f"{relative_path} must define a module-level _APPLICATION_NAME == {expected_name!r}"
    )


@pytest.mark.parametrize(
    ("relative_path", "expected_name"),
    REGISTERED_COMPONENTS,
    ids=[path for path, _ in REGISTERED_COMPONENTS],
)
def test_every_connect_site_in_registered_file_is_attributed(relative_path: str, expected_name: str) -> None:
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    sites = 0
    unmarked: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_psycopg2_connect(node.func):
            sites += 1
            if not _connect_site_marked(node):
                unmarked.append(node.lineno)
        elif _is_create_engine(node.func):
            sites += 1
            if not _create_engine_site_marked(node):
                unmarked.append(node.lineno)

    assert sites >= 1, f"{relative_path} has no recognised connect surface; the guard would be vacuous"
    assert unmarked == [], (
        f"{relative_path} lines {unmarked} open a DB connection without "
        f"fallback_application_name=_APPLICATION_NAME ({expected_name!r})"
    )


def test_registered_modules_expose_the_expected_identity_at_runtime() -> None:
    imported = {
        "scripts/node27_autopipeline.py": node27_autopipeline,
        "scripts/node27_ingest_run.py": node27_ingest_run,
        "workers/output_parser/parser.py": output_parser,
        "scripts/node27_refresh_coverage.py": node27_refresh_coverage,
        "apps/api/routes/hydro_display.py": hydro_display,
        "scripts/node27_timeseries_retention.py": node27_timeseries_retention,
        "scripts/node27_timeseries_compression.py": node27_timeseries_compression,
        "scripts/node27_raw_retention.py": node27_raw_retention,
        "scripts/node27_cold_residency.py": node27_cold_residency,
    }
    assert dict(REGISTERED_COMPONENTS).keys() == imported.keys()
    for relative_path, expected_name in REGISTERED_COMPONENTS:
        assert imported[relative_path]._APPLICATION_NAME == expected_name


def test_registered_identities_are_unique_and_fit_the_libpq_bound() -> None:
    names = [name for _path, name in REGISTERED_COMPONENTS]
    assert len(set(names)) == len(names)
    for name in names:
        assert name == name.lower()
        assert len(name.encode()) <= 63


def test_guard_rejects_an_unmarked_connect_site(tmp_path: Path) -> None:
    """Meta-proof: the guard's own predicate turns red on a drifted file."""
    drifted = ast.parse(
        "import psycopg2\n"
        '_APPLICATION_NAME = "nhms-autopipe"\n'
        "def a(u):\n"
        "    return psycopg2.connect(u, fallback_application_name=_APPLICATION_NAME)\n"
        "def b(u):\n"
        "    return psycopg2.connect(u)\n"
    )
    calls = [node for node in ast.walk(drifted) if isinstance(node, ast.Call) and _is_psycopg2_connect(node.func)]
    assert [_connect_site_marked(call) for call in calls] == [True, False]

    renamed = ast.parse('_APPLICATION_NAME = "nhms-typo"\n')
    assert _module_application_name(renamed) != "nhms-autopipe"

    literal = ast.parse('import psycopg2\npsycopg2.connect(u, fallback_application_name="nhms-autopipe")\n')
    literal_call = next(node for node in ast.walk(literal) if isinstance(node, ast.Call))
    assert _connect_site_marked(literal_call) is False


# --------------------------------------------------------------------------- #
# T3/T5c (#1728) -- deployed-unit closure for nhms-display-api.service
#
# The per-file guard above only walks REGISTERED_COMPONENTS, so before #1728 a
# whole process of unattributed modules stayed green: only hydro_display.py was
# registered, while the five other routers of the same uvicorn unit opened
# unnamed backends through the packages/common stores -- including the
# retry/cancel writes an operator most needs to tell apart before cancelling.
#
# This guard is rooted at the unit's ENTRYPOINT REGISTRY instead of at a file
# list: it reads apps/api/route_registry.py, so a router added to
# _BUSINESS_ROUTERS is walked whether or not anyone remembered this test.
#
# Honest limits (same class as the delegated guard's): static import graph
# only, ``unreachable`` verdicts are pinned human judgements rather than
# proofs, and only ``psycopg2.connect`` / ``create_engine`` count as connect
# surfaces. Two more, named by review and NOT covered by either half:
#   * both halves are keyed on the seam the routes use today -- a route that
#     constructs a store directly (``PsycopgForecastStore(dsn)`` instead of
#     ``X.from_env(application_name=...)``) is neither a ``from_env`` call site
#     for the factory half nor a new connect-owning module for the discovery
#     half, so it would open an unnamed backend with this file green;
#   * ``_forwards_injected_application_name`` matches the EXPRESSION at the
#     connect site (``**_attribution_connect_kwargs(<...>.application_name)``).
#     It proves the kwarg is spelled, not that a non-None name reached it: a
#     store whose ``application_name`` attribute is never populated from the
#     injected value still passes. That is what the executed T1/T2 cases at the
#     bottom of this file exist to cover, and they cover only the six stores
#     listed in DISPLAY_UNIT_STORE_CASES.
# --------------------------------------------------------------------------- #
ROUTE_REGISTRY = "apps/api/route_registry.py"
# route_registry.py takes the runtime router as a parameter, so it cannot be
# resolved from that file's imports; apps/api/main.py builds it here.
RUNTIME_ROUTER_SOURCE = "apps/api/startup_wiring.py"
# The helper each shared store uses to forward an injected name to libpq
# (empty kwargs when no name was injected, so nameless callers are unchanged).
ATTRIBUTION_KWARGS_HELPER = "_attribution_connect_kwargs"

# Import-graph vocabulary and walk shared with the delegated guard in
# tests/test_node27_connection_attribution_delegated.py (which imports this
# module, so the shared halves live here).
FIRST_PARTY_ROOTS = ("scripts", "workers", "packages", "apps", "services")

ATTRIBUTED = "attributed"
UNREACHABLE = "unreachable"


# Connection surfaces inside the display unit: one name per router, so
# pg_stat_activity separates the read-only display pool from the control-plane
# writes and from each shared store.
DISPLAY_UNIT_SURFACES: tuple[tuple[str, str], ...] = (
    ("apps/api/routes/forecast.py", "nhms-api-forecast"),
    ("apps/api/routes/data_sources.py", "nhms-api-data-sources"),
    ("apps/api/routes/best_available.py", "nhms-api-best-available"),
    ("apps/api/routes/models.py", "nhms-api-models"),
    ("apps/api/routes/state_snapshots.py", "nhms-api-state-snapshots"),
    ("apps/api/routes/pipeline.py", "nhms-api-pipeline"),
    ("apps/api/routes/hydro_display.py", "nhms-display-api"),
)

# Every module with a connect surface import-reachable from the unit's roots,
# with the same verdict shape the delegated guard uses.
DISPLAY_UNIT_CONNECT_CLOSURE: tuple[tuple[str, str, str], ...] = (
    ("apps/api/routes/hydro_display.py", ATTRIBUTED, "display read-only engine (#1714)"),
    ("apps/api/routes/pipeline.py", ATTRIBUTED, "control-plane retry/cancel engine"),
    ("packages/common/forecast_store.py", ATTRIBUTED, "forecast + data-sources stores and the station lookup"),
    ("packages/common/best_available.py", ATTRIBUTED, "best-available repository"),
    ("packages/common/model_registry.py", ATTRIBUTED, "model registry store"),
    ("packages/common/state_manager.py", ATTRIBUTED, "state-snapshot repository"),
    (
        "packages/common/grid_registry_store.py",
        UNREACHABLE,
        "import-only: models.py -> model_registry -> state_clone -> workers/mapping_builder/rewrite.py -> "
        "algorithm.py imports dataclasses from this module; PsycopgGridRegistryStore is constructed only in "
        "workers/grid_registry/__main__.py, which is not part of the display unit",
    ),
    (
        "packages/common/met_store.py",
        UNREACHABLE,
        "import-only: packages/common/model_registry.py:21 imports workers.forcing_producer.direct_grid_contract, "
        "so workers/forcing_producer/__init__.py:10 executes producer.py, which imports this module at line 31 "
        "(it IS in the unit's runtime sys.modules). PsycopgMetStore.from_env() is called only from worker/CLI "
        "factories -- workers/canonical_converter/converter.py:55, workers/data_adapters/{era5,gfs,ifs}_adapter.py "
        "from_env, workers/forcing_producer/producer.py:487 (ForcingProducer.from_env) and "
        "services/orchestrator/scheduler_adapters.py:413 -- none of which any display-unit route reaches",
    ),
    (
        "services/orchestrator/chain_compat_runtime.py",
        UNREACHABLE,
        "static-only: the single bridge is services/orchestrator/chain.py, imported ONLY inside "
        "services/orchestrator/__init__.py:53 (the module __getattr__ body), so `import apps.api.main` leaves "
        "services.orchestrator.chain out of sys.modules and this module is never executed by the unit",
    ),
    (
        "services/orchestrator/chain_repository.py",
        UNREACHABLE,
        "static-only: reached only through services/orchestrator/chain_compat_runtime.py, itself behind the "
        "deferred chain import in services/orchestrator/__init__.py:53; absent from the unit's runtime sys.modules",
    ),
    (
        "services/tile_publisher/publisher.py",
        UNREACHABLE,
        "static-only: imported by services/orchestrator/chain.py, which is behind the deferred import in "
        "services/orchestrator/__init__.py:53; absent from the unit's runtime sys.modules",
    ),
    (
        "workers/forcing_producer/store.py",
        UNREACHABLE,
        "static-only: imported function-locally at workers/forcing_producer/producer.py:485, inside "
        "ForcingProducer.from_env(), which no display-unit route calls; absent from the unit's runtime sys.modules",
    ),
)

# Store factories called from the unit's route layer. ``attributed`` rows must
# pass application_name=_APPLICATION_NAME; ``unreachable`` rows record why the
# factory opens no DB connection. A new factory call turns the discovery half
# red instead of silently shipping an unnamed backend.
DISPLAY_UNIT_STORE_FACTORIES: tuple[tuple[str, str, str, str], ...] = (
    ("apps/api/routes/forecast.py", "PsycopgForecastStore", ATTRIBUTED, "forecast series/catalog reads"),
    ("apps/api/routes/data_sources.py", "PsycopgForecastStore", ATTRIBUTED, "data-source catalog reads"),
    ("apps/api/routes/data_sources.py", "PsycopgStationLookup", ATTRIBUTED, "met station metadata reads"),
    ("apps/api/routes/best_available.py", "BestAvailableManager", ATTRIBUTED, "facade over the repository"),
    ("apps/api/routes/models.py", "PsycopgModelRegistryStore", ATTRIBUTED, "model registry reads/writes"),
    ("apps/api/routes/state_snapshots.py", "StateManager", ATTRIBUTED, "facade over the repository"),
    (
        "apps/api/routes/pipeline.py",
        "ArtifactReaderConfig",
        UNREACHABLE,
        "artifact log/reader configuration read from the environment; opens no database connection",
    ),
)


def _first_party_imports(path: Path) -> set[str]:
    """Dotted first-party module names imported by ``path`` (any nesting)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = path.parent.relative_to(REPO_ROOT).as_posix().replace("/", ".")
                for _ in range(node.level - 1):
                    package = package.rsplit(".", 1)[0]
                module = f"{package}.{node.module}" if node.module else package
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            # ``from pkg.mod import name`` may name a submodule, not an object.
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in names if name.split(".")[0] in FIRST_PARTY_ROOTS}


def _module_path(dotted: str) -> Path | None:
    module = REPO_ROOT / (dotted.replace(".", "/") + ".py")
    if module.is_file():
        return module
    package = REPO_ROOT / dotted.replace(".", "/") / "__init__.py"
    return package if package.is_file() else None


def _imported_first_party_modules(path: Path) -> set[str]:
    """What importing ``path`` actually pulls in: leaf names AND every ancestor package.

    ``import a.b.c`` executes ``a/__init__.py`` and ``a/b/__init__.py`` before
    ``a/b/c``, so a package ``__init__`` is part of the import graph even when
    nobody names it. Walking only the leaf dotted names hid whole subtrees: the
    unit closure never enqueued ``workers/forcing_producer/__init__.py``, so
    ``packages/common/met_store.py`` -- which that package's ``producer.py``
    imports at module level, and which owns two bare ``psycopg2.connect``
    surfaces -- was invisible while the discovery half stayed green.
    """
    names: set[str] = set()
    for dotted in _first_party_imports(path):
        parts = dotted.split(".")
        names.update(".".join(parts[: index + 1]) for index in range(len(parts)))
    return names


def _owns_connect_surface(path: Path) -> bool:
    """True if the module names ``psycopg2.connect`` / ``create_engine`` at all.

    Deliberately matches bare attribute REFERENCES, not just calls:
    ``display_watermark.py`` assigns ``connect = psycopg2.connect`` and calls it
    later, which a call-only scan misses entirely -- precisely the shape this
    guard exists to catch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (_is_psycopg2_connect(node.func) or _is_create_engine(node.func)):
            return True
        if isinstance(node, ast.Attribute) and _is_psycopg2_connect(node):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "create_engine":
            return True
    return False


def _import_alias_modules(tree: ast.Module) -> dict[str, str]:
    """``from apps.api.routes.x import router as y`` -> {"y": "apps/api/routes/x.py"}."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        source = _module_path(node.module)
        if source is None:
            continue
        relative = source.relative_to(REPO_ROOT).as_posix()
        for alias in node.names:
            aliases[alias.asname or alias.name] = relative
    return aliases


def display_unit_root_modules() -> tuple[str, ...]:
    """Root modules of nhms-display-api.service, read from the route registry.

    Never a hard-coded router count: the tuple in ``route_registry.py`` and the
    ``include_router`` calls beside it are the source of truth, so a newly
    registered router is walked automatically.
    """
    tree = ast.parse((REPO_ROOT / ROUTE_REGISTRY).read_text(encoding="utf-8"))
    aliases = _import_alias_modules(tree)
    router_names: list[str] = []
    # ``for router in _BUSINESS_ROUTERS: api.include_router(router)`` -- the loop
    # variable is already covered by expanding the tuple itself. Only names bound
    # from that exact tuple are skipped, so a loop over anything else stays
    # unresolved and turns this red.
    loop_aliases = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "_BUSINESS_ROUTERS"
    }
    business_routers_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            targets = []
        if any(isinstance(target, ast.Name) and target.id == "_BUSINESS_ROUTERS" for target in targets):
            business_routers_found = True
            value = node.value
            if isinstance(value, ast.Tuple):
                router_names.extend(element.id for element in value.elts if isinstance(element, ast.Name))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            if node.args[0].id not in loop_aliases:
                router_names.append(node.args[0].id)

    roots: list[str] = []
    unresolved: list[str] = []
    for name in router_names:
        if name == "runtime_router":
            resolved = RUNTIME_ROUTER_SOURCE
        else:
            resolved = aliases.get(name, "")
        if not resolved:
            unresolved.append(name)
        elif resolved not in roots:
            roots.append(resolved)
    assert business_routers_found, f"{ROUTE_REGISTRY} no longer declares _BUSINESS_ROUTERS; the guard is blind"
    assert unresolved == [], f"{ROUTE_REGISTRY} registers routers this guard cannot resolve: {unresolved}"
    return tuple(roots)


def _unit_connect_owning_closure() -> set[str]:
    """Modules with a connect surface import-reachable from any unit root."""
    owners: set[str] = set()
    visited: set[Path] = set()
    pending = [REPO_ROOT / root for root in display_unit_root_modules()]
    seen_modules: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        if _owns_connect_surface(current):
            owners.add(current.relative_to(REPO_ROOT).as_posix())
        for dotted in _imported_first_party_modules(current):
            if dotted in seen_modules:
                continue
            seen_modules.add(dotted)
            resolved = _module_path(dotted)
            if resolved is not None:
                pending.append(resolved)
    return owners


def _forwards_injected_application_name(call: ast.Call) -> bool:
    """``**_attribution_connect_kwargs(<...>.application_name)`` at a connect site.

    A shared store may not hard-code a name, so its attribution is the injected
    value forwarded through the module's helper. ``_attribution_connect_kwargs(None)``
    is deliberately NOT accepted.
    """
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        value = keyword.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == ATTRIBUTION_KWARGS_HELPER
            and len(value.args) == 1
        ):
            continue
        argument = value.args[0]
        if isinstance(argument, ast.Attribute) and argument.attr == "application_name":
            return True
        if isinstance(argument, ast.Name) and argument.id == "application_name":
            return True
    return False


def _display_unit_connect_sites(relative_path: str) -> tuple[int, list[int]]:
    """(recognised connect sites, line numbers of the unattributed ones)."""
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    sites = 0
    unattributed: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_psycopg2_connect(node.func):
            sites += 1
            if not (_connect_site_marked(node) or _forwards_injected_application_name(node)):
                unattributed.append(node.lineno)
        elif _is_create_engine(node.func):
            sites += 1
            if not _create_engine_site_marked(node):
                unattributed.append(node.lineno)
    return sites, unattributed


def _from_env_call_sites(relative_path: str) -> set[tuple[str, str]]:
    """(module, factory class) for every ``X.from_env(...)`` call in a module."""
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    sites: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_env"
            and isinstance(node.func.value, ast.Name)
        ):
            sites.add((relative_path, node.func.value.id))
    return sites


def test_display_unit_roots_resolve_from_the_route_registry() -> None:
    """Vacuity guard: every registered router resolves to a real module."""
    roots = display_unit_root_modules()
    assert roots, "the display unit has no resolvable route roots; the closure would be empty"
    for root in roots:
        assert (REPO_ROOT / root).is_file(), f"{root} is registered in {ROUTE_REGISTRY} but does not exist"
    # The two surfaces #1714/#1728 exist to separate must both be in scope.
    assert "apps/api/routes/hydro_display.py" in roots
    assert "apps/api/routes/pipeline.py" in roots
    assert RUNTIME_ROUTER_SOURCE in roots
    # Every router module named in DISPLAY_UNIT_SURFACES is actually registered.
    assert {path for path, _name in DISPLAY_UNIT_SURFACES} <= set(roots)


def test_every_connect_owning_module_in_the_display_unit_is_classified() -> None:
    """Discovery half: nothing may connect inside the unit unclassified."""
    discovered = _unit_connect_owning_closure()
    classified = {module for module, _verdict, _detail in DISPLAY_UNIT_CONNECT_CLOSURE}
    assert discovered == classified, (
        "the set of connect-owning modules import-reachable from "
        f"{ROUTE_REGISTRY} moved. Unclassified: {sorted(discovered - classified)}; "
        f"stale registry rows: {sorted(classified - discovered)}. Add each new module to "
        "DISPLAY_UNIT_CONNECT_CLOSURE as 'attributed' (every connect site names a surface) or "
        "'unreachable' (with the reason no call path in this unit reaches it)."
    )


@pytest.mark.parametrize(
    ("relative_path", "detail"),
    [(module, detail) for module, verdict, detail in DISPLAY_UNIT_CONNECT_CLOSURE if verdict == ATTRIBUTED],
    ids=[module for module, verdict, _d in DISPLAY_UNIT_CONNECT_CLOSURE if verdict == ATTRIBUTED],
)
def test_attributed_display_unit_module_attributes_every_connect_site(relative_path: str, detail: str) -> None:
    """Classification half: no unnamed backend from an attributed module."""
    sites, unattributed = _display_unit_connect_sites(relative_path)
    assert sites >= 1, f"{relative_path} ({detail}) has no connect surface left; the registry row is stale"
    assert unattributed == [], (
        f"{relative_path} lines {unattributed} open a DB connection without naming a surface: a route "
        "module must pass fallback_application_name=_APPLICATION_NAME, a shared store must forward "
        f"**{ATTRIBUTION_KWARGS_HELPER}(<self>.application_name)"
    )


@pytest.mark.parametrize(
    ("relative_path", "detail"),
    [(module, detail) for module, verdict, detail in DISPLAY_UNIT_CONNECT_CLOSURE if verdict == UNREACHABLE],
    ids=[module for module, verdict, _d in DISPLAY_UNIT_CONNECT_CLOSURE if verdict == UNREACHABLE],
)
def test_unreachable_display_unit_module_is_pinned_with_a_reason(relative_path: str, detail: str) -> None:
    """An ``unreachable`` row is a human judgement; it must stay explicit."""
    sites, _unattributed = _display_unit_connect_sites(relative_path)
    assert sites >= 1, f"{relative_path} no longer has a connect surface; drop the stale registry row"
    assert len(detail.split()) >= 5, f"{relative_path} needs a recorded reason, not a placeholder"


def test_every_store_factory_call_in_the_display_unit_route_layer_is_classified() -> None:
    """A store built without a name is unnamed at runtime while the AST is green."""
    discovered: set[tuple[str, str]] = set()
    for root in display_unit_root_modules():
        discovered |= _from_env_call_sites(root)
    classified = {(module, factory) for module, factory, _v, _d in DISPLAY_UNIT_STORE_FACTORIES}
    assert discovered == classified, (
        f"the set of X.from_env(...) calls in the display unit's route layer moved. "
        f"Unclassified: {sorted(discovered - classified)}; stale rows: {sorted(classified - discovered)}. "
        "Classify each as 'attributed' (pass application_name=_APPLICATION_NAME) or 'unreachable' "
        "(with the reason it opens no database connection)."
    )


@pytest.mark.parametrize(
    ("relative_path", "factory"),
    [(module, factory) for module, factory, verdict, _d in DISPLAY_UNIT_STORE_FACTORIES if verdict == ATTRIBUTED],
    ids=[
        f"{module}::{factory}"
        for module, factory, verdict, _d in DISPLAY_UNIT_STORE_FACTORIES
        if verdict == ATTRIBUTED
    ],
)
def test_route_layer_store_factory_injects_the_module_identity(relative_path: str, factory: str) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    sites = 0
    unattributed: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "from_env"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == factory
        ):
            continue
        sites += 1
        injected = next((kw.value for kw in node.keywords if kw.arg == "application_name"), None)
        if not _references_identity_constant(injected):
            unattributed.append(node.lineno)
    assert sites >= 1, f"{relative_path} no longer builds {factory}; the registry row is stale"
    assert unattributed == [], (
        f"{relative_path} lines {unattributed} build {factory} without "
        "application_name=_APPLICATION_NAME, so its backend lands in pg_stat_activity unattributed"
    )


@pytest.mark.parametrize(
    ("relative_path", "expected_name"),
    DISPLAY_UNIT_SURFACES,
    ids=[path for path, _ in DISPLAY_UNIT_SURFACES],
)
def test_display_unit_route_module_declares_its_surface_name(relative_path: str, expected_name: str) -> None:
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    assert _module_application_name(tree) == expected_name, (
        f"{relative_path} must define a module-level _APPLICATION_NAME == {expected_name!r}"
    )


def test_display_unit_surface_names_are_unique_and_fit_the_libpq_bound() -> None:
    names = [name for _path, name in DISPLAY_UNIT_SURFACES]
    assert len(set(names)) == len(names)
    for name in names:
        assert name == name.lower()
        assert len(name.encode()) <= 63
    # No display-unit surface may collide with a registered node-27 component
    # other than hydro_display.py, which is the same connection surface.
    component_names = {name for path, name in REGISTERED_COMPONENTS if path != "apps/api/routes/hydro_display.py"}
    assert component_names.isdisjoint(set(names))


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/common/forecast_store.py",
        "packages/common/best_available.py",
        "packages/common/model_registry.py",
        "packages/common/state_manager.py",
        "packages/common/object_store_forcing.py",
    ],
)
def test_shared_store_module_hard_codes_no_surface_name(relative_path: str) -> None:
    """packages/common owns the seam, never the identity (#1728 D1)."""
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    surface_names = {name for _path, name in DISPLAY_UNIT_SURFACES}
    hard_coded = sorted(
        {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in surface_names
        }
    )
    assert hard_coded == [], f"{relative_path} hard-codes a route-layer identity: {hard_coded}"


# --------------------------------------------------------------------------- #
# T1/T2 (#1728) -- the route-layer name reaches the driver
#
# The static halves above prove the wiring is written; these prove it is
# EXECUTED: each case runs the router's real dependency provider, so a store
# that silently drops the kwarg somewhere between from_env() and
# psycopg2.connect fails here even while every AST check stays green.
# --------------------------------------------------------------------------- #
def _open_forecast_route_connection() -> None:
    forecast.get_forecast_store()._transaction().__enter__()


def _open_forecast_nameless_connection() -> None:
    PsycopgForecastStore.from_env()._transaction().__enter__()


def _open_data_sources_route_connection() -> None:
    data_sources.get_data_source_store()._transaction().__enter__()


def _open_station_lookup_route_connection() -> None:
    data_sources.get_station_lookup().lookup("STA-1")


def _open_station_lookup_nameless_connection() -> None:
    PsycopgStationLookup.from_env().lookup("STA-1")


def _open_best_available_route_connection() -> None:
    best_available.get_best_available_manager().repository._fetch_all("SELECT 1", ())


def _open_best_available_nameless_connection() -> None:
    BestAvailableManager.from_env().repository._fetch_all("SELECT 1", ())


def _open_models_route_connection() -> None:
    models.get_model_registry_store()._transaction().__enter__()


def _open_models_nameless_connection() -> None:
    PsycopgModelRegistryStore.from_env()._transaction().__enter__()


def _open_state_snapshots_route_connection() -> None:
    state_snapshots.get_state_manager().repository._fetch_all("SELECT 1", ())


def _open_state_snapshots_nameless_connection() -> None:
    StateManager.from_env().repository._fetch_all("SELECT 1", ())


# (case id, route-layer invoker, nameless-store invoker, expected identity).
DISPLAY_UNIT_STORE_CASES: tuple[tuple[str, Any, Any, str], ...] = (
    ("forecast", _open_forecast_route_connection, _open_forecast_nameless_connection, "nhms-api-forecast"),
    (
        "data_sources_store",
        _open_data_sources_route_connection,
        _open_forecast_nameless_connection,
        "nhms-api-data-sources",
    ),
    (
        "data_sources_station_lookup",
        _open_station_lookup_route_connection,
        _open_station_lookup_nameless_connection,
        "nhms-api-data-sources",
    ),
    (
        "best_available",
        _open_best_available_route_connection,
        _open_best_available_nameless_connection,
        "nhms-api-best-available",
    ),
    ("models", _open_models_route_connection, _open_models_nameless_connection, "nhms-api-models"),
    (
        "state_snapshots",
        _open_state_snapshots_route_connection,
        _open_state_snapshots_nameless_connection,
        "nhms-api-state-snapshots",
    ),
)


def _display_unit_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str) -> None:
    """The env the uvicorn unit reads when a route builds its store."""
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))


@pytest.mark.parametrize(
    ("case_id", "open_route_connection", "_open_nameless_connection", "expected_name"),
    DISPLAY_UNIT_STORE_CASES,
    ids=[case[0] for case in DISPLAY_UNIT_STORE_CASES],
)
def test_display_unit_route_store_connect_carries_its_surface_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    open_route_connection: Any,
    _open_nameless_connection: Any,
    expected_name: str,
) -> None:
    _display_unit_env(monkeypatch, tmp_path, DSN)
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        open_route_connection()

    assert probe.called, f"{case_id} never reached psycopg2.connect"
    assert probe.args == (DSN,)
    assert probe.kwargs.pop("fallback_application_name") == expected_name
    # The shared stores connect with no other kwargs; the injection may not add any.
    assert probe.kwargs == {}


@pytest.mark.parametrize(
    ("case_id", "_open_route_connection", "open_nameless_connection", "_expected_name"),
    DISPLAY_UNIT_STORE_CASES,
    ids=[case[0] for case in DISPLAY_UNIT_STORE_CASES],
)
def test_shared_store_without_a_name_connects_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    _open_route_connection: Any,
    open_nameless_connection: Any,
    _expected_name: str,
) -> None:
    """Legacy compatibility: no name injected -> the kwarg is absent, not None.

    Every non-display caller of these stores (workers, scripts, tests) builds
    them without a name; passing ``fallback_application_name=None`` would change
    what libpq receives on all of them.
    """
    _display_unit_env(monkeypatch, tmp_path, DSN)
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        open_nameless_connection()

    assert probe.called, f"{case_id} never reached psycopg2.connect"
    assert probe.args == (DSN,)
    assert "fallback_application_name" not in probe.kwargs
    assert probe.kwargs == {}


@pytest.mark.parametrize(
    ("case_id", "open_route_connection", "_open_nameless_connection", "expected_name"),
    DISPLAY_UNIT_STORE_CASES,
    ids=[case[0] for case in DISPLAY_UNIT_STORE_CASES],
)
def test_display_unit_route_store_keeps_the_operator_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case_id: str,
    open_route_connection: Any,
    _open_nameless_connection: Any,
    expected_name: str,
) -> None:
    """T4 over the display unit: `?application_name=` in the DSN still wins."""
    _display_unit_env(monkeypatch, tmp_path, DSN_WITH_OVERRIDE)
    probe = _probe_psycopg2_connect(monkeypatch)

    with pytest.raises(_ConnectIntercepted):
        open_route_connection()

    # The store may not rewrite the operator's DSN to inject its identity.
    assert probe.args == (DSN_WITH_OVERRIDE,)
    conninfo = psycopg2.extensions.make_dsn(probe.args[0], **probe.kwargs)
    parsed = psycopg2.extensions.parse_dsn(conninfo)
    assert parsed["application_name"] == "operator-override"
    assert parsed["fallback_application_name"] == expected_name


def test_pipeline_engine_adds_its_identity_and_leaves_other_parameters_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_create_engine(url: str, **kwargs: Any) -> str:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return "engine"

    monkeypatch.setattr(pipeline, "create_engine", _fake_create_engine)

    # __wrapped__ bypasses the lru_cache so the probe never pollutes it.
    assert pipeline._engine.__wrapped__(DSN) == "engine"

    assert captured["url"] == DSN
    assert captured["kwargs"] == {
        "future": True,
        "connect_args": {"fallback_application_name": "nhms-api-pipeline"},
    }


def test_pipeline_and_display_engines_are_separate_surfaces() -> None:
    """The retry/cancel writes must be distinguishable from the display pool.

    #1728's operator scenario: both engines live in the same uvicorn process, so
    a shared name would put the control-plane writes and the read-only display
    queries under one label in pg_stat_activity.
    """
    assert pipeline._APPLICATION_NAME != hydro_display._APPLICATION_NAME
    assert (pipeline._APPLICATION_NAME, hydro_display._APPLICATION_NAME) == (
        "nhms-api-pipeline",
        "nhms-display-api",
    )
