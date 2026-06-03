"""Deterministic CI tests for the publish-qdown CLI wiring (#260 / M23-9 7.1-7.2).

No real DB / Slurm / network: every external boundary is monkeypatched or
injected so the suite is reproducible in CI. Covers:

* publish-qdown PASS path (fake publisher -> published dict structure).
* publish-qdown BLOCKED/failure path truthfully reports failure (exit 1 +
  ``failure_payload`` JSON), and never emits a false "published"/success.
* Slurm gateway state transitions via injectable probe (healthy -> no blocker;
  unhealthy -> SLURM_GATEWAY_UNAVAILABLE; self-reference ->
  SLURM_GATEWAY_SELF_REFERENCE) with redaction (no secrets in evidence).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import cli
from services.orchestrator.scheduler import ProductionSchedulerConfig, _slurm_gateway_check
from services.tile_publisher import PublishError
from services.tile_publisher.publisher import PublishResult


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakePublisher:
    """Stands in for TilePublisher; records the cycle and returns a canned result."""

    def __init__(self, result: PublishResult | None = None, error: PublishError | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    def publish_qdown_cycle(self, cycle_id: str) -> PublishResult:
        self.calls.append(cycle_id)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _published_result(cycle_id: str) -> PublishResult:
    return PublishResult(
        cycle_id=cycle_id,
        status="published",
        layers=(
            {
                "layer_id": f"q_down_{cycle_id}_seg",
                "layer_type": "q_down_timeseries",
                "source_run_id": "run-1",
                "quality_state": "degraded",
                "unavailable_products": ["return_period_result"],
            },
        ),
        artifacts=(
            {"artifact_id": "q_down_manifest_run-1", "artifact_type": "q_down_manifest", "uri": "published://m"},
        ),
        lineage={
            "cycle_id": cycle_id,
            "published_basins": 1,
            "quality_state": "degraded",
            "manifest_uri": "published://m",
        },
    )


# ---------------------------------------------------------------------------
# Part 1 - publish-qdown PASS
# ---------------------------------------------------------------------------
def test_publish_qdown_pass_returns_published_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(result=_published_result(cycle_id))
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    published = cli._publish_qdown(cycle_id=cycle_id)

    assert fake.calls == [cycle_id]
    assert published["status"] == "published"
    assert published["cycle_id"] == cycle_id
    # to_dict() exposes layers/artifacts/lineage structure faithfully.
    assert published["layers"][0]["layer_type"] == "q_down_timeseries"
    assert published["lineage"]["quality_state"] == "degraded"


def test_publish_qdown_argparse_pass_prints_json_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(result=_published_result(cycle_id))
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    rc = cli._argparse_main(["publish-qdown", "--cycle-id", cycle_id])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "published"
    assert payload["cycle_id"] == cycle_id


# ---------------------------------------------------------------------------
# Part 1 - publish-qdown BLOCKED / failure (truthful reporting)
# ---------------------------------------------------------------------------
def test_publish_qdown_propagates_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePublisher(
        error=PublishError("NO_PUBLISHABLE_QDOWN_PRODUCTS", "nothing to publish", {"cycle_id": "gfs_2026052112"})
    )
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    with pytest.raises(PublishError) as excinfo:
        cli._publish_qdown(cycle_id="gfs_2026052112")
    assert excinfo.value.error_code == "NO_PUBLISHABLE_QDOWN_PRODUCTS"


def test_publish_qdown_wraps_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def publish_qdown_cycle(self, cycle_id: str) -> PublishResult:
            raise RuntimeError("boom")

    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: _Boom()))

    with pytest.raises(PublishError) as excinfo:
        cli._publish_qdown(cycle_id="gfs_2026052112")
    assert excinfo.value.error_code == "PUBLISH_QDOWN_FAILED"


def test_publish_qdown_argparse_failure_reports_failure_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(
        error=PublishError("NO_PUBLISHABLE_QDOWN_PRODUCTS", "nothing to publish", {"cycle_id": cycle_id})
    )
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    rc = cli._argparse_main(["publish-qdown", "--cycle-id", cycle_id])

    assert rc == 1
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    # Failure is reported truthfully, not faked as success.
    assert payload["status"] == "failed_publish"
    assert payload["error_code"] == "NO_PUBLISHABLE_QDOWN_PRODUCTS"
    assert payload["cycle_id"] == cycle_id
    assert payload["layers"] == []


def test_publish_qdown_failure_emits_no_false_live_readiness(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(error=PublishError("QDOWN_PUBLISH_FAILED", "db down"))
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    rc = cli._argparse_main(["publish-qdown", "--cycle-id", cycle_id])

    assert rc == 1
    out = capsys.readouterr().out
    # No-false-live-readiness: failure output must not claim a published/success state.
    assert '"status": "published"' not in out
    assert '"status": "failed_publish"' in out


# ---------------------------------------------------------------------------
# Part 1 - publish-qdown via the click entry point (production default path)
#
# main() prefers _click_main when click is importable, so the argparse tests
# above never exercise the code that actually runs in production. These cover
# the click wiring and assert it stays behaviourally identical to argparse.
# ---------------------------------------------------------------------------
def test_publish_qdown_click_pass_prints_published_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(result=_published_result(cycle_id))
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    # argv is not None -> _click_main runs cli.main(standalone_mode=False),
    # so the command's return value flows back instead of click sys.exit-ing.
    rc = cli._click_main(["publish-qdown", "--cycle-id", cycle_id])

    assert rc == 0
    assert fake.calls == [cycle_id]
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "published"
    assert payload["cycle_id"] == cycle_id
    assert payload["layers"][0]["layer_type"] == "q_down_timeseries"
    assert payload["lineage"]["quality_state"] == "degraded"


def test_publish_qdown_click_failure_reports_failure_payload_and_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cycle_id = "gfs_2026052112"
    fake = _FakePublisher(
        error=PublishError("NO_PUBLISHABLE_QDOWN_PRODUCTS", "nothing to publish", {"cycle_id": cycle_id})
    )
    monkeypatch.setattr(cli.TilePublisher, "from_env", classmethod(lambda cls: fake))

    # The click command raises SystemExit(1); with standalone_mode=False click
    # does not swallow it, so it propagates exactly like the argparse rc==1.
    with pytest.raises(SystemExit) as excinfo:
        cli._click_main(["publish-qdown", "--cycle-id", cycle_id])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    payload = json.loads(out.strip())
    # Failure is reported truthfully via failure_payload, never faked as success.
    assert payload["status"] == "failed_publish"
    assert payload["error_code"] == "NO_PUBLISHABLE_QDOWN_PRODUCTS"
    assert payload["cycle_id"] == cycle_id
    assert payload["layers"] == []
    # No-false-readiness: failure output must not claim a published/success state.
    assert '"status": "published"' not in out


# ---------------------------------------------------------------------------
# Part 2 - Slurm gateway deterministic state transitions (#258 semantics)
# ---------------------------------------------------------------------------
def _gateway_config(tmp_path: Path, *, url: str, service_port: int = 8000) -> ProductionSchedulerConfig:
    return ProductionSchedulerConfig(
        workspace_root=tmp_path,
        slurm_gateway_url=url,
        service_port=service_port,
    )


def _healthy_probe(_config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "mode": "slurm",
        "backend": "slurm",
        "healthy": True,
        "submit_capable": True,
        "accounting_available": True,
    }


def _unhealthy_probe(_config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "mode": "slurm",
        "healthy": False,
        "submit_capable": False,
        "accounting_available": False,
        "reason": "gateway connection refused",
    }


def test_gateway_healthy_probe_produces_no_blocker(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")
    checks, blockers = _slurm_gateway_check(config, probe=_healthy_probe)

    assert blockers == []
    assert checks["healthy"] is True
    assert checks["submit_capable"] is True
    assert checks["accounting_available"] is True


def test_gateway_unhealthy_probe_produces_unavailable_blocker(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")
    checks, blockers = _slurm_gateway_check(config, probe=_unhealthy_probe)

    codes = [blocker["code"] for blocker in blockers]
    assert codes == ["SLURM_GATEWAY_UNAVAILABLE"]
    assert checks["healthy"] is False


def test_gateway_state_transition_healthy_to_unhealthy_is_deterministic(tmp_path: Path) -> None:
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")

    # Reproducible: the same probe always yields the same (checks, blockers).
    first_checks, first_blockers = _slurm_gateway_check(config, probe=_healthy_probe)
    repeat_checks, repeat_blockers = _slurm_gateway_check(config, probe=_healthy_probe)
    assert (first_checks, first_blockers) == (repeat_checks, repeat_blockers)
    assert first_blockers == []

    # healthy -> unhealthy transition deterministically yields the blocker.
    _, transition_blockers = _slurm_gateway_check(config, probe=_unhealthy_probe)
    assert [blocker["code"] for blocker in transition_blockers] == ["SLURM_GATEWAY_UNAVAILABLE"]


def test_gateway_self_reference_produces_self_reference_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real slurm backend pointed at this service's own loopback listen address.
    monkeypatch.setattr("services.orchestrator.scheduler._slurm_gateway_backend", lambda: "slurm")
    config = _gateway_config(tmp_path, url="http://localhost:8000", service_port=8000)

    checks, blockers = _slurm_gateway_check(config, probe=_healthy_probe)

    codes = [blocker["code"] for blocker in blockers]
    assert codes == ["SLURM_GATEWAY_SELF_REFERENCE"]
    # Self-reference is decisive: the health probe is not consulted.
    assert "healthy" not in checks
    assert checks["self_reference"] is True


def test_gateway_blocker_evidence_is_redacted_without_secrets(tmp_path: Path) -> None:
    secret_url = "http://user:super-secret-token@gateway.example.test:9100"
    config = _gateway_config(tmp_path, url=secret_url)

    def _probe_with_secret(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {
            "mode": "slurm",
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": "auth failed",
        }

    checks, blockers = _slurm_gateway_check(config, probe=_probe_with_secret)

    # Credentials in the gateway URL never reach evidence (userinfo stripped).
    serialized = json.dumps({"checks": checks, "blockers": blockers})
    assert "super-secret-token" not in serialized
    assert blockers[0]["host"] == "gateway.example.test"


def test_gateway_probe_credential_in_checks_is_scrubbed_by_redact_payload(tmp_path: Path) -> None:
    """Exercise the redact_payload main path on a field that lands in ``checks``.

    Unlike the URL-only case above (where userinfo is stripped before it ever
    reaches evidence), here a credential-shaped string is returned by the probe
    in ``version``, flows into ``checks``, and must be scrubbed by the
    ``redact_payload(checks)`` call at the end of ``_slurm_gateway_check``.
    """
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")

    def _probe_with_secret_version(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {
            "mode": "slurm",
            "backend": "slurm",
            "version": "v1 token=super-secret-token password=hunter2",
            "healthy": True,
            "submit_capable": True,
            "accounting_available": True,
        }

    checks, blockers = _slurm_gateway_check(config, probe=_probe_with_secret_version)

    # A healthy gateway adds no blocker (never-break-userspace).
    assert blockers == []
    # redact_payload's SENSITIVE_ASSIGNMENT_RE rewrote the secret assignments.
    assert "super-secret-token" not in json.dumps(checks)
    assert "hunter2" not in json.dumps(checks)
    assert checks["version"] == "v1 token=[redacted] password=[redacted]"


def test_gateway_probe_raises_is_caught_and_blocked_with_redacted_reason(tmp_path: Path) -> None:
    """A probe that raises must fail safe as BLOCKED, not crash the preflight.

    The injected exception text carries a credential; the fail-safe branch
    redacts the reason before it reaches the blocker evidence.
    """
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")

    def _probe_raises(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        raise RuntimeError("boom token=super-secret-token")

    # Must not propagate the RuntimeError to the caller.
    checks, blockers = _slurm_gateway_check(config, probe=_probe_raises)

    codes = [blocker["code"] for blocker in blockers]
    assert codes == ["SLURM_GATEWAY_UNAVAILABLE"]
    assert checks["healthy"] is False
    # The exception message is redacted everywhere it surfaces.
    serialized = json.dumps({"checks": checks, "blockers": blockers})
    assert "super-secret-token" not in serialized
    assert blockers[0]["reason"] == "boom token=[redacted]"


def test_gateway_probe_returned_reason_credential_is_redacted_in_blocker(tmp_path: Path) -> None:
    """A probe that *returns* (not raises) an unhealthy result with a
    credential-shaped ``reason`` must have that reason redacted before it lands
    in the blocker evidence -- the normal-return branch was previously leaking
    the raw reason into the un-redacted ``blockers`` list.
    """
    config = _gateway_config(tmp_path, url="http://gateway.example.test:9100")

    def _probe_with_secret_reason(_config: ProductionSchedulerConfig) -> dict[str, Any]:
        return {
            "mode": "slurm",
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": "auth failed token=super-secret-token password=hunter2",
        }

    checks, blockers = _slurm_gateway_check(config, probe=_probe_with_secret_reason)

    # Structure is preserved: the blocker code still identifies the failure.
    codes = [blocker["code"] for blocker in blockers]
    assert codes == ["SLURM_GATEWAY_UNAVAILABLE"]
    # The credential-shaped reason is scrubbed before reaching evidence.
    serialized = json.dumps({"checks": checks, "blockers": blockers})
    assert "super-secret-token" not in serialized
    assert "hunter2" not in serialized
    assert blockers[0]["reason"] == "auth failed token=[redacted] password=[redacted]"
