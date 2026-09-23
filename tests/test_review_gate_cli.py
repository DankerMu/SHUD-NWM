"""Requirement tests for the tracked round-ceiling memory CLI (#2261).

`scripts/review_gate.py` is the only writer of `.review-gate-issues.json` and
the cross-PR escalation read. Every case drives the public CLI (`main(argv)`,
plus one real subprocess run of the script path the instructions document)
against a memory file in `tmp_path`; only the round-trip case reads the
committed file, and it never writes it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import review_gate

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_MEMORY = REPO_ROOT / ".review-gate-issues.json"


def write_memory(root: Path, history: object) -> Path:
    path = root / ".review-gate-issues.json"
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_memory(root: Path) -> dict:
    return json.loads((root / ".review-gate-issues.json").read_text(encoding="utf-8"))


def run(root: Path, *argv: str) -> int:
    return review_gate.main(["--root", str(root), *argv])


def entry(*, ceiling: list[int] | None = None, gate_entries: int = 0, closed: list[dict] | None = None) -> dict:
    return {"ceilingPrs": ceiling or [], "gateEntries": gate_entries, "closed": closed or []}


def test_outcomes_vocabulary_is_the_four_close_values() -> None:
    assert review_gate.OUTCOMES == ("merged", "superseded-by-split", "abandoned", "descoped")


def test_unambiguous_bare_key_is_folded_into_issues(tmp_path: Path, capsys) -> None:
    # One bare key absent from `issues`, one identical to its `issues` copy:
    # both fold, the file ends with `issues` only, and the fold is announced.
    same = entry(closed=[{"pr": 1751, "outcome": "merged", "rounds": 2}])
    write_memory(tmp_path, {
        "issues": {"1736": same},
        "1736": same,
        "1800": entry(closed=[{"pr": 1810, "outcome": "merged", "rounds": 1}]),
    })

    assert run(tmp_path, "record", "--issue", "1900", "--pr", "1910", "--rounds", "1", "--outcome", "merged") == 0

    history = read_memory(tmp_path)
    assert list(history) == ["issues"]
    assert history["issues"] == {
        "1736": same,
        "1800": entry(closed=[{"pr": 1810, "outcome": "merged", "rounds": 1}]),
        "1900": entry(closed=[{"pr": 1910, "outcome": "merged", "rounds": 1}]),
    }
    err = capsys.readouterr().err
    assert "folded bare top-level key '1736'" in err
    assert "folded bare top-level key '1800'" in err


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(
            ("record", "--issue", "1660", "--pr", "1700", "--rounds", "1", "--outcome", "merged"), id="record"
        ),
        pytest.param(("check", "--issue", "1660"), id="check"),
    ],
)
def test_conflicting_bare_key_refuses_every_subcommand_and_prints_both_records(
    tmp_path: Path, capsys, argv: tuple[str, ...]
) -> None:
    # The real pre-#2469 shape (`d1e86d70a^`): bare `1660` with gateEntries 1
    # beside issues["1660"] with gateEntries 0 - no safe automatic winner.
    closed = [{"outcome": "merged", "pr": 1696, "rounds": 2}]
    path = write_memory(tmp_path, {
        "issues": {"1660": {"ceilingPrs": [], "closed": closed, "gateEntries": 0}},
        "1660": {"ceilingPrs": [], "closed": closed, "gateEntries": 1},
    })
    before = path.read_bytes()

    assert run(tmp_path, *argv) == 1

    err = capsys.readouterr().err
    assert "bare top-level key '1660' conflicts with issues['1660']" in err
    assert '"gateEntries": 1' in err and '"gateEntries": 0' in err
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "outcome_args",
    [pytest.param(("--outcome", "closed"), id="closed"), pytest.param((), id="missing")],
)
def test_out_of_vocabulary_or_missing_outcome_is_rejected_by_argparse(
    tmp_path: Path, capsys, outcome_args: tuple[str, ...]
) -> None:
    path = write_memory(tmp_path, {"issues": {"7": entry()}})
    before = path.read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        run(tmp_path, "record", "--issue", "7", "--pr", "8", "--rounds", "1", *outcome_args)

    assert excinfo.value.code == 2
    assert "--outcome" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_record_is_idempotent_per_issue_and_pr_and_replaces_a_differing_close(tmp_path: Path) -> None:
    write_memory(tmp_path, {"issues": {}})
    argv = ("record", "--issue", "5", "--issue", "6", "--pr", "9", "--rounds", "2", "--outcome", "merged")

    assert run(tmp_path, *argv) == 0
    first = (tmp_path / ".review-gate-issues.json").read_bytes()
    assert run(tmp_path, *argv) == 0
    assert (tmp_path / ".review-gate-issues.json").read_bytes() == first
    assert read_memory(tmp_path)["issues"]["5"] == entry(closed=[{"pr": 9, "outcome": "merged", "rounds": 2}])

    assert run(tmp_path, "record", "--issue", "5", "--pr", "9", "--rounds", "3", "--outcome", "descoped") == 0
    issues = read_memory(tmp_path)["issues"]
    assert issues["5"]["closed"] == [{"pr": 9, "outcome": "descoped", "rounds": 3}]
    assert issues["6"]["closed"] == [{"pr": 9, "outcome": "merged", "rounds": 2}]


def test_record_never_touches_gate_entries_and_new_entries_start_at_zero(tmp_path: Path) -> None:
    prior = entry(gate_entries=1, closed=[{"pr": 1696, "outcome": "merged", "rounds": 2}])
    write_memory(tmp_path, {"issues": {"1660": prior}})

    assert run(tmp_path, "record", "--issue", "1660", "--issue", "1661", "--pr", "1700", "--rounds", "1",
               "--outcome", "merged", "--ceiling") == 0

    issues = read_memory(tmp_path)["issues"]
    assert issues["1660"]["gateEntries"] == 1
    assert issues["1661"]["gateEntries"] == 0
    assert issues["1660"]["closed"] == [
        {"pr": 1696, "outcome": "merged", "rounds": 2},
        {"pr": 1700, "outcome": "merged", "rounds": 1},
    ]


def test_ceiling_is_added_once(tmp_path: Path) -> None:
    write_memory(tmp_path, {"issues": {}})
    argv = ("record", "--issue", "3", "--pr", "40", "--rounds", "2", "--outcome", "superseded-by-split", "--ceiling")

    assert run(tmp_path, *argv) == 0
    assert run(tmp_path, *argv) == 0

    assert read_memory(tmp_path)["issues"]["3"]["ceilingPrs"] == [40]


def test_check_escalates_on_a_prior_ceiling_with_and_without_pr(tmp_path: Path, capsys) -> None:
    write_memory(tmp_path, {"issues": {"12": entry(ceiling=[100]), "13": entry()}})

    assert run(tmp_path, "check", "--issue", "12", "--pr", "101") == 2
    assert "#100" in capsys.readouterr().out
    assert run(tmp_path, "check", "--issue", "12") == 2
    assert "#100" in capsys.readouterr().out
    # The ceiling PR itself is not its own successor.
    assert run(tmp_path, "check", "--issue", "12", "--pr", "100") == 0
    assert run(tmp_path, "check", "--issue", "13", "--pr", "101") == 0
    assert run(tmp_path, "check", "--issue", "14") == 0
    # A batch escalates when any of its issues does.
    assert run(tmp_path, "check", "--issue", "13", "--issue", "12", "--pr", "101") == 2


def test_missing_memory_file_loads_empty_and_record_creates_it(tmp_path: Path) -> None:
    assert review_gate.load_history(tmp_path) == {"issues": {}}
    assert run(tmp_path, "check", "--issue", "1") == 0
    assert not (tmp_path / ".review-gate-issues.json").exists()

    assert run(tmp_path, "record", "--issue", "1", "--pr", "2", "--rounds", "0", "--outcome", "abandoned") == 0

    assert (tmp_path / ".review-gate-issues.json").read_text(encoding="utf-8") == (
        '{\n  "issues": {\n    "1": {\n      "ceilingPrs": [],\n      "gateEntries": 0,\n'
        '      "closed": [\n        {\n          "pr": 2,\n          "outcome": "abandoned",\n'
        '          "rounds": 0\n        }\n      ]\n    }\n  }\n}\n'
    )


def test_malformed_entry_or_on_disk_outcome_fails_the_load(tmp_path: Path, capsys) -> None:
    write_memory(tmp_path, {"issues": {"1": entry(closed=[{"pr": 7, "outcome": "closed", "rounds": 1}])}})
    assert run(tmp_path, "check", "--issue", "1") == 1
    assert "issues[1].closed[0].outcome='closed' is outside OUTCOMES" in capsys.readouterr().err

    write_memory(tmp_path, {"issues": {"1": {"ceilingPrs": [], "gateEntries": True, "closed": []}}})
    assert run(tmp_path, "check", "--issue", "1") == 1
    assert "issues[1].gateEntries must be int" in capsys.readouterr().err


def test_committed_memory_round_trips_byte_identically(tmp_path: Path) -> None:
    # Load (through the structure guard) and save must reproduce the committed
    # bytes exactly, so a `record` diff shows only the recorded close.
    history = review_gate.load_history(REPO_ROOT)
    review_gate.save_history(tmp_path, history)

    assert (tmp_path / ".review-gate-issues.json").read_bytes() == COMMITTED_MEMORY.read_bytes()


def test_documented_script_invocation_runs(tmp_path: Path) -> None:
    write_memory(tmp_path, {"issues": {"21": entry(ceiling=[300])}})

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "review_gate.py"), "--root", str(tmp_path),
         "check", "--issue", "21", "--pr", "301"],
        capture_output=True, text=True, check=False,
    )

    assert proc.returncode == 2, proc.stderr
    assert "#300" in proc.stdout
