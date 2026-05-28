from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

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
