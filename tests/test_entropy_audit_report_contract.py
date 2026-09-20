"""The entropy audit's report envelope and its two CLI modes (#1823 partition).

The JSON/Markdown schema contract, the report-only metadata carve-out, the two
whole-repository zero-finding invariants, the four "running the audit does not
write the baseline" cases, and the hard-gate CLI's exit code, gated-check-id
list and parseable stdout.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    REPO_ROOT,
    _assert_unallowlisted_budget_counted_report_only_finding,
    _entropy_baseline_snapshot,
    _findings_by_check,
    _repo_report,
    _run_entropy_audit_cli,
    _setup_clean_hard_gate_fixture,
    _write,
)


def test_entropy_audit_json_schema_is_stable() -> None:
    report = _repo_report(REPO_ROOT)

    assert set(report) == {"metadata", "module_heatmap", "findings", "high_spread_patterns"}
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_written"] is False
    assert metadata["baseline_path"] == ".entropy-baseline/latest.json"
    assert "summary_counts" in metadata
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert {
        "by_check_id",
        "by_priority",
        "by_role",
        "by_allowlist_state",
        "by_gate_eligibility",
        "by_budget_count",
    } <= set(summary_counts)
    assert metadata["budget_counted_count"] == summary_counts["by_budget_count"]["budget_counted"]
    assert metadata["gate_eligible_count"] == summary_counts["by_gate_eligibility"]["gate_eligible"]
    structural_budget = metadata["structural_file_budget"]
    assert isinstance(structural_budget, dict)
    assert structural_budget["schema_version"] == audit_repo_entropy.STRUCTURAL_FILE_BUDGET_SCHEMA_VERSION
    assert structural_budget["mode"] == "report-only"
    assert {
        "thresholds",
        "mandatory_governance_count",
        "yellow_zone_count",
        "governed_exemption_count",
        "unknown_line_count_count",
        "ownership_growth_signal_count",
        "oversized_files",
        "yellow_zone_files",
        "governed_exemptions",
        "unknown_line_count_files",
        "ownership_growth_signals",
        "top_oversized_modules",
        "comparison_base_ref",
    } <= set(structural_budget)
    comparison_base = structural_budget["comparison_base_ref"]
    assert isinstance(comparison_base, dict)
    assert {"requested", "requested_source", "resolved", "ref_kind", "status"} <= set(comparison_base)
    thresholds = structural_budget["thresholds"]
    assert isinstance(thresholds, dict)
    assert thresholds["yellow_zone_min_physical_lines"] == 500
    assert thresholds["mandatory_governance_over_physical_lines"] == 1000
    compatibility_guard = metadata["compatibility_facade_guard"]
    assert isinstance(compatibility_guard, dict)
    assert (
        compatibility_guard["schema_version"]
        == audit_repo_entropy.COMPATIBILITY_FACADE_GUARD_SCHEMA_VERSION
    )
    assert compatibility_guard["mode"] == "report-only"
    assert {
        "comparison_base_ref",
        "governed_facade_count",
        "signal_count",
        "facades",
        "signals",
    } <= set(compatibility_guard)
    scoped_context = metadata["scoped_agent_context"]
    assert isinstance(scoped_context, dict)
    assert (
        scoped_context["schema_version"]
        == audit_repo_entropy.SCOPED_AGENT_CONTEXT_SCHEMA_VERSION
    )
    assert scoped_context["mode"] == "report-only"
    assert {
        "governed_scope_count",
        "missing_instruction_count",
        "stale_context_count",
        "missing_glossary_link_count",
        "signal_count",
        "scopes",
        "signals",
    } <= set(scoped_context)
    assert metadata["max_scanned_text_file_bytes"] == audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES
    assert metadata["max_artifact_fingerprint_bytes"] == audit_repo_entropy.MAX_ARTIFACT_FINGERPRINT_BYTES
    assert ".venv" in metadata["skipped_path_families"]
    assert "node_modules" in metadata["skipped_path_families"]
    assert {
        "role-env-boundary",
        "qhh-diagnostic-token",
        "paused-workflow-condition",
        "broad-e2e-api-mock",
        "stale-display-route-token",
        "placeholder-path-token",
        "makefile-toolchain-discipline",
        "openapi-frontend-types-delegated",
        "openapi-frontend-types-presence",
        "openapi-frontend-types-signal",
        "slurm-gateway-route-leakage",
        "agent-artifact-ownership-policy",
        "apps-api-layer-inversion",
        "compatibility-facade-growth",
        "scoped-agent-context",
    } <= set(metadata["executed_check_families"])

    heatmap = report["module_heatmap"]
    assert isinstance(heatmap, list)
    assert heatmap, "expected at least one module row from known governance signals"
    heatmap_fields = {
        "module",
        "structure",
        "semantics",
        "behavior",
        "context",
        "protocol",
        "control",
        "priority",
        "finding_count",
    }
    assert heatmap_fields <= set(heatmap[0])

    findings = report["findings"]
    assert isinstance(findings, list)
    assert findings, "expected at least one finding from report-only baseline signals"
    finding_fields = {
        "id",
        "title",
        "axis",
        "axis_scores",
        "governance_face",
        "role",
        "evidence_path",
        "severity",
        "priority",
        "owner_area",
        "allowlist_reason",
        "allowlist_key",
        "allowlist_state",
        "budget_counted",
        "gate_eligible",
        "description",
        "recommendation",
    }
    assert finding_fields <= set(findings[0])
    for finding in findings:
        assert finding["allowlist_state"] in {"allowlisted", "unallowlisted"}
        assert isinstance(finding["budget_counted"], bool)
        assert isinstance(finding["gate_eligible"], bool)
        if finding["allowlist_state"] == "allowlisted":
            assert isinstance(finding["allowlist_key"], str)
            assert finding["budget_counted"] is False
            assert finding["gate_eligible"] is False
        else:
            assert finding["allowlist_key"] is None
            assert finding["budget_counted"] is True
    assert {"broad-e2e-api-mock", "stale-display-route-token", "placeholder-path-token"} <= {
        finding["check_id"] for finding in findings
    }


def test_entropy_audit_report_mode_metadata_excludes_hard_gate_fields() -> None:
    report = _repo_report(REPO_ROOT, mode="report")
    metadata = report["metadata"]

    assert isinstance(metadata, dict)
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_written"] is False
    assert "hard_gate_status" not in metadata
    assert "hard_gate_gated_check_ids" not in metadata
    assert "hard_gate_failing_count" not in metadata
    assert audit_repo_entropy._exit_code_for_report(report) == 0


def test_entropy_audit_current_repo_has_zero_apps_api_layer_inversion_findings() -> None:
    report = _repo_report(REPO_ROOT)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)

    layer_findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "apps-api-layer-inversion"
    ]

    assert layer_findings == []
    assert summary_counts["by_check_id"].get("apps-api-layer-inversion", 0) == 0


def test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings() -> None:
    report = _repo_report(REPO_ROOT, mode="hard-gate")
    metadata = report["metadata"]
    production_topology_findings = [
        finding
        for finding in report["findings"]
        if str(finding["check_id"]).startswith("production-topology-")
    ]

    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert production_topology_findings == []
    assert audit_repo_entropy._exit_code_for_report(report) == 0


def test_entropy_audit_json_report_preserves_repository_baseline() -> None:
    before = _entropy_baseline_snapshot()

    result = _run_entropy_audit_cli("--format", "json")
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 0
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_path"] == ".entropy-baseline/latest.json"
    assert metadata["baseline_exists"] is True
    assert metadata["baseline_written"] is False
    assert _entropy_baseline_snapshot() == before


def test_entropy_audit_markdown_report_preserves_repository_baseline() -> None:
    before = _entropy_baseline_snapshot()

    result = _run_entropy_audit_cli("--format", "markdown")

    assert result.returncode == 0
    assert "- Baseline path: `.entropy-baseline/latest.json`" in result.stdout
    assert "- Baseline written: `false`" in result.stdout
    assert "## Structural File Budget" in result.stdout
    assert "## Compatibility Facade Guard" in result.stdout
    assert "## Scoped Agent Context" in result.stdout
    assert "## Entropy Heatmap" in result.stdout
    assert "## High-Spread Patterns" in result.stdout
    assert "## Prioritized Cleanup Targets" in result.stdout
    assert _entropy_baseline_snapshot() == before


def test_compatibility_facade_guard_current_repo_passes_with_inventories() -> None:
    report = _repo_report(REPO_ROOT)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    guard = metadata["compatibility_facade_guard"]
    assert isinstance(guard, dict)

    assert guard["signal_count"] == 0
    assert guard["signals"] == []
    assert _findings_by_check(REPO_ROOT, audit_repo_entropy.COMPATIBILITY_FACADE_GUARD_CHECK_ID) == []

def test_entropy_audit_hard_gate_json_preserves_repository_baseline_and_parseable_stdout() -> None:
    before = _entropy_baseline_snapshot()

    result = _run_entropy_audit_cli("--mode", "hard-gate", "--format", "json", check=False)
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == (1 if metadata["hard_gate_failing_count"] else 0)
    assert metadata["mode"] == "hard-gate"
    assert metadata["baseline_path"] == ".entropy-baseline/latest.json"
    assert metadata["baseline_exists"] is True
    assert metadata["baseline_written"] is False
    assert metadata["hard_gate_status"] in {"pass", "fail"}
    assert metadata["hard_gate_gated_check_ids"] == sorted(audit_repo_entropy.HARD_GATE_CHECK_IDS)
    assert metadata["hard_gate_failing_count"] == metadata["gate_eligible_count"]
    assert _entropy_baseline_snapshot() == before

def test_entropy_audit_hard_gate_json_failure_is_parseable_and_counts_only_gated_findings(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "Makefile", "test:\n\tpython -m pytest\n")
    _write(tmp_path / "docs" / "active.md", "Historical token /hydro-met remains in docs.\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"),
            "--format",
            "json",
            "--mode",
            "hard-gate",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 1
    assert not (tmp_path / ".entropy-baseline" / "latest.json").exists()
    assert metadata["mode"] == "hard-gate"
    assert metadata["hard_gate_status"] == "fail"
    assert metadata["hard_gate_gated_check_ids"] == sorted(audit_repo_entropy.HARD_GATE_CHECK_IDS)
    assert metadata["hard_gate_failing_count"] == 1
    assert metadata["hard_gate_failing_count"] == metadata["gate_eligible_count"]
    assert {finding["check_id"] for finding in report["findings"]} >= {
        "makefile-toolchain-discipline",
        "stale-display-route-token",
    }
    stale_finding = next(
        finding for finding in report["findings"] if finding["check_id"] == "stale-display-route-token"
    )
    assert stale_finding["budget_counted"] is True
    assert stale_finding["gate_eligible"] is False


def test_entropy_audit_hard_gate_json_passes_with_no_gated_findings(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "docs" / "active.md", "Historical token /hydro-met remains in docs.\n")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"),
            "--format",
            "json",
            "--mode",
            "hard-gate",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 0
    assert metadata["mode"] == "hard-gate"
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert metadata["gate_eligible_count"] == 0
    assert not any(
        finding["check_id"] in audit_repo_entropy.HARD_GATE_CHECK_IDS for finding in report["findings"]
    )
    assert "stale-display-route-token" in {finding["check_id"] for finding in report["findings"]}
    stale_finding = next(
        finding for finding in report["findings"] if finding["check_id"] == "stale-display-route-token"
    )
    assert stale_finding["allowlist_state"] == "unallowlisted"
    assert stale_finding["allowlist_key"] is None
    assert stale_finding["budget_counted"] is True
    assert stale_finding["gate_eligible"] is False


def test_entropy_audit_hard_gate_json_reports_tracked_retired_path_as_report_only(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "retired placeholder returned\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=tmp_path, check=True)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"),
            "--format",
            "json",
            "--mode",
            "hard-gate",
        ],
        cwd=tmp_path,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
    )
    report = json.loads(result.stdout)
    metadata = report["metadata"]

    assert result.returncode == 0
    assert not (tmp_path / ".entropy-baseline" / "latest.json").exists()
    assert metadata["mode"] == "hard-gate"
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert "placeholder-path-exists" not in metadata["hard_gate_gated_check_ids"]

    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "placeholder-path-exists"
    ]
    assert len(findings) == 1
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    assert findings[0]["evidence_path"] == "apps/web/README.md"
    assert findings[0]["axis"] == "structure"


def test_entropy_audit_hard_gate_markdown_includes_status_and_report_sections(tmp_path: Path) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    markdown = audit_repo_entropy.render_markdown(report)

    assert "- Mode: `hard-gate`" in markdown
    assert "- Hard gate status: `pass`" in markdown
    assert "- Hard gate failing findings: `0`" in markdown
    assert "## Entropy Heatmap" in markdown
    assert "## High-Spread Patterns" in markdown
    assert "## Prioritized Cleanup Targets" in markdown
