#!/usr/bin/env python3
"""Watch the master ``Unit Tests (full)`` shards of the CI workflow (#2044, #2710).

The full suite runs only after merge, on master pushes. When it is cancelled at
the wall, or fails, nothing else turns red: PR checks were already green. This
watcher is run by ``.github/workflows/full-regression-watch.yml`` whenever CI
completes on master and has two subcommands.

The full regression is a matrix. "The full job" here means every shard job the
``unit-test`` matrix of ci.yml declares: the names ``Unit Tests (full) (<shard>)``
are derived from that matrix and compared exactly, never by prefix.

``check-run`` classifies one CI run's shard jobs (latest attempt):

* every shard success -> exit 0;
* skipped -> exit 0 only when the same run's ``Detect changed areas`` job
  succeeded (the path filter skipped it); any other skip comes from an upstream
  failure -> exit 1. The job-level ``if`` is evaluated before the matrix
  expands, so a skip shows up either as ONE job named ``Unit Tests (full)`` or
  as one skipped job per shard; both shapes are accepted;
* a shard cancelled / failure / timed_out -> exit 1 with one ``::error::``
  annotation per such shard carrying the SHA, the run, the shard job and a
  reason (``wall-timeout`` when the job's check-run annotations say the maximum
  execution time was exceeded);
* a declared shard absent, or a shard job the matrix does not declare -> exit 1
  naming it: a shard did not run, or the job was renamed and the watcher wiring
  is broken.

``margin`` reads the last successful full runs on master that carry the shard
jobs (at most ``MARGIN_SAMPLE`` within ``MARGIN_WINDOW_DAYS``; runs of the old
single job are not samples), takes the nearest-rank P95 of each shard's
durations and warns, naming the shard, when it exceeds ``MARGIN_THRESHOLD`` of
the job's ``timeout-minutes`` in ci.yml. It always exits 0.

Every invocation makes at most ``API_CALL_CAP`` GitHub API calls. Standard
library plus PyYAML only; the token comes from ``GITHUB_TOKEN`` and the
repository from ``--repo`` or ``GITHUB_REPOSITORY``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

JOB_NAME = "Unit Tests (full)"
CHANGES_JOB_NAME = "Detect changed areas"
CI_WORKFLOW_FILE = "ci.yml"
CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / CI_WORKFLOW_FILE
FULL_JOB_KEY = "unit-test"
API_CALL_CAP = 60
MARGIN_SAMPLE = 20
MARGIN_WINDOW_DAYS = 30
MARGIN_THRESHOLD = 0.8
RUNS_PER_PAGE = 100
WALL_TIMEOUT_MARKER = "exceeded the maximum execution time"
FAILED_CONCLUSIONS = ("cancelled", "failure", "timed_out")

Fetch = Callable[[str], Any]


class ApiCallCapReached(RuntimeError):
    """Raised by ``CappedFetch`` instead of making call number ``cap + 1``."""


class CappedFetch:
    """Count GitHub API calls and refuse to exceed the per-invocation cap."""

    def __init__(self, fetch: Fetch, cap: int = API_CALL_CAP) -> None:
        self._fetch = fetch
        self.cap = cap
        self.calls = 0

    def __call__(self, path: str) -> Any:
        if self.calls >= self.cap:
            raise ApiCallCapReached(f"API call cap of {self.cap} reached")
        self.calls += 1
        return self._fetch(path)


def github_fetch(token: str, api_url: str = "https://api.github.com") -> Fetch:
    """A fetch over the REST API; ``path`` is relative to ``api_url`` or absolute."""

    def fetch(path: str) -> Any:
        url = path if path.startswith("https://") else api_url + path
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    return fetch


def _jobs(fetch: Fetch, repo: str, run_id: int) -> list[dict[str, Any]]:
    payload = fetch(f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    return list(payload.get("jobs", []))


def _job_named(jobs: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    return next((job for job in jobs if job.get("name") == name), None)


def failure_reason(conclusion: str, annotations: Sequence[Mapping[str, Any]]) -> str:
    if any(WALL_TIMEOUT_MARKER in str(annotation.get("message", "")) for annotation in annotations):
        return "wall-timeout"
    return "cancelled" if conclusion == "cancelled" else "failed"


def _skip_verdict(jobs: Sequence[Mapping[str, Any]], sha: str, run_url: str) -> tuple[int, str]:
    changes = _job_named(jobs, CHANGES_JOB_NAME)
    if changes is not None and changes.get("conclusion") == "success":
        return 0, f"::notice title={JOB_NAME} skipped by the path filter::sha={sha} run={run_url}"
    changes_conclusion = changes.get("conclusion") if changes is not None else "absent"
    return 1, (
        f"::error title={JOB_NAME} skipped upstream::sha={sha} run={run_url} "
        f"reason={CHANGES_JOB_NAME} concluded {changes_conclusion}"
    )


def _job_error(
    name: str,
    job: Mapping[str, Any],
    sha: str,
    run_url: str,
    annotations_for: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
) -> str:
    conclusion = str(job.get("conclusion"))
    if conclusion in FAILED_CONCLUSIONS:
        reason = failure_reason(conclusion, annotations_for(job))
        return (
            f"::error title={name} {conclusion}::sha={sha} run={run_url} "
            f"job={job.get('html_url', '?')} reason={reason}"
        )
    return f"::error title={name} {conclusion}::sha={sha} run={run_url} reason=unexpected-conclusion"


def classify_run(
    run: Mapping[str, Any],
    jobs: Sequence[Mapping[str, Any]],
    annotations_for: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
    shard_names: Sequence[str] | None = None,
) -> tuple[int, str]:
    """(exit code, workflow-command lines) for one CI run's full-regression shards."""

    expected = tuple(shard_names) if shard_names is not None else full_job_shard_names()
    sha = run.get("head_sha", "?")
    run_url = run.get("html_url", "?")
    by_name = {str(job.get("name")): job for job in jobs}
    shards = {name: by_name[name] for name in expected if name in by_name}
    missing = [name for name in expected if name not in by_name]
    undeclared = sorted(name for name in by_name if name.startswith(f"{JOB_NAME} (") and name not in expected)

    unexpanded = by_name.get(JOB_NAME)
    if unexpanded is not None and not shards and not undeclared:
        # The matrix never expanded. Skipped is the path-filter shape; a failed
        # or unknown conclusion is reported as such. A lone success falls
        # through: one green job is not the declared shards.
        conclusion = str(unexpanded.get("conclusion"))
        if conclusion == "skipped":
            return _skip_verdict(jobs, sha, run_url)
        if conclusion != "success":
            return 1, _job_error(JOB_NAME, unexpanded, sha, run_url, annotations_for)
    if not missing and not undeclared and all(job.get("conclusion") == "skipped" for job in shards.values()):
        return _skip_verdict(jobs, sha, run_url)

    errors: list[str] = []
    if missing:
        errors.append(
            f"::error title={JOB_NAME} not found::job not found — renamed? watcher wiring broken "
            f"(sha={sha} run={run_url}) missing={', '.join(missing)}"
        )
    if undeclared:
        errors.append(
            f"::error title={JOB_NAME} undeclared shard::job is not in the ci.yml matrix "
            f"(sha={sha} run={run_url}) unexpected={', '.join(undeclared)}"
        )
    errors.extend(
        _job_error(name, job, sha, run_url, annotations_for)
        for name, job in shards.items()
        if job.get("conclusion") != "success"
    )
    if errors:
        return 1, "\n".join(errors)
    return 0, f"::notice title={JOB_NAME} success::sha={sha} run={run_url} shards={len(expected)}"


