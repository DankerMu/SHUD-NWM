from __future__ import annotations

import json
from pathlib import Path

from scripts import validate_two_node_docker_runtime as docker_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_static_checker_accepts_safe_compute_and_display_skeletons() -> None:
    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


def test_static_checker_rejects_forbidden_display_env(tmp_path: Path) -> None:
    display_env = tmp_path / "display.example"
    display_env.write_text(
        (REPO_ROOT / "infra/env/display.example").read_text(encoding="utf-8")
        + "\nSLURM_GATEWAY_URL=http://127.0.0.1:8081\n"
        + "SLURM_GATEWAY_BACKEND=slurm\n"
        + "WORKSPACE_ROOT=/scratch/private/workspace\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=display_env,
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_FORBIDDEN_ENV"}


def test_static_checker_rejects_display_hostconfig_and_mount_hazards(tmp_path: Path) -> None:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(
        """
services:
  display-api:
    image: nhms-app:test
    privileged: true
    network_mode: host
    pid: host
    ipc: host
    cap_add:
      - SYS_ADMIN
    read_only: false
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_REQUIRE_SERVICE_ROLE: "true"
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
    volumes:
      - type: bind
        source: /
        target: /host
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
      - type: bind
        source: /etc/slurm
        target: /etc/slurm
        read_only: true
      - type: bind
        source: ${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}
        target: ${NHMS_PUBLISHED_ARTIFACT_ROOT}
        read_only: true
""",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_HOSTCONFIG_PRIVILEGED",
        "DISPLAY_HOSTCONFIG_HOST_NETWORK",
        "DISPLAY_HOSTCONFIG_HOST_PID",
        "DISPLAY_HOSTCONFIG_HOST_IPC",
        "DISPLAY_HOSTCONFIG_CAP_ADD",
        "DISPLAY_ROOT_FILESYSTEM_WRITABLE",
        "DISPLAY_BROAD_HOST_ROOT_BIND",
        "DISPLAY_FORBIDDEN_MOUNT",
    }


def test_static_checker_rejects_short_form_display_mount_hazards(tmp_path: Path) -> None:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(
        """
services:
  display-api:
    image: nhms-app:test
    read_only: true
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_REQUIRE_SERVICE_ROLE: "true"
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
    volumes:
      - "/:/host:ro"
      - "/scratch/private:/private:ro"
      - "/var/run/docker.sock:/var/run/docker.sock"
      - "/etc/slurm:/etc/slurm:ro"
      - "${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}:${NHMS_PUBLISHED_ARTIFACT_ROOT}:rw"
      - "/tmp/not-published:${NHMS_PUBLISHED_ARTIFACT_ROOT}:ro"
      - "${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}:/wrong-published:ro"
""",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_BROAD_HOST_ROOT_BIND",
        "DISPLAY_FORBIDDEN_MOUNT",
        "DISPLAY_PUBLISHED_SOURCE_DRIFT",
        "DISPLAY_PUBLISHED_TARGET_DRIFT",
        "DISPLAY_PUBLISHED_MOUNT_NOT_READONLY",
    }
    broad_paths = {
        finding.details["path"] for finding in result.findings if finding.code == "DISPLAY_BROAD_HOST_ROOT_BIND"
    }
    assert {"/", "/scratch/private"} <= broad_paths
    forbidden_mounts = {
        finding.details["volume"] for finding in result.findings if finding.code == "DISPLAY_FORBIDDEN_MOUNT"
    }
    assert any("/var/run/docker.sock" in volume for volume in forbidden_mounts)
    assert any("/etc/slurm" in volume for volume in forbidden_mounts)
    source_drifts = [
        finding.details["actual"] for finding in result.findings if finding.code == "DISPLAY_PUBLISHED_SOURCE_DRIFT"
    ]
    target_drifts = [
        finding.details["actual"] for finding in result.findings if finding.code == "DISPLAY_PUBLISHED_TARGET_DRIFT"
    ]
    assert "/tmp/not-published" in source_drifts
    assert "/wrong-published" in target_drifts


def test_static_checker_rejects_publish_root_drift_and_legacy_runtime_env(tmp_path: Path) -> None:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(
        """
services:
  display-api:
    image: nhms-app:test
    read_only: true
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_REQUIRE_SERVICE_ROLE: "true"
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
      PUBLISHED_ARTIFACT_ROOT: /legacy/published
    volumes:
      - type: bind
        source: ${NHMS_PUBLISHED_ARTIFACT_ROOT}
        target: /wrong-published
""",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_PUBLISHED_SOURCE_DRIFT",
        "DISPLAY_PUBLISHED_TARGET_DRIFT",
        "DISPLAY_PUBLISHED_MOUNT_NOT_READONLY",
        "LEGACY_PUBLISHED_ARTIFACT_ENV",
    }


def test_static_checker_rejects_dev_compose_as_production_input() -> None:
    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/docker-compose.dev.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert "DEV_COMPOSE_PRODUCTION_MISUSE" in _codes(result)


def test_preflight_records_blocked_when_docker_is_unavailable(tmp_path: Path) -> None:
    evidence_root = tmp_path / "artifacts" / "preflight"

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=tmp_path,
        min_free_bytes=1024,
        command_runner=_docker_unavailable_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "BLOCKED"
    assert result.evidence_path.is_file()
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["tmpdir"]
    assert payload["evidence_root"] == str(evidence_root.resolve())
    assert set(payload["commands"]) >= {
        "docker_version",
        "docker_compose_version",
        "docker_info_docker_root",
        "docker_system_df",
        "df_h",
    }
    assert {blocker["code"] for blocker in payload["blockers"]} >= {"DOCKER_UNAVAILABLE"}


def test_preflight_records_blocked_when_space_is_low(tmp_path: Path) -> None:
    evidence_root = tmp_path / "artifacts" / "preflight"

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_available_runner,
        disk_usage_provider=_low_space,
    )

    assert result.status == "BLOCKED"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["docker_root_dir"] == "/var/lib/docker"
    assert "LOW_DISK_SPACE" in {blocker["code"] for blocker in payload["blockers"]}


def _codes(result: docker_runtime.StaticCheckResult) -> set[str]:
    return {finding.code for finding in result.findings}


def _docker_unavailable_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[0] == "df":
        return docker_runtime.CommandResult(command, 0, "Filesystem Size Used Avail Use% Mounted on\n", "")
    return docker_runtime.CommandResult(command, 127, "", "docker unavailable")


def _docker_available_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "info", "--format"):
        return docker_runtime.CommandResult(command, 0, '"/var/lib/docker"\n', "")
    if command[0] == "df":
        return docker_runtime.CommandResult(command, 0, "Filesystem Size Used Avail Use% Mounted on\n", "")
    return docker_runtime.CommandResult(command, 0, "ok\n", "")


def _high_space(path: Path) -> docker_runtime.DiskSpace:
    return docker_runtime.DiskSpace(total=10_000, used=1_000, free=9_000)


def _low_space(path: Path) -> docker_runtime.DiskSpace:
    return docker_runtime.DiskSpace(total=10_000, used=9_999, free=1)
