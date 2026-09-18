"""Unit pins for the node-27 coverage freshness alerter (issue #2080).

Evidence anchors 1-20 of
``openspec/changes/node27-coverage-freshness-alert/tasks.md`` §"Required
evidence". Every scenario runs through ``main(argv, now=..., observe=...,
env=...)`` with an injected clock and an injected observation provider — no
database, no network, no sendmail, no sleeps.

The single exception is evidence 20, which reaches into ``default_observe`` to
assert the *call shape* into ``services.tiles.mvt.national_discharge_cycles``
(the governing invariant of design D0). It fakes the SQLAlchemy engine/session
and is therefore still not a database test.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_coverage_freshness_alert as alerter

DSN_PASSWORD = "s3cr3t"
DSN = f"postgresql://nhms_display_ro:{DSN_PASSWORD}@127.0.0.1:55432/nhms"

#: The injected "now", and the newest cycle every fixture is written against.
T0 = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _env(**overrides: str) -> dict[str, str]:
    env = {"DATABASE_URL": DSN}
    env.update(overrides)
    return env


def _frontiers(source_key: str, ready: datetime | None, covered: Any) -> Any:
    return alerter.SourceFrontiers(source_key=source_key, ready_frontier=ready, covered_cycle=covered)


class RecordingObserve:
    """Injected observation provider that records whether it ran at all."""

    def __init__(self, observation: Mapping[str, Any] | None = None, error: BaseException | None = None) -> None:
        self.observation = dict(observation or {})
        self.error = error
        self.calls = 0

    def __call__(self, config: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.observation)


def _run(
    capsys: pytest.CaptureFixture[str],
    *,
    env: Mapping[str, str],
    observation: Mapping[str, Any] | None = None,
    observe: Any = None,
    now: datetime = T0,
) -> tuple[int, str, str]:
    provider = observe if observe is not None else RecordingObserve(observation)
    rc = alerter.main([], now=now, observe=provider, env=dict(env))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _verdict_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("VERDICT:")]


def _source_line(out: str, source_key: str) -> str:
    prefix = f"source={source_key} "
    matches = [line for line in out.splitlines() if line.startswith(prefix)]
    assert matches, f"no report row for {source_key!r} in:\n{out}"
    return matches[0]


# ---------------------------------------------------------------------------
# Evidence 1-8 — the criterion (design D1).
# ---------------------------------------------------------------------------


def test_evidence_1_frontiers_equal_is_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _err = _run(capsys, env=_env(), observation={"gfs": _frontiers("gfs", T0, T0)})

    assert rc == 0
    assert "gap=0.0d" in _source_line(out, "gfs")
    assert "status=ok" in _source_line(out, "gfs")


def test_evidence_2_gap_beyond_threshold_alerts_and_names_the_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={"gfs": _frontiers("gfs", T0, T0 - timedelta(days=5))},
    )

    assert rc == 1
    verdict = "\n".join(_verdict_lines(out))
    assert "gfs" in verdict
    assert "5.0" in verdict


@pytest.mark.parametrize("lag_days", [3, 4])
def test_evidence_3_and_4_below_or_at_the_threshold_does_not_trip(
    capsys: pytest.CaptureFixture[str], lag_days: int
) -> None:
    """Strictly greater trips; exact equality at 4.0 d does not."""

    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={"gfs": _frontiers("gfs", T0, T0 - timedelta(days=lag_days))},
    )

    assert rc == 0
    assert "status=ok" in _source_line(out, "gfs")


def test_evidence_5_full_ingest_stall_inside_the_window_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both frontiers frozen together — owned by the `frontier-stalled` lane."""

    frozen = T0 - timedelta(days=2)
    rc, out, _err = _run(capsys, env=_env(), observation={"gfs": _frontiers("gfs", frozen, frozen)})

    assert rc == 0
    assert "gap=0.0d" in _source_line(out, "gfs")


