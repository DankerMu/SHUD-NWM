"""The weekly floating-resolve report lane (#2709): script logic and wiring.

Since #2573 every CI job installs ``uv.lock``, so nothing surfaces upstream
breakage until someone upgrades the lock. This lane resolves without the lock
once a week, publishes the version differences, runs the tests and keeps ONE
tracking issue. It must never gate a merge. No network here: the GitHub API is
a fake that records every request.
"""

from __future__ import annotations

import re
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from scripts.ci import floating_resolve_report as report

REPO = "DankerMu/SHUD-NWM"
RUN_URL = f"https://github.com/{REPO}/actions/runs/1"
TITLE = "ci(deps): floating dependency resolve is failing"
FLOATING_WORKFLOW = Path(".github/workflows/floating-resolve-report.yml")
CI_WORKFLOW = Path(".github/workflows/ci.yml")
LOCKED_SYNC = "uv sync --locked --all-extras --dev"
MARKER_EXPRESSION = '-m "not e2e and not grib and not integration"'
STATUS_FUNCTION_CALL = re.compile(r"\b(?:cancelled|always|failure|success)\s*\(")


class FakeGitHub:
    """An in-memory issue tracker behind the script's ``request(method, path, body)``."""

    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = issues or []
        self.calls: list[tuple[str, str, Any]] = []
        self.comments: dict[int, list[str]] = {}

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith(f"/repos/{REPO}/issues?"):
            query = parse_qs(urlsplit(path).query)
            assert query == {"state": ["open"], "labels": ["tech-debt,needs-followup"], "per_page": ["100"]}
            # The list endpoint returns a bare array of open issues AND pull requests,
            # whatever their title. (The fake does not filter by label.)
            return [issue for issue in self.issues if issue["state"] == "open"]
        if method == "POST" and path == f"/repos/{REPO}/issues":
            issue = {"number": 9000 + len(self.issues), "state": "open", **body}
            self.issues.append(issue)
            return issue
        if method == "POST" and path.startswith(f"/repos/{REPO}/issues/") and path.endswith("/comments"):
            number = int(path.split("/")[-2])
            self.comments.setdefault(number, []).append(body["body"])
            return {"id": 1}
        raise AssertionError(f"unexpected request {method} {path}")

    def created(self) -> list[Any]:
        return [body for method, path, body in self.calls if method == "POST" and path.endswith("/issues")]


