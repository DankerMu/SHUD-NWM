from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping, Sequence

from packages.common.safe_fs import (
    SafeFilesystemError,
    read_bytes_limited_no_follow,
)
from services.production_closure import (
    readiness_item_contracts as _readiness_item_contracts,
)
from services.production_closure import (
    readiness_shared_artifacts as _readiness_shared_artifacts,
)

validate_readiness_item = _readiness_item_contracts.validate_readiness_item

MAX_RECEIPT_BYTES = _readiness_shared_artifacts.MAX_RECEIPT_BYTES
MAX_STRING_LENGTH = _readiness_shared_artifacts.MAX_STRING_LENGTH
PATH_TOKEN_RE = _readiness_shared_artifacts.PATH_TOKEN_RE
_bounded_redacted_payload = _readiness_shared_artifacts._bounded_redacted_payload
_path_for_evidence = _readiness_shared_artifacts._path_for_evidence
_redact_paths = _readiness_shared_artifacts._redact_paths
_refuse_symlink_components = _readiness_shared_artifacts._refuse_symlink_components

_DEPENDENCY_SUMMARY_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

DEPENDENCY_SUMMARY_CONTRACTS = {
    "slurm": {
        "issue": 147,
        "schema": "nhms.production_closure.slurm.v1",
        "allowed_statuses": {"ready", "submitted"},
    },
    "object_store": {
        "issue": 148,
        "schema": "nhms.production_closure.object_store.v1",
        "allowed_statuses": {"ready"},
    },
    "source": {
        "issue": 149,
        "schema": "nhms.production_closure.met.v1",
        "allowed_statuses": {"ready"},
    },
    "e2e": {
        "issue": 150,
        "schema": "nhms.production_closure.e2e.v1",
        "allowed_statuses": {"ready"},
    },
    "mvt": {
        "issue": 151,
        "schema": "nhms.production_closure.scale.v1",
        "allowed_statuses": {"ready"},
    },
}


def _dependency_summary_items(
    config: Any,
    *,
    read_dependency_summary_item: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    reader = read_dependency_summary_item or _read_dependency_summary_item
    items: list[dict[str, Any]] = []
    for name in DEPENDENCY_SUMMARY_CONTRACTS:
        root = config.dependency_roots.get(name)
        if root is None:
            items.append(
                _item(
                    item_id=f"deterministic-{name}-summary",
                    surface=f"{name}_production_like_evidence",
                    status="not_executed",
                    execution_mode="not_executed",
                    required_for_final=False,
                    live_proof_accepted=False,
                    artifact_refs=[],
                    residual_risk=(
                        "No existing production-closure summary was supplied to this readiness run; "
                        "deterministic fast CI remains self-contained."
                    ),
                    removal_criteria=(
                        f"Run or provide the {name} production-closure summary when deterministic dependency "
                        "lineage is needed for release review."
                    ),
                )
            )
            continue
        items.append(reader(name, root, config=config))
    return items


def _read_dependency_summary_item(
    name: str,
    root: Path,
    *,
    config: Any,
    find_summary_path: Callable[[str, Path], Path] | None = None,
    dependency_summary_blocked: Callable[..., dict[str, Any]] | None = None,
    dependency_summary_artifact_ref: Callable[[str, Path, Path], str] | None = None,
) -> dict[str, Any]:
    contract = DEPENDENCY_SUMMARY_CONTRACTS[name]
    find_path = find_summary_path or _find_summary_path
    blocked = dependency_summary_blocked or _dependency_summary_blocked
    artifact_ref = dependency_summary_artifact_ref or _dependency_summary_artifact_ref
    try:
        summary_path = find_path(name, root)
        raw = read_bytes_limited_no_follow(summary_path, max_bytes=MAX_RECEIPT_BYTES)
        if len(raw) > MAX_RECEIPT_BYTES:
            return blocked(
                name,
                summary_path,
                config=config,
                reason="Dependency summary exceeds bounded readiness ingestion limit.",
            )
        summary = json.loads(raw.decode("utf-8"))
        if not isinstance(summary, Mapping):
            return blocked(
                name,
                summary_path,
                config=config,
                reason="Dependency summary JSON must be an object.",
            )
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        RecursionError,
        UnicodeDecodeError,
        SafeFilesystemError,
        _readiness_item_contracts.ProductionReadinessValidationError,
    ) as error:
        return blocked(
            name,
            root,
            config=config,
            reason=f"Dependency summary could not be read: {_redact_paths(str(error), config=config)}.",
        )
    status = str(summary.get("status", "unknown"))
    schema_ok = summary.get("schema") == contract["schema"]
    issue_ok = summary.get("issue") == contract["issue"]
    accepted_status = status in contract["allowed_statuses"]
    summary_run_id = summary.get("run_id")
    run_id_ok = _dependency_summary_run_id_is_stable(summary_run_id)
    item_status = "passed" if schema_ok and issue_ok and accepted_status and run_id_ok else "blocked"
    summary_checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    producer_artifact_ref = artifact_ref(name, summary_path, root)
    public_status = _dependency_summary_public_status(status, config=config)
    return _item(
        item_id=f"deterministic-{name}-summary",
        surface=f"{name}_production_like_evidence",
        status=item_status,
        execution_mode="deterministic" if item_status == "passed" else "not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(summary_path, config=config)],
        residual_risk=(
            "Existing production-closure summary was consumed as deterministic review evidence; it is not live proof."
            if item_status == "passed"
            else "Existing production-closure summary is missing, malformed, or outside the expected contract."
        ),
        removal_criteria=(
            "Provide accepted live proof receipt for final readiness; keep deterministic producer evidence available "
            "for reviewer lineage."
            if item_status == "passed"
            else f"Provide a {contract['schema']} summary with accepted status for deterministic readiness review."
        ),
        dependencies=[
            f"issue=#{contract['issue']}",
            f"schema={contract['schema']}",
            f"summary_status={public_status}",
            f"producer_artifact_ref={producer_artifact_ref}",
            f"summary_checksum={summary_checksum}",
        ],
        details=_bounded_redacted_payload(
            {
                "dependency": name,
                "producer_issue": contract["issue"],
                "producer_schema": contract["schema"],
                "summary_schema": summary.get("schema"),
                "summary_issue": summary.get("issue"),
                "summary_run_id": _dependency_summary_public_run_id(summary_run_id, config=config),
                "summary_status": status,
                "summary_execution_mode": summary.get("execution_mode"),
                "summary_final_production_readiness_claimed": summary.get("final_production_readiness_claimed"),
                "producer_artifact_ref": producer_artifact_ref,
                "summary_checksum": summary_checksum,
            },
            config=config,
        ),
    )


