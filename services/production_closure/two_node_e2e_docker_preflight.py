from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

STATUS_PASS = "PASS"
STATUS_BLOCKED = "BLOCKED"

DOCKER_PREFLIGHT_SCHEMA = "nhms.two_node_docker.preflight.v1"
DOCKER_PREFLIGHT_REQUIRED_COMMANDS = (
    "docker_version",
    "docker_compose_version",
    "docker_info_docker_root",
    "docker_system_df",
    "df_h",
)
DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS = ("evidence_root", "tmpdir", "docker_root")
DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES = (
    "docker-preflight/summary.json",
    "docker-preflight/docker-preflight.json",
    "docker-preflight.json",
)
DOCKER_PREFLIGHT_LANE_OWNER = "services.production_closure.two_node_e2e_docker_preflight"
DOCKER_PREFLIGHT_LANE_VERIFICATION = (
    'uv run pytest -q tests/test_two_node_e2e_evidence.py -k "docker_preflight"'
)
DOCKER_PREFLIGHT_LANE_GUARD_SYMBOLS = (
    "DOCKER_PREFLIGHT_DOCUMENT_CANDIDATES",
    "DOCKER_PREFLIGHT_SCHEMA",
    "DOCKER_PREFLIGHT_REQUIRED_COMMANDS",
    "DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS",
    "DockerPreflightEvaluationHelpers",
    "evaluate_docker_preflight",
    "_docker_preflight_contract_blockers",
    "_docker_preflight_current_run_blockers",
)
DOCKER_PREFLIGHT_BLOCKER_NAMESPACES = (
    "TWO_NODE_E2E_DOCKER_PREFLIGHT_",
    "TWO_NODE_E2E_DOCKER_ROOT_",
    "TWO_NODE_E2E_RECORDED_PATH_OUTSIDE_APPROVED_ROOTS",
    "TWO_NODE_E2E_CURRENT_EVIDENCE_RUN_ID_",
    "TWO_NODE_E2E_STALE_EVIDENCE_RUN_ID",
)

LaneResultT = TypeVar("LaneResultT")
LaneResultT_co = TypeVar("LaneResultT_co", covariant=True)


class EvidenceDocumentLike(Protocol):
    path: Path
    payload: Mapping[str, Any]
    sha256: str


class BlockerFactory(Protocol):
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


class RecordedPathApprovalBlockers(Protocol):
    def __call__(
        self,
        payload: Mapping[str, Any],
        keys: Sequence[str],
        *,
        lane_name: str,
    ) -> list[dict[str, Any]]: ...


class CurrentRunBlockers(Protocol):
    def __call__(
        self,
        payload: Mapping[str, Any],
        evidence_run_id: str,
        *,
        lane_name: str,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DockerPreflightEvaluationHelpers(Generic[LaneResultT]):
    missing_lane: MissingLaneAdapter[LaneResultT]
    lane_from_status: LaneFromStatusAdapter[LaneResultT]
    normalized_status: Callable[[Any], str]
    blocker: BlockerFactory
    stale_lane_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    current_run_blockers: CurrentRunBlockers
    recorded_path_approval_blockers: RecordedPathApprovalBlockers
    int_value: Callable[[Any], int | None]


def evaluate_docker_preflight(
    doc: EvidenceDocumentLike | None,
    *,
    evidence_run_id: str,
    helpers: DockerPreflightEvaluationHelpers[LaneResultT],
) -> LaneResultT:
    if doc is None:
        return helpers.missing_lane("docker_preflight", "TWO_NODE_E2E_DOCKER_PREFLIGHT_MISSING")
    payload = doc.payload
    status = helpers.normalized_status(payload.get("status"))
    blockers = list(helpers.stale_lane_blockers(payload))
    summary_status = str(payload.get("status", "unknown"))
    if status == STATUS_PASS:
        preflight_contract_blockers = _docker_preflight_contract_blockers(payload, helpers=helpers)
        blockers.extend(preflight_contract_blockers)
        blockers.extend(
            _docker_preflight_current_run_blockers(
                payload,
                evidence_run_id=evidence_run_id,
                helpers=helpers,
                contract_complete=not preflight_contract_blockers,
            )
        )
        commands = payload.get("commands")
        if not isinstance(commands, Mapping):
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMANDS_MISSING",
                    "Docker preflight PASS must include live docker command evidence.",
                )
            )
        else:
            for command_name in DOCKER_PREFLIGHT_REQUIRED_COMMANDS:
                command = commands.get(command_name)
                if not isinstance(command, Mapping) or command.get("returncode") != 0:
                    blockers.append(
                        helpers.blocker(
                            "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_FAILED",
                            f"Docker preflight command {command_name} is missing or did not succeed.",
                            command=command_name,
                        )
                    )
        if not payload.get("docker_root_dir"):
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_ROOT_MISSING",
                    "Docker preflight PASS must record DockerRootDir.",
                )
            )
    return helpers.lane_from_status(
        "docker_preflight",
        doc,
        status=STATUS_BLOCKED if blockers and status == STATUS_PASS else status,
        summary_status=summary_status,
        blockers=blockers,
    )


