from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from scripts.governance import audit_repo_entropy, write_entropy_baseline

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO_ROOT / ".entropy-baseline"
BASELINE = REPO_ROOT / ".entropy-baseline" / "latest.json"
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"
BASELINE_WRITER_SCRIPT = REPO_ROOT / "scripts" / "governance" / "write_entropy_baseline.py"


def test_entropy_audit_json_schema_is_stable() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT)

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
    report = audit_repo_entropy.build_report(REPO_ROOT, mode="report")
    metadata = report["metadata"]

    assert isinstance(metadata, dict)
    assert metadata["mode"] == "report-only"
    assert metadata["baseline_written"] is False
    assert "hard_gate_status" not in metadata
    assert "hard_gate_gated_check_ids" not in metadata
    assert "hard_gate_failing_count" not in metadata
    assert audit_repo_entropy._exit_code_for_report(report) == 0


def test_entropy_audit_current_repo_has_zero_apps_api_layer_inversion_findings() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT)
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
    assert "## Entropy Heatmap" in result.stdout
    assert "## Prioritized Cleanup Targets" in result.stdout
    assert _entropy_baseline_snapshot() == before


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


def test_entropy_baseline_writer_creates_latest_with_required_fields_and_no_archive(tmp_path: Path) -> None:
    result = _run_entropy_baseline_writer_cli(tmp_path)
    payload = json.loads(result.stdout)
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"

    assert result.returncode == 0
    assert payload == {
        "archive_path": None,
        "baseline_path": ".entropy-baseline/latest.json",
        "baseline_written": True,
    }
    assert latest.exists()
    assert _baseline_archive_files(baseline_dir) == []

    baseline = json.loads(latest.read_text(encoding="utf-8"))
    _assert_required_baseline_fields(baseline)
    assert baseline["summary"]["overall_trend"] == "baseline"
    assert baseline["summary"]["governance_finding_count"] >= 1
    assert isinstance(baseline["modules"], dict)
    assert isinstance(baseline["high_spread_patterns"], list)
    assert isinstance(baseline["cleanup_priorities"], list)


def test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT)
    baseline = write_entropy_baseline.build_baseline_snapshot(REPO_ROOT, report)
    tracked_v1_summary = json.loads(BASELINE.read_text(encoding="utf-8"))["summary"]

    modules = baseline["modules"]
    assert isinstance(modules, dict)
    inventory = write_entropy_baseline._baseline_file_inventory(REPO_ROOT)
    emitted_module_file_count_sum = _emitted_module_file_count_sum(modules)
    assert baseline["summary"]["total_source_files"] == inventory.v1_summary_source_files
    assert inventory.v1_summary_source_files > 700
    assert inventory.total_source_files > inventory.v1_summary_source_files
    assert baseline["summary"]["total_source_files"] > emitted_module_file_count_sum
    assert tracked_v1_summary["total_test_files"] == 247
    assert tracked_v1_summary["total_instruction_files"] == 3
    assert inventory.total_test_files == 143
    assert inventory.total_instruction_files == 2
    assert inventory.v1_summary_test_files == 143
    assert inventory.v1_summary_instruction_files == 2
    assert baseline["summary"]["total_test_files"] == inventory.v1_summary_test_files
    assert baseline["summary"]["total_instruction_files"] == inventory.v1_summary_instruction_files
    assert baseline["summary"]["total_test_files"] != tracked_v1_summary["total_test_files"]
    assert baseline["summary"]["total_instruction_files"] != tracked_v1_summary["total_instruction_files"]
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("docs/runbooks/live.md")
    assert write_entropy_baseline._baseline_path_is_v1_summary_source_counted(
        "openspec/changes/example/spec.md"
    )
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("openapi/nhms.v1.yaml")
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("README.md")
    assert write_entropy_baseline._baseline_path_is_v1_summary_source_counted("services/api/main.py")
    assert modules["apps/frontend"]["file_count"] == 120
    assert modules["services/production_closure"]["file_count"] == 10
    assert modules["services/slurm_gateway"]["file_count"] == 11
    for zero_count_module in (
        "docs/governance",
        "docs/runbooks",
        "openapi",
        "openspec/m21-qhh-hydro-met-ops-mvp",
        "progress.md",
    ):
        assert zero_count_module in modules
        assert modules[zero_count_module]["file_count"] == 0

    orchestrator = modules["services/orchestrator"]
    assert isinstance(orchestrator, dict)
    assert orchestrator["file_count"] == _expected_services_orchestrator_file_count()
    assert orchestrator["finding_count"] == 0
    assert orchestrator["priority"] == "P1"
    assert orchestrator["structure"] == {
        "score": "high",
        "hotspots": ["services/orchestrator/scheduler.py", "services/orchestrator/chain.py"],
    }
    assert (REPO_ROOT / "services/orchestrator/scheduler_lease.py").is_file()
    assert baseline["summary"]["modules_with_high_entropy"] >= 2

    patterns = {
        pattern["description"]: pattern
        for pattern in baseline["high_spread_patterns"]
        if isinstance(pattern, dict)
    }
    assert patterns["stale-display-route-token"]["axis"] == "docs alignment"
    assert patterns["stale-display-route-token"]["spread_risk"] == "high"
    assert patterns["placeholder-path-token"]["axis"] == "legacy/dead-code"
    assert patterns["placeholder-path-token"]["spread_risk"] == "high"
    assert patterns["orchestrator mixed responsibilities in scheduler.py and chain.py"] == {
        "description": "orchestrator mixed responsibilities in scheduler.py and chain.py",
        "occurrences": 2,
        "files": ["services/orchestrator/scheduler.py", "services/orchestrator/chain.py"],
        "axis": "structure,behavior",
        "spread_risk": "high",
        "top_priority": "P1",
        "top_severity": "high",
    }

    cleanup_priorities = baseline["cleanup_priorities"]
    assert cleanup_priorities == [
        {
            "target": "Align current display runbooks with M26 single-map route authority",
            "impact": "high",
            "effort": "low",
            "axis": "context",
        },
        {
            "target": (
                "Stage large decomposition of services/orchestrator/scheduler.py and "
                "services/orchestrator/chain.py"
            ),
            "impact": "high",
            "effort": "high",
            "axis": "structure/behavior",
        },
        {
            "target": "Keep mocked Playwright regression separated from live display evidence",
            "impact": "medium",
            "effort": "medium",
            "axis": "behavior/context",
        },
    ]


def test_entropy_baseline_writer_v1_summary_sibling_counts_are_current_derived(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "services" / "api" / "main.py", "VALUE = 1\n")
    _write(tmp_path / "tests" / "test_api.py", "def test_api() -> None:\n    pass\n")
    _write(tmp_path / "AGENTS.md", "Instructions.\n")
    _write(
        tmp_path / ".entropy-baseline" / "latest.json",
        json.dumps(
            {
                "version": 1,
                "summary": {
                    "total_test_files": 247,
                    "total_instruction_files": 3,
                },
            }
        ),
    )

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): [
            "services/api/main.py",
            "tests/test_api.py",
            "AGENTS.md",
        ],
    )

    report = {
        "metadata": {
            "schema_version": "governance-4a.entropy-report.v1",
            "generated_at": "2026-06-12T00:00:00+00:00",
            "mode": "report-only",
            "finding_count": 0,
            "budget_counted_count": 0,
            "gate_eligible_count": 0,
            "check_family_count": 0,
            "summary_counts": {},
            "skipped_path_families": [],
        },
        "module_heatmap": [],
        "findings": [],
        "high_spread_patterns": [],
    }

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    baseline = write_entropy_baseline.build_baseline_snapshot(
        tmp_path,
        report,
        file_inventory=inventory,
    )

    assert inventory.total_test_files == 1
    assert inventory.total_instruction_files == 1
    assert inventory.v1_summary_test_files == 1
    assert inventory.v1_summary_instruction_files == 1
    assert baseline["summary"]["total_test_files"] == 1
    assert baseline["summary"]["total_instruction_files"] == 1


