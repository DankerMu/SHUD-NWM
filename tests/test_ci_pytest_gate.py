"""The ``changes`` job's ``pytest`` output gates ``Unit Tests (full)`` (#3219).

The output is true iff at least one changed path lies outside ``apps/frontend/``.
The test runs the step's own ``run:`` script from ci.yml with bash, the way the
runner does (``bash -e``, no pipefail), so the rule is proven on the shipped
text, not on a copy. Malformed input must fail the step: a silent ``false``
would skip the whole suite and the watcher would read it as a path-filter skip.
Expected values are the requirement's own cases, written as literals.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CI_WORKFLOW = Path(".github/workflows/ci.yml")
ALL_FILES_EXPRESSION = "${{ steps.filter.outputs.all_files }}"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _gate_step_id() -> str:
    output = _jobs()["changes"]["outputs"].get("pytest")
    assert isinstance(output, str), "changes job has no `pytest` output"
    prefix, suffix = "${{ steps.", ".outputs.pytest }}"
    assert output.startswith(prefix) and output.endswith(suffix), output
    return output[len(prefix) : -len(suffix)]


def _gate_step() -> dict[str, Any]:
    step_id = _gate_step_id()
    matches = [step for step in _jobs()["changes"]["steps"] if step.get("id") == step_id]
    assert len(matches) == 1, f"no single step with id {step_id!r} in the changes job"
    return matches[0]


def _run_gate(all_files: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], str]:
    jq = shutil.which("jq")
    assert jq, "jq is required: the runner image ships it and the gate step calls it"
    script = tmp_path / "gate.sh"
    script.write_text(str(_gate_step()["run"]), encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    bash = shutil.which("bash")
    assert bash
    env = {"PATH": f"{Path(jq).parent}:/usr/bin:/bin", "ALL_FILES_JSON": all_files, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        [bash, "-e", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output.read_text(encoding="utf-8")


def test_job_output_points_at_an_existing_step_fed_all_files_through_env() -> None:
    step = _gate_step()

    assert step["env"] == {"ALL_FILES_JSON": ALL_FILES_EXPRESSION}
    assert "${{" not in str(step["run"])  # never interpolated into the script
    assert "jq -e" not in str(step["run"])  # the result is jq's output, not its exit status


def test_full_suite_job_reads_the_pytest_output_for_every_event_but_dispatch() -> None:
    gate = " ".join(_jobs()["unit-test"]["if"].split())

    assert gate == "github.event_name == 'workflow_dispatch' || needs.changes.outputs.pytest == 'true'"


@pytest.mark.parametrize(
    ("all_files", "expected"),
    [
        ('["apps/frontend/src/App.vue", "apps/frontend/package.json"]', "false"),
        ("[]", "false"),
        ('["docs/runbooks/ci-test-routing.md"]', "true"),
        ('["openspec/changes/x/tasks.md"]', "true"),
        ('["apps/frontend/src/App.vue", "packages/common/db.py"]', "true"),
        ('["tests/apps/frontend/x.py"]', "true"),
        ('["apps/frontend-legacy/x.ts"]', "true"),
        ('["apps/frontend"]', "true"),
    ],
    ids=[
        "frontend-only",
        "empty-list",
        "docs-only",
        "openspec-only",
        "mixed",
        "frontend-segment-not-at-start",
        "sibling-prefix",
        "bare-directory-name",
    ],
)
def test_pytest_output_is_true_iff_a_path_lies_outside_apps_frontend(
    all_files: str, expected: str, tmp_path: Path
) -> None:
    result, output = _run_gate(all_files, tmp_path)

    assert result.returncode == 0, result.stderr
    assert output.splitlines() == [f"pytest={expected}"]


@pytest.mark.parametrize(
    "all_files",
    ["", "not json", '["docs/x.md"', "null", "{}", '"docs/x.md"', "[1]", "[] []"],
    ids=["empty-string", "garbage", "truncated", "null", "object", "string", "non-string-entry", "two-values"],
)
def test_malformed_all_files_fails_the_step_and_writes_no_output(all_files: str, tmp_path: Path) -> None:
    result, output = _run_gate(all_files, tmp_path)

    assert result.returncode != 0
    assert output == ""
