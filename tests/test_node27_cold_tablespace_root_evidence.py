"""Static root-evidence prerequisite contracts for the disposable oracle."""

from __future__ import annotations

import ast
import importlib.util
import os
import socket
from pathlib import Path

import pytest

from packages.common.node27_cold_tablespace_integration import default_config, root_evidence_setup_argv

_ROOT = Path(__file__).resolve().parents[1]
_HELPER = _ROOT / "scripts/node27_cold_tablespace_root_evidence_setup.py"


def _helper_module():
    spec = importlib.util.spec_from_file_location("node27_cold_tablespace_root_evidence_setup", _HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(tmp_path: Path, *, hostname: str):
    module = _helper_module()
    root = tmp_path / "nhms-1894-tablespace-deadbeef"
    return module.build_parser().parse_args(
        [
            "--action",
            "prepare",
            "--work-root",
            str(root),
            "--cold-path",
            str(root / "cold"),
            "--pgdata",
            str(root / "pgdata"),
            "--evidence-root",
            str(root / "evidence"),
            "--container-name",
            "nhms-1894-tablespace-deadbeef",
            "--prior-container-name",
            "nhms-1894-tablespace-deadbeef-before",
            "--host-port",
            "55494",
            "--image-id",
            "sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e",
            "--image-ref",
            "timescale/timescaledb-ha:pg15-latest",
            "--hostname",
            hostname,
            "--reader-gid",
            str(os.getgid()),
        ]
    )


def test_root_evidence_setup_uses_direct_sudo_argv_and_preserves_root_owner_contract(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / "nhms-1894-tablespace-deadbeef")
    argv = root_evidence_setup_argv(config, action="prepare")
    source = _HELPER.read_text(encoding="utf-8")

    assert argv[:2] == ("/usr/bin/sudo", "-n")
    assert "shell=True" not in source
    assert "os.geteuid() != 0" in source
    assert "expected_uid=os.getuid" not in source
    assert "args.reader_gid" in source
    assert "path.chmod(0o640)" in source


@pytest.mark.parametrize("hostname", ("nhms-1894-wrong-host", "wrong-host"))
def test_root_evidence_helper_rejects_noncanonical_hostname_before_any_path_mutation(
    tmp_path: Path, monkeypatch, hostname: str
) -> None:
    module = _helper_module()
    args = _arguments(tmp_path, hostname=hostname)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="hostname"):
        module._validate(args)

    assert not args.pgdata.exists()
    assert not args.evidence_root.exists()
    assert not args.cold_path.exists()


def test_root_evidence_helper_accepts_exact_synthetic_hostname_at_validation_seam(tmp_path: Path, monkeypatch) -> None:
    module = _helper_module()
    args = _arguments(tmp_path, hostname=f"nhms-1894-{socket.gethostname()}")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    module._validate(args)

    assert not args.pgdata.exists()
    assert not args.evidence_root.exists()
    assert not args.cold_path.exists()


def test_root_evidence_helper_has_only_explicit_owned_actions() -> None:
    tree = ast.parse(_HELPER.read_text(encoding="utf-8"), filename=str(_HELPER))
    choices = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "choices" and isinstance(node.value, ast.Tuple)
    ]

    assert any(
        {item.value for item in choice.elts if isinstance(item, ast.Constant)}
        == {"prepare", "create-cold-path", "cleanup"}
        for choice in choices
    )
