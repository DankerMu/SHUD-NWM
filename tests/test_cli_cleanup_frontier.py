"""Requirement-driven tests for the manual ``cleanup`` CLI frontier wiring.

Issue #1407 / change ``retention-frontier-out-of-pass``: the out-of-pass
deletion surface must consume the pipeline frontier recorded by the latest
scheduler pass receipt, and fail closed (forced dry-run) when that frontier is
unknown. Both entrypoints (click and argparse) are covered against the same
fixtures, because ``main()`` prefers click in production while argparse is the
fallback.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from services.orchestrator import cli

NOW = datetime.now(UTC)


def _cycle_name(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H")


def _write_cycle(root: Path, prefix: str, source: str, cycle: str) -> Path:
    path = root / prefix / source / cycle
    path.mkdir(parents=True, exist_ok=True)
    (path / "payload.nc").write_bytes(b"x")
    return path


def _write_receipt(
    evidence_dir: Path,
    pass_id: str,
    *,
    started_at: datetime,
    retention: dict[str, object] | None,
) -> Path:
    payload: dict[str, object] = {
        "schema_version": "nhms.production_scheduler.pass_evidence.v1",
        "pass_id": pass_id,
        "started_at": started_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    if retention is not None:
        payload["retention"] = retention
    path = evidence_dir / f"{pass_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _retention_block(bound: datetime | None, source: str | None = "scheduler_pass") -> dict[str, object]:
    return {
        "status": "completed",
        "frontier": {
            "active_lower_bound": None if bound is None else bound.astimezone(UTC).isoformat(),
            "source": None if bound is None else source,
            "protected_count": 0,
        },
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Isolated workspace + evidence dir + object store wired through env."""
    workspace = tmp_path / "workspace"
    evidence_dir = workspace / "scheduler" / "evidence"
    evidence_dir.mkdir(parents=True)
    store = tmp_path / "object-store"
    store.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(evidence_dir))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(store))
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS", raising=False)
    return {"workspace": workspace, "evidence_dir": evidence_dir, "store": store}


def _skipped_reasons(payload: dict[str, object]) -> set[str]:
    return {entry["reason"] for entry in payload["skipped"]}


# ---------------------------------------------------------------------------
# I1 - catch-up: a cycle at or after the receipt bound survives --execute
# ---------------------------------------------------------------------------
def test_cleanup_execute_exempts_cycles_at_or_after_receipt_frontier(env: dict[str, Path]) -> None:
    in_flight_cycle = NOW - timedelta(days=20)
    ancient_cycle = NOW - timedelta(days=40)
    in_flight = _write_cycle(env["store"], "raw", "gfs", _cycle_name(in_flight_cycle))
    ancient = _write_cycle(env["store"], "raw", "gfs", _cycle_name(ancient_cycle))
    _write_receipt(
        env["evidence_dir"],
        "pass-1",
        started_at=NOW,
        retention=_retention_block(in_flight_cycle.replace(minute=0, second=0, microsecond=0)),
    )

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    # The in-flight cycle survives; the one below the frontier is still collected.
    assert in_flight.exists()
    assert not ancient.exists()
    assert payload["dry_run"] is False
    assert "frontier_blocker" not in payload
    assert payload["frontier_source"] == "receipt:scheduler_pass"
    assert payload["frontier"]["active_lower_bound"] is not None
    assert payload["frontier"]["protected_count"] >= 1
    assert "pipeline_frontier_exempt" in _skipped_reasons(payload)


# ---------------------------------------------------------------------------
# I2 - unknown frontier forces dry-run instead of unprotected deletion
# ---------------------------------------------------------------------------
def test_cleanup_execute_without_receipt_forces_dry_run(env: dict[str, Path]) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert aged.exists()
    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "no_readable_receipt"
    assert payload["frontier_blocker"]["forced_dry_run"] is True
    assert payload["frontier_blocker"]["receipt_path"] is None
    assert payload["counts"]["deleted"] == 0
    assert payload["counts"]["planned"] == 1


def test_cleanup_execute_with_stale_receipt_forces_dry_run(env: dict[str, Path]) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    receipt = _write_receipt(
        env["evidence_dir"],
        "pass-old",
        started_at=NOW - timedelta(hours=48),
        retention=_retention_block(NOW - timedelta(days=30)),
    )

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "receipt_stale"
    assert payload["frontier_blocker"]["receipt_path"] == str(receipt)
    assert payload["frontier"]["active_lower_bound"] is None
    assert aged.exists()


def test_cleanup_execute_with_unresolvable_evidence_dir_forces_dry_run(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    # An evidence root outside the workspace fails the confinement check inside
    # ProductionSchedulerConfig.__post_init__.
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(tmp_path / "elsewhere" / "evidence"))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "evidence_dir_unresolved"
    assert aged.exists()


def test_cleanup_execute_with_disabled_pass_retention_forces_dry_run(env: dict[str, Path]) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    _write_receipt(
        env["evidence_dir"],
        "pass-disabled",
        started_at=NOW,
        retention={"status": "disabled", "enabled": False},
    )

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "pass_retention_not_run"
    assert aged.exists()


# ---------------------------------------------------------------------------
# I3 - a fresh null bound mirrors the pass (pure wall clock, not stricter)
# ---------------------------------------------------------------------------
def test_cleanup_mirrors_fresh_null_bound_as_pure_wall_clock(env: dict[str, Path]) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    _write_receipt(
        env["evidence_dir"],
        "pass-null",
        started_at=NOW,
        retention=_retention_block(None),
    )

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is False
    assert "frontier_blocker" not in payload
    # Mirror label lives at payload top level; retention's frontier block nulls
    # the source whenever the bound is null (retention.py frontier()).
    assert payload["frontier_source"] == "receipt:none"
    assert payload["frontier"]["active_lower_bound"] is None
    assert payload["frontier"]["source"] is None
    assert not aged.exists()


# ---------------------------------------------------------------------------
# I4 - both entrypoints behave identically on the same fixtures
# ---------------------------------------------------------------------------
def test_cleanup_click_entrypoint_forces_dry_run_without_receipt(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))

    rc = cli._click_main(["cleanup", "--retention-days", "14", "--execute"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "no_readable_receipt"
    assert payload["counts"]["deleted"] == 0
    assert aged.exists()


def test_cleanup_argparse_entrypoint_forces_dry_run_without_receipt(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))

    rc = cli._argparse_main(["cleanup", "--retention-days", "14", "--execute"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "no_readable_receipt"
    assert payload["counts"]["deleted"] == 0
    assert aged.exists()


def test_cleanup_entrypoints_agree_on_frontier_exempt_fixture(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    in_flight_cycle = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    _write_cycle(env["store"], "raw", "gfs", _cycle_name(in_flight_cycle))
    _write_receipt(
        env["evidence_dir"],
        "pass-1",
        started_at=NOW,
        retention=_retention_block(in_flight_cycle),
    )

    assert cli._click_main(["cleanup", "--retention-days", "14"]) == 0
    click_payload = json.loads(capsys.readouterr().out.strip())
    assert cli._argparse_main(["cleanup", "--retention-days", "14"]) == 0
    argparse_payload = json.loads(capsys.readouterr().out.strip())

    for key in ("frontier", "frontier_source", "skipped", "planned", "counts"):
        assert click_payload[key] == argparse_payload[key]
    assert click_payload["frontier_source"] == "receipt:scheduler_pass"
    assert "pipeline_frontier_exempt" in _skipped_reasons(click_payload)
