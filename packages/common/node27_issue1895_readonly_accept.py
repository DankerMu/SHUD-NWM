"""C2 canonical denied-write evidence acceptance and exact-SHA receipt binding."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from packages.common.node27_issue1895_private_receipt import (
    MAX_INPUT_BYTES,
    assert_bracket,
    assert_timestamp_order,
    private_parent_facts,
    publish_private_receipt,
    read_authoritative_json,
    read_private_receipt,
    refuse,
    require_matching_shas,
    require_sha,
    utc_now,
)
from services.production_closure.readonly_db_validation import (
    AUTHORITATIVE_EVIDENCE_FILENAMES,
    LIVE_EVIDENCE_SCHEMA,
)

ARTIFACT = "nhms-issue1895-c2-readonly-boundary"
SCHEMA_VERSION = "1.0"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FULL_SCOPE_SOURCES = frozenset({"GFS", "IFS"})
CANONICAL_ROLE = {"current_user": "nhms_display_ro", "role_type": "readonly_candidate"}
FULL_SCOPE_FACT_KEYS = frozenset(
    {
        "merged_source_evidence",
        "declared_sources",
        "reduced_scope",
        "source_bundle_count",
        "source_artifact_sources",
        "display_identity_sources",
    }
)
REQUIRED_KEYS = (
    "artifact",
    "schema_version",
    "status",
    "head_sha",
    "reviewed_sha",
    "run_id",
    "started_at",
    "ended_at",
    "generated_at",
    "canonical_summary",
    "authoritative_files",
    "checks",
)


def _error(message: str, code: str) -> None:
    refuse(message, code, stage="c2")


def canonical_lane_dir(evidence_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        _error("C2 run_id is not a safe current run identifier", "C2_RUN_ID_INVALID")
    return evidence_root / run_id / "db" / "readonly-db-boundary"


def _read_authoritative(lane_dir: Path, filename: str) -> tuple[bytes, Any, dict[str, int]]:
    return read_authoritative_json(
        lane_dir / filename,
        containment_root=lane_dir,
        label=f"C2 {filename}",
        max_bytes=MAX_INPUT_BYTES,
        require_private_file=True,
        require_mapping=filename in {"summary.json", "role.json"},
        stage="c2",
    )


def _full_scope_facts(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Project the closed GFS/IFS merge facts from canonical summary evidence."""

    provenance = summary.get("validation_provenance")
    display_identity = summary.get("display_identity")
    if not isinstance(provenance, Mapping) or not isinstance(display_identity, Mapping):
        _error("canonical C2 summary lacks full GFS/IFS scope facts", "C2_SUMMARY_SCOPE")
    source_artifacts = provenance.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        _error("canonical C2 summary lacks source artifact facts", "C2_SUMMARY_SCOPE")
    artifact_sources: list[list[str]] = []
    for artifact in source_artifacts:
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("sources"), list):
            _error("canonical C2 source artifact facts are invalid", "C2_SUMMARY_SCOPE")
        artifact_sources.append(sorted(str(source) for source in artifact["sources"]))
    return {
        "merged_source_evidence": provenance.get("merged_source_evidence"),
        "declared_sources": sorted(str(source) for source in provenance.get("declared_sources", [])),
        "reduced_scope": provenance.get("reduced_scope"),
        "source_bundle_count": provenance.get("source_bundle_count"),
        "source_artifact_sources": sorted(artifact_sources),
        "display_identity_sources": sorted(display_identity),
    }


