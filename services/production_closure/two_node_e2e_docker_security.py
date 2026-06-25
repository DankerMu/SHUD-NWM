from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"

DOCKER_SECURITY_SUMMARY_SCHEMA = "nhms.two_node_docker.security_summary.v1"
DOCKER_SECURITY_CHILD_SCHEMAS: Mapping[str, str] = {
    "source_trust": "nhms.two_node_docker.source_trust.v1",
    "static": "nhms.two_node_docker.static_check.v1",
    "smoke": "nhms.two_node_docker.app_smoke.v1",
}
DOCKER_SOURCE_TRUST_COMMON_REQUIRED_LABELS = frozenset(
    {
        "trust path component",
        "checkout root",
        "infra directory",
        "compute compose source",
        "display compose source",
        "env source directory",
        "systemd source directory",
        "compute systemd unit source",
        "display systemd unit source",
    }
)
DOCKER_SOURCE_TRUST_ROLE_LABELS: Mapping[str, str] = {
    "compute": "compute role env",
    "display": "display role env",
}
DOCKER_SECURITY_DOCUMENT_CANDIDATES = (
    "docker-security/summary.json",
    "docker-security/display-isolation.json",
    "docker-security/docker-smoke.json",
    "docker-smoke/docker-smoke.json",
    "docker-smoke.json",
)
DOCKER_SECURITY_LANE_OWNER = "services.production_closure.two_node_e2e_docker_security"
DOCKER_SECURITY_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_security or docker_display"'
)
DOCKER_SECURITY_LANE_GUARD_SYMBOLS = (
    "DOCKER_SECURITY_DOCUMENT_CANDIDATES",
    "DOCKER_SECURITY_SUMMARY_SCHEMA",
    "DOCKER_SECURITY_CHILD_SCHEMAS",
    "DOCKER_REQUIRED_FALSE_PROOFS",
    "DOCKER_REQUIRED_TRUE_PROOFS",
    "DOCKER_FORBIDDEN_BOOL_KEYS",
    "DOCKER_FORBIDDEN_FINDING_TOKENS",
    "DOCKER_SOURCE_TRUST_COMMON_REQUIRED_LABELS",
    "DOCKER_SOURCE_TRUST_ROLE_LABELS",
    "DockerSecurityEvaluationHelpers",
    "evaluate_docker_security",
    "docker_display_security_proofs",
    "_docker_security_summary_contract_issues",
    "_docker_security_child_artifact_issues",
    "_docker_display_security_proofs",
    "_raw_docker_security_analysis",
)
DOCKER_SECURITY_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_DOCKER_SECURITY_",
    "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_",
    "TWO_NODE_E2E_DOCKER_STATIC_",
    "TWO_NODE_E2E_DOCKER_SMOKE_",
    "TWO_NODE_E2E_DOCKER_DISPLAY_",
    "TWO_NODE_E2E_DISPLAY_",
)

LaneResultT = TypeVar("LaneResultT")
LaneResultT_co = TypeVar("LaneResultT_co", covariant=True)


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class BlockerFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class FindingFactory(Protocol):
    def __call__(self, code: str, message: str, **details: Any) -> dict[str, Any]: ...


class MissingLaneAdapter(Protocol[LaneResultT_co]):
    def __call__(self, name: str, code: str) -> LaneResultT_co: ...


class LaneFromStatusAdapter(Protocol[LaneResultT_co]):
    def __call__(
        self,
        name: str,
        doc: EvidenceDocumentLike,
        *,
        status: str,
        summary_status: str,
        blockers: Sequence[Mapping[str, Any]] = (),
        findings: Sequence[Mapping[str, Any]] = (),
    ) -> LaneResultT_co: ...


class CurrentRunBlockers(Protocol):
    def __call__(
        self,
        payload: Mapping[str, Any],
        evidence_run_id: str,
        *,
        lane_name: str,
    ) -> list[dict[str, Any]]: ...


class ReadJson(Protocol):
    def __call__(self, path: Path, *, containment_root: Path) -> EvidenceDocumentLike: ...


@dataclass(frozen=True)
class DockerSecurityEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: Callable[[Any], str]
    blocker: BlockerFactory
    finding: FindingFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    has_live_docker_evidence: Callable[[Mapping[str, Any]], bool]
    runtime_config: Callable[[Mapping[str, Any]], dict[str, Any]]
    bool_lookup: Callable[[Mapping[str, Any], str], bool | None]
    bool_lookup_any: Callable[[Mapping[str, Any], Sequence[str]], bool | None]
    payload_findings: Callable[[Mapping[str, Any]], list[Mapping[str, Any]]]
    payload_blockers: Callable[[Mapping[str, Any]], list[Mapping[str, Any]]]
    first_mapping_value: Callable[[Mapping[str, Any], Sequence[str]], Any]
    approved_artifact_path: Callable[[str], Path]
    approved_artifact_containment_root: Callable[[Path], Path]
    path_is_relative_to: Callable[[Path, Path], bool]
    public_path: Callable[[Path], str]
    read_json: ReadJson
    explicit_bundle_run_ids_from_value: Callable[[Any], list[tuple[str, Any]]]
    evidence_error_type: type[Exception]