def _docker_preflight_contract_blockers(
    payload: Mapping[str, Any],
    *,
    helpers: DockerPreflightEvaluationHelpers[Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if payload.get("schema_version") != DOCKER_PREFLIGHT_SCHEMA and payload.get("schema") != DOCKER_PREFLIGHT_SCHEMA:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_SCHEMA_UNRECOGNIZED",
                "Docker preflight PASS must use the known preflight producer schema.",
                schema=payload.get("schema") or payload.get("schema_version"),
                expected_schema=DOCKER_PREFLIGHT_SCHEMA,
            )
        )
    for key in ("evidence_root", "tmpdir", "docker_root_dir", "min_free_bytes"):
        value = payload.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            blockers.append(
                helpers.blocker(
                    "TWO_NODE_E2E_DOCKER_PREFLIGHT_RESOURCE_EVIDENCE_MISSING",
                    "Docker preflight PASS must record DockerRootDir, TMPDIR, evidence root, and min-free evidence.",
                    evidence_key=key,
                )
            )
    blockers.extend(
        helpers.recorded_path_approval_blockers(
            payload,
            ("evidence_root", "tmpdir"),
            lane_name="docker_preflight",
        )
    )
    producer_blockers = payload.get("blockers")
    if isinstance(producer_blockers, list) and producer_blockers:
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_PRODUCER_BLOCKERS_PRESENT",
                "Docker preflight PASS cannot contain producer blockers.",
                producer_blocker_count=len(producer_blockers),
            )
        )
    disk = payload.get("disk")
    if not isinstance(disk, Mapping):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_MISSING",
                "Docker preflight PASS must include disk evidence.",
            )
        )
    else:
        min_free_bytes = helpers.int_value(payload.get("min_free_bytes"))
        for label in DOCKER_PREFLIGHT_REQUIRED_DISK_LABELS:
            snapshot = disk.get(label)
            if not isinstance(snapshot, Mapping) or snapshot.get("free_bytes") is None:
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_MISSING",
                        "Docker preflight PASS must include free-space evidence for required disk labels.",
                        label=label,
                    )
                )
                continue
            free_bytes = helpers.int_value(snapshot.get("free_bytes"))
            if free_bytes is None:
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_DISK_EVIDENCE_INVALID",
                        "Docker preflight disk free_bytes must be numeric.",
                        label=label,
                    )
                )
            elif min_free_bytes is not None and free_bytes < min_free_bytes:
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_LOW_DISK_SPACE",
                        "Docker preflight PASS contradicts required free-space minimum.",
                        label=label,
                        free_bytes=free_bytes,
                        min_free_bytes=min_free_bytes,
                    )
                )
    commands = payload.get("commands")
    if not isinstance(commands, Mapping):
        blockers.append(
            helpers.blocker(
                "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMANDS_MISSING",
                "Docker preflight PASS must include command evidence.",
            )
        )
    else:
        for command_name in DOCKER_PREFLIGHT_REQUIRED_COMMANDS:
            if command_name not in commands:
                blockers.append(
                    helpers.blocker(
                        "TWO_NODE_E2E_DOCKER_PREFLIGHT_COMMAND_EVIDENCE_MISSING",
                        "Docker preflight PASS is missing required command evidence.",
                        command=command_name,
                    )
                )
    return blockers


def _docker_preflight_current_run_blockers(
    payload: Mapping[str, Any],
    *,
    evidence_run_id: str,
    helpers: DockerPreflightEvaluationHelpers[Any],
    contract_complete: bool,
) -> list[dict[str, Any]]:
    _ = contract_complete
    return helpers.current_run_blockers(payload, evidence_run_id, lane_name="docker_preflight")
