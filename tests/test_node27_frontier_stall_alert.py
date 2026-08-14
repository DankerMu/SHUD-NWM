"""Unit pins for the node-27 frontier stall alerter (issue #1368).

Test anchors B1-B28 of
``openspec/changes/node27-frontier-stall-alert/tasks.md`` §2. Every test
injects a fake clock (explicit ``now=`` datetimes), a fake sendmail runner
(records invocations) and a fake observation provider — no database, no real
sendmail, no network, no sleeps.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_frontier_stall_alert as alerter

DSN_PASSWORD = "s3cr3t-frontier-pw"
DSN = f"postgresql://nhms_display_ro:{DSN_PASSWORD}@127.0.0.1:55432/nhms"
RECIPIENT = "frontier-ops@example.invalid"
SENDER = "NHMS Frontier Alert <nwm@node-27-test>"
T0 = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes / helpers.
# ---------------------------------------------------------------------------


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "DATABASE_URL": DSN,
        "NHMS_ALERT_EMAIL_TO": RECIPIENT,
        "NHMS_ALERT_EMAIL_FROM": SENDER,
        "NHMS_FRONTIER_STALL_HOURS": "4",
        "NHMS_FRONTIER_RESEND_HOURS": "6",
        "NHMS_FRONTIER_STATE_PATH": str(tmp_path / "state" / "frontier-alert-state.json"),
        "NHMS_FRONTIER_RECEIPT_PATH": str(tmp_path / "receipts" / "frontier-alert-receipt.json"),
        "NHMS_FRONTIER_SENDMAIL": "/usr/sbin/sendmail",
        "NHMS_FRONTIER_QUERY_FAIL_TICKS": "2",
    }
    env.update(overrides)
    return env


def _config(tmp_path: Path, **overrides: str) -> alerter.AlertConfig:
    return alerter.config_from_env(_env(tmp_path, **overrides))


class FakeSendmail:
    """Records every invocation; returns queued results (default success)."""

    def __init__(self, results: list[alerter.SendResult] | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self._results = list(results or [])

    def __call__(self, argv: list[str], message: str) -> alerter.SendResult:
        self.calls.append((list(argv), message))
        if self._results:
            return self._results.pop(0)
        return alerter.SendResult(returncode=0)

    @property
    def messages(self) -> list[str]:
        return [message for _argv, message in self.calls]

    def subject(self, index: int = 0) -> str:
        for line in self.messages[index].splitlines():
            if line.startswith("Subject: "):
                return line[len("Subject: ") :]
        raise AssertionError("message has no Subject header")

    def header(self, name: str, index: int = 0) -> str:
        prefix = f"{name}: "
        for line in self.messages[index].splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :]
        raise AssertionError(f"message has no {name} header")


class ExplodingSendmail:
    """Raises ``_MustNotHappen`` (a ``BaseException``) on purpose: after the
    round-3 whole-class containment in ``_Outbox.send``, an ``AssertionError``
    here would be absorbed into a "failed send" record and this guard would
    stop guarding."""

    def __call__(self, argv: list[str], message: str) -> alerter.SendResult:  # pragma: no cover
        raise _MustNotHappen("sendmail must not be invoked")


def _obs(
    source_key: str,
    *,
    frontier: datetime,
    cycles: int,
    latest_created: datetime,
) -> alerter.SourceObservation:
    return alerter.SourceObservation(
        source_key=source_key, frontier=frontier, cycles=cycles, latest_created=latest_created
    )


def _baseline_snapshot() -> dict[str, alerter.SourceObservation]:
    return {
        "gfs": _obs(
            "gfs",
            frontier=datetime(2026, 8, 12, 18, 0, tzinfo=UTC),
            cycles=40,
            latest_created=datetime(2026, 8, 12, 23, 30, tzinfo=UTC),
        ),
        "ifs": _obs(
            "ifs",
            frontier=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            cycles=17,
            latest_created=datetime(2026, 8, 12, 22, 10, tzinfo=UTC),
        ),
    }


def _provider(snapshot: Mapping[str, alerter.SourceObservation]) -> alerter.ObservationProvider:
    def observe(_config: alerter.AlertConfig) -> dict[str, alerter.SourceObservation]:
        return dict(snapshot)

    return observe


def _failing_provider(error: Exception) -> alerter.ObservationProvider:
    def observe(_config: alerter.AlertConfig) -> dict[str, alerter.SourceObservation]:
        raise error

    return observe


class _MustNotHappen(BaseException):
    """Derived from BaseException on purpose: ``run_tick`` treats any
    ``Exception`` from the provider as a query failure, which would quietly
    absorb a guard violation instead of failing the test."""


def _exploding_provider() -> alerter.ObservationProvider:
    def observe(_config: alerter.AlertConfig):  # pragma: no cover - must not run
        raise _MustNotHappen("observation must not be attempted")

    return observe


def _tick(
    config: alerter.AlertConfig,
    *,
    now: datetime,
    snapshot: Mapping[str, alerter.SourceObservation] | None = None,
    observe: alerter.ObservationProvider | None = None,
    sendmail: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    provider = observe if observe is not None else _provider(snapshot or {})
    return alerter.run_tick(
        config,
        now=now,
        observe=provider,
        sendmail_runner=sendmail or FakeSendmail(),
        dry_run=dry_run,
    )


def _read_state(config: alerter.AlertConfig) -> alerter.AlertState:
    load = alerter.load_state(config.state_path)
    assert load.status == alerter.STATE_LOAD_OK
    assert load.state is not None
    return load.state


def _bootstrap(config: alerter.AlertConfig, snapshot, *, now: datetime = T0) -> None:
    sendmail = FakeSendmail()
    receipt = _tick(config, now=now, snapshot=snapshot, sendmail=sendmail)
    assert receipt["state_load"] == alerter.STATE_LOAD_MISSING
    assert sendmail.calls == []


# ---------------------------------------------------------------------------
# B1 — stalled snapshot alerts exactly once at the threshold.
# ---------------------------------------------------------------------------


def test_b1_stall_beyond_window_sends_exactly_one_alert(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    early = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=3, minutes=59), snapshot=snapshot, sendmail=early)
    assert early.calls == []
    assert receipt["stalled"] is False

    sendmail = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=sendmail)

    assert len(sendmail.calls) == 1
    argv, _message = sendmail.calls[0]
    assert argv == ["/usr/sbin/sendmail", "-t", "-i"]
    assert sendmail.header("To") == RECIPIENT
    assert sendmail.header("From") == SENDER
    assert alerter.EVENT_STALLED in sendmail.subject()
    assert sendmail.header("X-NHMS-Alert-Event") == alerter.EVENT_STALLED
    assert receipt["stalled"] is True
    assert receipt["stall_alert"] == "initial"
    assert receipt["emails"][0]["sent"] is True
    state = _read_state(config)
    assert state.alert_active is True
    assert state.last_alert_at == T0 + timedelta(hours=4)


# ---------------------------------------------------------------------------
# B2 — the four directional progress shapes reset the clock, silently.
# ---------------------------------------------------------------------------


def _advanced(shape: str) -> dict[str, alerter.SourceObservation]:
    snapshot = _baseline_snapshot()
    base = snapshot["gfs"]
    if shape == "frontier":
        snapshot["gfs"] = _obs(
            "gfs",
            frontier=base.frontier + timedelta(hours=6),
            cycles=base.cycles,
            latest_created=base.latest_created,
        )
    elif shape == "cycles":
        snapshot["gfs"] = _obs(
            "gfs", frontier=base.frontier, cycles=base.cycles + 1, latest_created=base.latest_created
        )
    elif shape == "latest_created":
        snapshot["gfs"] = _obs(
            "gfs",
            frontier=base.frontier,
            cycles=base.cycles,
            latest_created=base.latest_created + timedelta(minutes=20),
        )
    elif shape == "new_source":
        snapshot["cma"] = _obs(
            "cma",
            frontier=datetime(2026, 8, 12, 6, 0, tzinfo=UTC),
            cycles=1,
            latest_created=datetime(2026, 8, 12, 21, 0, tzinfo=UTC),
        )
    else:  # pragma: no cover - guard
        raise AssertionError(f"unknown shape {shape}")
    return snapshot


@pytest.mark.parametrize("shape", ["frontier", "cycles", "latest_created", "new_source"])
def test_b2_directional_progress_resets_clock_without_email(tmp_path: Path, shape: str) -> None:
    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())

    sendmail = FakeSendmail()
    now = T0 + timedelta(hours=5)
    receipt = _tick(config, now=now, snapshot=_advanced(shape), sendmail=sendmail)

    assert sendmail.calls == []
    assert receipt["progress"] is True
    assert receipt["stalled"] is False
    assert receipt["progress_reasons"]
    assert _read_state(config).last_change_at == now


def test_b2_null_source_id_group_is_kept_and_can_progress(tmp_path: Path) -> None:
    """A NULL ``source_id`` row is COALESCEd to ``__null_source__`` and must
    never be dropped — a dropped group is an invisible advance (under-report)."""

    config = _config(tmp_path)
    key = alerter.NULL_SOURCE_KEY
    first = {
        key: _obs(
            key,
            frontier=datetime(2026, 8, 12, 6, 0, tzinfo=UTC),
            cycles=3,
            latest_created=datetime(2026, 8, 12, 23, 0, tzinfo=UTC),
        )
    }
    _bootstrap(config, first)
    assert key in _read_state(config).snapshot

    sendmail = FakeSendmail()
    advanced = {
        key: _obs(
            key,
            frontier=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
            cycles=4,
            latest_created=datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
        )
    }
    receipt = _tick(config, now=T0 + timedelta(hours=5), snapshot=advanced, sendmail=sendmail)

    assert sendmail.calls == []
    assert receipt["progress"] is True
    assert key in receipt["baseline"]


# ---------------------------------------------------------------------------
# B2b — directional negatives. None of these is progress.
# ---------------------------------------------------------------------------


def _decreased_cycles() -> dict[str, alerter.SourceObservation]:
    snapshot = _baseline_snapshot()
    base = snapshot["gfs"]
    snapshot["gfs"] = _obs(
        "gfs", frontier=base.frontier, cycles=base.cycles - 5, latest_created=base.latest_created
    )
    return snapshot


def _source_disappeared() -> dict[str, alerter.SourceObservation]:
    snapshot = _baseline_snapshot()
    del snapshot["gfs"]
    return snapshot


@pytest.mark.parametrize(
    "label, snapshot_factory",
    [
        ("cycles_decrease", _decreased_cycles),
        ("source_disappeared", _source_disappeared),
        # A lifecycle transition inside ('succeeded','parsed','published')
        # touches the same row of the same cycle: identical markers.
        ("in_set_status_transition", _baseline_snapshot),
    ],
)
def test_b2b_non_progress_shapes_do_not_reset_the_stall_clock(
    tmp_path: Path, label: str, snapshot_factory
) -> None:
    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())

    sendmail = FakeSendmail()
    receipt = _tick(
        config, now=T0 + timedelta(hours=5), snapshot=snapshot_factory(), sendmail=sendmail
    )

    assert receipt["progress"] is False, label
    assert receipt["stalled"] is True, label
    assert len(sendmail.calls) == 1
    assert alerter.EVENT_STALLED in sendmail.subject()
    state = _read_state(config)
    assert state.last_change_at == T0
    # Baseline never lowers, and a vanished source keeps its high-water entry.
    assert state.snapshot["gfs"].cycles == 40
    assert state.snapshot["gfs"].frontier == datetime(2026, 8, 12, 18, 0, tzinfo=UTC)


def test_b2b_decrease_then_recover_is_not_progress(tmp_path: Path) -> None:
    """``succeeded -> failed -> parsed`` leaves and re-enters the lifecycle set.

    If the baseline tracked the last observation instead of the high-water
    mark, the re-entry would read as a strict increase and would reset the
    stall clock in the middle of a real outage (under-report direction)."""

    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())

    dropped = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=1), snapshot=_decreased_cycles(), sendmail=dropped)
    assert dropped.calls == []
    assert _read_state(config).snapshot["gfs"].cycles == 40

    restored = FakeSendmail()
    receipt = _tick(
        config, now=T0 + timedelta(hours=5), snapshot=_baseline_snapshot(), sendmail=restored
    )

    assert receipt["progress"] is False
    assert receipt["stalled"] is True
    assert len(restored.calls) == 1
    assert _read_state(config).last_change_at == T0


def test_b2b_observation_query_excludes_non_post_ingest_statuses() -> None:
    """The snapshot contract: ``failed``/``cancelled``/``superseded``/``pending``
    rows never enter an observation. Enforced by the SQL filter, so it is
    pinned on the query text + the status constants the provider is built on."""

    assert alerter.POST_INGEST_STATUSES == ("succeeded", "parsed", "published")
    query = " ".join(alerter.OBSERVATION_QUERY.split())
    assert "WHERE cycle_time IS NOT NULL" in query
    assert "AND status IN ('succeeded','parsed','published')" in query
    assert "COALESCE(source_id, '__null_source__') AS source_key" in query
    assert "max(cycle_time) AS frontier" in query
    assert "count(DISTINCT cycle_time) AS cycles" in query
    assert "max(created_at) AS latest_created" in query
    for status in alerter.EXCLUDED_STATUSES:
        assert f"'{status}'" not in query, status


def test_observation_query_is_read_only() -> None:
    """The lane runs under the read-only display role; the only statement it
    issues must be a SELECT."""

    query = alerter.OBSERVATION_QUERY.strip().upper()
    assert query.startswith("SELECT")
    tokens = set(re.findall(r"[A-Z_]+", query))
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT"):
        assert verb not in tokens, verb


# ---------------------------------------------------------------------------
# B3 — resend window.
# ---------------------------------------------------------------------------


def test_b3_resend_only_after_the_resend_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    first = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=first)
    assert len(first.calls) == 1

    quiet = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=9, minutes=59), snapshot=snapshot, sendmail=quiet)
    assert quiet.calls == []
    assert receipt["stalled"] is True
    assert receipt["stall_alert"] is None

    resend = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=10), snapshot=snapshot, sendmail=resend)
    assert len(resend.calls) == 1
    assert receipt["stall_alert"] == "resend"
    assert alerter.EVENT_STALLED in resend.subject()


# ---------------------------------------------------------------------------
# B4 — recovery closes the loop exactly once and re-arms the full window.
# ---------------------------------------------------------------------------


def test_b4_recovery_sends_one_email_and_rearms_the_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=FakeSendmail())
    assert _read_state(config).alert_active is True

    recovery = FakeSendmail()
    resumed = _advanced("frontier")
    receipt = _tick(config, now=T0 + timedelta(hours=5), snapshot=resumed, sendmail=recovery)

    assert len(recovery.calls) == 1
    assert alerter.EVENT_RECOVERED in recovery.subject()
    assert receipt["recovered"] is True
    state = _read_state(config)
    assert state.alert_active is False
    assert state.last_alert_at is None

    # A second progress-free tick right after recovery must stay quiet, and a
    # new stall needs the full window measured from the recovery.
    quiet = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=8, minutes=59), snapshot=resumed, sendmail=quiet)
    assert quiet.calls == []

    restall = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=9), snapshot=resumed, sendmail=restall)
    assert len(restall.calls) == 1
    assert receipt["stall_alert"] == "initial"


# ---------------------------------------------------------------------------
# B5 — state survives a restart; the write is atomic.
# ---------------------------------------------------------------------------


def test_b5_state_round_trips_across_instances(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=FakeSendmail())

    # A fresh config object (new process) reads back the identical state.
    reloaded = alerter.load_state(_config(tmp_path).state_path)
    assert reloaded.status == alerter.STATE_LOAD_OK
    assert reloaded.state is not None
    assert reloaded.state.alert_active is True
    assert reloaded.state.last_change_at == T0
    assert reloaded.state.snapshot["gfs"].cycles == 40
    assert reloaded.state.schema_version == alerter.STATE_SCHEMA_VERSION


def test_b5_interrupted_write_leaves_the_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())
    before = config.state_path.read_bytes()

    def _boom(src, dst):  # noqa: ANN001 - monkeypatched os.replace
        raise OSError("simulated crash between tmp write and rename")

    monkeypatch.setattr(alerter.os, "replace", _boom)
    state = _read_state(config)
    state.alert_active = True
    with pytest.raises(OSError):
        alerter.write_state(config.state_path, state)

    assert config.state_path.read_bytes() == before
    monkeypatch.undo()
    assert _read_state(config).alert_active is False


# ---------------------------------------------------------------------------
# B6 — corrupt state over-reports; missing state bootstraps silently.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "{not json at all",
        json.dumps({"schema_version": 99, "snapshot": {}, "last_change_at": "2026-08-13T00:00:00+00:00"}),
    ],
    ids=["unparsable", "schema_mismatch"],
)
def test_b6_corrupt_state_alerts_and_rebuilds_the_baseline(tmp_path: Path, payload: str) -> None:
    config = _config(tmp_path)
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(payload, encoding="utf-8")

    sendmail = FakeSendmail()
    now = T0 + timedelta(hours=2)
    receipt = _tick(config, now=now, snapshot=_baseline_snapshot(), sendmail=sendmail)

    assert receipt["state_load"] == alerter.STATE_LOAD_CORRUPT
    assert len(sendmail.calls) == 1
    assert alerter.DEGRADED_STATE_CORRUPT in sendmail.subject()
    assert alerter.EVENT_DEGRADED in sendmail.subject()
    assert receipt["degraded_events"] == [alerter.DEGRADED_STATE_CORRUPT]
    assert receipt["baseline_reset_at"] == now.isoformat()
    assert receipt["baseline_established_at"] is None
    state = _read_state(config)
    assert state.baseline_reset_at == now
    assert state.snapshot["gfs"].cycles == 40
    # The rebuild is honest: the clock restarts from now and is recorded.
    assert state.last_change_at == now


def test_b6_missing_state_bootstraps_silently(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sendmail = FakeSendmail()

    receipt = _tick(config, now=T0, snapshot=_baseline_snapshot(), sendmail=sendmail)

    assert receipt["state_load"] == alerter.STATE_LOAD_MISSING
    assert sendmail.calls == []
    assert receipt["degraded_events"] == []
    assert receipt["baseline_established_at"] == T0.isoformat()
    assert receipt["baseline_reset_at"] is None
    state = _read_state(config)
    assert state.baseline_established_at == T0
    assert state.snapshot["ifs"].cycles == 17


# ---------------------------------------------------------------------------
# B7 — query failures never buy the frontier extra time.
# ---------------------------------------------------------------------------


def test_b7_query_failures_keep_the_stall_clock_and_raise_observability(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())
    broken = _failing_provider(RuntimeError(f"connection failed for {DSN}"))

    first = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=1), observe=broken, sendmail=first)
    assert first.calls == []
    assert receipt["consecutive_query_failures"] == 1
    assert receipt["observation_status"] == "failed"

    second = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=2), observe=broken, sendmail=second)
    assert len(second.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in second.subject()
    assert receipt["consecutive_query_failures"] == 2

    third = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=3), observe=broken, sendmail=third)
    assert third.calls == []  # 6 h dedup on the degraded family
    assert receipt["consecutive_query_failures"] == 3

    # The stall clock never moved: crossing the window while blind still fires.
    stalled = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=5), observe=broken, sendmail=stalled)
    assert len(stalled.calls) == 1
    assert alerter.EVENT_STALLED in stalled.subject()
    assert receipt["last_change_at"] == T0.isoformat()
    assert _read_state(config).last_change_at == T0


# ---------------------------------------------------------------------------
# B8 — missing configuration fails closed before anything happens.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["DATABASE_URL", "NHMS_ALERT_EMAIL_TO"])
def test_b8_missing_env_is_a_structured_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    env = _env(tmp_path)
    env.pop(missing)
    sendmail = FakeSendmail()

    code = main_with(env, now=T0, observe=_exploding_provider(), sendmail=sendmail)

    assert code == 2
    assert sendmail.calls == []
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["status"] == "failed"
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert missing in payload["reason"]
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "receipts").exists()


def main_with(env: Mapping[str, str], *, now: datetime, observe, sendmail, argv=None) -> int:
    return alerter.main(
        argv if argv is not None else ["--once"],
        now=now,
        observe=observe,
        sendmail_runner=sendmail,
        env=env,
    )


# ---------------------------------------------------------------------------
# B9 — no DSN password on any outlet, under failure injection.
# ---------------------------------------------------------------------------


def test_b9_no_dsn_password_reaches_any_outlet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    _bootstrap(config, _baseline_snapshot())

    # Failure injection 1+2: a psycopg2-shaped error that echoes the whole
    # conninfo, and a sendmail failure whose stderr echoes it too.
    broken = _failing_provider(
        RuntimeError(
            f'could not connect: dsn={DSN} password={DSN_PASSWORD} '
            'FATAL: password authentication failed for user "nhms_display_ro"'
        )
    )
    leaky_sendmail = FakeSendmail(
        [
            alerter.SendResult(returncode=75, error=f"deferred: local delivery agent saw {DSN}"),
            alerter.SendResult(returncode=75, error=f"deferred: local delivery agent saw {DSN}"),
        ]
    )
    main_with(env, now=T0 + timedelta(hours=1), observe=broken, sendmail=leaky_sendmail)
    code = main_with(env, now=T0 + timedelta(hours=5), observe=broken, sendmail=leaky_sendmail)
    assert code == 1

    # Failure injection 3: corrupt state on top of the failing query.
    config.state_path.write_text("{corrupt", encoding="utf-8")
    main_with(env, now=T0 + timedelta(hours=6), observe=broken, sendmail=leaky_sendmail)

    logs = capsys.readouterr()
    outlets = {
        "email": "\n".join(leaky_sendmail.messages),
        "log_stdout": logs.out,
        "log_stderr": logs.err,
        "receipt": config.receipt_path.read_text(encoding="utf-8"),
        "state": config.state_path.read_text(encoding="utf-8"),
        "events": config.events_path.read_text(encoding="utf-8"),
    }
    for name, text in outlets.items():
        assert DSN_PASSWORD not in text, f"DSN password leaked into {name}"
        assert DSN not in text, f"DSN leaked into {name}"
    # Redaction happened rather than the text simply being absent.
    assert "***" in outlets["receipt"]
    assert "***" in outlets["email"]
    assert "***" in outlets["state"]
    assert "***" in outlets["events"]


# ---------------------------------------------------------------------------
# B10 — a failed send is not a delivered alert.
# ---------------------------------------------------------------------------


def test_b10_failed_send_is_not_recorded_and_retries_next_tick(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    failing = FakeSendmail([alerter.SendResult(returncode=1, error="sendmail: cannot connect")])
    receipt = _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=failing)

    assert len(failing.calls) == 1
    assert receipt["emails"][0]["sent"] is False
    assert receipt["emails"][0]["returncode"] == 1
    assert receipt["send_failures"] == 1
    assert receipt["status"] == "degraded"
    assert alerter.receipt_exit_code(receipt) == 1
    state = _read_state(config)
    assert state.alert_active is True
    assert state.last_alert_at is None

    retry = FakeSendmail()
    now = T0 + timedelta(hours=4, minutes=30)
    receipt = _tick(config, now=now, snapshot=snapshot, sendmail=retry)

    assert len(retry.calls) == 1
    assert alerter.EVENT_STALLED in retry.subject()
    assert receipt["emails"][0]["sent"] is True
    assert _read_state(config).last_alert_at == now
    # B18: the retry is still the FIRST delivered alert of this stall. Labeling
    # it "resend" would tell the operator a mail they never got had gone out.
    assert receipt["stall_alert"] == "initial"
    assert receipt["emails"][0]["detail"] == "initial"

    # The next one, now that a mail was actually delivered, is a resend.
    later = FakeSendmail()
    receipt = _tick(config, now=now + timedelta(hours=6), snapshot=snapshot, sendmail=later)
    assert len(later.calls) == 1
    assert receipt["stall_alert"] == "resend"


# ---------------------------------------------------------------------------
# B12 — --dry-run evaluates everything and touches nothing.
# ---------------------------------------------------------------------------


def test_b12_dry_run_has_zero_side_effects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=FakeSendmail())
    capsys.readouterr()

    before = {
        path: path.read_bytes()
        for path in (config.state_path, config.receipt_path, config.events_path)
    }

    sendmail = ExplodingSendmail()
    code = main_with(
        env,
        now=T0 + timedelta(hours=10),
        observe=_provider(snapshot),
        sendmail=sendmail,
        argv=["--once", "--dry-run"],
    )

    assert code == 0
    for path, payload in before.items():
        assert path.read_bytes() == payload, path
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["dry_run"] is True
    assert printed["stalled"] is True
    assert printed["emails"][0]["event"] == alerter.EVENT_STALLED
    assert printed["emails"][0]["dry_run"] is True
    assert printed["emails"][0]["sent"] is False


# ---------------------------------------------------------------------------
# B13 — single-instance mutex.
# ---------------------------------------------------------------------------


def test_b13_second_instance_is_a_structured_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    held = alerter.acquire_lock(config.lock_path)
    assert held is not None
    try:
        code = main_with(
            env, now=T0, observe=_exploding_provider(), sendmail=ExplodingSendmail()
        )
    finally:
        alerter.release_lock(held)

    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "skipped"
    assert payload["code"] == alerter.CODE_CONCURRENT_INVOCATION
    assert not config.state_path.exists()
    assert not config.receipt_path.exists()
    assert not config.events_path.exists()


def test_b13_lock_is_released_so_the_next_tick_runs(tmp_path: Path) -> None:
    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    sendmail = FakeSendmail()

    assert main_with(env, now=T0, observe=_provider(_baseline_snapshot()), sendmail=sendmail) == 0
    assert config.state_path.exists()

    fd = alerter.acquire_lock(config.lock_path)
    assert fd is not None
    alerter.release_lock(fd)


# ---------------------------------------------------------------------------
# B14 — byte-level and parser-resource state corruption stay contained.
# ---------------------------------------------------------------------------


def _deeply_nested_json(depth: int = 100_000) -> str:
    return "[" * depth + "]" * depth


@pytest.mark.parametrize(
    "writer",
    [
        lambda path: path.write_bytes(b"\xff\xfe\x00\x80garbage"),
        lambda path: path.write_text(_deeply_nested_json(), encoding="utf-8"),
    ],
    ids=["non_utf8_bytes", "pathological_nesting"],
)
def test_b14_byte_and_parser_corruption_land_in_the_corrupt_branch(tmp_path: Path, writer) -> None:
    """``UnicodeDecodeError`` is a ValueError (not an OSError) and
    ``RecursionError`` is a RuntimeError: neither is caught by a naive
    OSError/JSONDecodeError pair, and either escaping means the alerter goes
    silent exactly when its own state is damaged."""

    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())
    writer(config.state_path)

    load = alerter.load_state(config.state_path)
    assert load.status == alerter.STATE_LOAD_CORRUPT
    assert load.state is None

    sendmail = FakeSendmail()
    now = T0 + timedelta(hours=1)
    receipt = _tick(config, now=now, snapshot=_baseline_snapshot(), sendmail=sendmail)

    assert receipt["state_load"] == alerter.STATE_LOAD_CORRUPT
    assert receipt["degraded_events"] == [alerter.DEGRADED_STATE_CORRUPT]
    assert len(sendmail.calls) == 1
    state = _read_state(config)
    assert state.baseline_reset_at == now
    assert state.snapshot["gfs"].cycles == 40


def test_b14_corruption_is_contained_end_to_end_through_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    _bootstrap(config, _baseline_snapshot())
    config.state_path.write_bytes(b"\xff\xfe\x00\x80garbage")

    sendmail = FakeSendmail()
    code = main_with(
        env, now=T0 + timedelta(hours=1), observe=_provider(_baseline_snapshot()), sendmail=sendmail
    )

    assert code == 1  # degraded, but a controlled degraded
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["degraded_events"] == [alerter.DEGRADED_STATE_CORRUPT]
    assert "Traceback" not in printed.get("status", "")
    assert len(sendmail.calls) == 1
    # The state is usable again on the very next tick.
    assert alerter.load_state(config.state_path).status == alerter.STATE_LOAD_OK


# ---------------------------------------------------------------------------
# B15 — every blocking call is bounded.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[str] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.closed = False

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_b15_default_observe_passes_a_bounded_connect_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unbounded connect would let the watchdog hang the way the pass it
    watches hung. Pinned at the call site, not only on the constant."""

    import sys
    import types

    cursor = _FakeCursor(
        [
            {
                "source_key": "gfs",
                "frontier": datetime(2026, 8, 12, 18, 0, tzinfo=UTC),
                "cycles": 40,
                "latest_created": datetime(2026, 8, 12, 23, 30, tzinfo=UTC),
            }
        ]
    )
    connection = _FakeConnection(cursor)
    recorded: dict[str, Any] = {}

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_extras = types.ModuleType("psycopg2.extras")
    fake_extras.RealDictCursor = object  # type: ignore[attr-defined]

    def _connect(dsn: str, **kwargs: Any) -> _FakeConnection:
        recorded["dsn"] = dsn
        recorded.update(kwargs)
        return connection

    fake_psycopg2.connect = _connect  # type: ignore[attr-defined]
    fake_psycopg2.extras = fake_extras  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)

    config = _config(tmp_path)
    snapshot = alerter.default_observe(config)

    assert recorded["dsn"] == DSN
    assert recorded["connect_timeout"] == alerter.CONNECT_TIMEOUT_SEC
    assert 0 < alerter.CONNECT_TIMEOUT_SEC <= 60
    assert f"SET statement_timeout = {alerter.QUERY_TIMEOUT_MS}" in cursor.executed
    assert 0 < alerter.QUERY_TIMEOUT_MS <= 300_000
    assert connection.closed is True
    assert snapshot["gfs"].cycles == 40