DOCKER_FORBIDDEN_BOOL_KEYS = (
    "slurm_route_available",
    "slurm_routes_enabled",
    "slurm_cli_present",
    "slurm_config_present",
    "slurm_socket_present",
    "munge_path_present",
    "docker_socket_present",
    "forbidden_hostconfig_hazard",
    "forbidden_mount_hazard",
    "forbidden_env_hazard",
    "writable_published_artifact_mount",
    "display_write_capability_present",
)
DOCKER_FORBIDDEN_FINDING_TOKENS = (
    "SLURM",
    "MUNGE",
    "DOCKER_SOCKET",
    "HOSTCONFIG",
    "FORBIDDEN_MOUNT",
    "FORBIDDEN_ENV",
    "WRITABLE",
)
DOCKER_REQUIRED_FALSE_PROOFS: Mapping[str, tuple[str, ...]] = {
    "slurm_routes_enabled": ("slurm_routes_enabled",),
    "slurm_route_available": ("slurm_route_available",),
    "slurm_cli_present": ("slurm_cli_present",),
    "slurm_config_present": ("slurm_config_present",),
    "slurm_socket_present": ("slurm_socket_present",),
    "munge_path_present": ("munge_path_present", "munge_socket_present"),
    "docker_socket_present": ("docker_socket_present", "docker_socket_mount_present"),
    "privileged": ("privileged", "hostconfig_privileged", "display_privileged"),
    "host_network": ("host_network", "host_network_mode", "network_mode_host"),
    "host_pid": ("host_pid", "host_pid_mode", "pid_mode_host"),
    "host_ipc": ("host_ipc", "host_ipc_mode", "ipc_mode_host"),
    "cap_add_present": ("cap_add_present", "capabilities_added", "linux_capabilities_added"),
    "forbidden_hostconfig_hazard": ("forbidden_hostconfig_hazard",),
    "forbidden_mount_hazard": ("forbidden_mount_hazard", "forbidden_mount_present"),
    "forbidden_env_hazard": ("forbidden_env_hazard", "forbidden_env_present"),
    "broad_host_bind_present": ("broad_host_bind_present", "broad_bind_present"),
    "private_workspace_bind_present": ("private_workspace_bind_present", "private_workspace_mount_present"),
    "workspace_mount_present": ("workspace_mount_present", "workspace_bind_present"),
    "writable_published_artifact_mount": (
        "writable_published_artifact_mount",
        "published_artifact_mount_writable",
    ),
    "display_write_capability_present": ("display_write_capability_present", "control_mutations_enabled"),
}
DOCKER_REQUIRED_TRUE_PROOFS: Mapping[str, tuple[str, ...]] = {
    "published_artifacts_readonly": ("published_artifacts_readonly", "published_artifact_mount_readonly"),
    "root_filesystem_readonly": ("root_filesystem_readonly", "readonly_root_filesystem", "read_only_root_filesystem"),
    "cap_drop_all": ("cap_drop_all", "all_capabilities_dropped"),
}
DOCKER_DISPLAY_FORBIDDEN_SCHEDULER_ROOT_ENV_KEYS = frozenset(
    {
        "NHMS_SCHEDULER_LOCK_ROOT",
        "NHMS_SCHEDULER_EVIDENCE_ROOT",
        "NHMS_SCHEDULER_RUNTIME_ROOT",
        "NHMS_SCHEDULER_TEMP_ROOT",
    }
)
DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "SLURM_GATEWAY_URL",
        "SLURM_GATEWAY_BACKEND",
        "SLURM_GATEWAY_TEMPLATE_DIR",
        "SLURM_GATEWAY_WORKSPACE_DIR",
        "WORKSPACE_ROOT",
        "RUN_WORKSPACE_ROOT",
        "SHARED_LOG_ROOT",
        "OBJECT_STORE_ROOT",
        *DOCKER_DISPLAY_FORBIDDEN_SCHEDULER_ROOT_ENV_KEYS,
        "NHMS_BASINS_ROOT",
        "NHMS_MODEL_ASSET_ROOT",
        "SHUD_EXECUTABLE",
        "MUNGE_SOCKET",
        "MUNGE_KEY",
        "DOCKER_HOST",
    }
)
DOCKER_FORBIDDEN_MOUNT_TOKENS = (
    "/etc/slurm",
    "/etc/munge",
    "/run/munge",
    "/var/run/munge",
    "munge.key",
    ".nhms-runs",
    "WORKSPACE_ROOT",
    "NHMS_BASINS_ROOT",
    "NHMS_MODEL_ASSET_ROOT",
    "MUNGE_SOCKET",
    "MUNGE_KEY",
)
DOCKER_BROAD_HOST_ROOTS = frozenset({"/", "/root", "/home", "/etc", "/run", "/var", "/scratch"})
def _with_context(item: Mapping[str, Any], **context: Any) -> dict[str, Any]:
    merged = dict(item)
    for key, value in context.items():
        merged.setdefault(key, value)
    return merged


def _raw_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return None


def _bool_lookup_values(value: Any, keys: frozenset[str]) -> list[bool]:
    values: list[bool] = []
    for key, nested in _walk_evidence_values(value):
        if key in keys:
            parsed = _raw_bool(nested)
            if parsed is not None:
                values.append(parsed)
    return values


def _walk_evidence_values(value: Any) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen = 0
    while stack and seen < 100_000:
        current, depth = stack.pop()
        seen += 1
        if depth > 256:
            continue
        if isinstance(current, Mapping):
            for key, nested in current.items():
                text_key = str(key)
                result.append((text_key, nested))
                if isinstance(nested, Mapping | list):
                    stack.append((nested, depth + 1))
        elif isinstance(current, list):
            for nested in current:
                if isinstance(nested, Mapping | list):
                    stack.append((nested, depth + 1))
    return result