def test_entropy_baseline_writer_v1_summary_sibling_counts_match_first_write_and_replacement(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "services" / "api" / "main.py", "VALUE = 1\n")
    _write(tmp_path / "tests" / "test_api.py", "def test_api() -> None:\n    pass\n")
    _write(tmp_path / "docs" / "test_docs.py", "def test_docs() -> None:\n    pass\n")
    _write(tmp_path / "AGENTS.md", "Instructions.\n")
    _write(tmp_path / "docs" / "AGENTS.md", "Docs-local instructions.\n")

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    assert inventory.total_test_files == 2
    assert inventory.v1_summary_test_files == 1
    assert inventory.total_instruction_files == 2
    assert inventory.v1_summary_instruction_files == 1

    first_result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    first_baseline = json.loads(first_result.baseline_path.read_text(encoding="utf-8"))
    first_summary = first_baseline["summary"]

    assert first_summary["total_test_files"] == 1
    assert first_summary["total_instruction_files"] == 1

    stale_latest_bytes = (
        json.dumps(
            {
                "version": 1,
                "summary": {
                    "total_test_files": 247,
                    "total_instruction_files": 3,
                },
            }
        )
        + "\n"
    ).encode("utf-8")
    first_result.baseline_path.write_bytes(stale_latest_bytes)

    replacement_result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    replacement_baseline = json.loads(replacement_result.baseline_path.read_text(encoding="utf-8"))
    replacement_summary = replacement_baseline["summary"]

    assert replacement_summary["total_test_files"] == first_summary["total_test_files"]
    assert replacement_summary["total_instruction_files"] == first_summary["total_instruction_files"]
    assert replacement_summary["total_test_files"] == inventory.v1_summary_test_files
    assert replacement_summary["total_instruction_files"] == inventory.v1_summary_instruction_files
    assert replacement_summary["total_test_files"] != 247
    assert replacement_summary["total_instruction_files"] != 3
    assert replacement_result.archive_path is not None
    assert replacement_result.archive_path.read_bytes() == stale_latest_bytes
    assert not (tmp_path / ".entropy-baseline" / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_v1_summary_source_count_excludes_context_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "services" / "api" / "main.py", "def main() -> None:\n    pass\n")
    _write(tmp_path / "apps" / "frontend" / "src" / "App.tsx", "export const App = () => null;\n")
    _write(tmp_path / "packages" / "common" / "model.py", "VALUE = 1\n")
    _write(tmp_path / "scripts" / "tool.py", "VALUE = 1\n")
    _write(tmp_path / "services" / "api" / "test_main.py", "def test_main() -> None:\n    pass\n")
    _write(tmp_path / "AGENTS.md", "Instructions.\n")
    _write(tmp_path / "docs" / "runbooks" / "live.md", "Current docs mention /hydro-met.\n")
    _write(tmp_path / "openspec" / "changes" / "example" / "design.md", "OpenSpec context.\n")
    _write(tmp_path / "openapi" / "nhms.v1.yaml", "openapi: 3.1.0\n")
    _write(tmp_path / "README.md", "Repository docs.\n")

    tracked_paths = [
        "services/api/main.py",
        "apps/frontend/src/App.tsx",
        "packages/common/model.py",
        "scripts/tool.py",
        "services/api/test_main.py",
        "AGENTS.md",
        "docs/runbooks/live.md",
        "openspec/changes/example/design.md",
        "openapi/nhms.v1.yaml",
        "README.md",
    ]
    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): tracked_paths,
    )

    report = {
        "metadata": {
            "schema_version": "governance-4a.entropy-report.v1",
            "generated_at": "2026-06-12T00:00:00+00:00",
            "mode": "report-only",
            "finding_count": 0,
            "budget_counted_count": 0,
            "gate_eligible_count": 0,
            "check_family_count": 0,
            "summary_counts": {},
            "skipped_path_families": [],
        },
        "module_heatmap": [
            {
                "module": "services/api",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
            {
                "module": "apps/frontend",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
            {
                "module": "docs/runbooks",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
            {
                "module": "openspec/example",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
            {
                "module": "openapi",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
            {
                "module": "README.md",
                "structure": "low",
                "semantics": "low",
                "behavior": "low",
                "context": "low",
                "protocol": "low",
                "control": "low",
                "priority": "P3",
                "finding_count": 0,
            },
        ],
        "findings": [],
        "high_spread_patterns": [],
    }
    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    baseline = write_entropy_baseline.build_baseline_snapshot(
        tmp_path,
        report,
        file_inventory=inventory,
    )

    modules = baseline["modules"]
    assert baseline["version"] == 1
    assert inventory.total_source_files == 8
    assert inventory.v1_summary_source_files == 5
    assert baseline["summary"]["total_source_files"] == 5
    assert baseline["summary"]["total_source_files"] != _emitted_module_file_count_sum(modules)
    assert baseline["summary"]["total_test_files"] == 1
    assert baseline["summary"]["total_instruction_files"] == 1
    assert modules["docs/runbooks"]["file_count"] == 0
    assert modules["openspec/example"]["file_count"] == 0
    assert modules["openapi"]["file_count"] == 0
    assert modules["README.md"]["file_count"] == 0


def test_services_orchestrator_file_count_includes_tracked_scheduler_execution_module() -> None:
    report = audit_repo_entropy.build_report(REPO_ROOT)
    baseline = write_entropy_baseline.build_baseline_snapshot(REPO_ROOT, report)

    orchestrator = baseline["modules"]["services/orchestrator"]
    assert isinstance(orchestrator, dict)
    assert orchestrator["file_count"] == _expected_services_orchestrator_file_count()


@pytest.mark.parametrize(
    ("remote_url", "expected_repo", "blocked_fragments"),
    (
        (
            "https://example.com/org/repo.git?access_token=ghp_secret-query",
            "https://example.com/org/repo.git",
            ("access_token", "ghp_secret-query", "?"),
        ),
        (
            "https://example.com/org/repo.git#ghp_secret-fragment",
            "https://example.com/org/repo.git",
            ("ghp_secret-fragment", "#"),
        ),
        (
            "https://user:ghp_secret-userinfo@example.com/org/repo.git?token=ghp_secret-query#ghp_secret-fragment",
            "https://example.com/org/repo.git",
            ("user:", "ghp_secret-userinfo", "token=", "ghp_secret-query", "ghp_secret-fragment", "?", "#"),
        ),
        (
            "git@github.com:org/repo.git",
            "github.com:org/repo.git",
            ("git@",),
        ),
    ),
)
def test_entropy_baseline_writer_redacts_remote_url_secret_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_url: str,
    expected_repo: str,
    blocked_fragments: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        write_entropy_baseline,
        "_git_output",
        lambda _root, *args: remote_url if args == ("config", "--get", "remote.origin.url") else "unknown",
    )

    result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    baseline = json.loads(result.baseline_path.read_text(encoding="utf-8"))
    baseline_text = result.baseline_bytes.decode("utf-8")

    assert baseline["repo"] == expected_repo
    for fragment in blocked_fragments:
        assert fragment not in baseline["repo"]
        assert fragment not in baseline_text


def test_entropy_baseline_writer_archives_previous_latest_bytes_exactly_once(
    tmp_path: Path,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{\n  "previous": true\n}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)

    result = _run_entropy_baseline_writer_cli(tmp_path)

    assert result.returncode == 0
    assert latest.read_bytes() != previous_bytes
    archives = _baseline_archive_files(baseline_dir)
    assert len(archives) == 1
    assert archives[0].read_bytes() == previous_bytes
    assert json.loads(result.stdout)["archive_path"] == f".entropy-baseline/{archives[0].name}"

    new_baseline = json.loads(latest.read_text(encoding="utf-8"))
    _assert_required_baseline_fields(new_baseline)


def test_entropy_baseline_writer_bounds_file_write_surface(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\n")
    _write(tmp_path / "apps" / "frontend" / "e2e" / "mocked.spec.ts", "await page.goto('/')\n")
    before = _relative_files(tmp_path)
    before_bytes = _file_bytes_by_relative_path(tmp_path)

    result = _run_entropy_baseline_writer_cli(tmp_path)

    assert result.returncode == 0
    created = _relative_files(tmp_path) - before
    assert created == {".entropy-baseline/latest.json"}
    for path, content in before_bytes.items():
        assert (tmp_path / path).read_bytes() == content

    second_before = _relative_files(tmp_path)
    second_before_bytes = _file_bytes_by_relative_path(tmp_path)
    second_result = _run_entropy_baseline_writer_cli(tmp_path)

    assert second_result.returncode == 0
    second_created = _relative_files(tmp_path) - second_before
    assert len(second_created) == 1
    archive = next(iter(second_created))
    assert archive.startswith(".entropy-baseline/")
    assert archive.endswith(".json")
    assert archive != ".entropy-baseline/latest.json"
    assert ".entropy-baseline/.latest.json.tmp" not in _relative_files(tmp_path)
    assert (tmp_path / archive).read_bytes() == second_before_bytes[".entropy-baseline/latest.json"]
    for path, content in second_before_bytes.items():
        if path != ".entropy-baseline/latest.json":
            assert (tmp_path / path).read_bytes() == content


def test_entropy_baseline_writer_failure_preserves_existing_latest_bytes(tmp_path: Path) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    (baseline_dir / ".latest.json.tmp").write_text("blocked temp path\n", encoding="utf-8")

    result = _run_entropy_baseline_writer_cli(tmp_path, check=False)

    assert result.returncode == 1
    assert "ERROR: entropy baseline write failed: unable to write temporary latest baseline" in result.stderr
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert (baseline_dir / ".latest.json.tmp").read_text(encoding="utf-8") == "blocked temp path\n"


def test_entropy_baseline_writer_fails_before_writing_when_snapshot_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\n")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def mutating_build_report(repo_root: Path, *, mode: audit_repo_entropy.AuditMode = "report") -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\nmutated\n")
        return report

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", mutating_build_report)

    with pytest.raises(
        write_entropy_baseline.BaselineWriteError,
        match="repository snapshot changed during baseline generation",
    ):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_untracked_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "local.md", "Untracked local note still mentions /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_ignored_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "docs/local.md\n")
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "local.md", "Ignored local note still mentions /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_dirty_tracked_report_visible_files_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "active.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    _write(tmp_path / "docs" / "active.md", "Dirty tracked docs still mention /hydro-met.\n")

    assert any(
        finding["evidence_path"] == "docs/active.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )

    def unexpected_build_report(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("writer dirty preflight must run before report generation")

    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", unexpected_build_report)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_untracked_report_visible_files_created_during_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def creating_untracked_build_report(
        repo_root: Path,
        *,
        mode: audit_repo_entropy.AuditMode = "report",
    ) -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "local.md", "Untracked local note still mentions /hydro-met.\n")
        return report

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "build_report",
        creating_untracked_build_report,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_ignored_report_visible_files_created_during_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "docs/local.md\n")
    _write(tmp_path / "docs" / "tracked.md", "Current docs are clean.\n")
    _commit_all(tmp_path, "initial tracked docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report

    def creating_ignored_build_report(
        repo_root: Path,
        *,
        mode: audit_repo_entropy.AuditMode = "report",
    ) -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / "docs" / "local.md", "Ignored local note still mentions /hydro-met.\n")
        return report

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "build_report",
        creating_ignored_build_report,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="dirty or untracked paths"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert any(
        finding["evidence_path"] == "docs/local.md"
        for finding in audit_repo_entropy.build_report(tmp_path)["findings"]
        if finding["check_id"] == "stale-display-route-token"
    )
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_bounds_status_path_collection() -> None:
    limit = write_entropy_baseline.MAX_WORKTREE_STATUS_PATHS_IN_ERROR + 1
    yielded: list[int] = []

    def status_records() -> Iterable[bytes]:
        for index in range(100):
            yielded.append(index)
            yield f"?? docs/local-{index}.md".encode()

    paths = write_entropy_baseline._bounded_git_status_porcelain_z_paths(
        status_records(),
        max_paths=limit,
    )

    assert paths == [f"docs/local-{index}.md" for index in range(limit)]
    assert yielded == list(range(limit))


def test_git_tracked_paths_preserves_non_ascii_path_identity(tmp_path: Path) -> None:
    _init_git(tmp_path)
    unicode_path = "docs/说明.md"
    quoted_literal = '"docs/\\350\\257\\264\\346\\230\\216.md"'
    _write(tmp_path / unicode_path, "Unicode path identity.\n")
    _write(tmp_path / "README.md", "Repository readme.\n")
    subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "docs/说明.md", "README.md"], cwd=tmp_path, check=True)

    tracked_paths = audit_repo_entropy._git_tracked_paths(tmp_path)
    scoped_paths = audit_repo_entropy._git_tracked_paths(tmp_path, ["docs"])

    assert unicode_path in tracked_paths
    assert quoted_literal not in tracked_paths
    assert scoped_paths == [unicode_path]


def test_entropy_baseline_writer_snapshot_uses_unicode_paths_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    unicode_path = "docs/说明.md"
    quoted_literal = '"docs/\\350\\257\\264\\346\\230\\216.md"'
    _write(tmp_path / unicode_path, "Current docs still mention /hydro-met.\n")
    subprocess.run(["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", unicode_path], cwd=tmp_path, check=True)
    _commit_all(tmp_path, "initial unicode docs")
    previous_bytes = b'{"previous": true}\n'
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_build_report = write_entropy_baseline.audit_repo_entropy.build_report
    observed_inventory: write_entropy_baseline.BaselineFileInventory | None = None
    observed_snapshot: write_entropy_baseline.SnapshotIdentity | None = None

    def observing_build_baseline_snapshot(
        repo_root: Path,
        report: dict[str, object],
        *,
        timestamp: object | None = None,
        file_inventory: write_entropy_baseline.BaselineFileInventory | None = None,
        snapshot: write_entropy_baseline.SnapshotIdentity | None = None,
    ) -> dict[str, object]:
        nonlocal observed_inventory, observed_snapshot
        observed_inventory = file_inventory
        observed_snapshot = snapshot
        return original_build_baseline_snapshot(
            repo_root,
            report,
            timestamp=timestamp,
            file_inventory=file_inventory,
            snapshot=snapshot,
        )

    def mutating_build_report(repo_root: Path, *, mode: audit_repo_entropy.AuditMode = "report") -> dict[str, object]:
        report = real_build_report(repo_root, mode=mode)
        _write(tmp_path / unicode_path, "Current docs still mention /hydro-met.\nmutated\n")
        return report

    original_build_baseline_snapshot = write_entropy_baseline.build_baseline_snapshot
    monkeypatch.setattr(write_entropy_baseline.audit_repo_entropy, "build_report", mutating_build_report)
    monkeypatch.setattr(
        write_entropy_baseline,
        "build_baseline_snapshot",
        observing_build_baseline_snapshot,
    )

    with pytest.raises(
        write_entropy_baseline.BaselineWriteError,
        match="repository snapshot changed during baseline generation",
    ):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert observed_inventory is not None
    assert observed_snapshot is not None
    assert unicode_path in observed_inventory.relative_paths
    assert quoted_literal not in observed_inventory.relative_paths
    assert any(fingerprint[0] == unicode_path for fingerprint in observed_inventory.file_fingerprints)
    assert unicode_path in observed_snapshot.inventory_paths
    assert quoted_literal not in observed_snapshot.inventory_paths
    assert any(fingerprint[0] == unicode_path for fingerprint in observed_snapshot.inventory_file_fingerprints)
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_oversized_latest_before_temp_write(tmp_path: Path) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b"x" * (write_entropy_baseline.MAX_ARCHIVED_LATEST_BYTES + 1)
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)

    result = _run_entropy_baseline_writer_cli(tmp_path, check=False)

    assert result.returncode == 1
    assert "existing latest baseline exceeds archive size limit" in result.stderr
    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_rejects_oversized_inventory_before_temp_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    tracked_paths = [
        f"apps/api/generated_{index}.py"
        for index in range(write_entropy_baseline.MAX_BASELINE_INVENTORY_FILES + 1)
    ]

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): tracked_paths,
    )

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="baseline inventory file count"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_snapshot_identity_does_not_walk_huge_fallback_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "services" / "api" / "main.py", "VALUE = 1\n")

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): ["services/api/main.py"],
    )

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    assert inventory.relative_paths == ("services/api/main.py",)

    fallback_calls = 0

    def huge_fallback_paths(_root: Path) -> list[str]:
        nonlocal fallback_calls
        fallback_calls += 1
        return [
            f"generated/fallback_{index}.py"
            for index in range(write_entropy_baseline.MAX_BASELINE_INVENTORY_FILES + 1)
        ]

    monkeypatch.setattr(write_entropy_baseline, "_fallback_inventory_relative_paths", huge_fallback_paths)

    result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    baseline = json.loads(result.baseline_path.read_text(encoding="utf-8"))

    assert fallback_calls == 0
    assert inventory.total_source_files == 1
    assert baseline["summary"]["total_source_files"] == 1
    assert _baseline_archive_files(tmp_path / ".entropy-baseline") == []


