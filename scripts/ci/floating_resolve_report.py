#!/usr/bin/env python3
"""Report the weekly floating dependency resolve (#2709).

Every CI job installs ``uv.lock`` (#2573), so upstream breakage stays invisible
until someone upgrades the lock. ``.github/workflows/floating-resolve-report.yml``
resolves without the lock once a week, runs the tests and calls this script. It
is report-only: it never gates a merge and never writes to the repository.

``diff OLD NEW`` prints the package-version differences between two lock files,
one ``name: old -> new`` line per package.

``report`` turns the job's step outcomes into one of four failure kinds and
keeps ONE tracking issue, found by its fixed title among the open issues that
carry the labels this script creates it with:

* ``injected`` - the ``inject_failure`` input failed the run on purpose;
* ``resolve``  - ``uv lock --upgrade`` or the unlocked ``uv sync`` failed;
* ``tests``    - pytest returned a non-zero exit code;
* ``timeout``  - a pytest step ended without an exit code (killed at its timeout).

A failure comments on the open tracking issue, or creates it when there is
none. A success creates nothing; an open tracking issue gets a "passing again"
comment and is left open for a human to close. A run that failed outside those
steps (checkout, setup, a cancelled job) is neither: a warning, no issue.

A GitHub API error while reporting a passing run is a warning and exit 0 (the
lane itself passed); while reporting a failure it is an error and exit 1.

Standard library only, so it runs before (and without) the project environment.
The token comes from ``GITHUB_TOKEN`` and the repository from ``--repo`` or
``GITHUB_REPOSITORY``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

TRACKING_TITLE = "ci(deps): floating dependency resolve is failing"
# Both exist in the repository's label list (checked 2026-10-04).
TRACKING_LABELS = ("tech-debt", "needs-followup")
RESOLVE_STEPS = ("lock", "sync")
PYTEST_STEPS = ("proj", "tests")
STEPS = ("inject", *RESOLVE_STEPS, *PYTEST_STEPS)
# GitHub rejects issue and comment bodies above 65536 characters.
DIFF_LINE_LIMIT = 200
KIND_TEXT = {
    "injected": "the failure was injected on purpose (`inject_failure: true`); no upstream state is involved",
    "resolve": "`uv lock --upgrade` or the unlocked `uv sync` failed",
    "tests": "pytest failed on the freshly resolved dependency set",
    "timeout": "a pytest step was killed at its timeout before pytest returned",
}

Request = Callable[[str, str, Any], Any]


def github_request(token: str, api_url: str = "https://api.github.com") -> Request:
    """``request(method, path, body)`` over the REST API; ``body`` is JSON or None."""

    def request(method: str, path: str, body: Any = None) -> Any:
        http_request = urllib.request.Request(
            api_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(http_request, timeout=30) as response:
            return json.load(response)

    return request


# --- diff ---------------------------------------------------------------------


def lock_versions(lock_text: str) -> dict[str, tuple[str, ...]]:
    """Package name -> its sorted versions (a lock may fork one name by marker)."""

    versions: dict[str, set[str]] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        if "version" in package:
            versions.setdefault(str(package["name"]), set()).add(str(package["version"]))
    return {name: tuple(sorted(found)) for name, found in versions.items()}


def lock_diff_lines(old_text: str, new_text: str) -> list[str]:
    old, new = lock_versions(old_text), lock_versions(new_text)
    return [
        f"{name}: {', '.join(old.get(name, ())) or '(absent)'} -> {', '.join(new.get(name, ())) or '(absent)'}"
        for name in sorted(set(old) | set(new))
        if old.get(name) != new.get(name)
    ]


# --- report -------------------------------------------------------------------


def failure_kind(
    outcomes: Mapping[str, str], exit_codes: Mapping[str, str], job_cancelled: bool = False
) -> str | None:
    """None for a passing run, else ``injected``/``resolve``/``tests``/``timeout``/``unclassified``."""

    if outcomes.get("inject") == "failure":
        return "injected"
    if job_cancelled:
        return "unclassified"
    if any(outcomes.get(step) == "failure" for step in RESOLVE_STEPS):
        return "resolve"
    for step in PYTEST_STEPS:
        if outcomes.get(step) in ("failure", "cancelled"):
            return "tests" if exit_codes.get(step, "").strip() else "timeout"
    if all(outcomes.get(step) == "success" for step in (*RESOLVE_STEPS, *PYTEST_STEPS)):
        return None
    return "unclassified"


def find_tracking_issue(request: Request, repo: str) -> int | None:
    """The number of the oldest OPEN issue whose title is exactly ``TRACKING_TITLE``.

    Uses the issue list endpoint, not ``/search/issues``: the search index lags,
    so two failing runs close together would each see no issue and open two.
    The list also returns pull requests, which are skipped.
    """

    query = urllib.parse.urlencode({"state": "open", "labels": ",".join(TRACKING_LABELS), "per_page": 100})
    items = request("GET", f"/repos/{repo}/issues?{query}", None)
    numbers = [
        int(item["number"])
        for item in items
        if item.get("title") == TRACKING_TITLE and item.get("state") == "open" and "pull_request" not in item
    ]
    return min(numbers) if numbers else None


def _failure_text(kind: str, run_url: str, diff_text: str) -> str:
    lines = [
        "The floating dependency resolve lane failed.",
        "",
        f"- kind: `{kind}` - {KIND_TEXT[kind]}",
        f"- run: {run_url}",
        "",
        "Package versions that differ from the checked-in `uv.lock` (full lock files are in the run's artifact):",
        "",
        "```text",
    ]
    diff_lines = diff_text.strip().splitlines()
    lines.extend(diff_lines[:DIFF_LINE_LIMIT] or ["(no diff was produced)"])
    if len(diff_lines) > DIFF_LINE_LIMIT:
        lines.append(f"... truncated, {len(diff_lines) - DIFF_LINE_LIMIT} more lines in the artifact")
    lines.append("```")
    return "\n".join(lines)


def report(request: Request, repo: str, kind: str | None, run_url: str, diff_text: str) -> str:
    """Create or comment per the module docstring; returns what was done."""

    number = find_tracking_issue(request, repo)
    if kind is None:
        if number is None:
            return "passing; no open tracking issue, nothing written"
        text = f"The floating dependency resolve lane is passing again: {run_url}\n\nLeft open for a human to close."
        request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": text})
        return f"passing again; commented on #{number}"
    text = _failure_text(kind, run_url, diff_text)
    if number is not None:
        request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": text})
        return f"{kind}; commented on #{number}"
    body = {"title": TRACKING_TITLE, "body": text, "labels": list(TRACKING_LABELS)}
    created = request("POST", f"/repos/{repo}/issues", body)
    return f"{kind}; created #{created.get('number', '?')}"


def main(argv: Sequence[str] | None = None, request: Request | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    diff = commands.add_parser("diff", help="package-version differences between two lock files")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    reporter = commands.add_parser("report", help="create or update the tracking issue from the step outcomes")
    reporter.add_argument("--run-url", required=True)
    reporter.add_argument("--diff-file", type=Path)
    reporter.add_argument("--job-cancelled", choices=("true", "false"), default="false")
    for step in STEPS:
        reporter.add_argument(f"--{step}", required=True, help=f"outcome of the `{step}` step")
    for step in PYTEST_STEPS:
        reporter.add_argument(f"--{step}-rc", default="", help=f"pytest exit code of `{step}`; empty = none")
    args = parser.parse_args(argv)

    if args.command == "diff":
        lines = lock_diff_lines(args.old.read_text(encoding="utf-8"), args.new.read_text(encoding="utf-8"))
        print("\n".join(lines) if lines else "no package version differences")
        return 0

    kind = failure_kind(
        {step: getattr(args, step) for step in STEPS},
        {step: getattr(args, f"{step}_rc") for step in PYTEST_STEPS},
        job_cancelled=args.job_cancelled == "true",
    )
    if kind == "unclassified":
        print(
            "::warning title=Floating resolve report::the run ended outside the resolve and test steps "
            "(setup failure or cancelled job); no tracking issue was written"
        )
        return 0
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    if request is None:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            parser.error("GITHUB_TOKEN is required")
        request = github_request(token)
    diff_text = args.diff_file.read_text(encoding="utf-8") if args.diff_file and args.diff_file.is_file() else ""
    try:
        done = report(request, args.repo, kind, args.run_url, diff_text)
    except urllib.error.URLError as error:  # HTTPError is a subclass
        if kind is None:
            # The lane passed; a GitHub API hiccup must not turn it red.
            print(f"::warning title=Floating resolve report::passing run, but the GitHub API call failed: {error}")
            return 0
        print(f"::error title=Floating resolve report::kind={kind}, and the GitHub API call failed: {error}")
        return 1
    print(f"floating resolve report: {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