def _normalize_posix_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def evaluate_docker_security(
    doc: EvidenceDocumentLike | None,
    *,
    evidence_run_id: str,
    helpers: DockerSecurityEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    if doc is None:
        return helpers.missing_lane("docker_security", "TWO_NODE_E2E_DOCKER_SECURITY_MISSING")
    payload = doc.payload
    status = helpers.normalized_status(payload.get("status"))
    blockers = list(helpers.stale_lane_blockers(payload))
    findings: list[dict[str, Any]] = []
    summary_status = str(payload.get("status", "unknown"))
    docker_proofs = _docker_display_security_proofs(payload)
    if status == STATUS_PASS:
        blockers.extend(helpers.current_run_blockers(payload, evidence_run_id, lane_name="docker_security"))
        contract_blockers, contract_findings = _docker_security_summary_contract_issues(
            doc,
            payload,
            evidence_run_id=evidence_run_id,
            summary_status=status,
            helpers=helpers,
        )
        blockers.extend(contract_blockers)
        findings.extend(contract_findings)
        if not helpers.has_live_docker_evidence(payload):
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_LIVE_CONTAINER_EVIDENCE_MISSING",
                    "Docker display security PASS requires live Docker/container evidence.",
                )
            )
        blockers.extend(_docker_missing_required_proof_blockers(docker_proofs, helpers=helpers))
    runtime = helpers.runtime_config(payload)
    if runtime:
        if runtime.get("service_role") != "display_readonly" or runtime.get("display_readonly") is not True:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DISPLAY_RUNTIME_ROLE_INVALID",
                    "Display runtime config must report display_readonly.",
                    runtime_config=runtime,
                )
            )
        if runtime.get("slurm_routes_enabled") is not False:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DISPLAY_SLURM_ROUTES_ENABLED",
                    "display_readonly runtime config must report Slurm routes disabled.",
                    runtime_config=runtime,
                )
            )
    if status == STATUS_PASS:
        if not runtime:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DISPLAY_RUNTIME_ROLE_INVALID",
                    "Display runtime config must report display_readonly.",
                    runtime_config=runtime,
                )
            )
    if helpers.bool_lookup(payload, "slurm_routes_unavailable") is False:
        findings.append(
            helpers.finding(
                "TWO_NODE_E2E_DISPLAY_SLURM_ROUTE_AVAILABLE",
                "Display Docker evidence shows a Slurm route is reachable.",
            )
        )
    if helpers.bool_lookup(payload, "published_artifacts_readonly") is False:
        findings.append(
            helpers.finding(
                "TWO_NODE_E2E_DISPLAY_PUBLISHED_ARTIFACTS_WRITABLE",
                "Display Docker evidence does not prove readonly published artifacts.",
            )
        )
    findings.extend(_docker_proof_findings(docker_proofs, helpers=helpers))
    for key in DOCKER_FORBIDDEN_BOOL_KEYS:
        value = helpers.bool_lookup(payload, key)
        if value is True:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                    f"Display Docker evidence exposes forbidden capability {key}.",
                    capability=key,
                )
            )
    for finding in helpers.payload_findings(payload):
        code = str(finding.get("code") or "")
        if any(token in code.upper() for token in DOCKER_FORBIDDEN_FINDING_TOKENS):
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_SECURITY_FINDING",
                    "Display Docker evidence contains a forbidden security finding.",
                    source_code=code,
                )
            )
    forbidden = helpers.first_mapping_value(payload, ("forbidden_capabilities", "capability_leaks"))
    if isinstance(forbidden, Sequence) and not isinstance(forbidden, str | bytes | bytearray) and forbidden:
        findings.append(
            helpers.finding(
                "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                "Display Docker evidence lists forbidden capabilities.",
                capabilities=list(forbidden),
            )
        )
    if findings:
        status = STATUS_FAIL
    elif blockers and status == STATUS_PASS:
        status = STATUS_BLOCKED
    return helpers.lane_from_status(
        "docker_security",
        doc,
        status=status,
        summary_status=summary_status,
        blockers=blockers,
        findings=findings,
    )



def docker_display_security_proofs(payload: Mapping[str, Any]) -> dict[str, bool | None]:
    """Return governed display Docker security proof states for shared consumers."""
    return _docker_display_security_proofs(payload)


def _docker_display_security_proofs(payload: Mapping[str, Any]) -> dict[str, bool | None]:
    proofs: dict[str, bool | None] = {}
    for proof_name, aliases in DOCKER_REQUIRED_FALSE_PROOFS.items():
        proofs[proof_name] = _governed_false_proof(payload, aliases)
    for proof_name, aliases in DOCKER_REQUIRED_TRUE_PROOFS.items():
        proofs[proof_name] = _governed_true_proof(payload, aliases)
    slurm_unavailable = _governed_true_proof(payload, ("slurm_routes_unavailable",))
    _merge_governed_docker_proof(
        proofs,
        "slurm_route_available",
        None if slurm_unavailable is None else not slurm_unavailable,
    )
    raw = _raw_docker_security_analysis(payload)
    for proof_name, value in raw.items():
        _merge_governed_docker_proof(proofs, proof_name, value)
    return proofs


def _docker_security_summary_contract_issues(
    doc: EvidenceDocumentLike,
    payload: Mapping[str, Any],
    *,
    evidence_run_id: str,
    summary_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    observed_schema = payload.get("schema_version") or payload.get("schema")
    if observed_schema != DOCKER_SECURITY_SUMMARY_SCHEMA:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SUMMARY_SCHEMA_MISSING",
                "Docker security PASS requires a producer security-summary schema.",
                expected_schema=DOCKER_SECURITY_SUMMARY_SCHEMA,
                schema=observed_schema,
            )
        )
    source_artifacts = payload.get("source_artifacts")
    if not isinstance(source_artifacts, Mapping):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACTS_MISSING",
                "Docker security PASS requires source_trust, static, and smoke source artifacts.",
            )
        )
        return blockers, findings
    for artifact_name, expected_schema in DOCKER_SECURITY_CHILD_SCHEMAS.items():
        artifact = source_artifacts.get(artifact_name)
        artifact_records: list[Mapping[str, Any]]
        if isinstance(artifact, Mapping):
            artifact_records = [artifact]
        elif artifact_name == "source_trust" and isinstance(artifact, list):
            artifact_records = [item for item in artifact if isinstance(item, Mapping)]
        else:
            artifact_records = []
        if not artifact_records:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_MISSING",
                    "Docker security summary is missing a required source artifact.",
                    artifact=artifact_name,
                )
            )
            continue
        child_payloads: list[Mapping[str, Any]] = []
        child_statuses: list[str] = []
        artifact_blockers, artifact_findings = _docker_security_child_artifact_issues(
            doc,
            artifact_name,
            artifact_records,
            expected_schema=expected_schema,
            evidence_run_id=evidence_run_id,
            summary_status=summary_status,
            helpers=helpers,
        )
        blockers.extend(artifact_blockers)
        findings.extend(artifact_findings)
        for artifact_record in artifact_records:
            payload = _read_docker_security_child_payload_for_contract(
                doc,
                artifact_name,
                artifact_record,
                evidence_run_id=evidence_run_id,
                summary_status=summary_status,
                helpers=helpers,
            )
            if payload is not None:
                child_payloads.append(payload)
                child_statuses.append(helpers.normalized_status(payload.get("status")))
        if artifact_name == "source_trust":
            blockers.extend(
                _docker_source_trust_combined_role_blockers(
                    child_payloads,
                    child_statuses=child_statuses,
                    helpers=helpers,
                )
            )
    return blockers, findings