def test_evidence_6_ingest_stall_beyond_the_window_is_not_evaluated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    frozen = T0 - timedelta(days=20)
    rc, out, _err = _run(capsys, env=_env(), observation={"gfs": _frontiers("gfs", frozen, frozen)})

    assert rc == 0
    row = _source_line(out, "gfs")
    assert "status=not-evaluated" in row
    assert "reason=outside-window" in row


def test_evidence_7_in_window_source_without_a_covered_cycle_alerts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, _err = _run(capsys, env=_env(), observation={"gfs": _frontiers("gfs", T0, None)})

    assert rc == 1
    assert "status=no-covered-cycle" in _source_line(out, "gfs")
    assert "no-covered-cycle" in "\n".join(_verdict_lines(out))


def test_evidence_8_only_the_breaching_source_is_named_in_the_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={
            "gfs": _frontiers("gfs", T0, T0 - timedelta(days=6)),
            "ifs": _frontiers("ifs", T0, T0),
        },
    )

    assert rc == 1
    verdict = "\n".join(_verdict_lines(out))
    assert "gfs" in verdict
    assert "ifs" not in verdict
    assert "status=ok" in _source_line(out, "ifs")


def test_evidence_9_null_source_key_never_alerts(capsys: pytest.CaptureFixture[str]) -> None:
    """`national_discharge_cycles` takes a `str` source, so NULL-source runs can
    never be listed by the per-source catalog and must not drive the exit code."""

    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={
            alerter.NULL_SOURCE_KEY: _frontiers(alerter.NULL_SOURCE_KEY, T0, T0 - timedelta(days=99)),
        },
    )

    assert rc == 0
    row = _source_line(out, alerter.NULL_SOURCE_KEY)
    assert "status=not-evaluated" in row
    assert "reason=null-source" in row


# ---------------------------------------------------------------------------
# Evidence 10 — the empty-observation fork (design D6).
# ---------------------------------------------------------------------------


def test_evidence_10a_every_source_outside_the_window_is_healthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = T0 - timedelta(days=30)
    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={
            "gfs": _frontiers("gfs", old, None),
            "ifs": _frontiers("ifs", old, old),
        },
    )

    assert rc == 0
    assert "status=not-evaluated" in _source_line(out, "gfs")
    assert "status=not-evaluated" in _source_line(out, "ifs")


def test_evidence_10b_zero_source_keys_is_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty ready-frontier result means the observer cannot see the run set
    the national catalog is built from — never "healthy, nothing to do"."""

    rc, out, err = _run(capsys, env=_env(), observation={})

    assert rc == 3
    assert "no display-ready source key" in out
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_NO_SOURCES
    assert payload["status"] == "failed"


# ---------------------------------------------------------------------------
# Evidence 11-13, 17 — threshold and config (design D2).
# ---------------------------------------------------------------------------


def test_evidence_11_threshold_override_is_applied(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, _err = _run(
        capsys,
        env=_env(NHMS_COVERAGE_GAP_DAYS="2.5"),
        observation={"gfs": _frontiers("gfs", T0, T0 - timedelta(days=3))},
    )

    assert rc == 1
    assert "threshold=2.5d" in out
    assert "status=gap-exceeded" in _source_line(out, "gfs")


def test_evidence_12_invalid_threshold_is_a_config_error_before_any_observation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The window-valued case is BUILT from the constant, never from the literal
    # 12, so raising the window cannot silently flip refusal into acceptance.
    bad_values = ["abc", "0", "-1", "inf", "nan", str(alerter.lookback_days())]

    for raw in bad_values:
        observe = RecordingObserve({"gfs": _frontiers("gfs", T0, T0)})
        rc = alerter.main([], now=T0, observe=observe, env=_env(NHMS_COVERAGE_GAP_DAYS=raw))
        captured = capsys.readouterr()

        assert rc == 2, f"{raw!r} should be refused"
        assert observe.calls == 0, f"{raw!r} must not reach the observation"
        payload = json.loads(captured.err.strip().splitlines()[-1])
        assert payload["code"] == alerter.CODE_CONFIG_INVALID


def test_evidence_13_missing_database_url_is_a_config_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    observe = RecordingObserve({"gfs": _frontiers("gfs", T0, T0)})
    rc = alerter.main([], now=T0, observe=observe, env={})
    captured = capsys.readouterr()

    assert rc == 2
    assert observe.calls == 0
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert "DATABASE_URL" in payload["reason"]


def test_import_time_display_failure_is_a_config_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2465: a display module that will not import is a CONFIG failure (exit 2),
    not an observation one — `lookback_days()` is called inside `config_from_env`,
    before any database work.

    The pinned seam is the REAL `from services.tiles.mvt import …` statement inside
    `lookback_days`, not the handler around it: poisoning `sys.modules` makes CPython
    raise at that statement on every call (the import is function-local, so it re-runs
    per invocation). Monkeypatching `lookback_days` wholesale would pin only `main`'s
    error handling and would stay green if that import ever grew a
    `try/except ImportError` fallback constant — a regression that trades exit 2 for
    exit 0/1 under a silently wrong threshold, and one the static source scan in
    `test_lazy_display_import_keeps_the_module_import_light` cannot see either.

    `DATABASE_URL` is deliberately present: `_required_env` reads it first, so without
    it the run would refuse on the missing-DSN path and never reach the import at all.
    """

    monkeypatch.setitem(sys.modules, "services.tiles.mvt", None)
    observe = RecordingObserve({"gfs": _frontiers("gfs", T0, T0)})

    rc = alerter.main([], now=T0, observe=observe, env=_env())
    captured = capsys.readouterr()

    assert rc == 2
    assert observe.calls == 0
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert payload["reason"].startswith(("ImportError:", "ModuleNotFoundError:"))
    assert DSN_PASSWORD not in captured.out
    assert DSN_PASSWORD not in captured.err