# ---------------------------------------------------------------------------
# B16 — a baseline is only ever established from a real observation.
# ---------------------------------------------------------------------------


def test_b16_corrupt_plus_failed_observation_leaves_the_baseline_pending(tmp_path: Path) -> None:
    """Verifier probe geometry A-C1. If the empty rebuild baseline were
    persisted as live, the next successful observation would read as four new
    sources = progress, pushing the stall clock out by the whole outage."""

    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    stall = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=stall)
    assert len(stall.calls) == 1
    assert _read_state(config).alert_active is True

    # T0+5h: state corrupt AND the observation fails on the same tick.
    config.state_path.write_text("{corrupt", encoding="utf-8")
    blind = FakeSendmail()
    rebuild_at = T0 + timedelta(hours=5)
    receipt = _tick(
        config,
        now=rebuild_at,
        observe=_failing_provider(RuntimeError("db down")),
        sendmail=blind,
    )

    assert receipt["state_load"] == alerter.STATE_LOAD_CORRUPT
    assert receipt["baseline_pending"] is True
    assert receipt["baseline_reset_at"] is None
    assert receipt["baseline_established_at"] is None
    assert len(blind.calls) == 1  # the monitoring-degraded mail still goes out
    state = _read_state(config)
    assert state.baseline_pending is True
    assert state.baseline_pending_kind == alerter.BASELINE_ORIGIN_RESET
    assert state.snapshot == {}
    assert state.last_change_at == rebuild_at

    # T0+6h: the observation comes back with EXACTLY the pre-corruption data.
    fill_at = T0 + timedelta(hours=6)
    fill = FakeSendmail()
    receipt = _tick(config, now=fill_at, snapshot=snapshot, sendmail=fill)

    assert fill.calls == []
    assert receipt["progress"] is False
    assert receipt["baseline_filled"] is True
    assert receipt["baseline_pending"] is False
    state = _read_state(config)
    assert state.last_change_at == rebuild_at  # clock untouched by the fill
    assert state.snapshot["gfs"].cycles == 40
    # B24: the fill stamps the field matching the ORIGIN. A corruption rebuild
    # that stamped ``baseline_established_at`` would read forever after as a
    # fresh installation instead of a recorded degradation.
    assert state.baseline_reset_at == fill_at
    assert state.baseline_established_at is None
    assert receipt["baseline_reset_at"] == fill_at.isoformat()
    assert receipt["baseline_established_at"] is None

    # The stall clock therefore runs from the rebuild point: T0+9h, not T0+10h.
    quiet = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=8, minutes=59), snapshot=snapshot, sendmail=quiet)
    assert quiet.calls == []

    fires = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=9), snapshot=snapshot, sendmail=fires)
    assert len(fires.calls) == 1
    assert alerter.EVENT_STALLED in fires.subject()
    assert receipt["stall_alert"] == "initial"