def _docker_security_child_artifact_issues(
    doc: EvidenceDocumentLike,
    artifact_name: str,
    artifacts: Sequence[Mapping[str, Any]],
    *,
    expected_schema: str,
    evidence_run_id: str,
    summary_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for artifact in artifacts:
        artifact_blockers, artifact_findings = _docker_security_single_child_artifact_issues(
            doc,
            artifact_name,
            artifact,
            expected_schema=expected_schema,
            evidence_run_id=evidence_run_id,
            summary_status=summary_status,
            helpers=helpers,
        )
        blockers.extend(artifact_blockers)
        findings.extend(artifact_findings)
    return blockers, findings


def _docker_security_single_child_artifact_issues(
    doc: EvidenceDocumentLike,
    artifact_name: str,
    artifact: Mapping[str, Any],
    *,
    expected_schema: str,
    evidence_run_id: str,
    summary_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    raw_path = artifact.get("path")
    raw_sha256 = artifact.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip():
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PATH_MISSING",
                "Docker security source artifact must include a path.",
                artifact=artifact_name,
            )
        )
        return blockers, findings
    if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", raw_sha256.strip()):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_SHA_MISSING",
                "Docker security source artifact must include a sha256 digest.",
                artifact=artifact_name,
                path=raw_path,
            )
        )
        return blockers, findings
    try:
        path = helpers.approved_artifact_path(raw_path)
    except helpers.evidence_error_type:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_OUTSIDE_APPROVED_ROOT",
                "Docker security source artifact path must stay under approved evidence roots.",
                artifact=artifact_name,
                path=raw_path,
            )
        )
        return blockers, findings
    run_dir = doc.path.parent.parent
    containment_root = helpers.approved_artifact_containment_root(path)
    if not helpers.path_is_relative_to(path, run_dir):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED",
                "Docker security source artifact must come from the current evidence run directory.",
                artifact=artifact_name,
                path=helpers.public_path(path),
                expected_run_dir=helpers.public_path(run_dir),
            )
        )
        return blockers, findings
    try:
        child_doc = helpers.read_json(path, containment_root=containment_root)
    except FileNotFoundError:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_MISSING",
                "Docker security source artifact file is missing.",
                artifact=artifact_name,
                path=helpers.public_path(path),
            )
        )
        return blockers, findings
    except helpers.evidence_error_type as error:
        code = (
            "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_TOO_LARGE"
            if error.error_code == "TWO_NODE_E2E_EVIDENCE_PAYLOAD_TOO_LARGE"
            else "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PATH_UNSAFE"
            if error.error_code == "TWO_NODE_E2E_EVIDENCE_PATH_UNSAFE"
            else "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_JSON_INVALID"
        )
        blockers.append(
            helpers.blocker(
                code,
                "Docker security source artifact must be safely readable bounded JSON.",
                artifact=artifact_name,
                path=helpers.public_path(path),
            )
        )
        return blockers, findings
    if child_doc.sha256 != raw_sha256.lower():
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_HASH_MISMATCH",
                "Docker security source artifact sha256 does not match file content.",
                artifact=artifact_name,
                path=helpers.public_path(path),
            )
        )
    child_payload = child_doc.payload
    child_schema = child_payload.get("schema_version") or child_payload.get("schema")
    if child_schema != expected_schema:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_SCHEMA_INVALID",
                "Docker security source artifact has an unexpected schema.",
                artifact=artifact_name,
                expected_schema=expected_schema,
                schema=child_schema,
            )
        )
    child_status = helpers.normalized_status(child_payload.get("status"))
    if child_status == STATUS_FAIL:
        findings.append(
            helpers.finding(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_FAILED",
                "Docker security source artifact failed and must not be summarized as PASS.",
                artifact=artifact_name,
                child_status=child_status,
            )
        )
    elif child_status != STATUS_PASS:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_NOT_PASS",
                "Docker security source artifact must be PASS before final Docker security can PASS.",
                artifact=artifact_name,
                child_status=child_status,
            )
        )
    if not _docker_security_child_current_run_compatible(
        path,
        run_dir,
        child_payload,
        evidence_run_id,
        summary_status=summary_status,
        helpers=helpers,
    ):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_STALE_OR_UNSCOPED",
                "Docker security source artifact must be current-run-compatible.",
                artifact=artifact_name,
                path=helpers.public_path(path),
                expected_evidence_run_id=evidence_run_id,
            )
        )
    child_contract_blockers, child_contract_findings = _docker_security_child_subcontract_issues(
        artifact_name,
        child_payload,
        helpers=helpers,
    )
    blockers.extend(child_contract_blockers)
    findings.extend(child_contract_findings)
    return blockers, findings


def _read_docker_security_child_payload_for_contract(
    doc: EvidenceDocumentLike,
    artifact_name: str,
    artifact: Mapping[str, Any],
    *,
    evidence_run_id: str,
    summary_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> Mapping[str, Any] | None:
    blockers, findings = _docker_security_single_child_artifact_issues(
        doc,
        artifact_name,
        artifact,
        expected_schema=DOCKER_SECURITY_CHILD_SCHEMAS[artifact_name],
        evidence_run_id=evidence_run_id,
        summary_status=summary_status,
        helpers=helpers,
    )
    if blockers or findings:
        return None
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    try:
        path = helpers.approved_artifact_path(raw_path)
        containment_root = helpers.approved_artifact_containment_root(path)
        return helpers.read_json(path, containment_root=containment_root).payload
    except (FileNotFoundError, helpers.evidence_error_type):
        return None


def _docker_security_child_current_run_compatible(
    path: Path,
    run_dir: Path,
    child_payload: Mapping[str, Any],
    evidence_run_id: str,
    *,
    summary_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> bool:
    explicit_ids = helpers.explicit_bundle_run_ids_from_value(child_payload)
    if explicit_ids:
        return all(str(value) == evidence_run_id for _, value in explicit_ids)
    if helpers.normalized_status(summary_status) == STATUS_PASS:
        return False
    return helpers.path_is_relative_to(path, run_dir)


def _docker_security_child_subcontract_issues(
    artifact_name: str,
    child_payload: Mapping[str, Any],
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    child_status = helpers.normalized_status(child_payload.get("status"))
    producer_blockers = helpers.payload_blockers(child_payload)
    producer_findings = helpers.payload_findings(child_payload)
    if child_status == STATUS_PASS:
        if producer_blockers:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PRODUCER_BLOCKERS_PRESENT",
                    "Docker security PASS child cannot contain producer blockers.",
                    artifact=artifact_name,
                    producer_blocker_count=len(producer_blockers),
                )
            )
        if producer_findings:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_ARTIFACT_PRODUCER_FINDINGS_PRESENT",
                    "Docker security PASS child cannot contain producer findings.",
                    artifact=artifact_name,
                    producer_finding_count=len(producer_findings),
                )
            )
    if artifact_name == "source_trust":
        blockers.extend(
            _docker_source_trust_child_blockers(
                child_payload,
                child_status=child_status,
                helpers=helpers,
            )
        )
    elif artifact_name == "static":
        static_blockers, static_findings = _docker_static_child_issues(
            child_payload,
            child_status=child_status,
            helpers=helpers,
        )
        blockers.extend(static_blockers)
        findings.extend(static_findings)
    elif artifact_name == "smoke":
        blockers.extend(
            _docker_smoke_child_blockers(
                child_payload,
                child_status=child_status,
                helpers=helpers,
            )
        )
    return blockers, findings


