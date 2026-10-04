"""The master full-regression watcher (#2044): classifier, margin and wiring.

Fixture JSON only, no network: every GitHub API response is a dict served by a
fake fetch keyed on the request path. The fixture shapes follow what was
measured on 2026-10-01 for master push run 35759146799, whose job 106852569935
(`Unit Tests (full)`) concluded ``cancelled`` with the failure-level annotations
``The job has exceeded the maximum execution time of 1h0m0s`` and
``The operation was canceled.``.

Since #2710 the full regression is a matrix: GitHub names its jobs
``Unit Tests (full) (<shard>)``, and the watcher's notion of "the full job" is
every shard job the ``ci.yml`` matrix declares.
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


def _shard_names() -> tuple[str, ...]:
    """The matrix job names, derived here from ci.yml independently of the watcher."""
    job = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["unit-test"]
    return tuple(f"{job['name']} ({value})" for value in job["strategy"]["matrix"]["shard"])


def _shards(conclusion: str | None = "success", **overrides: str | None) -> list[dict[str, Any]]:
    """One job per declared shard; ``overrides`` maps a shard value ("s2") to its conclusion."""
    jobs = []
    for name in _shard_names():
        value = name.rsplit("(", 1)[1].rstrip(")")
        jobs.append(
            _job(
                name,
                overrides.get(f"s{value}", conclusion),
                html_url=f"https://github.com/{REPO}/actions/runs/{RUN_ID}/job/{value}",
                check_run_url=f"https://api.github.com/repos/{REPO}/check-runs/{value}",
            )
        )
    return jobs


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
            f"https://api.github.com/repos/{REPO}/check-runs/": annotations or [],
        }
    )


# --- check-run classification ------------------------------------------------


def test_wall_killed_shard_fails_with_sha_run_shard_job_and_wall_timeout_reason() -> None:
    api = _run_api([_job("Detect changed areas", "success"), *_shards(s2="cancelled")], WALL_ANNOTATIONS)

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    assert line == (
        f"::error title=Unit Tests (full) (2) cancelled::sha=abc123 run={RUN['html_url']} "
        f"job=https://github.com/{REPO}/actions/runs/{RUN_ID}/job/2 reason=wall-timeout"
    )
    # The latest attempt only: a re-run's earlier attempt must not be read.
    assert f"/repos/{REPO}/actions/runs/{RUN_ID}/jobs?filter=latest&per_page=100" in api.paths
    # Annotations are read for the failed shard only.
    assert [path for path in api.paths if path.endswith("/annotations")] == [
        f"https://api.github.com/repos/{REPO}/check-runs/2/annotations"
    ]


def test_unexpanded_wall_killed_job_still_fails_with_its_reason() -> None:
    # The pre-matrix job shape (and a matrix that failed to expand) stays red.
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


@pytest.mark.parametrize(
    ("conclusion", "reason"),
    [("cancelled", "cancelled"), ("failure", "failed"), ("timed_out", "failed")],
)
def test_failed_conclusions_without_a_wall_annotation_fail_with_their_reason(conclusion: str, reason: str) -> None:
    api = _run_api(
        [_job("Detect changed areas", "success"), *_shards(s3=conclusion)],
        [{"annotation_level": "failure", "message": "Process completed with exit code 1."}],
    )

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    assert line.startswith(f"::error title=Unit Tests (full) (3) {conclusion}::sha=abc123 ")
    assert line.endswith(f"reason={reason}")


def test_every_failed_shard_is_named_on_its_own_line() -> None:
    api = _run_api([*_shards(s1="failure", s4="cancelled")], WALL_ANNOTATIONS)

    code, text = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    first, second = text.splitlines()
    assert first.startswith("::error title=Unit Tests (full) (1) failure::sha=abc123 ")
    assert second.startswith("::error title=Unit Tests (full) (4) cancelled::sha=abc123 ")


def test_all_shards_success_passes_with_a_notice() -> None:
    assert len(_shard_names()) == 4

    code, line = watch.check_run(_run_api(_shards()), REPO, RUN_ID)

    assert code == 0
    assert line.startswith("::notice title=Unit Tests (full) success::sha=abc123")
    assert line.endswith("shards=4")


def test_a_single_success_job_under_the_old_name_is_not_the_full_regression() -> None:
    # One green job named `Unit Tests (full)` proves none of the declared shards ran.
    code, line = watch.check_run(_run_api([_job(watch.JOB_NAME, "success")]), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) not found::")
    assert "missing=" + ", ".join(_shard_names()) in line


def test_missing_shard_fails_naming_it() -> None:
    jobs = [_job("Detect changed areas", "success"), *_shards()]
    del jobs[3]  # shard 3

    code, line = watch.check_run(_run_api(jobs), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) not found::job not found — renamed? watcher wiring broken")
    assert "sha=abc123" in line
    assert line.endswith("missing=Unit Tests (full) (3)")


@pytest.mark.parametrize(
    "renamed", ["Unit Tests (full) (shard 2)", "Unit Tests (full) (5)", "Unit Tests (full) (2, 3.11)"]
)
def test_renamed_or_undeclared_shard_job_fails(renamed: str) -> None:
    jobs = _shards()
    jobs[1] = _job(renamed, "success")

    code, text = watch.check_run(_run_api(jobs), REPO, RUN_ID)

    assert code == 1
    assert "missing=Unit Tests (full) (2)" in text
    assert f"unexpected={renamed}" in text


def test_expected_shard_names_are_exact_not_a_prefix_match() -> None:
    # Injected names: the classifier compares whole names, whatever the matrix values are.
    names = ("Unit Tests (full) (a)", "Unit Tests (full) (b)")
    jobs = [_job("Unit Tests (full) (a)", "success"), _job("Unit Tests (full) (b) retry", "success")]

    code, line = watch.classify_run(RUN, jobs, lambda job: [], names)

    assert code == 1
    assert "missing=Unit Tests (full) (b)" in line


def test_path_filter_skip_passes_as_one_unexpanded_skipped_job() -> None:
    # The job-level `if` is evaluated before the matrix expands.
    api = _run_api([_job("Detect changed areas", "success"), _job(watch.JOB_NAME, "skipped")])

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 0
    assert "skipped by the path filter" in line


def test_path_filter_skip_passes_as_skipped_shard_jobs() -> None:
    api = _run_api([_job("Detect changed areas", "success"), *_shards("skipped")])

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 0
    assert "skipped by the path filter" in line


@pytest.mark.parametrize("skipped_shape", ["unexpanded", "shards"])
@pytest.mark.parametrize(
    "changes",
    [_job("Detect changed areas", "failure"), _job("Detect changed areas", "cancelled"), None],
)
def test_skip_caused_by_an_upstream_failure_fails(changes: dict[str, Any] | None, skipped_shape: str) -> None:
    skipped = [_job(watch.JOB_NAME, "skipped")] if skipped_shape == "unexpanded" else _shards("skipped")
    jobs = skipped + ([changes] if changes else [])

    code, line = watch.check_run(_run_api(jobs), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) skipped upstream::sha=abc123")


def test_one_skipped_shard_beside_running_shards_fails_naming_it() -> None:
    api = _run_api([_job("Detect changed areas", "success"), *_shards(s2="skipped")])

    code, line = watch.check_run(api, REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) (2) skipped::sha=abc123")
    assert "reason=unexpected-conclusion" in line


def test_skipped_shards_with_one_missing_fail_naming_the_missing_shard() -> None:
    jobs = [_job("Detect changed areas", "success"), *_shards("skipped")[:-1]]

    code, line = watch.check_run(_run_api(jobs), REPO, RUN_ID)

    assert code == 1
    assert "missing=Unit Tests (full) (4)" in line


def test_absent_full_job_fails_as_broken_wiring() -> None:
    code, line = watch.check_run(_run_api([_job("Unit Tests", "success")]), REPO, RUN_ID)

    assert code == 1
    assert line.startswith("::error title=Unit Tests (full) not found::job not found — renamed? watcher wiring broken")
    assert "sha=abc123" in line
    assert "missing=" + ", ".join(_shard_names()) in line


@pytest.mark.parametrize("jobs", [[_job("Unit Tests (full)", "neutral")], None])
def test_unexpected_conclusion_fails_closed(jobs: list[dict[str, Any]] | None) -> None:
    code, line = watch.check_run(_run_api(jobs or _shards(s1="neutral")), REPO, RUN_ID)

    assert code == 1
    assert "reason=unexpected-conclusion" in line


def test_no_run_id_checks_the_newest_completed_master_push_run() -> None:
    api = _run_api(_shards())
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


def test_margin_line_names_the_shard_it_is_about() -> None:
    line = watch.margin_line([25.0] * 20, 30, name="Unit Tests (full) (2)")

    assert line == "::warning title=Unit Tests (full) (2) margin::P95=25.0min > 80% of 30min (n=20)"


def test_margin_threshold_and_sample_constants_are_unchanged() -> None:
    assert (watch.MARGIN_THRESHOLD, watch.MARGIN_SAMPLE, watch.MARGIN_WINDOW_DAYS) == (0.8, 20, 30)
    assert watch.API_CALL_CAP == 60


def _margin_api(
    run_count: int,
    *,
    success_every: int = 1,
    minutes: float = 40.0,
    unsharded_every: int = 0,
    slow_shard: int = 0,
) -> FakeApi:
    """Runs 1..N, newest first; every shard of run ``id`` took ``minutes + id/100``.

    ``unsharded_every``: those runs carry the old single job (58 min) instead of
    shards. ``slow_shard``: that shard value takes twice as long.
    """
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
        if unsharded_every and run_id % unsharded_every == 0:
            single = _job(
                watch.JOB_NAME,
                "success",
                started_at=started.isoformat(),
                completed_at=(started + timedelta(minutes=58)).isoformat(),
            )
            return {"jobs": [single]}
        shard_jobs = []
        for index, name in enumerate(_shard_names(), start=1):
            took = (minutes + run_id / 100) * (2 if index == slow_shard else 1)
            shard_jobs.append(
                _job(
                    name,
                    # A failed run: its LAST shard failed, the others were green.
                    conclusion if index == len(_shard_names()) else "success",
                    started_at=started.isoformat(),
                    completed_at=(started + timedelta(minutes=took)).isoformat(),
                )
            )
        return {"jobs": shard_jobs}

    return FakeApi(
        {
            f"/repos/{REPO}/actions/workflows/ci.yml/runs": runs_page,
            f"/repos/{REPO}/actions/runs/": jobs,
        }
    )


def test_margin_pages_the_window_sorts_newest_first_and_stops_at_twenty_successes() -> None:
    api = _margin_api(150)
    capped = watch.CappedFetch(api)

    per_shard, note = watch.successful_shard_durations(capped, REPO, datetime(2026, 10, 1, tzinfo=UTC))

    assert note == ""
    assert list(per_shard) == list(_shard_names())
    for durations in per_shard.values():
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

    per_shard, note = watch.successful_shard_durations(capped, REPO, datetime(2026, 10, 1, tzinfo=UTC))

    assert capped.calls == watch.API_CALL_CAP == 60
    assert "cap of 60" in note
    # A run whose last shard failed is not a successful run: no shard of it is sampled.
    assert [len(durations) for durations in per_shard.values()] == [(60 - 2) // 5] * 4
    lines = watch.margin_lines(per_shard, 60, note)
    assert len(lines) == 4
    for name, line in zip(_shard_names(), lines, strict=True):
        assert line.startswith(f"::warning title={name} margin::P95=")
        assert f"(n={(60 - 2) // 5})" in line and "cap of 60" in line


def test_margin_ignores_runs_that_carry_the_old_single_job() -> None:
    # Every second run is a pre-matrix run (one 58-min job): it is not a sample,
    # and it must not push any shard's P95 towards the wall.
    api = _margin_api(60, unsharded_every=2, minutes=10.0)
    capped = watch.CappedFetch(api)

    per_shard, note = watch.successful_shard_durations(capped, REPO, datetime(2026, 10, 1, tzinfo=UTC))

    assert note == ""
    odd_runs = [run_id for run_id in range(1, 61) if run_id % 2][:20]
    for durations in per_shard.values():
        assert durations == pytest.approx([10 + run_id / 100 for run_id in odd_runs])
    text = watch.margin(watch.CappedFetch(api), REPO, 30, datetime(2026, 10, 1, tzinfo=UTC))
    assert all(line.startswith("::notice ") and "(n=20)" in line for line in text.splitlines())
    assert len(text.splitlines()) == 4


def test_margin_with_only_unsharded_runs_reports_no_successful_runs() -> None:
    api = _margin_api(5, unsharded_every=1)

    text = watch.margin(watch.CappedFetch(api), REPO, 30, datetime(2026, 10, 1, tzinfo=UTC))

    assert text == "::notice title=Unit Tests (full) margin::no successful runs found (n=0)"


def test_margin_warns_for_the_one_shard_whose_p95_exceeds_80_percent_of_the_shard_timeout() -> None:
    # 13 min per shard against a 30-min timeout, but shard 3 takes twice that: 26 > 24.
    api = _margin_api(20, minutes=13.0, slow_shard=3)

    lines = watch.margin(watch.CappedFetch(api), REPO, 30, datetime(2026, 10, 1, tzinfo=UTC)).splitlines()

    kinds = {line.split(" margin::")[0]: line for line in lines}
    assert set(kinds) == {
        "::notice title=Unit Tests (full) (1)",
        "::notice title=Unit Tests (full) (2)",
        "::warning title=Unit Tests (full) (3)",
        "::notice title=Unit Tests (full) (4)",
    }
    assert "> 80% of 30min (n=20)" in kinds["::warning title=Unit Tests (full) (3)"]


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
    api = _run_api([_job("Detect changed areas", "success"), *_shards(s1="cancelled")], WALL_ANNOTATIONS)

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


def test_shard_names_are_read_from_the_ci_matrix() -> None:
    job = _load(CI_WORKFLOW)["jobs"][watch.FULL_JOB_KEY]

    assert "${{" not in job["name"]
    assert list(job["strategy"]["matrix"]) == ["shard"]
    assert type(job["timeout-minutes"]) is int
    assert watch.full_job_shard_names() == _shard_names()
    assert watch.full_job_shard_names() == tuple(f"Unit Tests (full) ({value})" for value in (1, 2, 3, 4))


def _ci_copy(tmp_path: Path, mutate: Any) -> Path:
    workflow = _load(CI_WORKFLOW)
    mutate(workflow["jobs"][watch.FULL_JOB_KEY])
    path = tmp_path / "ci.yml"
    path.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    return path


def test_shard_names_follow_the_matrix_list(tmp_path: Path) -> None:
    path = _ci_copy(tmp_path, lambda job: job["strategy"]["matrix"].update(shard=[1, 2]))

    assert watch.full_job_shard_names(path) == ("Unit Tests (full) (1)", "Unit Tests (full) (2)")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job: job["strategy"]["matrix"].update(python=["3.11", "3.12"]),
        lambda job: job["strategy"]["matrix"].update(include=[{"shard": 9}]),
        lambda job: job.pop("strategy"),
        lambda job: job["strategy"]["matrix"].update(shard=[]),
        lambda job: job["strategy"]["matrix"].update(shard="${{ fromJSON(needs.changes.outputs.shards) }}"),
        lambda job: job.update(name="Unit Tests (full) ${{ matrix.shard }}"),
    ],
)
def test_a_ci_matrix_the_watcher_cannot_name_is_refused(tmp_path: Path, mutate: Any) -> None:
    path = _ci_copy(tmp_path, mutate)

    with pytest.raises(ValueError):
        watch.full_job_shard_names(path)


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