class _RaisingDisplayModuleFinder:
    """Meta-path finder whose `services.tiles.mvt` module body raises on execution.

    Stands in for dependency drift inside the display module (e.g. a shapely
    release that dropped an attribute the module touches at import). The error
    is raised while the module BODY executes, so CPython propagates it unchanged
    through the real `from services.tiles.mvt import …` statement — unlike a
    missing attribute on an already-imported module, which `import_from` would
    convert to `ImportError: cannot import name`.
    """

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != "services.tiles.mvt":
            return None
        import importlib.util

        return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        raise self.error


def test_any_import_time_exception_class_prefixes_the_config_reason(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2472/#2473 round 1: the exit-2 discriminator the runbook routes on is the
    `<ExceptionClass>: ` prefix, not `ImportError:` specifically. `main`'s generic
    config-stage handler prefixes EVERY non-`CoverageAlertConfigError` class, and
    the only unguarded call in `config_from_env` is the lazy display import, so a
    non-ImportError class raised there (AttributeError from dependency drift) is the
    same import stage and must read `AttributeError: …` under the same code.
    """

    monkeypatch.delitem(sys.modules, "services.tiles.mvt", raising=False)
    finder = _RaisingDisplayModuleFinder(AttributeError("module 'shapely' has no attribute 'drifted'"))
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    observe = RecordingObserve({"gfs": _frontiers("gfs", T0, T0)})

    rc = alerter.main([], now=T0, observe=observe, env=_env())
    captured = capsys.readouterr()

    assert rc == 2
    assert observe.calls == 0
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert payload["reason"] == "AttributeError: module 'shapely' has no attribute 'drifted'"


@pytest.mark.parametrize(
    ("env", "expected_reason"),
    [
        ({}, "DATABASE_URL must be set"),
        (_env(NHMS_COVERAGE_GAP_DAYS="abc"), "NHMS_COVERAGE_GAP_DAYS must be a number, got 'abc'"),
    ],
    ids=["missing-dsn", "non-numeric-threshold"],
)
def test_config_error_reasons_carry_no_exception_class_prefix(
    capsys: pytest.CaptureFixture[str],
    env: dict[str, str],
    expected_reason: str,
) -> None:
    """The other half of the runbook's exit-2 split: a `CoverageAlertConfigError`
    (missing DSN, unusable threshold) is reported as its bare message, so a
    `reason` WITHOUT a `<ExceptionClass>: ` prefix routes to the env file (§11.4),
    never to the import check (§11.5).
    """

    observe = RecordingObserve({"gfs": _frontiers("gfs", T0, T0)})

    rc = alerter.main([], now=T0, observe=observe, env=env)
    captured = capsys.readouterr()

    assert rc == 2
    assert observe.calls == 0
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert payload["reason"] == expected_reason
    assert not re.match(r"^[A-Za-z_][A-Za-z0-9_]*: ", payload["reason"])


def test_evidence_17_default_threshold_is_derived_from_the_display_constant() -> None:
    lookback = alerter.lookback_days()
    assert alerter.default_gap_days() == lookback / alerter.GAP_THRESHOLD_DIVISOR
    assert alerter.default_gap_days() < lookback

    config = alerter.config_from_env(_env())
    assert config.gap_days == lookback / alerter.GAP_THRESHOLD_DIVISOR
    assert config.lookback_days == lookback


# ---------------------------------------------------------------------------
# Evidence 14 — redaction (design D6).
# ---------------------------------------------------------------------------


def test_evidence_14_observation_failure_is_fail_closed_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = f"postgresql://u:{DSN_PASSWORD}@h/db"
    observe = RecordingObserve(error=RuntimeError(f"could not connect to {dsn}"))

    rc = alerter.main([], now=T0, observe=observe, env={"DATABASE_URL": dsn})
    captured = capsys.readouterr()

    assert rc == 3
    assert observe.calls == 1
    assert DSN_PASSWORD not in captured.out
    assert DSN_PASSWORD not in captured.err
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_OBSERVATION_FAILED
    assert _verdict_lines(captured.out)


def _driver_error(kind: str) -> BaseException:
    """Real SQLAlchemy-wrapped psycopg2 errors, built the way SQLAlchemy builds
    them (`DBAPIError(statement, params, orig)`), with the driver messages
    measured on node-27 (2026-09-18) — never a hand-made reason string."""

    import psycopg2
    import psycopg2.errors
    import sqlalchemy.exc

    if kind == "refused":
        return sqlalchemy.exc.OperationalError(
            "SET statement_timeout = 30000",
            {},
            psycopg2.OperationalError(
                'connection to server at "127.0.0.1", port 1 failed: Connection refused\n'
                "\tIs the server running on that host and accepting TCP/IP connections?\n"
            ),
        )
    return sqlalchemy.exc.ProgrammingError(
        alerter.READY_FRONTIER_QUERY,
        {},
        psycopg2.errors.UndefinedTable('relation "hydro.hydro_run" does not exist\nLINE 4: FROM hydro.hydro_run h\n'),
    )


@pytest.mark.parametrize(
    ("kind", "prefix"),
    [
        ("refused", "OperationalError: (psycopg2.OperationalError) "),
        ("wrong-database", "ProgrammingError: (psycopg2.errors.UndefinedTable) "),
    ],
)
def test_observation_failure_reason_carries_the_class_and_driver_class_the_runbook_routes_on(
    capsys: pytest.CaptureFixture[str],
    kind: str,
    prefix: str,
) -> None:
    """Characterization pin (#2473) — the code already behaves so.

    Runbook §11.2 routes `COVERAGE_FRESHNESS_OBSERVATION_FAILED` on the reason's
    `<SQLAlchemy class>: (<driver class>) …` shape: the SQLAlchemy class alone
    cannot route (`OperationalError` is both "unreachable" and "statement
    timeout"; `ProgrammingError` is both "permission denied" and "database
    without the lane's relations"). If the reason format drifts, the runbook's
    routing reads a line the mail no longer carries.
    """

    observe = RecordingObserve(error=_driver_error(kind))

    rc, out, err = _run(capsys, env=_env(), observe=observe)

    assert rc == alerter.EXIT_OBSERVATION == 3
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["code"] == "COVERAGE_FRESHNESS_OBSERVATION_FAILED"
    assert payload["reason"].startswith(prefix)
    assert _verdict_lines(out)[0].startswith(f"VERDICT: FAIL observation failed: {prefix}")


def test_unparsable_dsn_never_reaches_the_driver(capsys: pytest.CaptureFixture[str]) -> None:
    """Characterization pin (#2473) for the routing leg WITHOUT `(psycopg2.`.

    Runs the REAL `default_observe` (no injected provider): SQLAlchemy rejects
    the DSN while building the engine, before any connection, so no database is
    needed. The reason is exactly the one measured on node-27 for the same DSN
    shape — a bare `ValueError:` with no driver class — which is why the runbook
    sends this leg to the probe instead of to the database.
    """

    env = {"DATABASE_URL": f"postgresql://nhms_display_ro:{DSN_PASSWORD}@127.0.0.1:notaport/nhms"}

    rc = alerter.main([], now=T0, env=env)
    captured = capsys.readouterr()

    assert rc == 3
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == "COVERAGE_FRESHNESS_OBSERVATION_FAILED"
    assert payload["reason"] == "ValueError: invalid literal for int() with base 10: 'notaport'"
    assert "(psycopg2." not in payload["reason"]
    assert _verdict_lines(captured.out)[0] == (
        "VERDICT: FAIL observation failed: ValueError: invalid literal for int() with base 10: 'notaport'"
    )
    assert DSN_PASSWORD not in captured.out + captured.err


# ---------------------------------------------------------------------------
# Evidence 15 — journal budget (design D3).
# ---------------------------------------------------------------------------


def test_evidence_15_report_fits_the_mailed_journal_tail(capsys: pytest.CaptureFixture[str]) -> None:
    observation: dict[str, Any] = {}
    breaching = [f"src{index:02d}" for index in range(5)]
    for key in breaching:
        observation[key] = _frontiers(key, T0, T0 - timedelta(days=6))
    for index in range(20):
        key = f"ok{index:02d}"
        observation[key] = _frontiers(key, T0, T0)

    rc, out, _err = _run(capsys, env=_env(), observation=observation)

    assert rc == 1
    lines = out.splitlines()
    assert len(lines) <= alerter.MAX_REPORT_LINES
    for key in breaching:
        assert f"source={key} " in out
    assert "more sources omitted" in out

    # Once the verdict block starts, nothing else may follow it — it has to be
    # the part that survives `journalctl -n 30`.
    first_verdict = next(index for index, line in enumerate(lines) if line.startswith("VERDICT:"))
    assert all(line.startswith("VERDICT:") for line in lines[first_verdict:])


def test_more_breaching_sources_than_the_table_holds_is_reported_honestly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The "breaching rows survive truncation" guarantee is BOUNDED (D3).

    Beyond the table's capacity alerting rows do fall off too — but nothing is
    hidden: the header carries the true `breaching=` count, the omission line
    carries the exact number of dropped rows, and the verdict still names the
    first `VERDICT_SOURCE_LIST_LIMIT` sources plus a `+N more` remainder.
    Growing the table instead would push the verdict out of the journal tail.
    """

    observation: dict[str, Any] = {
        f"bad{index:02d}": _frontiers(f"bad{index:02d}", T0, T0 - timedelta(days=6)) for index in range(22)
    }
    for index in range(3):
        key = f"ok{index:02d}"
        observation[key] = _frontiers(key, T0, T0)

    rc, out, _err = _run(capsys, env=_env(), observation=observation)

    assert rc == 1
    lines = out.splitlines()
    assert len(lines) <= alerter.MAX_REPORT_LINES
    assert "breaching=22" in lines[0]

    rows = [line for line in lines if line.startswith("source=")]
    omission = [line for line in lines if "more sources omitted" in line]
    assert len(omission) == 1
    assert f"… {len(observation) - len(rows)} more sources omitted" == omission[0]

    verdict = "\n".join(_verdict_lines(out))
    kept_names = sorted(observation)[: alerter.VERDICT_SOURCE_LIST_LIMIT]
    for key in kept_names:
        assert key in verdict
    assert f"+{22 - alerter.VERDICT_SOURCE_LIST_LIMIT} more" in verdict


# ---------------------------------------------------------------------------
# Evidence 16 — the covered frontier arrives as a formatted string.
# ---------------------------------------------------------------------------


def test_evidence_16_iso_string_default_cycle_is_parsed_into_a_day_gap(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, out, _err = _run(
        capsys,
        env=_env(),
        observation={"gfs": _frontiers("gfs", T0, "2026-09-12T00:00:00Z")},
    )

    assert rc == 1
    row = _source_line(out, "gfs")
    assert "gap=5.0d" in row
    assert "gap=120" not in row


# ---------------------------------------------------------------------------
# Evidence 18 — the one re-derived statement (design D0).
# ---------------------------------------------------------------------------


def test_evidence_18_ready_frontier_statement_matches_the_catalog_run_set() -> None:
    query = alerter.READY_FRONTIER_QUERY

    assert "status IN ('succeeded', 'parsed', 'published')" in query
    assert "mi.active_flag" in query
    assert "mi.river_network_version_id IS NOT NULL" in query
    assert "h.cycle_time IS NOT NULL" in query
    assert "lower(h.source_id)" in query
    # The coverage join is exactly what the ready side must NOT carry.
    assert "run_display_coverage" not in query


def test_ready_frontier_statement_is_read_only() -> None:
    upper = alerter.READY_FRONTIER_QUERY.upper()
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE"):
        assert verb not in upper


# ---------------------------------------------------------------------------
# Evidence 19 — statelessness.
# ---------------------------------------------------------------------------


def test_evidence_19_rerun_is_deterministic_and_writes_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    observation = {
        "gfs": _frontiers("gfs", T0, T0 - timedelta(days=6)),
        "ifs": _frontiers("ifs", T0, T0),
    }

    first = _run(capsys, env=_env(), observation=observation)
    second = _run(capsys, env=_env(), observation=observation)

    assert first == second
    assert first[0] == 1
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Evidence 20 — covered-side parity by construction (design D0, the governing
# invariant). The one test that reaches into `default_observe`: a call-shape
# assertion with a faked engine/session, not a database test.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: Any, *_args: Any, **_kwargs: Any) -> _FakeResult:
        self.executed.append(str(statement))
        if "hydro_run" in str(statement):
            return _FakeResult(self.rows)
        return _FakeResult([])


def test_evidence_20_covered_frontier_comes_from_national_discharge_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy
    import sqlalchemy.orm

    from services.tiles import mvt

    rows = [
        {"source_key": "gfs", "ready_frontier": T0},
        {"source_key": alerter.NULL_SOURCE_KEY, "ready_frontier": T0},
    ]
    session = _FakeSession(rows)
    engine_calls: dict[str, Any] = {}
    disposed: list[bool] = []

    class _FakeEngine:
        def dispose(self) -> None:
            disposed.append(True)

    def _fake_create_engine(url: str, **kwargs: Any) -> _FakeEngine:
        engine_calls["url"] = url
        engine_calls.update(kwargs)
        return _FakeEngine()

    def _fake_session(engine: Any, **_kwargs: Any) -> _FakeSession:
        engine_calls["session_engine"] = engine
        return session

    spy_calls: list[dict[str, Any]] = []
    spy_return = {"source": "gfs", "cycles": [], "default_cycle": "2026-09-12T00:00:00Z"}

    def _fake_cycles(passed_session: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        spy_calls.append({"session": passed_session, "args": args, "kwargs": kwargs})
        return spy_return

    monkeypatch.setattr(sqlalchemy, "create_engine", _fake_create_engine)
    monkeypatch.setattr(sqlalchemy.orm, "Session", _fake_session)
    monkeypatch.setattr(mvt, "national_discharge_cycles", _fake_cycles)

    config = alerter.config_from_env(_env())
    observation = alerter.default_observe(config)

    # Engine bounds (design D6).
    assert engine_calls["url"] == DSN
    assert engine_calls["connect_args"] == {"connect_timeout": alerter.CONNECT_TIMEOUT_SEC}
    assert 0 < alerter.CONNECT_TIMEOUT_SEC <= 60
    assert any(f"SET statement_timeout = {alerter.QUERY_TIMEOUT_MS}" in stmt for stmt in session.executed)
    assert 0 < alerter.QUERY_TIMEOUT_MS <= 300_000
    assert disposed == [True]

    # Parity by construction: exactly one catalog call, for the real source key
    # only, with the module's own default listing limit.
    assert len(spy_calls) == 1
    assert spy_calls[0]["session"] is session
    assert spy_calls[0]["kwargs"] == {"source": "gfs"}
    assert spy_calls[0]["args"] == ()

    # `default_cycle` is used UNMODIFIED — nothing on the covered side is
    # re-derived in this module.
    assert observation["gfs"].covered_cycle is spy_return["default_cycle"]
    assert observation[alerter.NULL_SOURCE_KEY].covered_cycle is None


def _non_docstring_literals(source: str) -> list[str]:
    """Every string constant the module can EXECUTE (docstrings excluded)."""

    tree = ast.parse(source)
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstring_ids.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids
    ]


def test_module_does_not_re_derive_the_coverage_predicate() -> None:
    """The observer must own no coverage predicate of its own (design D0).

    Docstrings are excluded on purpose: the module must *explain* why it does not
    re-derive the predicate, while none of the SQL or logic it actually runs may
    mention the coverage table or its columns.
    """

    literals = _non_docstring_literals(Path(alerter.__file__).read_text(encoding="utf-8"))
    for needle in ("run_display_coverage", "segment_count", "river_sample_count", "lead_time_hours"):
        assert not any(needle in literal for literal in literals), needle


def test_alerter_imports_no_api_or_frontend_module() -> None:
    """ADR 0001 display carve-out, as observed by the sibling frontier lane."""

    source = Path(alerter.__file__).read_text(encoding="utf-8")
    assert "apps.api" not in source
    assert "apps.frontend" not in source


def test_lazy_display_import_keeps_the_module_import_light() -> None:
    """`services.tiles.mvt` is imported inside functions, never at module scope,
    so `--help` and config parsing work on a host without the display stack."""

    source = Path(alerter.__file__).read_text(encoding="utf-8")
    module_scope = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("services.tiles" in line for line in module_scope)
    assert not any("sqlalchemy" in line for line in module_scope)
    assert alerter.build_parser().description


# ---------------------------------------------------------------------------
# #2473 — every structured failure code has a documented destination.
# ---------------------------------------------------------------------------

_RUNBOOK_PATH = Path(__file__).resolve().parents[1] / "docs/runbooks/current-production-ops.md"


def _runbook_section_11(text: str) -> str:
    """Runbook §11, delimited by its own `## 11.` and `## 12.` headings."""

    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## 11."))
    end = next(index for index, line in enumerate(lines) if index > start and line.startswith("## 12."))
    return "\n".join(lines[start:end])


def test_every_structured_failure_code_is_named_in_runbook_section_11() -> None:
    """Spec scenario "Every structured failure code has a documented destination".

    The codes are read from the script's own executable string literals, never
    restated here, so a code added to the script without a runbook destination
    turns this red.
    """

    literals = _non_docstring_literals(Path(alerter.__file__).read_text(encoding="utf-8"))
    codes = sorted({literal for literal in literals if re.fullmatch(r"COVERAGE_FRESHNESS_[A-Z_]+", literal)})
    assert codes, "no COVERAGE_FRESHNESS_* code found in the script — the scan itself is broken"

    section = _runbook_section_11(_RUNBOOK_PATH.read_text(encoding="utf-8"))
    missing = [code for code in codes if code not in section]
    assert not missing, f"codes the script emits but runbook §11 never names: {missing}"