def _docker_source_trust_child_blockers(
    child_payload: Mapping[str, Any],
    *,
    child_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    if child_status != STATUS_PASS:
        return []
    blockers: list[dict[str, Any]] = []
    untrusted_keys = (
        "untrusted_owner",
        "source_trust_failed",
        "trusted_owner_missing",
        "group_or_world_writable",
        "symlink_rejected",
    )
    for key in untrusted_keys:
        if helpers.bool_lookup_any(child_payload, (key,)) is True:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_SECURITY_SOURCE_TRUST_UNCLEAN",
                    "Docker source_trust child PASS contradicts source-trust safety fields.",
                    artifact="source_trust",
                    evidence_key=key,
                )
            )
    checked_paths = child_payload.get("checked_paths")
    if not isinstance(checked_paths, list) or not checked_paths:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_CHECKED_PATHS_MISSING",
                "Docker source_trust child PASS requires non-empty checked_paths proof records.",
                artifact="source_trust",
            )
        )
        return blockers
    records = [record for record in checked_paths if isinstance(record, Mapping)]
    labels = {str(record.get("label") or "") for record in records}
    required_labels = _docker_source_trust_required_labels(child_payload)
    for label in sorted(required_labels - labels):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_REQUIRED_LABEL_MISSING",
                "Docker source_trust child PASS is missing a required checked path label.",
                artifact="source_trust",
                label=label,
            )
        )
    for record in records:
        if str(record.get("label") or "") in required_labels:
            blockers.extend(_docker_source_trust_record_blockers(record, helpers=helpers))
    return blockers


def _docker_source_trust_combined_role_blockers(
    child_payloads: Sequence[Mapping[str, Any]],
    *,
    child_statuses: Sequence[str],
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    if not child_payloads:
        return []
    if any(status != STATUS_PASS for status in child_statuses):
        return []
    records: list[Mapping[str, Any]] = []
    for payload in child_payloads:
        checked_paths = payload.get("checked_paths")
        if isinstance(checked_paths, list):
            records.extend(record for record in checked_paths if isinstance(record, Mapping))
    labels = {str(record.get("label") or "") for record in records}
    blockers: list[dict[str, Any]] = []
    for role, label in DOCKER_SOURCE_TRUST_ROLE_LABELS.items():
        role_records = [record for record in records if str(record.get("label") or "") == label]
        if not role_records:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_ROLE_ENV_PROOF_MISSING",
                    "Docker source_trust final PASS requires both compute and display role env proof.",
                    artifact="source_trust",
                    role=role,
                    label=label,
                    observed_labels=sorted(labels),
                )
            )
            continue
        for record in role_records:
            blockers.extend(_docker_source_trust_record_blockers(record, helpers=helpers))
    return blockers


def _docker_source_trust_required_labels(child_payload: Mapping[str, Any]) -> set[str]:
    required = set(DOCKER_SOURCE_TRUST_COMMON_REQUIRED_LABELS)
    role_values = _docker_source_trust_required_roles(child_payload)
    for role in role_values:
        label = DOCKER_SOURCE_TRUST_ROLE_LABELS.get(role)
        if label:
            required.add(label)
    return required


def _docker_source_trust_required_roles(child_payload: Mapping[str, Any]) -> set[str]:
    default_roles = set(DOCKER_SOURCE_TRUST_ROLE_LABELS)
    roles = child_payload.get("roles")
    if not isinstance(roles, list):
        return default_roles
    role_values = {str(role).strip() for role in roles if str(role).strip()}
    if role_values and role_values <= set(DOCKER_SOURCE_TRUST_ROLE_LABELS):
        return role_values
    return default_roles


