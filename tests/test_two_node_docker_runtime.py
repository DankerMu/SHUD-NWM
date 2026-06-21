from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import validate_two_node_docker_runtime as docker_runtime
from services.production_closure import two_node_e2e_evidence as e2e_evidence

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


def test_static_report_emits_final_compatible_docker_proofs(tmp_path: Path) -> None:
    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    report_path = docker_runtime.write_static_report(
        result,
        tmp_path / "artifacts" / "two-node-e2e" / "run-123" / "docker-security" / "static-compose-env-check.json",
        tmp_path,
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    for proof in docker_runtime.DOCKER_REQUIRED_FALSE_PROOFS:
        assert payload[proof] is False
    for proof in docker_runtime.DOCKER_REQUIRED_TRUE_PROOFS:
        assert payload[proof] is True
    assert set(docker_runtime.DOCKER_STATIC_REQUIRED_PROOFS) <= set(payload["proofs"]["static_required"])


def test_static_report_contradictory_child_blocks_security_summary(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    run_id = "run-123"
    _write_json(source_trust, _source_trust_payload(run_id, security_root, roles=("compute",)))
    _write_json(source_trust_display, _source_trust_payload(run_id, security_root, roles=("display",)))
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": run_id,
            "findings": [],
            **_static_proof_payload(),
            "docker_socket_present": True,
        },
    )
    _write_json(smoke_report, _smoke_payload(run_id))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=run_id,
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["source_statuses"]["static"] == "FAIL"
    static_blocker = next(item for item in payload["blockers"] if item["source"] == "static")
    assert static_blocker["source_findings"][0]["code"] == "DOCKER_SECURITY_STATIC_PROOF_CONTRADICTS_PASS"


@pytest.mark.parametrize("mutation", ["missing_role_env_labels", "unsafe_role_env_labels"])
def test_docker_security_summary_blocks_empty_source_trust_roles_without_safe_role_env_proof(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    source_trust = security_root / "two-node-docker-source-trust-combined.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    run_id = "run-123"
    payload = _source_trust_payload(run_id, security_root)
    payload["roles"] = []
    if mutation == "missing_role_env_labels":
        payload["checked_paths"] = [
            record for record in payload["checked_paths"] if not str(record["label"]).endswith("role env")
        ]
    else:
        for record in payload["checked_paths"]:
            if str(record["label"]).endswith("role env"):
                record["mode"] = "0644"
    _write_json(source_trust, payload)
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": run_id,
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload(run_id))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=run_id,
        source_trust_report=source_trust,
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["source_statuses"]["source_trust"] == "BLOCKED"
    blocker = next(item for item in payload["blockers"] if item["source"] == "source_trust")
    role_env_blockers = {
        (item["code"], item["label"])
        for item in blocker["source_blockers"]
        if item.get("label") in {"compute role env", "display role env"}
    }
    if mutation == "missing_role_env_labels":
        expected_code = "DOCKER_SECURITY_SOURCE_TRUST_REQUIRED_LABEL_MISSING"
    else:
        expected_code = "DOCKER_SECURITY_SOURCE_TRUST_ROLE_ENV_MODE_INVALID"
    assert role_env_blockers >= {
        (expected_code, "compute role env"),
        (expected_code, "display role env"),
    }


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
        if "compose.compute.yml" in command:
            rendered = yaml.safe_load(completed.stdout)
            for service_name in ("compute-api", "scheduler-once"):
                assert (
                    rendered["services"][service_name]["environment"]["NHMS_REQUIRE_FORECAST_WARM_START"]
                    == "true"
                )


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


