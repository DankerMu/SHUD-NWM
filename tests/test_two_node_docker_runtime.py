from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import validate_two_node_docker_runtime as docker_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_audited_compose_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    audited_keys = (
        docker_runtime.COMPUTE_AUDITED_INTERPOLATION_ENV
        | docker_runtime.DISPLAY_AUDITED_INTERPOLATION_ENV
        | set(docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example"))
        | set(docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example"))
    )
    for key in audited_keys:
        monkeypatch.delenv(key, raising=False)


def test_static_checker_accepts_safe_compute_and_display_skeletons() -> None:
    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


def test_docker_compose_examples_render_when_cli_is_available() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available")

    commands = [
        ["docker", "compose", "--env-file", "infra/env/compute.example", "-f", "infra/compose.compute.yml", "config"],
        ["docker", "compose", "--env-file", "infra/env/display.example", "-f", "infra/compose.display.yml", "config"],
        [
            "docker",
            "compose",
            "--env-file",
            "infra/env/compute.example",
            "--profile",
            "manual",
            "-f",
            "infra/compose.compute.yml",
            "config",
        ],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        if completed.returncode != 0 and (
            "not a docker command" in completed.stderr
            or "unknown command" in completed.stderr
            or "unknown shorthand flag" in completed.stderr
        ):
            pytest.skip("docker compose plugin is not available")
        assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("bad_input", ["display_env", "display_compose"])
def test_static_cli_replaces_stale_pass_report_when_setup_fails(tmp_path: Path, bad_input: str) -> None:
    repo_root = tmp_path
    report_path = repo_root / "artifacts" / "stage-change" / "static.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.two_node_docker.static_check.v1",
                "change_id": docker_runtime.CHANGE_ID,
                "checked_at": "2000-01-01T00:00:00Z",
                "status": "PASS",
                "finding_count": 0,
                "findings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bad_display_env = tmp_path / "bad-display.env"
    bad_display_env.write_text("NHMS_SERVICE_ROLE=display_readonly\nnot-a-key-value-line\n", encoding="utf-8")
    bad_display_compose = tmp_path / "bad-compose.yml"
    bad_display_compose.write_text("services:\n  display-api:\n    image: [unterminated\n", encoding="utf-8")

    args = [
        sys.executable,
        str(REPO_ROOT / "scripts/validate_two_node_docker_runtime.py"),
        "--repo-root",
        str(repo_root),
        "static",
        "--compute-compose",
        str(REPO_ROOT / "infra/compose.compute.yml"),
        "--display-compose",
        str(bad_display_compose if bad_input == "display_compose" else REPO_ROOT / "infra/compose.display.yml"),
        "--compute-env",
        str(REPO_ROOT / "infra/env/compute.example"),
        "--display-env",
        str(bad_display_env if bad_input == "display_env" else REPO_ROOT / "infra/env/display.example"),
        "--report",
        str(report_path),
    ]

    completed = subprocess.run(args, cwd=REPO_ROOT, check=False, capture_output=True, text=True)

    assert completed.returncode != 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "nhms.two_node_docker.static_check.v1"
    assert payload["status"] == "FAIL"
    assert payload["checked_at"] != "2000-01-01T00:00:00Z"
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["code"] == "STATIC_VALIDATION_SETUP_FAILED"


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


def test_static_checker_rejects_display_env_file_and_volumes_from(tmp_path: Path) -> None:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(
        """
services:
  display-api:
    image: nhms-app:test
    read_only: true
    env_file:
      - infra/env/compute.example
    volumes_from:
      - compute-api:ro
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_REQUIRE_SERVICE_ROLE: "true"
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
      DATABASE_URL: ${DATABASE_URL:?set readonly display database url}
      NHMS_PUBLISHED_ARTIFACT_ROOT: ${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}
      NHMS_PUBLISHED_ARTIFACT_URI_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_URI_PREFIX:-published://}
      NHMS_PUBLISHED_ARTIFACT_S3_BUCKET: ${NHMS_PUBLISHED_ARTIFACT_S3_BUCKET:-}
      NHMS_PUBLISHED_ARTIFACT_S3_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_S3_PREFIX:-}
      NHMS_LOG_TAIL_MAX_BYTES: ${NHMS_LOG_TAIL_MAX_BYTES:-1048576}
      NHMS_ARTIFACT_BACKEND: ${NHMS_ARTIFACT_BACKEND:-local}
    volumes:
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
    assert _codes(result) >= {"DISPLAY_ENV_FILE_UNSUPPORTED", "DISPLAY_VOLUMES_FROM_UNSUPPORTED"}


@pytest.mark.parametrize("surface", ["configs", "secrets"])
@pytest.mark.parametrize(
    "target",
    [
        "/etc/slurm/slurm.conf",
        "/etc/munge/munge.key",
        "/run/munge/munge.socket",
        "/extra/config",
    ],
)
def test_static_checker_rejects_display_configs_and_secrets(
    tmp_path: Path,
    surface: str,
    target: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    source_name = f"{surface[:-1]}_fixture"
    service[surface] = [{"source": source_name, "target": target}]
    compose[surface] = {source_name: {"file": f"/host{target}"}}
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    expected_code = "DISPLAY_CONFIG_UNSUPPORTED" if surface == "configs" else "DISPLAY_SECRET_UNSUPPORTED"
    assert result.status == "FAIL"
    assert expected_code in _codes(result)
    findings = [finding for finding in result.findings if finding.code == expected_code]
    assert findings[0].details["target"] == target
    assert findings[0].details["top_level"]["file"] == f"/host{target}"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("devices", ["/dev/sda:/dev/sda:r"], "DISPLAY_HOST_DEVICE_UNSUPPORTED"),
        ("devices", ["${DISPLAY_DEVICE:-/dev/kmsg:/dev/kmsg:r}"], "DISPLAY_HOST_DEVICE_UNSUPPORTED"),
        ("device_cgroup_rules", ["c 1:3 rmw"], "DISPLAY_DEVICE_CGROUP_RULE_UNSUPPORTED"),
        (
            "device_cgroup_rules",
            ["${DISPLAY_DEVICE_RULE:-c 1:3 rmw}"],
            "DISPLAY_DEVICE_CGROUP_RULE_UNSUPPORTED",
        ),
        (
            "device_requests",
            [{"driver": "nvidia", "count": 1, "capabilities": [["gpu"]]}],
            "DISPLAY_DEVICE_REQUEST_UNSUPPORTED",
        ),
        (
            "device_requests",
            [{"driver": "${DISPLAY_DEVICE_DRIVER:-nvidia}", "count": 1, "capabilities": [["gpu"]]}],
            "DISPLAY_DEVICE_REQUEST_UNSUPPORTED",
        ),
    ],
)
def test_static_checker_rejects_display_device_ingress_surfaces(
    tmp_path: Path,
    field: str,
    value: Any,
    expected_code: str,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"][field] = value
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)
    if "${" in str(value):
        assert "DISPLAY_HOSTCONFIG_DYNAMIC_INTERPOLATION" in _codes(result)


@pytest.mark.parametrize(
    "deploy",
    [
        {"resources": {"reservations": {"devices": [{"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}]}}},
        {"replicas": 1, "restart_policy": {"condition": "on-failure"}},
    ],
)
def test_static_checker_rejects_display_deploy_subtree(tmp_path: Path, deploy: dict[str, Any]) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["deploy"] = deploy
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_DEPLOY_UNSUPPORTED" in _codes(result)


@pytest.mark.parametrize(
    ("surface", "field", "value", "expected_code"),
    [
        ("include", None, ["./compose.extra.yml"], "DISPLAY_INCLUDE_UNSUPPORTED"),
        (
            "extends",
            "extends",
            {"file": "compose.compute.yml", "service": "compute-api"},
            "DISPLAY_EXTENDS_UNSUPPORTED",
        ),
        ("network_service", "network_mode", "service:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
        ("network_container", "network_mode", "container:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
        ("pid_service", "pid", "service:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
        ("pid_container", "pid", "container:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
        ("ipc_service", "ipc", "service:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
        ("ipc_container", "ipc", "container:compute-api", "DISPLAY_NAMESPACE_SHARING_UNSUPPORTED"),
    ],
)
def test_static_checker_rejects_display_inheritance_and_namespace_surfaces(
    tmp_path: Path,
    surface: str,
    field: str | None,
    value: Any,
    expected_code: str,
) -> None:
    compose = _safe_display_compose()
    if surface == "include":
        compose["include"] = value
    elif field is not None:
        compose["services"]["display-api"][field] = value
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)


def test_static_checker_rejects_display_named_volume_forbidden_sources(tmp_path: Path) -> None:
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
      DATABASE_URL: ${DATABASE_URL:?set readonly display database url}
      NHMS_PUBLISHED_ARTIFACT_ROOT: ${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}
      NHMS_PUBLISHED_ARTIFACT_URI_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_URI_PREFIX:-published://}
      NHMS_PUBLISHED_ARTIFACT_S3_BUCKET: ${NHMS_PUBLISHED_ARTIFACT_S3_BUCKET:-}
      NHMS_PUBLISHED_ARTIFACT_S3_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_S3_PREFIX:-}
      NHMS_LOG_TAIL_MAX_BYTES: ${NHMS_LOG_TAIL_MAX_BYTES:-1048576}
      NHMS_ARTIFACT_BACKEND: ${NHMS_ARTIFACT_BACKEND:-local}
    volumes:
      - type: bind
        source: ${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}
        target: ${NHMS_PUBLISHED_ARTIFACT_ROOT}
        read_only: true
      - docker-sock:/socket:ro
      - host-root:/host:ro
volumes:
  docker-sock:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /run/docker.sock
  host-root:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /
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
    assert "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE" in _codes(result)
    devices = {
        finding.details["device"]
        for finding in result.findings
        if finding.code == "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE"
    }
    assert {"/run/docker.sock", "/"} <= devices


def test_static_checker_rejects_display_docker_socket_paths_long_and_short(tmp_path: Path) -> None:
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
      DATABASE_URL: ${DATABASE_URL:?set readonly display database url}
      NHMS_PUBLISHED_ARTIFACT_ROOT: ${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}
      NHMS_PUBLISHED_ARTIFACT_URI_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_URI_PREFIX:-published://}
      NHMS_PUBLISHED_ARTIFACT_S3_BUCKET: ${NHMS_PUBLISHED_ARTIFACT_S3_BUCKET:-}
      NHMS_PUBLISHED_ARTIFACT_S3_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_S3_PREFIX:-}
      NHMS_LOG_TAIL_MAX_BYTES: ${NHMS_LOG_TAIL_MAX_BYTES:-1048576}
      NHMS_ARTIFACT_BACKEND: ${NHMS_ARTIFACT_BACKEND:-local}
    volumes:
      - "${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}:${NHMS_PUBLISHED_ARTIFACT_ROOT}:ro"
      - "/run/docker.sock:/run/docker.sock:ro"
      - type: bind
        source: /tmp/runtime/docker.sock
        target: /socket
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
    forbidden_mounts = {
        finding.details["volume"] for finding in result.findings if finding.code == "DISPLAY_FORBIDDEN_MOUNT"
    }
    assert any("/run/docker.sock" in volume for volume in forbidden_mounts)
    assert any("/tmp/runtime/docker.sock" in volume for volume in forbidden_mounts)


def test_static_checker_rejects_extra_display_literal_bind(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append(
        {"type": "bind", "source": "/mnt/public", "target": "/extra", "read_only": True}
    )
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_UNAPPROVED_MOUNT" in _codes(result)


def test_static_checker_rejects_extra_display_named_bind(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append("public-data:/extra:ro")
    compose["volumes"] = {
        "public-data": {
            "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "/mnt/public"},
        }
    }
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_UNAPPROVED_MOUNT", "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE"}


@pytest.mark.parametrize(
    "volume",
    [
        "../../nhms-production/workspace:/workspace:ro",
        {"type": "bind", "source": "../../../../var/run/docker.sock", "target": "/socket", "read_only": True},
    ],
)
def test_static_checker_rejects_display_relative_bind_sources(tmp_path: Path, volume: Any) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append(volume)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_RELATIVE_MOUNT_SOURCE", "DISPLAY_UNAPPROVED_MOUNT"}


def test_static_checker_rejects_display_named_volume_relative_device(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append("relative-workspace:/workspace:ro")
    compose["volumes"] = {
        "relative-workspace": {
            "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "../../nhms-production/workspace"},
        }
    }
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_UNAPPROVED_MOUNT",
        "DISPLAY_NAMED_VOLUME_RELATIVE_DEVICE",
        "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE",
    }


@pytest.mark.parametrize(
    "volume",
    [
        "/etc/munge:/etc/munge:ro",
        {"type": "bind", "source": "/tmp/munge.key", "target": "/run/secrets/munge.key", "read_only": True},
    ],
)
def test_static_checker_rejects_display_munge_mounts(tmp_path: Path, volume: Any) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append(volume)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_FORBIDDEN_MOUNT", "DISPLAY_UNAPPROVED_MOUNT"}


def test_static_checker_rejects_display_named_volume_munge_key(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append("munge-key:/run/secrets/munge.key:ro")
    compose["volumes"] = {
        "munge-key": {
            "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "/etc/munge/munge.key"},
        }
    }
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_UNAPPROVED_MOUNT", "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE"}


def test_static_checker_rejects_writable_display_artifact_submount(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append(
        {
            "type": "bind",
            "source": "/mnt/display-cache",
            "target": "/var/lib/nhms/published/logs",
            "read_only": False,
        }
    )
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_UNAPPROVED_MOUNT",
        "DISPLAY_ARTIFACT_OVERLAY_MOUNT",
        "DISPLAY_UNAPPROVED_WRITABLE_MOUNT",
    }


def test_static_checker_rejects_display_tmpfs_under_artifact_root(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["tmpfs"].append("/var/lib/nhms/published/cache:size=16m")
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_TMPFS_ARTIFACT_OVERLAY" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("privileged", "${DISPLAY_PRIVILEGED:-false}"),
        ("privileged", "${DISPLAY_PRIVILEGED:+true}"),
        ("network_mode", "$DISPLAY_NETWORK_MODE"),
        ("network_mode", "${DISPLAY_NET+host}"),
        ("network_mode", "${DISPLAY_NETWORK_MODE:?set display network mode}"),
        ("pid", "${DISPLAY_PID_MODE:-}"),
        ("ipc", "${DISPLAY_IPC_MODE:-shareable}"),
        ("cap_add", ["${DISPLAY_CAP_ADD:-}"]),
        ("read_only", "${DISPLAY_READ_ONLY:-true}"),
    ],
)
def test_static_checker_rejects_display_hostconfig_dynamic_interpolation(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"][field] = value
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_HOSTCONFIG_DYNAMIC_INTERPOLATION" in _codes(result)


def test_static_checker_rejects_display_hostconfig_interpolation_defaults(tmp_path: Path) -> None:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(
        """
services:
  display-api:
    image: nhms-app:test
    privileged: ${DISPLAY_PRIVILEGED:-true}
    network_mode: ${DISPLAY_NETWORK_MODE:-host}
    pid: ${DISPLAY_PID_MODE:-host}
    ipc: ${DISPLAY_IPC_MODE:-host}
    cap_add:
      - ${DISPLAY_CAP:-SYS_ADMIN}
    read_only: ${DISPLAY_READ_ONLY:-false}
    environment:
      NHMS_SERVICE_ROLE: display_readonly
      NHMS_REQUIRE_SERVICE_ROLE: "true"
      NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS: "true"
      NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS: "false"
      DATABASE_URL: ${DATABASE_URL:?set readonly display database url}
      NHMS_PUBLISHED_ARTIFACT_ROOT: ${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}
      NHMS_PUBLISHED_ARTIFACT_URI_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_URI_PREFIX:-published://}
      NHMS_PUBLISHED_ARTIFACT_S3_BUCKET: ${NHMS_PUBLISHED_ARTIFACT_S3_BUCKET:-}
      NHMS_PUBLISHED_ARTIFACT_S3_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_S3_PREFIX:-}
      NHMS_LOG_TAIL_MAX_BYTES: ${NHMS_LOG_TAIL_MAX_BYTES:-1048576}
      NHMS_ARTIFACT_BACKEND: ${NHMS_ARTIFACT_BACKEND:-local}
    volumes:
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
    }


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("cap_drop", None, "DISPLAY_HOSTCONFIG_CAP_DROP_INVALID"),
        ("cap_drop", ["NET_RAW"], "DISPLAY_HOSTCONFIG_CAP_DROP_INVALID"),
        ("cap_drop", ["${DISPLAY_CAP_DROP:-ALL}"], "DISPLAY_HOSTCONFIG_DYNAMIC_INTERPOLATION"),
        ("security_opt", None, "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID"),
        ("security_opt", ["no-new-privileges:false"], "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID"),
        (
            "security_opt",
            ["no-new-privileges:true", "seccomp=unconfined"],
            "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
        ),
        (
            "security_opt",
            ["no-new-privileges:true", "apparmor=unconfined"],
            "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
        ),
        (
            "security_opt",
            ["no-new-privileges:true", "label:disable"],
            "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
        ),
        (
            "security_opt",
            ["no-new-privileges:true", "no-new-privileges:false"],
            "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID",
        ),
        ("security_opt", ["NO-NEW-PRIVILEGES:TRUE"], "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID"),
        ("security_opt", ["No-New-Privileges:True"], "DISPLAY_HOSTCONFIG_NO_NEW_PRIVILEGES_INVALID"),
        (
            "security_opt",
            ["${DISPLAY_SECURITY_OPT:-no-new-privileges:true}"],
            "DISPLAY_HOSTCONFIG_DYNAMIC_INTERPOLATION",
        ),
    ],
)
def test_static_checker_rejects_display_capability_hardening_drift(
    tmp_path: Path,
    field: str,
    value: Any,
    expected_code: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    if value is None:
        service.pop(field)
    else:
        service[field] = value
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)


