"""Shared entropy-audit test helpers (non-collectible support module).

#1823 partitioned the 9860-line ``tests/test_entropy_audit_script.py`` into the
fifteen ``tests/test_entropy_audit_*.py`` suites. Everything below is that
monolith's module prefix (line 17-75: the repository/baseline path constants and
the memoized ``build_report`` accessor) and its private helper tail (line
9241-9860: finding selectors, fixture builders and shared assertions), moved here
verbatim so every partition imports one owner instead of copying them.

The filename deliberately does not start with ``test_``: pytest must not collect
it, and ``scripts/select_ci_tests.py`` routes it through
``SUPPORT_MODULE_TEST_RULES`` rather than the ``tests/**`` suite branch.

Design D1 note for the monkeypatch surface: none of the names patched by the
partitions live here. Every ``monkeypatch.setattr`` target is a real
``scripts.governance.audit_repo_entropy`` / ``scripts.governance.write_entropy_baseline``
attribute and #1823 moved no production code, so each patch still lands on the
module that owns the call site.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterable
from pathlib import Path

from scripts.governance import audit_repo_entropy, write_entropy_baseline

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO_ROOT / ".entropy-baseline"
BASELINE = REPO_ROOT / ".entropy-baseline" / "latest.json"
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "governance" / "audit_repo_entropy.py"
BASELINE_WRITER_SCRIPT = REPO_ROOT / "scripts" / "governance" / "write_entropy_baseline.py"

_REPO_REPORT_MEMO: dict[tuple[str, str | None], dict[str, object]] = {}


def _repo_report(
    root: Path,
    *,
    mode: audit_repo_entropy.AuditMode = "report",
    structural_base_ref: str | None = None,
) -> dict[str, object]:
    """Return ``build_report(root, ...)``, memoized for the whole-repository root.

    A full-repository ``build_report`` walks every tracked file and costs ~20s per
    call on a developer machine; this module asks for the same
    ``(mode, structural_base_ref)`` combination from roughly ten places, which used
    to dominate the file's runtime.

    Memoizing is sound because ``build_report`` is a pure function of the tree it
    scans except for ``metadata.generated_at``, a wall-clock timestamp: two
    back-to-back calls on this repository compare equal in every other key,
    including the full ``findings`` list, and no assertion in this file reads
    ``generated_at`` off a real report (the only ``generated_at`` occurrences are
    literals inside synthetic baseline fixtures). No test in this file writes into
    the repository tree, so the scanned inputs cannot change mid-session.

    Two guardrails keep the memo from leaking:

    * Only ``REPO_ROOT`` is memoized. A ``tmp_path`` root is built fresh by each
      test, so it always recomputes -- which also preserves the tests that
      ``monkeypatch.setattr(audit_repo_entropy, "build_report", ...)``, since the
      passthrough resolves the attribute at call time.
    * Callers get a deep copy, so a test that mutates its report cannot corrupt a
      sibling test's view. The copy costs milliseconds against a ~20s rebuild.
    """
    if root != REPO_ROOT:
        return audit_repo_entropy.build_report(
            root,
            mode=mode,
            structural_base_ref=structural_base_ref,
        )
    key = (mode, structural_base_ref)
    if key not in _REPO_REPORT_MEMO:
        _REPO_REPORT_MEMO[key] = audit_repo_entropy.build_report(
            REPO_ROOT,
            mode=mode,
            structural_base_ref=structural_base_ref,
        )
    return copy.deepcopy(_REPO_REPORT_MEMO[key])


def _structural_public_surface_detail(*tokens: str) -> str:
    return "new public surface tokens: " + ", ".join(
        audit_repo_entropy._structural_bounded_detail_token(token) for token in tokens
    )


def _findings_by_check(
    root: Path,
    check_id: str,
    *,
    structural_base_ref: str | None = None,
) -> list[dict[str, object]]:
    return [
        finding
        for finding in _repo_report(root, structural_base_ref=structural_base_ref)["findings"]
        if finding["check_id"] == check_id
    ]


def _route_authority_findings(root: Path) -> list[dict[str, object]]:
    return _findings_by_check(root, "stale-display-route-token")


def _compatibility_facade_guard(root: Path, structural_base_ref: str) -> dict[str, object]:
    report = _repo_report(root, structural_base_ref=structural_base_ref)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    guard = metadata["compatibility_facade_guard"]
    assert isinstance(guard, dict)
    return guard


def _scoped_agent_context(root: Path) -> dict[str, object]:
    report = _repo_report(root)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    context = metadata["scoped_agent_context"]
    assert isinstance(context, dict)
    return context


def _scoped_agent_context_signals(root: Path, signal_type: str) -> list[dict[str, object]]:
    context = _scoped_agent_context(root)
    signals = context["signals"]
    assert isinstance(signals, list)
    return [
        signal
        for signal in signals
        if isinstance(signal, dict) and signal["signal_type"] == signal_type
    ]


def _scoped_agent_context_scope(root: Path, instruction_path: str) -> dict[str, object]:
    context = _scoped_agent_context(root)
    scopes = context["scopes"]
    assert isinstance(scopes, list)
    for scope in scopes:
        assert isinstance(scope, dict)
        if scope["instruction_path"] == instruction_path:
            return scope
    raise AssertionError(f"missing scoped context record for {instruction_path}")


def _compatibility_facade_signals(
    root: Path,
    structural_base_ref: str,
    signal_type: str,
) -> list[dict[str, object]]:
    guard = _compatibility_facade_guard(root, structural_base_ref)
    signals = guard["signals"]
    assert isinstance(signals, list)
    return [
        signal
        for signal in signals
        if isinstance(signal, dict) and signal["signal_type"] == signal_type
    ]


def _assert_compatibility_facade_report_only_finding(
    root: Path,
    structural_base_ref: str,
    message_key: str,
) -> None:
    findings = [
        finding
        for finding in _findings_by_check(
            root,
            audit_repo_entropy.COMPATIBILITY_FACADE_GUARD_CHECK_ID,
            structural_base_ref=structural_base_ref,
        )
        if message_key in str(finding["description"])
    ]
    assert findings, f"expected report-only finding with {message_key}"
    assert findings[0]["check_id"] == audit_repo_entropy.COMPATIBILITY_FACADE_GUARD_CHECK_ID
    assert findings[0]["gate_eligible"] is False
    assert findings[0]["budget_counted"] is True


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


def _expected_module_file_count(module: str) -> int:
    return len(_baseline_counted_paths_for_module(module))


def _baseline_counted_paths_for_module(module: str) -> set[str]:
    tracked_paths = audit_repo_entropy._git_tracked_paths(REPO_ROOT, (module,))
    return {
        relative_path
        for relative_path in tracked_paths
        if not write_entropy_baseline._baseline_path_is_file_count_skipped(relative_path)
        and write_entropy_baseline._baseline_path_is_v1_source_counted(relative_path)
        and audit_repo_entropy._module_for_relative(relative_path) == module
    }


def _expected_baseline_test_count(*, v1_summary: bool = False) -> int:
    return _expected_baseline_sibling_count(
        write_entropy_baseline._baseline_path_is_test,
        write_entropy_baseline._baseline_path_is_v1_summary_test_counted,
        v1_summary=v1_summary,
    )


def _expected_baseline_instruction_count(*, v1_summary: bool = False) -> int:
    return _expected_baseline_sibling_count(
        write_entropy_baseline._baseline_path_is_instruction,
        write_entropy_baseline._baseline_path_is_v1_summary_instruction_counted,
        v1_summary=v1_summary,
    )


def _expected_baseline_sibling_count(
    classifier: Callable[[str], bool],
    v1_summary_classifier: Callable[[str], bool],
    *,
    v1_summary: bool,
) -> int:
    count = 0
    for relative_path in audit_repo_entropy._git_tracked_paths(REPO_ROOT):
        if write_entropy_baseline._baseline_path_is_file_count_skipped(relative_path):
            continue
        if audit_repo_entropy._repo_text_rejection_reason(REPO_ROOT, REPO_ROOT / relative_path) is not None:
            continue
        if v1_summary:
            count += int(v1_summary_classifier(relative_path))
        else:
            count += int(classifier(relative_path))
    return count


def _expected_apps_frontend_file_count() -> int:
    return len(_apps_frontend_baseline_counted_paths())


def _apps_frontend_baseline_counted_path_exists(relative_path: str) -> bool:
    return relative_path in _apps_frontend_baseline_counted_paths()


def _apps_frontend_baseline_counted_paths() -> set[str]:
    return _baseline_counted_paths_for_module("apps/frontend")


def _baseline_archive_files(baseline_dir: Path) -> list[Path]:
    if not baseline_dir.exists():
        return []
    return sorted(path for path in baseline_dir.glob("*.json") if path.name != "latest.json")


def _structural_budget(root: Path, *, structural_base_ref: str | None = None) -> dict[str, object]:
    report = _repo_report(root, structural_base_ref=structural_base_ref)
    metadata = report["metadata"]
    assert isinstance(metadata, dict)
    budget = metadata["structural_file_budget"]
    assert isinstance(budget, dict)
    return budget


def _structural_records_by_path(records: object) -> dict[str, dict[str, object]]:
    assert isinstance(records, list)
    by_path: dict[str, dict[str, object]] = {}
    for record in records:
        assert isinstance(record, dict)
        by_path[str(record["path"])] = record
    return by_path


def _structural_growth_signal_types(budget: dict[str, object], path: str) -> set[str]:
    signals = budget["ownership_growth_signals"]
    assert isinstance(signals, list)
    return {
        str(signal["signal_type"])
        for signal in signals
        if isinstance(signal, dict) and signal["path"] == path
    }


def _structural_growth_signal_details(
    budget: dict[str, object],
    path: str,
    signal_type: str,
) -> list[str]:
    signals = budget["ownership_growth_signals"]
    assert isinstance(signals, list)
    return [
        str(signal["detail"])
        for signal in signals
        if isinstance(signal, dict)
        and signal["path"] == path
        and signal["signal_type"] == signal_type
    ]


def _git_rev_parse(root: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def _structural_python_fixture(line_count: int, *header_lines: str) -> str:
    assert line_count >= len(header_lines)
    lines = [*header_lines]
    lines.extend(f"VALUE_{index} = {index}" for index in range(line_count - len(header_lines)))
    return "\n".join(lines) + "\n"


def _structural_ts_fixture(line_count: int, *header_lines: str) -> str:
    assert line_count >= len(header_lines)
    lines = [*header_lines]
    lines.extend(f"export const value{index} = {index};" for index in range(line_count - len(header_lines)))
    return "\n".join(lines) + "\n"


def _structural_ts_private_fixture(line_count: int, *header_lines: str) -> str:
    assert line_count >= len(header_lines)
    lines = [*header_lines]
    lines.extend(f"const value{index} = {index};" for index in range(line_count - len(header_lines)))
    return "\n".join(lines) + "\n"


def _structural_yaml_fixture(line_count: int, *header_lines: str) -> str:
    assert line_count >= len(header_lines)
    lines = [*header_lines]
    lines.extend(f"package_{index}: {index}" for index in range(line_count - len(header_lines)))
    return "\n".join(lines) + "\n"


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _file_bytes_by_relative_path(root: Path) -> dict[str, bytes]:
    return {path: (root / path).read_bytes() for path in _relative_files(root)}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def _complete_archive_status_front_matter(body: str) -> str:
    return f"""
    ---
    status: archived
    current_authority:
      - path: docs/governance/DOC_STATUS.md
        section: Archive And Supersession Markers
        reason: archive marker semantics
    superseded_by: none
    status_since: 2026-06-24
    archive_scope: whole-document
    retained_for: audit evidence
    ---
    {body}
    """


def _complete_historical_baseline_front_matter(body: str = "") -> str:
    return f"""
    ---
    status: historical baseline
    current_authority:
      - path: docs/runbooks/current-production-ops.md
        section: Current production operations
        reason: current node-22/node-27 production authority
    status_since: 2026-08-24
    archive_scope: whole-document
    retained_for: historical bring-up and incident evidence
    ---
    {body}
    """


def _append_inventory_line(root: Path, relative_path: str, line: str) -> None:
    path = root / relative_path
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def _setup_compatibility_facade_guard_fixture(root: Path) -> str:
    _init_git(root)
    _write(root / "services" / "orchestrator" / "scheduler_state.py", "\n")
    _write(root / "services" / "orchestrator" / "chain_manifests.py", "\n")
    _write(
        root / "services" / "orchestrator" / "scheduler.py",
        """
        from __future__ import annotations

        from services.orchestrator import scheduler_state as _scheduler_state

        ExistingSchedulerAlias = _scheduler_state.ExistingSchedulerAlias

        def existing_scheduler_forwarder(value: object) -> object:
            return _scheduler_state.existing_scheduler_forwarder(value)
        """,
    )
    _write(
        root / "services" / "orchestrator" / "chain.py",
        """
        from __future__ import annotations

        from services.orchestrator import chain_manifests

        ExistingChainAlias = chain_manifests.ExistingChainAlias

        def existing_chain_forwarder(value: object) -> object:
            return chain_manifests.existing_chain_forwarder(value)
        """,
    )
    _write(
        root / "docs" / "governance" / "SCHEDULER_COMPATIBILITY_INVENTORY.md",
        """
        # Scheduler Compatibility Inventory

        ## Guard Hook Seed

        - ExistingSchedulerAlias
        - existing_scheduler_forwarder
        """,
    )
    _write(
        root / "docs" / "governance" / "CHAIN_COMPATIBILITY_INVENTORY.md",
        """
        # Chain Compatibility Inventory

        PipelineEvent appears here as owner-context prose only; it is not a
        Guard Hook Seed selector until listed below.

        ## Guard Hook Seed

        - ExistingChainAlias
        - existing_chain_forwarder
        """,
    )
    _commit_all(root, "base facade inventories")
    return _git_rev_parse(root, "HEAD")


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