def _issue(number: int, title: str, state: str = "open", **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": title, "state": state, **extra}


# --- report: one tracking issue ------------------------------------------------------


def test_failure_without_an_open_tracking_issue_creates_exactly_one() -> None:
    api = FakeGitHub([_issue(7, "ci(deps): something else"), _issue(8, TITLE + " again")])

    report.report(api, REPO, "tests", RUN_URL, "fastapi: 0.136.0 -> 0.137.0")

    (created,) = api.created()
    assert created["title"] == TITLE == report.TRACKING_TITLE
    assert created["labels"] == list(report.TRACKING_LABELS)
    assert RUN_URL in created["body"]
    assert "kind: `tests`" in created["body"]
    assert "fastapi: 0.136.0 -> 0.137.0" in created["body"]
    assert api.comments == {}


def test_failure_with_an_open_tracking_issue_comments_and_creates_nothing() -> None:
    api = FakeGitHub([_issue(41, TITLE)])

    report.report(api, REPO, "resolve", RUN_URL, "")

    assert api.created() == []
    assert list(api.comments) == [41]
    (comment,) = api.comments[41]
    assert RUN_URL in comment and "kind: `resolve`" in comment


def test_two_consecutive_failures_leave_one_issue_with_one_comment_for_the_later_failure() -> None:
    api = FakeGitHub()

    report.report(api, REPO, "tests", RUN_URL, "")
    report.report(api, REPO, "timeout", RUN_URL + "2", "")

    assert len(api.created()) == 1
    assert [issue["title"] for issue in api.issues] == [TITLE]
    (comments,) = api.comments.values()
    assert len(comments) == 1 and "kind: `timeout`" in comments[0]


def test_success_without_a_tracking_issue_makes_no_write() -> None:
    api = FakeGitHub([_issue(7, "unrelated")])

    report.report(api, REPO, None, RUN_URL, "")

    assert [method for method, _path, _body in api.calls] == ["GET"]
    assert api.created() == [] and api.comments == {}


def test_success_with_an_open_tracking_issue_comments_passing_again_and_does_not_close_it() -> None:
    api = FakeGitHub([_issue(41, TITLE)])

    report.report(api, REPO, None, RUN_URL, "")

    assert api.created() == []
    (comment,) = api.comments[41]
    assert "passing again" in comment and RUN_URL in comment
    assert not any(method in ("PATCH", "PUT", "DELETE") for method, _path, _body in api.calls)
    assert api.issues[0]["state"] == "open"


def test_tracking_issue_match_is_the_exact_title_of_an_open_issue_not_a_pull_request() -> None:
    api = FakeGitHub(
        [
            _issue(3, TITLE, state="closed"),
            _issue(4, TITLE.upper()),
            _issue(5, TITLE, pull_request={"url": "x"}),
            _issue(6, "re: " + TITLE),
        ]
    )

    assert report.find_tracking_issue(api, REPO) is None
    api.issues.extend([_issue(12, TITLE), _issue(11, TITLE)])
    assert report.find_tracking_issue(api, REPO) == 11  # the oldest one, deterministically


def test_tracking_issue_lookup_uses_the_consistent_list_endpoint_never_the_search_index() -> None:
    api = FakeGitHub([_issue(41, TITLE)])

    assert report.find_tracking_issue(api, REPO) == 41

    ((method, path, body),) = api.calls
    assert method == "GET" and body is None
    assert path.startswith(f"/repos/{REPO}/issues?")
    assert "/search/issues" not in path


def test_injected_failure_says_so() -> None:
    api = FakeGitHub()

    report.report(api, REPO, "injected", RUN_URL, "")

    assert "injected on purpose" in api.created()[0]["body"]


def test_long_lock_diff_is_truncated_in_the_issue_text() -> None:
    api = FakeGitHub()
    diff = "\n".join(f"pkg{index}: 1 -> 2" for index in range(5000))

    report.report(api, REPO, "tests", RUN_URL, diff)

    body = api.created()[0]["body"]
    assert len(body) < 60000  # the API rejects bodies above 65536 characters
    assert "pkg0: 1 -> 2" in body and "truncated" in body


# --- report: failure kinds -----------------------------------------------------------

NOT_RUN = {"lock": "skipped", "sync": "skipped", "proj": "skipped", "tests": "skipped"}
ALL_GOOD = {"inject": "skipped", "lock": "success", "sync": "success", "proj": "success", "tests": "success"}


@pytest.mark.parametrize(
    ("changed", "exit_codes", "expected"),
    [
        ({}, {"proj": "0", "tests": "0"}, None),
        ({"inject": "failure", **NOT_RUN}, {}, "injected"),
        ({**NOT_RUN, "lock": "failure"}, {}, "resolve"),
        ({"sync": "failure", "proj": "skipped", "tests": "skipped"}, {}, "resolve"),
        ({"proj": "failure", "tests": "skipped"}, {"proj": "139"}, "tests"),
        ({"tests": "failure"}, {"proj": "0", "tests": "1"}, "tests"),
        # The step was killed at its own timeout: pytest never returned an exit code.
        ({"tests": "failure"}, {"proj": "0", "tests": ""}, "timeout"),
        ({"tests": "cancelled"}, {"proj": "0"}, "timeout"),
        ({"proj": "failure", "tests": "skipped"}, {}, "timeout"),
        # A step outside the lane's own subject failed (checkout, setup, the diff):
        # nothing ran to a verdict, so this is neither a pass nor an upstream break.
        (NOT_RUN, {}, "unclassified"),
        ({"lock": "", "sync": "", "proj": "", "tests": ""}, {}, "unclassified"),
    ],
)
def test_failure_kind(changed: dict[str, str], exit_codes: dict[str, str], expected: str | None) -> None:
    assert report.failure_kind({**ALL_GOOD, **changed}, exit_codes) == expected


def test_a_cancelled_job_is_unclassified_not_a_timeout() -> None:
    outcomes = {**ALL_GOOD, "tests": "cancelled"}

    assert report.failure_kind(outcomes, {"proj": "0"}, job_cancelled=True) == "unclassified"


def _report_argv(**outcomes: str) -> list[str]:
    merged = {**ALL_GOOD, **outcomes}
    argv = ["--repo", REPO, "report", "--run-url", RUN_URL, "--job-cancelled", "false"]
    for step, outcome in merged.items():
        argv += [f"--{step}", outcome]
    return argv


def test_report_command_files_the_issue_with_the_diff_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeGitHub()
    diff = tmp_path / "lock-diff.txt"
    diff.write_text("eccodes: 2.48.0 -> 2.49.0\n", encoding="utf-8")
    argv = [*_report_argv(tests="failure"), "--proj-rc", "0", "--tests-rc", "1", "--diff-file", str(diff)]

    assert report.main(argv, request=api) == 0

    assert "eccodes: 2.48.0 -> 2.49.0" in api.created()[0]["body"]
    assert "created" in capsys.readouterr().out


def test_report_command_writes_nothing_for_an_unclassified_failure(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeGitHub([_issue(41, TITLE)])
    argv = _report_argv(lock="skipped", sync="skipped", proj="skipped", tests="skipped")

    assert report.main(argv, request=api) == 0

    assert api.calls == []
    assert capsys.readouterr().out.startswith("::warning ")


def _api_down(method: str, path: str, body: Any = None) -> Any:
    raise urllib.error.HTTPError(path, 502, "Bad Gateway", None, None)  # type: ignore[arg-type]


def test_report_command_on_a_passing_run_only_warns_when_the_api_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert report.main(_report_argv(), request=_api_down) == 0

    out = capsys.readouterr().out
    assert out.startswith("::warning ") and "502" in out


def test_report_command_on_a_failing_run_exits_non_zero_when_the_api_fails(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*_report_argv(tests="failure"), "--proj-rc", "0", "--tests-rc", "1"]

    assert report.main(argv, request=_api_down) == 1

    out = capsys.readouterr().out
    assert out.startswith("::error ") and "kind=tests" in out


def test_report_command_tolerates_a_missing_diff_file(tmp_path: Path) -> None:
    api = FakeGitHub()
    argv = [*_report_argv(inject="failure"), "--diff-file", str(tmp_path / "absent.txt")]

    assert report.main(argv, request=api) == 0
    assert len(api.created()) == 1


# --- diff ---------------------------------------------------------------------------------

OLD_LOCK = """version = 1

[[package]]
name = "fastapi"
version = "0.136.0"

[[package]]
name = "numpy"
version = "1.26.4"

[[package]]
name = "numpy"
version = "2.1.0"

[[package]]
name = "gone"
version = "1.0"

[[package]]
name = "nhms"
source = { editable = "." }
"""
NEW_LOCK = """version = 1

[[package]]
name = "fastapi"
version = "0.137.0"

[[package]]
name = "numpy"
version = "1.26.4"

[[package]]
name = "numpy"
version = "2.2.0"

[[package]]
name = "eckitlib"
version = "2.3.0.30"

[[package]]
name = "nhms"
source = { editable = "." }
"""


def test_lock_diff_lists_changed_added_and_removed_packages_by_name() -> None:
    assert report.lock_diff_lines(OLD_LOCK, NEW_LOCK) == [
        "eckitlib: (absent) -> 2.3.0.30",
        "fastapi: 0.136.0 -> 0.137.0",
        "gone: 1.0 -> (absent)",
        "numpy: 1.26.4, 2.1.0 -> 1.26.4, 2.2.0",
    ]


def test_lock_diff_of_identical_locks_is_empty_and_the_command_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old, new = tmp_path / "old.lock", tmp_path / "new.lock"
    old.write_text(OLD_LOCK, encoding="utf-8")
    new.write_text(OLD_LOCK, encoding="utf-8")

    assert report.lock_diff_lines(OLD_LOCK, OLD_LOCK) == []
    assert report.main(["diff", str(old), str(new)]) == 0
    assert capsys.readouterr().out == "no package version differences\n"

    new.write_text(NEW_LOCK, encoding="utf-8")
    assert report.main(["diff", str(old), str(new)]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "fastapi: 0.136.0 -> 0.137.0"


# --- workflow wiring ------------------------------------------------------------------------


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(FLOATING_WORKFLOW.read_text(encoding="utf-8"))


def _job() -> dict[str, Any]:
    (job,) = _workflow()["jobs"].values()
    return job


def _step(step_id: str) -> dict[str, Any]:
    return next(step for step in _job()["steps"] if step.get("id") == step_id)


def _index(step_id: str) -> int:
    return _job()["steps"].index(_step(step_id))


def test_triggers_are_exactly_a_weekly_schedule_and_manual_dispatch() -> None:
    workflow = _workflow()
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    (schedule,) = triggers["schedule"]
    minute, hour, day_of_month, month, day_of_week = schedule["cron"].split()
    assert minute.isdigit() and hour.isdigit()
    assert (day_of_month, month) == ("*", "*")
    assert day_of_week in {"0", "1", "2", "3", "4", "5", "6"}  # one fixed weekday: weekly
    inject = triggers["workflow_dispatch"]["inputs"]["inject_failure"]
    assert inject["type"] == "boolean" and inject["default"] is False


def test_permissions_are_minimal_and_the_lane_has_its_own_concurrency_group() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    assert "permissions" not in _job()
    assert "${{" not in workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_job_repeats_the_full_job_prerequisites() -> None:
    steps = _job()["steps"]
    runs = [str(step.get("run", "")) for step in steps]

    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"]["fetch-depth"] == 0
    assert any(str(step.get("uses", "")).startswith("astral-sh/setup-uv@") for step in steps)
    assert 'echo "$PWD/.venv/bin" >> "$GITHUB_PATH"' in runs
    assert any("mkdir -p /scratch/frd_muziyao" in run for run in runs)


def test_resolve_is_unlocked_and_the_locked_copy_and_diff_are_published() -> None:
    steps = _job()["steps"]
    runs = [str(step.get("run", "")) for step in steps]

    assert not any("--locked" in run or "--frozen" in run for run in runs)
    assert _step("lock")["run"].strip().splitlines() == ["cp uv.lock uv.lock.locked", "uv lock --upgrade"]
    assert _step("sync")["run"].strip() == "uv sync --all-extras --dev"
    diff_run = next(run for run in runs if "floating_resolve_report.py diff" in run)
    assert "uv.lock.locked uv.lock > lock-diff.txt" in diff_run and "$GITHUB_STEP_SUMMARY" in diff_run
    upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
    assert upload["with"]["path"].split() == ["uv.lock.locked", "uv.lock", "lock-diff.txt"]
    assert _index("lock") < runs.index(diff_run) < steps.index(upload) < _index("sync")


def test_native_proj_isolation_runs_first_then_the_marker_filtered_suite() -> None:
    proj, tests = _step("proj")["run"], _step("tests")["run"]

    assert "pytest -q tests/test_native_proj_isolation.py" in proj
    assert "pytest tests/ -q" in tests and MARKER_EXPRESSION in tests
    assert _index("sync") < _index("proj") < _index("tests")
    # Each records pytest's exit code, so a step killed at its timeout (no code)
    # is told apart from a test failure.
    for run in (proj, tests):
        assert 'echo "rc=$rc" >> "$GITHUB_OUTPUT"' in run


def test_pytest_step_timeouts_stay_below_the_job_timeout_so_the_report_still_runs() -> None:
    job = _job()
    step_minutes = [_step(step_id)["timeout-minutes"] for step_id in ("proj", "tests")]

    assert all(type(minutes) is int for minutes in [*step_minutes, job["timeout-minutes"]])
    assert sum(step_minutes) < job["timeout-minutes"]


def test_injected_failure_runs_before_the_resolve_and_only_on_request() -> None:
    inject = _step("inject")

    assert inject["if"] == "${{ inputs.inject_failure }}"
    assert inject["run"].strip().endswith("exit 1")
    assert _index("inject") < _index("lock")


def test_report_step_always_runs_last_with_every_outcome_and_the_stock_interpreter() -> None:
    steps = _job()["steps"]
    last = steps[-1]
    run = " ".join(last["run"].split())

    assert last["if"] == "always()"
    assert last["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert run.startswith("python scripts/ci/floating_resolve_report.py report ")
    assert "uv run" not in run  # the venv may not exist when the resolve failed
    for step_id in ("inject", "lock", "sync", "proj", "tests"):
        # Quoted: an empty outcome must stay an argument, not swallow the next flag.
        assert f'--{step_id} "${{{{ steps.{step_id}.outcome }}}}"' in run
    for step_id in ("proj", "tests"):
        assert f'--{step_id}-rc "${{{{ steps.{step_id}.outputs.rc }}}}"' in run
    # `job.status`, not `cancelled()`: status functions are only valid in `if:`.
    assert "--job-cancelled ${{ job.status == 'cancelled' }}" in run
    assert "--diff-file lock-diff.txt" in run
    assert "--run-url" in run and "${{ github.run_id }}" in run


def _status_function_misuses(node: Any, trail: str = "") -> list[str]:
    """Strings calling a status function anywhere but under an `if` key."""
    if isinstance(node, dict):
        return [
            misuse
            for key, value in node.items()
            if key != "if"
            for misuse in _status_function_misuses(value, f"{trail}/{key}")
        ]
    if isinstance(node, list):
        return [
            misuse
            for position, value in enumerate(node)
            for misuse in _status_function_misuses(value, f"{trail}[{position}]")
        ]
    if isinstance(node, str):
        return [f"{trail}: {call}" for call in STATUS_FUNCTION_CALL.findall(node)]
    return []


def test_status_functions_appear_only_in_if_conditions() -> None:
    # GitHub rejects the WHOLE workflow file ("Unrecognized function: 'cancelled'")
    # when a status function is used outside an `if:`; yaml.safe_load does not.
    workflow = _workflow()

    assert _status_function_misuses(workflow) == []
    # Anti-vacuity: the walk reaches a step's `run` text and skips only `if`.
    assert _status_function_misuses({"steps": [{"if": "always()", "run": "x ${{ cancelled() }}"}]}) == [
        "/steps[0]/run: cancelled("
    ]


def test_workflow_never_writes_to_the_repository() -> None:
    text = FLOATING_WORKFLOW.read_text(encoding="utf-8")

    for forbidden in ("git push", "git commit", "gh pr", "pull-requests:", "contents: write"):
        assert forbidden not in text


def test_ci_yml_keeps_its_three_locked_installs_and_does_not_call_the_floating_lane() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert text.count(f"run: {LOCKED_SYNC}\n") == 3
    assert "uv lock --upgrade" not in text
    assert "floating_resolve_report" not in text