@pytest.mark.parametrize(
    "extra_volume",
    [
        "${EXTRA_DISPLAY_BIND:-/mnt/public}:/extra:ro",
        "/mnt/public:${EXTRA_TARGET:-/safe}:ro",
        "/mnt/public:/extra:${EXTRA_MODE:-ro}",
        {
            "type": "bind",
            "source": "${EXTRA_DISPLAY_BIND:-/mnt/public}",
            "target": "/extra",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "${EXTRA_DISPLAY_BIND:+/scratch/private}",
            "target": "/extra",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "${EXTRA_DISPLAY_BIND+/scratch/private}",
            "target": "/extra",
            "read_only": True,
        },
        {
            "type": "${DISPLAY_MOUNT_TYPE:+bind}",
            "source": "/mnt/public",
            "target": "/extra",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/mnt/public",
            "target": "${EXTRA_TARGET:-/safe}",
            "read_only": True,
        },
        {
            "type": "bind",
            "source": "/mnt/public",
            "target": "/extra",
            "mode": "${EXTRA_MODE:-ro}",
        },
    ],
)
def test_static_checker_rejects_display_mount_dynamic_interpolation(
    tmp_path: Path,
    extra_volume: Any,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append(extra_volume)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_MOUNT_DYNAMIC_INTERPOLATION" in _codes(result)


@pytest.mark.parametrize(
    "device",
    [
        "${DISPLAY_BIND:-/mnt/public}",
        "${DISPLAY_BIND:+/scratch/private}",
        "${DISPLAY_BIND+/scratch/private}",
    ],
)
def test_static_checker_rejects_display_named_bind_dynamic_device(tmp_path: Path, device: str) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"].append("dynamic-public:/extra:ro")
    compose["volumes"] = {
        "dynamic-public": {
            "driver": "local",
            "driver_opts": {
                "type": "none",
                "o": "bind",
                "device": device,
            },
        },
    }
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_NAMED_VOLUME_DYNAMIC_INTERPOLATION" in _codes(result)


@pytest.mark.parametrize(
    ("entry", "expected_codes"),
    [
        (
            "${EXTRA_ENV:-WORKSPACE_ROOT=/scratch/private}",
            {"DISPLAY_ENV_DYNAMIC_KEY", "DISPLAY_FORBIDDEN_ENV"},
        ),
        (
            "${EXTRA_ENV:+SLURM_GATEWAY_URL=http://127.0.0.1:8081}",
            {"DISPLAY_ENV_DYNAMIC_KEY", "DISPLAY_FORBIDDEN_ENV"},
        ),
        (
            "${EXTRA_ENV+SLURM_GATEWAY_URL=http://127.0.0.1:8081}",
            {"DISPLAY_ENV_DYNAMIC_KEY", "DISPLAY_FORBIDDEN_ENV"},
        ),
        (
            "${DISPLAY_ENV_KEY}=literal",
            {"DISPLAY_ENV_DYNAMIC_KEY"},
        ),
    ],
)
def test_static_checker_rejects_display_environment_list_dynamic_keys_and_forbidden_rendering(
    tmp_path: Path,
    entry: str,
    expected_codes: set[str],
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    service["environment"] = _environment_dict_to_list(service["environment"])
    service["environment"].append(entry)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= expected_codes


def test_static_checker_accepts_display_environment_list_literal_entries(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    service["environment"] = _environment_dict_to_list(service["environment"])
    service["environment"].append("DISPLAY_LITERAL=ok")
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


def test_static_checker_rejects_display_env_file_object_store_root(tmp_path: Path) -> None:
    display_env = tmp_path / "display.example"
    display_env.write_text(
        (REPO_ROOT / "infra/env/display.example").read_text(encoding="utf-8")
        + "\nOBJECT_STORE_ROOT=/scratch/private/object-store\n",
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
    assert "DISPLAY_FORBIDDEN_ENV" in _codes(result)


@pytest.mark.parametrize(
    "environment_entry",
    [
        {"OBJECT_STORE_ROOT": "/scratch/private/object-store"},
        ["OBJECT_STORE_ROOT=/scratch/private/object-store"],
        ["${EXTRA_ENV:-OBJECT_STORE_ROOT=/scratch/private/object-store}"],
    ],
)
def test_static_checker_rejects_display_service_object_store_root(
    tmp_path: Path,
    environment_entry: dict[str, str] | list[str],
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    if isinstance(environment_entry, dict):
        service["environment"]["OBJECT_STORE_ROOT"] = environment_entry["OBJECT_STORE_ROOT"]
    else:
        service["environment"] = _environment_dict_to_list(service["environment"])
        service["environment"].extend(environment_entry)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_FORBIDDEN_ENV" in _codes(result)


def test_static_checker_rejects_display_hard_coded_compute_roots(tmp_path: Path) -> None:
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
      DATABASE_URL: ${DATABASE_URL:?set readonly display database url}
      NHMS_PUBLISHED_ARTIFACT_ROOT: ${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}
      NHMS_PUBLISHED_ARTIFACT_URI_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_URI_PREFIX:-published://}
      NHMS_PUBLISHED_ARTIFACT_S3_BUCKET: ${NHMS_PUBLISHED_ARTIFACT_S3_BUCKET:-}
      NHMS_PUBLISHED_ARTIFACT_S3_PREFIX: ${NHMS_PUBLISHED_ARTIFACT_S3_PREFIX:-}
      NHMS_LOG_TAIL_MAX_BYTES: ${NHMS_LOG_TAIL_MAX_BYTES:-1048576}
      NHMS_ARTIFACT_BACKEND: ${NHMS_ARTIFACT_BACKEND:-local}
    volumes:
      - type: bind
        source: ${NHMS_PUBLISHED_ARTIFACT_HOST_ROOT}
        target: ${NHMS_PUBLISHED_ARTIFACT_ROOT}
        read_only: true
      - /scratch/frd_muziyao/nhms-production/workspace/run-a:/workspace:ro
      - /volume/data/nwm/Basins:/basins:ro
      - /volume/data/nwm/model-assets/forcing:/model-assets:ro
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
    assert "DISPLAY_FORBIDDEN_MOUNT" in _codes(result)
    matched_roots = {
        finding.details.get("root_key")
        for finding in result.findings
        if finding.code == "DISPLAY_FORBIDDEN_MOUNT"
    }
    assert {"WORKSPACE_ROOT", "NHMS_BASINS_ROOT", "NHMS_MODEL_ASSET_ROOT"} <= matched_roots


def test_static_checker_rejects_ambient_display_published_host_root_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "/scratch/frd_muziyao/private-leak")

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert "DISPLAY_AMBIENT_ENV_OVERRIDE" in _codes(result)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("WORKSPACE_ROOT", "/scratch/frd_muziyao/ambient-workspace"),
        ("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "/scratch/frd_muziyao/ambient-published"),
        ("NHMS_BASINS_ROOT", "/volume/data/nwm/ambient-basins"),
        ("NHMS_MODEL_ASSET_ROOT", "/volume/data/nwm/ambient-model-assets"),
        ("DATABASE_URL", "postgresql://ambient-writer:change-me@db.internal.example:5432/nhms"),
        ("NHMS_AUTH_MODE", "dev"),
        ("NHMS_APP_IMAGE", "ambient-app"),
        ("NHMS_IMAGE_TAG", "ambient-tag"),
        ("NHMS_CONTAINER_UID", "4242"),
        ("NHMS_CONTAINER_GID", "4242"),
        ("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "other://"),
        ("NHMS_PUBLISHED_ARTIFACT_S3_BUCKET", "ambient-bucket"),
        ("NHMS_PUBLISHED_ARTIFACT_S3_PREFIX", "ambient-prefix"),
    ],
)
def test_static_checker_rejects_compute_ambient_overrides(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(key, value)

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_AMBIENT_ENV_OVERRIDE" and finding.details["key"] == key
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NHMS_APP_IMAGE", "ambient-display-app"),
        ("NHMS_IMAGE_TAG", "ambient-display-tag"),
        ("NHMS_CONTAINER_UID", "5252"),
        ("NHMS_CONTAINER_GID", "5252"),
        ("NHMS_DISPLAY_API_PORT", "18000"),
        ("NHMS_PUBLISHED_ARTIFACT_HOST_ROOT", "/scratch/frd_muziyao/display-private-leak"),
        ("NHMS_PUBLISHED_ARTIFACT_ROOT", "/wrong/container/published"),
        ("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "other://"),
        ("NHMS_PUBLISHED_ARTIFACT_S3_BUCKET", "other-bucket"),
        ("NHMS_PUBLISHED_ARTIFACT_S3_PREFIX", "other-prefix"),
        ("NHMS_AUTH_MODE", "dev"),
        ("DATABASE_URL", "postgresql://nhms_control_rw:change-me@db.internal.example:5432/nhms"),
    ],
)
def test_static_checker_rejects_display_ambient_overrides(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str,
) -> None:
    monkeypatch.setenv(key, value)

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_AMBIENT_ENV_OVERRIDE" and finding.details["key"] == key
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("mutation", "alias_key", "process_value"),
    [
        ("image", "DISPLAY_APP_IMAGE", "ambient-display-app"),
        ("port", "DISPLAY_PORT", "18000"),
        ("aws_secret", "DISPLAY_AWS_SECRET_ACCESS_KEY", "ambient-secret"),
    ],
)
def test_static_checker_rejects_display_compose_alias_interpolation_keys_absent_from_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    alias_key: str,
    process_value: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    if mutation == "image":
        service["image"] = "${DISPLAY_APP_IMAGE:-nhms-app}:${NHMS_IMAGE_TAG:-m22-placeholder}"
    elif mutation == "port":
        service["ports"] = ["127.0.0.1:${DISPLAY_PORT:-8000}:8000"]
    elif mutation == "aws_secret":
        service["environment"]["AWS_SECRET_ACCESS_KEY"] = (
            "${DISPLAY_AWS_SECRET_ACCESS_KEY:-readonly-secret-placeholder}"
        )
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    display_compose = _write_display_compose(tmp_path, compose)
    monkeypatch.setenv(alias_key, process_value)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_INTERPOLATION_ENV_MISSING", "DISPLAY_INTERPOLATION_ENV_UNAPPROVED"}


@pytest.mark.parametrize(
    ("mutation", "alias_key", "process_value"),
    [
        ("image", "COMPUTE_APP_IMAGE", "ambient-compute-app"),
        ("workspace", "COMPUTE_WORKSPACE_ROOT", "/scratch/frd_muziyao/ambient-workspace"),
        ("published", "COMPUTE_PUBLISHED_ARTIFACT_HOST_ROOT", "/scratch/frd_muziyao/ambient-published"),
    ],
)
def test_static_checker_rejects_compute_compose_alias_interpolation_keys_absent_from_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    alias_key: str,
    process_value: str,
) -> None:
    compose = _safe_compute_compose()
    if mutation == "image":
        for service in compose["services"].values():
            service["image"] = "${COMPUTE_APP_IMAGE:-nhms-app}:${NHMS_IMAGE_TAG:-m22-placeholder}"
    elif mutation == "workspace":
        _mutate_compute_required_mount(
            compose,
            target_key="WORKSPACE_ROOT",
            values={"source": "${COMPUTE_WORKSPACE_ROOT:-/scratch/frd_muziyao/ambient-workspace}"},
        )
    elif mutation == "published":
        _mutate_compute_required_mount(
            compose,
            target_key="NHMS_PUBLISHED_ARTIFACT_ROOT",
            values={"source": "${COMPUTE_PUBLISHED_ARTIFACT_HOST_ROOT:-/scratch/frd_muziyao/ambient-published}"},
        )
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    compute_compose = _write_compute_compose(tmp_path, compose)
    monkeypatch.setenv(alias_key, process_value)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"COMPUTE_INTERPOLATION_ENV_MISSING", "COMPUTE_INTERPOLATION_ENV_UNAPPROVED"}


@pytest.mark.parametrize(
    ("role", "omitted_key", "process_value"),
    [
        ("compute", "OBJECT_STORE_ROOT", "/scratch/frd_muziyao/ambient-object-store"),
        ("display", "S3_ENDPOINT_URL", "https://ambient-object-store.internal.example"),
    ],
)
def test_static_checker_rejects_custom_env_omitting_optional_compose_used_keys_with_ambient_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    omitted_key: str,
    process_value: str,
) -> None:
    source = REPO_ROOT / f"infra/env/{role}.example"
    custom_env = tmp_path / f"{role}.example"
    custom_env.write_text(
        "\n".join(
            line
            for line in source.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{omitted_key}=")
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(omitted_key, process_value)

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=custom_env if role == "compute" else Path("infra/env/compute.example"),
        display_env=custom_env if role == "display" else Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    expected_code = f"{role.upper()}_INTERPOLATION_ENV_MISSING"
    assert expected_code in _codes(result)
    assert any(
        finding.code == expected_code and omitted_key in finding.details["missing_keys"]
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "value", "alias_key", "process_value"),
    [
        (
            "DATABASE_URL",
            "${DISPLAY_DATABASE_URL:-postgresql://nhms_display_ro:change-me@db.internal.example:5432/nhms}",
            "DISPLAY_DATABASE_URL",
            "postgresql://nhms_control_rw:change-me@db.internal.example:5432/nhms",
        ),
        (
            "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
            "${DISPLAY_MUTATIONS_DISABLED:-true}",
            "DISPLAY_MUTATIONS_DISABLED",
            "false",
        ),
    ],
)
def test_static_checker_rejects_display_critical_env_alias_interpolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    value: str,
    alias_key: str,
    process_value: str,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["environment"][env_key] = value
    display_compose = _write_display_compose(tmp_path, compose)
    monkeypatch.setenv(alias_key, process_value)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_RUNTIME_ENV_ALIAS_INTERPOLATION" and finding.details["key"] == env_key
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "value", "alias_key", "process_value"),
    [
        (
            "AWS_SECRET_ACCESS_KEY",
            "${DISPLAY_AWS_SECRET_ACCESS_KEY:-readonly-secret-placeholder}",
            "DISPLAY_AWS_SECRET_ACCESS_KEY",
            "ambient-secret",
        ),
        (
            "S3_ENDPOINT_URL",
            "${DISPLAY_S3_ENDPOINT_URL:-https://object-store.internal.example}",
            "DISPLAY_S3_ENDPOINT_URL",
            "https://ambient-object-store.internal.example",
        ),
    ],
)
def test_static_checker_rejects_display_optional_env_alias_interpolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    value: str,
    alias_key: str,
    process_value: str,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["environment"][env_key] = value
    display_compose = _write_display_compose(tmp_path, compose)
    monkeypatch.setenv(alias_key, process_value)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_RUNTIME_ENV_ALIAS_INTERPOLATION" and finding.details["key"] == env_key
        for finding in result.findings
    )