def test_entropy_baseline_writer_archive_failure_cleans_temp_and_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)

    def failing_copy(_source_path: Path, destination_path: Path) -> None:
        destination_path.write_bytes(b"partial archive\n")
        raise OSError("archive fsync failed")

    monkeypatch.setattr(write_entropy_baseline, "_bounded_copy_file", failing_copy)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="unable to archive existing latest baseline"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_replace_failure_rolls_back_archive_and_preserves_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    real_replace = os.replace

    def failing_replace(source: object, destination: object) -> None:
        if Path(source).name == ".latest.json.tmp" and Path(destination).name == "latest.json":
            raise OSError("replace failed")
        real_replace(source, destination)

    monkeypatch.setattr(write_entropy_baseline.os, "replace", failing_replace)

    with pytest.raises(write_entropy_baseline.BaselineWriteError, match="unable to replace latest baseline"):
        write_entropy_baseline.write_entropy_baseline(tmp_path)

    assert latest.read_bytes() == previous_bytes
    assert _baseline_archive_files(baseline_dir) == []
    assert not (baseline_dir / ".latest.json.tmp").exists()


def test_entropy_baseline_writer_avoids_archive_timestamp_collisions(tmp_path: Path) -> None:
    baseline_dir = tmp_path / ".entropy-baseline"
    latest = baseline_dir / "latest.json"
    previous_bytes = b'{"previous": true}\n'
    existing_archive_bytes = b'{"already": "archived"}\n'
    timestamp = write_entropy_baseline.datetime(2026, 6, 12, 16, 2, 45, tzinfo=write_entropy_baseline.UTC)
    baseline_dir.mkdir()
    latest.write_bytes(previous_bytes)
    existing_archive = baseline_dir / "2026-06-12T160245Z.json"
    existing_archive.write_bytes(existing_archive_bytes)

    result = write_entropy_baseline.write_entropy_baseline(tmp_path, now=timestamp)

    assert result.archive_path == baseline_dir / "2026-06-12T160245Z-01.json"
    assert existing_archive.read_bytes() == existing_archive_bytes
    assert result.archive_path.read_bytes() == previous_bytes
    assert latest.read_bytes() == result.baseline_bytes


def test_entropy_baseline_writer_fallback_inventory_skips_entropy_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention /hydro-met.\n")
    _write(tmp_path / ".entropy-baseline" / "latest.json", '{"legacy": true}\n')
    _write(tmp_path / ".entropy-baseline" / "2026-06-12T160245Z.json", '{"archive": true}\n')

    monkeypatch.setattr(
        write_entropy_baseline.audit_repo_entropy,
        "_git_tracked_paths",
        lambda _root, pathspecs=(): [],
    )

    inventory = write_entropy_baseline._baseline_file_inventory(tmp_path)
    assert all(not path.startswith(".entropy-baseline/") for path in inventory.relative_paths)
    assert ".entropy-baseline/latest.json" not in inventory.relative_paths

    result = write_entropy_baseline.write_entropy_baseline(tmp_path)
    baseline = json.loads(result.baseline_path.read_text(encoding="utf-8"))

    assert ".entropy-baseline/latest.json" not in result.baseline_bytes.decode("utf-8")
    assert inventory.total_source_files == 1
    assert baseline["summary"]["total_source_files"] == 0


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


def test_entropy_audit_skips_root_runtime_trees_without_skipping_source_packages(
    tmp_path: Path,
) -> None:
    root = tmp_path
    root_runtime_artifact = root / "artifacts" / "runtime.py"
    root_runtime_data = root / "data" / "runtime.py"
    source_artifact = root / "services" / "artifacts" / "model.py"
    source_data = root / "services" / "data" / "loader.py"

    for path in (root_runtime_artifact, root_runtime_data, source_artifact, source_data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VALUE = 1\n", encoding="utf-8")

    scanned = {
        path.relative_to(root).as_posix()
        for path in audit_repo_entropy._iter_text_files(root, [root])
    }

    assert "artifacts/runtime.py" not in scanned
    assert "data/runtime.py" not in scanned
    assert "services/artifacts/model.py" in scanned
    assert "services/data/loader.py" in scanned


def test_entropy_audit_skips_oversized_scanned_text_files(tmp_path: Path) -> None:
    oversized = tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts"
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_text(
        "x" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1)
        + "\nawait page.route('**/api/v1/**', route => route.abort())\n",
        encoding="utf-8",
    )

    scanned = {
        path.relative_to(tmp_path).as_posix()
        for path in audit_repo_entropy._iter_text_files(tmp_path, [tmp_path])
    }
    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert "apps/frontend/e2e/live.spec.ts" not in scanned
    assert findings == []


