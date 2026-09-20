"""The manual publisher CLI: cutover gate, failure payload and operator warning.

Partition (#1102 partition of the 3218-line / 59-case
tests/test_publish_scheduler_file_registry.py). `main()`'s three cutover-gate
outcomes (refusal without a bypass, `--allow-uncovered` with its warning, and a
declaration resolved from the environment), the #1132 proof that every failure
payload's `cutover_gate` block comes from the shared normalizer, and the #1104
operator-gate startup warning on both a successful and a failing run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.publish_scheduler_file_registry as registry_script
from tests.provider_mode_helpers import make_directory_with_explicit_mode, write_provider_destination
from tests.publish_registry_helpers import (
    _fake_publish_basins_package,
    _fake_sources,
    _inventory_model,
    _stub_source_identity_for_synthetic_inventories,  # noqa: F401  (registers the autouse stub on this module)
)

# ---------------------------------------------------------------------------
# Round-2 fix pass (#1080): manual publisher CLI now wires the cutover gate.
#
# The former round-1 scaffolding "gate refusal preserves canonical bytes under
# the same lock" test was removed per R2-N6 in the round-2 review: it wrapped
# the publisher in a test-owned destination lock but did NOT prove the
# publisher itself acquires that same canonical lock at replace time, so the
# concurrency invariant it claimed to test lived entirely in test scaffolding.
# The truthful coverage lives in
# `tests/test_scheduler_file_provider_refresh.py::test_full_runner_refresh_lock_is_held_during_precommit_gate`
# (T13, part a) which instruments the runner's real `refresh_lock` and proves
# a competing non-blocking acquire fails while the gate runs.
# ---------------------------------------------------------------------------


def test_manual_cli_refuses_undeclared_package_cutover_without_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T12 / C-D1: the manual CLI now refuses when the prospective set has
    a ``package_changed`` row and no cutover declaration is filed.  Without
    ``--allow-uncovered-cutover`` the wired gate must reject; the previous
    canonical bytes remain intact."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-13T00:00:00Z",
                # Same model_id as the prospective row, but a different
                # package_checksum -> package_changed.
                "models": [
                    {
                        "model_id": "basins_first_shud",
                        "basin_id": "basins_first",
                        "model_package_uri": "s3://nhms/models/basins_first_shud/OLD/package/",
                        "manifest_uri": "s3://nhms/models/basins_first_shud/OLD/manifest.json",
                        "package_checksum": "package-sha-basins_first_shud-OLD",
                    }
                ],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n"
    )
    before = canonical.read_bytes()
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
    ]

    exit_code = registry_script.main(argv)
    assert exit_code != 0
    captured = capsys.readouterr()
    err = captured.err
    # The refusal payload includes the wired-gate provider_reason.
    assert "registry_cutover_undeclared" in err, err
    # Canonical bytes untouched.
    assert canonical.read_bytes() == before
    # R2-A1: the refusal error payload records the cutover_gate audit fact
    # (enforced, declaration_present=False) so a later auditor reading stderr
    # can distinguish "gate ran and refused" from "gate was skipped".
    refusal_payload = json.loads(err.strip().splitlines()[-1])
    audit = refusal_payload.get("cutover_gate")
    assert audit == {
        "mode": "enforced",
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": False,
    }, refusal_payload


def test_manual_cli_allow_uncovered_bypasses_gate_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T12 / C-D1: ``--allow-uncovered-cutover`` bypasses the gate and
    prints a stderr WARNING banner.  This is the bootstrap/one-off recovery
    seam, not a regular publish path."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(canonical.parent)
    write_provider_destination(
        canonical,
        json.dumps(
            {
                "schema_version": "nhms.scheduler.file_model_registry.v1",
                "generated_at": "2026-07-13T00:00:00Z",
                "models": [
                    {
                        "model_id": "basins_first_shud",
                        "basin_id": "basins_first",
                        "model_package_uri": "s3://nhms/models/basins_first_shud/OLD/package/",
                        "manifest_uri": "s3://nhms/models/basins_first_shud/OLD/manifest.json",
                        "package_checksum": "package-sha-basins_first_shud-OLD",
                    }
                ],
                "checksum": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ).encode()
        + b"\n",
    )
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
        "--allow-uncovered-cutover",
    ]
    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    err = captured.err
    out = captured.out
    # #1104 (P2-4): a bare `"WARNING" in err` became vacuous once every run
    # emits the operator-gate startup warning, so pin the bypass banner itself.
    assert "WARNING: --allow-uncovered-cutover disables the #1080 registry" in err
    assert "allow-uncovered-cutover" in err
    # With bypass, publish should proceed (canonical bytes replaced).
    assert exit_code == 0
    assert canonical.read_bytes() != json.dumps(
        {
            "schema_version": "nhms.scheduler.file_model_registry.v1",
            "generated_at": "2026-07-13T00:00:00Z",
        },
        sort_keys=True,
    ).encode()
    # R2-A1: the summary emitted to stdout records the bypass on
    # `cutover_gate.mode` alongside the stderr WARNING, so persisted CLI
    # output distinguishes a bypass run from a gate-passing run without
    # relying on the ephemeral WARNING line.
    summary = json.loads(out)
    assert (
        summary["schema_version"]
        == "nhms.scheduler.basins_file_registry_publish.v2"
    )
    assert summary["cutover_gate"] == {
        "mode": "bypassed_allow_uncovered_cutover",
        "declaration_env": None,
        "declaration_present": False,
    }
    # The bypass audit fact also surfaces on the manifest publication
    # receipt so downstream operators reading `manifest-last.json`'s
    # companion receipt see the same fact.
    assert summary["registry"]["cutover_gate"] == summary["cutover_gate"]


def test_manual_cli_records_declaration_present_when_env_resolves_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R2-A1: when the CLI runs with the gate enforced AND the operator has
    staged a declaration file the runner can open, the persisted CLI output
    (summary on happy path, error payload on refusal path) records
    ``cutover_gate={mode:enforced, declaration_env:<env>,
    declaration_present:true}``.  This closes the byte-identical hole where
    an enforced-with-declaration run and an enforced-no-declaration run
    would otherwise be indistinguishable in later audit.

    Using bootstrap-shaped setup (no previous canonical) with a schema-valid
    but generation-mismatched declaration: the gate correctly refuses on
    declaration_invalid, and the assertion under test is that the audit
    still reports ``declaration_present=True`` — the audit is about "did
    the operator stage a file the runner could open", not "did the gate
    ultimately accept it"."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    canonical.parent.mkdir(parents=True)
    # Schema-valid declaration file (readable, correct JSON shape) but
    # deliberately-unmatched generation — the audit still records
    # `declaration_present=True` because the operator DID stage a file the
    # runner could open.  A separate audit fact would record the gate's
    # decision on the declaration's semantic validity.
    declaration_path = tmp_path / "declarations" / "cutover.json"
    declaration_path.parent.mkdir(parents=True)
    declaration_path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.scheduler.registry_package_cutover.v1",
                "generated_at": "2026-07-14T12:00:00Z",
                "generation": "manifest-000000000000",
                "entries": [],
            },
            sort_keys=True,
        )
        + "\n"
    )
    monkeypatch.setenv(
        "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", str(declaration_path)
    )

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
    ]
    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    # Either exit code is acceptable here; the audit fact is what's under
    # test.  Read from whichever channel carries the payload.
    payload_json = captured.out if exit_code == 0 else captured.err.strip().splitlines()[-1]
    payload = json.loads(payload_json)
    assert payload["cutover_gate"] == {
        "mode": "enforced",
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": True,
    }, payload


# ---------------------------------------------------------------------------
# #1132: CLI failure diagnostics route through the shared normalizer
# ---------------------------------------------------------------------------


_CLI_FAILURE_TRIGGERS = (
    "registry_publish_error",
    "basins_discovery_error",
    "file_provider_error",
)
# A legal three-field block: an illegal sentinel would be refused by the
# un-stubbed services-side normalizer and the CLI would fail down a different
# branch (pass for the wrong reason).
_NORMALIZER_SENTINEL = {
    "mode": "not_wired",
    "declaration_env": "SENTINEL_ENV",
    "declaration_present": False,
}


def _install_cli_failure(
    trigger: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive ``main`` into one specific stderr failure branch, cutover-free.

    Each trigger is a deterministic error with no cutover-gate involvement, so
    the payload assertion cannot pass because of an unrelated refusal.
    """
    options = {
        "--basins-root": str(tmp_path / "Basins"),
        "--registry-manifest": str(tmp_path / "shared/scheduler/registry/manifest-last.json"),
        "--object-store-root": str(tmp_path / "private-objects"),
        "--object-store-prefix": "s3://nhms",
        "--work-dir": str(tmp_path / "work"),
    }
    if trigger == "registry_publish_error":
        monkeypatch.delenv("OBJECT_STORE_PREFIX", raising=False)
        options["--object-store-prefix"] = ""
    elif trigger == "basins_discovery_error":
        options["--basins-root"] = str(tmp_path / "missing-basins")
    else:
        def raise_provider_error(**kwargs: object) -> dict[str, Any]:
            del kwargs
            raise registry_script.SchedulerFileProviderError(
                "registry_manifest_invalid", field="models", evidence={"phase": "publish"}
            )

        monkeypatch.setattr(
            registry_script, "publish_all_basin_scheduler_registry", raise_provider_error
        )
    return [item for pair in options.items() for item in pair]