def test_static_checker_rejects_display_critical_env_literal_drift(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    service_env = compose["services"]["display-api"]["environment"]
    service_env["DATABASE_URL"] = "postgresql://nhms_control_rw:change-me@db.internal.example:5432/nhms"
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT" and finding.details["key"] == "DATABASE_URL"
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "alternate_value", "operator"),
    [
        (
            "DATABASE_URL",
            "postgresql://nhms_control_rw:change-me@db.internal.example:5432/nhms",
            ":+",
        ),
        (
            "DATABASE_URL",
            "postgresql://nhms_control_rw:change-me@db.internal.example:5432/nhms",
            "+",
        ),
        ("AWS_SECRET_ACCESS_KEY", "not-env-file-secret", ":+"),
        ("AWS_SECRET_ACCESS_KEY", "not-env-file-secret", "+"),
    ],
)
def test_static_checker_rejects_display_same_key_alternate_env_drift(
    tmp_path: Path,
    env_key: str,
    alternate_value: str,
    operator: str,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["environment"][env_key] = f"${{{env_key}{operator}{alternate_value}}}"
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT" and finding.details["key"] == env_key
        for finding in result.findings
    )


@pytest.mark.parametrize("operator", [":+", "+"])
def test_static_checker_accepts_same_key_alternate_when_it_matches_env_file(
    tmp_path: Path,
    operator: str,
) -> None:
    display_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    compute_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example")
    display_compose = _safe_display_compose()
    display_compose["services"]["display-api"]["environment"]["AWS_SECRET_ACCESS_KEY"] = (
        f"${{AWS_SECRET_ACCESS_KEY{operator}{display_env['AWS_SECRET_ACCESS_KEY']}}}"
    )
    compute_compose = _safe_compute_compose()
    for service in compute_compose["services"].values():
        service["environment"]["WORKSPACE_ROOT"] = f"${{WORKSPACE_ROOT{operator}{compute_env['WORKSPACE_ROOT']}}}"

    result = docker_runtime.run_static_check(
        compute_compose=_write_compute_compose(tmp_path, compute_compose),
        display_compose=_write_display_compose(tmp_path, display_compose),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


@pytest.mark.parametrize(
    ("field", "operator", "key", "expected_rendered"),
    [
        ("image", ":+", "NHMS_APP_IMAGE", "unexpected-display-app"),
        ("image", "+", "NHMS_APP_IMAGE", "unexpected-display-app"),
        ("user", ":+", "NHMS_CONTAINER_UID", "0"),
        ("user", "+", "NHMS_CONTAINER_UID", "0"),
        ("ports", ":+", "NHMS_DISPLAY_API_PORT", "18000"),
        ("ports", "+", "NHMS_DISPLAY_API_PORT", "18000"),
    ],
)
def test_static_checker_rejects_display_full_tree_same_key_interpolation_drift(
    tmp_path: Path,
    field: str,
    operator: str,
    key: str,
    expected_rendered: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    display_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    if field == "image":
        service["image"] = f"${{{key}{operator}{expected_rendered}}}:${{NHMS_IMAGE_TAG}}"
        assert docker_runtime._resolve_compose_value(service["image"], display_env).startswith(expected_rendered)
    elif field == "user":
        service["user"] = f"${{{key}{operator}{expected_rendered}}}:${{NHMS_CONTAINER_GID}}"
        assert docker_runtime._resolve_compose_value(service["user"], display_env).startswith(f"{expected_rendered}:")
    elif field == "ports":
        service["ports"] = [f"127.0.0.1:${{{key}{operator}{expected_rendered}}}:8000"]
        assert expected_rendered in docker_runtime._resolve_compose_value(service["ports"][0], display_env)
    else:
        raise AssertionError(f"unhandled field: {field}")
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_INTERPOLATION_VALUE_DRIFT"
        and finding.details["key"] == key
        and finding.details["rendered_value"] == expected_rendered
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("field", "operator", "key", "expected_rendered"),
    [
        ("image", ":+", "NHMS_APP_IMAGE", "unexpected-compute-app"),
        ("image", "+", "NHMS_APP_IMAGE", "unexpected-compute-app"),
        ("user", ":+", "NHMS_CONTAINER_UID", "0"),
        ("user", "+", "NHMS_CONTAINER_UID", "0"),
    ],
)
def test_static_checker_rejects_compute_full_tree_same_key_interpolation_drift(
    tmp_path: Path,
    field: str,
    operator: str,
    key: str,
    expected_rendered: str,
) -> None:
    compose = _safe_compute_compose()
    compute_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example")
    for service in compose["services"].values():
        if field == "image":
            service["image"] = f"${{{key}{operator}{expected_rendered}}}:${{NHMS_IMAGE_TAG}}"
            assert docker_runtime._resolve_compose_value(service["image"], compute_env).startswith(expected_rendered)
        elif field == "user":
            service["user"] = f"${{{key}{operator}{expected_rendered}}}:${{NHMS_CONTAINER_GID}}"
            resolved_user = docker_runtime._resolve_compose_value(service["user"], compute_env)
            assert resolved_user.startswith(f"{expected_rendered}:")
        else:
            raise AssertionError(f"unhandled field: {field}")
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_VALUE_DRIFT"
        and finding.details["key"] == key
        and finding.details["rendered_value"] == expected_rendered
        for finding in result.findings
    )