def test_b16_bootstrap_plus_failed_observation_is_pending_and_silent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    blind = FakeSendmail()

    receipt = _tick(
        config, now=T0, observe=_failing_provider(RuntimeError("db down")), sendmail=blind
    )

    assert blind.calls == []  # a fresh install is not a degradation
    assert receipt["state_load"] == alerter.STATE_LOAD_MISSING
    assert receipt["baseline_pending"] is True
    assert receipt["baseline_established_at"] is None
    state = _read_state(config)
    assert state.snapshot == {}
    assert state.baseline_pending is True
    assert state.baseline_pending_kind == alerter.BASELINE_ORIGIN_BOOTSTRAP

    fill_at = T0 + timedelta(hours=1)
    fill = FakeSendmail()
    receipt = _tick(config, now=fill_at, snapshot=_baseline_snapshot(), sendmail=fill)

    assert fill.calls == []
    assert receipt["progress"] is False
    assert receipt["baseline_filled"] is True
    state = _read_state(config)
    assert state.baseline_pending is False
    assert state.baseline_established_at == fill_at
    assert state.baseline_reset_at is None  # B24 dual: bootstrap origin
    assert state.last_change_at == T0  # the pending fill never moves the clock
    assert state.snapshot["ifs"].cycles == 17


# ---------------------------------------------------------------------------
# B17 — a degraded alert whose send failed is retried, once.
# ---------------------------------------------------------------------------


