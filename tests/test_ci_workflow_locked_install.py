"""Every ci.yml job that runs pytest installs the locked dependency set (#2573).

A floating ``pip install -e ".[dev]"`` resolved eccodes 2.49 plus the
``eccodeslib``/``eckitlib`` wheels; eckit's bundled PROJ, loaded RTLD_GLOBAL,
collided with pyproj's own PROJ and killed a pytest job at interpreter exit
after every test had passed. The jobs now run
``uv sync --locked --all-extras --dev`` and put the venv on PATH through
``$GITHUB_PATH`` only; job- and workflow-level ``env`` overrides of PATH are
forbidden.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

CI_WORKFLOW_PATH = Path(".github/workflows/ci.yml")
LOCKED_SYNC = "uv sync --locked --all-extras --dev"
REQUIRED_PYTEST_JOBS = ("real-db-integration", "unit-test")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _run_scripts(job: dict[str, Any]) -> list[str]:
    return [str(step.get("run", "")) for step in job.get("steps", []) if isinstance(step, dict)]


# The word `pytest`, except as the key of a `key=value` pair: the `changes` job
# writes its `pytest=<bool>` gate output (#3219) and installs nothing.
_PYTEST_WORD = re.compile(r"(?<![\w.-])pytest(?![\w=-])")


def _runs_pytest(job: dict[str, Any]) -> bool:
    return any(_PYTEST_WORD.search(run) for run in _run_scripts(job))


def _locked_install_violations(workflow: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    jobs = workflow.get("jobs", {})
    pytest_jobs = {name: job for name, job in jobs.items() if _runs_pytest(job)}
    missing = sorted(set(REQUIRED_PYTEST_JOBS) - set(pytest_jobs))
    if missing:
        violations.append(f"expected pytest jobs missing: {missing}")
    for name, job in sorted(pytest_jobs.items()):
        steps = [step for step in job.get("steps", []) if isinstance(step, dict)]
        runs = _run_scripts(job)
        if not any(str(step.get("uses", "")).startswith("astral-sh/setup-uv@") for step in steps):
            violations.append(f"{name}: no astral-sh/setup-uv step")
        if not any(run.strip() == LOCKED_SYNC for run in runs):
            violations.append(f"{name}: no `{LOCKED_SYNC}` step")
        if not any(".venv/bin" in run and "$GITHUB_PATH" in run for run in runs):
            violations.append(f"{name}: no step appending .venv/bin to $GITHUB_PATH")
        if any("pip install -e" in run for run in runs):
            violations.append(f"{name}: floating `pip install -e` is back")
        job_env = job.get("env") or {}
        for key in ("PATH", "VIRTUAL_ENV"):
            if key in job_env:
                violations.append(f"{name}: job env sets {key}")
    workflow_env = workflow.get("env") or {}
    for key in ("PATH", "VIRTUAL_ENV"):
        if key in workflow_env:
            violations.append(f"workflow env sets {key}")
    return violations


def test_every_pytest_job_installs_from_the_lock_and_sets_path_via_github_path() -> None:
    assert _locked_install_violations(_workflow()) == []


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ("pytest -q", True),
        ('cd tests && pytest "${files[@]}"', True),
        ("python -m pytest tests/", True),
        ('echo "pytest=$result" >> "$GITHUB_OUTPUT"', False),
        ("rm -rf .pytest_cache", False),
    ],
)
def test_a_pytest_job_is_one_whose_steps_name_pytest_as_a_word_not_an_output_key(run: str, expected: bool) -> None:
    assert _runs_pytest({"steps": [{"run": run}]}) is expected


def test_the_changes_job_gate_output_does_not_make_it_a_pytest_job() -> None:
    changes = _workflow()["jobs"]["changes"]

    assert any("pytest=" in run for run in _run_scripts(changes))
    assert not _runs_pytest(changes)


def test_lock_sync_runs_before_the_first_pytest_step() -> None:
    jobs = _workflow()["jobs"]
    for name in REQUIRED_PYTEST_JOBS:
        runs = _run_scripts(jobs[name])
        sync_at = next(index for index, run in enumerate(runs) if run.strip() == LOCKED_SYNC)
        path_at = next(index for index, run in enumerate(runs) if "$GITHUB_PATH" in run)
        pytest_at = next(index for index, run in enumerate(runs) if "pytest" in run)
        assert sync_at < path_at < pytest_at, name


def _mutate(workflow: dict[str, Any], job: str, old: str, new: str) -> dict[str, Any]:
    mutated = copy.deepcopy(workflow)
    for step in mutated["jobs"][job]["steps"]:
        if isinstance(step, dict) and isinstance(step.get("run"), str) and old in step["run"]:
            step["run"] = step["run"].replace(old, new)
            return mutated
    raise AssertionError(f"{job}: no step runs {old!r}")


@pytest.mark.parametrize("job", REQUIRED_PYTEST_JOBS)
def test_guard_reds_on_a_floating_install(job: str) -> None:
    mutated = _mutate(_workflow(), job, LOCKED_SYNC, 'pip install -e ".[dev]"')

    violations = _locked_install_violations(mutated)

    assert f"{job}: no `{LOCKED_SYNC}` step" in violations
    assert f"{job}: floating `pip install -e` is back" in violations


@pytest.mark.parametrize("job", REQUIRED_PYTEST_JOBS)
def test_guard_reds_when_the_venv_is_not_put_on_path(job: str) -> None:
    mutated = _mutate(_workflow(), job, '>> "$GITHUB_PATH"', "")

    assert f"{job}: no step appending .venv/bin to $GITHUB_PATH" in _locked_install_violations(mutated)


@pytest.mark.parametrize("key", ["PATH", "VIRTUAL_ENV"])
def test_guard_reds_on_a_job_or_workflow_env_path_override(key: str) -> None:
    job_level = copy.deepcopy(_workflow())
    job_level["jobs"]["unit-test"]["env"] = {key: "/x"}
    workflow_level = copy.deepcopy(_workflow())
    workflow_level["env"] = {key: "/x"}

    assert f"unit-test: job env sets {key}" in _locked_install_violations(job_level)
    assert f"workflow env sets {key}" in _locked_install_violations(workflow_level)