def test_openapi_frontend_type_fingerprint_skips_large_contract_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    openapi = tmp_path / "openapi" / "nhms.v1.yaml"
    frontend_types = tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts"
    openapi.parent.mkdir(parents=True, exist_ok=True)
    frontend_types.parent.mkdir(parents=True, exist_ok=True)
    openapi.write_text(
        "openapi: 3.1.0\n" + "x" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1),
        encoding="utf-8",
    )
    frontend_types.write_text(
        "export interface paths {}\n" + "y" * (audit_repo_entropy.MAX_SCANNED_TEXT_FILE_BYTES + 1),
        encoding="utf-8",
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(findings[0]["allowlist_reason"]).startswith("report-only fingerprint skipped ")
    assert "exceeds-" in str(findings[0]["allowlist_reason"])
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-skipped"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_openapi_frontend_type_fingerprint_skips_symlink_artifacts(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    openapi_target = tmp_path / "external-openapi.yaml"
    openapi_target.write_text("openapi: 3.1.0\n", encoding="utf-8")
    openapi = tmp_path / "openapi" / "nhms.v1.yaml"
    frontend_types = tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts"
    openapi.parent.mkdir(parents=True, exist_ok=True)
    frontend_types.parent.mkdir(parents=True, exist_ok=True)
    openapi.symlink_to(openapi_target)
    frontend_types.write_text("export interface paths {}\n", encoding="utf-8")

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert str(findings[0]["allowlist_reason"]).startswith("report-only fingerprint skipped ")
    assert "openapi/nhms.v1.yaml:symlink" in str(findings[0]["allowlist_reason"])
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-skipped"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False


def test_role_env_boundary_finds_display_service_in_generic_compose_without_compute_false_positive(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "infra" / "docker-compose.runtime.yml",
        """
        services:
          compute:
            environment:
              WORKSPACE_ROOT: /workspace
          display:
            image: nginx:alpine
            environment:
              SLURM_GATEWAY_URL: http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "infra/docker-compose.runtime.yml"
    assert findings[0]["line"] == 8


def test_role_env_boundary_finds_frontend_env_example_without_compute_false_positive(tmp_path: Path) -> None:
    _write(
        tmp_path / "apps" / "frontend" / ".env.example",
        """
        VITE_PUBLIC_NAME=nhms
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )
    _write(
        tmp_path / "infra" / "compute.env.example",
        """
        WORKSPACE_ROOT=/workspace
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert [finding["evidence_path"] for finding in findings] == ["apps/frontend/.env.example"]
    assert findings[0]["line"] == 2


@pytest.mark.parametrize("env_name", [".env", ".env.local"])
def test_role_env_boundary_scans_frontend_extensionless_env_dotfiles(
    tmp_path: Path,
    env_name: str,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / env_name,
        """
        VITE_PUBLIC_NAME=nhms
        SLURM_GATEWAY_URL=http://gateway:8000
        """,
    )

    findings = _findings_by_check(tmp_path, "role-env-boundary")

    assert [finding["evidence_path"] for finding in findings] == [f"apps/frontend/{env_name}"]
    assert findings[0]["line"] == 2


def test_makefile_toolchain_detects_unmanaged_python_after_uv_run_segment(tmp_path: Path) -> None:
    _write(
        tmp_path / "Makefile",
        """
        test:
        \tuv run python -m compileall scripts && python -m pytest
        """,
    )

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "Makefile"
    assert findings[0]["line"] == 2


def test_makefile_toolchain_skips_symlink_to_outside_file(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("entropy-outside") / "Makefile"
    outside.write_text("test:\n\tpython -m pytest\n", encoding="utf-8")
    (tmp_path / "Makefile").symlink_to(outside)

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert findings == []


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m compileall scripts && uv run python -m pytest",
        "uv run pytest -q ; uv run ruff check .",
        "uv run python -m pip install -e .",
    ],
)
def test_makefile_toolchain_allows_fully_uv_run_protected_compound_commands(
    tmp_path: Path,
    command: str,
) -> None:
    _write(
        tmp_path / "Makefile",
        f"""
        test:
        \t{command}
        """,
    )

    findings = _findings_by_check(tmp_path, "makefile-toolchain-discipline")

    assert findings == []


def test_broad_e2e_mock_classifies_live_label_as_high_and_mocked_e2e_as_medium(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        "await page.route('**/api/v1/**', route => route.abort())\n",
    )
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "visual-preview.spec.ts",
        "await page.route('**/api/v1/**', route => route.abort())\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "broad-e2e-api-mock")
    }

    assert findings["apps/frontend/e2e/live.spec.ts"]["severity"] == "high"
    assert findings["apps/frontend/e2e/live.spec.ts"]["priority"] == "P1"
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_reason"] is None
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_state"] == "unallowlisted"
    assert findings["apps/frontend/e2e/live.spec.ts"]["allowlist_key"] is None
    assert findings["apps/frontend/e2e/live.spec.ts"]["budget_counted"] is True
    assert findings["apps/frontend/e2e/live.spec.ts"]["gate_eligible"] is True
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["severity"] == "medium"
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["priority"] == "P2"
    assert (
        findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_reason"]
        == "deterministic mocked/preview/visual e2e broad mock"
    )
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_state"] == "allowlisted"
    assert (
        findings["apps/frontend/e2e/visual-preview.spec.ts"]["allowlist_key"]
        == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    )
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["budget_counted"] is False
    assert findings["apps/frontend/e2e/visual-preview.spec.ts"]["gate_eligible"] is False


def test_broad_e2e_mock_detects_multiline_live_and_unallowlisted_registrations(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "route-authority.spec.ts",
        """
        await page.route(
          "**/api/v1/**",
          route => route.abort(),
        )
        """,
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "broad-e2e-api-mock")
    }

    assert set(findings) == {
        "apps/frontend/e2e/live.spec.ts",
        "apps/frontend/e2e/route-authority.spec.ts",
    }
    assert findings["apps/frontend/e2e/live.spec.ts"]["severity"] == "high"
    assert findings["apps/frontend/e2e/live.spec.ts"]["priority"] == "P1"
    _assert_unallowlisted_budget_counted_gate_eligible_finding(findings["apps/frontend/e2e/live.spec.ts"])
    assert findings["apps/frontend/e2e/route-authority.spec.ts"]["severity"] == "medium"
    assert findings["apps/frontend/e2e/route-authority.spec.ts"]["priority"] == "P2"
    _assert_unallowlisted_budget_counted_gate_eligible_finding(
        findings["apps/frontend/e2e/route-authority.spec.ts"]
    )


def test_broad_e2e_mock_ignores_route_calls_on_non_page_identifiers(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "live.spec.ts",
        """
        await homepage.route('**/api/v1/**', route => route.abort())
        await page.route('**/api/v1/**', route => route.abort())
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/live.spec.ts"
    assert finding["line"] == 2
    _assert_unallowlisted_budget_counted_gate_eligible_finding(finding)


def test_broad_e2e_mock_skips_frontend_generated_artifacts(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "artifacts" / "live.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert findings == []


def test_broad_e2e_mock_detects_multiline_mocked_preview_visual_allowlist(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "e2e" / "mocked-preview-visual.spec.ts",
        """
        await page.route(
          '**/api/v1/**',
          route => route.abort(),
        )
        """,
    )

    findings = _findings_by_check(tmp_path, "broad-e2e-api-mock")

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/mocked-preview-visual.spec.ts"
    assert finding["severity"] == "medium"
    assert finding["priority"] == "P2"
    assert finding["allowlist_reason"] == "deterministic mocked/preview/visual e2e broad mock"
    assert finding["allowlist_key"] == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_allowlist_key_normalizes_equivalent_broad_mock_wording() -> None:
    base = audit_repo_entropy.FindingSpec(
        check_id="broad-e2e-api-mock",
        title="Deterministic frontend E2E path uses broad API mock",
        axis="behavior",
        governance_face="docs alignment",
        role="display_readonly",
        evidence_path="apps/frontend/e2e/visual-preview.spec.ts",
        line=1,
        severity="medium",
        priority="P2",
        owner_area="frontend e2e",
        module="apps/frontend",
        allowlist_reason="deterministic mocked/preview/visual e2e broad mock",
        description="Broad API mocks can be mistaken for live display evidence.",
        recommendation="Keep broad API mocks in deterministic mocked regressions.",
    )
    equivalent = audit_repo_entropy.FindingSpec(
        check_id=base.check_id,
        title=base.title,
        axis=base.axis,
        governance_face=base.governance_face,
        role=base.role,
        evidence_path="apps/frontend/e2e/mock.visual.spec.ts",
        line=1,
        severity=base.severity,
        priority=base.priority,
        owner_area=base.owner_area,
        module=base.module,
        allowlist_reason="visual preview deterministic API mock evidence",
        description=base.description,
        recommendation=base.recommendation,
    )

    base_record = audit_repo_entropy._finding_record(1, base)
    equivalent_record = audit_repo_entropy._finding_record(2, equivalent)

    assert base_record["allowlist_key"] == "broad-e2e-api-mock:deterministic-mocked-preview-visual"
    assert equivalent_record["allowlist_key"] == base_record["allowlist_key"]
    assert base_record["allowlist_reason"] != equivalent_record["allowlist_reason"]
    assert base_record["allowlist_state"] == "allowlisted"
    assert equivalent_record["budget_counted"] is False
    assert equivalent_record["gate_eligible"] is False


@pytest.mark.parametrize("allowlist_reason", [None, "", " \t\n "])
def test_empty_allowlist_reason_does_not_allowlist_gated_check(
    allowlist_reason: str | None,
) -> None:
    record = audit_repo_entropy._finding_record(
        1,
        audit_repo_entropy.FindingSpec(
            check_id="broad-e2e-api-mock",
            title="Live-labeled frontend E2E path uses broad API mock",
            axis="behavior",
            governance_face="docs alignment",
            role="display_readonly",
            evidence_path="apps/frontend/e2e/live.spec.ts",
            line=1,
            severity="high",
            priority="P1",
            owner_area="frontend e2e",
            module="apps/frontend",
            allowlist_reason=allowlist_reason,
            description="Broad API mocks can be mistaken for live display evidence.",
            recommendation="Keep live evidence specs on real API calls or narrowly scoped mocks.",
        ),
    )

    assert record["allowlist_key"] is None
    assert record["allowlist_state"] == "unallowlisted"
    assert record["budget_counted"] is True
    assert record["gate_eligible"] is True


def test_non_empty_unknown_allowlist_reason_uses_stable_slug_and_skips_budget() -> None:
    record = audit_repo_entropy._finding_record(
        1,
        audit_repo_entropy.FindingSpec(
            check_id="broad-e2e-api-mock",
            title="Frontend E2E path uses broad API mock",
            axis="behavior",
            governance_face="docs alignment",
            role="display_readonly",
            evidence_path="apps/frontend/e2e/contract.spec.ts",
            line=1,
            severity="medium",
            priority="P2",
            owner_area="frontend e2e",
            module="apps/frontend",
            allowlist_reason="Approved QA fixture exception",
            description="Broad API mocks can be mistaken for live display evidence.",
            recommendation="Keep broad API mocks in deterministic mocked regressions.",
        ),
    )

    assert record["allowlist_key"] == "broad-e2e-api-mock:approved-qa-fixture-exception"
    assert record["allowlist_state"] == "allowlisted"
    assert record["budget_counted"] is False
    assert record["gate_eligible"] is False


def test_archived_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "archived" / "m22.md", "Historical evidence mentions apps/web.\n")
    _write(
        tmp_path / "docs" / "governance" / "LEGACY_DEAD_CODE_INVENTORY.md",
        "Inventory keeps workers/sbatch_templates as retired evidence.\n",
    )

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    archived = findings["docs/archived/m22.md"]
    assert archived["allowlist_reason"] == "governed archived evidence documents retired placeholder paths"
    assert archived["allowlist_key"] == (
        "placeholder-path-token:governed-archived-retired-placeholder-evidence"
    )
    assert archived["allowlist_state"] == "allowlisted"
    assert archived["budget_counted"] is False
    assert archived["gate_eligible"] is False

    inventory = findings["docs/governance/LEGACY_DEAD_CODE_INVENTORY.md"]
    assert inventory["allowlist_key"] == "placeholder-path-token:governance-retired-placeholder-inventory"
    assert inventory["budget_counted"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_completed_governance_2_openspec_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path
        / "openspec"
        / "changes"
        / "governance-2-legacy-dead-code-retirement"
        / "tasks.md",
        "Completed evidence keeps apps/web as a retired placeholder path.\n",
    )
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    governed = findings["openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md"]
    assert (
        governed["allowlist_reason"]
        == "governed completed OpenSpec evidence documents retired placeholder paths"
    )
    assert governed["allowlist_key"] == (
        "placeholder-path-token:governed-completed-openspec-retired-placeholder-evidence"
    )
    assert governed["allowlist_state"] == "allowlisted"
    assert governed["budget_counted"] is False
    assert governed["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_key"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True
    assert active_doc["gate_eligible"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_governance_5_e1_fixture_retired_path_tokens_are_allowlisted_without_budget_count(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path
        / "openspec"
        / "changes"
        / "governance-5-e1-entropy-baseline-burndown"
        / "tasks.md",
        "Fixture evidence keeps apps/web and workers/sbatch_templates as retired path examples.\n",
    )
    _write(tmp_path / "docs" / "active.md", "Current docs still mention services/tile-publisher.\n")

    findings = {
        str(finding["evidence_path"]): finding
        for finding in _findings_by_check(tmp_path, "placeholder-path-token")
    }

    governed = findings[
        "openspec/changes/governance-5-e1-entropy-baseline-burndown/tasks.md"
    ]
    assert (
        governed["allowlist_reason"]
        == "governed Governance-5 E1 fixture evidence documents retired placeholder paths"
    )
    assert governed["allowlist_key"] == (
        "placeholder-path-token:governed-governance-5-e1-fixture-evidence-documents-retired-placeholder-paths"
    )
    assert governed["allowlist_state"] == "allowlisted"
    assert governed["budget_counted"] is False
    assert governed["gate_eligible"] is False

    active_doc = findings["docs/active.md"]
    assert active_doc["allowlist_reason"] is None
    assert active_doc["allowlist_key"] is None
    assert active_doc["allowlist_state"] == "unallowlisted"
    assert active_doc["budget_counted"] is True
    assert active_doc["gate_eligible"] is False
    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_active_doc_retired_path_tokens_remain_budget_counted(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "docs" / "active.md", "Current docs still mention apps/web.\n")

    findings = _findings_by_check(tmp_path, "placeholder-path-token")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "docs/active.md"
    assert findings[0]["allowlist_reason"] is None
    assert findings[0]["allowlist_key"] is None
    assert findings[0]["allowlist_state"] == "unallowlisted"
    assert findings[0]["budget_counted"] is True
    assert findings[0]["gate_eligible"] is False


def test_tracked_apps_web_file_emits_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "retired placeholder returned\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=tmp_path, check=True)

    report = audit_repo_entropy.build_report(tmp_path)
    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "placeholder-path-exists"
    ]

    assert len(findings) == 1
    finding = findings[0]
    assert finding["title"] == "Tracked retired path returned to active tree"
    assert finding["evidence_path"] == "apps/web/README.md"
    assert finding["allowlist_reason"] is None
    assert finding["allowlist_key"] is None
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert summary_counts["by_check_id"]["placeholder-path-exists"] == 1


@pytest.mark.parametrize("retired_prefix", audit_repo_entropy.RETIRED_ACTIVE_TREE_PREFIXES)
def test_tracked_file_under_each_retired_prefix_emits_retired_path_return_finding(
    tmp_path: Path,
    retired_prefix: str,
) -> None:
    _init_git(tmp_path)
    tracked_file = f"{retired_prefix}/README.md"
    _write(tmp_path / tracked_file, "tracked retired path returned\n")
    subprocess.run(["git", "add", tracked_file], cwd=tmp_path, check=True)

    report = audit_repo_entropy.build_report(tmp_path)
    findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "placeholder-path-exists"
    ]

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == tracked_file
    assert findings[0]["description"] == (
        f"Tracked file `{tracked_file}` returned under retired active-tree prefix "
        f"`{retired_prefix}`."
    )
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    summary_counts = metadata["summary_counts"]
    assert isinstance(summary_counts, dict)
    assert summary_counts["by_check_id"]["placeholder-path-exists"] == 1


def test_force_added_ignored_retired_worker_path_emits_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / ".gitignore", "workers/\n")
    _write(tmp_path / "workers" / "shud-runtime" / "README.md", "ignored but tracked\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-f", "workers/shud-runtime/README.md"], cwd=tmp_path, check=True)

    findings = _findings_by_check(tmp_path, "placeholder-path-exists")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "workers/shud-runtime/README.md"
    assert findings[0]["allowlist_state"] == "unallowlisted"
    assert findings[0]["budget_counted"] is True
    assert findings[0]["gate_eligible"] is False


def test_untracked_filesystem_retired_path_does_not_emit_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "untracked retired placeholder\n")

    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_active_underscore_paths_do_not_emit_retired_path_return_finding(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "workers" / "shud_runtime" / "__init__.py", "\n")
    _write(tmp_path / "workers" / "output_parser" / "__init__.py", "\n")
    _write(tmp_path / "services" / "tile_publisher" / "__init__.py", "\n")
    subprocess.run(
        [
            "git",
            "add",
            "workers/shud_runtime/__init__.py",
            "workers/output_parser/__init__.py",
            "services/tile_publisher/__init__.py",
        ],
        cwd=tmp_path,
        check=True,
    )

    assert _findings_by_check(tmp_path, "placeholder-path-exists") == []


def test_non_git_root_does_not_emit_retired_path_return_false_positive(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "apps" / "web" / "README.md", "filesystem-only retired placeholder\n")

    report = audit_repo_entropy.build_report(tmp_path)

    assert not any(
        finding["check_id"] == "placeholder-path-exists"
        for finding in report["findings"]
    )


