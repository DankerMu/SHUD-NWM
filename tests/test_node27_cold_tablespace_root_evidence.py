"""Static root-evidence prerequisite contracts for the disposable oracle."""

from __future__ import annotations

import ast
import importlib.util
import os
import socket
from pathlib import Path

import pytest

from packages.common import node27_cold_tablespace_evidence as evidence
from packages.common.node27_cold_tablespace_evidence import (
    EvidencePolicy,
    parse_backup_inventory,
    verify_root_storage_evidence,
)
from packages.common.node27_cold_tablespace_integration import (
    IntegrationResources,
    RootEvidenceCapability,
    default_config,
    root_evidence_setup_argv,
)

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
            "--runtime-uid",
            "1005",
            "--runtime-gid",
            "1005",
            "--reader-gid",
            str(os.getgid()),
        ]
    )


def test_root_evidence_setup_uses_direct_sudo_argv_and_preserves_root_owner_contract(tmp_path: Path) -> None:
    config = default_config(work_root=tmp_path / "nhms-1894-tablespace-deadbeef")
    resources = IntegrationResources(
        config=config,
        capability=RootEvidenceCapability(
            strategy="sudo",
            image_postgres_uid=1000,
            image_postgres_gid=1000,
            runtime_uid=1005,
            runtime_gid=1005,
            image_id=config.image_id,
            image_ref=config.image_ref,
            image_default_user="postgres",
            root_proof="sudo-noninteractive",
        ),
    )
    argv = root_evidence_setup_argv(resources, action="prepare")
    source = _HELPER.read_text(encoding="utf-8")

    assert argv[:2] == ("/usr/bin/sudo", "-n")
    assert argv[argv.index("--runtime-uid") + 1] == "1005"
    assert argv[argv.index("--runtime-gid") + 1] == "1005"
    assert "--postgres-uid" not in argv
    assert "shell=True" not in source
    assert "os.geteuid() != 0" in source
    assert "expected_uid=os.getuid" not in source
    assert "args.runtime_uid" in source
    assert "args.reader_gid" in source
    assert "path.chmod(0o640)" in source


def test_host_rendered_documents_preserve_production_parser_contract_after_root_seal(
    tmp_path: Path, monkeypatch
) -> None:
    module = _helper_module()
    args = _arguments(tmp_path, hostname=f"nhms-1894-{socket.gethostname()}")
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)

    args.work_root.mkdir(mode=0o700)
    args.work_root.chmod(0o700)
    module._validate_render(args)
    module._render_evidence(args)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    module._validate(args)
    monkeypatch.setattr(module.os, "chown", lambda _path, _uid, _gid: None)
    module._seal_evidence(args)
    original_fstat = evidence.os.fstat

    class RootOwnedStat:
        def __init__(self, observed) -> None:
            self._observed = observed
            self.st_uid = 0

        def __getattr__(self, name: str):
            return getattr(self._observed, name)

    monkeypatch.setattr(evidence.os, "fstat", lambda fd: RootOwnedStat(original_fstat(fd)))

    policy = EvidencePolicy(
        expected_hostname=args.hostname,
        array_device="/dev/md0",
        max_age_seconds=300,
        expected_uid=0,
        approved_modes=(0o640,),
        mdadm_argv=("/usr/sbin/mdadm", "--detail", "/dev/md0"),
        smartctl_prefix=("/usr/sbin/smartctl",),
        backup_argv=("/usr/local/sbin/nhms-backup-inventory", "--json"),
        expected_pgdata=str(args.pgdata),
    )
    health = verify_root_storage_evidence(
        args.evidence_root / "mdadm.json",
        {device: args.evidence_root / f"smart-{Path(device).name}.json" for device in ("/dev/sdb1", "/dev/sdc1")},
        policy=policy,
        now=module.datetime.now(module.UTC),
    )
    backup = parse_backup_inventory(
        args.evidence_root / "backup.json",
        policy=policy,
        external_targets=("/home/postgres/pgdata/tablespaces/nhms_cold",),
        now=module.datetime.now(module.UTC),
    )

    assert health.healthy is True
    assert backup.complete is True


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
        == {"prepare", "render", "seal", "create-cold-path", "cleanup"}
        for choice in choices
    )