def check_run(
    fetch: Fetch, repo: str, run_id: int | None, shard_names: Sequence[str] | None = None
) -> tuple[int, str]:
    if run_id is None:
        query = urllib.parse.urlencode({"branch": "master", "event": "push", "status": "completed", "per_page": 1})
        runs = fetch(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs?{query}").get("workflow_runs", [])
        if not runs:
            return 1, "::error title=No CI run::no completed master push CI run to check"
        run_id = int(runs[0]["id"])
    run = fetch(f"/repos/{repo}/actions/runs/{run_id}")
    jobs = _jobs(fetch, repo, run_id)

    def annotations_for(job: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        return list(fetch(f"{job['check_run_url']}/annotations"))

    return classify_run(run, jobs, annotations_for, shard_names)


def p95_nearest_rank(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def full_job_timeout_minutes(path: Path = CI_WORKFLOW_PATH) -> int:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    return int(workflow["jobs"][FULL_JOB_KEY]["timeout-minutes"])


def full_job_shard_names(path: Path = CI_WORKFLOW_PATH) -> tuple[str, ...]:
    """The job names GitHub gives the full-regression matrix: ``<name> (<shard>)``.

    Read from ci.yml so the shard count lives in one place. Anything that would
    make the names unpredictable (a templated name, a second matrix dimension,
    ``include``/``exclude``, a computed shard list) is refused.
    """

    job = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][FULL_JOB_KEY]
    if job.get("name") != JOB_NAME:
        raise ValueError(f"jobs.{FULL_JOB_KEY}.name must be the static string {JOB_NAME!r}, got {job.get('name')!r}")
    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict) or list(matrix) != ["shard"]:
        raise ValueError(f"jobs.{FULL_JOB_KEY}.strategy.matrix must have exactly one dimension, `shard`")
    values = matrix["shard"]
    if not isinstance(values, list) or not values or not all(type(value) in (int, str) for value in values):
        raise ValueError(f"jobs.{FULL_JOB_KEY}.strategy.matrix.shard must be a non-empty literal list")
    return tuple(f"{JOB_NAME} ({value})" for value in values)


def _minutes(job: Mapping[str, Any]) -> float:
    started = datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00"))
    completed = datetime.fromisoformat(str(job["completed_at"]).replace("Z", "+00:00"))
    return (completed - started).total_seconds() / 60


def margin_line(durations: Sequence[float], timeout_minutes: int, note: str = "", name: str = JOB_NAME) -> str:
    suffix = f"; {note}" if note else ""
    if not durations:
        return f"::notice title={name} margin::no successful runs found (n=0){suffix}"
    p95 = p95_nearest_rank(durations)
    shape = f"P95={p95:.1f}min"
    if p95 > MARGIN_THRESHOLD * timeout_minutes:
        return (
            f"::warning title={name} margin::{shape} > {MARGIN_THRESHOLD:.0%} of {timeout_minutes}min "
            f"(n={len(durations)}){suffix}"
        )
    return (
        f"::notice title={name} margin::{shape} <= {MARGIN_THRESHOLD:.0%} of {timeout_minutes}min "
        f"(n={len(durations)}){suffix}"
    )


def margin_lines(per_shard: Mapping[str, Sequence[float]], timeout_minutes: int, note: str = "") -> list[str]:
    """One margin line per shard; a single n=0 notice when no sharded run was found."""

    if not any(per_shard.values()):
        return [margin_line([], timeout_minutes, note)]
    return [margin_line(durations, timeout_minutes, note, name) for name, durations in per_shard.items()]


def successful_shard_durations(
    fetch: Fetch, repo: str, now: datetime, shard_names: Sequence[str] | None = None
) -> tuple[dict[str, list[float]], str]:
    """Per-shard durations of the newest successful sharded runs, and a note if the scan was cut short.

    A run is a sample only when it carries every declared shard job and all of
    them succeeded; a run of the old single job is ignored.
    """

    expected = tuple(shard_names) if shard_names is not None else full_job_shard_names()
    since = (now - timedelta(days=MARGIN_WINDOW_DAYS)).date().isoformat()
    runs: list[Mapping[str, Any]] = []
    per_shard: dict[str, list[float]] = {name: [] for name in expected}
    samples = 0
    note = ""
    try:
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "branch": "master",
                    "event": "push",
                    "status": "completed",
                    "created": f">={since}",
                    "per_page": RUNS_PER_PAGE,
                    "page": page,
                }
            )
            batch = fetch(f"/repos/{repo}/actions/workflows/{CI_WORKFLOW_FILE}/runs?{query}").get("workflow_runs", [])
            runs.extend(batch)
            if len(batch) < RUNS_PER_PAGE:
                break
            page += 1
        for run in sorted(runs, key=lambda item: str(item["created_at"]), reverse=True):
            if samples >= MARGIN_SAMPLE:
                break
            jobs = _jobs(fetch, repo, int(run["id"]))
            shards = [job for name in expected if (job := _job_named(jobs, name)) is not None]
            if len(shards) == len(expected) and all(job.get("conclusion") == "success" for job in shards):
                samples += 1
                for name, job in zip(expected, shards, strict=True):
                    per_shard[name].append(_minutes(job))
    except ApiCallCapReached as exc:
        note = str(exc)
    return per_shard, note


def margin(
    fetch: Fetch, repo: str, timeout_minutes: int, now: datetime, shard_names: Sequence[str] | None = None
) -> str:
    per_shard, note = successful_shard_durations(fetch, repo, now, shard_names)
    return "\n".join(margin_lines(per_shard, timeout_minutes, note))


def _parse_run_id(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isdigit():
        raise SystemExit(f"--run-id must be a number, got {value!r}")
    return int(value)


def main(argv: Sequence[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-run", help="classify one CI run's full-regression shards")
    check.add_argument("--run-id", default="", help="CI run id; empty = newest completed master push run")
    commands.add_parser("margin", help="warn when a shard's P95 nears the job timeout")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if fetch is None:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            parser.error("GITHUB_TOKEN is required")
        fetch = github_fetch(token)
    capped = CappedFetch(fetch)

    if args.command == "check-run":
        code, line = check_run(capped, args.repo, _parse_run_id(args.run_id))
        print(line)
        return code
    try:
        line = margin(capped, args.repo, full_job_timeout_minutes(), datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - margin is a warning; it never fails the job
        line = f"::warning title={JOB_NAME} margin::could not compute the margin: {type(exc).__name__}: {exc}"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