@pytest.mark.parametrize("operator", [":+", "+"])
def test_static_checker_accepts_full_tree_same_key_alternate_when_it_matches_env_file(
    tmp_path: Path,
    operator: str,
) -> None:
    display_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    compute_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example")
    display_compose = _safe_display_compose()
    display_service = display_compose["services"]["display-api"]
    display_service["image"] = (
        f"${{NHMS_APP_IMAGE{operator}{display_env['NHMS_APP_IMAGE']}}}:"
        f"${{NHMS_IMAGE_TAG{operator}{display_env['NHMS_IMAGE_TAG']}}}"
    )
    display_service["ports"] = [
        f"127.0.0.1:${{NHMS_DISPLAY_API_PORT{operator}{display_env['NHMS_DISPLAY_API_PORT']}}}:8000"
    ]
    compute_compose = _safe_compute_compose()
    for service in compute_compose["services"].values():
        service["user"] = (
            f"${{NHMS_CONTAINER_UID{operator}{compute_env['NHMS_CONTAINER_UID']}}}:"
            f"${{NHMS_CONTAINER_GID{operator}{compute_env['NHMS_CONTAINER_GID']}}}"
        )

    result = docker_runtime.run_static_check(
        compute_compose=_write_compute_compose(tmp_path, compute_compose),
        display_compose=_write_display_compose(tmp_path, display_compose),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


@pytest.mark.parametrize(
    "env_key",
    sorted(docker_runtime.DISPLAY_AUDITED_RUNTIME_ENV),
)
def test_static_checker_rejects_display_required_env_null_imports(
    tmp_path: Path,
    env_key: str,
) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["environment"][env_key] = None
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "DISPLAY_RUNTIME_ENV_NULL_IMPORT" and finding.details["key"] == env_key
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "process_value"),
    [
        ("DATABASE_URL", "postgresql://ambient-writer:change-me@db.internal.example:5432/nhms"),
        ("NHMS_SERVICE_ROLE", "dev_monolith"),
        ("NHMS_REQUIRE_SERVICE_ROLE", "false"),
        ("NHMS_AUTH_MODE", "dev"),
        ("WORKSPACE_ROOT", "/scratch/frd_muziyao/ambient-workspace"),
        ("OBJECT_STORE_ROOT", "/scratch/frd_muziyao/ambient-object-store"),
        ("SLURM_GATEWAY_URL", "http://ambient-slurm-gateway.internal.example:8081"),
        ("SHUD_EXECUTABLE", "/ambient/bin/shud"),
        ("SLURM_GATEWAY_TEMPLATE_DIR", "/ambient/slurm/templates"),
        ("SLURM_GATEWAY_WORKSPACE_DIR", "/ambient/slurm/workspace"),
        ("NHMS_PUBLISHED_ARTIFACT_ROOT", "/ambient/published"),
    ],
)
def test_static_checker_rejects_compute_audited_env_null_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    process_value: str,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"][env_key] = None
    compute_compose = _write_compute_compose(tmp_path, compose)
    monkeypatch.setenv(env_key, process_value)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_RUNTIME_ENV_NULL_IMPORT" and finding.details["key"] == env_key
        for finding in result.findings
    )


