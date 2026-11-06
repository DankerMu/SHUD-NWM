"""#3059: the #2240 production PGDATA relocation identity is retired.

The 14T RAID target the tool was contracted against was removed from node-27 on
2026-11-05 (ADR 0002 Revision 2026-11-05). Production invocations must refuse
through the public CLI with an explicit reason; only disposable mechanics stay.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.node27_pgdata_host import Host, MigrationError, path_identity
from packages.common.node27_pgdata_migrate import DEFAULTS, Migration
from scripts.node27_pgdata_migrate import main

FINDMNT = "/usr/bin/findmnt"


class ProductionShapedHost(Host):
    """Real host owner with only the process boundary (`command`) replaced.

    The read-only workspace durability probe (`findmnt`) answers with a durable
    ext4 mount; any other command (docker, systemctl, ...) is recorded and fails,
    so a retired production invocation that reaches the container is visible.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def command(self, argv, *, timeout=30, allow_failure=False, max_bytes=1024 * 1024):
        self.commands.append(list(argv))
        if argv[0] == FINDMNT:
            row = {"source": "/dev/mapper/vg-home", "fstype": "ext4", "target": "/home", "options": "rw"}
            return SimpleNamespace(returncode=0, stdout=json.dumps({"filesystems": [row]}), stderr="")
        raise MigrationError("unexpected host command in retired production path")

    def non_probe_commands(self) -> list[list[str]]:
        return [argv for argv in self.commands if argv[0] != FINDMNT]


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "durable" / "workspace"
    workspace.mkdir(parents=True, mode=0o700)
    workspace.chmod(0o700)
    return workspace


def _assert_retirement_reason(text: str) -> None:
    assert "production PGDATA relocation is retired" in text
    assert "ADR 0002 Revision 2026-11-05" in text


@pytest.fixture
def production_host(monkeypatch: pytest.MonkeyPatch) -> ProductionShapedHost:
    host = ProductionShapedHost()
    monkeypatch.setattr("scripts.node27_pgdata_migrate.Migration", lambda path: Migration(path, host=host))
    return host


@pytest.fixture
def lifecycle_lock_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []

    def refuse(*args):
        calls.append(args)
        raise AssertionError("retired production action must refuse before the lifecycle lock")

    monkeypatch.setattr("packages.common.node27_pgdata_migrate.timeseries_lifecycle_lock", refuse)
    return calls


def test_production_plan_reports_relocation_retired_as_its_only_blocker(
    tmp_path: Path, production_host: ProductionShapedHost, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)

    assert main(["--workspace", str(workspace)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["mutation"] is False and payload["action"] == "plan"
    assert len(payload["blockers"]) == 1
    _assert_retirement_reason(payload["blockers"][0])
    assert production_host.non_probe_commands() == []
    assert not (workspace / "state.json").exists()


def _frozen_production_state(workspace: Path, stage: str, *, writes_released: bool) -> dict:
    state = {
        "schema_version": 1,
        "stage": stage,
        "writes_released": writes_released,
        "operation": "ab" * 16,
        "workspace": str(workspace),
        "workspace_identity": path_identity(workspace),
        "config": dict(DEFAULTS),
    }
    path = workspace / "state.json"
    path.write_text(json.dumps(state))
    path.chmod(0o600)
    return state


def test_production_prepare_refuses_before_lifecycle_lock(
    tmp_path: Path,
    production_host: ProductionShapedHost,
    lifecycle_lock_calls: list[tuple],
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = _workspace(tmp_path)

    assert main(["--workspace", str(workspace), "--action", "prepare", "--enforce"]) == 2

    captured = capsys.readouterr()
    error = json.loads(captured.err)
    assert error["ok"] is False
    _assert_retirement_reason(error["error"])
    assert captured.out == ""
    assert lifecycle_lock_calls == []
    assert production_host.non_probe_commands() == []
    assert not (workspace / "state.json").exists()


@pytest.mark.parametrize(
    ("action", "stage", "writes_released"),
    [
        ("copy", "prepared", False),
        ("activate", "copy_verified", False),
        ("rollback", "prepared", False),
        ("release", "activated_readonly", False),
        ("release", "release_intent", True),
    ],
)
def test_frozen_production_workspace_refuses_allowed_transition_before_lifecycle_lock(
    tmp_path: Path,
    production_host: ProductionShapedHost,
    lifecycle_lock_calls: list[tuple],
    capsys: pytest.CaptureFixture[str],
    action: str,
    stage: str,
    writes_released: bool,
) -> None:
    workspace = _workspace(tmp_path)
    state = _frozen_production_state(workspace, stage, writes_released=writes_released)

    assert main(["--workspace", str(workspace), "--action", action, "--enforce"]) == 2

    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    _assert_retirement_reason(error["error"])
    assert lifecycle_lock_calls == []
    assert production_host.non_probe_commands() == []
    assert json.loads((workspace / "state.json").read_text()) == state


def test_production_health_refuses_retired_raid_target(tmp_path: Path) -> None:
    host = ProductionShapedHost()
    config = {
        **DEFAULTS,
        "mdadm_evidence": str(tmp_path / "mdadm.txt"),
        "smart_evidence": {"/dev/sda": str(tmp_path / "sda.txt"), "/dev/sdb": str(tmp_path / "sdb.txt")},
    }

    with pytest.raises(MigrationError) as refused:
        host.health(config)

    _assert_retirement_reason(str(refused.value))
    assert host.non_probe_commands() == []


def test_disposable_health_scope_is_unchanged() -> None:
    assert ProductionShapedHost().health({**DEFAULTS, "disposable_root": "/tmp/nhms-pgdata-oracle-" + "0" * 32}) == {
        "scope": "isolated-mechanics-only",
        "hardware_health": "not-applicable",
    }
