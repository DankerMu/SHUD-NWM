#!/usr/bin/env python3
"""loop_log_audit.py - keep/cut decidability audit over the review-loop accountability log.

The log (docs/review-loop-log.jsonl) exists to be consumed, not only appended:
field data showed both consuming repos collecting keep/cut samples well past
the decision threshold with no recorded ADR. This script makes "a decision is
now owed" a mechanical fact instead of a prose expectation. It is tracked
repo governance tooling (#2477), restored from the #2036 version; no
installed workflow step runs it, so it is run on demand
(`uv run python scripts/governance/loop_log_audit.py`). Exit 2 means at least
one DECIDABLE item exists - record the keep/cut or rotation ADR in docs/adr/
(or a one-line recorded deferral with reason).

`--check-entry FILE` validates one pending log line (a single JSON object)
with `loop_log_entry_errors` instead of auditing the log.

Reported:
  DECIDABLE keep-cut     a canonical fixture level has >= --min-sample merged
                         PRs with zero total gate_net_catch: the review loop
                         never caught anything there - decide keep/narrow/cut.
  DECIDABLE lens-rotation the rotation evidence CONTRADICTS the decision
                         already recorded in docs/adr/ (--rotation-decision),
                         or the sample is still below --min-multiround.
                         Consistent evidence prints as NOTE lens-rotation: a
                         settled question must not be re-asked on every
                         closure. --rotation-decision none restores the
                         unconditional "sample reached, decide" behaviour.
  NOTE off-vocabulary    fixture labels outside none|compact|expanded|high|
                         broad-expanded fragment the keep/cut sample (they are
                         excluded from the buckets above).
  NOTE non-compliant   catches missing an integer `round` or a non-empty
        catches          `lens` cannot be attributed to a lens; they are
                         excluded from the rotation figures and reported with
                         their PR numbers instead of vanishing silently.
  NOTE rotation sample   entries dropped from the rotation denominator - no
                         later round added a lens (contraction, not rotation),
                         an unusable round-1 lens set, or a declared
                         `rotation_intent: incidental`. Reported so the
                         denominator shrinks visibly, never silently.
  NOTE terminal outcomes ceiling-split/abandoned/descoped lines - each one
                         obligates an upstream sizing-retro when the issue
                         came from stage-change-pipeline.

Deterministic, stdlib-only. Exit codes: 0 = nothing decidable,
2 = decidable item(s) or unreadable input (attention required).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

DEFAULT_LOG = Path(__file__).resolve().parents[2] / "docs" / "review-loop-log.jsonl"
FIXTURE_LEVELS = ("none", "compact", "expanded", "high", "broad-expanded")
TERMINAL_OUTCOMES = ("ceiling-split", "abandoned", "descoped")
# The outcome vocabulary of a loop-log LINE (not the review-gate memory's
# close outcomes in scripts/review_gate.py - a different record).
OUTCOMES = ("merged", "ceiling-split", "abandoned", "descoped")
ENTRY_REQUIRED_KEYS = ("issue", "pr", "date", "fixture", "rounds")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROTATION_INTENTS = ("deliberate", "incidental")
ROTATION_DECISIONS = ("keep", "cut", "none")

# Phase 7 final review used to be written into `catches` under a pseudo-lens
# name. Those names are by construction absent from round 1, so every such
# catch scored as a rotated-in lens - a round ROLE counted as a lens identity.
# This set is a read-compatibility shim for the lines already in the ledger:
# it is CLOSED and will never grow. New lines record final review in
# `phase7_catches`, and `loop_log_entry_errors` (--check-entry) rejects any
# new catch using one of these names. `prose-truth-class-sweep` is deliberately
# NOT here: it reads as an ordinary sweep lens, so classifying it either way
# would be a guess.
FINAL_REVIEW_LEGACY_LENSES = frozenset({
    "final-review",
    "final-review-rerun",
    "final-review-gap-sweep",
    "final-full-pass",
    "final-gap-sweep",
    "full-diff-final",
    "gap-sweep",
    "phase7-final-and-delta",
})


class Attribution(NamedTuple):
    """Later-round catches of one entry, split three ways plus the unusable."""

    core: int
    rotated: int
    final_review: int
    skipped: int


class RotationSample(NamedTuple):
    """The rotation denominator and every reason an entry was kept out of it."""

    multiround: list[dict]
    entries: list[dict]
    excluded_empty_core: int
    excluded_bad_shape: int
    excluded_subset_only: int
    excluded_incidental: int
    declared_deliberate: int
    declared_incidental: int


def parse_log(path: Path) -> list[dict] | None:
    if not path.is_file():
        print(f"loop_log_audit: log not found: {path}", file=sys.stderr)
        return None
    entries = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"loop_log_audit: {path}:{lineno}: invalid JSON ({exc.msg}) - "
                  "fix the log before auditing", file=sys.stderr)
            return None
        if not isinstance(entry, dict):
            print(f"loop_log_audit: {path}:{lineno}: line is not a JSON object", file=sys.stderr)
            return None
        entries.append(entry)
    return entries


def is_compliant_catch(catch: object) -> bool:
    """A catch is attributable only with a non-negative integer `round`
    (`0` is the fixture-review round; bool is not an integer) and a non-empty
    string `lens`. Same definition as `check_catch` below.
    """
    if not isinstance(catch, dict):
        return False
    value = catch.get("round")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return False
    lens = catch.get("lens")
    return isinstance(lens, str) and bool(lens)


def non_compliant_catches(entry: dict) -> int:
    return sum(1 for catch in entry.get("catches") or [] if not is_compliant_catch(catch))


def is_lens_list(value: object) -> bool:
    """True for one round's lens mix: a non-empty list of non-empty lens names.

    This is the shape rule in ONE place, for the reader and the writer alike
    (`loop_log_entry_errors` uses it). A bare string - the flat
    shape 53 historic lines use, one lens name per round - is NOT a lens list:
    `set("correctness")` is a set of characters, so no lens name can ever
    match it, core is pinned at 0 and every later-round catch is forced into
    `rotated`. An empty list fails for the same reason with an empty core set.
    Either way the record is unattributable, not evidence about rotation - see
    docs/adr/0003-review-lens-rotation-keep.md.
    """
    return (isinstance(value, list) and bool(value)
            and all(isinstance(lens, str) and lens for lens in value))


def round_one_lenses(entry: dict) -> set[str] | None:
    """The round-1 lens set, or None when it cannot be used as a core set.

    `round_lenses` itself may be any JSON shape in a hand-written line, so it
    is type-checked before indexing: the audit reports a malformed record, it
    does not abort the whole run over one bad line.
    """
    lenses = entry.get("round_lenses")
    if not isinstance(lenses, list) or not lenses or not is_lens_list(lenses[0]):
        return None
    return set(lenses[0])


def is_final_review_catch(catch: dict) -> bool:
    """Phase 7 final review is a round role, never a rotated-in lens.

    The lens NAME decides, not the round number: the legacy convention logged
    final review with whatever round index the run had reached, so keying on
    `round` would leave those catches in the core/rotated split.
    """
    return catch["lens"] in FINAL_REVIEW_LEGACY_LENSES


def rotation_attribution(entry: dict) -> Attribution:
    """Later-round catches split into (core, rotated, final_review), plus the
    count of catches skipped as non-compliant.

    A non-compliant catch is never counted anywhere: a missing `lens` must not
    become a free rotated-in credit (it is not in the round-1 lens set, so it
    used to score as rotated), and a missing `round` must not silently default
    to round 1 and disappear. Both are reported instead.

    Final review is counted from both conventions - the legacy pseudo-lens
    names in `catches` and the `phase7_catches` field that superseded them -
    and lands in neither core nor rotated. `phase7_catches` items carry no
    `round` (Phase 7 runs after the numbered rounds), so they are counted as
    objects rather than run through the compliant-catch check.
    """
    core_lenses = round_one_lenses(entry) or set()
    core = rotated = final_review = skipped = 0
    for catch in entry.get("catches") or []:
        if not is_compliant_catch(catch):
            skipped += 1
            continue
        if is_final_review_catch(catch):
            final_review += 1
            continue
        if catch["round"] < 2:
            continue
        if catch["lens"] in core_lenses:
            core += 1
        else:
            rotated += 1
    for catch in entry.get("phase7_catches") or []:
        if isinstance(catch, dict):
            final_review += 1
        else:
            skipped += 1
    return Attribution(core, rotated, final_review, skipped)


def declared_rotation_intent(entry: dict) -> str | None:
    """The entry's `rotation_intent`, or None when absent or off-vocabulary.

    Off-vocabulary values fall back to inference rather than failing the
    audit; `loop_log_entry_errors` (--check-entry) rejects them.
    """
    intent = entry.get("rotation_intent")
    return intent if intent in ROTATION_INTENTS else None


def rotated_in_later_round(entry: dict, core_lenses: set[str]) -> bool:
    """True when some round after the first contributed a lens round 1 lacked.

    A later round that is a SUBSET of round 1 is a contraction, not a
    rotation: its catches can only ever land in core, while the PR still
    occupied a slot in the rotation denominator and diluted the ratio. Later
    rounds that are not lens lists carry no usable lens names and contribute
    nothing here.
    """
    lenses = entry.get("round_lenses")
    for later in (lenses[1:] if isinstance(lenses, list) else []):
        if is_lens_list(later) and set(later) - core_lenses:
            return True
    return False


def rotation_sample(merged: list[dict]) -> RotationSample:
    """The multi-round entries that actually rotated, and why the rest are out.

    Membership is declared when the entry carries `rotation_intent`
    (`deliberate` keeps it even with identical lens sets, `incidental` drops
    it even with differing ones) and inferred from the lens sets otherwise.
    Every exclusion is counted so the denominator is auditable.
    """
    multiround = [e for e in merged if e.get("rounds", 0) >= 2 and e.get("round_lenses")]
    entries: list[dict] = []
    empty_core = bad_shape = subset_only = incidental = 0
    deliberate_count = incidental_count = 0
    for entry in multiround:
        intent = declared_rotation_intent(entry)
        if intent == "deliberate":
            deliberate_count += 1
        elif intent == "incidental":
            incidental_count += 1
        core_lenses = round_one_lenses(entry)
        if core_lenses is None:
            lenses = entry.get("round_lenses")
            if isinstance(lenses, list) and lenses and isinstance(lenses[0], list) and not lenses[0]:
                empty_core += 1
            else:
                bad_shape += 1
            continue
        if intent == "incidental":
            incidental += 1
            continue
        if intent != "deliberate" and not rotated_in_later_round(entry, core_lenses):
            subset_only += 1
            continue
        entries.append(entry)
    return RotationSample(multiround, entries, empty_core, bad_shape, subset_only, incidental,
                          deliberate_count, incidental_count)


def check_catch(index: int, catch: object, findings: list[str],
                field: str = "catches", require_round: bool = True) -> None:
    """One finding per violation of the compliant-catch schema.

    A catch is attributable only when it carries a non-negative integer
    `round` (`0` is the fixture-review round; bool is not an integer) and a
    non-empty string `lens`. Same definition as `is_compliant_catch`.

    `phase7_catches` items reuse this schema minus `round`: Phase 7 runs after
    the numbered rounds, so there is no round to record. A `round` that IS
    present is still validated.

    A `catches` item naming a legacy Phase 7 pseudo-lens is rejected outright.
    Those names are absent from `round_lenses[0]` by construction, so the
    audit scores them as rotated-in lenses; the compatibility shim
    (FINAL_REVIEW_LEGACY_LENSES) exists for the lines already in the ledger
    and must not acquire new members.
    """
    where = f"[loop-log] {field}[{index}]"
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
        elif field == "catches" and lens in FINAL_REVIEW_LEGACY_LENSES:
            findings.append(
                f"{where} lens `{lens}` is a Phase 7 final-review round role, not a cross-review "
                "lens - record final-review findings in `phase7_catches` instead, or the "
                "lens-rotation attribution counts them as rotated-in lenses"
            )


def loop_log_entry_errors(entry: dict) -> list[str]:
    """Every write-time finding for one pending log line (empty = clean).

    Pure: no file IO. Required keys, fixture level must be a single canonical
    token (composites like `expanded/high` and ad-hoc labels like `standard`
    fragment the keep/cut sample), outcome vocabulary, date format,
    `rotation_intent` vocabulary, every `round_lenses[i]` a non-empty LIST of
    lens names, and - on a merged line - the schema of every `catches[i]` /
    `phase7_catches[i]` item (see `check_catch`). Historical rows are read
    through the audit's compatibility shims instead; this is the rule for new
    lines only.
    """
    findings: list[str] = []
    for key in ENTRY_REQUIRED_KEYS:
        if key not in entry:
            findings.append(f"[loop-log] missing required key `{key}`")
    fixture = entry.get("fixture")
    if fixture is not None and fixture not in FIXTURE_LEVELS:
        findings.append(
            f"[loop-log] fixture `{fixture}` is off-vocabulary - use exactly one of "
            f"{'|'.join(FIXTURE_LEVELS)} (the effective tier, never composites or ad-hoc labels: "
            "off-vocabulary levels fragment the keep/cut sample)"
        )
    outcome = entry.get("outcome", "merged")
    if outcome not in OUTCOMES:
        findings.append(f"[loop-log] outcome `{outcome}` invalid - one of {'|'.join(OUTCOMES)}")
    date = entry.get("date")
    if date is not None and not (isinstance(date, str) and DATE_RE.match(date)):
        findings.append("[loop-log] date must be YYYY-MM-DD")
    rounds = entry.get("rounds")
    if rounds is not None and not (isinstance(rounds, int) and rounds >= 0):
        findings.append("[loop-log] rounds must be a non-negative integer")
    lenses = entry.get("round_lenses")
    if lenses is not None:
        if not isinstance(lenses, list):
            findings.append("[loop-log] round_lenses must be a list (one entry per round)")
        else:
            for index, per_round in enumerate(lenses):
                if not is_lens_list(per_round):
                    findings.append(
                        f"[loop-log] round_lenses[{index}] must be a non-empty LIST of "
                        "lens names for that round (`[[\"lens-a\",\"lens-b\"],[\"lens-c\"]]`). A bare "
                        "string is the historical flat-list shape (one lens name per round): "
                        "`set(\"lens-a\")` is a set of characters, so core is structurally 0 and every "
                        "later-round catch is forced into rotated - loop_log_audit.py now excludes "
                        "those rows from the rotation sample as unattributable"
                    )
    intent = entry.get("rotation_intent")
    if intent is not None and intent not in ROTATION_INTENTS:
        findings.append(
            f"[loop-log] rotation_intent `{intent}` invalid - one of "
            f"{'|'.join(ROTATION_INTENTS)} (it declares whether the later-round lens "
            "change was a deliberate rotation; omit it to let the audit infer from the lens sets)"
        )
    if outcome == "merged":
        for key in ("gate_net_catch", "verdicts"):
            if key not in entry:
                findings.append(f"[loop-log] merged line missing `{key}` "
                                "(terminal outcomes are exempt)")
        for field, require_round in (("catches", True), ("phase7_catches", False)):
            items = entry.get(field)
            if items is None:
                continue
            if not isinstance(items, list):
                findings.append(f"[loop-log] {field} must be a list")
                continue
            for index, catch in enumerate(items):
                check_catch(index, catch, findings, field=field, require_round=require_round)
    return findings


def check_entry_file(raw_path: str) -> int:
    """`--check-entry`: validate one pending line held in a file; 2 on findings."""
    path = Path(raw_path)
    if not path.is_file():
        findings = [f"{raw_path}:0: [loop-log] --check-entry target missing"]
    else:
        try:
            entry = json.loads(path.read_text(encoding="utf-8").strip())
        except json.JSONDecodeError as exc:
            findings = [f"{raw_path}:1: [loop-log] not valid JSON ({exc.msg})"]
        else:
            if not isinstance(entry, dict):
                findings = [f"{raw_path}:1: [loop-log] entry must be a single JSON object"]
            else:
                findings = [f"{raw_path}:1: {finding}" for finding in loop_log_entry_errors(entry)]
    for line in findings:
        print(line)
    if findings:
        print(f"loop_log_audit: {len(findings)} finding(s) in the pending line - fix it before appending",
              file=sys.stderr)
        return 2
    print(f"loop_log_audit: pending line clean ({raw_path})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", default=str(DEFAULT_LOG),
                        help="path to review-loop-log.jsonl (default: this repo's docs/review-loop-log.jsonl)")
    parser.add_argument("--check-entry", metavar="FILE", default=None,
                        help="validate one pending log line (a single JSON object in FILE) with "
                             "loop_log_entry_errors instead of auditing --log; exit 2 on findings")
    parser.add_argument("--min-sample", type=int, default=8,
                        help="merged-PR sample per fixture level before keep/cut is decidable (default 8)")
    parser.add_argument("--min-multiround", type=int, default=8,
                        help="merged multi-round PRs with lens attribution before rotation is decidable (default 8)")
    parser.add_argument("--rotation-decision", choices=ROTATION_DECISIONS, default="keep",
                        help="the rotation decision already recorded in docs/adr/ (default keep). The "
                             "rotation line escalates to DECIDABLE only when the evidence contradicts "
                             "it (keep recorded but rotated share below 50%%, or cut recorded but at or "
                             "above 50%%) or the sample is still below --min-multiround; otherwise it is "
                             "a NOTE. This flag exists because the gate had been re-asking a question "
                             "already settled in docs/adr/0003-review-lens-rotation-keep.md on every "
                             "single closure. Use `none` for the old unconditional behaviour.")
    args = parser.parse_args(argv)
    if args.check_entry is not None:
        return check_entry_file(args.check_entry)

    entries = parse_log(Path(args.log))
    if entries is None:
        return 2

    merged = [e for e in entries if e.get("outcome", "merged") == "merged"]
    terminal = [e for e in entries if e.get("outcome", "merged") in TERMINAL_OUTCOMES]
    print(f"loop_log_audit: {len(entries)} line(s) - {len(merged)} merged, {len(terminal)} terminal")

    decidable = 0

    off_vocab: dict[str, int] = {}
    by_level: dict[str, list[dict]] = {}
    for e in merged:
        fixture = e.get("fixture", "<missing>")
        if fixture in FIXTURE_LEVELS:
            by_level.setdefault(fixture, []).append(e)
        else:
            off_vocab[fixture] = off_vocab.get(fixture, 0) + 1

    for level in FIXTURE_LEVELS:
        sample = by_level.get(level, [])
        if not sample:
            continue
        total_catch = sum(e.get("gate_net_catch", 0) for e in sample)
        line = f"fixture {level}: {len(sample)} merged PR(s), total gate_net_catch {total_catch}"
        if len(sample) >= args.min_sample and total_catch == 0:
            decidable += 1
            print(f"DECIDABLE keep-cut: {line} - the loop never caught anything at this level; "
                  "record a keep/narrow/cut ADR (docs/adr/) or a one-line recorded deferral")
        else:
            print(line)

    if off_vocab:
        labels = ", ".join(f"{k}({v})" for k, v in sorted(off_vocab.items()))
        print(f"NOTE off-vocabulary fixture labels excluded from keep/cut buckets: {labels} - "
              "a pending line using one is rejected by --check-entry")

    # Scan every entry, not just the multi-round subset: a line without a
    # `round_lenses` key never reaches the attribution block, so its
    # unattributable catches would otherwise never be seen at all.
    skipped_total = 0
    skipped_prs: list[str] = []
    for e in entries:
        n = non_compliant_catches(e)
        if n:
            skipped_total += n
            skipped_prs.append(str(e.get("pr", "<missing>")))
    if skipped_total:
        print(f"NOTE non-compliant catches skipped: {skipped_total} in {len(skipped_prs)} entry(ies) "
              f"(pr {', '.join(skipped_prs)}) - a catch needs `round` (non-negative integer, 0 = "
              "fixture review) and a non-empty `lens` to be attributable; these are excluded from "
              "the rotation figures")

    sample = rotation_sample(merged)
    if sample.multiround:
        excluded = (sample.excluded_empty_core + sample.excluded_bad_shape
                    + sample.excluded_subset_only + sample.excluded_incidental)
        if excluded:
            print(f"NOTE rotation sample: {excluded} of {len(sample.multiround)} multi-round merged "
                  f"PR(s) excluded from the rotation denominator - no-rotation "
                  f"(later rounds only narrowed round 1)={sample.excluded_subset_only}, "
                  f"unattributable round-1 lens set (empty={sample.excluded_empty_core}, "
                  f"non-list={sample.excluded_bad_shape}), "
                  f"declared incidental={sample.excluded_incidental}")
        declared = sample.declared_deliberate + sample.declared_incidental
        print(f"NOTE rotation_intent: declared on {declared} of {len(sample.multiround)} multi-round "
              f"entry(ies) (deliberate={sample.declared_deliberate}, "
              f"incidental={sample.declared_incidental}) - the rest of the sample is inferred from "
              "the round-1/later lens-set difference")
    if sample.entries:
        core = rotated = final_review = 0
        for e in sample.entries:
            attribution = rotation_attribution(e)
            core += attribution.core
            rotated += attribution.rotated
            final_review += attribution.final_review
        attributed = core + rotated
        share = f"{rotated / attributed:.1%}" if attributed else "n/a"
        line = (f"rotation attribution: {len(sample.entries)} rotating multi-round merged PR(s), "
                f"later-round catches core={core} rotated={rotated} final_review={final_review} "
                f"skipped={skipped_total} (rotated share {share})")
        recorded = args.rotation_decision
        below_min = len(sample.entries) < args.min_multiround
        if recorded == "none":
            reason = "sample reached" if not below_min else None
        elif below_min:
            reason = (f"decision `{recorded}` is recorded on a rotation sample below "
                      f"--min-multiround ({len(sample.entries)} < {args.min_multiround})")
        elif attributed and recorded == "keep" and rotated / attributed < 0.5:
            reason = f"recorded decision is `keep` but the rotated share has fallen below 50% ({share})"
        elif attributed and recorded == "cut" and rotated / attributed >= 0.5:
            reason = f"recorded decision is `cut` but the rotated share is at or above 50% ({share})"
        else:
            reason = None
        if reason is None:
            if recorded == "none":
                print(line)
            else:
                print(f"NOTE lens-rotation: {line} - consistent with the `{recorded}` decision already "
                      "recorded in docs/adr/0003-review-lens-rotation-keep.md; nothing new is owed")
        else:
            decidable += 1
            if recorded == "none":
                print(f"DECIDABLE lens-rotation: {line} - decide keep (catches concentrate in "
                      "rotated-in lenses) or revert to the round-1 mix, and record it in docs/adr/")
            else:
                print(f"DECIDABLE lens-rotation: {line} - {reason}; re-adjudicate and record it "
                      "in docs/adr/")

    if terminal:
        outcomes = {}
        for e in terminal:
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items()))
        print(f"NOTE terminal outcomes: {summary} - each obligates an upstream sizing-retro "
              "(stage-change-pipeline) when the issue came from that pipeline")

    if decidable:
        print(f"loop_log_audit: {decidable} DECIDABLE item(s) - record the ADR or a recorded deferral "
              "in docs/adr/", file=sys.stderr)
        return 2
    print("loop_log_audit: nothing decidable yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
