"""Requirement tests for the review-loop lens-rotation attribution audit.

The audited figure gates a recorded keep/cut ADR (docs/adr/0003-review-lens-
rotation-keep.md), so its arithmetic is a real contract: a PR that only ran a
SUBSET of its round-1 lenses in later rounds did not rotate anything and must
not sit in the denominator, and a Phase 7 final-review catch is a round role,
not a rotated-in lens. Both defects biased the ratio in the same direction, so
nothing downstream would have noticed them by feel.

The module under test lives in the force-added (gitignored but tracked)
`.agents/skills/subagent-workflow/scripts/` asset tree, so it is loaded by
path; a checkout without that asset skips instead of failing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / ".agents" / "skills" / "subagent-workflow" / "scripts"
AUDIT_SCRIPT = SCRIPTS_DIR / "loop_log_audit.py"
EVIDENCE_SCRIPT = SCRIPTS_DIR / "evidence_check.py"
REAL_LOG = REPO_ROOT / "docs" / "review-loop-log.jsonl"


def _load(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        # Module-level skip needs the explicit opt-in, otherwise pytest turns
        # it into a collection ERROR and a checkout without the force-added
        # asset fails the suite instead of skipping it.
        pytest.skip(f"tracked skill asset absent: {path}", allow_module_level=True)
    # evidence_check.py imports its siblings (review_gate, loop_log_audit) the
    # way the installed skill runs it - by script dir on sys.path.
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load(AUDIT_SCRIPT, "loop_log_audit")
evidence = _load(EVIDENCE_SCRIPT, "evidence_check_tracked")


def merged_entry(pr: int, round_lenses: object, catches: list[dict] | None = None, **extra: object) -> dict:
    """A merged log line with the keys the audit reads.

    `gate_net_catch` is non-zero on purpose: a level with zero total catch
    raises the unrelated keep-cut DECIDABLE and would mask the exit codes the
    rotation cases assert.
    """
    entry: dict = {
        "issue": pr - 1,
        "pr": pr,
        "date": "2026-09-04",
        "fixture": "expanded",
        "rounds": len(round_lenses) if isinstance(round_lenses, list) else 2,
        "gate_net_catch": 1,
        "verdicts": {"confirmed": 1, "plausible": 0, "refuted": 0},
        "round_lenses": round_lenses,
        "catches": catches or [],
    }
    entry.update(extra)
    return entry


def write_log(tmp_path: Path, entries: list[dict], name: str = "log.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def catch(round_: int, lens: str) -> dict:
    return {"round": round_, "lens": lens, "class": "correctness", "severity": "P1"}


def test_subset_only_later_round_is_excluded_from_denominator(tmp_path: Path) -> None:
    """Round 2 narrowing to a subset of round 1 is a contraction, not a rotation.

    Its later-round catches were previously credited to `core` while the PR
    still occupied a slot in the denominator.
    """
    entry = merged_entry(
        101,
        [["correctness", "state-semantics"], ["correctness"]],
        [catch(1, "correctness"), catch(2, "correctness")],
    )
    sample = audit.rotation_sample([entry])

    assert sample.entries == []
    assert sample.excluded_subset_only == 1
    attribution = audit.rotation_attribution(entry)
    assert (attribution.core, attribution.rotated, attribution.final_review) == (1, 0, 0)

    log = write_log(tmp_path, [entry])
    entries = audit.parse_log(log)
    assert audit.rotation_sample(entries).entries == []


def test_added_lens_is_sampled_and_its_catch_is_rotated(tmp_path: Path) -> None:
    entry = merged_entry(
        102,
        [["correctness"], ["correctness", "blast-radius"]],
        [catch(1, "correctness"), catch(2, "blast-radius")],
    )
    sample = audit.rotation_sample([entry])

    assert [e["pr"] for e in sample.entries] == [102]
    attribution = audit.rotation_attribution(entry)
    assert (attribution.core, attribution.rotated, attribution.final_review) == (0, 1, 0)


def test_later_round_catch_on_a_round_one_lens_is_core() -> None:
    entry = merged_entry(
        103,
        [["correctness"], ["correctness", "blast-radius"]],
        [catch(2, "correctness")],
    )
    attribution = audit.rotation_attribution(entry)

    assert (attribution.core, attribution.rotated, attribution.final_review) == (1, 0, 0)


@pytest.mark.parametrize("lens", ["final-review", "gap-sweep", "phase7-final-and-delta"])
def test_legacy_pseudo_lens_catch_is_final_review_not_rotated(lens: str) -> None:
    """Phase 7 under the old convention wrote itself into `catches`.

    Those names are by construction absent from round 1, so every one of them
    used to score as a rotated-in lens.
    """
    assert lens in audit.FINAL_REVIEW_LEGACY_LENSES
    entry = merged_entry(
        104,
        [["correctness"], ["correctness", "blast-radius"], [lens]],
        [catch(3, lens)],
    )
    attribution = audit.rotation_attribution(entry)

    assert (attribution.core, attribution.rotated, attribution.final_review) == (0, 0, 1)


def test_phase7_catches_field_is_counted_as_final_review() -> None:
    entry = merged_entry(
        105,
        [["correctness"], ["correctness", "blast-radius"]],
        [catch(2, "blast-radius")],
        phase7_catches=[
            {"lens": "review-final", "class": "doc-drift", "severity": "P3"},
            {"lens": "review-final", "class": "doc-drift", "severity": "P3"},
        ],
    )
    attribution = audit.rotation_attribution(entry)

    assert (attribution.core, attribution.rotated, attribution.final_review) == (0, 1, 2)


def test_empty_round_one_lens_set_is_excluded_and_reported(tmp_path: Path, capsys) -> None:
    """With an empty core set every catch scores rotated - unattributable."""
    entry = merged_entry(106, [[], ["correctness"]], [catch(2, "correctness")])
    sample = audit.rotation_sample([entry])

    assert sample.entries == []
    assert sample.excluded_empty_core == 1

    log = write_log(tmp_path, [entry])
    audit.main(["--log", str(log), "--min-multiround", "1"])
    out = capsys.readouterr().out
    assert "empty=1" in out


def test_non_list_round_one_lens_set_is_excluded_and_reported(tmp_path: Path, capsys) -> None:
    """`set("correctness")` is a set of characters, so core is pinned at 0.

    Historic lines (PR 1239-1388) recorded `round_lenses` as a flat list of
    strings; treating those as a lens set is the same unattributable shape as
    an empty one, not evidence about rotation.
    """
    entry = merged_entry(107, ["correctness", "blast-radius"], [catch(2, "blast-radius")])
    sample = audit.rotation_sample([entry])

    assert sample.entries == []
    assert sample.excluded_bad_shape == 1

    log = write_log(tmp_path, [entry])
    audit.main(["--log", str(log), "--min-multiround", "1"])
    out = capsys.readouterr().out
    assert "non-list=1" in out


def test_malformed_round_lenses_does_not_abort_the_audit(tmp_path: Path, capsys) -> None:
    """One hand-written bad line must not take the whole run down."""
    entry = merged_entry(110, {"round1": ["correctness"]}, [catch(2, "correctness")])
    sample = audit.rotation_sample([entry])

    assert sample.entries == []
    assert sample.excluded_bad_shape == 1

    log = write_log(tmp_path, [entry])
    assert audit.main(["--log", str(log), "--min-multiround", "1"]) == 0
    assert "non-list=1" in capsys.readouterr().out


def test_rotation_intent_incidental_excludes_a_qualifying_pr() -> None:
    entry = merged_entry(
        108,
        [["correctness"], ["correctness", "blast-radius"]],
        [catch(2, "blast-radius")],
        rotation_intent="incidental",
    )
    sample = audit.rotation_sample([entry])

    assert sample.entries == []
    assert sample.excluded_incidental == 1
    assert sample.declared_incidental == 1


def test_rotation_intent_deliberate_overrides_the_set_difference_inference() -> None:
    entry = merged_entry(
        109,
        [["correctness"], ["correctness"]],
        [catch(2, "correctness")],
        rotation_intent="deliberate",
    )
    sample = audit.rotation_sample([entry])

    assert [e["pr"] for e in sample.entries] == [109]
    assert sample.declared_deliberate == 1
    assert sample.excluded_subset_only == 0


def _rotating(pr: int, later_lens: str, catches: list[dict]) -> dict:
    return merged_entry(pr, [["correctness"], ["correctness", later_lens]], catches)


def test_recorded_keep_decision_prints_note_and_exits_zero_when_evidence_agrees(
    tmp_path: Path, capsys
) -> None:
    entries = [
        _rotating(201, "blast-radius", [catch(2, "blast-radius"), catch(2, "blast-radius")]),
        _rotating(202, "oracle-integrity", [catch(2, "oracle-integrity"), catch(2, "correctness")]),
    ]
    log = write_log(tmp_path, entries)

    code = audit.main(["--log", str(log), "--min-multiround", "1", "--rotation-decision", "keep"])
    out = capsys.readouterr().out

    assert code == 0
    assert "NOTE lens-rotation" in out
    assert "DECIDABLE lens-rotation" not in out
    assert "core=1 rotated=3" in out


def test_recorded_keep_decision_escalates_when_rotated_share_falls_below_half(
    tmp_path: Path, capsys
) -> None:
    entries = [
        _rotating(203, "blast-radius", [catch(2, "correctness"), catch(2, "correctness")]),
        _rotating(204, "oracle-integrity", [catch(2, "correctness"), catch(2, "oracle-integrity")]),
    ]
    log = write_log(tmp_path, entries)

    code = audit.main(["--log", str(log), "--min-multiround", "1", "--rotation-decision", "keep"])
    captured = capsys.readouterr()

    assert code == 2
    assert "DECIDABLE lens-rotation" in captured.out
    assert "core=3 rotated=1" in captured.out


def test_recorded_cut_decision_escalates_when_rotation_still_pays(tmp_path: Path, capsys) -> None:
    entries = [_rotating(205, "blast-radius", [catch(2, "blast-radius")])]
    log = write_log(tmp_path, entries)

    code = audit.main(["--log", str(log), "--min-multiround", "1", "--rotation-decision", "cut"])
    out = capsys.readouterr().out

    assert code == 2
    assert "DECIDABLE lens-rotation" in out


def test_recorded_decision_escalates_while_the_sample_is_below_the_threshold(
    tmp_path: Path, capsys
) -> None:
    entries = [_rotating(206, "blast-radius", [catch(2, "blast-radius")])]
    log = write_log(tmp_path, entries)

    code = audit.main(["--log", str(log), "--min-multiround", "8", "--rotation-decision", "keep"])
    out = capsys.readouterr().out

    assert code == 2
    assert "DECIDABLE lens-rotation" in out


def test_rotation_decision_none_reproduces_the_always_decidable_behaviour(
    tmp_path: Path, capsys
) -> None:
    entries = [_rotating(207, "blast-radius", [catch(2, "blast-radius")])]
    log = write_log(tmp_path, entries)

    decidable = audit.main(["--log", str(log), "--min-multiround", "1", "--rotation-decision", "none"])
    out = capsys.readouterr().out
    assert decidable == 2
    assert "DECIDABLE lens-rotation" in out

    below = audit.main(["--log", str(log), "--min-multiround", "8", "--rotation-decision", "none"])
    out = capsys.readouterr().out
    assert below == 0
    assert "DECIDABLE lens-rotation" not in out


def test_default_rotation_decision_is_the_recorded_keep(tmp_path: Path, capsys) -> None:
    """Without the flag the audit must not re-ask the settled question."""
    entries = [_rotating(208, "blast-radius", [catch(2, "blast-radius")])]
    log = write_log(tmp_path, entries)

    code = audit.main(["--log", str(log), "--min-multiround", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "NOTE lens-rotation" in out


def test_real_ledger_still_audits_and_the_buckets_account_for_every_counted_catch() -> None:
    """A ledger edit that breaks attribution must fail here, not in an ADR.

    The absolute counts are deliberately not pinned - the ledger grows on
    every merge. What is pinned is that the denominator partitions (every
    multi-round entry is either sampled or counted under exactly one
    exclusion) and that the three buckets account for every catch the audit
    was willing to attribute.
    """
    entries = audit.parse_log(REAL_LOG)
    assert entries is not None

    merged = [e for e in entries if e.get("outcome", "merged") == "merged"]
    sample = audit.rotation_sample(merged)
    assert sample.entries

    # Independent recomputation of the multi-round population, then the
    # partition: nothing may be dropped without landing in an exclusion count.
    multiround = [e for e in merged if e.get("rounds", 0) >= 2 and e.get("round_lenses")]
    assert sample.multiround == multiround
    assert len(sample.entries) + (
        sample.excluded_empty_core
        + sample.excluded_bad_shape
        + sample.excluded_subset_only
        + sample.excluded_incidental
    ) == len(multiround)

    core = rotated = final_review = 0
    for entry in sample.entries:
        attribution = audit.rotation_attribution(entry)
        core += attribution.core
        rotated += attribution.rotated
        final_review += attribution.final_review

    expected = 0
    for entry in sample.entries:
        for item in entry.get("catches") or []:
            if not audit.is_compliant_catch(item):
                continue
            if item["lens"] in audit.FINAL_REVIEW_LEGACY_LENSES or item["round"] >= 2:
                expected += 1
        expected += sum(1 for item in entry.get("phase7_catches") or [] if isinstance(item, dict))

    assert core + rotated + final_review == expected
    assert rotated > 0


def check_entry(tmp_path: Path, entry: dict) -> list[str]:
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(entry), encoding="utf-8")
    findings: list[str] = []
    evidence.check_loop_log_entry(str(path), findings)
    return findings


def pending_entry(**extra: object) -> dict:
    entry = merged_entry(300, [["correctness"], ["correctness", "blast-radius"]],
                         [catch(2, "blast-radius")])
    entry["outcome"] = "merged"
    entry.update(extra)
    return entry


def test_pending_entry_with_the_new_convention_is_accepted(tmp_path: Path) -> None:
    entry = pending_entry(
        rotation_intent="deliberate",
        phase7_catches=[{"lens": "review-final", "class": "doc-drift", "severity": "P3"}],
    )
    assert check_entry(tmp_path, entry) == []


@pytest.mark.parametrize("lens", ["final-review", "full-diff-final"])
def test_new_entry_using_a_legacy_pseudo_lens_in_catches_is_rejected(tmp_path: Path, lens: str) -> None:
    """The shim in the audit is read-compatibility only; writers get one convention."""
    findings = check_entry(tmp_path, pending_entry(catches=[catch(3, lens)]))

    assert len(findings) == 1
    assert "phase7_catches" in findings[0]
    assert lens in findings[0]


def test_off_vocabulary_rotation_intent_is_rejected(tmp_path: Path) -> None:
    findings = check_entry(tmp_path, pending_entry(rotation_intent="maybe"))

    assert len(findings) == 1
    assert "rotation_intent" in findings[0]


def test_phase7_catch_needs_a_lens_but_not_a_round(tmp_path: Path) -> None:
    assert check_entry(tmp_path, pending_entry(phase7_catches=[{"lens": "review-final"}])) == []

    findings = check_entry(tmp_path, pending_entry(phase7_catches=[{"class": "doc-drift"}]))
    assert len(findings) == 1
    assert "phase7_catches[0] missing `lens`" in findings[0]


def test_new_entry_with_flat_string_round_lenses_is_rejected(tmp_path: Path) -> None:
    """The flat shape (one lens NAME per round) is what poisoned the figure.

    Reader-side tolerance without writer-side rejection is the same
    two-conventions failure the legacy pseudo-lens names caused.
    """
    entry = pending_entry(round_lenses=["binding-soundness", "final-review"])
    findings = check_entry(tmp_path, entry)

    assert len(findings) == 2
    assert "round_lenses[0] must be a non-empty LIST" in findings[0]
    assert "unattributable" in findings[0]


def test_nested_round_lenses_are_accepted(tmp_path: Path) -> None:
    entry = pending_entry(round_lenses=[["correctness", "test-oracle-honesty"], ["blast-radius"]])

    assert check_entry(tmp_path, entry) == []


@pytest.mark.parametrize("lenses", ["correctness", {"round1": ["correctness"]}, [["correctness"], []]])
def test_malformed_round_lenses_shapes_are_rejected(tmp_path: Path, lenses: object) -> None:
    findings = check_entry(tmp_path, pending_entry(round_lenses=lenses))

    assert findings
    assert all("round_lenses" in f for f in findings)


def test_flat_string_round_lenses_fails_the_cli(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(pending_entry(round_lenses=["correctness", "final-review"])),
                    encoding="utf-8")

    code = evidence.main(["--root", str(REPO_ROOT), "--loop-log-entry", str(path)])

    assert code == 2


def test_audit_tolerates_the_historical_flat_rows_under_the_unattributable_note(capsys) -> None:
    """History stays readable: the audit counts the flat rows, never crashes.

    The count is recomputed here straight off the ledger rather than taken
    from the audit, so a shape regression on either side shows up as a
    mismatch.
    """
    entries = audit.parse_log(REAL_LOG)
    assert entries is not None
    merged = [e for e in entries if e.get("outcome", "merged") == "merged"]
    flat = [e for e in merged
            if e.get("rounds", 0) >= 2 and e.get("round_lenses")
            and not isinstance(e["round_lenses"][0], list)]
    assert flat, "the reader-side tolerance would be dead code"

    sample = audit.rotation_sample(merged)
    assert sample.excluded_bad_shape == len(flat)
    assert all(e not in sample.entries for e in flat)

    # The exit code is NOT pinned: it legitimately flips to 2 the day the
    # rotated share crosses the recorded decision. Seeing the NOTE proves the
    # run reached the rotation block on the real ledger.
    audit.main(["--log", str(REAL_LOG)])
    assert f"non-list={len(flat)}" in capsys.readouterr().out


def test_historical_ledger_lines_are_not_retroactively_broken(tmp_path: Path) -> None:
    """The write-time rejection must not turn the existing ledger into findings.

    Only `--loop-log-entry` (a pending line) is validated; the audit reads the
    committed rows through the compatibility shim instead.
    """
    entries = audit.parse_log(REAL_LOG)
    assert entries is not None
    legacy_rows = [
        e for e in entries
        if any(isinstance(c, dict) and c.get("lens") in audit.FINAL_REVIEW_LEGACY_LENSES
               for c in e.get("catches") or [])
    ]
    assert legacy_rows, "the compatibility shim would be dead code"

    audited = audit.rotation_sample([e for e in entries if e.get("outcome", "merged") == "merged"])
    assert audited.entries
