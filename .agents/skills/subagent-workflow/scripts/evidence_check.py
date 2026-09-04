#!/usr/bin/env python3
"""evidence_check.py - mechanical evidence-hygiene linter for the subagent workflow.

The orchestrator runs this before spawning any comprehensive cross-review
round (initial or Phase 6.5 rerun) and again on the drafted PR body/evidence
files before posting at Phase 8. It lints the orchestrator's own bookkeeping -
the artifacts reviewers and the merge gate consume - so stale-evidence
findings stop burning comprehensive review rounds. A review round is the most
expensive linter there is; this closes the classes a script can catch and
leaves reviewers only what machines cannot judge.

Checks:
  placeholder   unreplaced template tokens (`<sha>`, `<fix summary>`, ...) and
                TODO/TBD/FIXME markers. Runs only on orchestrator-authored
                files passed via --file (PR body draft, evidence manifest,
                comment drafts); reviewer-authored reports may legitimately
                quote code or discuss TODOs and are exempt by design.
  head-sha      lines labeling a SHA as the current/frozen head must
                prefix-match the actual HEAD of --root (`git rev-parse HEAD`,
                or --head). Historical anchors such as `Last clean reviewed
                SHA` are not current-head claims and are not checked.
  round-status  "Round K pending/in progress/..." claims are stale once
                .review-gate.json has recorded round K. Skipped with a note
                when no state file exists.
  gate-lock     .review-gate.json says `locked` - the mechanical gate
                transition was skipped or its corrective action has not been
                registered. Spawning reviewers or posting while locked is the
                out-of-band bypass this check exists to catch; a deliberate
                override (e.g. a user-approved descope merge at the terminal
                ceiling) proceeds as a recorded skip block, never silently.
  loop-log      --loop-log-entry validates a pending review-loop-log line
                before it is appended: required keys, fixture level must be a
                single canonical token (none|compact|expanded|high|
                broad-expanded - composites like `expanded/high` and ad-hoc
                labels like `standard` fragment the keep/cut sample), outcome
                vocabulary, date format, and - on a merged line - the schema
                of every `catches[i]`: a `round` (non-negative integer, `0` =
                the fixture-review round) and a non-empty `lens`. A catch
                missing either key is unattributable, so it silently distorts
                the lens-rotation figures loop_log_audit.py reports. Phase 7
                final review belongs in `phase7_catches` (same item schema,
                `round` not required - it runs after the numbered rounds);
                writing it into `catches` under a legacy pseudo-lens name
                (`final-review`, `gap-sweep`, ...) is rejected, because those
                names score as rotated-in lenses. `rotation_intent`, when
                present, must be `deliberate` or `incidental`. Every
                `round_lenses[i]` must be a non-empty LIST of lens names: the
                historical flat-list shape (a bare string per round) makes the
                audit's core set a set of characters, so it excludes those
                rows as unattributable.

Scanned files: every --file target (all checks) plus *.md/*.txt/*.log under
the review dir from .review-gate.json or --evidence-dir (head-sha and
round-status only).

Exit codes: 0 = clean, 2 = findings or nothing checkable (attention required).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import loop_log_audit
import review_gate

TEXT_SUFFIXES = {".md", ".txt", ".log"}
PLACEHOLDER_RE = re.compile(r"(?<![0-9A-Za-z_])<[A-Za-z][^<>\n]{0,40}>")
MARKER_RE = re.compile(r"\b(TODO|TBD|FIXME)\b")
HEAD_CLAIM_RE = re.compile(
    r"(?i)(?:\bcurrent\s+head\b(?:\s+sha)?|\bfrozen\b[^\n]{0,12}?\bsha\b|\bfull_sha\b|冻结[^\n]{0,8}?SHA)"
    r"[^0-9a-f\n]{0,20}\b([0-9a-f]{7,40})\b"
)
ROUND_CLAIM_RE = re.compile(
    r"(?i)\bround\s*#?\s*(\d+)\b[^\n]{0,40}?"
    r"(?:\bpending\b|\bin[- ]progress\b|\bnot\s+yet\b|待复审|待审|待跑|进行中|未完成)"
)
FIXTURE_LEVELS = ("none", "compact", "expanded", "high", "broad-expanded")
OUTCOMES = ("merged", "ceiling-split", "abandoned", "descoped")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_REQUIRED_KEYS = ("issue", "pr", "date", "fixture", "rounds")


def check_catch(raw_path: str, index: int, catch: object, findings: list[str],
                field: str = "catches", require_round: bool = True) -> None:
    """One finding per violation of the compliant-catch schema.

    A catch is attributable only when it carries a non-negative integer
    `round` (`0` is the fixture-review round; bool is not an integer) and a
    non-empty string `lens`. Same definition as loop_log_audit.py.

    `phase7_catches` items reuse this schema minus `round`: Phase 7 runs after
    the numbered rounds, so there is no round to record. A `round` that IS
    present is still validated.

    A `catches` item naming a legacy Phase 7 pseudo-lens is rejected outright.
    Those names are absent from `round_lenses[0]` by construction, so the
    audit scores them as rotated-in lenses; the compatibility shim in
    loop_log_audit.py exists for the lines already in the ledger and must not
    acquire new members.
    """
    where = f"{raw_path}:1: [loop-log] {field}[{index}]"
    if not isinstance(catch, dict):
        findings.append(f"{where} must be an object")
        return
    if "round" not in catch:
        if require_round:
            findings.append(f"{where} missing `round`")
    else:
        value = catch["round"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            findings.append(f"{where} round must be a non-negative integer")
    if "lens" not in catch:
        findings.append(f"{where} missing `lens`")
    else:
        lens = catch["lens"]
        if not isinstance(lens, str) or not lens:
            findings.append(f"{where} lens must be a non-empty string")
        elif field == "catches" and lens in loop_log_audit.FINAL_REVIEW_LEGACY_LENSES:
            findings.append(
                f"{where} lens `{lens}` is a Phase 7 final-review round role, not a cross-review "
                "lens - record final-review findings in `phase7_catches` instead, or the "
                "lens-rotation attribution counts them as rotated-in lenses"
            )


def check_loop_log_entry(raw_path: str, findings: list[str]) -> None:
    path = Path(raw_path)
    if not path.is_file():
        findings.append(f"{raw_path}:0: [loop-log] --loop-log-entry target missing")
        return
    text = path.read_text(encoding="utf-8").strip()
    try:
        entry = json.loads(text)
    except json.JSONDecodeError as exc:
        findings.append(f"{raw_path}:1: [loop-log] not valid JSON ({exc.msg})")
        return
    if not isinstance(entry, dict):
        findings.append(f"{raw_path}:1: [loop-log] entry must be a single JSON object")
        return
    for key in ENTRY_REQUIRED_KEYS:
        if key not in entry:
            findings.append(f"{raw_path}:1: [loop-log] missing required key `{key}`")
    fixture = entry.get("fixture")
    if fixture is not None and fixture not in FIXTURE_LEVELS:
        findings.append(
            f"{raw_path}:1: [loop-log] fixture `{fixture}` is off-vocabulary - use exactly one of "
            f"{'|'.join(FIXTURE_LEVELS)} (the effective tier, never composites or ad-hoc labels: "
            "off-vocabulary levels fragment the keep/cut sample)"
        )
    outcome = entry.get("outcome", "merged")
    if outcome not in OUTCOMES:
        findings.append(f"{raw_path}:1: [loop-log] outcome `{outcome}` invalid - one of {'|'.join(OUTCOMES)}")
    date = entry.get("date")
    if date is not None and not (isinstance(date, str) and DATE_RE.match(date)):
        findings.append(f"{raw_path}:1: [loop-log] date must be YYYY-MM-DD")
    rounds = entry.get("rounds")
    if rounds is not None and not (isinstance(rounds, int) and rounds >= 0):
        findings.append(f"{raw_path}:1: [loop-log] rounds must be a non-negative integer")
    lenses = entry.get("round_lenses")
    if lenses is not None:
        if not isinstance(lenses, list):
            findings.append(f"{raw_path}:1: [loop-log] round_lenses must be a list (one entry per round)")
        else:
            for index, per_round in enumerate(lenses):
                if not loop_log_audit.is_lens_list(per_round):
                    findings.append(
                        f"{raw_path}:1: [loop-log] round_lenses[{index}] must be a non-empty LIST of "
                        "lens names for that round (`[[\"lens-a\",\"lens-b\"],[\"lens-c\"]]`). A bare "
                        "string is the historical flat-list shape (one lens name per round): "
                        "`set(\"lens-a\")` is a set of characters, so core is structurally 0 and every "
                        "later-round catch is forced into rotated - loop_log_audit.py now excludes "
                        "those rows from the rotation sample as unattributable"
                    )
    intent = entry.get("rotation_intent")
    if intent is not None and intent not in loop_log_audit.ROTATION_INTENTS:
        findings.append(
            f"{raw_path}:1: [loop-log] rotation_intent `{intent}` invalid - one of "
            f"{'|'.join(loop_log_audit.ROTATION_INTENTS)} (it declares whether the later-round lens "
            "change was a deliberate rotation; omit it to let the audit infer from the lens sets)"
        )
    if outcome == "merged":
        for key in ("gate_net_catch", "verdicts"):
            if key not in entry:
                findings.append(f"{raw_path}:1: [loop-log] merged line missing `{key}` "
                                "(terminal outcomes are exempt)")
        for field, require_round in (("catches", True), ("phase7_catches", False)):
            items = entry.get(field)
            if items is None:
                continue
            if not isinstance(items, list):
                findings.append(f"{raw_path}:1: [loop-log] {field} must be a list")
                continue
            for index, catch in enumerate(items):
                check_catch(raw_path, index, catch, findings, field=field, require_round=require_round)


def resolve_head(root: Path, head: str | None) -> str | None:
    if head:
        return head.lower()
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower()


def load_state(root: Path) -> dict | None:
    path = root / review_gate.STATE_NAME
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def is_placeholder(token: str) -> bool:
    inner = token[1:-1]
    if "://" in inner or "@" in inner or inner.lower().startswith("http"):
        return False  # autolinks and mail addresses, not template tokens
    return True


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def scan_file(path: Path, root: Path, head: str, last_round: int | None,
              authored: bool, findings: list[str]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = rel(path, root)
    for lineno, line in enumerate(text.splitlines(), 1):
        if authored:
            for m in PLACEHOLDER_RE.finditer(line):
                if is_placeholder(m.group(0)):
                    findings.append(f"{name}:{lineno}: [placeholder] unreplaced template token {m.group(0)}")
            for m in MARKER_RE.finditer(line):
                findings.append(f"{name}:{lineno}: [placeholder] unfinished-work marker {m.group(1)}")
        for m in HEAD_CLAIM_RE.finditer(line):
            claim = m.group(1).lower()
            if not head.startswith(claim):
                findings.append(
                    f"{name}:{lineno}: [head-sha] current/frozen head claim {claim} "
                    f"does not match actual HEAD {head[:12]}"
                )
        if last_round is not None:
            for m in ROUND_CLAIM_RE.finditer(line):
                claimed = int(m.group(1))
                if claimed <= last_round:
                    findings.append(
                        f"{name}:{lineno}: [round-status] round {claimed} claimed pending "
                        f"but the ledger already recorded round {last_round}"
                    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
                        help="project root holding %s (default: CLAUDE_PROJECT_DIR or cwd)" % review_gate.STATE_NAME)
    parser.add_argument("--head", default=None,
                        help="expected HEAD SHA (default: git rev-parse HEAD in --root)")
    parser.add_argument("--file", dest="files", action="append", default=[],
                        help="orchestrator-authored artifact (PR body draft, manifest); repeatable, gets all checks")
    parser.add_argument("--evidence-dir", default=None,
                        help="override the review dir from %s" % review_gate.STATE_NAME)
    parser.add_argument("--loop-log-entry", default=None,
                        help="pending review-loop-log line (single JSON object in a file) to validate "
                             "before appending")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    head = resolve_head(root, args.head)
    if head is None:
        print(f"evidence_check: cannot resolve HEAD in {root} - not a git repo? pass --head", file=sys.stderr)
        return 2

    state = load_state(root)
    rounds = (state or {}).get("rounds", [])
    last_round = max((rd["n"] for rd in rounds), default=None) if rounds else None
    if last_round is None:
        print("evidence_check: no recorded rounds - round-status check skipped")

    findings: list[str] = []
    if state and state.get("locked"):
        findings.append(
            f"{review_gate.STATE_NAME}:0: [gate-lock] review gate is locked "
            f"({state.get('lockReason')}) - register the persisted retro (record-retro) before "
            "spawning reviewers or posting; proceeding anyway is a recorded skip block, not a default"
        )
    if args.loop_log_entry:
        check_loop_log_entry(args.loop_log_entry, findings)

    authored: list[Path] = []
    for raw in args.files:
        path = Path(raw)
        if not path.is_file():
            findings.append(f"{raw}:0: [input] --file target missing")
        else:
            authored.append(path.resolve())

    evidence_files: list[Path] = []
    ev_dir = args.evidence_dir or (state or {}).get("reviewDir")
    if ev_dir:
        ev_path = (root / ev_dir).resolve()
        if ev_path.is_dir():
            evidence_files = sorted(
                p.resolve() for p in ev_path.rglob("*")
                if p.is_file() and p.suffix in TEXT_SUFFIXES
            )
        else:
            print(f"evidence_check: review dir not present yet ({ev_path}) - skipped")

    authored_set = set(authored)
    scanned = authored + [p for p in evidence_files if p not in authored_set]
    if not scanned and not args.loop_log_entry and not findings:
        print("evidence_check: nothing to check - pass --file and/or ensure the review dir exists", file=sys.stderr)
        return 2

    for path in scanned:
        scan_file(path, root, head, last_round, path in authored_set, findings)

    for line in findings:
        print(line)
    if findings:
        print(f"evidence_check: {len(findings)} finding(s) - fix the bookkeeping before spawning reviewers or posting",
              file=sys.stderr)
        return 2
    checked = len(scanned) + (1 if args.loop_log_entry else 0)
    print(f"evidence_check: clean ({checked} file(s), head {head[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