def test_unavailable_git_metadata_does_not_crash_or_emit_retired_path_return_false_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _write(tmp_path / "apps" / "web" / "README.md", "tracked but git unavailable\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=tmp_path, check=True)
    real_run = subprocess.run

    def unavailable_git_ls_files(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and command[:2] == ["git", "ls-files"]:
            raise OSError("git metadata unavailable")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(audit_repo_entropy.subprocess, "run", unavailable_git_ls_files)

    report = audit_repo_entropy.build_report(tmp_path)

    assert not any(
        finding["check_id"] == "placeholder-path-exists"
        for finding in report["findings"]
    )


def test_slurm_gateway_route_leakage_finds_direct_business_route_decorators_and_path_literals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "services" / "slurm_gateway" / "app.py",
        """
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/api/v1/models")
        def list_models():
            return []

        FRONTEND_PATH = "/static/assets"
        FORECAST_SERIES = "/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series"
        """,
    )

    descriptions = {
        str(finding["description"])
        for finding in _findings_by_check(tmp_path, "slurm-gateway-route-leakage")
    }

    assert any("direct route decorator" in description for description in descriptions)
    assert any("path literal `/static/assets`" in description for description in descriptions)
    assert any("forecast-series" in description for description in descriptions)


def test_openapi_frontend_type_drift_emits_delegated_and_fingerprint_signals(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )
    _write(
        tmp_path / "apps" / "frontend" / "src" / "api" / "types.ts",
        """
        export interface paths {}
        export interface components {}
        """,
    )
    _write(
        tmp_path / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-delegated")
    signal_findings = _findings_by_check(tmp_path, "openapi-frontend-types-signal")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "tests/test_openapi_drift.py"
    assert findings[0]["allowlist_reason"] == "existing OpenAPI drift tests are the enforced contract oracle"
    assert findings[0]["allowlist_key"] == "openapi-frontend-types-delegated:existing-contract-oracle-delegation"
    assert findings[0]["budget_counted"] is False
    assert findings[0]["gate_eligible"] is False
    assert len(signal_findings) == 1
    assert signal_findings[0]["evidence_path"] == "apps/frontend/src/api/types.ts"
    assert str(signal_findings[0]["allowlist_reason"]).startswith("report-only fingerprint ")
    assert signal_findings[0]["allowlist_key"] == "openapi-frontend-types-signal:report-only-fingerprint-record"
    assert signal_findings[0]["budget_counted"] is False
    assert signal_findings[0]["gate_eligible"] is False


def test_openapi_frontend_type_drift_emits_presence_signal_when_artifact_missing(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )

    findings = _findings_by_check(tmp_path, "openapi-frontend-types-presence")

    assert len(findings) == 1
    assert findings[0]["evidence_path"] == "openapi/nhms.v1.yaml"
    assert findings[0]["severity"] == "high"
    assert findings[0]["priority"] == "P1"


def test_agent_artifact_ownership_skips_doc_status_symlink_to_outside_file(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    outside = tmp_path_factory.mktemp("entropy-outside") / "DOC_STATUS.md"
    outside.write_text(
        "\n".join(
            [
                ".agents/skills/**",
                ".codex/tmp/",
                ".codex/cache/",
                ".codex/evidence/",
                "apps/frontend/artifacts/**",
                "Root `artifacts/`",
                ".dockerignore",
            ]
        ),
        encoding="utf-8",
    )
    doc_status = tmp_path / "docs" / "governance" / "DOC_STATUS.md"
    doc_status.parent.mkdir(parents=True, exist_ok=True)
    doc_status.symlink_to(outside)

    findings = _findings_by_check(tmp_path, "agent-artifact-ownership-policy")

    assert {finding["description"] for finding in findings} == {
        f"`DOC_STATUS.md` does not mention expected ownership term `{term}`."
        for term in (
            ".agents/skills/**",
            ".codex/tmp/",
            ".codex/cache/",
            ".codex/evidence/",
            "apps/frontend/artifacts/**",
            "Root `artifacts/`",
            ".dockerignore",
        )
    }


@pytest.mark.parametrize(
    ("check_ids", "setup"),
    [
        (("qhh-diagnostic-token",), lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            ("paused-workflow-condition",),
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            ("broad-e2e-api-mock",),
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            ("stale-display-route-token",),
            lambda root: _write(root / "apps/frontend/src/routes.ts", 'const oldRoute = "/hydro-met";\n'),
        ),
        (
            ("placeholder-path-token", "placeholder-path-exists"),
            lambda root: _setup_placeholder_path_drift(root),
        ),
        (
            ("makefile-toolchain-discipline",),
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            ("slurm-gateway-route-leakage",),
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi.staticfiles import StaticFiles\n"
                "from apps.api.routes.forecast import router as forecast_router\n"
                "def attach(app):\n"
                "    app.include_router(forecast_router)\n",
            ),
        ),
        (
            (
                "agent-artifact-ownership-policy",
                "agent-artifact-ignore-policy",
                "tracked-generated-artifact",
            ),
            lambda root: _setup_agent_artifact_drift(root),
        ),
        (
            ("apps-api-layer-inversion",),
            lambda root: _write(root / "packages/common/bad_import.py", "from apps.api.main import create_app\n"),
        ),
    ],
)
def test_entropy_audit_required_families_emit_positive_signals(
    tmp_path: Path,
    check_ids: tuple[str, ...],
    setup: Callable[[Path], object],
) -> None:
    setup(tmp_path)

    emitted = {str(finding["check_id"]) for finding in audit_repo_entropy.build_report(tmp_path)["findings"]}

    assert set(check_ids) <= emitted


@pytest.mark.parametrize(
    ("check_id", "setup"),
    [
        (
            "role-env-boundary",
            lambda root: _write(
                root / "apps" / "frontend" / ".env.example",
                "SLURM_GATEWAY_URL=http://gateway:8000\n",
            ),
        ),
        ("qhh-diagnostic-token", lambda root: _write(root / "services/orchestrator/run.py", "run_qhh_cycle()\n")),
        (
            "broad-e2e-api-mock",
            lambda root: _write(
                root / "apps/frontend/e2e/live.spec.ts",
                "await page.route('**/api/v1/**', route => route.abort())\n",
            ),
        ),
        (
            "slurm-gateway-route-leakage",
            lambda root: _write(
                root / "services/slurm_gateway/app.py",
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/api/v1/models')\n"
                "def list_models():\n"
                "    return []\n",
            ),
        ),
        ("openapi-frontend-types-presence", lambda root: (root / "apps/frontend/src/api/types.ts").unlink()),
        (
            "paused-workflow-condition",
            lambda root: _write(
                root / ".github/workflows/check.yml",
                "if: github.event_name == 'pull_request' && false\n",
            ),
        ),
        (
            "makefile-toolchain-discipline",
            lambda root: _write(root / "Makefile", "test:\n\tpython -m pytest\n"),
        ),
        (
            "agent-artifact-ownership-policy",
            lambda root: _write(root / "docs/governance/DOC_STATUS.md", "Governed docs placeholder.\n"),
        ),
        (
            "agent-artifact-ignore-policy",
            lambda root: _write(root / ".gitignore", "# missing generated artifact ignores\n"),
        ),
        ("tracked-generated-artifact", lambda root: _track_generated_artifact(root)),
    ],
)
def test_entropy_audit_hard_gate_fails_for_each_gated_check_id(
    tmp_path: Path,
    check_id: str,
    setup: Callable[[Path], object],
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    setup(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]
    gated_findings = [finding for finding in report["findings"] if finding["check_id"] == check_id]

    assert gated_findings
    assert metadata["hard_gate_status"] == "fail"
    assert metadata["hard_gate_failing_count"] == len(
        [
            finding
            for finding in report["findings"]
            if finding["gate_eligible"]
        ]
    )
    assert all(
        finding["check_id"] in audit_repo_entropy.HARD_GATE_CHECK_IDS
        for finding in report["findings"]
        if finding["gate_eligible"]
    )
    assert audit_repo_entropy._exit_code_for_report(report) == 1


def test_entropy_audit_hard_gate_keeps_delegated_and_fingerprint_openapi_signals_report_only(
    tmp_path: Path,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]

    assert {
        "openapi-frontend-types-delegated",
        "openapi-frontend-types-signal",
    } <= {finding["check_id"] for finding in report["findings"]}
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0
    assert "openapi-frontend-types-delegated" not in metadata["hard_gate_gated_check_ids"]
    assert "openapi-frontend-types-signal" not in metadata["hard_gate_gated_check_ids"]
    for finding in report["findings"]:
        if finding["check_id"] in {"openapi-frontend-types-delegated", "openapi-frontend-types-signal"}:
            assert finding["allowlist_state"] == "allowlisted"
            assert finding["budget_counted"] is False
            assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "relative_path",
    [
        "packages/common/synthetic_api_import.py",
        "services/production_closure/synthetic_api_import.py",
        "workers/flood_frequency/synthetic_api_import.py",
    ],
)
def test_entropy_audit_apps_api_layer_inversion_remains_standalone_report_only_finding(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _setup_clean_hard_gate_fixture(tmp_path)
    _write(tmp_path / relative_path, "from apps.api.routes.forecast import router\n")

    report = audit_repo_entropy.build_report(tmp_path, mode="hard-gate")
    metadata = report["metadata"]
    layer_findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "apps-api-layer-inversion"
    ]

    assert len(layer_findings) == 1
    finding = layer_findings[0]
    assert finding["evidence_path"] == relative_path
    assert finding["axis"] == "structure"
    assert finding["governance_face"] == "role boundary"
    assert finding["role"] == "shared_contract"
    assert finding["owner_area"] == "layering"
    assert finding["priority"] == "P1"
    assert finding["severity"] == "high"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False
    assert "`apps.api.routes.forecast`" in finding["description"]
    assert "apps-api-layer-inversion" not in audit_repo_entropy.HARD_GATE_CHECK_IDS
    assert "apps-api-layer-inversion" not in metadata["hard_gate_gated_check_ids"]
    assert metadata["hard_gate_status"] == "pass"
    assert metadata["hard_gate_failing_count"] == 0


def test_route_authority_current_runbook_active_legacy_alias_is_report_only_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["check_id"] == "stale-display-route-token"
    assert finding["evidence_path"] == "docs/runbooks/current.md"
    assert "/forecast" in finding["description"]
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False


def test_route_authority_route_valued_forms_are_detected_without_substring_false_positives(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        BASE_URL=$BASE_URL/forecast
        command --path=/forecast
        callback ?next=/forecast
        Ignore foo/hydro-met and some/path/hydro-met.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 3
    assert {finding["line"] for finding in findings} == {1, 2, 3}
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert all(finding["allowlist_key"] is None for finding in findings)
    assert all(finding["budget_counted"] is True for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_placeholder_url_route_valued_forms_are_detected(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        Current route link: ${BASE_URL}/forecast
        Current route quoted link: "${BASE_URL}/forecast"
        Current route placeholder link: <frontend-base-url>/forecast
        Ignore foo/hydro-met and some/path/hydro-met.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 3
    assert {finding["line"] for finding in findings} == {1, 2, 3}
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert all(finding["allowlist_key"] is None for finding in findings)
    assert all(finding["budget_counted"] is True for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_historical_runbook_banner_allowlists_deep_legacy_evidence(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Preserved run output:

        1. Open /hydro-met for current live browser proof.
        2. Visit /forecast for current display proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 2
    assert {finding["evidence_path"] for finding in findings} == {
        "docs/runbooks/historical.md",
    }
    assert {finding["allowlist_reason"] for finding in findings} == {
        "historical plan or pre-M26 display evidence",
    }
    assert {finding["allowlist_key"] for finding in findings} == {
        "stale-display-route-token:historical-plan-or-pre-m26-evidence",
    }
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_historical_banner_does_not_allowlist_later_current_instruction(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Frozen receipt: Open /hydro-met for current live browser proof.

        # Current operator procedure

        Open /forecast.
        Use /forecast route.
        Current display route: /forecast.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 10, 11, 12}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False
    for line_no in (10, 11, 12):
        active = by_line[line_no]
        assert "/forecast" in str(active["description"])
        assert active["allowlist_reason"] is None
        assert active["allowlist_key"] is None
        assert active["allowlist_state"] == "unallowlisted"
        assert active["budget_counted"] is True
        assert active["gate_eligible"] is False


def test_route_authority_current_section_heading_governs_beyond_short_lookback(
    tmp_path: Path,
) -> None:
    lines = [
        "> **Historical / superseded by M26**: frozen smoke evidence.",
        "> Current route authority is the M26 single-map `/` display entrypoint.",
        "",
        "# Historical smoke evidence",
        "",
        "Frozen receipt: Open /hydro-met for current live browser proof.",
        "",
        "# Current operator procedure",
        "",
        *(f"Setup note {index}: prepare operator context." for index in range(1, 14)),
        "Open /forecast.",
        "Use /forecast route.",
    ]
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        "\n".join(lines) + "\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 23, 24}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False

    for line_no in (23, 24):
        active = by_line[line_no]
        assert "/forecast" in str(active["description"])
        assert active["allowlist_reason"] is None
        assert active["allowlist_key"] is None
        assert active["allowlist_state"] == "unallowlisted"
        assert active["budget_counted"] is True
        assert active["gate_eligible"] is False


def test_route_authority_historical_banner_keeps_later_current_route_values_as_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "historical.md",
        """
        > **Historical / superseded by M26**: frozen smoke evidence.
        > Current route authority is the M26 single-map `/` display entrypoint.

        # Historical smoke evidence

        Frozen receipt: Open /hydro-met for current live browser proof.

        # Current operator procedure

        BASE_URL=$BASE_URL/forecast
        command --path=/forecast
        callback ?next=/forecast
        Use /forecast as the current route.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {6, 10, 11, 12, 13}
    frozen = by_line[6]
    assert "/hydro-met" in str(frozen["description"])
    assert frozen["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert frozen["allowlist_state"] == "allowlisted"
    assert frozen["budget_counted"] is False
    for line_no in (10, 11, 12, 13):
        finding = by_line[line_no]
        assert "/forecast" in str(finding["description"])
        assert finding["allowlist_reason"] is None
        assert finding["allowlist_key"] is None
        assert finding["allowlist_state"] == "unallowlisted"
        assert finding["budget_counted"] is True
        assert finding["gate_eligible"] is False


def test_route_authority_evidence_boundary_heading_allowlists_diagnostic_route_references(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        IFS deterministic `/forecast` browser smoke 标注 144h actual horizon.

        ## Current operator procedure

        Open /meteorology for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/forecast", "/meteorology"}
    for token in ("/hydro-met", "/forecast"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
    _assert_unallowlisted_budget_counted_report_only_finding(by_token["/meteorology"])


def test_route_authority_evidence_boundary_active_instruction_is_report_only_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        Diagnostic `/meteorology` browser evidence remains frozen.
        Open /forecast for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/forecast"}
    for token in ("/hydro-met", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
        assert by_token[token]["gate_eligible"] is False

    active = by_token["/forecast"]
    assert active["evidence_path"] == "docs/runbooks/current.md"
    assert active["line"] == 5
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    "active_line",
    [
        "Open /forecast.",
        "Current display route: /forecast.",
    ],
)
def test_route_authority_evidence_boundary_terse_active_route_is_report_only_drift(
    tmp_path: Path,
    active_line: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        ## #214 evidence boundary

        - `/hydro-met` browser proof 状态以 #214 evidence matrix 为准。
        Diagnostic `/meteorology` browser evidence remains frozen.
        {active_line}
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/forecast"}
    for token in ("/hydro-met", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:historical-plan-or-pre-m26-evidence"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
        assert by_token[token]["gate_eligible"] is False

    active = by_token["/forecast"]
    assert active["evidence_path"] == "docs/runbooks/current.md"
    assert active["line"] == 5
    _assert_unallowlisted_budget_counted_report_only_finding(active)


@pytest.mark.parametrize(
    "line",
    [
        "Current route links: BASE_URL=$BASE_URL/forecast",
        "Current route deep links: --path=/forecast",
        "Current route bookmark: ?next=/forecast",
    ],
)
def test_route_authority_current_route_valued_context_takes_precedence_over_compatibility_words(
    tmp_path: Path,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_inherited_current_heading_route_value_compatibility_words_are_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Current operator procedure

        Deep links: --path=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_current_child_heading_route_value_is_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Current operator procedure

        ## Deep links
        BASE_URL=$BASE_URL/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 4
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


@pytest.mark.parametrize("route_value", ["--path=/forecast", "?next=/forecast"])
def test_route_authority_inherited_current_parent_list_route_value_is_drift(
    tmp_path: Path,
    route_value: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        - Current route values:
          - {route_value}
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_current_table_row_route_value_compatibility_words_are_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Context | Label | Value |
        |---|---|---|
        | Current route | Deep links | --path=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_blockquoted_current_table_row_route_value_is_drift(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > | Context | Label | Value |
        > |---|---|---|
        > | Current route | Deep links | --path=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_blockquoted_historical_heading_does_not_govern_normal_current_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_independent_blockquote_does_not_inherit_stale_blockquote_heading(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        > Frozen /hydro-met receipt

        Current operator note outside quote.

        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {2, 6}
    historical = by_line[2]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False

    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_normal_historical_heading_does_not_govern_blockquoted_current_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Historical pre-M26 evidence
        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(finding)


def test_route_authority_normal_historical_heading_restores_after_intervening_blockquote(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        # Historical pre-M26 evidence
        > preserved quoted note
        Preserved /hydro-met receipt
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 3
    assert "/hydro-met" in str(finding["description"])
    assert finding["allowlist_reason"] == "historical plan or pre-M26 display evidence"
    assert finding["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_blockquoted_historical_heading_expires_after_normal_heading(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > # Historical pre-M26 evidence
        > Frozen /hydro-met receipt
        # Current operator procedure
        > Current route link ?next=/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {2, 4}
    historical = by_line[2]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False

    active = by_line[4]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_blockquoted_historical_table_does_not_merge_with_normal_current_table(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > | Context | Value |
        > |---|---|
        > | Historical pre-M26 evidence | /hydro-met |
        | Context | Value |
        |---|---|
        | Current route | ?next=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {3, 6}
    historical = by_line[3]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False
    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


def test_route_authority_normal_historical_table_does_not_merge_with_blockquoted_current_table(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Context | Value |
        |---|---|
        | Historical pre-M26 evidence | /hydro-met |
        > | Context | Value |
        > |---|---|
        > | Current route | ?next=/forecast |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {3, 6}
    historical = by_line[3]
    assert "/hydro-met" in str(historical["description"])
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert historical["allowlist_state"] == "allowlisted"
    assert historical["budget_counted"] is False
    active = by_line[6]
    assert "/forecast" in str(active["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(active)


@pytest.mark.parametrize(
    ("line", "expected_key"),
    [
        ("--path=/hydro-met -> / redirect alias", "stale-display-route-token:m26-route-consolidation-or-redirect"),
        (
            "${BASE_URL}/hydro-met -> / redirect alias",
            "stale-display-route-token:m26-route-consolidation-or-redirect",
        ),
        (
            "<frontend-base-url>/hydro-met redirects to /",
            "stale-display-route-token:m26-route-consolidation-or-redirect",
        ),
        (
            "Historical pre-M26 evidence used --path=/forecast",
            "stale-display-route-token:historical-plan-or-pre-m26-evidence",
        ),
        (
            "Historical pre-M26 evidence used ${BASE_URL}/forecast",
            "stale-display-route-token:historical-plan-or-pre-m26-evidence",
        ),
        (
            "Compatibility context keeps --path=/forecast deep links",
            "stale-display-route-token:legacy-route-compatibility-context",
        ),
        (
            "Compatibility context keeps \"${BASE_URL}/forecast\" deep links",
            "stale-display-route-token:legacy-route-compatibility-context",
        ),
    ],
)
def test_route_authority_explicit_route_valued_allowlist_contexts_still_allowlist(
    tmp_path: Path,
    line: str,
    expected_key: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["allowlist_key"] == expected_key
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_current_runbook_allowlist_contexts_are_distinct(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        /hydro-met -> / redirect alias
        Compatibility context keeps /meteorology deep links
        Historical pre-M26 evidence used /flood-alerts
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/hydro-met", "/meteorology", "/flood-alerts"}
    redirect = by_token["/hydro-met"]
    compatibility = by_token["/meteorology"]
    historical = by_token["/flood-alerts"]
    assert redirect["allowlist_reason"] == "M26 route-consolidation redirect alias"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert compatibility["allowlist_reason"] == "legacy route compatibility context"
    assert compatibility["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert historical["allowlist_reason"] == "historical plan or pre-M26 display evidence"
    assert historical["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert len({finding["allowlist_reason"] for finding in findings}) == 3
    assert len({finding["allowlist_key"] for finding in findings}) == 3
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)
    assert all(finding["gate_eligible"] is False for finding in findings)


def test_route_authority_markdown_table_list_and_wrapped_contexts_allowlist_governed_mentions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "README.md",
        """
        legacy redirect aliases (compatibility only; not active independent pages):

        | Old route | Target |
        |---|---|
        | `/overview`, `/hydro-met`, `/forecast` | `/` |

        - Legacy compatibility aliases:
          `/meteorology`, `/flood-alerts`

        Current route authority: `/` is active display proof. `/basins/:id` and
        `/segments/:id` only belong to legacy redirect /
        compatibility context.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {
        "/overview",
        "/hydro-met",
        "/forecast",
        "/meteorology",
        "/flood-alerts",
        "/basins/:id",
        "/segments/:id",
    }
    assert by_token["/hydro-met"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    assert by_token["/meteorology"]["allowlist_key"] == (
        "stale-display-route-token:legacy-route-compatibility-context"
    )
    assert by_token["/basins/:id"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)


@pytest.mark.parametrize(
    "continuation",
    [
        "  all `replace` redirect to `/` with semantic query parameters.",
        "  all old aliases redirect to `/` with semantic query parameters.",
        "  全 `replace` 重定向到 `/` + 语义参数。",
    ],
)
def test_route_authority_wrapped_list_redirect_continuation_allowlists_route_list(
    tmp_path: Path,
    continuation: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"""
        - Legacy display routes:
          (`/hydro-met`/`/overview`/`/forecast`/`/meteorology`)
        {continuation}
        - Open /flood-alerts for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {
        "/hydro-met",
        "/overview",
        "/forecast",
        "/meteorology",
        "/flood-alerts",
    }
    for token in ("/hydro-met", "/overview", "/forecast", "/meteorology"):
        assert by_token[token]["allowlist_key"] == (
            "stale-display-route-token:m26-route-consolidation-or-redirect"
        )
        assert by_token[token]["allowlist_state"] == "allowlisted"
        assert by_token[token]["budget_counted"] is False
    _assert_unallowlisted_budget_counted_report_only_finding(by_token["/flood-alerts"])


def test_route_authority_top_level_sibling_list_context_does_not_allowlist_active_route(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Compatibility context keeps /hydro-met deep links.
        - Open /forecast.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


def test_route_authority_blockquoted_sibling_list_context_does_not_allowlist_active_route(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Historical compatibility redirect keeps /hydro-met deep links.
        > - Open /forecast for current live browser proof.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


def test_route_authority_top_level_sibling_list_context_does_not_allowlist_route_value(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Historical pre-M26 evidence used /hydro-met.
        - BASE_URL=$BASE_URL/forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_line = {finding["line"]: finding for finding in findings}

    assert set(by_line) == {1, 2}
    legacy = by_line[1]
    assert "/hydro-met" in str(legacy["description"])
    assert legacy["allowlist_key"] == "stale-display-route-token:historical-plan-or-pre-m26-evidence"
    assert legacy["allowlist_state"] == "allowlisted"
    assert legacy["budget_counted"] is False

    active = by_line[2]
    assert "/forecast" in str(active["description"])
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["allowlist_state"] == "unallowlisted"
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("- Compatibility context keeps /forecast deep links.\n", "/forecast"),
        ("- Compatibility context keeps legacy deep links:\n  /forecast\n", "/forecast"),
    ],
)
def test_route_authority_same_item_and_continuation_list_contexts_still_allowlist_route_mentions(
    tmp_path: Path,
    text: str,
    token: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert token in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_blockquoted_same_item_continuation_inherits_list_context(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Compatibility context keeps legacy deep links:
        >   /forecast
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "text",
    [
        """
        > - Compatibility context keeps legacy deep links:
        >   - /forecast
        """,
        """
        - Compatibility context keeps legacy deep links:
          - /forecast
        """,
    ],
)
def test_route_authority_nested_child_list_inherits_parent_context(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["line"] == 2
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_key"] == "stale-display-route-token:legacy-route-compatibility-context"
    assert finding["allowlist_state"] == "allowlisted"
    assert finding["budget_counted"] is False
    assert finding["gate_eligible"] is False


def test_route_authority_current_runbook_mixed_active_and_redirect_contexts_are_distinct(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current live browser proof; /hydro-met -> / redirect alias.\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_comma_sibling_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current proof, /hydro-met -> / redirect alias.\n",
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /forecast for current proof /hydro-met -> / redirect alias.\n",
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_inherited_parent_list_redirect_context_does_not_allowlist_active_child_mixed_line(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Legacy redirect aliases:
          - Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_blockquoted_parent_list_redirect_context_does_not_allowlist_active_child_mixed_line(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        > - Legacy redirect aliases:
        >   - Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        """
        # Legacy redirect aliases

        Open /forecast for current proof /hydro-met -> / redirect alias.
        """,
        """
        Legacy redirect aliases:

        | Proof |
        |---|
        | Open /forecast for current proof /hydro-met -> / redirect alias |
        """,
    ],
)
def test_route_authority_inherited_heading_or_table_redirect_context_does_not_allowlist_active_mixed_line(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_current_runbook_table_cell_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Active proof | Redirect alias |
        |---|---|
        | Open /forecast for current proof | /hydro-met -> / redirect alias |
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def test_route_authority_current_runbook_same_table_cell_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        | Proof |
        |---|
        | Open /forecast for current proof /hydro-met -> / redirect alias |
        """,
    )

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_current_runbook_same_list_item_redirect_context_is_per_mention(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        """
        - Open /forecast for current proof,
          /hydro-met -> / redirect alias.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


@pytest.mark.parametrize(
    "text",
    [
        "- Open /forecast for current proof /hydro-met -> / redirect alias.\n",
        "- Open /forecast for current proof\n  /hydro-met -> / redirect alias.\n",
    ],
)
def test_route_authority_current_runbook_same_list_item_no_delimiter_redirect_context_is_per_mention(
    tmp_path: Path,
    text: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", text)

    _assert_forecast_active_and_hydro_redirect(_route_authority_findings(tmp_path))


def test_route_authority_current_runbook_same_route_mixed_contexts_keep_active_finding(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "/hydro-met -> / redirect alias; Open /hydro-met for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)
    hydro_findings = [finding for finding in findings if "/hydro-met" in str(finding["description"])]

    assert len(hydro_findings) == 2
    assert {finding["allowlist_state"] for finding in hydro_findings} == {
        "allowlisted",
        "unallowlisted",
    }
    active = next(finding for finding in hydro_findings if finding["allowlist_state"] == "unallowlisted")
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False


@pytest.mark.parametrize(
    ("token", "line"),
    [
        (
            "/forecast",
            "/forecast redirects to / and Visit /forecast for current display proof.",
        ),
        (
            "/forecast",
            "/forecast redirects to / and current route link ?next=/forecast.",
        ),
        (
            "/forecast",
            "/forecast redirects to / and Current route link: ${BASE_URL}/forecast.",
        ),
        (
            "/hydro-met",
            "/hydro-met redirects to / and Open /hydro-met for current live browser proof.",
        ),
        (
            "/hydro-met",
            "/hydro-met redirects to / and --path=/hydro-met current display proof.",
        ),
        (
            "/forecast",
            "/forecast 重定向到 / 且打开 /forecast 做 current browser proof.",
        ),
    ],
)
def test_route_authority_current_runbook_same_token_redirect_first_mixed_line_keeps_active_finding(
    tmp_path: Path,
    token: str,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)
    token_findings = [finding for finding in findings if token in str(finding["description"])]

    assert len(token_findings) == 2
    by_state = {finding["allowlist_state"]: finding for finding in token_findings}
    assert set(by_state) == {"allowlisted", "unallowlisted"}
    assert by_state["allowlisted"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    _assert_unallowlisted_budget_counted_report_only_finding(by_state["unallowlisted"])


@pytest.mark.parametrize(
    "line",
    [
        "Open /forecast for current live browser proof -> capture the receipt.",
        "Open /forecast for current compatibility/deep-link browser proof.",
    ],
)
def test_route_authority_current_runbook_active_line_with_unrelated_allowlist_words_is_drift(
    tmp_path: Path,
    line: str,
) -> None:
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{line}\n")

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False


@pytest.mark.parametrize(
    "context_line",
    [
        "Compatibility context keeps legacy deep links available.",
        "Historical pre-M26 evidence used legacy display aliases.",
    ],
)
def test_route_authority_current_runbook_adjacent_allowlist_context_does_not_allowlist_active_line(
    tmp_path: Path,
    context_line: str,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        f"{context_line}\nOpen /forecast.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/forecast" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["allowlist_key"] is None


def test_route_authority_legacy_hydro_met_token_uses_route_boundaries(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Ignore foo/hydro-met and some/path/hydro-met in non-route path examples.\n",
    )

    assert _route_authority_findings(tmp_path) == []

    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open /hydro-met for current live browser proof.\n",
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert "/hydro-met" in str(finding["description"])
    assert finding["allowlist_state"] == "unallowlisted"


def test_route_authority_m26_references_preserve_expected_allowlist_keys(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "openspec" / "changes" / "m26-unified-map-display" / "proposal.md",
        """
        `/hydro-met` and `/forecast` redirect to `/`.
        Delete `HydroMetPage` after preserving historical pre-M26 evidence.
        """,
    )

    findings = _route_authority_findings(tmp_path)
    by_token = _route_authority_findings_by_token(findings)

    assert by_token["/hydro-met"]["allowlist_key"] == (
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    )
    hydro_page = next(finding for finding in findings if "HydroMetPage" in str(finding["description"]))
    assert hydro_page["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)


def test_route_authority_current_repo_m26_route_evidence_preserves_m26_allowlist_key() -> None:
    expected_rows = {
        ("openspec/changes/m26-unified-map-display/proposal.md", 15, "HydroMetPage"),
        ("openspec/changes/m26-unified-map-display/proposal.md", 39, "/hydro-met"),
        ("openspec/changes/m26-unified-map-display/tasks.md", 12, "/hydro-met"),
        ("openspec/changes/m26-unified-map-display/tasks.md", 49, "/hydro-met"),
    }
    findings = [
        finding
        for finding in _route_authority_findings(REPO_ROOT)
        if (
            finding["evidence_path"],
            finding["line"],
            _route_authority_token_from_finding(finding),
        )
        in expected_rows
    ]

    assert {
        (
            finding["evidence_path"],
            finding["line"],
            _route_authority_token_from_finding(finding),
        )
        for finding in findings
    } == expected_rows
    assert {finding["allowlist_key"] for finding in findings} == {
        "stale-display-route-token:m26-route-consolidation-or-redirect"
    }
    assert all(finding["allowlist_state"] == "allowlisted" for finding in findings)
    assert all(finding["budget_counted"] is False for finding in findings)


def test_route_authority_large_line_matches_route_tokens_without_prefix_scan_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = ["/forecast", "/hydro-met", "/meteorology", "/flood-alerts"]
    large_line = " ".join(f"Open {tokens[index % len(tokens)]} for current proof." for index in range(800))
    _write(tmp_path / "docs" / "runbooks" / "current.md", f"{large_line}\n")

    call_count = 0
    original_mention_context = audit_repo_entropy._stale_route_mention_context

    def counting_mention_context(*args: object) -> object:
        nonlocal call_count
        call_count += 1
        return original_mention_context(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_context", counting_mention_context)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == len(tokens)
    assert set(_route_authority_findings_by_token(findings)) == set(tokens)
    assert call_count == len(tokens)


def test_route_authority_duplicate_tokens_dedupe_before_expensive_context_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_count = 400
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open " + " ".join("/forecast" for _index in range(token_count)) + " for current proof\n",
    )
    redirect_span_call_count = 0
    original_redirect_span = audit_repo_entropy._stale_route_mention_redirect_span

    def counting_redirect_span(*args: object) -> str:
        nonlocal redirect_span_call_count
        redirect_span_call_count += 1
        return original_redirect_span(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_redirect_span", counting_redirect_span)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert "/forecast" in str(findings[0]["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    assert redirect_span_call_count <= 1


def test_route_authority_duplicate_tokens_precompute_semantic_work_per_unique_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_count = 400
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "Open " + " ".join("/forecast" for _index in range(token_count)) + " for current proof\n",
    )
    semantic_key_call_count = 0
    route_valued_call_count = 0
    clause_analysis_call_count = 0
    original_semantic_key = audit_repo_entropy._stale_route_mention_semantic_key
    original_route_valued = audit_repo_entropy._route_token_is_route_valued
    original_clause_analysis = audit_repo_entropy._stale_route_clause_analysis

    def counting_semantic_key(*args: object) -> str:
        nonlocal semantic_key_call_count
        semantic_key_call_count += 1
        return original_semantic_key(*args)

    def counting_route_valued(*args: object) -> bool:
        nonlocal route_valued_call_count
        route_valued_call_count += 1
        return original_route_valued(*args)

    def counting_clause_analysis(
        line: str,
        start: int,
        end: int,
    ) -> audit_repo_entropy._StaleRouteClauseAnalysis:
        nonlocal clause_analysis_call_count
        clause_analysis_call_count += 1
        return original_clause_analysis(line, start, end)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_mention_semantic_key", counting_semantic_key)
    monkeypatch.setattr(audit_repo_entropy, "_route_token_is_route_valued", counting_route_valued)
    monkeypatch.setattr(audit_repo_entropy, "_stale_route_clause_analysis", counting_clause_analysis)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    assert "/forecast" in str(findings[0]["description"])
    _assert_unallowlisted_budget_counted_report_only_finding(findings[0])
    assert semantic_key_call_count == 0
    assert route_valued_call_count == 0
    assert clause_analysis_call_count == 1


def test_route_authority_list_structural_context_is_cached_per_list_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 80
    route_lines = [f"  Open /forecast for current proof {index}." for index in range(route_line_count)]
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "- Current operator procedure:\n" + "\n".join(route_lines) + "\n",
    )
    list_item_end_call_count = 0
    original_list_item_end_index = audit_repo_entropy._list_item_end_index

    def counting_list_item_end_index(*args: object) -> int:
        nonlocal list_item_end_call_count
        list_item_end_call_count += 1
        return original_list_item_end_index(*args)

    monkeypatch.setattr(audit_repo_entropy, "_list_item_end_index", counting_list_item_end_index)

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert list_item_end_call_count <= 2


def test_route_authority_paragraph_structural_context_is_cached_per_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 60
    route_lines = [
        f"Open /forecast for current proof {index} and"
        for index in range(route_line_count - 1)
    ]
    route_lines.append(f"Open /forecast for current proof {route_line_count - 1}.")
    _write(tmp_path / "docs" / "runbooks" / "current.md", "\n".join(route_lines) + "\n")
    paragraph_call_count = 0
    original_paragraph_text = audit_repo_entropy._stale_route_paragraph_governing_text_for_range

    def counting_paragraph_text(*args: object) -> str:
        nonlocal paragraph_call_count
        paragraph_call_count += 1
        return original_paragraph_text(*args)

    monkeypatch.setattr(
        audit_repo_entropy,
        "_stale_route_paragraph_governing_text_for_range",
        counting_paragraph_text,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert paragraph_call_count == 1


def test_route_authority_blockquote_paragraph_structural_context_is_cached_per_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_line_count = 60
    route_lines = [
        f"> Open /forecast for current proof {index} and"
        for index in range(route_line_count - 1)
    ]
    route_lines.append(f"> Open /forecast for current proof {route_line_count - 1}.")
    _write(tmp_path / "docs" / "runbooks" / "current.md", "\n".join(route_lines) + "\n")
    paragraph_call_count = 0
    original_paragraph_text = audit_repo_entropy._stale_route_paragraph_governing_text_for_range

    def counting_paragraph_text(*args: object) -> str:
        nonlocal paragraph_call_count
        paragraph_call_count += 1
        return original_paragraph_text(*args)

    monkeypatch.setattr(
        audit_repo_entropy,
        "_stale_route_paragraph_governing_text_for_range",
        counting_paragraph_text,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == route_line_count
    assert all("/forecast" in str(finding["description"]) for finding in findings)
    assert all(finding["allowlist_state"] == "unallowlisted" for finding in findings)
    assert paragraph_call_count == 1


def test_route_authority_route_free_large_markdown_does_not_build_governing_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "\n".join(f"# Current operator procedure {index}\nNo legacy route token here." for index in range(600))
        + "\n",
    )
    call_count = 0
    original_line_context = audit_repo_entropy._stale_route_line_context

    def counting_line_context(*args: object) -> object:
        nonlocal call_count
        call_count += 1
        return original_line_context(*args)

    monkeypatch.setattr(audit_repo_entropy, "_stale_route_line_context", counting_line_context)

    findings = _route_authority_findings(tmp_path)

    assert findings == []
    assert call_count == 0


def test_route_authority_current_repo_has_no_unallowlisted_findings_in_current_docs() -> None:
    guarded_entrypoints = {"README.md", "progress.md", "CLAUDE.md", "docs/governance/DOC_STATUS.md"}
    findings = [
        finding
        for finding in _route_authority_findings(REPO_ROOT)
        if finding["check_id"] == "stale-display-route-token"
        and finding["allowlist_state"] == "unallowlisted"
        and (
            str(finding["evidence_path"]).startswith("docs/runbooks/")
            or finding["evidence_path"] in guarded_entrypoints
        )
    ]

    assert findings == []


def test_route_authority_legacy_alias_coverage_includes_all_current_redirect_forms(
    tmp_path: Path,
) -> None:
    expected_tokens = {
        "/overview",
        "/hydro-met",
        "/forecast",
        "/meteorology",
        "/flood-alerts",
        "/basins/:id",
        "/segments/:id",
        "/basins/demo",
        "/segments/demo",
    }
    _write(
        tmp_path / "docs" / "runbooks" / "current.md",
        "\n".join(f"Open {token} for current live browser proof." for token in sorted(expected_tokens)),
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == len(expected_tokens)
    descriptions = "\n".join(str(finding["description"]) for finding in findings)
    for token in expected_tokens:
        assert token in descriptions


def test_route_authority_expanded_aliases_do_not_scan_frontend_e2e_unless_old_token(
    tmp_path: Path,
) -> None:
    e2e_path = tmp_path / "apps" / "frontend" / "e2e" / "m11-routes.spec.ts"
    _write(
        e2e_path,
        """
        await page.goto('/overview')
        await page.goto('/forecast')
        await page.goto('/flood-alerts')
        await page.goto('/basins/demo')
        await page.goto('/segments/demo')
        """,
    )

    assert _route_authority_findings(tmp_path) == []

    _write(
        e2e_path,
        """
        await page.goto('/overview')
        await page.goto('/forecast')
        await page.goto('/flood-alerts')
        await page.goto('/basins/demo')
        await page.goto('/segments/demo')
        await page.goto('/hydro-met')
        """,
    )

    findings = _route_authority_findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["evidence_path"] == "apps/frontend/e2e/m11-routes.spec.ts"
    assert "/hydro-met" in finding["description"]
    assert all(
        token not in str(finding["description"])
        for token in ("/overview", "/forecast", "/flood-alerts", "/basins/demo", "/segments/demo")
    )


def test_route_authority_expanded_aliases_do_not_scan_app_route_source_of_truth(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "src" / "App.tsx",
        """
        <Route path="/overview" element={<LegacyRedirect />} />
        <Route path="/forecast" element={<LegacyRedirect />} />
        <Route path="/flood-alerts" element={<LegacyRedirect />} />
        <Route
          path="/basins/:basinId"
          element={<LegacyRedirect param={{ name: 'basinId', queryKey: 'basinId' }} />}
        />
        <Route
          path="/segments/:segmentId"
          element={<LegacyRedirect param={{ name: 'segmentId', queryKey: 'segmentId' }} />}
        />
        """,
    )

    assert _route_authority_findings(tmp_path) == []


def test_route_authority_expanded_aliases_do_not_scan_frontend_fixtures(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path / "apps" / "frontend" / "src" / "fixtures" / "routes.ts",
        """
        export const fixtureRoutes = [
          '/overview',
          '/forecast',
          '/flood-alerts',
          '/basins/demo',
          '/segments/demo',
        ]
        """,
    )

    assert _route_authority_findings(tmp_path) == []


def test_route_authority_skips_generated_artifact_roots(tmp_path: Path) -> None:
    _write(tmp_path / "artifacts" / "generated.md", "Open /overview for current proof.\n")

    assert _route_authority_findings(tmp_path) == []


def _findings_by_check(root: Path, check_id: str) -> list[dict[str, object]]:
    return [
        finding
        for finding in audit_repo_entropy.build_report(root)["findings"]
        if finding["check_id"] == check_id
    ]


def _route_authority_findings(root: Path) -> list[dict[str, object]]:
    return _findings_by_check(root, "stale-display-route-token")


def _route_authority_findings_by_token(
    findings: Iterable[dict[str, object]],
) -> dict[str, dict[str, object]]:
    by_token: dict[str, dict[str, object]] = {}
    for finding in findings:
        token = _route_authority_token_from_finding(finding)
        if token is not None:
            by_token[token] = finding
    return by_token


def _route_authority_token_from_finding(finding: dict[str, object]) -> str | None:
    description = str(finding["description"])
    if "HydroMetPage" in description:
        return "HydroMetPage"
    match = audit_repo_entropy.LEGACY_DISPLAY_ROUTE_PATTERN.search(description)
    return match.group("token") if match else None


def _assert_forecast_active_and_hydro_redirect(findings: Iterable[dict[str, object]]) -> None:
    by_token = _route_authority_findings_by_token(findings)

    assert set(by_token) == {"/forecast", "/hydro-met"}
    active = by_token["/forecast"]
    redirect = by_token["/hydro-met"]
    assert active["allowlist_state"] == "unallowlisted"
    assert active["allowlist_reason"] is None
    assert active["allowlist_key"] is None
    assert active["budget_counted"] is True
    assert active["gate_eligible"] is False
    assert redirect["allowlist_state"] == "allowlisted"
    assert redirect["allowlist_key"] == "stale-display-route-token:m26-route-consolidation-or-redirect"


def _run_entropy_audit_cli(
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), *args],
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
    )


def _run_entropy_baseline_writer_cli(
    repo_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BASELINE_WRITER_SCRIPT), "--repo-root", str(repo_root), *args],
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _entropy_baseline_snapshot() -> dict[str, object]:
    assert BASELINE.exists(), "repository entropy baseline fixture must exist"
    latest_stat = BASELINE.stat()
    return {
        "latest_bytes": BASELINE.read_bytes(),
        "latest_stat": _stable_file_stat(latest_stat),
        "directory_entries": sorted(
            path.relative_to(BASELINE_DIR).as_posix()
            for path in BASELINE_DIR.rglob("*")
            if path.is_file()
        ),
    }


def _stable_file_stat(path_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_mode,
        path_stat.st_size,
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
    )


def _assert_unallowlisted_budget_counted_report_only_finding(
    finding: dict[str, object],
) -> None:
    assert finding["allowlist_reason"] is None
    assert finding["allowlist_key"] is None
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is False


def _assert_unallowlisted_budget_counted_gate_eligible_finding(
    finding: dict[str, object],
) -> None:
    assert finding["allowlist_reason"] is None
    assert finding["allowlist_key"] is None
    assert finding["allowlist_state"] == "unallowlisted"
    assert finding["budget_counted"] is True
    assert finding["gate_eligible"] is True


def _assert_required_baseline_fields(baseline: dict[str, object]) -> None:
    assert {
        "version",
        "timestamp",
        "repo",
        "branch",
        "commit",
        "summary",
        "metadata",
        "modules",
        "high_spread_patterns",
        "cleanup_priorities",
    } <= set(baseline)
    assert baseline["version"] == 1
    assert isinstance(baseline["timestamp"], str)
    assert isinstance(baseline["repo"], str)
    assert isinstance(baseline["branch"], str)
    assert isinstance(baseline["commit"], str)
    summary = baseline["summary"]
    assert isinstance(summary, dict)
    assert {
        "total_source_files",
        "total_test_files",
        "total_instruction_files",
        "total_modules",
        "modules_with_high_entropy",
        "overall_trend",
        "governance_finding_count",
        "budget_counted_count",
        "gate_eligible_count",
        "check_family_count",
    } <= set(summary)
    for field in (
        "total_source_files",
        "total_test_files",
        "total_instruction_files",
        "total_modules",
        "modules_with_high_entropy",
        "governance_finding_count",
        "budget_counted_count",
        "gate_eligible_count",
        "check_family_count",
    ):
        assert isinstance(summary[field], int)
        assert summary[field] >= 0
    modules = baseline["modules"]
    assert isinstance(modules, dict)
    for row in modules.values():
        assert isinstance(row, dict)
        assert isinstance(row["file_count"], int)
        assert row["file_count"] >= 0


def _emitted_module_file_count_sum(modules: dict[str, object]) -> int:
    total = 0
    for row in modules.values():
        assert isinstance(row, dict)
        total += int(row["file_count"])
    return total


def _expected_services_orchestrator_file_count() -> int:
    tracked_paths = audit_repo_entropy._git_tracked_paths(REPO_ROOT, ("services/orchestrator",))
    expected = 16
    if "services/orchestrator/scheduler_execution.py" in tracked_paths:
        expected += 1
    return expected


def _baseline_archive_files(baseline_dir: Path) -> list[Path]:
    if not baseline_dir.exists():
        return []
    return sorted(path for path in baseline_dir.glob("*.json") if path.name != "latest.json")


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _file_bytes_by_relative_path(root: Path) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in _relative_files(root)}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _setup_agent_artifact_drift(root: Path) -> None:
    _write(root / "docs/governance/DOC_STATUS.md", "Governed docs placeholder.\n")
    _write(root / ".gitignore", "# intentionally incomplete\n")
    _write(root / ".dockerignore", "# intentionally incomplete\n")
    _write(root / "artifacts/leaked.txt", "generated\n")
    _init_git(root)
    subprocess.run(["git", "add", "artifacts/leaked.txt"], cwd=root, check=True)


def _setup_placeholder_path_drift(root: Path) -> None:
    _init_git(root)
    _write(root / "docs" / "active.md", "Still mentions apps/web.\n")
    _write(root / "apps" / "web" / "README.md", "retired placeholder\n")
    subprocess.run(["git", "add", "apps/web/README.md"], cwd=root, check=True)


def _setup_clean_hard_gate_fixture(root: Path) -> None:
    _write(
        root / "openapi" / "nhms.v1.yaml",
        """
        openapi: 3.1.0
        info:
          title: NHMS API
          version: 1.0.0
        paths: {}
        """,
    )
    _write(
        root / "apps" / "frontend" / "src" / "api" / "types.ts",
        """
        export interface paths {}
        export interface components {}
        """,
    )
    _write(
        root / "tests" / "test_openapi_drift.py",
        """
        def test_openapi_generated_types_are_current() -> None:
            assert True
        """,
    )
    _write(
        root / "docs" / "governance" / "DOC_STATUS.md",
        """
        .agents/skills/**
        .codex/tmp/
        .codex/cache/
        .codex/evidence/
        apps/frontend/artifacts/**
        Root `artifacts/`
        .dockerignore
        """,
    )
    _write(
        root / ".gitignore",
        """
        .codex/
        artifacts/
        apps/frontend/artifacts/
        """,
    )
    _write(
        root / ".dockerignore",
        """
        .agents
        .codex
        apps/frontend/artifacts
        """,
    )
    _init_git(root)


def _track_generated_artifact(root: Path) -> None:
    _write(root / "artifacts" / "leaked.txt", "generated\n")
    subprocess.run(["git", "add", "-f", "artifacts/leaked.txt"], cwd=root, check=True)


def _init_git(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Entropy Test",
            "-c",
            "user.email=entropy-test@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=root,
        check=True,
    )
