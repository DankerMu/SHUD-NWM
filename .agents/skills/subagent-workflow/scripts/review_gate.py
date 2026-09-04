#!/usr/bin/env python3
"""review_gate.py - mechanical state machine for the subagent-workflow three-round hard gate.

The orchestrator records every comprehensive cross-review round here. The CLI
recomputes the gate lock, appends the human-readable ledger line, and refuses
illegal transitions (recording a round that ran while locked, a `converging`
or `depth` retro that does not qualify) at the moment they are attempted - not at the
pre-merge gate. The optional `review-gate` hook (Claude Code PreToolUse)
denies implementer/reviewer subagent spawns whenever the persisted state says
`locked`; it reads the precomputed `locked`/`lockReason` fields and contains
no gate logic of its own.

State file: <root>/.review-gate.json. Deterministic, stdlib-only.

Hard ceiling: 5 comprehensive rounds per PR (cost governor). The 5th not-clean
round is terminal - only a `breadth` (PR split) retro may be registered, and
`record-round` refuses any 6th round regardless of retro budgets.

Per-issue ceiling memory (0.28.0): when `open` is given `--issue`, ceiling
events and gate entries are recorded in <root>/.review-gate-issues.json, which
survives `close` (commit it; only .review-gate.json is per-PR ephemeral). A
later PR for an issue that already hit the ceiling on another PR is escalated:
`depth`/`noise` retros are refused unless `record-retro --user-approved
"<one-line user decision>"` records an explicit user call (the approval is
appended to the ledger for audit). `breadth` and `converging` are unaffected -
the anchor (a prior ceiling) derives from not-clean rounds and cannot be
label-gamed, and the escape hatch keeps a genuinely recurring invariant from
being forced into a forbidden split without a human in the loop.

Commands:
  open         --pr N [--issue N] [--review-dir PATH]
  record-round --sha SHA (--clean | --not-clean) [--verified N]
               [--highest critical|major|minor|none] [--classes a,b]
  record-retro --path FILE --shape breadth|depth|noise|converging
               [--user-approved TEXT]
  lock         --reason TEXT        (working-day / same-invariant triggers)
  status       [--assert-unlocked]
  close        [--outcome merged|superseded-by-split|abandoned|descoped]

Exit codes: 0 = ok/unlocked, 2 = refused or locked (attention required).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

STATE_NAME = ".review-gate.json"
HISTORY_NAME = ".review-gate-issues.json"
LEDGER_NAME = "round-ledger.log"
MAX_ROUNDS = 5
SEVERITIES = ["none", "minor", "major", "critical"]
SHAPES = ["breadth", "depth", "noise", "converging"]
OUTCOMES = ["merged", "superseded-by-split", "abandoned", "descoped"]
DEFAULT_BLOCKED = ["implementer", "reviewer"]


def state_path(root: str) -> Path:
    return Path(root).resolve() / STATE_NAME


def load_state(root: str) -> dict:
    path = state_path(root)
    if not path.is_file():
        raise SystemExit(f"review_gate: no {STATE_NAME} at {path.parent} - run `open --pr <N>` first")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_state(root: str, state: dict) -> None:
    locked, reason = compute_lock(state)
    state["locked"] = locked
    state["lockReason"] = reason
    state_path(root).write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def history_path(root: str) -> Path:
    return Path(root).resolve() / HISTORY_NAME


def load_history(root: str) -> dict:
    path = history_path(root)
    if not path.is_file():
        return {"issues": {}}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def issue_record(history: dict, issue: int) -> dict:
    return history["issues"].setdefault(str(issue), {"ceilingPrs": [], "gateEntries": 0, "closed": []})


def save_history(root: str, history: dict) -> None:
    history_path(root).write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def record_ceiling(root: str, state: dict) -> None:
    if state.get("issue") is None:
        return
    history = load_history(root)
    rec = issue_record(history, state["issue"])
    if state["pr"] not in rec["ceilingPrs"]:
        rec["ceilingPrs"].append(state["pr"])
        save_history(root, history)


def escalation_reason(root: str, issue: int | None, pr: int) -> str | None:
    if issue is None:
        return None
    rec = load_history(root)["issues"].get(str(issue))
    if not rec:
        return None
    prior = [p for p in rec.get("ceilingPrs", []) if p != pr]
    if prior:
        return (f"issue #{issue} already hit the round ceiling on PR "
                f"{', '.join(f'#{p}' for p in prior)}")
    return None


def compute_lock(state: dict) -> tuple[bool, str | None]:
    if state.get("manualLock"):
        return True, (
            f"manual gate ({state['manualLock']}): persist a Review Failure Retro and register it "
            "with record-retro before any further review/fix action"
        )
    rounds = state.get("rounds", [])
    if rounds and not rounds[-1]["clean"] and rounds[-1]["n"] >= MAX_ROUNDS:
        return True, (
            f"round ceiling: {MAX_ROUNDS} comprehensive rounds reached and the loop is still not clean - "
            "terminal for the ordinary loop. The corrective action is a PR split (children re-enter as new "
            "PRs with fresh counters); descope or a user decision are the only alternatives. No retro "
            "extends this budget"
        )
    if not rounds or rounds[-1]["clean"] or rounds[-1]["n"] < 3:
        return False, None
    last = rounds[-1]
    retros = state.get("retros", [])
    if not retros:
        return True, (
            f"three-round hard gate: round {last['n']} is not clean and no Review Failure Retro is "
            "registered - persist the retro and run record-retro before any implementer fix, "
            "review round, Phase 7, CI wait, or merge"
        )
    retro = retros[-1]
    if last["n"] < retro["atRound"] + retro["budget"]:
        return False, None
    suffix = "; `converging` is no longer selectable" if any(r["shape"] == "converging" for r in retros) else ""
    return True, (
        f"post-gate budget exhausted: round {last['n']} is not clean after the {retro['shape']} retro "
        f"at round {retro['atRound']} (budget {retro['budget']} round(s)) - register a new retro with a "
        f"stronger corrective action{suffix}"
    )


def severity_rank(sev: str) -> int:
    return SEVERITIES.index(sev)


def compute_repeats(rounds: list[dict], classes: list[str]) -> list[str]:
    first_seen: dict[str, int] = {}
    for rd in rounds:
        for cls in rd.get("classes", []):
            first_seen.setdefault(cls, rd["n"])
    return [f"{cls} (also round {first_seen[cls]})" for cls in classes if cls in first_seen]


def converging_disqualifiers(state: dict) -> list[str]:
    out = []
    rounds = state.get("rounds", [])
    for rd in rounds:
        if rd.get("repeats"):
            out.append(f"failure-class repeat in round {rd['n']}: {'; '.join(rd['repeats'])}")
        if rd["n"] >= 3 and not rd["clean"] and rd.get("highest") in ("critical", "major"):
            out.append(f"{rd['highest']} finding in round {rd['n']}")
    for prev, cur in zip(rounds, rounds[1:]):
        if cur["verified"] > prev["verified"] or severity_rank(cur["highest"]) > severity_rank(prev["highest"]):
            out.append(
                f"trend not non-increasing: round {prev['n']} -> {cur['n']} "
                f"(verified {prev['verified']} -> {cur['verified']}, highest {prev['highest']} -> {cur['highest']})"
            )
    if len(rounds) >= 2 and not any(
        cur["verified"] < prev["verified"] or severity_rank(cur["highest"]) < severity_rank(prev["highest"])
        for prev, cur in zip(rounds, rounds[1:])
    ):
        out.append("no strictly decreasing round-over-round metric (verified count or highest severity)")
    if any(r["shape"] == "converging" for r in state.get("retros", [])):
        out.append("converging already used once for this PR")
    if rounds and rounds[-1]["n"] >= 5:
        out.append("round 5 reached - converging is no longer selectable")
    return out


def _bullets_after(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if heading in line:
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            found = [rest] if rest and not rest.startswith("<") else []
            for follow in lines[i + 1:]:
                stripped = follow.strip()
                if not stripped.startswith("-"):
                    break
                content = stripped.lstrip("- ").strip()
                if content and not content.startswith("<"):
                    found.append(content)
            return found
    return []


def depth_form_problems(text: str) -> list[str]:
    problems = []
    inv = re.search(r"^\s*-?\s*Invariant:\s*(.+)$", text, re.MULTILINE)
    if not inv or inv.group(1).strip().startswith("<"):
        problems.append("missing or unfilled `Invariant:` line naming the single recurring rule")
    if len(_bullets_after(text, "Recurring findings:")) < 2:
        problems.append("`Recurring findings:` must map at least two verified findings "
                        "(across rounds or sibling surfaces) to the invariant")
    return problems


def split_rebuttal_present(text: str) -> bool:
    return bool(_bullets_after(text, "Split rebuttal"))


def ledger_line(rd: dict, gate: str) -> str:
    classes = ", ".join(rd["classes"]) if rd["classes"] else "none"
    repeats = f"yes ({'; '.join(rd['repeats'])})" if rd["repeats"] else "no"
    return (
        f"Round {rd['n']} | {rd['sha']} | {'clean' if rd['clean'] else 'not-clean'} | "
        f"verified findings: {rd['verified']} | highest severity: {rd['highest']} | "
        f"failure classes: {classes} | repeats prior class: {repeats} | gate: {gate}"
    )


def append_ledger(root: str, state: dict, line: str) -> None:
    review_dir = Path(root).resolve() / state["reviewDir"]
    review_dir.mkdir(parents=True, exist_ok=True)
    with (review_dir / LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def cmd_open(args) -> int:
    path = state_path(args.root)
    if path.is_file():
        print(f"review_gate: {path} already exists - run `close` first if the previous PR is done", file=sys.stderr)
        return 2
    escalated = escalation_reason(args.root, args.issue, args.pr)
    state = {
        "enabled": True,
        "pr": args.pr,
        "issue": args.issue,
        "issueEscalated": escalated,
        "reviewDir": args.review_dir,
        "rounds": [],
        "retros": [],
        "manualLock": None,
        "blockedSubagents": DEFAULT_BLOCKED,
    }
    save_state(args.root, state)
    print(f"review_gate: opened for PR #{args.pr} (state: {path}, ledger: {args.review_dir}/{LEDGER_NAME})")
    if escalated:
        print(f"review_gate: ESCALATED - {escalated}; depth/noise retros on this PR require "
              "--user-approved with a recorded user decision (split/descope are the defaults)", file=sys.stderr)
    return 0


def cmd_record_round(args) -> int:
    state = load_state(args.root)
    if len(state["rounds"]) >= MAX_ROUNDS:
        append_ledger(args.root, state,
                      f"VIOLATION | round attempt beyond the {MAX_ROUNDS}-round ceiling refused: sha {args.sha}")
        print(f"review_gate: refused - the {MAX_ROUNDS}-round ceiling is reached for this PR. The ordinary "
              "loop is closed: split the PR (fresh counters per child), descope, or take a user decision, "
              "then `close`.", file=sys.stderr)
        return 2
    was_locked, prior_reason = state.get("locked", False), state.get("lockReason")
    clean = args.clean
    classes = [c.strip() for c in (args.classes or "").split(",") if c.strip()]
    if not clean and (args.verified is None or args.verified < 1 or args.highest == "none" or not classes):
        print("review_gate: --not-clean requires --verified >= 1, --highest, and --classes "
              "(a not-clean round has at least one actionable finding)", file=sys.stderr)
        return 2
    rd = {
        "n": (state["rounds"][-1]["n"] + 1) if state["rounds"] else 1,
        "sha": args.sha,
        "clean": clean,
        "verified": 0 if clean else args.verified,
        "highest": "none" if clean else args.highest,
        "classes": [] if clean else classes,
        "repeats": [] if clean else compute_repeats(state["rounds"], classes),
        "violation": "recorded-while-locked" if was_locked else None,
    }
    state["rounds"].append(rd)
    save_state(args.root, state)
    locked, reason = state["locked"], state["lockReason"]
    gate = "none"
    if locked:
        if state.get("manualLock"):
            gate = "manual"
        elif not rd["clean"] and rd["n"] >= MAX_ROUNDS:
            gate = "round-ceiling"
        elif not state["retros"]:
            gate = "three-round"
        else:
            gate = "post-gate-budget"
    if gate == "round-ceiling":
        record_ceiling(args.root, state)
    line = ledger_line(rd, gate)
    append_ledger(args.root, state, line)
    print(line)
    if was_locked:
        append_ledger(args.root, state, f"VIOLATION | round {rd['n']} ran while locked: {prior_reason}")
        print(f"review_gate: VIOLATION - this round ran while the gate was locked ({prior_reason}). "
              f"It is recorded for the accountability log; the gate stays locked.", file=sys.stderr)
        return 2
    if locked:
        print(f"review_gate: GATE LOCKED - {reason}", file=sys.stderr)
        return 2
    return 0


def cmd_record_retro(args) -> int:
    state = load_state(args.root)
    retro_file = Path(args.path)
    if not retro_file.is_file():
        print(f"review_gate: retro file missing: {args.path} - persist the retro first", file=sys.stderr)
        return 2
    text = retro_file.read_text(encoding="utf-8")
    if not text.strip():
        print(f"review_gate: retro file empty: {args.path} - persist the retro first", file=sys.stderr)
        return 2
    rounds = state.get("rounds", [])
    if rounds and not rounds[-1]["clean"] and rounds[-1]["n"] >= MAX_ROUNDS and args.shape != "breadth":
        print("review_gate: refused - the round ceiling is terminal; only a `breadth` retro documenting "
              "the PR split may be registered (descope or a user decision go through `close`)", file=sys.stderr)
        return 2
    if args.shape in ("depth", "noise") and state.get("issueEscalated") and not args.user_approved:
        print(f"review_gate: `{args.shape}` refused - {state['issueEscalated']}. A successor PR for a "
              "ceiling-hit issue does not get to keep digging by default: split or descope, or pass "
              "--user-approved \"<one-line user decision>\" to record an explicit human call to continue",
              file=sys.stderr)
        return 2
    if args.shape == "depth":
        problems = depth_form_problems(text)
        if problems:
            print("review_gate: `depth` refused - retro lacks required depth evidence:", file=sys.stderr)
            for item in problems:
                print(f"  - {item}", file=sys.stderr)
            print("name the invariant and map the recurring findings, or choose breadth/noise instead", file=sys.stderr)
            return 2
    if state["retros"] and args.shape in ("depth", "noise") and not split_rebuttal_present(text):
        print("review_gate: refused - from the second gate entry a PR split is the default corrective action. "
              "A depth/noise retro must carry a non-empty `Split rebuttal:` citing why the surfaces are not "
              "independently splittable (`breadth` is the split; `converging` is exempt)", file=sys.stderr)
        return 2
    if args.shape == "converging":
        disqualifiers = converging_disqualifiers(state)
        if disqualifiers:
            print("review_gate: `converging` refused - disqualifiers:", file=sys.stderr)
            for item in disqualifiers:
                print(f"  - {item}", file=sys.stderr)
            print("choose breadth, depth, or noise instead", file=sys.stderr)
            return 2
    at_round = state["rounds"][-1]["n"] if state["rounds"] else 0
    retro = {"atRound": at_round, "shape": args.shape, "path": str(retro_file), "budget": 2 if args.shape == "converging" else 1}
    state["retros"].append(retro)
    state["manualLock"] = None
    save_state(args.root, state)
    if state.get("issue") is not None:
        history = load_history(args.root)
        issue_record(history, state["issue"])["gateEntries"] += 1
        save_history(args.root, history)
    line = f"Retro | shape: {args.shape} | at round {at_round} | {retro_file} | budget: {retro['budget']} round(s)"
    append_ledger(args.root, state, line)
    if args.user_approved and args.shape in ("depth", "noise") and state.get("issueEscalated"):
        append_ledger(args.root, state,
                      f"User decision | post-ceiling {args.shape} continuation approved: {args.user_approved}")
    status = ("gate unlocked - next action must be the retro's corrective action"
              if not state["locked"] else f"gate remains locked ({state['lockReason']})")
    print(f"review_gate: retro registered ({line}); {status}")
    if args.shape == "depth" and not any(rd.get("repeats") for rd in state["rounds"]):
        warn = ("Depth-without-recurrence | ledger shows no failure-class repeat - verify the depth claim "
                "at grill/Phase 8 (label granularity or misclassification)")
        append_ledger(args.root, state, warn)
        print(f"review_gate: WARNING - {warn}", file=sys.stderr)
    return 0


def cmd_lock(args) -> int:
    state = load_state(args.root)
    state["manualLock"] = args.reason
    save_state(args.root, state)
    append_ledger(args.root, state, f"Manual gate | {args.reason}")
    print(f"review_gate: locked ({args.reason}) - persist and register a Review Failure Retro to continue")
    return 0


def cmd_status(args) -> int:
    state = load_state(args.root)
    rounds = state["rounds"]
    last = rounds[-1] if rounds else None
    issue = f" | issue #{state['issue']}" if state.get("issue") is not None else ""
    print(f"PR #{state['pr']}{issue} | rounds: {len(rounds)} | latest: "
          f"{'-' if not last else ledger_line(last, 'n/a')}")
    print(f"locked: {state['locked']}" + (f" | {state['lockReason']}" if state["locked"] else ""))
    if state.get("issueEscalated"):
        print(f"escalated: {state['issueEscalated']}")
    if args.assert_unlocked and state["locked"]:
        print(f"review_gate: assert-unlocked failed - {state['lockReason']}", file=sys.stderr)
        return 2
    return 0


def cmd_close(args) -> int:
    path = state_path(args.root)
    if not path.is_file():
        print("review_gate: nothing to close")
        return 0
    with path.open(encoding="utf-8") as fh:
        state = json.load(fh)
    if state.get("issue") is not None:
        history = load_history(args.root)
        issue_record(history, state["issue"])["closed"].append(
            {"pr": state["pr"], "outcome": args.outcome or "closed", "rounds": len(state.get("rounds", []))})
        save_history(args.root, history)
    os.remove(path)
    print(f"review_gate: closed ({path} removed)")
    if args.outcome and args.outcome != "merged":
        print(f"review_gate: terminal outcome `{args.outcome}` - append the terminal accountability line "
              "(outcome field) to docs/review-loop-log.jsonl, and when the issue came from "
              "stage-change-pipeline, route it back as a sizing-retro line; split children re-enter that "
              "pipeline's Stage 5 implementation-ready contract, not the workflow as bare fixtures",
              file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
                        help="project root holding %s (default: CLAUDE_PROJECT_DIR or cwd)" % STATE_NAME)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("open", help="start gate tracking for a PR")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--issue", type=int, default=None,
                   help="source issue number - enables cross-PR ceiling memory in %s" % HISTORY_NAME)
    p.add_argument("--review-dir", default=None)
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("record-round", help="record a comprehensive cross-review round")
    p.add_argument("--sha", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--clean", action="store_true")
    group.add_argument("--not-clean", dest="clean", action="store_false")
    p.add_argument("--verified", type=int, default=None, help="verified finding count (CONFIRMED + blocking PLAUSIBLE)")
    p.add_argument("--highest", choices=SEVERITIES, default="none")
    p.add_argument("--classes", default="", help="comma-separated failure classes")
    p.set_defaults(fn=cmd_record_round)

    p = sub.add_parser("record-retro", help="register a persisted Review Failure Retro")
    p.add_argument("--path", required=True)
    p.add_argument("--shape", choices=SHAPES, required=True)
    p.add_argument("--user-approved", default=None,
                   help="one-line recorded user decision; required for depth/noise on a PR whose issue "
                        "already hit the round ceiling on another PR")
    p.set_defaults(fn=cmd_record_retro)

    p = sub.add_parser("lock", help="manual gate trigger (working-day, same-invariant)")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_lock)

    p = sub.add_parser("status", help="print gate state")
    p.add_argument("--assert-unlocked", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("close", help="remove gate state (PR merged or abandoned)")
    p.add_argument("--outcome", choices=OUTCOMES, default=None,
                   help="how the PR ended; non-merged outcomes remind about the terminal "
                        "accountability line and the upstream sizing-retro")
    p.set_defaults(fn=cmd_close)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "open" and args.review_dir is None:
        args.review_dir = f".workplans/pr-{args.pr}/review"
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
