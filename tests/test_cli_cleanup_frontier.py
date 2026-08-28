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
    # Issue #1318: keep the additional-root knobs out of the ambient
    # environment so every test states its own gate and window.
    monkeypatch.delenv("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", raising=False)
    monkeypatch.delenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", raising=False)
    monkeypatch.delenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", raising=False)
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


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cleanup_execute_deletes_below_bound_and_exempts_above_it(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str], entrypoint: str
) -> None:
    """The ok-bound execute path, end to end through each entrypoint."""
    bound = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    in_flight = _write_cycle(env["store"], "raw", "gfs", _cycle_name(bound))
    below_bound = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(bound))

    run = cli._click_main if entrypoint == "click" else cli._argparse_main
    rc = run(["cleanup", "--retention-days", "14", "--execute"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert in_flight.exists()
    assert not below_bound.exists()
    assert payload["dry_run"] is False
    assert "frontier_blocker" not in payload
    assert payload["counts"]["deleted"] == 1
    assert payload["deleted"][0]["key"] == f"raw/gfs/{_cycle_name(NOW - timedelta(days=40))}"
    assert "pipeline_frontier_exempt" in _skipped_reasons(payload)
    # The bound and its receipt-derived source label are both readable from the
    # retention frontier block, not only from the payload's own top-level label.
    assert payload["frontier"]["active_lower_bound"] == bound.isoformat()
    assert payload["frontier"]["source"] == "receipt:scheduler_pass"
    assert payload["frontier"]["protected_count"] == 1
    assert payload["frontier_source"] == "receipt:scheduler_pass"


def test_cleanup_execute_with_future_dated_receipt_forces_dry_run(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A future-dated receipt must not outrank a genuine one and delete silently.

    Reproduces review round-1 finding B: the bogus receipt wins selection on
    ``started_at``, and under a one-sided freshness check it never goes stale,
    so its null bound reads as a healthy pass mirror and the in-flight cycle
    protected by the genuine receipt is deleted.
    """
    bound = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    in_flight = _write_cycle(env["store"], "raw", "gfs", _cycle_name(bound))
    _write_receipt(env["evidence_dir"], "pass-genuine", started_at=NOW, retention=_retention_block(bound))
    bogus = _write_receipt(
        env["evidence_dir"],
        "pass-future",
        started_at=NOW + timedelta(days=2),
        retention=_retention_block(None),
    )

    rc = cli._click_main(["cleanup", "--retention-days", "14", "--execute"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert in_flight.exists()
    assert payload["dry_run"] is True
    assert payload["frontier_blocker"]["reason"] == "receipt_stale"
    assert payload["frontier_blocker"]["receipt_path"] == str(bogus)
    assert payload["counts"]["deleted"] == 0
    assert "frontier_source" not in payload


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_cleanup_survives_an_overflowing_freshness_cap(
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    """An unusable freshness cap falls back to the default, not a traceback."""
    monkeypatch.setenv("NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS", "9" * 30)
    bound = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    _write_cycle(env["store"], "raw", "gfs", _cycle_name(bound))
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(bound))

    run = cli._click_main if entrypoint == "click" else cli._argparse_main
    rc = run(["cleanup", "--retention-days", "14"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    # The 24h default applied, so this fresh receipt still reads as ok.
    assert payload["frontier_source"] == "receipt:scheduler_pass"
    assert "frontier_blocker" not in payload


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


# ---------------------------------------------------------------------------
# I5 (#1503) - the payload discloses which evidence directory was consulted.
#
# Under the three commonest blocker reasons ``receipt_path`` is always null, so
# without this key a mis-resolved workspace root (the relative default under a
# wrong cwd) is indistinguishable from genuinely missing evidence.
# ---------------------------------------------------------------------------
def test_blocker_discloses_the_probed_evidence_dir_when_it_is_missing(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = env["workspace"] / "scheduler" / "absent-evidence"
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(absent))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["frontier_blocker"]["reason"] == "evidence_dir_missing"
    assert payload["frontier_blocker"]["receipt_path"] is None
    assert payload["frontier_blocker"]["evidence_dir"] == str(absent)


def test_blocker_discloses_the_probed_evidence_dir_without_a_readable_receipt(
    env: dict[str, Path],
) -> None:
    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["frontier_blocker"]["reason"] == "no_readable_receipt"
    assert payload["frontier_blocker"]["receipt_path"] is None
    assert payload["frontier_blocker"]["evidence_dir"] == str(env["evidence_dir"])


def test_unresolved_evidence_dir_carries_the_key_as_an_explicit_null(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key stays present so the payload shape does not vary by reason."""
    monkeypatch.setenv("NHMS_SCHEDULER_EVIDENCE_ROOT", str(tmp_path / "elsewhere" / "evidence"))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["frontier_blocker"]["reason"] == "evidence_dir_unresolved"
    assert "evidence_dir" in payload["frontier_blocker"]
    assert payload["frontier_blocker"]["evidence_dir"] is None


def test_ok_path_discloses_the_evidence_dir_at_the_payload_top_level(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bound = (NOW - timedelta(days=20)).replace(minute=0, second=0, microsecond=0)
    _write_cycle(env["store"], "raw", "gfs", _cycle_name(bound))
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(bound))

    assert cli._click_main(["cleanup", "--retention-days", "14"]) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    assert "frontier_blocker" not in payload
    assert payload["frontier_source"] == "receipt:scheduler_pass"
    assert payload["evidence_dir"] == str(env["evidence_dir"])


def test_blocker_evidence_dir_is_the_absolute_path_the_relative_default_derived(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The disclosed path is the one actually probed, not the configured string.

    This is the #1503 motivating scenario: with no evidence root configured the
    workspace root's relative default is resolved against the current working
    directory, so an operator running from the wrong cwd silently probes an
    empty tree. The key only distinguishes that from genuinely missing evidence
    if it carries the derived absolute path.
    """
    monkeypatch.delenv("NHMS_SCHEDULER_EVIDENCE_ROOT", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", "ws-relative")
    monkeypatch.chdir(tmp_path)
    _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["frontier_blocker"]["reason"] == "evidence_dir_missing"
    disclosed = payload["frontier_blocker"]["evidence_dir"]
    assert disclosed is not None
    assert Path(disclosed).is_absolute()
    assert (
        Path(disclosed).resolve()
        == (tmp_path / "ws-relative" / "scheduler" / "evidence").resolve()
    )


# ---------------------------------------------------------------------------
# I6 (#1318) - `--retention-days` scopes to the object-store window only.
#
# The additional runs/-only roots keep NHMS_RETENTION_EXTRA_ROOTS_DAYS, so
# `cleanup --retention-days 1 --execute` cannot turn into a one-day mass
# deletion across the workspace and copyback roots. The expected window here is
# deliberately 7 -- not the 30-day default -- because a test written against
# the default stays green even if the env value is never read at all.
# ---------------------------------------------------------------------------
def _write_run_workspace(root: Path, cycle: datetime) -> str:
    run_id = f"fcst_gfs_{_cycle_name(cycle)}_model_a"
    path = root / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "out.nc").write_bytes(b"x")
    return f"runs/{run_id}"


def test_cleanup_retention_days_overrides_only_the_object_store_window(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    copyback = tmp_path / "copyback"
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_ENABLED", "true")
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", "7")
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback))
    ten_days = NOW - timedelta(days=10)
    copyback_key = _write_run_workspace(copyback, ten_days)
    workspace_key = _write_run_workspace(env["workspace"], ten_days)
    aged_cycle = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=3)))
    # a fresh receipt with a null bound => pure wall-clock, exactly as the pass
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(None))

    payload = cli._run_cleanup(retention_days=1, dry_run=True)

    assert "frontier_blocker" not in payload
    assert payload["retention_days"] == 1
    assert payload["extra_roots"]["retention_days"] == 7
    assert payload["extra_roots"]["enabled"] is True
    assert set(payload["extra_roots"]["roots"]) == {
        str(env["workspace"].resolve()),
        str(copyback.resolve()),
    }
    planned = {(entry["root"], entry["key"]) for entry in payload["planned"]}
    # the 10-day-old runs fall outside the 7-day additional window; a fallback
    # to the 30-day default would have skipped them as within_retention_window
    assert (str(copyback.resolve()), copyback_key) in planned
    assert (str(env["workspace"].resolve()), workspace_key) in planned
    # the object-store root still answers to --retention-days
    assert (str(env["store"].resolve()), "raw/gfs/" + aged_cycle.name) in planned
    assert payload["dry_run"] is True
    assert (copyback / copyback_key).exists()


def test_cleanup_with_the_gate_closed_sweeps_no_additional_root(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    copyback = tmp_path / "copyback"
    monkeypatch.setenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", str(copyback))
    monkeypatch.setenv("NHMS_RETENTION_EXTRA_ROOTS_DAYS", "7")
    ancient = NOW - timedelta(days=400)
    copyback_key = _write_run_workspace(copyback, ancient)
    workspace_key = _write_run_workspace(env["workspace"], ancient)
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(None))

    payload = cli._run_cleanup(retention_days=1, dry_run=False)

    assert payload["extra_roots"]["enabled"] is False
    assert payload["extra_roots"]["roots"] == []
    roots = {entry["root"] for entry in [*payload["planned"], *payload["skipped"]]}
    assert str(copyback.resolve()) not in roots
    assert str(env["workspace"].resolve()) not in roots
    assert (copyback / copyback_key).exists()
    assert (env["workspace"] / workspace_key).exists()


# ---------------------------------------------------------------------------
# I7 (#1616) - the cleanup CLI hands retention the raw OBJECT_STORE_ROOT env
# value, so a blank/relative primary is rejected as a deletion surface instead
# of resolving against the CWD (or, for the pass, the scheduler workspace).
# The scheduler-normalized location is where __post_init__ would anchor a
# relative value: <workspace>/<relative/store>.
# ---------------------------------------------------------------------------
def _write_cycle_and_run(root: Path, cycle: datetime) -> str:
    cycle_name = _cycle_name(cycle)
    _write_cycle(root, "raw", "gfs", cycle_name)
    run_id = f"fcst_gfs_{cycle_name}_model_a"
    path = root / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "out.nc").write_bytes(b"x")
    return f"runs/{run_id}"


def _seed_cli_raw_primary_old_trees(
    env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Seed aged retention-shaped trees under the CWD and under the
    scheduler-normalized workspace location; returns (cwd, normalized_store).

    The normalized location is ``<workspace>/relative/store`` -- exactly where
    ``ProductionSchedulerConfig.__post_init__`` would anchor the relative value
    ``"relative/store"``. ``WORKSPACE_ROOT`` comes from the ``env`` fixture, so
    the cleanup's evidence-dir derivation still reads the receipts written to
    ``env["evidence_dir"]``.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    normalized_store = env["workspace"] / "relative" / "store"
    aged = NOW - timedelta(days=40)
    _write_cycle_and_run(cwd, aged)
    _write_cycle_and_run(normalized_store, aged)
    return cwd, normalized_store


@pytest.mark.parametrize("value", ["", "   ", "relative/store"])
def test_cleanup_raw_primary_blank_or_relative_is_never_scanned(
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    value: str,
) -> None:
    """[3.2] ``OBJECT_STORE_ROOT`` as ``""`` / whitespace / ``"relative/store"``:
    no CWD or scheduler-normalized tree is scanned or removed, physical bytes
    survive, and ``skipped`` carries the exact primary reason token."""
    from services.orchestrator.retention import (
        PRIMARY_ROOT_BLANK_REASON,
        PRIMARY_ROOT_NOT_ABSOLUTE_REASON,
    )

    monkeypatch.setenv("OBJECT_STORE_ROOT", value)
    cwd, normalized_store = _seed_cli_raw_primary_old_trees(env, tmp_path, monkeypatch)
    # a fresh null-bound receipt keeps the ok (non-blocked) path
    _write_receipt(env["evidence_dir"], "pass-null", started_at=NOW, retention=_retention_block(None))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    expected_reason = (
        PRIMARY_ROOT_BLANK_REASON if value.strip() == "" else PRIMARY_ROOT_NOT_ABSOLUTE_REASON
    )
    assert payload["dry_run"] is False
    assert "frontier_blocker" not in payload
    assert [(entry["key"], entry["reason"]) for entry in payload["skipped"]] == [
        ("", expected_reason)
    ]
    assert payload["planned"] == []
    assert payload["deleted"] == []
    # physical bytes under both old-tree locations survive
    aged = _cycle_name(NOW - timedelta(days=40))
    for location in (cwd, normalized_store):
        assert (location / f"raw/gfs/{aged}/payload.nc").exists()
        assert (location / f"runs/fcst_gfs_{aged}_model_a/out.nc").exists()


def test_cleanup_absolute_primary_still_works(env: dict[str, Path], tmp_path: Path) -> None:
    """[3.2] An ordinary absolute configured primary stays functional through the
    CLI: the aged cycle is deleted, not skipped."""
    aged = _write_cycle(env["store"], "raw", "gfs", _cycle_name(NOW - timedelta(days=40)))
    _write_receipt(env["evidence_dir"], "pass-1", started_at=NOW, retention=_retention_block(None))

    payload = cli._run_cleanup(retention_days=14, dry_run=False)

    assert payload["dry_run"] is False
    assert payload["skipped"] == []
    assert not aged.exists()
    assert any(entry["key"] == f"raw/gfs/{_cycle_name(NOW - timedelta(days=40))}" for entry in payload["deleted"])
