from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_two_node_docker_source_trust.py"


def test_source_trust_preflight_passes_for_trusted_owner_and_0600_role_envs(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = checkout / "artifacts" / "evidence"

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[f"{_current_owner()},root-equivalent-example"],
        roles=["compute,display"],
    )

    assert result.returncode == 0
    assert result.stderr == ""
    summary = json.loads((evidence_root / "two-node-docker-source-trust.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["roles"] == ["compute", "display"]
    assert summary["blockers"] == []
    checked_labels = {item["label"] for item in summary["checked_paths"]}
    assert {
        "trust path component",
        "checkout root",
        "infra directory",
        "compute role env",
        "display role env",
        "compute compose source",
        "display compose source",
        "env source directory",
        "systemd source directory",
        "compute systemd unit source",
        "display systemd unit source",
    } <= checked_labels
    for record in summary["checked_paths"]:
        assert record["exists"] is True
        assert record["trusted_owner"] is True
        assert record["is_symlink"] is False
        assert record["group_writable"] is False
        assert record["world_writable"] is False
        if record["expected_kind"] == "directory":
            assert record["is_directory"] is True
        else:
            assert record["is_regular"] is True
    role_modes = {
        record["label"]: record["mode"]
        for record in summary["checked_paths"]
        if record["label"].endswith("role env")
    }
    assert role_modes == {"compute role env": "0600", "display role env": "0600"}
    evidence_text = (evidence_root / "two-node-docker-source-trust.txt").read_text(encoding="utf-8")
    assert "status: PASS" in evidence_text
    assert "writer-secret" not in json.dumps(summary)
    assert "readonly-secret" not in evidence_text


def test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = Path("/scratch/frd_muziyao/nwm-test/source-trust-explicit/docker-security")

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["compute"],
        evidence_run_id="source-trust-explicit",
    )

    assert result.returncode == 0
    summary = json.loads((evidence_root / "two-node-docker-source-trust-compute.json").read_text(encoding="utf-8"))
    assert summary["status"] == "PASS"
    assert summary["evidence_run_id"] == "source-trust-explicit"
    assert summary["roles"] == ["compute"]
    checked_labels = {item["label"] for item in summary["checked_paths"]}
    assert "compute role env" in checked_labels
    assert "display role env" not in checked_labels
    assert not (evidence_root / "two-node-docker-source-trust.json").exists()
    for path in (
        evidence_root / "two-node-docker-source-trust-compute.json",
        evidence_root / "two-node-docker-source-trust-compute.txt",
    ):
        path.unlink()
    evidence_root.rmdir()
    evidence_root.parent.rmdir()


def test_source_trust_allows_repo_artifacts_evidence_root(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = checkout / "artifacts" / "source-trust"

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["display"],
    )

    assert result.returncode == 0
    assert (evidence_root / "two-node-docker-source-trust-display.json").is_file()
    assert (evidence_root / "two-node-docker-source-trust-display.txt").is_file()


def test_source_trust_rejects_unapproved_tmp_evidence_root(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = tmp_path / "outside-evidence"

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["compute"],
    )

    assert result.returncode == 2
    assert "evidence root must be under checkout artifacts/ or /scratch/frd_muziyao" in result.stderr
    assert not evidence_root.exists()


def test_source_trust_rejects_symlink_evidence_root(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    real_evidence = checkout / "artifacts" / "real-evidence"
    real_evidence.mkdir(parents=True)
    evidence_root = checkout / "artifacts" / "linked-evidence"
    evidence_root.symlink_to(real_evidence, target_is_directory=True)

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["compute"],
    )

    assert result.returncode == 2
    assert "must not be a symlink" in result.stderr
    assert not (real_evidence / "two-node-docker-source-trust-compute.json").exists()


def test_source_trust_rejects_existing_symlink_text_target(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = checkout / "artifacts" / "source-trust"
    evidence_root.mkdir(parents=True)
    target = tmp_path / "outside-text-target"
    target.write_text("do not overwrite\n", encoding="utf-8")
    text_target = evidence_root / "two-node-docker-source-trust-display.txt"
    text_target.symlink_to(target)

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["display"],
    )

    assert result.returncode == 2
    assert "must not be a symlink" in result.stderr
    assert target.read_text(encoding="utf-8") == "do not overwrite\n"
    assert not (evidence_root / "two-node-docker-source-trust-display.json").exists()


def test_source_trust_blocks_0644_role_env_before_direct_compose_sentinel(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    display_env = checkout / "infra" / "env" / "display.env"
    display_env.chmod(0o644)
    evidence_root = checkout / "artifacts" / "evidence with spaces"
    sentinel = tmp_path / "compose-sentinel"

    command = _shell_command(
        [
            sys.executable,
            str(SCRIPT),
            "--checkout-root",
            str(checkout),
            "--evidence-root",
            str(evidence_root),
            "--trust-root",
            str(tmp_path),
            "--trusted-owner",
            _current_owner(),
            "--role",
            "display",
        ]
    )
    script = f"{command} && touch {shlex.quote(str(sentinel))}"

    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 2
    assert "BLOCKED:" in result.stderr
    assert "display role env must be mode 0600" in result.stderr
    assert not sentinel.exists()
    summary = json.loads((evidence_root / "two-node-docker-source-trust-display.json").read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"


def test_source_trust_untrusted_owner_allowlist_covers_role_env_and_sources(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    evidence_root = checkout / "artifacts" / "evidence"

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=["definitely-not-the-current-owner"],
        roles=["compute", "display"],
    )

    assert result.returncode == 2
    assert "BLOCKED:" in result.stderr
    summary = json.loads((evidence_root / "two-node-docker-source-trust.json").read_text(encoding="utf-8"))
    blocker_text = json.dumps(summary["blockers"])
    assert "compute role env has untrusted owner" in blocker_text
    assert "display role env has untrusted owner" in blocker_text
    assert any(
        phrase in blocker_text
        for phrase in (
            "compute compose source has untrusted owner",
            "display compose source has untrusted owner",
            "systemd source directory has untrusted owner",
            "env source directory has untrusted owner",
        )
    )


def test_source_trust_rejects_symlink_source(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path / "checkout")
    compose = checkout / "infra" / "compose.display.yml"
    target = tmp_path / "display-compose-target.yml"
    target.write_text("services: {}\n", encoding="utf-8")
    compose.unlink()
    compose.symlink_to(target)
    evidence_root = checkout / "artifacts" / "evidence"

    result = _run_preflight(
        checkout_root=checkout,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["display"],
    )

    assert result.returncode == 2
    assert "display compose source must not be a symlink" in result.stderr
    summary = json.loads((evidence_root / "two-node-docker-source-trust-display.json").read_text(encoding="utf-8"))
    assert any(item["label"] == "display compose source" and item["is_symlink"] for item in summary["checked_paths"])


def test_source_trust_rejects_symlink_checkout_path_component(tmp_path: Path) -> None:
    real_checkout = _make_checkout(tmp_path / "real-checkout")
    checkout_link = tmp_path / "checkout-link"
    checkout_link.symlink_to(real_checkout, target_is_directory=True)
    evidence_root = real_checkout / "artifacts" / "evidence"

    result = _run_preflight(
        checkout_root=checkout_link,
        evidence_root=evidence_root,
        trust_root=tmp_path,
        trusted_owners=[_current_owner()],
        roles=["compute"],
    )

    assert result.returncode == 2
    assert "trust path component must not be a symlink" in result.stderr
    summary = json.loads((evidence_root / "two-node-docker-source-trust-compute.json").read_text(encoding="utf-8"))
    assert any(item["label"] == "trust path component" and item["is_symlink"] for item in summary["checked_paths"])


def test_docker_readme_requires_source_trust_before_direct_compose_and_absolute_unit_install() -> None:
    readme = (REPO_ROOT / "infra" / "README.two-node-docker.md").read_text(encoding="utf-8")
    bash_blocks = re.findall(r"```bash\n(.*?)\n```", readme, flags=re.DOTALL)
    compose_blocks = [block for block in bash_blocks if re.search(r"(?m)^docker compose --env-file ", block)]

    assert compose_blocks
    for block in compose_blocks:
        first_compose = block.index("docker compose --env-file")
        assert "scripts/validate_two_node_docker_source_trust.py" in block[:first_compose]

    install_blocks = [block for block in bash_blocks if "sudo install -m 0644" in block]
    assert install_blocks
    for block in install_blocks:
        first_install = block.index("sudo install -m 0644")
        assert "scripts/validate_two_node_docker_source_trust.py" in block[:first_install]

    assert 'sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-compute-compose.service"' in readme
    assert 'sudo install -m 0644 "$CHECKOUT_ROOT/infra/systemd/nhms-display-compose.service"' in readme
    assert "sudo install -m 0644 infra/systemd/" not in readme
    assert 'test "$(stat -c \'%a\' infra/env/compute.env)" = "600"' not in readme
    assert 'test "$(stat -c \'%a\' infra/env/display.env)" = "600"' not in readme


def _run_preflight(
    *,
    checkout_root: Path,
    evidence_root: Path,
    trust_root: Path,
    trusted_owners: list[str],
    roles: list[str],
    evidence_run_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--checkout-root",
        str(checkout_root),
        "--evidence-root",
        str(evidence_root),
        "--trust-root",
        str(trust_root),
    ]
    for owner in trusted_owners:
        command.extend(["--trusted-owner", owner])
    for role in roles:
        command.extend(["--role", role])
    if evidence_run_id is not None:
        command.extend(["--evidence-run-id", evidence_run_id])
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _make_checkout(path: Path) -> Path:
    infra = path / "infra"
    env = infra / "env"
    systemd = infra / "systemd"
    env.mkdir(parents=True)
    systemd.mkdir()
    (infra / "compose.compute.yml").write_text("services: {}\n", encoding="utf-8")
    (infra / "compose.display.yml").write_text("services: {}\n", encoding="utf-8")
    (systemd / "nhms-compute-compose.service").write_text("[Service]\n", encoding="utf-8")
    (systemd / "nhms-display-compose.service").write_text("[Service]\n", encoding="utf-8")
    (env / "compute.env").write_text("DATABASE_URL=postgresql://writer:writer-secret@db/nhms\n", encoding="utf-8")
    (env / "display.env").write_text(
        "DATABASE_URL=postgresql://readonly:readonly-secret@db/nhms\n",
        encoding="utf-8",
    )
    for directory in (path, infra, env, systemd):
        directory.chmod(0o755)
    for source in (
        infra / "compose.compute.yml",
        infra / "compose.display.yml",
        systemd / "nhms-compute-compose.service",
        systemd / "nhms-display-compose.service",
    ):
        source.chmod(0o644)
    (env / "compute.env").chmod(0o600)
    (env / "display.env").chmod(0o600)
    return path


def _current_owner() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def _shell_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)
