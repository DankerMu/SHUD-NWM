"""The report composition, markdown rendering and CLI of the entropy audit.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Owns
``build_report`` (the one entrypoint ``scripts/governance/write_entropy_baseline.py``
calls), ``_collect_findings`` (the check-family roster), the metadata block, the
hard-gate exit rule, ``render_markdown`` and ``main``. The historical module path
stays the console entrypoint and re-exports these names."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from scripts.governance.entropy_audit.check_env_and_tokens import (
    _check_broad_e2e_mocks,
    _check_display_env_boundaries,
    _check_paused_workflows,
    _check_qhh_diagnostic_tokens,
)
from scripts.governance.entropy_audit.check_paths_and_api import (
    _check_agent_artifact_ownership,
    _check_apps_api_layer_inversion,
    _check_makefile_toolchain,
    _check_openapi_frontend_type_drift,
    _check_placeholder_paths,
    _check_slurm_gateway_route_leakage,
)
from scripts.governance.entropy_audit.check_stale_routes import _check_stale_route_tokens
from scripts.governance.entropy_audit.check_topology import _check_production_topology_drift
from scripts.governance.entropy_audit.constants import (
    CHECK_FAMILIES,
    HARD_GATE_CHECK_IDS,
    MAX_ARTIFACT_FINGERPRINT_BYTES,
    MAX_SCANNED_TEXT_FILE_BYTES,
    PRIORITY_RANK,
    SEVERITY_RANK,
    STRUCTURAL_BUDGET_BASE_REF_ENV,
)
from scripts.governance.entropy_audit.facade_guard import (
    _compatibility_facade_guard_findings,
    _compatibility_facade_guard_markdown_lines,
    _compatibility_facade_guard_summary,
)
from scripts.governance.entropy_audit.findings import (
    _dedupe_findings,
    _finding_record,
    _high_spread_patterns,
    _module_heatmap,
    _summary_counts,
)
from scripts.governance.entropy_audit.schema import AuditMode, FindingSpec
from scripts.governance.entropy_audit.scoped_context import (
    _scoped_agent_context_findings,
    _scoped_agent_context_markdown_lines,
    _scoped_agent_context_summary,
)
from scripts.governance.entropy_audit.structural_budget import (
    _structural_file_budget_markdown_lines,
    _structural_file_budget_summary,
)


def repo_root_from(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / ".git").exists():
            return candidate
    return start


def build_report(
    repo_root: Path | None = None,
    *,
    mode: AuditMode = "report",
    structural_base_ref: str | None = None,
) -> dict[str, object]:
    root = repo_root_from(repo_root)
    compatibility_facade_guard = _compatibility_facade_guard_summary(
        root,
        structural_base_ref=structural_base_ref,
    )
    scoped_agent_context = _scoped_agent_context_summary(root)
    findings = sorted(
        _dedupe_findings(
            [
                *_collect_findings(root),
                *_compatibility_facade_guard_findings(compatibility_facade_guard),
                *_scoped_agent_context_findings(scoped_agent_context),
            ]
        ),
        key=lambda item: (
            -PRIORITY_RANK[item.priority],
            -SEVERITY_RANK[item.severity],
            item.check_id,
            item.evidence_path,
            item.line or 0,
        ),
    )
    finding_records = [_finding_record(index, finding) for index, finding in enumerate(findings, start=1)]
    structural_file_budget = _structural_file_budget_summary(
        root,
        structural_base_ref=structural_base_ref,
    )
    return {
        "metadata": _metadata(
            root,
            finding_records,
            mode=mode,
            structural_file_budget=structural_file_budget,
            compatibility_facade_guard=compatibility_facade_guard,
            scoped_agent_context=scoped_agent_context,
        ),
        "module_heatmap": _module_heatmap(finding_records),
        "findings": finding_records,
        "high_spread_patterns": _high_spread_patterns(finding_records),
    }


def render_markdown(report: dict[str, object]) -> str:
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    heatmap = report["module_heatmap"]
    findings = report["findings"]
    patterns = report["high_spread_patterns"]
    assert isinstance(heatmap, list)
    assert isinstance(findings, list)
    assert isinstance(patterns, list)

    lines = [
        "# Repository Entropy Audit",
        "",
        f"- Mode: `{metadata['mode']}`",
    ]
    if metadata["mode"] == "hard-gate":
        lines.extend(
            [
                f"- Hard gate status: `{metadata['hard_gate_status']}`",
                f"- Hard gate failing findings: `{metadata['hard_gate_failing_count']}`",
                "- Hard gate gated check IDs: `"
                + "`, `".join(str(check_id) for check_id in metadata["hard_gate_gated_check_ids"])
                + "`",
            ]
        )
    lines.extend(
        [
            f"- Generated: `{metadata['generated_at']}`",
            f"- Baseline path: `{metadata['baseline_path']}`",
            f"- Baseline written: `{str(metadata['baseline_written']).lower()}`",
            f"- Findings: `{metadata['finding_count']}`",
            f"- Budget-counted findings: `{metadata['budget_counted_count']}`",
            f"- Gate-eligible findings: `{metadata['gate_eligible_count']}`",
        ]
    )
    lines.extend(_structural_file_budget_markdown_lines(metadata.get("structural_file_budget")))
    lines.extend(_compatibility_facade_guard_markdown_lines(metadata.get("compatibility_facade_guard")))
    lines.extend(_scoped_agent_context_markdown_lines(metadata.get("scoped_agent_context")))
    lines.extend(
        [
            "",
            "## Entropy Heatmap",
            "",
            "| Module | Structure | Semantics | Behavior | Context | Protocol | Control | Priority | Findings |",
            "|---|---:|---:|---:|---:|---:|---:|---|---:|",
        ]
    )
    for row in heatmap:
        assert isinstance(row, dict)
        lines.append(
            "| {module} | {structure} | {semantics} | {behavior} | {context} | {protocol} | "
            "{control} | {priority} | {finding_count} |".format(**row)
        )

    lines.extend(["", "## High-Spread Patterns", ""])
    if patterns:
        for pattern in patterns:
            assert isinstance(pattern, dict)
            lines.append(
                "- **{pattern}**: {occurrence_count} findings across {module_count} modules; "
                "top priority `{top_priority}`; roles `{roles}`.".format(
                    pattern=pattern["pattern"],
                    occurrence_count=pattern["occurrence_count"],
                    module_count=pattern["module_count"],
                    top_priority=pattern["top_priority"],
                    roles=", ".join(pattern["roles"]) if pattern["roles"] else "none",
                )
            )
    else:
        lines.append("- No replicated patterns detected.")

    lines.extend(["", "## Prioritized Cleanup Targets", ""])
    if findings:
        for finding in findings[:20]:
            assert isinstance(finding, dict)
            location = finding["evidence_path"]
            if finding.get("line"):
                location = f"{location}:{finding['line']}"
            state = str(finding["allowlist_state"])
            gate = "gate-eligible" if finding["gate_eligible"] else "not gate-eligible"
            lines.append(
                "- `{priority}` `{severity}` **{title}** ({axis}, {role}) at `{location}`: "
                "{recommendation} [{state}; {gate}]".format(
                    priority=finding["priority"],
                    severity=finding["severity"],
                    title=finding["title"],
                    axis=finding["axis"],
                    role=finding["role"],
                    location=location,
                    recommendation=finding["recommendation"],
                    state=state,
                    gate=gate,
                )
            )
    else:
        lines.append("- No cleanup targets detected.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a repository entropy audit.")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument(
        "--mode",
        choices=("report", "hard-gate"),
        default="report",
        help="Run in report-only mode by default, or explicitly evaluate prepared hard-gate findings.",
    )
    parser.add_argument(
        "--structural-base-ref",
        default=None,
        help=(
            "Compare structural ownership growth against this git ref. "
            f"Defaults to ${STRUCTURAL_BUDGET_BASE_REF_ENV}, then origin/master merge-base, "
            "HEAD^, and local-only HEAD fallback outside CI."
        ),
    )
    args = parser.parse_args(argv)

    report = build_report(mode=args.mode, structural_base_ref=args.structural_base_ref)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
    return _exit_code_for_report(report)


def _metadata(
    root: Path,
    findings: list[dict[str, object]],
    *,
    mode: AuditMode,
    structural_file_budget: dict[str, object],
    compatibility_facade_guard: dict[str, object],
    scoped_agent_context: dict[str, object],
) -> dict[str, object]:
    baseline_path = ".entropy-baseline/latest.json"
    summary_counts = _summary_counts(findings)
    metadata: dict[str, object] = {
        "schema_version": "governance-4a.entropy-report.v1",
        "mode": "hard-gate" if mode == "hard-gate" else "report-only",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repo_root": root.as_posix(),
        "baseline_path": baseline_path,
        "baseline_exists": (root / baseline_path).exists(),
        "baseline_written": False,
        "finding_count": len(findings),
        "check_family_count": len({str(finding["check_id"]) for finding in findings}),
        "budget_counted_count": int(summary_counts["by_budget_count"]["budget_counted"]),
        "gate_eligible_count": int(summary_counts["by_gate_eligibility"]["gate_eligible"]),
        "summary_counts": summary_counts,
        "structural_file_budget": structural_file_budget,
        "compatibility_facade_guard": compatibility_facade_guard,
        "scoped_agent_context": scoped_agent_context,
        "max_scanned_text_file_bytes": MAX_SCANNED_TEXT_FILE_BYTES,
        "max_artifact_fingerprint_bytes": MAX_ARTIFACT_FINGERPRINT_BYTES,
        "executed_check_families": list(CHECK_FAMILIES),
        "skipped_path_families": sorted(
            [
                ".git",
                ".venv",
                "node_modules",
                "dist",
                "artifacts",
                "data",
                ".nhms-*",
                "caches",
            ]
        ),
    }
    if mode == "hard-gate":
        failing_count = _hard_gate_failing_count(findings)
        metadata.update(
            {
                "hard_gate_status": "fail" if failing_count else "pass",
                "hard_gate_gated_check_ids": list(HARD_GATE_CHECK_IDS),
                "hard_gate_failing_count": failing_count,
            }
        )
    return metadata


def _hard_gate_failing_count(findings: Iterable[dict[str, object]]) -> int:
    return sum(1 for finding in findings if bool(finding["gate_eligible"]))


def _exit_code_for_report(report: dict[str, object]) -> int:
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    if metadata.get("mode") != "hard-gate":
        return 0
    return 1 if int(metadata["hard_gate_failing_count"]) > 0 else 0


def _collect_findings(root: Path) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    findings.extend(_check_display_env_boundaries(root))
    findings.extend(_check_production_topology_drift(root))
    findings.extend(_check_qhh_diagnostic_tokens(root))
    findings.extend(_check_paused_workflows(root))
    findings.extend(_check_broad_e2e_mocks(root))
    findings.extend(_check_stale_route_tokens(root))
    findings.extend(_check_placeholder_paths(root))
    findings.extend(_check_makefile_toolchain(root))
    findings.extend(_check_openapi_frontend_type_drift(root))
    findings.extend(_check_slurm_gateway_route_leakage(root))
    findings.extend(_check_agent_artifact_ownership(root))
    findings.extend(_check_apps_api_layer_inversion(root))
    return findings
