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

T5b (delegated connect surfaces / ``DELEGATED_CONNECT_CLOSURE``) lives in
``tests/test_node27_connection_attribution_delegated.py``.
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

from apps.api.routes import hydro_display
from packages.common import display_coverage
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
        "NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND": "5",
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