@pytest.mark.parametrize(
    ("secret_key", "fake_secret"),
    [
        ("AWS_SECRET_ACCESS_KEY", "SETUP_AWS_SECRET left,comma;tail"),
        ("DATABASE_URL", "postgresql://setup:SETUP_DB_SECRET left,comma;tail@db/nhms"),
    ],
)
def test_static_cli_redacts_setup_failure_secret_parse_messages(
    tmp_path: Path,
    secret_key: str,
    fake_secret: str,
) -> None:
    repo_root = tmp_path
    report_path = repo_root / "artifacts" / "stage-change" / "static.json"
    bad_display_compose = tmp_path / "bad-compose.yml"
    bad_display_compose.write_text(
        f"""
services:
  display-api:
    environment:
      {secret_key}: [{fake_secret}
""",
        encoding="utf-8",
    )
    args = [
        sys.executable,
        str(REPO_ROOT / "scripts/validate_two_node_docker_runtime.py"),
        "--repo-root",
        str(repo_root),
        "static",
        "--compute-compose",
        str(REPO_ROOT / "infra/compose.compute.yml"),
        "--display-compose",
        str(bad_display_compose),
        "--compute-env",
        str(REPO_ROOT / "infra/env/compute.example"),
        "--display-env",
        str(REPO_ROOT / "infra/env/display.example"),
        "--report",
        str(report_path),
    ]

    completed = subprocess.run(args, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    report_text = report_path.read_text(encoding="utf-8")
    combined_output = completed.stdout + completed.stderr + report_text

    assert completed.returncode != 0
    assert fake_secret not in combined_output
    assert fake_secret.split()[0] not in combined_output
    assert "<redacted>" in combined_output
    payload = json.loads(report_text)
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


@pytest.mark.parametrize(
    "env_key",
    [
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
    ],
)
def test_static_checker_rejects_display_env_file_scheduler_roots(tmp_path: Path, env_key: str) -> None:
    display_env = tmp_path / "display.example"
    display_env.write_text(
        (REPO_ROOT / "infra/env/display.example").read_text(encoding="utf-8")
        + f"\n{env_key}=/scratch/private/scheduler-root\n",
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


def test_static_checker_rejects_display_env_file_object_store_root_drift(tmp_path: Path) -> None:
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
    assert "ENV_REQUIRED_VALUE_INVALID" in _codes(result)


@pytest.mark.parametrize(
    "environment_entry",
    [
        {"NHMS_SCHEDULER_RUNTIME_ROOT": "/scratch/private/scheduler-runtime"},
        ["NHMS_SCHEDULER_RUNTIME_ROOT=/scratch/private/scheduler-runtime"],
        ["${EXTRA_ENV:-NHMS_SCHEDULER_RUNTIME_ROOT=/scratch/private/scheduler-runtime}"],
    ],
)
def test_static_checker_rejects_display_service_object_store_root(
    tmp_path: Path,
    environment_entry: dict[str, str] | list[str],
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    if isinstance(environment_entry, dict):
        service["environment"].update(environment_entry)
    else:
        service["environment"] = _environment_dict_to_list(service["environment"])
        service["environment"].extend(environment_entry)
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "DISPLAY_FORBIDDEN_ENV" in _codes(result)


@pytest.mark.parametrize(
    ("env_key", "value"),
    [
        ("NHMS_SCHEDULER_LOCK_ROOT", "/scratch/private/scheduler-locks"),
        ("NHMS_SCHEDULER_LOCK_ROOT", ""),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", "/scratch/private/scheduler-evidence"),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", ""),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "/scratch/private/scheduler-runtime"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", ""),
        ("NHMS_SCHEDULER_TEMP_ROOT", "/scratch/private/scheduler-temp"),
        ("NHMS_SCHEDULER_TEMP_ROOT", ""),
    ],
)
def test_two_node_e2e_evidence_rejects_display_scheduler_root_env(
    env_key: str,
    value: str,
) -> None:
    payload = {
        "display_container_inspect": [
            {
                "Config": {
                    "Env": [
                        "NHMS_SERVICE_ROLE=display_readonly",
                        f"{env_key}={value}",
                    ]
                }
            }
        ]
    }

    proofs = e2e_evidence._docker_display_security_proofs(payload)
    findings = e2e_evidence._docker_proof_findings(proofs)

    assert proofs["forbidden_env_hazard"] is True
    assert {
        (finding["code"], finding.get("capability"))
        for finding in findings
    } >= {("TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY", "forbidden_env_hazard")}


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


def test_static_checker_rejects_live_mvt_flag_as_compute_interpolation(tmp_path: Path) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"]["NHMS_ENABLE_LIVE_POSTGIS_MVT"] = "${NHMS_ENABLE_LIVE_POSTGIS_MVT:-true}"
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_ENV_UNAPPROVED"
        and finding.details["unapproved_keys"] == ["NHMS_ENABLE_LIVE_POSTGIS_MVT"]
        for finding in result.findings
    )


@pytest.mark.parametrize(
    ("env_key", "expression"),
    [
        ("NHMS_REQUIRE_FORECAST_WARM_START", "${NHMS_REQUIRE_FORECAST_WARM_START:-true}"),
        ("NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC", "${NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC-0,12}"),
        ("GFS_CYCLE_HOURS_UTC", "${GFS_CYCLE_HOURS_UTC-0,12}"),
        ("IFS_CYCLE_HOURS_UTC", "${IFS_CYCLE_HOURS_UTC-0,12}"),
    ],
)
def test_static_checker_approves_compute_scheduler_allowed_cycle_hours_interpolation(
    tmp_path: Path,
    env_key: str,
    expression: str,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"][env_key] = expression
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


@pytest.mark.parametrize("env_key", ["NHMS_REQUIRE_FORECAST_WARM_START", "GFS_CYCLE_HOURS_UTC", "IFS_CYCLE_HOURS_UTC"])
def test_static_checker_requires_compute_runtime_env(
    tmp_path: Path,
    env_key: str,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"].pop(env_key, None)
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert {
        (finding.service, finding.details.get("key"))
        for finding in result.findings
        if finding.code == "COMPUTE_RUNTIME_ENV_MISSING"
    } >= {
        ("compute-api", env_key),
        ("scheduler-once", env_key),
    }


def test_static_checker_rejects_unapproved_compute_scheduler_cycle_lag_interpolation(tmp_path: Path) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"]["NHMS_SCHEDULER_CYCLE_LAG_HOURS"] = "${NHMS_SCHEDULER_CYCLE_LAG_HOURS:-6}"
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_ENV_UNAPPROVED"
        and finding.details["unapproved_keys"] == ["NHMS_SCHEDULER_CYCLE_LAG_HOURS"]
        for finding in result.findings
    )


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
    ("field", "value", "expected_rendered"),
    [
        ("image", "evil/${NHMS_APP_IMAGE}:${NHMS_IMAGE_TAG}", "evil/nhms-app:m22-placeholder"),
        ("user", "prefix-${NHMS_CONTAINER_UID}:${NHMS_CONTAINER_GID}", "prefix-1000:1000"),
        ("ports", "127.0.0.1:1${NHMS_DISPLAY_API_PORT}:8000", "127.0.0.1:18000:8000"),
        ("command", "echo prefix-${NHMS_APP_IMAGE}", "echo prefix-nhms-app"),
        ("entrypoint", "echo prefix-${NHMS_APP_IMAGE}", "echo prefix-nhms-app"),
    ],
)
def test_static_checker_rejects_display_field_render_drift(
    tmp_path: Path,
    field: str,
    value: str,
    expected_rendered: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    if field == "ports":
        service[field] = [value]
    elif field in {"command", "entrypoint"}:
        service[field] = ["sh", "-lc", value]
    else:
        service[field] = value
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    field_findings = [
        finding
        for finding in result.findings
        if finding.code == "DISPLAY_INTERPOLATION_FIELD_RENDER_DRIFT" and finding.details["field"] == field
    ]
    assert field_findings
    assert expected_rendered in json.dumps(field_findings[0].details["actual_rendered"])


@pytest.mark.parametrize(
    ("field", "value", "expected_rendered"),
    [
        ("image", "evil/${NHMS_APP_IMAGE}:${NHMS_IMAGE_TAG}", "evil/nhms-app:m22-placeholder"),
        ("user", "prefix-${NHMS_CONTAINER_UID}:${NHMS_CONTAINER_GID}", "prefix-1000:1000"),
        ("command", "echo prefix-${NHMS_APP_IMAGE}", "echo prefix-nhms-app"),
        ("entrypoint", "echo prefix-${NHMS_APP_IMAGE}", "echo prefix-nhms-app"),
    ],
)
def test_static_checker_rejects_compute_field_render_drift(
    tmp_path: Path,
    field: str,
    value: str,
    expected_rendered: str,
) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        if field in {"command", "entrypoint"}:
            service[field] = ["sh", "-lc", value]
        else:
            service[field] = value
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    field_findings = [
        finding
        for finding in result.findings
        if finding.code == "COMPUTE_INTERPOLATION_FIELD_RENDER_DRIFT" and finding.details["field"] == field
    ]
    assert field_findings
    assert expected_rendered in json.dumps(field_findings[0].details["actual_rendered"])


@pytest.mark.parametrize("role", ["display", "compute"])
@pytest.mark.parametrize("entrypoint", [[], ""])
def test_static_checker_rejects_present_empty_entrypoint_overrides(
    tmp_path: Path,
    role: str,
    entrypoint: Any,
) -> None:
    if role == "display":
        compose = _safe_display_compose()
        compose["services"]["display-api"]["entrypoint"] = entrypoint
        result = _run_display_static_check(_write_display_compose(tmp_path, compose))
    elif role == "compute":
        compose = _safe_compute_compose()
        for service in compose["services"].values():
            service["entrypoint"] = entrypoint
        result = _run_compute_static_check(_write_compute_compose(tmp_path, compose))
    else:
        raise AssertionError(f"unhandled role: {role}")

    expected_code = f"{role.upper()}_INTERPOLATION_FIELD_RENDER_DRIFT"
    assert result.status == "FAIL"
    assert any(
        finding.code == expected_code and finding.details["field"] == "entrypoint"
        for finding in result.findings
    )


@pytest.mark.parametrize("role", ["display", "compute"])
def test_static_checker_accepts_null_entrypoint_as_absent(tmp_path: Path, role: str) -> None:
    if role == "display":
        compose = _safe_display_compose()
        compose["services"]["display-api"]["entrypoint"] = None
        result = _run_display_static_check(_write_display_compose(tmp_path, compose))
    elif role == "compute":
        compose = _safe_compute_compose()
        for service in compose["services"].values():
            service["entrypoint"] = None
        result = _run_compute_static_check(_write_compute_compose(tmp_path, compose))
    else:
        raise AssertionError(f"unhandled role: {role}")

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


@pytest.mark.parametrize("ports", [None, []])
def test_static_checker_accepts_compute_ports_absent_or_empty_list(tmp_path: Path, ports: Any) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["ports"] = ports

    result = _run_compute_static_check(_write_compute_compose(tmp_path, compose))

    assert result.status == "PASS", [finding.to_dict() for finding in result.findings]


@pytest.mark.parametrize("ports", ["", {}])
def test_static_checker_rejects_invalid_falsey_compute_ports(tmp_path: Path, ports: Any) -> None:
    compose = _safe_compute_compose()
    compose["services"]["compute-api"]["ports"] = ports

    result = _run_compute_static_check(_write_compute_compose(tmp_path, compose))

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_FIELD_RENDER_DRIFT" and finding.details["field"] == "ports"
        for finding in result.findings
    )


def test_compose_dollar_escape_parser_keeps_escaped_braced_interpolation_literal() -> None:
    env = {"NHMS_APP_IMAGE": "nhms-app"}

    escaped = "$${NHMS_APP_IMAGE:+wrong}"

    assert docker_runtime._compose_interpolation_occurrences_from_text(escaped) == []
    assert docker_runtime._compose_interpolation_keys_from_text(escaped) == set()
    assert docker_runtime._resolve_compose_value(escaped, env) == "${NHMS_APP_IMAGE:+wrong}"


def test_compose_interpolation_accepts_bounded_nested_payloads() -> None:
    nested = "${A:-${B:-fallback}}"

    assert docker_runtime._compose_interpolation_keys_from_text(nested) == {"A", "B"}
    assert docker_runtime._resolve_compose_value(nested, {}) == "fallback"


def test_compose_interpolation_accepts_bounded_nested_objects() -> None:
    nested: dict[str, Any] = {"services": {"display-api": {"environment": [{"SAFE": "${DATABASE_URL}"}]}}}

    assert docker_runtime._compose_interpolation_keys(nested) == {"DATABASE_URL"}
    assert list(docker_runtime._compose_interpolation_text_nodes(nested))


def test_compose_interpolation_rejects_over_limit_nesting_without_recursion_error() -> None:
    levels = docker_runtime.MAX_COMPOSE_INTERPOLATION_DEPTH + 2
    over_limit = "${A:-" * levels + "fallback" + "}" * levels

    with pytest.raises(docker_runtime.ComposeInterpolationLimitError):
        docker_runtime._compose_interpolation_keys_from_text(over_limit)
    with pytest.raises(docker_runtime.ComposeInterpolationLimitError):
        docker_runtime._resolve_compose_value(over_limit, {})


@pytest.mark.parametrize("shape", ["mapping", "sequence"])
def test_compose_interpolation_rejects_over_limit_object_traversal_without_recursion_error(shape: str) -> None:
    nested: Any = "${DATABASE_URL}"
    for index in range(docker_runtime.MAX_COMPOSE_INTERPOLATION_DEPTH + 2):
        nested = {f"level_{index}": nested} if shape == "mapping" else [nested]

    with pytest.raises(docker_runtime.ComposeInterpolationLimitError):
        docker_runtime._compose_interpolation_keys(nested)
    with pytest.raises(docker_runtime.ComposeInterpolationLimitError):
        list(docker_runtime._compose_interpolation_text_nodes(nested))


def test_compose_interpolation_accepts_below_limit_aggregate_occurrences() -> None:
    payload_occurrences = (docker_runtime.MAX_COMPOSE_INTERPOLATION_OCCURRENCES - 2) // 2
    aggregate = (
        "${A:-"
        + "$A" * payload_occurrences
        + "}${B:-"
        + "$B" * payload_occurrences
        + "}"
    )

    occurrences = docker_runtime._compose_interpolation_occurrences_from_text(aggregate)

    assert len(occurrences) == docker_runtime.MAX_COMPOSE_INTERPOLATION_OCCURRENCES


def test_compose_interpolation_rejects_over_limit_aggregate_occurrences_without_recursion_error() -> None:
    payload_occurrences = docker_runtime.MAX_COMPOSE_INTERPOLATION_OCCURRENCES // 2
    aggregate = (
        "${A:-"
        + "$A" * payload_occurrences
        + "}${B:-"
        + "$B" * payload_occurrences
        + "}"
    )

    with pytest.raises(docker_runtime.ComposeInterpolationLimitError):
        docker_runtime._compose_interpolation_occurrences_from_text(aggregate)


def test_static_checker_reports_over_limit_interpolation_as_finding(tmp_path: Path) -> None:
    levels = docker_runtime.MAX_COMPOSE_INTERPOLATION_DEPTH + 2
    over_limit = "${NHMS_APP_IMAGE:-" * levels + "nhms-app" + "}" * levels
    compose = _safe_display_compose()
    compose["services"]["display-api"]["image"] = f"{over_limit}:${{NHMS_IMAGE_TAG}}"
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "COMPOSE_INTERPOLATION_COMPLEXITY_LIMIT_EXCEEDED" in _codes(result)


@pytest.mark.parametrize("shape", ["mapping", "sequence"])
def test_static_checker_reports_over_limit_object_traversal_as_finding(tmp_path: Path, shape: str) -> None:
    nested: Any = "${DATABASE_URL}"
    for index in range(docker_runtime.MAX_COMPOSE_INTERPOLATION_DEPTH + 2):
        nested = {f"level_{index}": nested} if shape == "mapping" else [nested]
    compose = _safe_display_compose()
    compose["x-over-limit"] = nested
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "COMPOSE_INTERPOLATION_COMPLEXITY_LIMIT_EXCEEDED" in _codes(result)


def test_static_checker_reports_over_limit_aggregate_occurrences_as_finding(tmp_path: Path) -> None:
    payload_occurrences = docker_runtime.MAX_COMPOSE_INTERPOLATION_OCCURRENCES // 2
    aggregate = (
        "${DATABASE_URL:-"
        + "$DATABASE_URL" * payload_occurrences
        + "}${DATABASE_URL:+"
        + "$DATABASE_URL" * payload_occurrences
        + "}"
    )
    compose = _safe_display_compose()
    compose["services"]["display-api"]["image"] = f"nhms-app:{aggregate}"
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert "COMPOSE_INTERPOLATION_COMPLEXITY_LIMIT_EXCEEDED" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value", "key", "expected_rendered"),
    [
        (
            "image",
            "$$${NHMS_APP_IMAGE:+unexpected-display-app}:${NHMS_IMAGE_TAG}",
            "NHMS_APP_IMAGE",
            "$unexpected-display-app",
        ),
        ("image", "$$$NHMS_APP_IMAGE:${NHMS_IMAGE_TAG}", "NHMS_APP_IMAGE", "$nhms-app"),
        (
            "user",
            "$$${NHMS_CONTAINER_UID:+0}:${NHMS_CONTAINER_GID}",
            "NHMS_CONTAINER_UID",
            "$0",
        ),
        ("user", "$$$NHMS_CONTAINER_UID:${NHMS_CONTAINER_GID}", "NHMS_CONTAINER_UID", "$1000"),
    ],
)
def test_static_checker_rejects_display_dollar_run_interpolation_drift(
    tmp_path: Path,
    field: str,
    value: str,
    key: str,
    expected_rendered: str,
) -> None:
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    display_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    service[field] = value
    assert docker_runtime._resolve_compose_value(value, display_env).startswith(expected_rendered)
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
    ("field", "value", "key", "expected_rendered"),
    [
        (
            "image",
            "$$${NHMS_APP_IMAGE:+unexpected-compute-app}:${NHMS_IMAGE_TAG}",
            "NHMS_APP_IMAGE",
            "$unexpected-compute-app",
        ),
        ("image", "$$$NHMS_APP_IMAGE:${NHMS_IMAGE_TAG}", "NHMS_APP_IMAGE", "$nhms-app"),
        (
            "user",
            "$$${NHMS_CONTAINER_UID:+0}:${NHMS_CONTAINER_GID}",
            "NHMS_CONTAINER_UID",
            "$0",
        ),
        ("user", "$$$NHMS_CONTAINER_UID:${NHMS_CONTAINER_GID}", "NHMS_CONTAINER_UID", "$1000"),
    ],
)
def test_static_checker_rejects_compute_dollar_run_interpolation_drift(
    tmp_path: Path,
    field: str,
    value: str,
    key: str,
    expected_rendered: str,
) -> None:
    compose = _safe_compute_compose()
    compute_env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/compute.example")
    for service in compose["services"].values():
        service[field] = value
    assert docker_runtime._resolve_compose_value(value, compute_env).startswith(expected_rendered)
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_INTERPOLATION_VALUE_DRIFT"
        and finding.details["key"] == key
        and finding.details["rendered_value"] == expected_rendered
        for finding in result.findings
    )


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


