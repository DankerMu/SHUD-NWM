"""The governance workflow runs the entropy hard gate on every change (#2602).

ci.yml's `backend` path filter skips both pytest jobs for docs-, openspec- and
instructions-only changes, so the pytest node that asserts zero hard-gate
findings never ran for exactly the changes most likely to introduce a
production-topology finding. ``governance.yml`` has no path filter; a separate
job there runs the hard-gate CLI and its exit code decides the job. The
existing report-only job keeps its report-mode contract.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

GOVERNANCE_WORKFLOW = Path(".github/workflows/governance.yml")
HARD_GATE_JOB = "production-topology-hard-gate"
REPORT_ONLY_JOB = "entropy-audit"


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(GOVERNANCE_WORKFLOW.read_text(encoding="utf-8"))


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    return workflow.get("on", workflow.get(True))


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def _hard_gate_violations(workflow: dict[Any, Any]) -> list[str]:
    violations: list[str] = []
    triggers = _triggers(workflow)
    if "pull_request" not in triggers:
        violations.append("workflow does not run on pull_request")
    if (triggers.get("push") or {}).get("branches") != ["master"]:
        violations.append("workflow does not run on master push")
    for event in ("push", "pull_request"):
        filters = triggers.get(event) or {}
        if any(key in filters for key in ("paths", "paths-ignore")):
            violations.append(f"{event} trigger is path-filtered")
    job = workflow["jobs"].get(HARD_GATE_JOB)
    if job is None:
        return [*violations, "hard-gate job missing"]
    if job.get("name") != "Production Topology Hard Gate":
        violations.append("hard-gate job display name changed")
    if "if" in job or "needs" in job:
        violations.append("hard-gate job is conditional")
    if job.get("continue-on-error"):
        violations.append("hard-gate job may not absorb its failure")
    runs = _runs(job)
    if "scripts/governance/audit_repo_entropy.py --mode hard-gate --format json" not in runs:
        violations.append("hard-gate job does not run the hard-gate CLI")
    if "|| rc=$?" not in runs or "exit $rc" not in runs:
        violations.append("hard-gate job does not exit with the CLI's return code")
    if any(step.get("continue-on-error") or "if" in step for step in job["steps"]):
        violations.append("a hard-gate step is conditional or absorbs failure")
    return violations


def _report_only_violations(workflow: dict[Any, Any]) -> list[str]:
    job = workflow["jobs"][REPORT_ONLY_JOB]
    runs = _runs(job)
    violations: list[str] = []
    if job.get("name") != "Entropy Audit (report-only)":
        violations.append("report-only job display name changed")
    if "--mode hard-gate" in runs:
        violations.append("report-only job passes --mode hard-gate")
    if 'assert metadata["mode"] == "report-only", metadata' not in runs:
        violations.append("report-only job lost its report-mode assertion")
    if "git diff --exit-code -- .entropy-baseline/latest.json" not in runs:
        violations.append("report-only job lost its baseline no-write guard")
    return violations


def test_hard_gate_job_runs_on_every_pull_request_and_master_push() -> None:
    assert _hard_gate_violations(_workflow()) == []


def test_report_only_job_keeps_its_report_mode_contract() -> None:
    assert _report_only_violations(_workflow()) == []


def test_hard_gate_guard_reds_on_a_path_filter_or_a_job_condition() -> None:
    path_filtered = copy.deepcopy(_workflow())
    _triggers(path_filtered)["pull_request"] = {"paths": ["**/*.py"]}
    conditional = copy.deepcopy(_workflow())
    conditional["jobs"][HARD_GATE_JOB]["if"] = "github.event_name == 'push'"
    removed = copy.deepcopy(_workflow())
    del removed["jobs"][HARD_GATE_JOB]

    assert "pull_request trigger is path-filtered" in _hard_gate_violations(path_filtered)
    assert "hard-gate job is conditional" in _hard_gate_violations(conditional)
    assert "hard-gate job missing" in _hard_gate_violations(removed)


def test_hard_gate_guard_reds_when_the_exit_code_is_swallowed() -> None:
    swallowed = copy.deepcopy(_workflow())
    for step in swallowed["jobs"][HARD_GATE_JOB]["steps"]:
        if "run" in step:
            step["run"] = step["run"].replace("exit $rc", "exit 0")

    assert "hard-gate job does not exit with the CLI's return code" in _hard_gate_violations(swallowed)


def test_report_only_guard_reds_when_the_report_job_is_switched_to_hard_gate() -> None:
    switched = copy.deepcopy(_workflow())
    for step in switched["jobs"][REPORT_ONLY_JOB]["steps"]:
        if "run" in step:
            step["run"] = step["run"].replace(
                "audit_repo_entropy.py --format json", "audit_repo_entropy.py --mode hard-gate --format json"
            )

    assert "report-only job passes --mode hard-gate" in _report_only_violations(switched)