def test_b17_failed_degraded_send_is_persisted_and_retried(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    config.state_path.write_text("{corrupt", encoding="utf-8")
    failing = FakeSendmail([alerter.SendResult(returncode=1, error="sendmail: queue full")])
    receipt = _tick(config, now=T0 + timedelta(hours=1), snapshot=snapshot, sendmail=failing)

    assert len(failing.calls) == 1
    assert receipt["emails"][0]["sent"] is False
    assert receipt["degraded_pending"] == [
        {"kind": alerter.DEGRADED_STATE_CORRUPT, "reason": receipt["state_load_reason"]}
    ]
    state = _read_state(config)
    assert state.last_degraded_alert_by_kind == {}
    assert [entry["kind"] for entry in state.degraded_pending] == [alerter.DEGRADED_STATE_CORRUPT]

    # A --dry-run tick in between drains nothing: no send, state untouched,
    # and the receipt still reports the queue as pending.
    before = config.state_path.read_bytes()
    dry_receipt = _tick(
        config,
        now=T0 + timedelta(hours=1, minutes=10),
        snapshot=snapshot,
        sendmail=ExplodingSendmail(),  # raises if the dry run tries to send
        dry_run=True,
    )
    assert dry_receipt["emails"][0]["dry_run"] is True
    assert dry_receipt["degraded_pending"] == [
        {"kind": alerter.DEGRADED_STATE_CORRUPT, "reason": receipt["state_load_reason"]}
    ]
    assert config.state_path.read_bytes() == before

    retry_at = T0 + timedelta(hours=1, minutes=30)
    retry = FakeSendmail()
    receipt = _tick(config, now=retry_at, snapshot=snapshot, sendmail=retry)

    assert len(retry.calls) == 1
    assert alerter.DEGRADED_STATE_CORRUPT in retry.subject()
    assert "retry" in retry.messages[0]
    assert receipt["state_load"] == alerter.STATE_LOAD_OK  # the retry is not a new corruption
    assert receipt["degraded_pending"] == []
    state = _read_state(config)
    assert state.last_degraded_alert_by_kind == {alerter.DEGRADED_STATE_CORRUPT: retry_at}
    assert state.degraded_pending == []

    # No duplicate inside the 6 h dedup window, and nothing left pending.
    quiet = FakeSendmail()
    receipt = _tick(config, now=retry_at + timedelta(hours=1), snapshot=snapshot, sendmail=quiet)
    assert quiet.calls == []
    assert receipt["degraded_events"] == []


def test_b17_throttled_pending_degraded_survives_until_the_window_opens(tmp_path: Path) -> None:
    """A pending retry that lands inside the dedup window is not dropped.

    Reachable geometry: the resend window is re-read from the environment on
    every tick, so an operator widening 6 h -> 24 h can put an already-queued
    retry back inside a throttle window. The queued alert must survive that,
    not be absorbed."""

    snapshot = _baseline_snapshot()
    narrow = _config(tmp_path, NHMS_FRONTIER_STALL_HOURS="48")
    _bootstrap(narrow, snapshot)
    broken = _failing_provider(RuntimeError("db down"))

    _tick(narrow, now=T0 + timedelta(hours=1), observe=broken, sendmail=FakeSendmail())
    delivered = FakeSendmail()
    armed_at = T0 + timedelta(hours=2)
    _tick(narrow, now=armed_at, observe=broken, sendmail=delivered)
    assert len(delivered.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in delivered.subject()

    # Window elapsed -> the repeat is due, and its send fails -> queued.
    failing = FakeSendmail([alerter.SendResult(returncode=1, error="queue full")])
    _tick(narrow, now=armed_at + timedelta(hours=7), observe=broken, sendmail=failing)
    assert len(failing.calls) == 1
    assert [entry["kind"] for entry in _read_state(narrow).degraded_pending] == [
        alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE
    ]

    # The operator widens the resend window; the queued retry is now inside it.
    widened = _config(tmp_path, NHMS_FRONTIER_STALL_HOURS="48", NHMS_FRONTIER_RESEND_HOURS="24")
    throttled = FakeSendmail()
    _tick(widened, now=armed_at + timedelta(hours=8), snapshot=snapshot, sendmail=throttled)
    assert throttled.calls == []
    assert [entry["kind"] for entry in _read_state(widened).degraded_pending] == [
        alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE
    ], "a throttled pending retry must not be dropped"

    opened = FakeSendmail()
    _tick(widened, now=armed_at + timedelta(hours=25), snapshot=snapshot, sendmail=opened)
    assert len(opened.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in opened.subject()
    assert _read_state(widened).degraded_pending == []


def test_b28_legacy_scalar_clock_is_not_migrated_and_never_suppresses(tmp_path: Path) -> None:
    """A round-1 scalar cannot be attributed to a kind. Fanning it onto both
    kinds would re-create the cross-kind suppression the per-kind clock exists
    to remove; dropping it costs at most one duplicate mail."""

    config = _config(tmp_path, NHMS_FRONTIER_STALL_HOURS="48")
    snapshot = _baseline_snapshot()
    armed_at = T0 + timedelta(hours=1)
    legacy_state = {
        "schema_version": 1,
        "snapshot": alerter.snapshot_to_json(snapshot),
        "last_change_at": T0.isoformat(),
        "alert_active": False,
        "last_alert_at": None,
        "consecutive_query_failures": 0,
        "baseline_established_at": T0.isoformat(),
        "baseline_reset_at": None,
        # Round-1 shape: ONE scalar clock for the whole degraded family.
        "last_degraded_alert_at": armed_at.isoformat(),
        "last_error": None,
        "baseline_pending": False,
        "degraded_pending": [],
    }
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

    loaded = _read_state(config)
    assert loaded.last_degraded_alert_by_kind == {}
    # design.md D1: baseline_pending_kind absent on a legacy state defaults to
    # bootstrap, and the state stays readable (schema_version unchanged).
    assert loaded.baseline_pending_kind == alerter.BASELINE_ORIGIN_BOOTSTRAP
    assert loaded.schema_version == alerter.STATE_SCHEMA_VERSION

    broken = _failing_provider(RuntimeError("db down"))
    _tick(config, now=armed_at + timedelta(minutes=30), observe=broken, sendmail=FakeSendmail())
    crossed = FakeSendmail()
    receipt = _tick(config, now=armed_at + timedelta(hours=1), observe=broken, sendmail=crossed)

    # Exactly one — not zero (suppressed by a migrated scalar) and not two.
    assert len(crossed.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in crossed.subject()
    assert receipt["degraded_events"] == [alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE]


# ---------------------------------------------------------------------------
# B22 — containment is whole-class, not an enumeration.
# ---------------------------------------------------------------------------


def test_b22_extreme_timestamp_state_self_heals_instead_of_dying_every_tick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``9999-12-31T23:59:59-14:00`` parses fine and then raises ``OverflowError``
    inside ``astimezone`` — neither an OSError/ValueError nor a RecursionError.
    Under an enumerated catch it escapes, so the alerter dies on every tick,
    mails nothing and never rewrites the state that is killing it: permanent
    silence."""

    env = _env(tmp_path)
    config = alerter.config_from_env(env)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    poisoned = json.loads(config.state_path.read_text(encoding="utf-8"))
    poisoned["last_change_at"] = "9999-12-31T23:59:59-14:00"
    config.state_path.write_text(json.dumps(poisoned), encoding="utf-8")

    load = alerter.load_state(config.state_path)
    assert load.status == alerter.STATE_LOAD_CORRUPT
    assert "OverflowError" in (load.reason or "")

    sendmail = FakeSendmail()
    heal_at = T0 + timedelta(hours=1)
    code = main_with(env, now=heal_at, observe=_provider(snapshot), sendmail=sendmail)

    assert code == 1  # degraded, controlled
    assert len(sendmail.calls) == 1
    assert alerter.DEGRADED_STATE_CORRUPT in sendmail.subject()
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["degraded_events"] == [alerter.DEGRADED_STATE_CORRUPT]

    # Self-healed: the poisoned state was REWRITTEN, so the next tick is normal.
    state = _read_state(config)
    assert state.last_change_at == heal_at
    assert state.baseline_reset_at == heal_at
    next_tick = FakeSendmail()
    receipt = _tick(config, now=heal_at + timedelta(minutes=30), snapshot=snapshot, sendmail=next_tick)
    assert next_tick.calls == []
    assert receipt["state_load"] == alerter.STATE_LOAD_OK


@pytest.mark.parametrize(
    "poison",
    [
        {"last_alert_at": "9999-12-31T23:59:59-14:00"},
        {"snapshot": {"gfs": {"frontier": "9999-12-31T23:59:59-14:00", "cycles": 1,
                              "latest_created": "2026-08-12T23:00:00+00:00"}}},
        {"baseline_established_at": "0001-01-01T00:00:00+14:00"},
    ],
    ids=["last_alert_at", "snapshot_marker", "baseline_stamp"],
)
def test_b22_every_timestamp_field_is_contained(tmp_path: Path, poison: dict[str, Any]) -> None:
    """The overflow is not special to one field — containment must be at the
    stage, not per field."""

    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())
    payload = json.loads(config.state_path.read_text(encoding="utf-8"))
    payload.update(poison)
    config.state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert alerter.load_state(config.state_path).status == alerter.STATE_LOAD_CORRUPT

    sendmail = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=1), snapshot=_baseline_snapshot(), sendmail=sendmail)
    assert receipt["degraded_events"] == [alerter.DEGRADED_STATE_CORRUPT]
    assert len(sendmail.calls) == 1


# ---------------------------------------------------------------------------
# B23 — the degraded dedup clock is per kind.
# ---------------------------------------------------------------------------


def test_b23_state_corrupt_mail_does_not_suppress_the_first_observability_mail(
    tmp_path: Path,
) -> None:
    """One shared clock let a delivered state-corrupt mail throttle the FIRST
    EVER observability-unavailable — an entire event class the operator had
    never seen, lost for a whole resend window."""

    config = _config(tmp_path, NHMS_FRONTIER_STALL_HOURS="48")
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    corrupt_at = T0 + timedelta(hours=1)
    config.state_path.write_text("{corrupt", encoding="utf-8")
    corrupt_mail = FakeSendmail()
    _tick(config, now=corrupt_at, snapshot=snapshot, sendmail=corrupt_mail)
    assert len(corrupt_mail.calls) == 1
    assert alerter.DEGRADED_STATE_CORRUPT in corrupt_mail.subject()

    broken = _failing_provider(RuntimeError("db down"))
    first_failure = FakeSendmail()
    _tick(config, now=corrupt_at + timedelta(minutes=30), observe=broken, sendmail=first_failure)
    assert first_failure.calls == []  # budget is 2 ticks

    # Budget crossed only 1 h after the state-corrupt mail: well inside the 6 h
    # window of the OTHER kind, and it must still go out.
    budget_crossed = FakeSendmail()
    receipt = _tick(config, now=corrupt_at + timedelta(hours=1), observe=broken, sendmail=budget_crossed)
    assert len(budget_crossed.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in budget_crossed.subject()
    assert receipt["degraded_events"] == [alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE]

    clocks = _read_state(config).last_degraded_alert_by_kind
    assert set(clocks) == {alerter.DEGRADED_STATE_CORRUPT, alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE}

    # Same-kind repeat inside its own window is still deduped.
    repeat = FakeSendmail()
    receipt = _tick(config, now=corrupt_at + timedelta(hours=2), observe=broken, sendmail=repeat)
    assert repeat.calls == []
    assert receipt["degraded_events"] == []
    assert receipt["consecutive_query_failures"] == 3

    # ...and released once that kind's own window elapses.
    reopened = FakeSendmail()
    _tick(config, now=corrupt_at + timedelta(hours=7), observe=broken, sendmail=reopened)
    assert len(reopened.calls) == 1
    assert alerter.DEGRADED_OBSERVABILITY_UNAVAILABLE in reopened.subject()


# ---------------------------------------------------------------------------
# B19 — header-breaking recipient/sender configuration fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["NHMS_ALERT_EMAIL_TO", "NHMS_ALERT_EMAIL_FROM"])
@pytest.mark.parametrize(
    "value",
    [
        "ops@example.invalid\r\nBcc: attacker@example.invalid",
        "ops@example.invalid\n",
        "ops@example.invalid\r",
    ],
    ids=["injected_header", "trailing_lf", "trailing_cr"],
)
def test_b19_crlf_in_addresses_is_rejected_at_config_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str, value: str
) -> None:
    env = _env(tmp_path, **{key: value})
    sendmail = FakeSendmail()

    code = main_with(env, now=T0, observe=_exploding_provider(), sendmail=sendmail)

    assert code == 2
    assert sendmail.calls == []
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert key in payload["reason"]


# ---------------------------------------------------------------------------
# B20 — an unusable lock path is a config error, not a traceback.
# ---------------------------------------------------------------------------


def test_b20_unwritable_lock_directory_is_a_structured_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readonly_root = tmp_path / "readonly"
    readonly_root.mkdir()
    env = _env(tmp_path, NHMS_FRONTIER_STATE_PATH=str(readonly_root / "nested" / "state.json"))
    readonly_root.chmod(0o500)
    sendmail = FakeSendmail()
    try:
        code = main_with(env, now=T0, observe=_exploding_provider(), sendmail=sendmail)
    finally:
        readonly_root.chmod(0o700)

    assert code == 2
    assert sendmail.calls == []
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert "lock" in payload["reason"]


def test_b20_lock_path_under_a_regular_file_is_a_structured_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    env = _env(tmp_path, NHMS_FRONTIER_STATE_PATH=str(blocker / "state.json"))
    sendmail = FakeSendmail()

    code = main_with(env, now=T0, observe=_exploding_provider(), sendmail=sendmail)

    assert code == 2
    assert sendmail.calls == []
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID


# ---------------------------------------------------------------------------
# B25 — wrapper contract (real bash subprocess).
# Sibling precedent: tests/test_node27_timeseries_retention.py wrapper section.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WRAPPER_PATH = _REPO_ROOT / "scripts/node27_frontier_stall_alert_once.sh"
_SERVICE_PATH = _REPO_ROOT / "infra/systemd/nhms-node27-frontier-alert.service"


def _wrapper_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A stub interpreter + entrypoint so the wrapper can run to completion
    without importing the real runner."""

    python_bin = tmp_path / "stub-python"
    python_bin.write_text(
        "#!/bin/sh\n"
        'echo "RUNNER_INVOKED args=$*"\n'
        'echo "RUNNER_SAW_DATABASE_URL=${DATABASE_URL:-<unset>}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    script = tmp_path / "runner.py"
    script.write_text("", encoding="utf-8")
    return python_bin, script


def _run_wrapper(tmp_path: Path, env_file: Path, *, injected: bool) -> subprocess.CompletedProcess[str]:
    python_bin, script = _wrapper_sandbox(tmp_path)
    process_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NODE27_FRONTIER_ALERT_REPO": str(tmp_path / "repo"),
        "NODE27_FRONTIER_ALERT_ENV_FILE": str(env_file),
        "NODE27_FRONTIER_ALERT_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "NODE27_FRONTIER_ALERT_LOG_ROOT": str(tmp_path / "logs"),
        "NODE27_FRONTIER_ALERT_PYTHON": str(python_bin),
        "NODE27_FRONTIER_ALERT_SCRIPT": str(script),
    }
    if injected:
        process_env["NODE27_FRONTIER_ALERT_ENV_INJECTED"] = "1"
        # systemd would have exported the file's contents itself.
        process_env["DATABASE_URL"] = "postgresql://injected:pw@127.0.0.1:55432/nhms"
    (tmp_path / "repo").mkdir(exist_ok=True)
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER_PATH)],
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_env_file(path: Path, mode: int) -> Path:
    path.write_text(
        'DATABASE_URL=postgresql://sourced:pw@127.0.0.1:55432/nhms\n'
        "NHMS_ALERT_EMAIL_TO=ops@example.invalid\n",
        encoding="utf-8",
    )
    path.chmod(mode)
    return path


def test_b25_injected_path_still_refuses_a_world_readable_env_file(tmp_path: Path) -> None:
    """The regression this pins: gating the 0600 check behind "should I
    source?" let the systemd path accept a mode-0644 file holding the
    read-only role's password, while .example, the runbook and the wrapper's
    own header all promised refusal."""

    env_file = _write_env_file(tmp_path / "alert.env", 0o644)

    result = _run_wrapper(tmp_path, env_file, injected=True)

    assert result.returncode == 2
    combined = result.stdout + result.stderr + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert "ENV_FILE_MODE_UNSAFE" in combined
    assert "RUNNER_INVOKED" not in combined


def test_b25_injected_path_refuses_a_symlinked_env_file(tmp_path: Path) -> None:
    target = _write_env_file(tmp_path / "real.env", 0o600)
    link = tmp_path / "alert.env"
    link.symlink_to(target)

    result = _run_wrapper(tmp_path, link, injected=True)

    assert result.returncode == 2
    combined = result.stdout + result.stderr + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert "ENV_FILE_SYMLINK_FORBIDDEN" in combined


def test_b25_dangling_symlink_is_a_symlink_not_a_missing_file(tmp_path: Path) -> None:
    link = tmp_path / "alert.env"
    link.symlink_to(tmp_path / "never-existed.env")

    result = _run_wrapper(tmp_path, link, injected=False)

    assert result.returncode == 2
    combined = result.stdout + result.stderr + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert "ENV_FILE_SYMLINK_FORBIDDEN" in combined


def test_b25_injected_path_proceeds_without_re_sourcing(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path / "alert.env", 0o600)

    result = _run_wrapper(tmp_path, env_file, injected=True)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "logs" / "frontier-alert.log").read_text(encoding="utf-8")
    assert "RUNNER_INVOKED args=" in log
    assert "--once" in log
    # The injected value survived: the file was NOT sourced over the top of it.
    assert "RUNNER_SAW_DATABASE_URL=postgresql://injected:pw@127.0.0.1:55432/nhms" in log


def test_b25_manual_path_sources_the_env_file(tmp_path: Path) -> None:
    env_file = _write_env_file(tmp_path / "alert.env", 0o600)

    result = _run_wrapper(tmp_path, env_file, injected=False)

    assert result.returncode == 0, result.stderr
    log = (tmp_path / "logs" / "frontier-alert.log").read_text(encoding="utf-8")
    assert "RUNNER_SAW_DATABASE_URL=postgresql://sourced:pw@127.0.0.1:55432/nhms" in log


def test_b25_manual_path_refuses_a_missing_env_file(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, tmp_path / "absent.env", injected=False)

    assert result.returncode == 2
    combined = result.stdout + result.stderr + (tmp_path / "bootstrap.log").read_text(encoding="utf-8")
    assert "ENV_FILE_MISSING" in combined


def test_b25_service_unit_injects_the_lane_scoped_sentinel() -> None:
    service = _SERVICE_PATH.read_text(encoding="utf-8")
    wrapper = _WRAPPER_PATH.read_text(encoding="utf-8")

    assert "Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1" in service
    assert "EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env" in service
    assert "NODE27_FRONTIER_ALERT_ENV_INJECTED" in wrapper
    # The sentinel must not be the repo-wide shared variable name.
    assert 'if [ -n "${DATABASE_URL:-}" ]; then' not in wrapper
    assert "TimeoutStartSec=900" in service


# ---------------------------------------------------------------------------
# B26 — numeric config and derived sender are fail-safe in the right direction.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["NHMS_FRONTIER_STALL_HOURS", "NHMS_FRONTIER_RESEND_HOURS"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e30", "1e400"])
def test_b26a_unusable_hour_windows_fail_closed_at_config_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str, value: str
) -> None:
    """``float()`` accepts nan/inf and ``1e30`` is finite yet overflows
    ``timedelta``: without constructing the delta during config parsing these
    blow up mid-tick, after the runner believed it was configured."""

    env = _env(tmp_path, **{key: value})
    sendmail = FakeSendmail()

    code = main_with(env, now=T0, observe=_exploding_provider(), sendmail=sendmail)

    assert code == 2
    assert sendmail.calls == []
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert key in payload["reason"]
    assert not config_paths_exist(tmp_path)