@pytest.mark.parametrize(
    ("role", "entry", "fake_secret", "expected_code"),
    [
        (
            "display",
            "AWS_SECRET_ACCESS_KEY=<fake-display-secret>",
            "<fake-display-secret>",
            "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT",
        ),
        (
            "compute",
            "DATABASE_URL=<fake-compute-dsn>",
            "<fake-compute-dsn>",
            "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT",
        ),
        (
            "display",
            "${DISPLAY_SECRET_ENTRY:-AWS_SECRET_ACCESS_KEY=<fake-display-payload>}",
            "<fake-display-payload>",
            "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT",
        ),
        (
            "compute",
            "${COMPUTE_SECRET_ENTRY:-DATABASE_URL=<fake-compute-payload>}",
            "<fake-compute-payload>",
            "COMPUTE_RUNTIME_ENV_LITERAL_DRIFT",
        ),
    ],
)
def test_static_result_serialization_redacts_secret_list_entries_and_payloads(
    tmp_path: Path,
    role: str,
    entry: str,
    fake_secret: str,
    expected_code: str,
) -> None:
    if role == "display":
        compose = _safe_display_compose()
        service = compose["services"]["display-api"]
        service["environment"] = _environment_dict_to_list(service["environment"])
        service["environment"].append(entry)
        result = _run_display_static_check(_write_display_compose(tmp_path, compose))
    elif role == "compute":
        compose = _safe_compute_compose()
        for service in compose["services"].values():
            service["environment"] = _environment_dict_to_list(service["environment"])
            service["environment"].append(entry)
        result = _run_compute_static_check(_write_compute_compose(tmp_path, compose))
    else:
        raise AssertionError(f"unhandled role: {role}")

    serialized = json.dumps(result.to_dict(), sort_keys=True)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)
    assert fake_secret not in serialized
    assert "<redacted>" in serialized