@pytest.mark.parametrize("trigger", _CLI_FAILURE_TRIGGERS)
def test_cli_failure_payload_cutover_gate_is_produced_by_shared_normalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    trigger: str,
) -> None:
    """#1132: every stderr failure payload must carry the SHARED normalizer's
    output, not an inline literal.

    Pins the call, not the value: the legal literals the CLI builds are fixed
    points of normalization, so a value-equality assertion stays green against
    an implementation that never calls the normalizer.  Stubbing the normalizer
    to return a sentinel is the only assertion that bites.
    """
    monkeypatch.setattr(
        registry_script,
        "normalize_cutover_gate_audit",
        lambda cutover_gate: dict(_NORMALIZER_SENTINEL),
    )
    argv = _install_cli_failure(trigger, tmp_path, monkeypatch)

    assert registry_script.main(argv) == 1

    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["cutover_gate"] == _NORMALIZER_SENTINEL, payload


# ---------------------------------------------------------------------------
# #1104: manual-publisher concurrency is operator-gated, not CAS-gated.
#
# `main()` never populates `expected_preimage`, so nothing in code stops the
# manual CLI from overwriting a refresh commit that lands between its snapshot
# and its own commit.  The mitigation is an explicit runbook prohibition, and
# the CLI must point every operator at it on startup.  These pins prove the
# warning reaches stderr on both a successful and a failing run, and that the
# machine-readable failure payload still parses from the final stderr line.
# ---------------------------------------------------------------------------

