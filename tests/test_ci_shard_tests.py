"""The sharded master full regression (#2710): partition and ci.yml wiring.

``Unit Tests (full)`` runs as a matrix; each shard runs the test files
``scripts/ci/shard_tests.py`` assigns to it. The partition must be complete and
disjoint for any file set, or the only whole-repo regression silently becomes a
partial one. Expected values here come from the partition rule itself (sets,
not a second implementation) and from ``git ls-files`` for the real tree.
"""

from __future__ import annotations

import fnmatch
import json
import random
import statistics
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci import shard_tests

CI_WORKFLOW = Path(".github/workflows/ci.yml")
MARKER_EXPRESSION = '-m "not e2e and not grib and not integration"'
LOCKED_SYNC = "uv sync --locked --all-extras --dev"


def _synthetic(count: int) -> tuple[list[str], dict[str, float]]:
    files = [f"tests/test_{index:03d}.py" for index in range(count)]
    # Every third file is unknown to the table: it must still land in one shard.
    durations = {path: float((index * 37) % 101 + 1) for index, path in enumerate(files) if index % 3}
    return files, durations


# --- the partition ---------------------------------------------------------------


@pytest.mark.parametrize(("count", "total"), [(4, 4), (5, 2), (57, 4), (200, 7), (9, 1)])
def test_partition_is_complete_and_disjoint(count: int, total: int) -> None:
    files, durations = _synthetic(count)

    shards = shard_tests.partition(files, durations, total)

    assert len(shards) == total
    flat = [path for shard in shards for path in shard]
    assert len(flat) == len(set(flat)) == count  # no file twice
    assert set(flat) == set(files)  # no file dropped


def test_partition_does_not_depend_on_the_input_order() -> None:
    files, durations = _synthetic(120)
    expected = shard_tests.partition(files, durations, 4)

    for seed in range(5):
        shuffled = files[:]
        random.Random(seed).shuffle(shuffled)
        assert shard_tests.partition(shuffled, durations, 4) == expected
    assert shard_tests.partition(reversed(files), durations, 4) == expected


def test_each_shard_is_listed_in_ascending_path_order() -> None:
    files, durations = _synthetic(120)

    for shard in shard_tests.partition(files, durations, 4):
        assert shard == sorted(shard)


def test_greedy_rule_heaviest_first_onto_the_lightest_shard_lowest_index_on_ties() -> None:
    durations = {"tests/test_a.py": 10.0, "tests/test_b.py": 7.0, "tests/test_c.py": 7.0, "tests/test_d.py": 2.0}

    # a(10) -> shard 1 (all empty, lowest index); b(7) -> shard 2; c(7) -> shard 2
    # is at 7 < 10 so c joins b (14); d(2) -> shard 1 (10 < 14).
    assert shard_tests.partition(list(durations), durations, 2) == [
        ["tests/test_a.py", "tests/test_d.py"],
        ["tests/test_b.py", "tests/test_c.py"],
    ]


def test_unknown_file_gets_the_median_weight() -> None:
    durations = {"tests/test_a.py": 1.0, "tests/test_b.py": 5.0, "tests/test_c.py": 100.0}
    assert shard_tests.default_weight(durations) == statistics.median(durations.values()) == 5.0

    # new(5, the median) ties with b(5): path order puts b first. c(100) -> shard 1,
    # b -> shard 2, new -> shard 2 (5 < 100), a -> shard 2 (10 < 100).
    shards = shard_tests.partition([*durations, "tests/test_new.py"], durations, 2)

    assert shards == [["tests/test_c.py"], ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]]


def test_default_weight_of_an_empty_table_is_positive() -> None:
    assert shard_tests.default_weight({}) > 0


def test_partition_balances_within_one_heaviest_file() -> None:
    files, durations = _synthetic(200)
    default = shard_tests.default_weight(durations)

    shards = shard_tests.partition(files, durations, 4)
    loads = [sum(durations.get(path, default) for path in shard) for shard in shards]

    assert max(loads) - min(loads) <= max(durations.values())


# --- the real tree -----------------------------------------------------------------


def _tracked_test_files() -> set[str]:
    listed = subprocess.run(["git", "ls-files", "tests"], check=True, capture_output=True, text=True).stdout.split("\n")
    return {
        path
        for path in listed
        if any(fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern) for pattern in ("test_*.py", "*_test.py"))
    }