def test_static_result_serialization_redacts_secret_values_across_finding_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file_secret = "ENV_SECRET_VALUE left,env-comma;env-tail]"
    list_secret = "LIST_SECRET_VALUE left,list-comma;list-tail]"
    payload_secret = "PAYLOAD_SECRET_VALUE left,payload-comma;payload-tail]"
    ambient_secret = "AMBIENT_SECRET_VALUE left,ambient-comma;ambient-tail]"
    display_env = tmp_path / "display.example"
    display_env.write_text(
        "\n".join(
            "AWS_SECRET_ACCESS_KEY=" + env_file_secret
            if line.startswith("AWS_SECRET_ACCESS_KEY=")
            else line
            for line in (REPO_ROOT / "infra/env/display.example").read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", ambient_secret)
    compose = _safe_display_compose()
    service = compose["services"]["display-api"]
    canonical_volume = dict(service["volumes"][0])
    source_secret_volume = dict(canonical_volume)
    source_secret_volume["source"] = "${AWS_SECRET_ACCESS_KEY}"
    target_secret_volume = dict(canonical_volume)
    target_secret_volume["target"] = "${AWS_SECRET_ACCESS_KEY}"
    service["volumes"] = [
        source_secret_volume,
        target_secret_volume,
        "secret-device:/secret:ro",
    ]
    compose["volumes"] = {
        "secret-device": {
            "driver": "local",
            "driver_opts": {"type": "none", "o": "bind", "device": "${AWS_SECRET_ACCESS_KEY}"},
        }
    }
    service["tmpfs"].append({"target": "${AWS_SECRET_ACCESS_KEY}"})
    service["image"] = "nhms-app-${AWS_SECRET_ACCESS_KEY}:${NHMS_IMAGE_TAG:-m22-placeholder}"
    service["environment"] = _environment_dict_to_list(service["environment"])
    service["environment"].append(f"AWS_SECRET_ACCESS_KEY={list_secret}")
    service["environment"].append(f"${{DISPLAY_SECRET_ENTRY:-AWS_SECRET_ACCESS_KEY={payload_secret}}}")
    display_compose = _write_display_compose(tmp_path, compose)

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=display_env,
        repo_root=REPO_ROOT,
    )
    serialized = json.dumps(result.to_dict(), sort_keys=True)

    assert result.status == "FAIL"
    assert _codes(result) >= {
        "DISPLAY_PUBLISHED_SOURCE_DRIFT",
        "DISPLAY_PUBLISHED_TARGET_DRIFT",
        "DISPLAY_UNAPPROVED_TMPFS",
        "DISPLAY_NAMED_VOLUME_FORBIDDEN_SOURCE",
        "DISPLAY_INTERPOLATION_FIELD_RENDER_DRIFT",
        "DISPLAY_RUNTIME_ENV_LITERAL_DRIFT",
        "DISPLAY_AMBIENT_ENV_OVERRIDE",
    }
    for secret_fragment in (
        "ENV_SECRET_VALUE",
        "env-comma;env-tail]",
        "LIST_SECRET_VALUE",
        "list-comma;list-tail]",
        "PAYLOAD_SECRET_VALUE",
        "payload-comma;payload-tail]",
        "AMBIENT_SECRET_VALUE",
        "ambient-comma;ambient-tail]",
    ):
        assert secret_fragment not in serialized
    assert "<redacted>" in serialized


def test_static_result_serialization_redacts_nested_secret_key_mappings() -> None:
    nested_secret = "NESTED_SECRET_VALUE left,nested-comma;nested-tail]"
    nested_database_secret = "postgresql://user:NESTED_DB_SECRET left,nested-db-tail]@db/nhms"
    json_mapping_secret = "JSON_DB_SECRET left,json-db-tail]"
    result = docker_runtime.StaticCheckResult(
        status="FAIL",
        findings=(
            docker_runtime.Finding(
                "TEST_SECRET_MAPPING",
                "synthetic finding for redaction coverage.",
                details={
                    "outer": {
                        "AWS_SECRET_ACCESS_KEY": nested_secret,
                        "nested": {"DATABASE_URL": nested_database_secret},
                    },
                    "json_line": f'"DATABASE_URL": "{json_mapping_secret}"',
                },
            ),
        ),
    )

    serialized = json.dumps(result.to_dict(), sort_keys=True)

    assert "NESTED_SECRET_VALUE" not in serialized
    assert "nested-comma;nested-tail]" not in serialized
    assert "NESTED_DB_SECRET" not in serialized
    assert "nested-db-tail]" not in serialized
    assert "JSON_DB_SECRET" not in serialized
    assert "json-db-tail]" not in serialized
    assert "<redacted>" in serialized


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
    ("mutation", "expected_code"),
    [
        ("missing", "DISPLAY_OBJECT_STORE_MOUNT_MISSING"),
        ("not_readonly", "DISPLAY_OBJECT_STORE_MOUNT_NOT_READONLY"),
        ("type_invalid", "DISPLAY_OBJECT_STORE_MOUNT_TYPE_INVALID"),
        ("identity_invalid", "DISPLAY_OBJECT_STORE_MOUNT_IDENTITY_INVALID"),
    ],
)
def test_static_checker_rejects_display_object_store_mount_contract(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    env = docker_runtime.parse_env_file(REPO_ROOT / "infra/env/display.example")
    compose = _safe_display_compose()
    volumes = compose["services"]["display-api"]["volumes"]
    object_store_volume = next(volume for volume in volumes if "OBJECT_STORE_ROOT" in str(volume.get("target", "")))

    if mutation == "missing":
        volumes.remove(object_store_volume)
    elif mutation == "not_readonly":
        object_store_volume["read_only"] = False
    elif mutation == "type_invalid":
        object_store_volume["type"] = "volume"
    elif mutation == "identity_invalid":
        object_store_volume["source"] = env["OBJECT_STORE_ROOT"]
        object_store_volume["target"] = env["OBJECT_STORE_ROOT"]
    else:
        raise AssertionError(f"unhandled mutation: {mutation}")
    display_compose = _write_display_compose(tmp_path, compose)

    result = _run_display_static_check(display_compose)

    assert result.status == "FAIL"
    assert expected_code in _codes(result)


@pytest.mark.parametrize(
    ("source_key", "target_key", "expected_code"),
    [
        ("WORKSPACE_ROOT", "WORKSPACE_ROOT", "COMPUTE_WORKSPACE_MOUNT_IDENTITY_INVALID"),
        ("OBJECT_STORE_ROOT", "OBJECT_STORE_ROOT", "COMPUTE_OBJECT_STORE_MOUNT_IDENTITY_INVALID"),
        (
            "NHMS_OBJECT_STORE_COPYBACK_ROOT",
            "NHMS_OBJECT_STORE_COPYBACK_ROOT",
            "COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_IDENTITY_INVALID",
        ),
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
        and "OBJECT_STORE_ROOT" not in str(volume.get("target", ""))
        and "NHMS_OBJECT_STORE_COPYBACK_ROOT" not in str(volume.get("target", ""))
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
        "COMPUTE_OBJECT_STORE_MOUNT_MISSING",
        "COMPUTE_OBJECT_STORE_COPYBACK_MOUNT_MISSING",
        "COMPUTE_MODEL_ASSET_MOUNT_MISSING",
    }
    assert "COMPUTE_RUNTIME_ENV_MISSING" not in scheduler_codes
    assert "COMPUTE_WORKSPACE_MOUNT_MISSING" not in scheduler_codes


def test_static_checker_requires_compute_host_gateway_extra_host(tmp_path: Path) -> None:
    compose = _safe_compute_compose()
    compose["services"]["compute-api"].pop("extra_hosts", None)
    compose["services"]["scheduler-once"]["extra_hosts"] = ["example.internal:127.0.0.1"]
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = _run_compute_static_check(compute_compose)

    assert result.status == "FAIL"
    findings = {
        (finding.service, finding.details.get("required"))
        for finding in result.findings
        if finding.code == "COMPUTE_HOST_GATEWAY_MISSING"
    }
    assert findings == {
        ("compute-api", docker_runtime.COMPUTE_REQUIRED_EXTRA_HOST),
        ("scheduler-once", docker_runtime.COMPUTE_REQUIRED_EXTRA_HOST),
    }


def test_static_checker_rejects_empty_compute_scheduler_allowed_roots(tmp_path: Path) -> None:
    compose = _safe_compute_compose()
    for service in compose["services"].values():
        service["environment"]["NHMS_SCHEDULER_ALLOWED_ROOTS"] = ""
    compute_compose = _write_compute_compose(tmp_path, compose)

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=Path("infra/compose.display.yml"),
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    findings = {
        (finding.service, finding.details.get("key"))
        for finding in result.findings
        if finding.code == "COMPUTE_RUNTIME_ENV_EMPTY"
    }
    assert findings >= {
        ("compute-api", "NHMS_SCHEDULER_ALLOWED_ROOTS"),
        ("scheduler-once", "NHMS_SCHEDULER_ALLOWED_ROOTS"),
    }


def test_static_checker_requires_literal_uv_cache_dir_for_non_root_runtime(tmp_path: Path) -> None:
    compute = docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")
    for service in compute["services"].values():
        service["environment"].pop("UV_CACHE_DIR", None)
    display = docker_runtime.load_compose(REPO_ROOT / "infra/compose.display.yml")
    display["services"]["display-api"]["environment"].pop("UV_CACHE_DIR", None)
    compute_compose = tmp_path / "compose.compute.yml"
    display_compose = tmp_path / "compose.display.yml"
    compute_compose.write_text(yaml.safe_dump(compute, sort_keys=False), encoding="utf-8")
    display_compose.write_text(yaml.safe_dump(display, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert {
        (finding.code, finding.service, finding.details.get("key"))
        for finding in result.findings
        if finding.code.endswith("_RUNTIME_ENV_MISSING")
    } >= {
        ("COMPUTE_RUNTIME_ENV_MISSING", "compute-api", "UV_CACHE_DIR"),
        ("COMPUTE_RUNTIME_ENV_MISSING", "scheduler-once", "UV_CACHE_DIR"),
        ("DISPLAY_RUNTIME_ENV_MISSING", "display-api", "UV_CACHE_DIR"),
    }


@pytest.mark.parametrize(
    ("object_store_root", "copyback_root", "relationship"),
    [
        (
            "/scratch/frd_muziyao/nhms-production/object-store",
            "/scratch/frd_muziyao/nhms-production/object-store/copyback",
            "copyback_root_under_object_store_root",
        ),
        (
            "/scratch/frd_muziyao/nhms-production/object-store/shared",
            "/scratch/frd_muziyao/nhms-production/object-store",
            "object_store_root_under_copyback_root",
        ),
    ],
)
def test_static_checker_rejects_compute_object_store_copyback_root_overlap(
    tmp_path: Path,
    object_store_root: str,
    copyback_root: str,
    relationship: str,
) -> None:
    compute_env = tmp_path / "compute.example"
    compute_env.write_text(
        "\n".join(
            "OBJECT_STORE_ROOT=" + object_store_root
            if line.startswith("OBJECT_STORE_ROOT=")
            else "NHMS_OBJECT_STORE_COPYBACK_ROOT=" + copyback_root
            if line.startswith("NHMS_OBJECT_STORE_COPYBACK_ROOT=")
            else line
            for line in (REPO_ROOT / "infra/env/compute.example").read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_static_check(
        compute_compose=Path("infra/compose.compute.yml"),
        display_compose=Path("infra/compose.display.yml"),
        compute_env=compute_env,
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert any(
        finding.code == "COMPUTE_OBJECT_STORE_COPYBACK_ROOT_OVERLAP"
        and finding.details["relationship"] == relationship
        for finding in result.findings
    )


def test_static_checker_rejects_uv_cache_dir_compose_interpolation(tmp_path: Path) -> None:
    compute = docker_runtime.load_compose(REPO_ROOT / "infra/compose.compute.yml")
    for service in compute["services"].values():
        service["environment"]["UV_CACHE_DIR"] = "${UV_CACHE_DIR:-/tmp/nhms-uv-cache}"
    display = docker_runtime.load_compose(REPO_ROOT / "infra/compose.display.yml")
    display["services"]["display-api"]["environment"]["UV_CACHE_DIR"] = "${UV_CACHE_DIR:-/tmp/nhms-uv-cache}"
    compute_compose = tmp_path / "compose.compute.yml"
    display_compose = tmp_path / "compose.display.yml"
    compute_compose.write_text(yaml.safe_dump(compute, sort_keys=False), encoding="utf-8")
    display_compose.write_text(yaml.safe_dump(display, sort_keys=False), encoding="utf-8")

    result = docker_runtime.run_static_check(
        compute_compose=compute_compose,
        display_compose=display_compose,
        compute_env=Path("infra/env/compute.example"),
        display_env=Path("infra/env/display.example"),
        repo_root=REPO_ROOT,
    )

    assert result.status == "FAIL"
    assert {
        (finding.code, tuple(finding.details.get("unapproved_keys", ())))
        for finding in result.findings
    } >= {
        ("COMPUTE_INTERPOLATION_ENV_UNAPPROVED", ("UV_CACHE_DIR",)),
        ("DISPLAY_INTERPOLATION_ENV_UNAPPROVED", ("UV_CACHE_DIR",)),
    }


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
        evidence_run_id="run-123",
        min_free_bytes=1024,
        command_runner=_docker_unavailable_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "BLOCKED"
    assert result.evidence_path.is_file()
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["evidence_run_id"] == "run-123"
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


def test_static_report_explicit_evidence_run_id_overrides_scratch_path_inference(tmp_path: Path) -> None:
    repo_root = tmp_path
    report_path = Path("/scratch/frd_muziyao/nwm-test/run-static-explicit/docker-security/static.json")
    result = docker_runtime.StaticCheckResult(status="PASS", findings=())

    written_path = docker_runtime.write_static_report(
        result,
        report_path,
        repo_root,
        evidence_run_id="run-static-explicit",
    )

    payload = json.loads(written_path.read_text(encoding="utf-8"))
    assert payload["evidence_run_id"] == "run-static-explicit"
    written_path.unlink()


def test_preflight_defaults_tmpdir_to_repo_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    evidence_root = repo_root / "artifacts" / "two-node-e2e" / "run-456" / "docker-preflight"
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
    assert payload["evidence_run_id"] == "run-456"
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


def test_preflight_explicit_evidence_run_id_overrides_path_derivation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    evidence_root = repo_root / "artifacts" / "preflight"
    monkeypatch.delenv("TMPDIR", raising=False)

    result = docker_runtime.run_preflight(
        evidence_root=evidence_root,
        repo_root=repo_root,
        evidence_run_id="explicit-run",
        min_free_bytes=100,
        command_runner=_docker_available_runner,
        disk_usage_provider=_high_space,
    )

    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert result.status == "PASS"
    assert payload["evidence_run_id"] == "explicit-run"


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


def test_app_docker_assets_static_contract_accepts_default_files() -> None:
    findings = docker_runtime._validate_app_docker_assets(
        repo_root=REPO_ROOT,
        dockerfile=Path("infra/docker/Dockerfile.app"),
        entrypoint=Path("infra/docker/entrypoint.sh"),
        dockerignore=Path(".dockerignore"),
    )

    assert findings == []


def test_app_dockerfile_static_contract_rejects_slurm_or_munge_install(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.app"
    dockerfile.write_text(
        """
FROM python:3.12-slim-bookworm
COPY pyproject.toml uv.lock apps/frontend/package.json apps/frontend/pnpm-lock.yaml ./
RUN apt-get update && apt-get install -y slurm-client munge
COPY apps/frontend/dist apps/frontend/dist
COPY infra/docker/entrypoint.sh infra/docker/entrypoint.sh
ENTRYPOINT ["infra/docker/entrypoint.sh"]
""",
        encoding="utf-8",
    )

    findings = docker_runtime._validate_app_docker_assets(
        repo_root=REPO_ROOT,
        dockerfile=dockerfile,
        entrypoint=Path("infra/docker/entrypoint.sh"),
        dockerignore=Path(".dockerignore"),
    )

    assert "APP_DOCKERFILE_FORBIDDEN_SLURM_MUNGE_INSTALL" in {finding.code for finding in findings}


def test_app_dockerignore_static_contract_requires_credential_patterns(tmp_path: Path) -> None:
    dockerignore = tmp_path / ".dockerignore"
    required_credential_patterns = {
        ".npmrc",
        ".pypirc",
        ".netrc",
        "pip.conf",
        "id_rsa",
        "id_rsa*",
        "id_ed25519",
        "id_ed25519*",
        "**/.env",
        "**/.env.*",
        "**/.npmrc",
        "**/.pypirc",
        "**/.netrc",
        "**/pip.conf",
        "**/id_rsa",
        "**/id_rsa*",
        "**/id_ed25519",
        "**/id_ed25519*",
        "**/*.pem",
        "**/*.key",
        "**/.aws",
        "**/.ssh",
        "**/secrets",
    }
    dockerignore.write_text(
        "\n".join(sorted(docker_runtime.REQUIRED_DOCKERIGNORE_PATTERNS - required_credential_patterns))
        + "\n",
        encoding="utf-8",
    )

    findings = docker_runtime._validate_app_docker_assets(
        repo_root=REPO_ROOT,
        dockerfile=Path("infra/docker/Dockerfile.app"),
        entrypoint=Path("infra/docker/entrypoint.sh"),
        dockerignore=dockerignore,
    )

    missing = {
        finding.details["required"]
        for finding in findings
        if finding.code == "APP_DOCKERIGNORE_PATTERN_MISSING"
    }
    assert required_credential_patterns <= missing


def test_entrypoint_requires_service_role_under_require_flag() -> None:
    completed = _run_entrypoint(["true"], {"NHMS_REQUIRE_SERVICE_ROLE": "true"})

    assert completed.returncode != 0
    assert "SERVICE_ROLE_REQUIRED" in completed.stderr


def test_entrypoint_allows_unset_require_service_role_for_local_default() -> None:
    completed = _run_entrypoint(["true"], {})

    assert completed.returncode == 0
    assert "nhms-entrypoint[" not in completed.stderr


@pytest.mark.parametrize("value", ["", "   "])
def test_entrypoint_rejects_explicit_empty_require_service_role_flag(value: str) -> None:
    completed = _run_entrypoint(["true"], {"NHMS_REQUIRE_SERVICE_ROLE": value})

    assert completed.returncode != 0
    assert "SERVICE_ROLE_REQUIRE_FLAG_INVALID" in completed.stderr


def test_entrypoint_allows_whitespace_padded_false_require_service_role_flag() -> None:
    completed = _run_entrypoint(["true"], {"NHMS_REQUIRE_SERVICE_ROLE": " false "})

    assert completed.returncode == 0
    assert "nhms-entrypoint[" not in completed.stderr


@pytest.mark.parametrize(
    "env",
    [
        {"NHMS_AUTH_MODE": "production "},
        {"AUTH_BACKEND": "oidc "},
        {"NHMS_REQUIRE_SERVICE_ROLE": " true "},
    ],
)
def test_entrypoint_trims_production_like_role_gate_env_before_requiring_role(
    env: dict[str, str],
) -> None:
    completed = _run_entrypoint(["true"], env)

    assert completed.returncode != 0
    assert "SERVICE_ROLE_REQUIRED" in completed.stderr


def test_entrypoint_rejects_invalid_require_service_role_flag() -> None:
    completed = _run_entrypoint(["true"], {"NHMS_REQUIRE_SERVICE_ROLE": "ture"})

    assert completed.returncode != 0
    assert "SERVICE_ROLE_REQUIRE_FLAG_INVALID" in completed.stderr


def test_entrypoint_rejects_unsupported_service_role() -> None:
    completed = _run_entrypoint(["true"], {"NHMS_SERVICE_ROLE": "control"})

    assert completed.returncode != 0
    assert "SERVICE_ROLE_UNSUPPORTED" in completed.stderr


def test_entrypoint_rejects_reserved_slurm_gateway_role() -> None:
    completed = _run_entrypoint(
        ["true"],
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "slurm_gateway"},
    )

    assert completed.returncode != 0
    assert "SERVICE_ROLE_RESERVED" in completed.stderr


@pytest.mark.parametrize(
    ("env_key", "value"),
    [
        ("SLURM_GATEWAY_URL", "http://node22.internal:8081"),
        ("SLURM_GATEWAY_BACKEND", "slurm"),
        ("SLURM_GATEWAY_BACKEND", "mock"),
        ("WORKSPACE_ROOT", "/workspace"),
        ("RUN_WORKSPACE_ROOT", "/workspace/runs"),
        ("SHARED_LOG_ROOT", "/workspace/logs"),
        ("NHMS_OBJECT_STORE_COPYBACK_ROOT", "/ghdc/data/nwm/object-store"),
        ("NHMS_SCHEDULER_LOCK_ROOT", "/workspace/scheduler/locks"),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", "/workspace/scheduler/evidence"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "/workspace/runtime"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "/workspace/tmp"),
        ("NHMS_BASINS_ROOT", "/data/Basins"),
        ("NHMS_MODEL_ASSET_ROOT", "/data/model-assets"),
        ("SLURM_GATEWAY_TEMPLATE_DIR", "/app/infra/sbatch"),
        ("SLURM_GATEWAY_WORKSPACE_DIR", "/workspace/slurm"),
        ("MUNGE_SOCKET", "/run/munge/munge.socket.2"),
        ("MUNGE_KEY", "/etc/munge/munge.key"),
        ("SHUD_EXECUTABLE", "/opt/shud/bin/shud"),
        ("DOCKER_HOST", "unix:///var/run/docker.sock"),
    ],
)
def test_entrypoint_rejects_display_forbidden_env_contract(env_key: str, value: str) -> None:
    completed = _run_entrypoint(
        ["true"],
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            env_key: value,
        },
    )

    assert completed.returncode != 0
    assert "DISPLAY_BOUNDARY_CONFIG_UNSAFE" in completed.stderr


def test_public_display_forbidden_env_docs_align_with_validator_and_entrypoint() -> None:
    entrypoint_text = (REPO_ROOT / "infra/docker/entrypoint.sh").read_text(encoding="utf-8")
    entrypoint_match = re.search(
        r"readonly DISPLAY_FORBIDDEN_PRESENT_ENVS=\(\n(?P<body>.*?)\n\)",
        entrypoint_text,
        flags=re.DOTALL,
    )
    assert entrypoint_match is not None
    entrypoint_keys = {
        line.strip()
        for line in entrypoint_match.group("body").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert entrypoint_keys == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS

    infra_readme_text = (REPO_ROOT / "infra/README.two-node-docker.md").read_text(encoding="utf-8")
    forbidden_block_match = re.search(
        r"display forbidden set 保持一致：\n\n```text\n(?P<body>.*?)\n```",
        infra_readme_text,
        flags=re.DOTALL,
    )
    assert forbidden_block_match is not None
    infra_readme_keys = {
        line.strip()
        for line in forbidden_block_match.group("body").splitlines()
        if line.strip() in docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS
    }
    assert infra_readme_keys == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS

    env_readme_text = (REPO_ROOT / "infra/env/README.md").read_text(encoding="utf-8")
    env_readme_section = env_readme_text.split("- Forbidden env keys must match", 1)[1].split(
        "- Forbidden container/host surfaces:",
        1,
    )[0]
    env_readme_keys = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", env_readme_section))
    assert env_readme_keys == docker_runtime.DISPLAY_FORBIDDEN_ENV_KEYS
    assert "NHMS_OBJECT_STORE_COPYBACK_ROOT" in infra_readme_keys
    assert "NHMS_OBJECT_STORE_COPYBACK_ROOT" in env_readme_keys


def test_entrypoint_trims_display_readonly_role_before_boundary_validation() -> None:
    completed = _run_entrypoint(
        ["true"],
        {
            "NHMS_REQUIRE_SERVICE_ROLE": " true ",
            "NHMS_SERVICE_ROLE": " display_readonly ",
            "WORKSPACE_ROOT": "/workspace",
        },
    )

    assert completed.returncode != 0
    assert "DISPLAY_BOUNDARY_CONFIG_UNSAFE" in completed.stderr


def test_entrypoint_rejects_display_forbidden_env_even_when_value_is_empty() -> None:
    for env_key in (
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "WORKSPACE_ROOT",
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
    ):
        completed = _run_entrypoint(
            ["true"],
            {
                "NHMS_REQUIRE_SERVICE_ROLE": "true",
                "NHMS_SERVICE_ROLE": "display_readonly",
                env_key: "",
            },
        )

        assert completed.returncode != 0
        assert "DISPLAY_BOUNDARY_CONFIG_UNSAFE" in completed.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["uv", "run", "nhms-pipeline", "plan-production", "--plan"],
        ["uv", "run", "python", "-m", "services.orchestrator.cli", "plan-production", "--plan"],
        ["uv", "run", "nhms-gfs", "--help"],
        ["uv", "run", "nhms-era5", "--help"],
        ["uv", "run", "nhms-ifs", "--help"],
        ["uv", "run", "nhms-forcing", "--help"],
        ["uv", "run", "nhms-shud-runtime", "--help"],
        ["uv", "run", "nhms-production", "--help"],
        ["uv", "run", "nhms-state", "--help"],
        ["uv", "run", "nhms-flood", "--help"],
        ["uv", "run", "nhms-model", "--help"],
        ["uv", "run", "nhms-canonical", "--help"],
        ["uv", "run", "nhms-parse", "--help"],
        ["uv", "run", "nhms-pipeline", "plan-production", "--source", "gfs"],
        ["sbatch", "--version"],
        ["/usr/bin/squeue"],
        ["scontrol", "show", "config"],
    ],
)
def test_entrypoint_rejects_display_compute_commands(command: list[str]) -> None:
    completed = _run_entrypoint(
        command,
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly"},
    )

    assert completed.returncode != 0
    assert "DISPLAY_COMMAND_FORBIDDEN" in completed.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["sh", "-c", "uv run nhms-pipeline plan-production --plan"],
        ["bash", "-lc", "sbatch --version"],
        ["bash", "-lc", "uv run nhms'-'pipeline plan-production --plan"],
    ],
)
def test_entrypoint_rejects_display_shell_wrapped_compute_commands(command: list[str]) -> None:
    completed = _run_entrypoint(
        command,
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly"},
    )

    assert completed.returncode != 0
    assert "DISPLAY_COMMAND_FORBIDDEN" in completed.stderr


def test_entrypoint_rejects_display_env_indirected_compute_command() -> None:
    completed = _run_entrypoint(
        ["bash", "-lc", "$NHMS_CMD"],
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "NHMS_CMD": "uv run nhms-pipeline plan-production --plan",
        },
    )

    assert completed.returncode != 0
    assert "DISPLAY_COMMAND_FORBIDDEN" in completed.stderr


def test_entrypoint_rejects_arbitrary_display_command_override() -> None:
    completed = _run_entrypoint(
        ["echo", "unsafe"],
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly"},
    )

    assert completed.returncode != 0
    assert "DISPLAY_COMMAND_FORBIDDEN" in completed.stderr


def test_entrypoint_allows_safe_explicit_display_command() -> None:
    completed = _run_entrypoint(
        ["true"],
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly"},
    )

    assert completed.returncode == 0
    assert "nhms-entrypoint[" not in completed.stderr


def test_docker_smoke_records_blocked_and_replaces_stale_pass_when_preflight_blocks(tmp_path: Path) -> None:
    evidence_root = tmp_path / "artifacts" / "docker-smoke"
    evidence_root.mkdir(parents=True)
    evidence_path = evidence_root / "docker-smoke.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.two_node_docker.app_smoke.v1",
                "change_id": docker_runtime.CHANGE_ID,
                "checked_at": "2000-01-01T00:00:00Z",
                "status": "PASS",
                "blockers": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_docker_smoke(
        evidence_root=evidence_root,
        repo_root=tmp_path,
        min_free_bytes=1024,
        command_runner=_docker_unavailable_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "BLOCKED"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["checked_at"] != "2000-01-01T00:00:00Z"
    assert {blocker["code"] for blocker in payload["blockers"]} == {"DOCKER_PREFLIGHT_BLOCKED"}


def test_docker_smoke_records_fail_and_replaces_stale_pass_when_build_fails(tmp_path: Path) -> None:
    evidence_root = tmp_path / "artifacts" / "docker-smoke"
    evidence_root.mkdir(parents=True)
    evidence_path = evidence_root / "docker-smoke.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.two_node_docker.app_smoke.v1",
                "change_id": docker_runtime.CHANGE_ID,
                "checked_at": "2000-01-01T00:00:00Z",
                "status": "PASS",
                "blockers": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = docker_runtime.run_docker_smoke(
        evidence_root=evidence_root,
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_build_fails_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "FAIL"
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["checked_at"] != "2000-01-01T00:00:00Z"
    assert {blocker["code"] for blocker in payload["blockers"]} == {"DOCKER_BUILD_FAILED"}


def test_docker_smoke_records_blocked_when_build_is_network_blocked(tmp_path: Path) -> None:
    result = docker_runtime.run_docker_smoke(
        evidence_root=tmp_path / "artifacts" / "docker-smoke",
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_build_network_blocked_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "BLOCKED"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert {blocker["code"] for blocker in payload["blockers"]} == {"DOCKER_BUILD_BLOCKED"}


def test_docker_smoke_passes_with_expected_role_boundary_probe_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "artifacts" / "tmp"))

    result = docker_runtime.run_docker_smoke(
        evidence_root=tmp_path / "artifacts" / "docker-smoke",
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_smoke_success_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "PASS"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert set(payload["commands"]) >= {
        "docker_build",
        "image_inspect",
        "image_absence_probe",
        "display_compute_env_reject",
        "slurm_gateway_reject",
        "compute_scheduler_command",
        "display_scheduler_reject",
        "display_startup_start",
        "display_startup_probe",
        "display_startup_logs",
        "display_startup_cleanup",
    }
    assert payload["commands"]["image_inspect"]["returncode"] == 0
    assert payload["blockers"] == []
    start_args = payload["commands"]["display_startup_start"]["args"]
    object_store_env = next(arg for arg in start_args if arg.startswith("OBJECT_STORE_ROOT="))
    object_store_root = object_store_env.split("=", 1)[1]
    assert object_store_root
    assert "-e" in start_args
    assert object_store_env in start_args
    assert "-v" in start_args
    assert f"{object_store_root}:{object_store_root}:ro" in start_args


def test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight(tmp_path: Path) -> None:
    evidence_root = Path("/scratch/frd_muziyao/nwm-test/run-smoke-explicit/docker-security")

    result = docker_runtime.run_docker_smoke(
        evidence_root=evidence_root,
        repo_root=tmp_path,
        evidence_run_id="run-smoke-explicit",
        min_free_bytes=100,
        command_runner=_docker_smoke_success_runner,
        disk_usage_provider=_high_space,
    )

    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    preflight = json.loads((evidence_root / "preflight" / "docker-preflight.json").read_text(encoding="utf-8"))
    assert result.status == "PASS"
    assert payload["evidence_run_id"] == "run-smoke-explicit"
    assert preflight["evidence_run_id"] == "run-smoke-explicit"
    shutil.rmtree(evidence_root.parent)


@pytest.mark.parametrize(
    ("probe_name", "expected_code"),
    [
        ("image_absence_probe", "APP_IMAGE_FORBIDDEN_CAPABILITY_PRESENT"),
        ("compute_scheduler_command", "COMPUTE_SCHEDULER_HELP_FAILED"),
        ("display_startup_start", "DISPLAY_STARTUP_FAILED"),
        ("display_startup_probe", "DISPLAY_STARTUP_PROBE_FAILED"),
    ],
)
def test_docker_smoke_required_probe_failure_never_passes(
    tmp_path: Path,
    probe_name: str,
    expected_code: str,
) -> None:
    result = docker_runtime.run_docker_smoke(
        evidence_root=tmp_path / "artifacts" / "docker-smoke",
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_smoke_probe_failure_runner(probe_name),
        disk_usage_provider=_high_space,
    )

    assert result.status == "FAIL"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert expected_code in {blocker["code"] for blocker in payload["blockers"]}


def test_docker_smoke_image_inspect_failure_never_passes(tmp_path: Path) -> None:
    result = docker_runtime.run_docker_smoke(
        evidence_root=tmp_path / "artifacts" / "docker-smoke",
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_image_inspect_failure_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "FAIL"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["commands"]["docker_build"]["returncode"] == 0
    assert payload["commands"]["image_inspect"]["returncode"] != 0
    assert {blocker["code"] for blocker in payload["blockers"]} == {"IMAGE_INSPECT_FAILED"}


def test_docker_smoke_display_startup_cleanup_failure_never_passes(tmp_path: Path) -> None:
    result = docker_runtime.run_docker_smoke(
        evidence_root=tmp_path / "artifacts" / "docker-smoke",
        repo_root=tmp_path,
        min_free_bytes=100,
        command_runner=_docker_smoke_cleanup_failure_runner,
        disk_usage_provider=_high_space,
    )

    assert result.status == "FAIL"
    payload = json.loads(result.evidence_path.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["commands"]["display_startup_start"]["returncode"] == 0
    assert payload["commands"]["display_startup_probe"]["returncode"] == 0
    assert "DISPLAY_STARTUP_CLEANUP_FAILED" in {blocker["code"] for blocker in payload["blockers"]}


def test_docker_smoke_required_probe_missing_never_passes() -> None:
    commands = {
        "image_inspect": docker_runtime.CommandResult(("inspect",), 0, "", ""),
        "image_absence_probe": docker_runtime.CommandResult(("probe",), 0, "", ""),
        "display_startup_start": docker_runtime.CommandResult(("start",), 0, "", ""),
        "display_startup_probe": docker_runtime.CommandResult(("display",), 0, "", ""),
        "display_compute_env_reject": docker_runtime.CommandResult(
            ("display-env",), 64, "", "nhms-entrypoint[DISPLAY_BOUNDARY_CONFIG_UNSAFE]"
        ),
        "slurm_gateway_reject": docker_runtime.CommandResult(
            ("gateway",), 64, "", "nhms-entrypoint[SERVICE_ROLE_RESERVED]"
        ),
        "display_scheduler_reject": docker_runtime.CommandResult(
            ("scheduler",), 64, "", "nhms-entrypoint[DISPLAY_COMMAND_FORBIDDEN]"
        ),
    }

    blockers = docker_runtime._docker_smoke_command_blockers(commands)

    blocker_codes = {blocker["code"] for blocker in blockers}
    assert "COMPUTE_SCHEDULER_HELP_FAILED_MISSING" in blocker_codes
    assert "DISPLAY_STARTUP_CLEANUP_MISSING" in blocker_codes


def test_docker_smoke_image_inspect_missing_never_passes() -> None:
    blockers = docker_runtime._docker_smoke_command_blockers({})

    assert {blocker["code"] for blocker in blockers} == {"IMAGE_INSPECT_MISSING"}
    assert docker_runtime._docker_smoke_status(blockers) == "FAIL"


def test_docker_security_summary_aggregates_source_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    child_run_id = "run-123"
    _write_json(
        source_trust,
        _source_trust_payload(child_run_id, security_root, roles=("compute",)),
    )
    _write_json(
        source_trust_display,
        _source_trust_payload(child_run_id, security_root, roles=("display",)),
    )
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": child_run_id,
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(
        smoke_report,
        _smoke_payload(child_run_id, dockerfile=str(repo_root / "infra/docker/Dockerfile.app")),
    )

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=child_run_id,
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["evidence_run_id"] == child_run_id
    assert payload["runtime_config"] == {
        "display_readonly": True,
        "service_role": "display_readonly",
        "slurm_routes_enabled": False,
    }
    assert payload["live_docker_evidence"] is True
    assert payload["proofs"] == {
        "live_container_checked": True,
        "smoke_passed": True,
        "source_trust_passed": True,
        "source_trust_roles": ["compute", "display"],
        "static_passed": True,
    }
    assert payload["source_artifacts"]["static"]["sha256"] == sha256(static_report.read_bytes()).hexdigest()
    assert isinstance(payload["source_artifacts"]["source_trust"], list)


def test_docker_security_summary_blocks_single_display_source_trust_without_compute_role(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    run_id = "run-123"
    _write_json(source_trust_display, _source_trust_payload(run_id, security_root, roles=("display",)))
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": run_id,
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload(run_id))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=run_id,
        source_trust_report=source_trust_display,
        static_report=static_report,
        smoke_report=smoke_report,
    )

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    blocker = next(item for item in summary["blockers"] if item["source"] == "source_trust")
    assert blocker["source_blockers"][0]["code"] == "DOCKER_SECURITY_SOURCE_TRUST_ROLE_ENV_PROOF_MISSING"
    assert blocker["source_blockers"][0]["role"] == "compute"


def test_docker_security_summary_rejects_blocked_source_trust_after_failed_publication(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    run_id = "run-123"
    blocked_display = _source_trust_payload(run_id, security_root, roles=("display",))
    blocked_display["status"] = "BLOCKED"
    blocked_display["blockers"] = [
        {
            "code": "SOURCE_TRUST_PUBLICATION_FAILED",
            "message": "source-trust evidence publication failed; previous PASS JSON was invalidated.",
        }
    ]
    _write_json(source_trust, _source_trust_payload(run_id, security_root, roles=("compute",)))
    _write_json(source_trust_display, blocked_display)
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": run_id,
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload(run_id))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=run_id,
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert summary["source_statuses"]["source_trust"] == "BLOCKED"
    blocker = next(item for item in summary["blockers"] if item["source"] == "source_trust")
    assert any(
        item.get("code") == "SOURCE_TRUST_PUBLICATION_FAILED"
        for item in blocker["source_blockers"]
    )
    assert summary["proofs"]["source_trust_passed"] is False


def test_docker_security_summary_blocks_unsafe_child_before_hash(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    security_root.mkdir(parents=True)
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    target = tmp_path / "outside-static.json"
    target.write_text("{bad-json", encoding="utf-8")
    _write_json(
        source_trust,
        _source_trust_payload("run-123", security_root, roles=("compute",)),
    )
    _write_json(
        source_trust_display,
        _source_trust_payload("run-123", security_root, roles=("display",)),
    )
    static_report.symlink_to(target)
    _write_json(
        smoke_report,
        _smoke_payload("run-123"),
    )

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id="run-123",
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    static_artifact = payload["source_artifacts"]["static"]
    assert static_artifact["sha256"] is None
    assert static_artifact["blocked"] is True
    blockers = payload["blockers"][0]["source_blockers"]
    assert blockers[0]["code"] == "DOCKER_SECURITY_SOURCE_SYMLINK"


def test_docker_security_summary_blocks_child_outside_approved_roots(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    security_root = repo_root / "artifacts" / "docker-security"
    security_root.mkdir(parents=True)
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = tmp_path / "outside-static.json"
    smoke_report = security_root / "docker-smoke.json"
    _write_json(
        source_trust,
        _source_trust_payload("run-123", security_root, roles=("compute",)),
    )
    _write_json(
        source_trust_display,
        _source_trust_payload("run-123", security_root, roles=("display",)),
    )
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": "run-123",
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload("run-123"))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id="run-123",
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["source_artifacts"]["static"]["sha256"] is None
    blockers = payload["blockers"][0]["source_blockers"]
    assert blockers[0]["code"] == "DOCKER_SECURITY_SOURCE_OUTSIDE_APPROVED_ROOT"


def test_docker_security_summary_blocks_oversized_child_before_digest(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    security_root.mkdir(parents=True)
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    _write_json(
        source_trust,
        _source_trust_payload("run-123", security_root, roles=("compute",)),
    )
    _write_json(
        source_trust_display,
        _source_trust_payload("run-123", security_root, roles=("display",)),
    )
    static_report.write_text("x" * (docker_runtime.MAX_SECURITY_CHILD_BYTES + 1), encoding="utf-8")
    _write_json(smoke_report, _smoke_payload("run-123"))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id="run-123",
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["source_artifacts"]["static"]["sha256"] is None
    blockers = payload["blockers"][0]["source_blockers"]
    assert blockers[0]["code"] == "DOCKER_SECURITY_SOURCE_TOO_LARGE"


def test_docker_security_summary_blocks_child_without_current_run_id(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    security_root.mkdir(parents=True)
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    _write_json(
        source_trust,
        {"schema": "nhms.two_node_docker.source_trust.v1", "status": "PASS", "blockers": []},
    )
    _write_json(source_trust_display, _source_trust_payload("run-123", security_root, roles=("display",)))
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": "run-123",
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload("run-123"))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id="run-123",
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["source_statuses"]["source_trust"] == "BLOCKED"
    assert payload["blockers"][0]["source_blockers"][0]["code"] == "DOCKER_SECURITY_SOURCE_RUN_ID_MISSING"


def test_docker_security_summary_blocks_source_trust_without_checked_paths(tmp_path: Path) -> None:
    repo_root = tmp_path
    security_root = repo_root / "artifacts" / "docker-security"
    security_root.mkdir(parents=True)
    source_trust = security_root / "two-node-docker-source-trust-compute.json"
    source_trust_display = security_root / "two-node-docker-source-trust-display.json"
    static_report = security_root / "static-compose-env-check.json"
    smoke_report = security_root / "docker-smoke.json"
    run_id = "run-123"
    payload = _source_trust_payload(run_id, security_root)
    payload.pop("checked_paths")
    _write_json(source_trust, payload)
    _write_json(source_trust_display, _source_trust_payload(run_id, security_root, roles=("display",)))
    _write_json(
        static_report,
        {
            "schema_version": "nhms.two_node_docker.static_check.v1",
            "status": "PASS",
            "evidence_run_id": run_id,
            "findings": [],
            **_static_proof_payload(),
        },
    )
    _write_json(smoke_report, _smoke_payload(run_id))

    output = docker_runtime.write_docker_security_summary(
        output=security_root / "summary.json",
        repo_root=repo_root,
        evidence_run_id=run_id,
        source_trust_report=[source_trust, source_trust_display],
        static_report=static_report,
        smoke_report=smoke_report,
    )

    summary = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == "BLOCKED"
    assert summary["source_statuses"]["source_trust"] == "BLOCKED"
    blocker = next(item for item in summary["blockers"] if item["source"] == "source_trust")
    assert blocker["source_blockers"][0]["code"] == "DOCKER_SECURITY_SOURCE_TRUST_CHECKED_PATHS_MISSING"


def test_image_absence_probe_rejects_scontrol_only_slurm_cli(tmp_path: Path) -> None:
    scontrol = tmp_path / "scontrol"
    scontrol.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    scontrol.chmod(0o755)

    completed = subprocess.run(
        ["/bin/sh", "-c", docker_runtime._image_absence_probe_script()],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path)},
    )

    assert completed.returncode == 1
    assert completed.stderr == "forbidden binary present: scontrol\n"


def test_command_result_bounds_stdout_and_stderr_in_evidence_payload() -> None:
    result = docker_runtime.CommandResult(
        ("docker", "logs", "container"),
        1,
        "o" * (docker_runtime.MAX_COMMAND_OUTPUT_BYTES + 100),
        "e" * (docker_runtime.MAX_COMMAND_OUTPUT_BYTES + 200),
    )

    payload = result.to_dict()

    assert len(payload["stdout"].encode()) <= docker_runtime.MAX_COMMAND_OUTPUT_BYTES
    assert len(payload["stderr"].encode()) <= docker_runtime.MAX_COMMAND_OUTPUT_BYTES
    assert payload["output_truncation"]["stdout"]["truncated"] is True
    assert payload["output_truncation"]["stdout"]["original_bytes"] == docker_runtime.MAX_COMMAND_OUTPUT_BYTES + 100
    assert payload["output_truncation"]["stderr"]["truncated"] is True
    assert payload["output_truncation"]["stderr"]["original_bytes"] == docker_runtime.MAX_COMMAND_OUTPUT_BYTES + 200


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


def _docker_build_fails_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "build", "-f"):
        return docker_runtime.CommandResult(command, 1, "", "build failed")
    return _docker_available_runner(args)


def _docker_build_network_blocked_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "build", "-f"):
        return docker_runtime.CommandResult(
            command,
            1,
            "",
            "Get \"https://registry-1.docker.io/v2/\": net/http: request canceled while waiting for connection "
            "(Client.Timeout exceeded while awaiting headers)",
        )
    return _docker_available_runner(args)


def _docker_smoke_success_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "run", "--rm"):
        if "NHMS_SERVICE_ROLE=display_readonly" in command and "WORKSPACE_ROOT=/workspace" in command:
            return docker_runtime.CommandResult(command, 64, "", "nhms-entrypoint[DISPLAY_BOUNDARY_CONFIG_UNSAFE]\n")
        if "NHMS_SERVICE_ROLE=slurm_gateway" in command:
            return docker_runtime.CommandResult(command, 64, "", "nhms-entrypoint[SERVICE_ROLE_RESERVED]\n")
        if "NHMS_SERVICE_ROLE=display_readonly" in command and list(command[-5:]) == [
            "uv",
            "run",
            "nhms-pipeline",
            "plan-production",
            "--plan",
        ]:
            return docker_runtime.CommandResult(command, 64, "", "nhms-entrypoint[DISPLAY_COMMAND_FORBIDDEN]\n")
    if command[:2] == ("docker", "exec"):
        return docker_runtime.CommandResult(command, 0, "display startup probe ok\n", "")
    return _docker_available_runner(args)


def _docker_smoke_probe_failure_runner(
    probe_name: str,
) -> Any:
    def runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
        command = tuple(args)
        if probe_name == "image_absence_probe" and command[:4] == ("docker", "run", "--rm", "--entrypoint"):
            return docker_runtime.CommandResult(command, 1, "", "forbidden binary present: sbatch\n")
        if probe_name == "compute_scheduler_command" and command[:3] == ("docker", "run", "--rm") and list(
            command[-5:]
        ) == ["uv", "run", "nhms-pipeline", "plan-production", "--help"]:
            return docker_runtime.CommandResult(command, 1, "", "help failed\n")
        if probe_name == "display_startup_start" and command[:4] == ("docker", "run", "--rm", "-d"):
            return docker_runtime.CommandResult(command, 1, "", "container did not start\n")
        if probe_name == "display_startup_probe" and command[:2] == ("docker", "exec"):
            return docker_runtime.CommandResult(command, 1, "", "runtime config did not report display_readonly\n")
        return _docker_smoke_success_runner(args)

    return runner


def _docker_smoke_cleanup_failure_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "rm", "-f"):
        return docker_runtime.CommandResult(command, 1, "", "cleanup failed\n")
    return _docker_smoke_success_runner(args)


def _docker_image_inspect_failure_runner(args: list[str] | tuple[str, ...]) -> docker_runtime.CommandResult:
    command = tuple(args)
    if command[:3] == ("docker", "image", "inspect"):
        return docker_runtime.CommandResult(command, 1, "", "No such image\n")
    return _docker_smoke_success_runner(args)


def _run_entrypoint(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if not (
            key.startswith("NHMS_")
            or key
            in {
                "AUTH_BACKEND",
                "SLURM_GATEWAY_URL",
                "SLURM_GATEWAY_BACKEND",
                "WORKSPACE_ROOT",
                "RUN_WORKSPACE_ROOT",
                "SHARED_LOG_ROOT",
                "OBJECT_STORE_ROOT",
                "NHMS_OBJECT_STORE_COPYBACK_ROOT",
                "MUNGE_SOCKET",
                "MUNGE_KEY",
                "SHUD_EXECUTABLE",
                "DOCKER_HOST",
            }
        )
    }
    clean_env.update(env)
    return subprocess.run(
        [str(REPO_ROOT / "infra/docker/entrypoint.sh"), *command],
        cwd=REPO_ROOT,
        env=clean_env,
        check=False,
        capture_output=True,
        text=True,
    )


def _source_trust_payload(
    run_id: str,
    root: Path,
    *,
    roles: tuple[str, ...] = ("compute", "display"),
) -> dict[str, Any]:
    labels = {
        "trust path component": "directory",
        "checkout root": "directory",
        "infra directory": "directory",
        "compute compose source": "file",
        "display compose source": "file",
        "env source directory": "directory",
        "systemd source directory": "directory",
        "compute systemd unit source": "file",
        "display systemd unit source": "file",
        "compute role env": "file",
        "display role env": "file",
    }
    checked_paths = []
    for label, expected_kind in labels.items():
        if label.endswith("role env") and label.split()[0] not in roles:
            continue
        is_directory = expected_kind == "directory"
        checked_paths.append(
            {
                "label": label,
                "path": str(root / label.replace(" ", "-")),
                "expected_kind": expected_kind,
                "exists": True,
                "trusted_owner": True,
                "is_symlink": False,
                "is_directory": is_directory,
                "is_regular": not is_directory,
                "group_writable": False,
                "world_writable": False,
                "mode": "0600" if label.endswith("role env") else ("0755" if is_directory else "0644"),
            }
        )
    return {
        "schema": "nhms.two_node_docker.source_trust.v1",
        "status": "PASS",
        "evidence_run_id": run_id,
        "roles": list(roles),
        "checked_paths": checked_paths,
        "blockers": [],
    }


def _static_proof_payload() -> dict[str, bool]:
    return {
        **{key: False for key in docker_runtime.DOCKER_REQUIRED_FALSE_PROOFS},
        **{key: True for key in docker_runtime.DOCKER_REQUIRED_TRUE_PROOFS},
    }


def _smoke_payload(run_id: str, *, dockerfile: str = "infra/docker/Dockerfile.app") -> dict[str, Any]:
    return {
        "schema_version": "nhms.two_node_docker.app_smoke.v1",
        "status": "PASS",
        "evidence_run_id": run_id,
        "image_tag": "nhms-app:test",
        "dockerfile": dockerfile,
        "commands": {
            "image_absence_probe": {"returncode": 0},
            "display_startup_start": {"returncode": 0},
            "display_startup_probe": {"returncode": 0},
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _high_space(path: Path) -> docker_runtime.DiskSpace:
    return docker_runtime.DiskSpace(total=10_000, used=1_000, free=9_000)


def _low_space(path: Path) -> docker_runtime.DiskSpace:
    return docker_runtime.DiskSpace(total=10_000, used=9_999, free=1)