_REFRESH_TIMER_UNIT = "nhms-scheduler-file-provider-refresh.timer"

# Captured from the pre-#1104 CLI for the `registry_publish_error` trigger:
# the startup warning must not perturb one byte of this payload.
_PREFIX_MISSING_FAILURE_PAYLOAD = {
    "cutover_gate": {
        "declaration_env": "NHMS_REGISTRY_CUTOVER_DECLARATION_PATH",
        "declaration_present": False,
        "mode": "enforced",
    },
    "error_code": "SCHEDULER_REGISTRY_OBJECT_STORE_PREFIX_MISSING",
    "message": "OBJECT_STORE_PREFIX or --object-store-prefix is required.",
}


def test_cli_prints_operator_gate_warning_on_successful_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1104 / 2.1: a run that publishes successfully still emits the
    operator-gate startup warning as its FIRST stderr line, naming the refresh
    timer unit, and leaves the stdout summary untouched."""
    inventory = {
        "schema_version": "basins.discovery.v1",
        "root": str(tmp_path / "Basins"),
        "resolved_root": str(tmp_path / "Basins"),
        "model_count": 1,
        "models": [_inventory_model("first")],
        "warnings": [],
    }
    monkeypatch.setattr(registry_script, "discover_basins_inventory", lambda _root: inventory)
    monkeypatch.setattr(registry_script, "publish_basins_package", _fake_publish_basins_package)
    monkeypatch.setattr(
        registry_script,
        "prepare_basins_import_sources",
        lambda inventory_path, package_manifest_path: _fake_sources(
            inventory, Path(package_manifest_path)
        ),
    )
    canonical = tmp_path / "shared/scheduler/registry/manifest-last.json"
    make_directory_with_explicit_mode(canonical.parent)
    monkeypatch.delenv("NHMS_REGISTRY_CUTOVER_DECLARATION_PATH", raising=False)

    argv = [
        "--basins-root",
        str(tmp_path / "Basins"),
        "--registry-manifest",
        str(canonical),
        "--object-store-root",
        str(tmp_path / "private-objects"),
        "--object-store-prefix",
        "s3://nhms",
        "--work-dir",
        str(tmp_path / "work"),
        # Same escape hatch as `_NO_DECLARATION`, spelled the way the CLI
        # exposes it (an empty value loads no declaration at all).
        "--calibration-overrides",
        "",
        # Bootstrap bypass: the only deterministic exit-0 CLI path in this
        # suite.  The pin below is on the startup warning, which is emitted
        # before the bypass branch is even evaluated.
        "--allow-uncovered-cutover",
    ]

    exit_code = registry_script.main(argv)
    captured = capsys.readouterr()
    startup_line = captured.err.strip().splitlines()[0]

    assert _REFRESH_TIMER_UNIT in startup_line, captured.err
    assert "WARNING" in startup_line, captured.err
    # P2-4: the startup warning must stay distinguishable from the
    # `--allow-uncovered-cutover` bypass banner, which is asserted separately.
    assert "allow-uncovered-cutover" not in startup_line, captured.err
    assert exit_code == 0
    # The warning is stderr-only: the stdout summary contract is unchanged.
    summary = json.loads(captured.out)
    assert summary["schema_version"] == "nhms.scheduler.basins_file_registry_publish.v2"


def test_cli_startup_warning_coexists_with_failure_json_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1104 / 2.2: on a deterministic failure the startup warning leads
    stderr and the existing JSON error payload still parses byte-for-byte
    identically from the final stderr line — the whole suite (and node-22
    operators) read that channel with ``strip().splitlines()[-1]``."""
    argv = _install_cli_failure("registry_publish_error", tmp_path, monkeypatch)

    assert registry_script.main(argv) == 1

    err = capsys.readouterr().err
    lines = err.strip().splitlines()
    assert _REFRESH_TIMER_UNIT in lines[0], err
    assert json.loads(lines[-1]) == _PREFIX_MISSING_FAILURE_PAYLOAD, err
    # Exactly one added line: warning + payload, nothing else on the channel.
    assert len(lines) == 2, err
