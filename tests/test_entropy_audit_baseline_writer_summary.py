"""The entropy baseline writer's v1 trend and summary counts (#1823 partition).

First write, archive-free creation, the v1 trend semantics for the current
repository, and the summary sibling/source/test/instruction counts derived from
the tracked module surface. The durability and refusal half is in
``tests/test_entropy_audit_baseline_writer_safety.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.governance import write_entropy_baseline
from tests.entropy_audit_helpers import (
    BASELINE,
    REPO_ROOT,
    _apps_frontend_baseline_counted_path_exists,
    _assert_required_baseline_fields,
    _baseline_archive_files,
    _emitted_module_file_count_sum,
    _expected_apps_frontend_file_count,
    _expected_baseline_instruction_count,
    _expected_baseline_test_count,
    _expected_module_file_count,
    _file_bytes_by_relative_path,
    _relative_files,
    _repo_report,
    _run_entropy_baseline_writer_cli,
    _write,
)


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
    report = _repo_report(REPO_ROOT)
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
    assert inventory.total_test_files == _expected_baseline_test_count()
    assert inventory.total_instruction_files == _expected_baseline_instruction_count()
    assert inventory.v1_summary_test_files == _expected_baseline_test_count(v1_summary=True)
    assert inventory.v1_summary_instruction_files == _expected_baseline_instruction_count(
        v1_summary=True
    )
    assert baseline["summary"]["total_test_files"] == inventory.v1_summary_test_files
    assert baseline["summary"]["total_instruction_files"] == inventory.v1_summary_instruction_files
    assert isinstance(baseline["summary"]["total_test_files"], int)
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("docs/runbooks/live.md")
    assert write_entropy_baseline._baseline_path_is_v1_summary_source_counted(
        "openspec/changes/example/spec.md"
    )
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("openapi/nhms.v1.yaml")
    assert not write_entropy_baseline._baseline_path_is_v1_summary_source_counted("README.md")
    assert write_entropy_baseline._baseline_path_is_v1_summary_source_counted("services/api/main.py")
    assert modules["apps/frontend"]["file_count"] == _expected_apps_frontend_file_count()
    assert _apps_frontend_baseline_counted_path_exists("apps/frontend/src/App.tsx")
    assert modules["services/production_closure"]["file_count"] == _expected_module_file_count(
        "services/production_closure"
    )
    assert modules["services/slurm_gateway"]["file_count"] == _expected_module_file_count(
        "services/slurm_gateway"
    )
    for zero_count_module in (
        "docs/governance",
        "docs/runbooks",
        "openapi",
        "openspec/archive",
        "progress.md",
    ):
        assert zero_count_module in modules
        assert modules[zero_count_module]["file_count"] == 0

    orchestrator = modules["services/orchestrator"]
    assert isinstance(orchestrator, dict)
    assert orchestrator["file_count"] == _expected_module_file_count("services/orchestrator")
    expected_orchestrator_scoped_findings = (
        0 if (REPO_ROOT / "services" / "orchestrator" / "AGENTS.md").is_file() else 1
    )
    assert orchestrator["finding_count"] == expected_orchestrator_scoped_findings
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


def test_services_orchestrator_file_count_matches_tracked_module_surface() -> None:
    report = _repo_report(REPO_ROOT)
    baseline = write_entropy_baseline.build_baseline_snapshot(REPO_ROOT, report)

    orchestrator = baseline["modules"]["services/orchestrator"]
    assert isinstance(orchestrator, dict)
    assert orchestrator["file_count"] == _expected_module_file_count("services/orchestrator")


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