def _docker_source_trust_record_blockers(
    record: Mapping[str, Any],
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    label = str(record.get("label") or "")
    expected_kind = str(record.get("expected_kind") or "")
    if label in {
        "checkout root",
        "infra directory",
        "env source directory",
        "systemd source directory",
        "trust path component",
    }:
        required_kind = "directory"
        kind_key = "is_directory"
    else:
        required_kind = "file"
        kind_key = "is_regular"
    blockers: list[dict[str, Any]] = []
    if expected_kind != required_kind:
        blockers.append(
            _docker_source_trust_record_blocker(
                record,
                "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_KIND_MISMATCH",
                helpers=helpers,
            )
        )
    for key, expected, code in (
        ("exists", True, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_PATH_MISSING"),
        ("is_symlink", False, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_SYMLINK"),
        ("trusted_owner", True, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_OWNER_UNTRUSTED"),
        ("group_writable", False, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_GROUP_WRITABLE"),
        ("world_writable", False, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_WORLD_WRITABLE"),
        (kind_key, True, "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_KIND_MISMATCH"),
    ):
        if record.get(key) is not expected:
            blockers.append(
                _docker_source_trust_record_blocker(
                    record,
                    code,
                    helpers=helpers,
                    evidence_key=key,
                    observed=record.get(key),
                )
            )
    if label in set(DOCKER_SOURCE_TRUST_ROLE_LABELS.values()) and str(record.get("mode") or "") != "0600":
        blockers.append(
            _docker_source_trust_record_blocker(
                record,
                "TWO_NODE_E2E_DOCKER_SOURCE_TRUST_ROLE_ENV_MODE_INVALID",
                helpers=helpers,
                evidence_key="mode",
                observed=record.get("mode"),
            )
        )
    return blockers


def _docker_source_trust_record_blocker(
    record: Mapping[str, Any],
    code: str,
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
    evidence_key: str | None = None,
    observed: Any = None,
) -> dict[str, Any]:
    blocker = helpers.blocker(
        code,
        "Docker source_trust child PASS contains an unsafe or incomplete checked path record.",
        artifact="source_trust",
        label=record.get("label"),
        path=record.get("path"),
    )
    if evidence_key is not None:
        blocker["evidence_key"] = evidence_key
        blocker["observed"] = observed
    return blocker


def _docker_static_child_issues(
    child_payload: Mapping[str, Any],
    *,
    child_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    if child_status != STATUS_PASS:
        return blockers, findings
    proofs = _docker_display_security_proofs(child_payload)
    blockers.extend(
        _with_context(blocker, artifact="static")
        for blocker in _docker_missing_required_static_child_proof_blockers(proofs, helpers=helpers)
    )
    findings.extend(
        _with_context(finding, artifact="static")
        for finding in _docker_proof_findings(proofs, helpers=helpers)
    )
    for key in DOCKER_FORBIDDEN_BOOL_KEYS:
        value = helpers.bool_lookup(child_payload, key)
        if value is True:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                    f"Docker static child exposes forbidden capability {key}.",
                    artifact="static",
                    capability=key,
                )
            )
    for finding in helpers.payload_findings(child_payload):
        code = str(finding.get("code") or "")
        if any(token in code.upper() for token in DOCKER_FORBIDDEN_FINDING_TOKENS):
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_SECURITY_FINDING",
                    "Docker static child contains a forbidden security finding.",
                    artifact="static",
                    source_code=code,
                )
            )
    return blockers, findings


def _docker_missing_required_static_child_proof_blockers(
    proofs: Mapping[str, bool | None],
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    static_required = (
        "privileged",
        "host_network",
        "host_pid",
        "host_ipc",
        "cap_add_present",
        "forbidden_hostconfig_hazard",
        "forbidden_mount_hazard",
        "forbidden_env_hazard",
        "docker_socket_present",
        "broad_host_bind_present",
        "private_workspace_bind_present",
        "workspace_mount_present",
        "writable_published_artifact_mount",
        "display_write_capability_present",
        "published_artifacts_readonly",
        "root_filesystem_readonly",
        "cap_drop_all",
    )
    for proof_name in static_required:
        if proofs.get(proof_name) is None:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_STATIC_CHILD_PROOF_MISSING",
                    "Docker static child PASS requires explicit static HostConfig/mount/env proof.",
                    proof=proof_name,
                )
            )
    return blockers


def _docker_smoke_child_blockers(
    child_payload: Mapping[str, Any],
    *,
    child_status: str,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    if child_status != STATUS_PASS:
        return []
    if helpers.has_live_docker_evidence(child_payload):
        return []
    return [
        helpers.blocker(
            "TWO_NODE_E2E_DOCKER_SMOKE_LIVE_COMMAND_EVIDENCE_MISSING",
            "Docker smoke child PASS requires live Docker command/smoke evidence.",
            artifact="smoke",
        )
    ]


def _governed_false_proof(payload: Mapping[str, Any], aliases: Sequence[str]) -> bool | None:
    values = _bool_lookup_values(payload, frozenset(aliases))
    if any(value is True for value in values):
        return True
    if any(value is False for value in values):
        return False
    return None


def _governed_true_proof(payload: Mapping[str, Any], aliases: Sequence[str]) -> bool | None:
    values = _bool_lookup_values(payload, frozenset(aliases))
    if any(value is False for value in values):
        return False
    if any(value is True for value in values):
        return True
    return None


def _merge_governed_docker_proof(
    proofs: dict[str, bool | None],
    proof_name: str,
    value: bool | None,
) -> None:
    if value is None:
        if proof_name in DOCKER_REQUIRED_FALSE_PROOFS and proofs.get(proof_name) is not True:
            proofs[proof_name] = None
        elif proof_name in DOCKER_REQUIRED_TRUE_PROOFS and proofs.get(proof_name) is not False:
            proofs[proof_name] = None
        return
    if proof_name in DOCKER_REQUIRED_FALSE_PROOFS:
        if value is True:
            proofs[proof_name] = True
        else:
            proofs.setdefault(proof_name, False)
    elif proof_name in DOCKER_REQUIRED_TRUE_PROOFS:
        if value is False:
            proofs[proof_name] = False
        else:
            proofs.setdefault(proof_name, True)
    else:
        proofs[proof_name] = value


def _docker_missing_required_proof_blockers(
    proofs: Mapping[str, bool | None],
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for proof_name in (*DOCKER_REQUIRED_FALSE_PROOFS.keys(), *DOCKER_REQUIRED_TRUE_PROOFS.keys()):
        if proofs.get(proof_name) is None:
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_PROOF_MISSING",
                    "Docker security PASS requires explicit no-capability/read-only proof for every governed "
                    "display surface.",
                    proof=proof_name,
                )
            )
    return blockers


def _docker_proof_findings(
    proofs: Mapping[str, bool | None],
    *,
    helpers: DockerSecurityEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for proof_name in DOCKER_REQUIRED_FALSE_PROOFS:
        if proofs.get(proof_name) is True:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_FORBIDDEN_CAPABILITY",
                    "Display Docker evidence exposes a forbidden capability.",
                    capability=proof_name,
                )
            )
    for proof_name in DOCKER_REQUIRED_TRUE_PROOFS:
        if proofs.get(proof_name) is False:
            findings.append(
                helpers.finding(
                    "TWO_NODE_E2E_DOCKER_DISPLAY_REQUIRED_READONLY_PROOF_FALSE",
                    "Display Docker evidence contradicts required readonly/no-capability proof.",
                    proof=proof_name,
                )
            )
    return findings


def _raw_docker_security_analysis(payload: Mapping[str, Any]) -> dict[str, bool | None]:
    raw_proofs: dict[str, bool | None] = {}
    mount_hazards = _empty_raw_mount_hazards()
    env_hazard = False
    published_readonly_seen = False
    for inspect_object in _docker_inspect_objects(payload):
        host_config = _first_raw_mapping(inspect_object, ("HostConfig", "host_config"))
        if host_config:
            _merge_raw_proof(
                raw_proofs,
                "privileged",
                _raw_bool(_first_present(host_config, ("Privileged", "privileged"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_network",
                _mode_is_host_or_shared(_first_present(host_config, ("NetworkMode", "network_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_pid",
                _mode_is_host_or_shared(_first_present(host_config, ("PidMode", "pid", "pid_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "host_ipc",
                _mode_is_host_or_shared(_first_present(host_config, ("IpcMode", "ipc", "ipc_mode"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "cap_add_present",
                _sequence_has_values(_first_present(host_config, ("CapAdd", "cap_add"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "cap_drop_all",
                _cap_drop_all(_first_present(host_config, ("CapDrop", "cap_drop"))),
            )
            _merge_raw_proof(
                raw_proofs,
                "root_filesystem_readonly",
                _raw_bool(
                    _first_present(
                        host_config,
                        ("ReadonlyRootfs", "ReadonlyRootFS", "read_only", "readonly_rootfs"),
                    )
                ),
            )
            _merge_raw_proof(raw_proofs, "forbidden_hostconfig_hazard", _hostconfig_hazard(host_config))
            _merge_mount_hazards(mount_hazards, _raw_bind_mounts(_first_present(host_config, ("Binds", "binds"))))
        config = _first_raw_mapping(inspect_object, ("Config", "config"))
        if config:
            env_hazard = env_hazard or _env_has_forbidden_keys(config.get("Env") or config.get("env"))
        _merge_mount_hazards(
            mount_hazards,
            _raw_structured_mounts(inspect_object.get("Mounts") or inspect_object.get("mounts")),
        )
    for compose_service in _docker_compose_service_objects(payload):
        _merge_raw_proof(raw_proofs, "privileged", _raw_bool(compose_service.get("privileged")))
        _merge_raw_proof(raw_proofs, "host_network", _mode_is_host_or_shared(compose_service.get("network_mode")))
        _merge_raw_proof(raw_proofs, "host_pid", _mode_is_host_or_shared(compose_service.get("pid")))
        _merge_raw_proof(raw_proofs, "host_ipc", _mode_is_host_or_shared(compose_service.get("ipc")))
        _merge_raw_proof(raw_proofs, "cap_add_present", _sequence_has_values(compose_service.get("cap_add")))
        _merge_raw_proof(raw_proofs, "cap_drop_all", _cap_drop_all(compose_service.get("cap_drop")))
        _merge_raw_proof(raw_proofs, "root_filesystem_readonly", _raw_bool(compose_service.get("read_only")))
        _merge_mount_hazards(mount_hazards, _compose_mount_hazards(compose_service.get("volumes")))
        env_hazard = env_hazard or _env_has_forbidden_keys(compose_service.get("environment"))
    if mount_hazards["docker_socket"]:
        raw_proofs["docker_socket_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("docker_socket_present", False)
    if mount_hazards["forbidden_mount"]:
        raw_proofs["forbidden_mount_hazard"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("forbidden_mount_hazard", False)
    if mount_hazards["broad_host_bind"]:
        raw_proofs["broad_host_bind_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("broad_host_bind_present", False)
    if mount_hazards["private_workspace_bind"]:
        raw_proofs["private_workspace_bind_present"] = True
        raw_proofs["workspace_mount_present"] = True
    elif mount_hazards["mount_evidence"]:
        raw_proofs.setdefault("private_workspace_bind_present", False)
        raw_proofs.setdefault("workspace_mount_present", False)
    if mount_hazards["writable_published"]:
        raw_proofs["writable_published_artifact_mount"] = True
        raw_proofs["published_artifacts_readonly"] = False
    elif mount_hazards["published_mount_unknown"]:
        raw_proofs.setdefault("writable_published_artifact_mount", None)
        raw_proofs.setdefault("published_artifacts_readonly", None)
    elif mount_hazards["published_mount_seen"]:
        raw_proofs.setdefault("writable_published_artifact_mount", False)
        published_readonly_seen = True
    if env_hazard:
        raw_proofs["forbidden_env_hazard"] = True
    elif _raw_env_evidence_present(payload):
        raw_proofs.setdefault("forbidden_env_hazard", False)
    if published_readonly_seen:
        raw_proofs.setdefault("published_artifacts_readonly", True)
    return raw_proofs


def _docker_inspect_objects(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    objects: list[Mapping[str, Any]] = []
    for key in (
        "docker_inspect",
        "container_inspect",
        "display_container_inspect",
        "inspect",
        "inspect_data",
        "container",
    ):
        for item in _mapping_objects(payload.get(key)):
            objects.append(item)
            if any(top_key in item for top_key in ("pid", "ipc", "cap_drop")):
                objects.append({"host_config": item})
    if any(key in payload for key in ("HostConfig", "host_config", "Config", "config", "Mounts", "mounts")):
        objects.append(payload)
    return objects


def _docker_compose_service_objects(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    services: list[Mapping[str, Any]] = []
    for key in ("compose_service", "display_service", "service"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            services.append(value)
    for key in ("compose", "compose_config", "docker_compose_config"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            raw_services = value.get("services")
            if isinstance(raw_services, Mapping):
                services.extend(item for item in raw_services.values() if isinstance(item, Mapping))
    raw_services = payload.get("services")
    if isinstance(raw_services, Mapping):
        services.extend(item for item in raw_services.values() if isinstance(item, Mapping))
    if any(
        key in payload
        for key in ("privileged", "network_mode", "pid", "ipc", "cap_add", "cap_drop", "volumes", "read_only")
    ):
        services.append(payload)
    return services


def _mapping_objects(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _first_raw_mapping(payload: Mapping[str, Any], keys: Sequence[str]) -> Mapping[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _merge_raw_proof(proofs: dict[str, bool | None], key: str, value: bool | None) -> None:
    if value is None:
        return
    if value is True:
        proofs[key] = True
    else:
        proofs.setdefault(key, False)


def _empty_raw_mount_hazards() -> dict[str, bool]:
    return {
        "mount_evidence": False,
        "published_mount_seen": False,
        "published_mount_unknown": False,
        "docker_socket": False,
        "forbidden_mount": False,
        "broad_host_bind": False,
        "private_workspace_bind": False,
        "writable_published": False,
    }


def _merge_mount_hazards(target: dict[str, bool], source: Mapping[str, bool]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, False) or bool(value)


def _raw_bind_mounts(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if not isinstance(item, str):
            continue
        parts = item.split(":")
        source = parts[0] if parts else ""
        target = parts[1] if len(parts) > 1 else ""
        mode = ":".join(parts[2:]) if len(parts) > 2 else ""
        read_only = _short_mount_read_only(mode)
        _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _raw_structured_mounts(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("Source") or item.get("source") or "")
        target = str(item.get("Destination") or item.get("Target") or item.get("target") or "")
        read_only = _mount_read_only(item)
        _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _compose_mount_hazards(value: Any) -> dict[str, bool]:
    hazards = _empty_raw_mount_hazards()
    if not isinstance(value, list):
        return hazards
    for item in value:
        if isinstance(item, str):
            parts = item.split(":")
            source = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else ""
            mode = ":".join(parts[2:]) if len(parts) > 2 else ""
            read_only = _short_mount_read_only(mode)
            _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
        elif isinstance(item, Mapping):
            source = str(item.get("source") or item.get("src") or "")
            target = str(item.get("target") or item.get("dst") or item.get("destination") or "")
            read_only = _mount_read_only(item)
            _record_mount_hazard(hazards, source=source, target=target, read_only=read_only)
    return hazards


def _record_mount_hazard(
    hazards: dict[str, bool],
    *,
    source: str,
    target: str,
    read_only: bool | None,
) -> None:
    if not source and not target:
        return
    hazards["mount_evidence"] = True
    if _is_docker_socket_path(source) or _is_docker_socket_path(target):
        hazards["docker_socket"] = True
    if _is_forbidden_mount_path(source) or _is_forbidden_mount_path(target):
        hazards["forbidden_mount"] = True
    if _is_broad_host_bind_source(source):
        hazards["broad_host_bind"] = True
    if _is_private_workspace_path(source) or _is_private_workspace_path(target):
        hazards["private_workspace_bind"] = True
    if _is_published_artifact_path(source) or _is_published_artifact_path(target):
        hazards["published_mount_seen"] = True
        if read_only is False:
            hazards["writable_published"] = True
        elif read_only is None:
            hazards["published_mount_unknown"] = True


def _mount_read_only(mount: Mapping[str, Any]) -> bool | None:
    if "RW" in mount:
        raw = _raw_bool(mount.get("RW"))
        return None if raw is None else not raw
    if "read_only" in mount:
        return _raw_bool(mount.get("read_only"))
    if "readonly" in mount:
        return _raw_bool(mount.get("readonly"))
    mode = str(mount.get("Mode") or mount.get("mode") or "")
    if mode:
        return _short_mount_read_only(mode)
    return None


def _short_mount_read_only(mode: str) -> bool | None:
    tokens = {part.strip().lower() for part in str(mode).split(",") if part.strip()}
    if "ro" in tokens:
        return True
    if "rw" in tokens:
        return False
    return None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _mode_is_host_or_shared(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return False
    return text == "host" or text.startswith("container:") or text.startswith("service:")


def _sequence_has_values(value: Any) -> bool | None:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return bool(value)
    return None


def _cap_drop_all(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().upper() == "ALL"
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return any(str(item).strip().upper() == "ALL" for item in value)
    return None


def _hostconfig_hazard(host_config: Mapping[str, Any]) -> bool:
    return any(
        value is True
        for value in (
            _raw_bool(_first_present(host_config, ("Privileged", "privileged"))),
            _mode_is_host_or_shared(_first_present(host_config, ("NetworkMode", "network_mode"))),
            _mode_is_host_or_shared(_first_present(host_config, ("PidMode", "pid", "pid_mode"))),
            _mode_is_host_or_shared(_first_present(host_config, ("IpcMode", "ipc", "ipc_mode"))),
            _sequence_has_values(_first_present(host_config, ("CapAdd", "cap_add"))),
        )
    )


def _env_has_forbidden_keys(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(str(key) in DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS for key in value.keys())
    if isinstance(value, list):
        for item in value:
            key = str(item).split("=", 1)[0].strip()
            if key in DOCKER_DISPLAY_FORBIDDEN_ENV_KEYS:
                return True
    return False


def _raw_env_evidence_present(payload: Mapping[str, Any]) -> bool:
    for inspect_object in _docker_inspect_objects(payload):
        config = _first_raw_mapping(inspect_object, ("Config", "config"))
        if config and ("Env" in config or "env" in config):
            return True
    return any("environment" in service for service in _docker_compose_service_objects(payload))


def _is_docker_socket_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in {"/var/run/docker.sock", "/run/docker.sock"} or (
        normalized.startswith("/") and normalized.endswith("/docker.sock")
    )


def _is_forbidden_mount_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    lowered = normalized.lower()
    return any(token.lower() in lowered for token in DOCKER_FORBIDDEN_MOUNT_TOKENS) or _is_munge_path(normalized)


def _is_munge_path(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return (
        normalized in {"/run/munge", "/var/run/munge", "/etc/munge"}
        or normalized.startswith("/run/munge/")
        or normalized.startswith("/var/run/munge/")
        or normalized.startswith("/etc/munge/")
        or normalized.endswith("/munge.key")
        or normalized.endswith("/munge.socket")
    )


def _is_broad_host_bind_source(value: str) -> bool:
    normalized = _normalize_posix_path(value)
    return normalized in DOCKER_BROAD_HOST_ROOTS or normalized.startswith("/scratch/")


def _is_private_workspace_path(value: str) -> bool:
    normalized = _normalize_posix_path(value).lower()
    return any(token in normalized for token in ("workspace", ".nhms-runs", "/basins", "/shud", "model_asset"))


def _is_published_artifact_path(value: str) -> bool:
    normalized = _normalize_posix_path(value).lower()
    return "published" in normalized and ("artifact" in normalized or "nhms" in normalized or "/var/lib" in normalized)
