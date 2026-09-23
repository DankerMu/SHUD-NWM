"""Structural guard over the committed review-gate issue memory (#2261).

`.review-gate-issues.json` is the cross-PR round-ceiling memory: it is the only
record of "this issue already burned a review round ceiling, so a successor PR
must go through a human decision". It is committed, and it has twice been
hand-merged across sessions (`e78cf98a`, `66ea7788`), which is how two bare
top-level keys ended up beside the canonical `issues` map. The escalation path
reads `history["issues"]` only, so anything written outside that map is silently
ignored — a corrupt memory looks exactly like a clean one at read time.

This guard parses the file with stdlib json and stays independent of the
writer's loader, so it reports every violation at once instead of stopping at
the first. The outcome vocabulary is not copied: it is imported from the
tracked writer `scripts/review_gate.py`, the single authority for `OUTCOMES`
(#2261). That module is tracked, so the import runs in every checkout and in
CI. The retired install-managed writer could not be imported, which is why an
earlier version of this guard kept a hand-synced copy.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import review_gate

REVIEW_GATE_ISSUE_MEMORY_PATH = Path(__file__).resolve().parents[1] / ".review-gate-issues.json"

# Source: `OUTCOMES` in the tracked writer scripts/review_gate.py.
OUTCOMES: frozenset[str] = frozenset(review_gate.OUTCOMES)

# The remedy names the tracked writer's real invocation; the test below pins
# that the subcommand and its --outcome choices exist in the CLI's parser.
RECORD_COMMAND = "uv run python scripts/review_gate.py record --issue N --pr P --rounds R --outcome"
RECORD_REMEDY = (
    f"Remedy when recording a close: `{RECORD_COMMAND} <{'|'.join(review_gate.OUTCOMES)}>` "
    "(--outcome is required and choice-restricted, so the tracked writer cannot emit this value)."
)

# The per-issue record shape written by `scripts/review_gate.py record`: every entry carries
# all three fields, and each has a fixed type.
ENTRY_FIELD_TYPES: tuple[tuple[str, type, str], ...] = (
    ("ceilingPrs", list, "list"),
    ("gateEntries", int, "int"),
    ("closed", list, "list"),
)


def review_gate_issue_memory_violations(history: Any) -> list[str]:
    """Return every structural violation in a parsed `.review-gate-issues.json`.

    A pure function over the parsed object: it never reads the filesystem and
    never raises on malformed input, so mutated copies can be fed to it
    directly. An empty list means the memory is well-formed.
    """
    violations: list[str] = []

    if not isinstance(history, dict):
        return [f"top level must be a JSON object, got {type(history).__name__}"]

    extra_keys = sorted(key for key in history if key != "issues")
    if extra_keys:
        violations.append(
            f"top-level keys must be exactly {{'issues'}}, found extra {extra_keys}; "
            "the escalation path reads history['issues'] only, so a bare top-level key is "
            "silently ignored — fold it into the issues map by hand and delete the bare copy"
        )
    if "issues" not in history:
        violations.append("top-level 'issues' map is missing")
        return violations

    issues = history["issues"]
    if not isinstance(issues, dict):
        return [*violations, f"history['issues'] must be a JSON object, got {type(issues).__name__}"]

    for issue in sorted(issues, key=lambda key: (len(key), key)):
        entry = issues[issue]
        if not isinstance(entry, dict):
            violations.append(f"issues[{issue}] must be a JSON object, got {type(entry).__name__}")
            continue

        for field, expected_type, type_name in ENTRY_FIELD_TYPES:
            if field not in entry:
                violations.append(f"issues[{issue}] is missing required field {field!r} ({type_name})")
                continue
            value = entry[field]
            # `type(...) is` on purpose for gateEntries: bool is a subclass of
            # int, and `True` is not a gate-entry count.
            if type(value) is not expected_type:
                violations.append(
                    f"issues[{issue}].{field} must be {type_name}, got {type(value).__name__}"
                )

        closed = entry.get("closed")
        if not isinstance(closed, list):
            continue
        for index, record in enumerate(closed):
            if not isinstance(record, dict):
                violations.append(
                    f"issues[{issue}].closed[{index}] must be a JSON object, got {type(record).__name__}"
                )
                continue
            if "outcome" not in record:
                violations.append(f"issues[{issue}].closed[{index}] is missing 'outcome'")
                continue
            outcome = record["outcome"]
            pr = record.get("pr", "<unknown>")
            hand_fix = (
                f"Remedy for this already-written record (issue {issue} / PR {pr}): the tracked "
                "writer refuses to load a memory holding it, so set this field by hand to the PR's "
                f"real outcome (`gh pr view {pr} --json state`) and commit .review-gate-issues.json."
            )
            # Type first: a JSON list/object outcome is unhashable, so the
            # membership test below would raise instead of reporting.
            if not isinstance(outcome, str):
                violations.append(
                    f"issues[{issue}].closed[{index}].outcome must be a string from "
                    f"{sorted(OUTCOMES)}, got {type(outcome).__name__} {outcome!r}. {hand_fix}"
                )
                continue
            if outcome not in OUTCOMES:
                # The cmd_close cause is only true of the literal fallback value
                # "closed"; any other string did not come from that path.
                cause = (
                    "Cause: the retired untracked review_gate.py's cmd_close "
                    'wrote `args.outcome or "closed"`, so a close run that omitted --outcome '
                    "persisted a value outside its own vocabulary. "
                    if outcome == "closed"
                    else ""
                )
                violations.append(
                    f"issues[{issue}].closed[{index}].outcome={outcome!r} is outside the "
                    f"OUTCOMES vocabulary {sorted(OUTCOMES)}. {cause}"
                    f"{RECORD_REMEDY} "
                    f"{hand_fix}"
                )

    return violations


def load_review_gate_issue_memory() -> Any:
    return json.loads(REVIEW_GATE_ISSUE_MEMORY_PATH.read_text(encoding="utf-8"))


def test_committed_review_gate_issue_memory_is_structurally_clean() -> None:
    # The whole point of the guard: the tracked file itself must satisfy it.
    violations = review_gate_issue_memory_violations(load_review_gate_issue_memory())

    assert not violations, "\n".join(["committed .review-gate-issues.json is corrupt:", *violations])


def test_committed_review_gate_issue_memory_has_only_the_issues_key() -> None:
    # (a) stated directly against the file, so the top-level shape is pinned
    # even if the violation-string wording changes.
    history = load_review_gate_issue_memory()

    assert set(history) == {"issues"}


def test_committed_review_gate_issue_memory_outcomes_are_in_vocabulary() -> None:
    # (b) stated directly: the only outcomes on disk are vocabulary members.
    # This is the assertion the 13 `outcome: "closed"` records (#2261) failed.
    history = load_review_gate_issue_memory()

    observed = {record["outcome"] for entry in history["issues"].values() for record in entry["closed"]}
    assert observed <= OUTCOMES, f"outcomes outside the vocabulary: {sorted(observed - OUTCOMES)}"


def test_committed_review_gate_issue_memory_entries_are_complete() -> None:
    # (c) stated directly: every entry carries all three fields at the right type.
    history = load_review_gate_issue_memory()

    for issue, entry in history["issues"].items():
        assert set(entry) >= {"ceilingPrs", "gateEntries", "closed"}, issue
        assert isinstance(entry["ceilingPrs"], list), issue
        assert type(entry["gateEntries"]) is int, issue
        assert isinstance(entry["closed"], list), issue


def test_mutant_bare_top_level_key_is_a_violation() -> None:
    # The real regression shape: a hand-merged bare key beside `issues`, which
    # the escalation path never reads (#2261, commits e78cf98a / 66ea7788).
    history = copy.deepcopy(load_review_gate_issue_memory())
    history["1660"] = {"ceilingPrs": [], "closed": [], "gateEntries": 1}

    violations = review_gate_issue_memory_violations(history)

    assert violations, "a bare top-level key must be a violation"
    assert any("['1660']" in violation for violation in violations), violations


def test_mutant_out_of_vocabulary_outcome_names_cause_and_remedy() -> None:
    # G-3b: the failure text must hand the reader the historical cause and the
    # tracked writer's real `record` invocation directly.
    history = copy.deepcopy(load_review_gate_issue_memory())
    history["issues"]["1736"]["closed"][0]["outcome"] = "closed"

    violations = review_gate_issue_memory_violations(history)

    assert violations, "an out-of-vocabulary outcome must be a violation"
    message = "\n".join(violations)
    assert "issues[1736].closed[0].outcome='closed'" in message, message
    assert "cmd_close" in message, message
    assert RECORD_COMMAND in message, message
    assert "issue 1736 / PR 1751" in message, message


def test_mutant_entry_missing_ceiling_prs_is_a_violation() -> None:
    # A truncated entry still answers `gateEntries`, so nothing downstream
    # notices that the ceiling list — the field escalation actually reads — is
    # gone.
    history = copy.deepcopy(load_review_gate_issue_memory())
    del history["issues"]["1660"]["ceilingPrs"]

    violations = review_gate_issue_memory_violations(history)

    assert violations, "a missing ceilingPrs must be a violation"
    assert any("missing required field 'ceilingPrs'" in violation for violation in violations), violations


@pytest.mark.parametrize(
    "gate_entries",
    [pytest.param(True, id="bool-true"), pytest.param("1", id="string")],
)
def test_gate_entries_must_be_a_real_int(gate_entries: object) -> None:
    # bool is an int subclass, so `isinstance` would let `True` through as a
    # gate-entry count; the guard uses an exact type check.
    history = copy.deepcopy(load_review_gate_issue_memory())
    history["issues"]["1660"]["gateEntries"] = gate_entries

    violations = review_gate_issue_memory_violations(history)

    assert any("gateEntries must be int" in violation for violation in violations), violations


def test_guard_tolerates_malformed_input_without_raising() -> None:
    # The guard is fed hand-edited JSON; it must report, not explode.
    assert review_gate_issue_memory_violations([]) == ["top level must be a JSON object, got list"]
    assert review_gate_issue_memory_violations({"issues": []}) == [
        "history['issues'] must be a JSON object, got list"
    ]
    assert review_gate_issue_memory_violations({}) == ["top-level 'issues' map is missing"]
    # JSON list/object outcomes are unhashable: a bare vocabulary membership
    # test on them raises TypeError instead of reporting.
    for outcome, type_name in ((["merged"], "list"), ({"a": 1}, "dict")):
        history = {"issues": {"1": {"ceilingPrs": [], "gateEntries": 1, "closed": [{"pr": 7, "outcome": outcome}]}}}
        violations = review_gate_issue_memory_violations(history)
        assert isinstance(violations, list) and len(violations) == 1, violations
        assert f"outcome must be a string from {sorted(OUTCOMES)}, got {type_name} {outcome!r}" in violations[0]


@pytest.mark.parametrize(
    ("outcome", "type_name"),
    [
        pytest.param(["merged"], "list", id="list"),
        pytest.param({"a": 1}, "dict", id="dict"),
        pytest.param(3, "int", id="int"),
        pytest.param(None, "NoneType", id="null"),
    ],
)
def test_guard_reports_non_string_outcome_without_raising(outcome: object, type_name: str) -> None:
    # A list/object outcome is unhashable; a vocabulary membership test on it
    # would raise TypeError instead of reporting. A non-string outcome also did
    # not come from cmd_close's `"closed"` fallback, so that cause must not be
    # claimed for it.
    history = {"issues": {"1": {"ceilingPrs": [], "gateEntries": 1, "closed": [{"pr": 7, "outcome": outcome}]}}}

    violations = review_gate_issue_memory_violations(history)

    assert len(violations) == 1, violations
    assert f"issues[1].closed[0].outcome must be a string from {sorted(OUTCOMES)}, got {type_name} {outcome!r}" in (
        violations[0]
    ), violations
    assert "cmd_close" not in violations[0], violations


def test_out_of_vocabulary_string_other_than_closed_omits_cmd_close_cause() -> None:
    # Only the literal "closed" is produced by cmd_close's fallback; attributing
    # any other string to it would misdescribe the input.
    history = {"issues": {"1": {"ceilingPrs": [], "gateEntries": 1, "closed": [{"pr": 7, "outcome": "merge"}]}}}

    violations = review_gate_issue_memory_violations(history)

    assert len(violations) == 1, violations
    assert "issues[1].closed[0].outcome='merge' is outside the OUTCOMES vocabulary" in violations[0], violations
    assert "cmd_close" not in violations[0], violations
    assert RECORD_COMMAND in violations[0], violations
    assert "issue 1 / PR 7" in violations[0], violations


def test_remedy_names_a_command_the_tracked_cli_accepts() -> None:
    # The remedy text must point at a subcommand that exists, with the same
    # --outcome vocabulary the guard enforces.
    parser = review_gate.build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert RECORD_COMMAND.split(" record ")[0] == "uv run python scripts/review_gate.py"
    assert "record" in subparsers.choices
    record = subparsers.choices["record"]
    options = {opt: action for action in record._actions for opt in action.option_strings}
    for flag in ("--issue", "--pr", "--rounds", "--outcome"):
        assert f" {flag} " in f" {RECORD_COMMAND} ", flag
        assert flag in options, flag
    assert tuple(options["--outcome"].choices) == review_gate.OUTCOMES
    assert options["--outcome"].required