def test_static_checker_rejects_compute_audited_env_alias_fallback_when_env_file_value_empty(tmp_path: Path) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"]["SLURM_GATEWAY_URL"] = "${SLURM_GATEWAY_URL:-${WORKSPACE_ROOT}}"
    compute_compose = _write_compute_compose(tmp_path, compose)
    compute_env = tmp_path / "compute.example"
    compute_env.write_text(
        "\n".join(
            line
            for line in (REPO_ROOT / "infra/env/compute.example").read_text(encoding="utf-8").splitlines()
            if not line.startswith("SLURM_GATEWAY_URL=")
        )
        + "\nSLURM_GATEWAY_URL=\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=compute_env,
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_RUNTIME_ENV_ALIAS_INTERPOLATION"
        and finding.details["key"] == "SLURM_GATEWAY_URL"
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "alternate_value", "operator"),
    [
        ("WORKSPACE_ROOT", "/scratch/frd_muziyao/alternate-workspace", ":+"),
        ("WORKSPACE_ROOT", "/scratch/frd_muziyao/alternate-workspace", "+"),
        ("SLURM_GATEWAY_URL", "http://alternate-slurm-gateway.internal.example:8081", ":+"),
        ("SLURM_GATEWAY_URL", "http://alternate-slurm-gateway.internal.example:8081", "+"),
    ],
)
def test_static_checker_rejects_compute_same_key_alternate_env_drift(
    tmp_path: Path,
    env_key: str,
    alternate_value: str,
    operator: str,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"][env_key] = f"${{{env_key}{operator}{alternate_value}}}"
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT"
        and finding.details["key"] == env_key
        and finding.details["rendered_value"] == alternate_value
        for finding in result.findings
    )


