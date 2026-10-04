#!/usr/bin/env python3
"""Partition the test files of the master full regression into shards (#2710).

``Unit Tests (full)`` runs as a matrix in ``.github/workflows/ci.yml``; each
matrix job runs the files this script prints for its shard::

    python scripts/ci/shard_tests.py --shard 2 --total 4 > shard-files.txt

The file set is listed from disk at run time with pytest's own collection rule
(``testpaths`` / ``python_files`` / ``norecursedirs`` of ``pyproject.toml``,
pytest's defaults where unset), so a new test file is picked up without
regenerating anything. Weights come from ``full_test_durations.json`` (seconds
per file); a file the table does not know gets the median. Assignment is
greedy: files sorted by (weight descending, path ascending), each put on the
currently lightest shard, the lowest index winning ties. Nothing else decides
the result, so every matrix job computes the same partition, the shards are
pairwise disjoint and their union is the whole file set. Each shard is printed
in ascending path order, which keeps today's cross-file order inside a shard.

An empty shard, or a shard index outside ``1..total``, exits non-zero and
prints no path: pytest given no path would run all of ``testpaths``.

``durations --junit FILE`` rebuilds the table from the junit XML of one full
run (the sum of ``testcase`` times per file). Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import statistics
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DURATIONS_PATH = Path(__file__).resolve().with_name("full_test_durations.json")
# pytest's defaults for the options pyproject.toml does not set.
DEFAULT_TESTPATHS = ("tests",)
DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
DEFAULT_NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv", "{arch}")
# Weight of a file when the table is empty; any positive number balances by count.
FALLBACK_WEIGHT = 1.0


def _pytest_options(root: Path) -> Mapping[str, Any]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    with pyproject.open("rb") as handle:
        return tomllib.load(handle).get("tool", {}).get("pytest", {}).get("ini_options", {})


def _option(options: Mapping[str, Any], key: str, default: Sequence[str]) -> tuple[str, ...]:
    value = options.get(key, default)
    return tuple(value.split()) if isinstance(value, str) else tuple(str(item) for item in value)


def collect_test_files(root: Path = REPO_ROOT) -> list[str]:
    """Every file pytest collects from ``testpaths``, as sorted repo-relative posix paths."""

    options = _pytest_options(root)
    testpaths = _option(options, "testpaths", DEFAULT_TESTPATHS)
    patterns = _option(options, "python_files", DEFAULT_PYTHON_FILES)
    norecurse = _option(options, "norecursedirs", DEFAULT_NORECURSEDIRS)
    found: set[str] = set()
    for testpath in testpaths:
        for directory, subdirectories, filenames in os.walk(root / testpath):
            subdirectories[:] = [
                name
                for name in subdirectories
                if name != "__pycache__" and not any(fnmatch.fnmatch(name, pattern) for pattern in norecurse)
            ]
            for filename in filenames:
                if any(fnmatch.fnmatch(filename, pattern) for pattern in patterns):
                    found.add((Path(directory) / filename).relative_to(root).as_posix())
    return sorted(found)


def load_durations(path: Path = DURATIONS_PATH) -> dict[str, float]:
    return {str(name): float(seconds) for name, seconds in json.loads(path.read_text(encoding="utf-8")).items()}


def default_weight(durations: Mapping[str, float]) -> float:
    return float(statistics.median(durations.values())) if durations else FALLBACK_WEIGHT


def partition(files: Iterable[str], durations: Mapping[str, float], total: int) -> list[list[str]]:
    """``total`` disjoint lists covering ``files``; a pure function of its arguments."""

    if total < 1:
        raise ValueError(f"total must be at least 1, got {total}")
    default = default_weight(durations)
    unique = set(files)
    weights = {path: durations.get(path, default) for path in unique}
    shards: list[list[str]] = [[] for _ in range(total)]
    loads = [0.0] * total
    for path in sorted(unique, key=lambda item: (-weights[item], item)):
        lightest = min(range(total), key=lambda index: (loads[index], index))
        shards[lightest].append(path)
        loads[lightest] += weights[path]
    return [sorted(shard) for shard in shards]


def _file_of(testcase: ET.Element, root: Path) -> str | None:
    declared = testcase.get("file")
    if declared and (root / declared).is_file():
        return Path(declared).as_posix()
    # pytest's default xunit2 has no `file`: `classname` is the dotted module
    # path, followed by the test classes for a method.
    parts = (testcase.get("classname") or "").split(".")
    for length in range(len(parts), 0, -1):
        candidate = Path(*parts[:length]).with_suffix(".py")
        if (root / candidate).is_file():
            return candidate.as_posix()
    return None


def durations_from_junit(junit: Path, root: Path = REPO_ROOT) -> tuple[dict[str, float], int]:
    """(seconds per collected test file, number of testcases that map to no such file)."""

    collected = set(collect_test_files(root))
    totals: dict[str, float] = {}
    unmapped = 0
    for testcase in ET.parse(junit).getroot().iter("testcase"):
        path = _file_of(testcase, root)
        if path is None or path not in collected:
            unmapped += 1
            continue
        totals[path] = totals.get(path, 0.0) + float(testcase.get("time") or 0.0)
    return {path: round(totals[path], 3) for path in sorted(totals)}, unmapped


def _shard_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.shard is None or args.total is None:
        parser.error("--shard and --total are required")
    if args.total < 1 or not 1 <= args.shard <= args.total:
        parser.error(f"--shard must be within 1..--total, got --shard {args.shard} --total {args.total}")
    files = collect_test_files(args.root)
    shards = partition(files, load_durations(args.durations), args.total)
    # The completeness guard runs in every matrix job, on the real tree.
    flat = [path for shard in shards for path in shard]
    if sorted(flat) != files:
        print(f"shard_tests: the {args.total} shards do not partition the {len(files)} test files", file=sys.stderr)
        return 1
    selected = shards[args.shard - 1]
    if not selected:
        print(f"shard_tests: shard {args.shard} of {args.total} is empty ({len(files)} test files)", file=sys.stderr)
        return 1
    print("\n".join(selected))
    return 0


def _durations_command(args: argparse.Namespace) -> int:
    durations, unmapped = durations_from_junit(args.junit, args.root)
    if not durations:
        print(f"shard_tests: no testcase of {args.junit} maps to a collected test file", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(durations, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    known = len(collect_test_files(args.root))
    print(
        f"wrote {args.output}: {len(durations)} of {known} collected files, "
        f"{sum(durations.values()):.0f}s total, {unmapped} testcases unmapped",
        file=sys.stderr,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int, help="1-based shard index")
    parser.add_argument("--total", type=int, help="number of shards")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--durations", type=Path, default=DURATIONS_PATH, help="duration table (JSON)")
    commands = parser.add_subparsers(dest="command")
    build = commands.add_parser("durations", help="rebuild the duration table from a junit XML")
    build.add_argument("--junit", type=Path, required=True)
    build.add_argument("--root", type=Path, default=REPO_ROOT)
    build.add_argument("--output", type=Path, default=DURATIONS_PATH)
    args = parser.parse_args(argv)
    if args.command == "durations":
        return _durations_command(args)
    return _shard_command(args, parser)


if __name__ == "__main__":
    sys.exit(main())