def config_paths_exist(tmp_path: Path) -> bool:
    return (tmp_path / "state").exists() or (tmp_path / "receipts").exists()


def test_b26a_large_but_usable_hour_window_is_accepted(tmp_path: Path) -> None:
    config = _config(tmp_path, NHMS_FRONTIER_STALL_HOURS="8760")  # one year
    assert config.stall_delta == timedelta(hours=8760)


@pytest.mark.parametrize(
    "hostname, expected_host",
    [
        ("node-27", "node-27"),
        ("node\r\n27", "node27"),
        ("\r\n", "node-27"),
        ("", "node-27"),
        ("  spaced  ", "spaced"),
    ],
)
def test_b26b_derived_sender_sanitizes_instead_of_rejecting(hostname: str, expected_host: str) -> None:
    """A decorative hostname anomaly must never become a zero-mail config
    error — that trades a cosmetic problem for silence, the wrong direction.
    An operator-supplied NHMS_ALERT_EMAIL_FROM with CR/LF is still rejected
    (B19); only the derived default is sanitized."""

    sender = alerter.default_email_from(hostname)

    assert sender == f"NHMS Frontier Alert <nwm@{expected_host}>"
    # And the sanitized value survives the header-safety gate unchanged.
    assert alerter._header_safe("NHMS_ALERT_EMAIL_FROM", sender) == sender