def _validate_full_scope_facts(value: object, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != FULL_SCOPE_FACT_KEYS:
        _error("C2 full-scope facts are not closed", code)
    declared_sources = value.get("declared_sources")
    source_artifact_sources = value.get("source_artifact_sources")
    display_identity_sources = value.get("display_identity_sources")
    if (
        value.get("merged_source_evidence") is not True
        or value.get("reduced_scope") is not False
        or value.get("source_bundle_count") != len(FULL_SCOPE_SOURCES)
        or declared_sources != ["GFS", "IFS"]
        or source_artifact_sources != [["GFS"], ["IFS"]]
        or display_identity_sources != ["GFS", "IFS"]
    ):
        _error("C2 full-scope facts are not exact GFS/IFS merge facts", code)
    return dict(value)


def _validate_canonical_summary(
    summary: Mapping[str, Any],
    *,
    run_id: str,
    siblings: Mapping[str, Any],
) -> dict[str, Any]:
    if summary.get("schema") != LIVE_EVIDENCE_SCHEMA:
        _error("canonical C2 summary is not live schema evidence", "C2_SUMMARY_SCHEMA")
    if summary.get("status") != "PASS":
        _error("canonical C2 summary is not PASS", "C2_SUMMARY_STATUS")
    if summary.get("run_id") != run_id:
        _error("canonical C2 summary run_id differs", "C2_SUMMARY_RUN")
    provenance = summary.get("validation_provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("mode") != "live"
        or provenance.get("live_readonly_proof") is not True
    ):
        _error("canonical C2 summary lacks live readonly provenance", "C2_SUMMARY_PROVENANCE")
    if (
        provenance.get("merged_source_evidence") is not True
        or provenance.get("reduced_scope") is not False
        or not isinstance(provenance.get("declared_sources"), list)
        or frozenset(provenance["declared_sources"]) != FULL_SCOPE_SOURCES
        or len(provenance["declared_sources"]) != len(FULL_SCOPE_SOURCES)
        or provenance.get("source_bundle_count") != len(FULL_SCOPE_SOURCES)
        or not isinstance(provenance.get("source_artifacts"), list)
        or len(provenance["source_artifacts"]) != len(FULL_SCOPE_SOURCES)
    ):
        _error("canonical C2 summary is not a full GFS/IFS merged scope", "C2_SUMMARY_SCOPE")
    display_identity = summary.get("display_identity")
    if not isinstance(display_identity, Mapping) or set(display_identity) != FULL_SCOPE_SOURCES:
        _error("canonical C2 summary lacks full GFS/IFS display identity", "C2_SUMMARY_SCOPE")
    role = summary.get("role")
    if not isinstance(role, Mapping) or {key: role.get(key) for key in CANONICAL_ROLE} != CANONICAL_ROLE:
        _error("canonical C2 summary readonly role differs", "C2_SUMMARY_ROLE")
    runtime = summary.get("runtime")
    if (
        not isinstance(runtime, Mapping)
        or runtime.get("service_role") != "display_readonly"
        or runtime.get("control_mutations_expected") is not False
    ):
        _error("canonical C2 summary runtime differs", "C2_SUMMARY_RUNTIME")
    permission_probes = summary.get("permission_probes")
    if not isinstance(permission_probes, list) or not permission_probes:
        _error("canonical C2 permission matrix is missing", "C2_SUMMARY_PROBES")
    operations = [
        operation
        for probe in permission_probes
        if isinstance(probe, Mapping) and isinstance(probe.get("operations"), list)
        for operation in probe["operations"]
        if isinstance(operation, Mapping)
    ]
    if not operations or any(operation.get("status") != "PASS" for operation in operations):
        _error("canonical C2 permission matrix is not denied-write PASS", "C2_SUMMARY_PROBES")
    expected = {
        "role.json": summary.get("role"),
        "route_smoke.json": summary.get("route_smoke"),
        "permission_probes.json": summary.get("permission_probes"),
    }
    for filename, expected_payload in expected.items():
        if siblings.get(filename) != expected_payload:
            _error("canonical C2 authoritative sibling differs from summary", "C2_SIBLING_MISMATCH")
    sibling_role = siblings.get("role.json")
    if not isinstance(sibling_role, Mapping) or {
        key: sibling_role.get(key) for key in CANONICAL_ROLE
    } != CANONICAL_ROLE:
        _error("canonical C2 role evidence differs", "C2_SUMMARY_ROLE")
    return _validate_full_scope_facts(_full_scope_facts(summary), code="C2_SUMMARY_SCOPE")


def accept_c2_evidence(
    *,
    evidence_root: Path,
    run_id: str,
    receipt_path: Path,
    head_sha: str,
    reviewed_sha: str,
    now: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Read the canonical validator's four authoritative files and bind exact SHA."""

    expected_sha = require_matching_shas(head_sha, reviewed_sha, stage="c2")
    started_at = now()
    lane_dir = canonical_lane_dir(evidence_root, run_id)
    payloads: dict[str, Any] = {}
    records: dict[str, dict[str, int | str]] = {}
    for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
        raw, payload, facts = _read_authoritative(lane_dir, filename)
        del raw
        payloads[filename] = payload
        records[filename] = facts
    summary = payloads["summary.json"]
    full_scope = _validate_canonical_summary(summary, run_id=run_id, siblings=payloads)
    ended_at = now()
    document: dict[str, Any] = {
        "artifact": ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "head_sha": expected_sha,
        "reviewed_sha": expected_sha,
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "generated_at": ended_at,
        "canonical_summary": records["summary.json"],
        "authoritative_files": records,
        "checks": {
            "summary_live_schema": True,
            "summary_pass": True,
            "summary_live_provenance": True,
            "summary_full_gfs_ifs_scope": full_scope,
            "summary_run_id": True,
            "readonly_role": True,
            "siblings_match": True,
        },
    }
    validate_c2_receipt(document)
    publish_private_receipt(receipt_path, document, code_prefix="C2_RECEIPT", stage="c2")
    return document


def _validate_file_facts(value: object, *, label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "st_dev",
        "st_ino",
        "st_uid",
        "st_mode",
        "st_nlink",
        "st_size",
        "sha256",
    }:
        _error(f"{label} facts are not closed", "C2_RECEIPT_FACTS")
    if any(
        isinstance(value.get(key), bool) or not isinstance(value.get(key), int) or int(value[key]) < 0
        for key in ("st_dev", "st_ino", "st_uid", "st_mode", "st_nlink", "st_size")
    ):
        _error(f"{label} facts are invalid", "C2_RECEIPT_FACTS")
    if value.get("st_uid") != os.geteuid() or value.get("st_mode") != 0o600 or value.get("st_nlink") != 1:
        _error(f"{label} is not private canonical evidence", "C2_RECEIPT_FACTS")
    if re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256") or "")) is None:
        _error(f"{label} digest is invalid", "C2_RECEIPT_FACTS")


def validate_c2_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != set(REQUIRED_KEYS):
        _error("C2 receipt keys are not closed", "C2_RECEIPT_KEYS")
    if (
        document.get("artifact") != ARTIFACT
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("status") != "PASS"
    ):
        _error("C2 receipt artifact/schema/status differs", "C2_RECEIPT_SCHEMA")
    require_matching_shas(document.get("head_sha"), document.get("reviewed_sha"), stage="c2")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        _error("C2 receipt run_id invalid", "C2_RECEIPT_RUN")
    assert_timestamp_order(document, stage="c2")
    _validate_file_facts(document.get("canonical_summary"), label="canonical summary")
    files = document.get("authoritative_files")
    if not isinstance(files, Mapping) or set(files) != set(AUTHORITATIVE_EVIDENCE_FILENAMES):
        _error("C2 receipt authoritative file set differs", "C2_RECEIPT_FILES")
    for filename in AUTHORITATIVE_EVIDENCE_FILENAMES:
        _validate_file_facts(files[filename], label=filename)
    if files["summary.json"] != document.get("canonical_summary"):
        _error("C2 receipt canonical summary facts differ", "C2_RECEIPT_FILES")
    checks = document.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != {
        "summary_live_schema",
        "summary_pass",
        "summary_live_provenance",
        "summary_full_gfs_ifs_scope",
        "summary_run_id",
        "readonly_role",
        "siblings_match",
    }:
        _error("C2 receipt checks differ", "C2_RECEIPT_CHECKS")
    for key in (
        "summary_live_schema",
        "summary_pass",
        "summary_live_provenance",
        "summary_run_id",
        "readonly_role",
        "siblings_match",
    ):
        if checks.get(key) is not True:
            _error("C2 receipt checks differ", "C2_RECEIPT_CHECKS")
    _validate_full_scope_facts(checks.get("summary_full_gfs_ifs_scope"), code="C2_RECEIPT_SCOPE")
    return dict(document)


def bind_c2_receipt(
    receipt_path: Path,
    *,
    evidence_root: Path,
    run_id: str,
    reviewed_sha: str,
    cmd_start: str,
    cmd_end: str,
) -> dict[str, Any]:
    expected_sha = require_sha(reviewed_sha, label="reviewed_sha", stage="c2")
    raw, document, info = read_private_receipt(receipt_path, code_prefix="C2_RECEIPT", stage="c2")
    del raw
    private_parent_facts(receipt_path, code_prefix="C2_RECEIPT")
    validated = validate_c2_receipt(document)
    if validated["head_sha"] != expected_sha or validated["reviewed_sha"] != expected_sha:
        _error("C2 receipt SHA does not bind reviewed SHA", "C2_BIND_SHA")
    if validated["run_id"] != run_id:
        _error("C2 receipt run_id does not bind current invocation", "C2_BIND_RUN")
    lane_dir = canonical_lane_dir(evidence_root, run_id)
    payloads: dict[str, Any] = {}
    for filename, expected in validated["authoritative_files"].items():
        raw_input, payload, observed = _read_authoritative(lane_dir, filename)
        if hashlib.sha256(raw_input).hexdigest() != expected["sha256"] or observed != expected:
            _error("C2 canonical evidence changed after acceptance", "C2_BIND_TAMPER")
        payloads[filename] = payload
    observed_scope = _validate_canonical_summary(payloads["summary.json"], run_id=run_id, siblings=payloads)
    if observed_scope != validated["checks"]["summary_full_gfs_ifs_scope"]:
        _error("C2 full-scope facts changed after acceptance", "C2_BIND_SCOPE_TAMPER")
    assert_bracket(info=info, document=validated, cmd_start=cmd_start, cmd_end=cmd_end, stage="c2")
    return validated


__all__ = (
    "ARTIFACT",
    "SCHEMA_VERSION",
    "accept_c2_evidence",
    "bind_c2_receipt",
    "canonical_lane_dir",
    "validate_c2_receipt",
)