def test_static_checker_rejects_same_key_alternate_payload_drift_when_env_file_value_is_empty(
    tmp_path: Path,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"]["OBJECT_STORE_PREFIX"] = "${OBJECT_STORE_PREFIX:+s3://alternate-prefix}"
    compute_compose = _write_compute_compose(tmp_path, compose)
    compute_env = tmp_path / "compute.example"
    compute_env.write_text(
        "\n".join(
            line
            for line in (REPO_ROOT / "infra/env/compute.example").read_text(encoding="utf-8").splitlines()
            if not line.startswith("OBJECT_STORE_PREFIX=")
        )
        + "\nOBJECT_STORE_PREFIX=\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=compute_env,
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_VALUE_DRIFT"
        and finding.details["key"] == "OBJECT_STORE_PREFIX"
        and finding.details["rendered_value"] == "s3://alternate-prefix"
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("entry", "target_key", "expected_rendered", "extra_env_line", "expected_codes"),
    [
        (
            "${DATABASE_URL:+DATABASE_URL=postgresql://nhms_other_rw:change-me@db.internal.example:5432/nhms}",
            "DATABASE_URL",
            "postgresql://nhms_other_rw:change-me@db.internal.example:5432/nhms",
            None,
            {"COMPUTE_INTERPOLATION_VALUE_DRIFT", "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT"},
        ),
        (
            "${DATABASE_URL:+OBJECT_STORE_ROOT=/ambient/object-store}",
            "OBJECT_STORE_ROOT",
            "/ambient/object-store",
            None,
            {"COMPUTE_INTERPOLATION_VALUE_DRIFT", "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT"},
        ),
        (
            "${WORKSPACE_ROOT:+SLURM_GATEWAY_WORKSPACE_DIR=/ambient/slurm/workspace}",
            "SLURM_GATEWAY_WORKSPACE_DIR",
            "/ambient/slurm/workspace",
            None,
            {"COMPUTE_INTERPOLATION_VALUE_DRIFT", "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT"},
        ),
        (
            "${COMPUTE_DYNAMIC_ENV:+DATABASE_URL=postgresql://nhms_other_rw:change-me@db.internal.example:5432/nhms}",
            "DATABASE_URL",
            "postgresql://nhms_other_rw:change-me@db.internal.example:5432/nhms",
            "COMPUTE_DYNAMIC_ENV=1",
            {"COMPUTE_INTERPOLATION_ENV_UNAPPROVED", "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT"},
        ),
    ],
)
def test_static_checker_rejects_compute_environment_list_dynamic_audited_overwrite(
    tmp_path: Path,
    entry: str,
    target_key: str,
    expected_rendered: str,
    extra_env_line: str | None,
    expected_codes: set[str],
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"] = _environment_dict_to_list(service["environment"])
        service["environment"].append(entry)
    compute_compose = _write_compute_compose(tmp_path, compose)
    compute_env = Path("infra/env/compute.example")
    if extra_env_line is not None:
        compute_env = tmp_path / "compute.example"
        compute_env.write_text(
            (REPO_ROOT / "infra/env/compute.example").read_text(encoding="utf-8") + f"\n{extra_env_line}\n",
            encoding="utf-8",
        )
    compute_env_path = compute_env if compute_env.is_absolute() else REPO_ROOT / compute_env
    compute_env_map = docker_runtime.parse_env_file(compute_env_path)
    rendered_env = docker_runtime._service_environment(next(iter(compose["services"].values())), compute_env_map)
    assert rendered_env[target_key] == expected_rendered
    assert rendered_env[target_key] != compute_env_map[target_key]

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=compute_env,
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert _codes(result) >= expected_codes


def test_static_checker_rejects_compute_workspace_volume_mount_type(tmp_path: Path) -> None:
    compose = docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")
    for service in compose["services"].values():
        for volume in service["volumes"]:
            if volume.get("target") == "${WORKSPACE_ROOT:?set compute workspace root}":
                volume["type"] = "volume"
    compute_compose = tmp_path / "compose.compute.yml"
    compute_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert "COMPUTE_WORKSPACE_MOUNT_TYPE_INVALID" in _codes(result)


def test_static_checker_rejects_compute_published_volume_mount_type(tmp_path: Path) -> None:
    compose = docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")
    for service in compose["services"].values():
        for volume in service["volumes"]:
            if volume.get("target") == "${NHMS_PUBLISHED_ARTIFACT_ROOT:?set container published artifact root}":
                volume["type"] = "volume"
    compute_compose = tmp_path / "compose.compute.yml"
    compute_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert "COMPUTE_PUBLISHED_MOUNT_TYPE_INVALID" in _codes(result)


def test_static_checker_rejects_display_published_volume_mount_type(tmp_path: Path) -> None:
    compose = _safe_display_compose()
    compose["services"]["display-api"]["volumes"][0]["type"] = "volume"
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert _codes(result) >= {"DISPLAY_PUBLISHED_MOUNT_TYPE_INVALID", "DISPLAY_PUBLISHED_MOUNT_MISSING"}


def test_static_checker_rejects_display_published_mount_literal_identity(tmp_path: Path) -> None:
    env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    compose = _safe_display_compose()
    volume = compose["services"]["display-api"]["volumes"][0]
    volume["source"] = env["NHMS_PUBLISHED_ARTIFACT_HOST_ROOT"]
    volume["target"] = env["NHMS_PUBLISHED_ARTIFACT_ROOT"]
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_PUBLISHED_MOUNT_IDENTITY_INVALID" in _codes(result)
    identity_findings = [
        finding for finding in result.findings if finding.code == "DISPLAY_PUBLISHED_MOUNT_IDENTITY_INVALID"
    ]
    assert set(identity_findings[0].details["fields"]) == {"source", "target"}


@pytest.mark.parametrize(
    ("source_key", "target_key", "expected_code"),
    [
        ("WORKSPACE_ROOT", "WORKSPACE_ROOT", "COMPUTE_WORKSPACE_MOUNT_IDENTITY_INVALID"),
        (
            "NHMS_PUBLISHED_ARTIFACT_HOST_ROOT",
            "NHMS_PUBLISHED_ARTIFACT_ROOT",
            "COMPUTE_PUBLISHED_MOUNT_IDENTITY_INVALID",
        ),
        ("NHMS_BASINS_ROOT", "NHMS_BASINS_ROOT", "COMPUTE_BASINS_MOUNT_IDENTITY_INVALID"),
        ("NHMS_MODEL_ASSET_ROOT", "NHMS_MODEL_ASSET_ROOT", "COMPUTE_MODEL_ASSET_MOUNT_IDENTITY_INVALID"),
    ],
)
def test_static_checker_rejects_compute_required_mount_literal_identity(
    tmp_path: Path,
    source_key: str,
    target_key: str,
    expected_code: str,
) -> None:
    env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example")
    compose = _safe_compute_compose()
    _mutate_compute_required_mount(
        compose,
        target_key=target_key,
        values={
            "source": env[source_key],
            "target": env[target_key],
        },
    )
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("type", "${COMPUTE_WORKSPACE_MOUNT_TYPE:-bind}", "COMPUTE_WORKSPACE_MOUNT_TYPE_INVALID"),
        (
            "source",
            "${COMPUTE_WORKSPACE_MOUNT_SOURCE:-/scratch/frd_muziyao/nhms-production/workspace}",
            "COMPUTE_WORKSPACE_MOUNT_IDENTITY_INVALID",
        ),
        (
            "target",
            "${COMPUTE_WORKSPACE_MOUNT_TARGET:-/scratch/frd_muziyao/nhms-production/workspace}",
            "COMPUTE_WORKSPACE_MOUNT_IDENTITY_INVALID",
        ),
    ],
)
def test_static_checker_rejects_compute_required_mount_dynamic_identity(
    tmp_path: Path,
    field: str,
    value: str,
    expected_code: str,
) -> None:
    compose = _safe_compute_compose()
    _mutate_compute_required_mount(
        compose,
        target_key="WORKSPACE_ROOT",
        values={field: value},
    )
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)


