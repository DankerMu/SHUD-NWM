"""Finding aggregation: dedupe, record shape, allowlist keys, heatmap, spread.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Turns the
``FindingSpec`` list every check family produces into the report's ordered
records, the summary counts, the per-module heatmap and the high-spread pattern
rollup."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from scripts.governance.entropy_audit.constants import (
    AXES,
    HARD_GATE_CHECK_IDS,
    PRIORITY_RANK,
    SCORE_RANK,
    SEVERITY_RANK,
)
from scripts.governance.entropy_audit.schema import AllowlistState, FindingSpec


def _dedupe_findings(findings: Iterable[FindingSpec]) -> list[FindingSpec]:
    seen: set[tuple[object, ...]] = set()
    result: list[FindingSpec] = []
    for finding in findings:
        key = (
            finding.check_id,
            finding.evidence_path,
            finding.line,
            finding.title,
            finding.allowlist_reason,
            finding.description,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def _finding_record(index: int, finding: FindingSpec) -> dict[str, object]:
    axis_scores = {axis: "low" for axis in AXES}
    axis_scores[finding.axis] = finding.severity
    allowlist_key = _allowlist_key(finding)
    allowlist_state = _allowlist_state(allowlist_key)
    budget_counted = allowlist_state == "unallowlisted"
    gate_eligible = budget_counted and finding.check_id in HARD_GATE_CHECK_IDS
    return {
        "id": f"ENT-{index:04d}",
        "check_id": finding.check_id,
        "title": finding.title,
        "axis": finding.axis,
        "axis_scores": axis_scores,
        "governance_face": finding.governance_face,
        "role": finding.role,
        "evidence_path": finding.evidence_path,
        "line": finding.line,
        "severity": finding.severity,
        "priority": finding.priority,
        "owner_area": finding.owner_area,
        "module": finding.module,
        "allowlist_reason": finding.allowlist_reason,
        "allowlist_key": allowlist_key,
        "allowlist_state": allowlist_state,
        "budget_counted": budget_counted,
        "gate_eligible": gate_eligible,
        "description": finding.description,
        "recommendation": finding.recommendation,
    }


def _allowlist_state(allowlist_key: str | None) -> AllowlistState:
    return "allowlisted" if allowlist_key else "unallowlisted"


def _allowlist_key(finding: FindingSpec) -> str | None:
    reason = finding.allowlist_reason
    if reason is None or not reason.strip():
        return None
    reason_key = _allowlist_reason_key(finding.check_id, reason)
    return f"{finding.check_id}:{reason_key}"


def _allowlist_reason_key(check_id: str, reason: str) -> str:
    normalized = _normalized_reason_text(reason)
    tokens = set(normalized.split())
    if check_id == "broad-e2e-api-mock" and tokens & {"deterministic", "mock", "mocked", "preview", "visual"}:
        return "deterministic-mocked-preview-visual"
    if check_id == "stale-display-route-token":
        if {"complete", "archive", "status", "marker"} <= tokens:
            return "complete-archive-status-marker"
        if "milestone" in tokens or "progress" in tokens:
            return "historical-milestone-summary"
        if "provenance" in tokens or "extraction" in tokens:
            return "library-extraction-provenance"
        if "compatibility" in tokens or "compatible" in tokens:
            return "legacy-route-compatibility-context"
        if "historical" in tokens or "pre" in tokens or "plans" in tokens:
            return "historical-plan-or-pre-m26-evidence"
        if "m26" in tokens or "redirect" in tokens:
            return "m26-route-consolidation-or-redirect"
    if check_id == "placeholder-path-token" and {"governance", "inventory"} <= tokens:
        return "governance-retired-placeholder-inventory"
    if check_id == "placeholder-path-token" and {"complete", "archive", "status", "marker"} <= tokens:
        return "complete-archive-status-marker"
    if check_id == "placeholder-path-token" and {"governed", "archived", "evidence"} <= tokens:
        return "governed-archived-retired-placeholder-evidence"
    if check_id == "placeholder-path-token" and {"governed", "completed", "openspec", "evidence"} <= tokens:
        return "governed-completed-openspec-retired-placeholder-evidence"
    if check_id == "openapi-frontend-types-delegated":
        return "existing-contract-oracle-delegation"
    if check_id == "openapi-frontend-types-signal":
        if "skipped" in tokens:
            return "report-only-fingerprint-skipped"
        if "fingerprint" in tokens:
            return "report-only-fingerprint-record"
    return _slug(normalized)


def _normalized_reason_text(reason: str) -> str:
    text = reason.lower()
    text = text.replace("report only", "report-only")
    text = text.replace("allow listed", "allowlisted")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _slug(text: str) -> str:
    return "-".join(text.split()) or "unspecified"


def _summary_counts(findings: list[dict[str, object]]) -> dict[str, object]:
    return {
        "by_check_id": _count_by(findings, "check_id"),
        "by_priority": _count_by(findings, "priority"),
        "by_role": _count_by(findings, "role"),
        "by_allowlist_state": _count_by_with_defaults(
            findings,
            "allowlist_state",
            ("allowlisted", "unallowlisted"),
        ),
        "by_gate_eligibility": _boolean_count_by(
            findings,
            "gate_eligible",
            true_key="gate_eligible",
            false_key="not_gate_eligible",
        ),
        "by_budget_count": _boolean_count_by(
            findings,
            "budget_counted",
            true_key="budget_counted",
            false_key="not_budget_counted",
        ),
    }


def _count_by(findings: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = str(finding[field])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _count_by_with_defaults(
    findings: list[dict[str, object]],
    field: str,
    defaults: tuple[str, ...],
) -> dict[str, int]:
    counts = {key: 0 for key in defaults}
    counts.update(_count_by(findings, field))
    return counts


def _boolean_count_by(
    findings: list[dict[str, object]],
    field: str,
    *,
    true_key: str,
    false_key: str,
) -> dict[str, int]:
    counts = {true_key: 0, false_key: 0}
    for finding in findings:
        counts[true_key if bool(finding[field]) else false_key] += 1
    return counts


def _module_heatmap(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    modules: dict[str, dict[str, object]] = {}
    for finding in findings:
        module = str(finding["module"])
        row = modules.setdefault(
            module,
            {
                "module": module,
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
        )
        row["finding_count"] = int(row["finding_count"]) + 1
        axis = str(finding["axis"])
        severity = str(finding["severity"])
        if SCORE_RANK[severity] > SCORE_RANK[str(row[axis])]:
            row[axis] = severity
        priority = str(finding["priority"])
        if PRIORITY_RANK[priority] > PRIORITY_RANK[str(row["priority"])]:
            row["priority"] = priority
    return sorted(
        modules.values(),
        key=lambda row: (-PRIORITY_RANK[str(row["priority"])], -int(row["finding_count"]), str(row["module"])),
    )


def _high_spread_patterns(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for finding in findings:
        grouped[str(finding["check_id"])].append(finding)
    patterns: list[dict[str, object]] = []
    for check_id, group in grouped.items():
        modules = sorted({str(finding["module"]) for finding in group})
        if len(group) < 2 and len(modules) < 2:
            continue
        priorities = [str(finding["priority"]) for finding in group]
        severities = [str(finding["severity"]) for finding in group]
        patterns.append(
            {
                "pattern": check_id,
                "occurrence_count": len(group),
                "module_count": len(modules),
                "modules": modules,
                "roles": sorted({str(finding["role"]) for finding in group}),
                "governance_faces": sorted({str(finding["governance_face"]) for finding in group}),
                "top_priority": max(priorities, key=lambda item: PRIORITY_RANK[item]),
                "top_severity": max(severities, key=lambda item: SEVERITY_RANK[item]),
            }
        )
    return sorted(
        patterns,
        key=lambda item: (
            -PRIORITY_RANK[str(item["top_priority"])],
            -int(item["occurrence_count"]),
            str(item["pattern"]),
        ),
    )
