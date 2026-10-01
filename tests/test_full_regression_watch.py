"""The master full-regression watcher (#2044): classifier, margin and wiring.

Fixture JSON only, no network: every GitHub API response is a dict served by a
fake fetch keyed on the request path. The fixture shapes follow what was
measured on 2026-10-01 for master push run 35759146799, whose job 106852569935
(`Unit Tests (full)`) concluded ``cancelled`` with the failure-level annotations
``The job has exceeded the maximum execution time of 1h0m0s`` and
``The operation was canceled.``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci import full_regression_watch as watch

REPO = "DankerMu/SHUD-NWM"
CI_WORKFLOW = Path(".github/workflows/ci.yml")
WATCH_WORKFLOW = Path(".github/workflows/full-regression-watch.yml")
RUN_ID = 35759146799
RUN = {
    "id": RUN_ID,
    "head_sha": "abc123",
    "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}",
    "event": "push",
}
CHECK_RUN_URL = f"https://api.github.com/repos/{REPO}/check-runs/106852569935"
WALL_ANNOTATIONS = [
    {"annotation_level": "failure", "message": "The job has exceeded the maximum execution time of 1h0m0s"},
    {"annotation_level": "failure", "message": "The operation was canceled."},
]


def _job(name: str, conclusion: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/106852569935",
        "check_run_url": CHECK_RUN_URL,
        **extra,
    }


class FakeApi:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.paths: list[str] = []

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response(path) if callable(response) else response
        raise AssertionError(f"unexpected API call {path}")


def _run_api(jobs: list[dict[str, Any]], annotations: list[dict[str, Any]] | None = None) -> FakeApi:
    return FakeApi(
        {
            f"/repos/{REPO}/actions/runs/{RUN_ID}/jobs": {"jobs": jobs},
            f"/repos/{REPO}/actions/runs/{RUN_ID}": RUN,
            f"{CHECK_RUN_URL}/annotations": annotations or [],
        }
    )


# --- check-run classification ------------------------------------------------


def test_wall_killed_run_fails_with_sha_run_job_and_wall_timeout_reason() -> None:
    api = _run_api(
        [_job("Detect changed areas", "success"), _job(watch.JOB_NAME, "cancelled")],
        WALL_ANNOTATIONS,
    )

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    assert line == (
        f"::error title=Unit Tests (full) cancelled::sha=abc123 run={RUN['html_url']} "
        f"job=https://github.com/{REPO}/actions/runs/{RUN_ID}/job/106852569935 reason=wall-timeout"
    )
    # The latest attempt only: a re-run's earlier attempt must not be read.
    assert f"/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100" in api.paths


@pytest.mark.parametrize(
    ("conclusion", "reason"),
    [("cancelled", "cancelled"), ("failure", "failed"), ("timed_out", "failed")],
)
def test_failed_conclusions_without_a_wall_annotation_fail_with_their_reason(conclusion: str, reason: str) -> None:
    api = _run_api(
        [_job("Detect changed areas", "success"), _job(watch.JOB_NAME, conclusion)],
        [{"annotation_level": "failure", "message": "Process completed with exit code 1."}],
    )

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    assert line.startswith(f"::error title=Unit Tests (full) {conclusion}::sha=abc123 ")
    assert line.endswith(f"reason={reason}")


def test_success_passes_with_a_notice() -> None:
    code, line = watch.check_run(_run_api([_job(watch.JOB_NAME, "success")]), REPO, RUN_ID)

    assert code == 0
    assert line.startswith("::notice title=Unit Tests (full) success::sha=abc123")


def test_path_filter_skip_passes() -> None:
    api = _run_api([_job("Detect changed areas", "success"), _job(watch.JOB_NAME, "skipped")])

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 0
    assert "skipped by the path filter" in line


@pytest.mark.parametrize(
    "changes",
    [_job("Detect changed areas", "failure"), _job("Detect changed areas", "cancelled"), None],
)
def test_skip_caused_by_an_upstream_failure_fails(changes: dict[str, Any] | None) -> None:
    jobs = [_job(watch.JOB_NAME, "skipped")] + ([changes] if changes else [])

    code, line = watch.check_run(_run_api(jobs), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) skipped upstream::sha=abc123")


def test_absent_full_job_fails_as_broken_wiring() -> None:
    code, line = watch.check_run(_run_api([_job("Unit Tests", "success")]), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) not found::job not found — renamed? watcher wiring broken")
    assert "sha=abc123" in line


def test_unexpected_conclusion_fails_closed() -> None:
    code, line = watch.check_run(_run_api([_job(watch.JOB_NAME, "neutral")]), REPO, RUN_ID)

    assert code == 1
    assert "reason=unexpected-conclusion" in line


def test_no_run_id_checks_the_newest_completed_master_push_run() -> None:
    api = _run_api([_job(watch.JOB_NAME, "success")])
    api.responses = {
        f"/repos/{REPO}/actions/workflows/ci.yml/runs": {"workflow_runs": [{"id": RUN_ID}]},
        **api.responses,
    }

    code, _line = watch.check_run(api, REPO, None)

    assert code == 0
    assert api.paths[0] == (
        f"/repos/{REPO}/actions/workflows/ci.yml/runs?branch=master&event=push&status=completed&per_page=1"
    )


@pytest.mark.parametrize(
    ("messages", "conclusion", "expected"),
    [
        (["The job has exceeded the maximum execution time of 1h0m0s"], "cancelled", "wall-timeout"),
        (["The job has exceeded the maximum execution time of 35m0s"], "failure", "wall-timeout"),
        (["The operation was canceled."], "cancelled", "cancelled"),
        ([], "failure", "failed"),
    ],
)
def test_annotation_parsing(messages: list[str], conclusion: str, expected: str) -> None:
    assert watch.failure_reason(conclusion, [{"message": message} for message in messages]) == expected


# --- margin --------------------------------------------------------------------


def test_p95_nearest_rank() -> None:
    assert watch.p95_nearest_rank(list(range(1, 21))) == 19  # ceil(0.95*20)=19th value
    assert watch.p95_nearest_rank(list(range(1, 11))) == 10  # ceil(9.5)=10th value
    assert watch.p95_nearest_rank([7.0]) == 7.0
    assert watch.p95_nearest_rank([30.0, 10.0, 20.0]) == 30.0


@pytest.mark.parametrize(
    ("p95", "kind"),
    [(48.0, "notice"), (48.1, "warning"), (47.9, "notice"), (59.0, "warning")],
)
def test_margin_threshold_edges(p95: float, kind: str) -> None:
    # 80% of 60 is exactly 48: strictly greater warns, equal does not.
    line = watch.margin_line([p95] * 20, 60)

    assert line.startswith(f"::{kind} title=Unit Tests (full) margin::P95={p95:.1f}min")
    assert "(n=20)" in line


def test_margin_with_no_successes_is_a_notice() -> None:
    assert watch.margin_line([], 60) == "::notice title=Unit Tests (full) margin::no successful runs found (n=0)"


def test_timeout_is_read_from_the_real_ci_workflow() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))

    assert watch.full_job_timeout_minutes() == workflow["jobs"]["unit-test"]["timeout-minutes"]
    assert watch.CI_WORKFLOW_PATH.resolve() == CI_WORKFLOW.resolve()


def _margin_api(run_count: int, *, success_every: int = 1, minutes: float = 40.0) -> FakeApi:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    runs = [
        {"id": index, "created_at": (now - timedelta(hours=index)).isoformat().replace("+00:00", "Z")}
        for index in range(1, run_count + 1)
    ]

    def runs_page(path: str) -> dict[str, Any]:
        page = int(path.rsplit("page=", 1)[1])
        return {"workflow_runs": runs[(page - 1) * 100 : page * 100]}

    def jobs(path: str) -> dict[str, Any]:
        run_id = int(path.split("/runs/")[1].split("/")[0])
        conclusion = "success" if run_id % success_every == 0 else "failure"
        started = now - timedelta(hours=run_id)
        return {
            "jobs": [
                _job(
                    watch.JOB_NAME,
                    conclusion,
                    started_at=started.isoformat(),
                    completed_at=(started + timedelta(minutes=minutes + run_id / 100)).isoformat(),
                )
            ]
        }

    return FakeApi(
        {
            f"/repos/{REPO}/actions/workflows/ci.yml/runs": runs_page,
            f"/repos/{REPO}/actions/runs/": jobs,
        }
    )


def test_margin_pages_the_window_sorts_newest_first_and_stops_at_twenty_successes() -> None:
    api = _margin_api(150)
    capped = watch.CappedFetch(api)

    durations, note = watch.successful_full_durations(capped, REPO, datetime(2026, 10, 1, tzinfo=UTC))

    assert note == ""
    assert len(durations) == 20
    # Newest first: runs 1..20 (created 1..20 hours ago), 40 + id/100 minutes.
    assert durations == pytest.approx([40 + run_id / 100 for run_id in range(1, 21)])
    listing = [path for path in api.paths if "/workflows/ci.yml/runs" in path]
    assert len(listing) == 2  # 100 + 50: the short second page ends the paging
    assert all("created=%3E%3D2026-09-01" in path for path in listing)
    assert capped.calls == 2 + 20


def test_margin_respects_the_api_call_cap_and_reports_n() -> None:
    # Only every fifth run succeeded: twenty successes would need 100 job fetches.
    api = _margin_api(150, success_every=5, minutes=55.0)
    capped = watch.CappedFetch(api)

    durations, note = watch.successful_full_durations(capped, REPO, datetime(2026, 10, 1, tzinfo=UTC))

    assert capped.calls == watch.API_CALL_CAP == 60
    assert len(durations) == (60 - 2) // 5
    assert "cap of 60" in note
    line = watch.margin_line(durations, 60, note)
    assert line.startswith("::warning title=Unit Tests (full) margin::P95=")
    assert f"(n={len(durations)})" in line


def test_capped_fetch_refuses_call_cap_plus_one() -> None:
    capped = watch.CappedFetch(lambda path: {}, cap=2)
    capped("/a")
    capped("/b")

    with pytest.raises(watch.ApiCallCapReached):
        capped("/c")
    assert capped.calls == 2


def test_margin_command_always_exits_zero_even_when_the_api_fails(capsys: pytest.CaptureFixture[str]) -> None:
    def broken(path: str) -> Any:
        raise OSError("network down")

    assert watch.main(["--repo", REPO, "margin"], fetch=broken) == 0
    assert capsys.readouterr().out.startswith("::warning title=Unit Tests (full) margin::could not compute")


def test_check_run_command_exit_code_is_the_classification(capsys: pytest.CaptureFixture[str]) -> None:
    api = _run_api([_job("Detect changed areas", "success"), _job(watch.JOB_NAME, "cancelled")], WALL_ANNOTATIONS)

    assert watch.main(["--repo", REPO, "check-run", "--run-id", str(RUN_ID)], fetch=api) == 1
    assert "reason=wall-timeout" in capsys.readouterr().out


# --- workflow wiring (the only pre-merge proof) ----------------------------------


def _load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    return workflow.get("on", workflow.get(True))


def test_watcher_job_name_matches_the_ci_full_job_name() -> None:
    assert watch.JOB_NAME == _load(CI_WORKFLOW)["jobs"][watch.FULL_JOB_KEY]["name"]
    assert watch.CHANGES_JOB_NAME == _load(CI_WORKFLOW)["jobs"]["changes"]["name"]


def test_watcher_triggers_on_completed_master_ci_runs_and_dispatch() -> None:
    triggers = _on(_load(WATCH_WORKFLOW))

    assert triggers["workflow_run"]["workflows"] == [_load(CI_WORKFLOW)["name"]]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert triggers["workflow_run"]["branches"] == ["master"]
    assert "run_id" in triggers["workflow_dispatch"]["inputs"]


def test_watcher_permissions_are_exactly_read_only() -> None:
    assert _load(WATCH_WORKFLOW)["permissions"] == {"actions": "read", "checks": "read", "contents": "read"}


def test_watcher_job_gate_and_steps() -> None:
    (job,) = _load(WATCH_WORKFLOW)["jobs"].values()
    gate = " ".join(job["if"].split())
    steps = job["steps"]
    runs = [str(step.get("run", "")) for step in steps]

    assert "github.event.workflow_run.event == 'push'" in gate
    assert "github.event_name == 'workflow_dispatch'" in gate
    assert "uv sync --locked --all-extras --dev" in runs
    assert not any("pip install" in run for run in runs)
    check = next(step for step in steps if "check-run" in str(step.get("run", "")))
    assert "--run-id \"$RUN_ID\"" in check["run"]
    assert check["env"]["RUN_ID"] == "${{ inputs.run_id || github.event.workflow_run.id }}"
    margin_step = next(step for step in steps if str(step.get("run", "")).endswith(" margin"))
    assert margin_step.get("if") == "always()"
    assert steps.index(margin_step) > steps.index(check)