def test_static_checker_rejects_compute_api_missing_required_env_and_mounts(tmp_path: Path) -> None:
    compose = docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")
    compute_api = compose["services"]["compute-api"]
    compute_env = dict(compute_api["environment"])
    compute_env.pop("DATABASE_URL")
    compute_api["environment"] = compute_env
    compute_api["volumes"] = [
        volume
        for volume in compute_api["volumes"]
        if "WORKSPACE_ROOT" not in str(volume.get("target", ""))
        and "NHMS_MODEL_ASSET_ROOT" not in str(volume.get("target", ""))
    ]
    compute_compose = tmp_path / "compose.compute.yml"
    compute_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    compute_api_codes = {finding.code for finding in result.findings if finding.service == "compute-api"}
    scheduler_codes = {finding.code for finding in result.findings if finding.service == "scheduler-once"}
    assert compute_api_codes >= {
        "COMPUTE_RUNTIME_ENV_MISSING",
        "COMPUTE_WORKSPACE_MOUNT_MISSING",
        "COMPUTE_MODEL_ASSET_MOUNT_MISSING",
    }
    assert "COMPUTE_RUNTIME_ENV_MISSING" not in scheduler_codes
    assert "COMPUTE_WORKSPACE_MOUNT_MISSING" not in scheduler_codes