def test_b26b_derived_sender_is_used_and_mailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerter.socket, "gethostname", lambda: "node\r\n27")
    env = _env(tmp_path)
    env.pop("NHMS_ALERT_EMAIL_FROM")
    config = alerter.config_from_env(env)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    sendmail = FakeSendmail()
    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=sendmail)

    assert len(sendmail.calls) == 1
    assert sendmail.header("From") == "NHMS Frontier Alert <nwm@node27>"
    # One header block, not an injected extra line.
    assert sendmail.messages[0].count("From: ") == 1


# ---------------------------------------------------------------------------
# B21 — the shipped .example is safe under BOTH readers.
# ---------------------------------------------------------------------------

_EXAMPLE_PATH = _REPO_ROOT / "infra/env/node27-frontier-alert.example"
_ASSIGNMENT_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
_COMMENTED_ASSIGNMENT_RE = re.compile(r"^#\s?(?P<body>[A-Z][A-Z0-9_]*=.*)$")


def _example_with_all_assignments_active() -> str:
    lines: list[str] = []
    for line in _EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        commented = _COMMENTED_ASSIGNMENT_RE.match(line)
        lines.append(commented.group("body") if commented else line)
    return "\n".join(lines) + "\n"


def _systemd_style_parse(text: str) -> dict[str, str]:
    """Minimal EnvironmentFile= reader: no expansion, strip matching quotes."""

    parsed: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(stripped)
        if match is None:
            continue
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[match.group("key")] = value
    return parsed


