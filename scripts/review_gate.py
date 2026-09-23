#!/usr/bin/env python3
"""review_gate.py - writer and escalation reader for the cross-PR round-ceiling memory.

`.review-gate-issues.json` records, per source issue, the PRs on which the
review loop hit its hard stop (`ceilingPrs`) and how each PR for that issue
ended (`closed`). Per-PR fix-pass counting is NOT here: the installed
`subagent-workflow` skill's `fix_gate.py` owns it in the gitignored
`.review-gate.json`, and it keeps no issue history. This tool is the tracked,
memory-only complement (#2261): the one writer of the committed file and the
cross-PR escalation read `fix_gate.py` does not do.

Commands:
  record --issue N [--issue M ...] --pr P --rounds R --outcome OUTCOME [--ceiling]
      Append `{"pr", "outcome", "rounds"}` to each issue's `closed` list.
      Idempotent per (issue, pr): an identical re-run changes nothing, a
      differing one replaces that pair's record. `--ceiling` adds P to
      `ceilingPrs` once (fix_gate.py locked on P). `gateEntries` is never
      touched; new entries start at 0. Run it in the post-merge archive commit.
  check --issue N [--issue M ...] [--pr P]
      Exit 2 and name the PRs when an issue already has a ceiling PR (other
      than P). Run it next to `fix_gate.py open --pr P`; before a PR exists,
      run it without --pr.

Every command loads through a structure guard: a bare top-level issue key is
folded into `issues` when `issues` lacks it or holds an identical record, and
the load fails (exit 1, both records printed) when they conflict. Entry shape
and the `OUTCOMES` vocabulary are validated on load (exit 1 on violation).

Deterministic, stdlib-only. Exit codes: 0 = ok, 1 = malformed memory,
2 = escalation (check) or usage error (argparse).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HISTORY_NAME = ".review-gate-issues.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
# The single authority for close outcomes; tests/test_review_gate_issue_memory.py
# imports it.
OUTCOMES = ("merged", "superseded-by-split", "abandoned", "descoped")


class MalformedMemory(Exception):
    """The committed memory is malformed; the message names what and where."""


def history_path(root: Path) -> Path:
    return Path(root) / HISTORY_NAME


def _record_json(record: object) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def _validate(issues: dict) -> None:
    for key, entry in issues.items():
        where = f"issues[{key}]"
        if not key.isdigit():
            raise MalformedMemory(f"{where}: issue key must be a decimal issue number")
        if not isinstance(entry, dict):
            raise MalformedMemory(f"{where} must be a JSON object, got {type(entry).__name__}")
        ceiling = entry.get("ceilingPrs")
        if not isinstance(ceiling, list) or any(type(pr) is not int for pr in ceiling):
            raise MalformedMemory(f"{where}.ceilingPrs must be a list of int, got {ceiling!r}")
        # `type(...) is` on purpose: bool is an int subclass.
        if type(entry.get("gateEntries")) is not int:
            raise MalformedMemory(f"{where}.gateEntries must be int, got {entry.get('gateEntries')!r}")
        closed = entry.get("closed")
        if not isinstance(closed, list):
            raise MalformedMemory(f"{where}.closed must be a list, got {closed!r}")
        for index, record in enumerate(closed):
            if not isinstance(record, dict):
                raise MalformedMemory(f"{where}.closed[{index}] must be a JSON object, got {record!r}")
            outcome = record.get("outcome")
            if not isinstance(outcome, str) or outcome not in OUTCOMES:
                raise MalformedMemory(
                    f"{where}.closed[{index}].outcome={outcome!r} is outside OUTCOMES {list(OUTCOMES)}"
                )


def load_history(root: Path) -> dict:
    """The memory as `{"issues": {...}}`, folded and validated.

    Raises MalformedMemory on a conflicting bare key or a malformed entry. A
    fold is reported on stderr and persisted by the next `record`.
    """
    path = history_path(root)
    if not path.is_file():
        return {"issues": {}}
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedMemory(f"{path}: invalid JSON ({exc.msg})") from exc
    if not isinstance(history, dict):
        raise MalformedMemory(f"{path}: top level must be a JSON object, got {type(history).__name__}")
    issues = history.setdefault("issues", {})
    if not isinstance(issues, dict):
        raise MalformedMemory(f"{path}: 'issues' must be a JSON object, got {type(issues).__name__}")
    for key in [k for k in history if k != "issues"]:
        bare = history[key]
        if key in issues and issues[key] != bare:
            raise MalformedMemory(
                f"{path}: bare top-level key {key!r} conflicts with issues[{key!r}] - "
                "keep the correct record under `issues`, delete the bare copy, then re-run\n"
                f"  bare:          {_record_json(bare)}\n"
                f"  issues[{key!r}]: {_record_json(issues[key])}"
            )
        if key not in issues:
            issues[key] = bare
        del history[key]
        print(f"review_gate: folded bare top-level key {key!r} into issues", file=sys.stderr)
    _validate(issues)
    return history


def save_history(root: Path, history: dict) -> None:
    # The historical serializer, so diffs stay minimal (no sort_keys).
    history_path(root).write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_record(args: argparse.Namespace) -> int:
    history = load_history(args.root)
    for issue in args.issue:
        entry = history["issues"].setdefault(str(issue), {"ceilingPrs": [], "gateEntries": 0, "closed": []})
        new = {"pr": args.pr, "outcome": args.outcome, "rounds": args.rounds}
        closed = entry["closed"]
        index = next((i for i, record in enumerate(closed) if record.get("pr") == args.pr), None)
        if index is None:
            closed.append(new)
            action = "recorded"
        elif closed[index] == new:
            action = "unchanged"
        else:
            closed[index] = new
            action = "replaced"
        if args.ceiling and args.pr not in entry["ceilingPrs"]:
            entry["ceilingPrs"].append(args.pr)
        print(f"review_gate: issue #{issue} PR #{args.pr} {args.outcome} rounds={args.rounds}: {action}"
              + (" (ceiling)" if args.ceiling else ""))
    save_history(args.root, history)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    issues = load_history(args.root)["issues"]
    escalated = False
    for issue in args.issue:
        entry = issues.get(str(issue))
        prior = [pr for pr in (entry or {}).get("ceilingPrs", []) if pr != args.pr]
        if prior:
            escalated = True
            print(f"review_gate: issue #{issue} already hit the review hard stop on PR "
                  f"{', '.join(f'#{pr}' for pr in prior)} - a successor PR needs an explicit user "
                  "decision before its review loop starts")
        else:
            print(f"review_gate: issue #{issue}: no prior ceiling PR")
    return 2 if escalated else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help=f"directory holding {HISTORY_NAME} (default: this repository's root)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="record how a PR closed for its source issue(s)")
    p.add_argument("--issue", type=int, action="append", required=True, help="source issue (repeatable)")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--rounds", type=int, required=True, help="review rounds the PR ran")
    p.add_argument("--outcome", choices=OUTCOMES, required=True)
    p.add_argument("--ceiling", action="store_true",
                   help="fix_gate.py locked on this PR (the loop hit its hard stop)")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("check", help="cross-PR escalation read")
    p.add_argument("--issue", type=int, action="append", required=True, help="source issue (repeatable)")
    p.add_argument("--pr", type=int, default=None, help="the PR about to be reviewed, when it exists")
    p.set_defaults(fn=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "record" and args.rounds < 0:
        print("review_gate: --rounds must be a non-negative integer", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except MalformedMemory as exc:
        print(f"review_gate: {HISTORY_NAME} is malformed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