def _dependency_summary_public_status(status: str, *, config: Any) -> str:
    redacted_status = _bounded_redacted_payload(status, config=config)
    return redacted_status if isinstance(redacted_status, str) else str(redacted_status)


def _dependency_summary_run_id_is_stable(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_STRING_LENGTH
        and bool(_DEPENDENCY_SUMMARY_RUN_ID_RE.fullmatch(value))
        and not PATH_TOKEN_RE.search(value)
    )


def _dependency_summary_public_run_id(value: Any, *, config: Any) -> Any:
    if _dependency_summary_run_id_is_stable(value):
        return value
    if value is None:
        return None
    if not isinstance(value, str):
        return _bounded_redacted_payload(value, config=config)
    if _dependency_summary_run_id_looks_path_like(value):
        suffix = "[truncated]" if len(value) > MAX_STRING_LENGTH else ""
        return f"[redacted-path]{suffix}"
    if len(value) > MAX_STRING_LENGTH:
        return "[invalid-run-id][truncated]"
    return "[invalid-run-id]"


def _dependency_summary_run_id_looks_path_like(value: str) -> bool:
    return "/" in value or "\\" in value or ".." in value or bool(PATH_TOKEN_RE.search(value))


def _dependency_summary_blocked(
    name: str,
    path: Path,
    *,
    config: Any,
    reason: str,
) -> dict[str, Any]:
    return _item(
        item_id=f"deterministic-{name}-summary",
        surface=f"{name}_production_like_evidence",
        status="blocked",
        execution_mode="not_executed",
        required_for_final=False,
        live_proof_accepted=False,
        artifact_refs=[_path_for_evidence(path, config=config)],
        residual_risk=reason,
        removal_criteria=f"Provide a readable bounded {name} production-closure summary.json artifact.",
    )


def _dependency_summary_artifact_ref(name: str, summary_path: Path, root: Path) -> str:
    try:
        relative = summary_path.resolve(strict=False).relative_to(root.expanduser().resolve(strict=False))
    except ValueError:
        relative = Path("summary.json")
    return f"{name}:{relative.as_posix()}"


def _dependency_bindings(items: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    bindings: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if item.get("status") != "passed":
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            continue
        dependency = details.get("dependency")
        if (
            isinstance(dependency, str)
            and dependency in DEPENDENCY_SUMMARY_CONTRACTS
            and _dependency_summary_run_id_is_stable(details.get("summary_run_id"))
        ):
            bindings[dependency] = details
    return bindings


def _find_summary_path(name: str, root: Path) -> Path:
    root = root.expanduser()
    candidates = [root / "summary.json", root / name / "summary.json"]
    if name == "object_store":
        candidates.append(root / "object-store" / "summary.json")
    for candidate in candidates:
        if candidate.exists():
            _refuse_symlink_components(candidate)
            if candidate.is_symlink():
                raise SafeFilesystemError(f"Dependency summary must not be a symlink: {candidate}")
            try:
                file_stat = candidate.stat(follow_symlinks=False)
            except OSError as error:
                raise SafeFilesystemError(f"Failed to stat dependency summary: {candidate}", kind="io") from error
            if not stat.S_ISREG(file_stat.st_mode):
                raise SafeFilesystemError(f"Dependency summary must be a regular file: {candidate}")
            return candidate
    raise FileNotFoundError(f"No summary.json found under {root}")


def _item(
    *,
    item_id: str,
    surface: str,
    status: str,
    execution_mode: str,
    required_for_final: bool,
    live_proof_accepted: bool,
    artifact_refs: Sequence[str],
    residual_risk: str,
    removal_criteria: str,
    exclusions: Sequence[Mapping[str, Any]] = (),
    dependencies: Sequence[str] = (),
    details: Mapping[str, Any] | None = None,
    owner: str = "release_owner",
    action: str | None = None,
) -> dict[str, Any]:
    item = {
        "item_id": item_id,
        "surface": surface,
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": required_for_final,
        "live_proof_accepted": live_proof_accepted,
        "artifact_refs": list(artifact_refs),
        "residual_risk": residual_risk,
        "removal_criteria": removal_criteria,
        "exclusions": [dict(exclusion) for exclusion in exclusions],
        "dependencies": list(dependencies),
        "owner": owner,
        "action": action or removal_criteria,
    }
    if details is not None:
        item["details"] = dict(details)
    validate_readiness_item(item)
    return item