def test_collected_file_set_matches_pytests_rule_on_the_real_tree() -> None:
    collected = shard_tests.collect_test_files()

    assert collected == sorted(collected)
    assert Path(__file__).resolve().relative_to(Path.cwd().resolve()).as_posix() in collected
    assert "tests/conftest.py" not in collected
    # Every tracked test file is listed; anything else listed is an untracked
    # test file on disk, which pytest would collect too.
    assert _tracked_test_files() <= set(collected)
    assert all(Path(path).is_file() for path in collected)


def test_real_tree_partition_is_complete_disjoint_and_never_empty() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    total = len(workflow["jobs"]["unit-test"]["strategy"]["matrix"]["shard"])
    collected = shard_tests.collect_test_files()
    durations = shard_tests.load_durations()

    shards = shard_tests.partition(collected, durations, total)

    flat = [path for shard in shards for path in shard]
    assert sorted(flat) == collected
    assert len(flat) == len(set(flat))
    assert all(shards)


def test_collection_follows_python_files_and_nested_directories(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["checks"]\npython_files = ["check_*.py"]\n', encoding="utf-8"
    )
    for relative in (
        "checks/check_a.py",
        "checks/nested/deep/check_b.py",
        "checks/test_ignored_by_python_files.py",
        "checks/conftest.py",
        "checks/__pycache__/check_cached.py",
        "checks/.hidden/check_hidden.py",
        "elsewhere/check_outside_testpaths.py",
    ):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")

    assert shard_tests.collect_test_files(tmp_path) == ["checks/check_a.py", "checks/nested/deep/check_b.py"]