def test_b21_example_is_syntax_clean_with_every_assignment_uncommented(tmp_path: Path) -> None:
    """Every commented example line is a line an operator will uncomment. An
    unquoted ``<nwm@host>`` would be a shell redirect the moment they do."""

    candidate = tmp_path / "node27-frontier-alert.env"
    candidate.write_text(_example_with_all_assignments_active(), encoding="utf-8")

    result = subprocess.run(["bash", "-n", str(candidate)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    # Guard against a vacuous pass: the sender line — the one most likely to be
    # a shell redirect if someone drops the quotes around ``<addr>`` — must be
    # present and quoted. It now ships ACTIVE (the derived nwm@<hostname>
    # default is not the authenticated account, which the shim refuses at
    # config time), so it is asserted on the SHIPPED file, not just on the
    # uncommented variant.
    shipped = _EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
    active_sender = [line for line in shipped if line.startswith("NHMS_ALERT_EMAIL_FROM=")]
    assert active_sender == ['NHMS_ALERT_EMAIL_FROM="NHMS Frontier Alert <alerts@example.com>"']
    assert not [line for line in shipped if line.startswith("#NHMS_ALERT_EMAIL_FROM")]


def test_b21_example_values_mean_the_same_thing_to_bash_and_systemd(tmp_path: Path) -> None:
    text = _example_with_all_assignments_active()
    candidate = tmp_path / "node27-frontier-alert.env"
    candidate.write_text(text, encoding="utf-8")

    expected = _systemd_style_parse(text)
    assert set(expected) >= {
        "DATABASE_URL",
        "NHMS_ALERT_EMAIL_TO",
        "NHMS_ALERT_EMAIL_FROM",
        "NHMS_FRONTIER_STALL_HOURS",
        "NHMS_FRONTIER_STATE_PATH",
        "NHMS_FRONTIER_SENDMAIL",
    }

    keys = sorted(expected)
    script = 'set -a; . "$1"; shift; for key in "$@"; do printf "%s=%s\\n" "$key" "${!key}"; done'
    result = subprocess.run(
        ["bash", "-c", script, "_", str(candidate), *keys], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

    from_bash = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert from_bash == expected

    # And the shipped values are accepted by the runner's own parser.
    config = alerter.config_from_env(expected)
    assert config.email_from == "NHMS Frontier Alert <alerts@example.com>"
    assert config.stall_hours == 4.0
    # The shipped channel is the authenticated SMTP shim, NOT node-27's local
    # /usr/sbin/sendmail: that postfix is null-routed (default_transport =
    # error) and exits 0 on mail it then asynchronously bounces (observed
    # 2026-08-13), which the alerter would record as a delivered alert.
    assert config.sendmail_path == "/home/nwm/NWM/scripts/node27_frontier_smtp_sendmail.py"
    assert "NHMS_SMTP_USER" in expected and "NHMS_SMTP_PASS" in expected
    # And the shipped From is deliverable through that shim: the shim fails
    # closed (exit 64, nothing connected) unless the From addr-spec IS the
    # authenticated account, so an .example whose two values disagree would
    # ship a factory config that can never send a single alert.
    assert parseaddr(expected["NHMS_ALERT_EMAIL_FROM"])[1] == expected["NHMS_SMTP_USER"]


# ---------------------------------------------------------------------------
# B31 — a successful send carries the channel's evidence line into the record.
# ---------------------------------------------------------------------------


def _talking_sendmail_binary(tmp_path: Path, *, stderr_text: str, exit_code: int = 0) -> Path:
    """A real executable standing in for the SMTP shim, so the REAL
    ``default_sendmail_runner`` is exercised: it prints its evidence (or its
    failure) on stderr exactly the way the shim does."""

    binary = tmp_path / "fake-shim-sendmail"
    binary.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f"printf '%b' \"{stderr_text}\" >&2\n"  # %b so \\n in the text is a real newline
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _stalled_tick_with_binary(tmp_path: Path, binary: Path) -> tuple[alerter.AlertConfig, dict[str, Any]]:
    env = _env(tmp_path, NHMS_FRONTIER_SENDMAIL=str(binary))
    config = alerter.config_from_env(env)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    receipt = alerter.run_tick(
        config,
        now=T0 + timedelta(hours=4),
        observe=_provider(snapshot),
        sendmail_runner=None,  # the REAL runner, real subprocess, real stderr
        dry_run=False,
    )
    return config, receipt


def test_b31_shim_acceptance_line_reaches_receipt_events_and_state(tmp_path: Path) -> None:
    """Without this, a delivery the destination synchronously accepted (250)
    and the null-routed-postfix era's fictitious exit 0 produce byte-identical
    receipts — the evidence the shim exists to create never leaves the pipe."""

    evidence = "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    binary = _talking_sendmail_binary(tmp_path, stderr_text=evidence)

    config, receipt = _stalled_tick_with_binary(tmp_path, binary)

    assert receipt["emails"][0]["sent"] is True
    assert receipt["emails"][0]["returncode"] == 0
    assert receipt["emails"][0]["error"] is None
    assert receipt["emails"][0]["evidence"] == evidence

    on_disk = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert on_disk["emails"][0]["evidence"] == evidence

    events = [json.loads(line) for line in config.events_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["evidence"] == evidence
    assert events[-1]["sent"] is True


def test_b31_a_silent_sendmail_records_no_evidence(tmp_path: Path) -> None:
    """Classic ``/usr/sbin/sendmail`` prints nothing on success: the field stays
    None instead of inventing an empty-string "proof"."""

    binary = _talking_sendmail_binary(tmp_path, stderr_text="")

    config, receipt = _stalled_tick_with_binary(tmp_path, binary)

    assert receipt["emails"][0]["sent"] is True
    assert receipt["emails"][0]["evidence"] is None
    events = [json.loads(line) for line in config.events_path.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["evidence"] is None


def test_b31_only_the_last_non_empty_stderr_line_is_kept(tmp_path: Path) -> None:
    """A chatty channel (``-v``) must not paste a transcript into every record;
    the evidence line is the last thing the shim prints."""

    binary = _talking_sendmail_binary(
        tmp_path,
        stderr_text="connect: smtp.163.com\\nEHLO ...\\n"
        "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1\\n\\n",
    )

    _config_unused, receipt = _stalled_tick_with_binary(tmp_path, binary)

    assert receipt["emails"][0]["evidence"] == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"


def test_b31_evidence_goes_through_the_dsn_redaction_chokepoint(tmp_path: Path) -> None:
    """The channel's stderr is untrusted text on the same outlet as ``error``:
    it must not be the one path that carries the DSN into the receipt."""

    binary = _talking_sendmail_binary(tmp_path, stderr_text=f"SMTP-ACCEPTED via {DSN} code=250")

    config, receipt = _stalled_tick_with_binary(tmp_path, binary)

    assert DSN_PASSWORD not in json.dumps(receipt)
    assert DSN_PASSWORD not in config.receipt_path.read_text(encoding="utf-8")
    assert DSN_PASSWORD not in config.events_path.read_text(encoding="utf-8")
    assert "SMTP-ACCEPTED" in receipt["emails"][0]["evidence"]


def test_b31_failure_branch_is_unchanged(tmp_path: Path) -> None:
    """Regression fence around the one line that was allowed to change: a
    non-zero exit still yields the stderr as ``error``, the same returncode, a
    failed send, no evidence — and the alert stays owed."""

    binary = _talking_sendmail_binary(
        tmp_path, stderr_text="SMTP-FAILED stage=login host=smtp.163.com", exit_code=69
    )

    config, receipt = _stalled_tick_with_binary(tmp_path, binary)

    assert receipt["emails"][0]["sent"] is False
    assert receipt["emails"][0]["returncode"] == 69
    assert receipt["emails"][0]["error"] == "SMTP-FAILED stage=login host=smtp.163.com"
    assert receipt["emails"][0]["evidence"] is None
    assert receipt["send_failures"] == 1
    assert receipt["status"] == "degraded"
    state = _read_state(config)
    assert state.alert_active is True
    assert state.last_alert_at is None


def test_b31_dry_run_records_keep_the_same_shape(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    sendmail = FakeSendmail()

    receipt = _tick(
        config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=sendmail, dry_run=True
    )

    assert sendmail.calls == []
    assert receipt["emails"][0]["dry_run"] is True
    assert receipt["emails"][0]["evidence"] is None


# ---------------------------------------------------------------------------
# B27 — the mail chain contains failures whole-class (round-3 invariant).
# ---------------------------------------------------------------------------


def _recording_sendmail_binary(tmp_path: Path) -> tuple[Path, Path]:
    """A real executable standing in for ``/usr/sbin/sendmail``, so the test
    exercises the REAL ``default_sendmail_runner`` without faking the failure
    away. It touches a marker file, which lets the test prove the failure
    happened before the process was ever spawned."""

    marker = tmp_path / "sendmail-was-invoked"
    binary = tmp_path / "fake-sendmail"
    binary.write_text(f'#!/bin/sh\ncat >/dev/null\n: >"{marker}"\n', encoding="utf-8")
    binary.chmod(0o700)
    return binary, marker


def test_b27_undecodable_recipient_does_not_kill_the_tick(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single non-UTF-8 byte in ``NHMS_ALERT_EMAIL_TO`` reaches the process as
    a surrogate (``os.environ`` surrogateescape). ``message.encode("utf-8")``
    then raises ``UnicodeEncodeError`` — not an ``OSError``, not a
    ``TimeoutExpired``. Under the enumerated catch it escaped the runner, the
    outbox and ``run_tick``, so the tick died before writing state, receipt or
    events: identical death every tick, forever, with zero mail and zero
    artifacts."""

    binary, marker = _recording_sendmail_binary(tmp_path)
    env = _env(
        tmp_path,
        NHMS_ALERT_EMAIL_TO="ops\udcc4@example.com",
        NHMS_FRONTIER_SENDMAIL=str(binary),
    )
    config = alerter.config_from_env(env)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    capsys.readouterr()

    stall_at = T0 + timedelta(hours=4)
    code = alerter.main(
        ["--once"],
        now=stall_at,
        observe=_provider(snapshot),
        sendmail_runner=None,  # the REAL runner, real encode path
        env=env,
    )

    assert code == 1  # degraded, controlled — not a traceback
    assert not marker.exists(), "the failure must be contained before exec, not after"

    # The send is recorded as a FAILURE, so the alert is still owed.
    receipt = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert receipt["send_failures"] == 1
    assert receipt["stall_alert"] == "initial"
    assert receipt["status"] == "degraded"
    assert receipt["emails"][0]["sent"] is False
    assert receipt["emails"][0]["returncode"] == alerter.SEND_INTERNAL_FAILURE_RC
    assert "UnicodeEncodeError" in receipt["emails"][0]["error"]
    assert DSN_PASSWORD not in json.dumps(receipt)

    state = _read_state(config)
    assert state.alert_active is True
    assert state.last_alert_at is None  # undelivered -> retried, never "done"

    events = [json.loads(line) for line in config.events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [alerter.EVENT_STALLED]
    assert events[0]["sent"] is False

    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["status"] == "degraded"
    assert printed["emails"][0]["sent"] is False

    # And the lane keeps running: the next tick retries and delivers.
    retry = FakeSendmail()
    receipt = _tick(config, now=stall_at + timedelta(minutes=30), snapshot=snapshot, sendmail=retry)
    assert len(retry.calls) == 1
    assert receipt["stall_alert"] == "initial"  # still the first DELIVERED one
    assert receipt["status"] == "stalled"
    assert _read_state(config).last_alert_at == stall_at + timedelta(minutes=30)


def test_b27_outbox_contains_a_runner_that_raises(tmp_path: Path) -> None:
    """``default_sendmail_runner`` is not the only mail-chain seam: the runner
    is injectable and ``build_message`` runs inside the outbox too, so the
    containment has to sit at ``_Outbox.send`` as well."""

    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    def exploding_runner(argv: list[str], message: str) -> alerter.SendResult:
        raise MemoryError("runner blew up in an unenumerated way")

    stall_at = T0 + timedelta(hours=4)
    receipt = _tick(config, now=stall_at, snapshot=snapshot, sendmail=exploding_runner)

    assert receipt["send_failures"] == 1
    assert receipt["emails"][0]["returncode"] == alerter.SEND_INTERNAL_FAILURE_RC
    assert "MemoryError" in receipt["emails"][0]["error"]
    assert _read_state(config).last_alert_at is None
    assert config.receipt_path.exists() and config.events_path.exists()


def test_b27_outbox_contains_a_message_build_failure(tmp_path: Path, monkeypatch) -> None:
    """The build step is inside the same contained region as the runner call."""

    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    def exploding_build(*_args: Any, **_kwargs: Any) -> str:
        raise ZeroDivisionError("message build blew up")

    monkeypatch.setattr(alerter, "build_message", exploding_build)
    sendmail = FakeSendmail()
    receipt = _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=sendmail)

    assert sendmail.calls == []
    assert receipt["send_failures"] == 1
    assert "ZeroDivisionError" in receipt["emails"][0]["error"]
    assert _read_state(config).last_alert_at is None


def test_b27_unexpected_config_stage_error_is_structured_and_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """Sweep convergence: the config stage guards the whole lane, so an
    unenumerated class there must not become a raw traceback — which is
    unstructured and can echo the DSN past the redaction chokepoint."""

    def exploding_config(_env):
        raise RuntimeError(f"driver exploded while parsing {DSN}")

    monkeypatch.setattr(alerter, "config_from_env", exploding_config)
    sendmail = FakeSendmail()

    code = main_with(_env(tmp_path), now=T0, observe=_exploding_provider(), sendmail=sendmail)

    assert code == 2
    assert sendmail.calls == []
    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["code"] == alerter.CODE_CONFIG_INVALID
    assert "RuntimeError" in payload["reason"]
    assert DSN_PASSWORD not in captured.err


def test_b27_unexpected_lock_stage_error_is_a_structured_config_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Same sweep, lock stage: ``OSError`` had an arm, everything else escaped."""

    def exploding_open(*_args: Any, **_kwargs: Any) -> int:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(alerter.os, "open", exploding_open)
    with pytest.raises(alerter.FrontierAlertConfigError) as excinfo:
        alerter.acquire_lock(tmp_path / "state" / "frontier.lock")
    assert "ValueError" in str(excinfo.value)


def test_b27_baseexception_from_the_runner_is_not_contained(tmp_path: Path) -> None:
    """Whole-class means ``Exception``, not ``BaseException``: a
    ``KeyboardInterrupt`` must still terminate the tick."""

    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)

    def interrupting_runner(argv: list[str], message: str) -> alerter.SendResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=interrupting_runner)


# ---------------------------------------------------------------------------
# Config surface pins (D4 env contract).
# ---------------------------------------------------------------------------


def test_config_defaults_match_the_design(tmp_path: Path) -> None:
    env = {"DATABASE_URL": DSN, "NHMS_ALERT_EMAIL_TO": RECIPIENT}
    config = alerter.config_from_env(env)

    assert config.stall_hours == 4.0
    assert config.resend_hours == 6.0
    assert config.query_fail_ticks == 2
    assert config.sendmail_path == "/usr/sbin/sendmail"
    assert config.state_path == alerter.DEFAULT_STATE_PATH
    assert config.receipt_path == alerter.DEFAULT_RECEIPT_PATH
    assert config.lock_path == alerter.DEFAULT_STATE_PATH.with_name(
        alerter.DEFAULT_STATE_PATH.name + ".lock"
    )
    assert config.email_from.startswith("NHMS Frontier Alert <nwm@")


@pytest.mark.parametrize(
    "key, value",
    [
        ("NHMS_FRONTIER_STALL_HOURS", "0"),
        ("NHMS_FRONTIER_STALL_HOURS", "not-a-number"),
        ("NHMS_FRONTIER_QUERY_FAIL_TICKS", "-1"),
        ("NHMS_FRONTIER_STATE_PATH", "relative/state.json"),
        ("NHMS_FRONTIER_SENDMAIL", "sendmail"),
    ],
)
def test_config_rejects_bad_values(tmp_path: Path, key: str, value: str) -> None:
    env = _env(tmp_path, **{key: value})
    with pytest.raises(alerter.FrontierAlertConfigError):
        alerter.config_from_env(env)


def test_receipt_and_event_log_record_every_alert(tmp_path: Path) -> None:
    """Evidence/audit surface: the latest-tick receipt is overwritten in place
    while the JSONL event log only ever appends."""

    config = _config(tmp_path)
    snapshot = _baseline_snapshot()
    _bootstrap(config, snapshot)
    assert not config.events_path.exists()  # a silent bootstrap logs no event

    _tick(config, now=T0 + timedelta(hours=4), snapshot=snapshot, sendmail=FakeSendmail())
    _tick(config, now=T0 + timedelta(hours=10), snapshot=snapshot, sendmail=FakeSendmail())

    events = [json.loads(line) for line in config.events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [alerter.EVENT_STALLED, alerter.EVENT_STALLED]
    assert [event["detail"] for event in events] == ["initial", "resend"]
    assert all(event["sent"] is True for event in events)

    receipt = json.loads(config.receipt_path.read_text(encoding="utf-8"))
    assert receipt["generated_at"] == (T0 + timedelta(hours=10)).isoformat()
    assert receipt["stall_alert"] == "resend"
    assert receipt["runner"] == "node27_frontier_stall_alert"


def test_state_and_receipt_are_written_with_owner_only_mode(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _bootstrap(config, _baseline_snapshot())

    assert os.stat(config.state_path).st_mode & 0o777 == 0o600
    assert os.stat(config.receipt_path).st_mode & 0o777 == 0o600