def test_static_checker_rejects_display_missing_required_inline_env(tmp_path: Path) -> None:
    compose = docker_runtime.load_compose(REPO_ROOT / "infra/compose.display.yml")
    service_env = dict(compose["services"]["display-api"]["environment"])
    service_env.pop("DATABASE_URL")
    service_env.pop("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX")
    compose["services"]["display-api"]["environment"] = service_env
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    missing = {
        finding.details["key"]
        for finding in result.findings
        if finding.code == "DISPLAY_RUNTIME_ENV_MISSING"
    }
    assert {"DATABASE_URL", "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX"} <= missing


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


def test_gitignore_ignores_real_env_files_but_not_examples() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", "infra/env/compute.env", "infra/env/display.env", "infra/env/local.env"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0
    assert "infra/env/*" in ignored.stdout

    for path in ("infra/env/compute.example", "infra/env/display.example", "infra/env/README.md"):
        trackable = subprocess.run(
            ["git", "check-ignore", "-v", path],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert trackable.returncode == 1


def test_evidence_roots_reject_repo_config_paths_and_allow_approved_roots(tmp_path: Path) -> None:
    result = docker_runtime.StaticCheckResult(status="PASS", findings=())

    with pytest.raises(ValueError, match="artifacts"):
        docker_runtime.ensure_approved_evidence_root(Path("infra/env"), REPO_ROOT)
    with pytest.raises(ValueError, match="artifacts"):
        docker_runtime.write_static_report(result, Path("infra/env/static.json"), REPO_ROOT)

    artifacts = docker_runtime.ensure_approved_evidence_root(Path("artifacts/static.json"), REPO_ROOT)
    assert artifacts == (REPO_ROOT / "artifacts/static.json").resolve()
    external_scratch = docker_runtime.ensure_approved_evidence_root(
        Path("/scratch/frd_muziyao/nwm-outside-evidence/static"),
        REPO_ROOT,
    )
    assert str(external_scratch).startswith("/scratch/frd_muziyao/nwm-outside-evidence")

    other_repo = tmp_path / "repo"
    other_repo.mkdir()
    with pytest.raises(ValueError, match="evidence/temp root"):
        docker_runtime.ensure_approved_evidence_root(tmp_path / "outside", other_repo)


def test_static_report_replaces_output_symlink_without_writing_target(tmp_path: Path) -> None:
    repo_root = tmp_path
    target = repo_root / "infra" / "env" / "display.example"
    target.parent.mkdir(parents=True)
    target.write_text("unchanged config\n", encoding="utf-8")
    report_path = repo_root / "artifacts" / "static" / "static-compose-env-check.json"
    report_path.parent.mkdir(parents=True)
    report_path.symlink_to(target)
    result = docker_runtime.StaticCheckResult(status="PASS", findings=())

    written_path = docker_runtime.write_static_report(result, report_path, repo_root)

    assert written_path == report_path
    assert report_path.is_file()
    assert not report_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "unchanged config\n"
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "PASS"


def test_preflight_defaults_tmpdir_to_repo_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    evidence_root = repo_root / "artifacts" / "preflight"
    expected_tmpdir = repo_root / "artifacts" / "tmp"
    monkeypatch.delenv("TMPDIR", raising=False)

    def runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
        assert os.environ["TMPDIR"] == str(expected_tmpdir.resolve())
        return _docker_available_runner(args)

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=repo_root,
        min_free_bytes=100,
        command_runner=runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "PASS"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["tmpdir"] == str(expected_tmpdir.resolve())


def test_preflight_blocks_explicit_tmpdir_outside_approved_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    evidence_root = repo_root / "artifacts" / "preflight"
    monkeypatch.setenv("TMPDIR", "/tmp")

    def runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
        raise AssertionError(f"preflight commands should be skipped for unsafe TMPDIR: {args}")

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=repo_root,
        min_free_bytes=100,
        command_runner=runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "BLOCKED"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["tmpdir"] == "/tmp"
    assert {blocker["code"] for blocker in payload["blockers"]} == {"TMPDIR_OUTSIDE_APPROVED_ROOT"}
    assert {command["returncode"] for command in payload["commands"].values()} == {125}


def test_preflight_allows_explicit_tmpdir_under_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    evidence_root = repo_root / "artifacts" / "preflight"
    tmpdir = repo_root / "artifacts" / "tmp" / "nested"
    monkeypatch.setenv("TMPDIR", str(tmpdir))

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=repo_root,
        min_free_bytes=100,
        command_runner=_docker_available_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "PASS"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["tmpdir"] == str(tmpdir.resolve())


def test_preflight_replaces_output_symlink_without_writing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    target = repo_root / "infra" / "env" / "display.example"
    target.parent.mkdir(parents=True)
    target.write_text("unchanged config\n", encoding="utf-8")
    evidence_root = repo_root / "artifacts" / "preflight"
    evidence_root.mkdir(parents=True)
    evidence_path = evidence_root / "docker-preflight.json"
    evidence_path.symlink_to(target)
    monkeypatch.delenv("TMPDIR", raising=False)

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=repo_root,
        min_free_bytes=100,
        command_runner=_docker_available_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "PASS"
    assert result.evidence_path == evidence_path
    assert evidence_path.is_file()
    assert not evidence_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "unchanged config\n"
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["status"] == "PASS"


def _safe_display_compose() -> dict[str, Any]:
    return docker_runtime.load_compose(REPO_ROOT / "infra/compose.display.yml")


def _safe_compute_compose() -> dict[str, Any]:
    return docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")


def _mutate_compute_required_mount(
    compose: dict[str, Any],
    *,
    target_key: str,
    values: dict[str, str],
) -> None:
    for service in compose["services"].values():
        for volume in service["volumes"]:
            if isinstance(volume, dict) and target_key in str(volume.get("target", "")):
                volume.update(values)
                return
    raise AssertionError(f"required compute mount for {target_key} not found")


def _write_compute_compose(tmp_path: Path, compose: dict[str, Any]) -> Path:
    compute_compose = tmp_path / "compose.compute.yml"
    compute_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    return compute_compose


def _write_display_compose(tmp_path: Path, compose: dict[str, Any]) -> Path:
    display_compose = tmp_path / "compose.display.yml"
    display_compose.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    return display_compose


def _environment_dict_to_list(environment: dict[str, Any]) -> list[str]:
    return [f"{key}={value}" for key, value in environment.items()]


def _run_display_static_check(display_compose: Path) -> docker_runtime.StaticCheckResult:
    return docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )


def _run_compute_static_check(compute_compose: Path) -> docker_runtime.StaticCheckResult:
    return docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )


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