def test_collection_defaults_to_both_pytest_patterns(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    for relative in ("tests/test_a.py", "tests/sub/b_test.py", "tests/helper.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")

    assert shard_tests.collect_test_files(tmp_path) == ["tests/sub/b_test.py", "tests/test_a.py"]


def test_duration_table_is_a_flat_sorted_map_of_test_paths_to_seconds() -> None:
    durations = json.loads(shard_tests.DURATIONS_PATH.read_text(encoding="utf-8"))

    assert durations and list(durations) == sorted(durations)
    assert all(isinstance(seconds, (int, float)) and seconds >= 0 for seconds in durations.values())
    # No "every key still exists" pin: deleting a test file must not require
    # regenerating the table. A stale key is simply never looked up.
    assert all(path.startswith("tests/") and path.endswith(".py") for path in durations)


def test_stale_table_entries_are_ignored() -> None:
    durations = {"tests/test_a.py": 1.0, "tests/test_deleted.py": 500.0}

    assert shard_tests.partition(["tests/test_a.py", "tests/test_b.py"], durations, 2) == [
        ["tests/test_b.py"],
        ["tests/test_a.py"],
    ]


# --- the CLI ---------------------------------------------------------------------------


def _tree(tmp_path: Path, count: int) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    for index in range(count):
        (tmp_path / "tests" / f"test_{index}.py").write_text("", encoding="utf-8")
    table = tmp_path / "durations.json"
    table.write_text(json.dumps({"tests/test_0.py": 9.0, "tests/test_1.py": 1.0}), encoding="utf-8")
    return table


def test_cli_prints_one_shard_and_the_shards_cover_the_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    table = _tree(tmp_path, 7)
    printed: list[list[str]] = []

    for shard in (1, 2, 3):
        argv = ["--shard", str(shard), "--total", "3", "--root", str(tmp_path), "--durations", str(table)]
        assert shard_tests.main(argv) == 0
        printed.append(capsys.readouterr().out.splitlines())

    assert all(lines == sorted(lines) and lines for lines in printed)
    assert sorted(path for lines in printed for path in lines) == [f"tests/test_{index}.py" for index in range(7)]


def test_cli_exits_non_zero_and_prints_nothing_for_an_empty_shard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    table = _tree(tmp_path, 2)

    code = shard_tests.main(["--shard", "3", "--total", "3", "--root", str(tmp_path), "--durations", str(table)])

    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert "empty" in captured.err


@pytest.mark.parametrize("shard", ["0", "5"])
def test_cli_rejects_a_shard_index_outside_the_matrix(shard: str, tmp_path: Path) -> None:
    table = _tree(tmp_path, 8)

    with pytest.raises(SystemExit) as excinfo:
        shard_tests.main(["--shard", shard, "--total", "4", "--root", str(tmp_path), "--durations", str(table)])
    assert excinfo.value.code != 0


def test_durations_subcommand_sums_testcase_time_per_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    for relative in ("tests/test_a.py", "tests/sub/test_b.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("", encoding="utf-8")
    junit = tmp_path / "full.xml"
    junit.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
  <testcase classname="tests.test_a" name="test_one" time="1.25"/>
  <testcase classname="tests.test_a.TestThing" name="test_two" time="0.5"/>
  <testcase classname="tests.test_a.TestThing.TestInner" name="test_three[x.y]" time="0.25"><skipped/></testcase>
  <testcase classname="tests.sub.test_b" name="test_four" time="3.0004"/>
  <testcase classname="tests.test_gone" name="test_five" time="9"/>
</testsuite></testsuites>
""",
        encoding="utf-8",
    )
    output = tmp_path / "table.json"

    code = shard_tests.main(["durations", "--junit", str(junit), "--root", str(tmp_path), "--output", str(output)])

    assert code == 0
    text = output.read_text(encoding="utf-8")
    assert json.loads(text) == {"tests/sub/test_b.py": 3.0, "tests/test_a.py": 2.0}
    assert list(json.loads(text)) == ["tests/sub/test_b.py", "tests/test_a.py"]
    assert text.endswith("\n")


# --- ci.yml wiring ----------------------------------------------------------------------


def _unit_test_job() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["unit-test"]


def _runs(job: dict[str, Any]) -> list[str]:
    return [str(step.get("run", "")) for step in job["steps"]]


def test_full_job_is_a_single_dimension_matrix_of_four_shards_that_do_not_cancel_each_other() -> None:
    strategy = _unit_test_job()["strategy"]

    assert strategy["fail-fast"] is False
    assert strategy["matrix"] == {"shard": [1, 2, 3, 4]}
    assert set(strategy) == {"fail-fast", "matrix"}


def test_full_job_name_is_static_and_its_timeout_is_a_literal_not_above_the_old_wall() -> None:
    job = _unit_test_job()

    assert job["name"] == "Unit Tests (full)"
    assert "${{" not in job["name"]
    assert type(job["timeout-minutes"]) is int
    assert job["timeout-minutes"] == 30
    assert job["timeout-minutes"] <= 60


def test_shard_total_comes_from_the_matrix_and_the_list_is_written_by_its_own_command() -> None:
    runs = _runs(_unit_test_job())
    lister = next(run for run in runs if "scripts/ci/shard_tests.py" in run)

    assert (
        "python scripts/ci/shard_tests.py --shard ${{ matrix.shard }} --total ${{ strategy.job-total }}"
        " > shard-files.txt"
    ) in lister
    assert "pytest" not in lister  # a failing lister fails its own step, before pytest starts
    assert runs.index(lister) < next(index for index, run in enumerate(runs) if "pytest" in run)


def test_no_command_substitution_feeds_pytest_anywhere_in_ci_yml() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "pytest $(" not in text
    assert "pytest `" not in text


def test_shard_pytest_step_reads_the_file_and_keeps_the_flags_and_marker_expression() -> None:
    (pytest_run,) = [run for run in _runs(_unit_test_job()) if "pytest" in run]

    assert "shard-files.txt" in pytest_run
    assert "pytest tests/" not in pytest_run  # never the whole testpath in a shard
    for flag in ("-q", "--tb=short", "--durations=25", MARKER_EXPRESSION):
        assert flag in pytest_run


def test_full_job_keeps_its_gate_and_prerequisite_steps() -> None:
    job = _unit_test_job()
    runs = _runs(job)

    assert job["needs"] == "changes"
    gate = " ".join(job["if"].split())
    assert gate == (
        "github.event_name == 'workflow_dispatch' || "
        "(needs.changes.outputs.backend == 'true' && github.event_name == 'push')"
    )
    assert job["steps"][0]["with"] == {"fetch-depth": 0}
    assert LOCKED_SYNC in runs
    assert 'echo "$PWD/.venv/bin" >> "$GITHUB_PATH"' in runs
    assert any("mkdir -p /scratch/frd_muziyao" in run for run in runs)


def test_ci_yml_still_has_exactly_three_locked_installs() -> None:
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert text.count(f"run: {LOCKED_SYNC}\n") == 3
