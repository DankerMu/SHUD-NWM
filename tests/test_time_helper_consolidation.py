"""Time/identity helper consolidation (OpenSpec change ``entropy-burndown-2026-10``, task 3.1).

Two halves:

* semantic unit tests of the canonical helpers ``utc_now``, ``parse_iso_utc`` and
  ``format_iso_utc`` in ``packages/common/contracts/time_normalization.py``;
* a ceiling on LOCAL re-definitions of those concepts.  The number of local
  helpers may only go down; ``tests/time_helper_ceiling.json`` (exactly
  ``{"ceiling": <int>}``) holds the bound:

  - ``count > ceiling`` fails -- someone added a local helper; import the
    canonical one instead (raising the ceiling is an explicit, reviewed ratchet
    change);
  - ``count * 20 < ceiling * 19`` (more than 5 % below, integer arithmetic)
    fails too -- a cleanup must lower the ceiling in the same PR.

What is counted (the test's own method is the definition):

* files: ``sorted(root.rglob("*.py"))`` for every root in ``_SCAN_ROOTS``,
  skipping any path whose repo-relative ``parts`` contain one of
  ``_EXCLUDED_DIR_PARTS`` and the canonical modules in ``_CANONICAL_FILES``; a
  ``SyntaxError`` propagates;
* definitions: only the DIRECT children of ``ast.Module.body`` that are
  ``FunctionDef``/``AsyncFunctionDef`` (methods, nested functions and
  definitions inside ``if``/``try`` are not counted) whose name
  ``_RULE.fullmatch``-es;
* count = hits - entries of ``tests/time_helper_allowlist.json``.

The allowlist is a top-level list of ``{"file", "name", "reason"}`` entries,
sorted by ``(file, name)``, registering only definitions that KEEP a matching
name because they are a different concept or a patch seam (a helper renamed out
of the rule needs no entry).  Every entry must name a module-level definition
that still exists, or it is stale and fails.

``scripts/select_ci_tests.py`` routes every changed ``*.py`` under the roots in
``_SCAN_ROOTS`` to this module (``TIME_HELPER_CONSOLIDATION_ROOTS``) and both
JSON files to it as well; a selector meta-test parses ``_SCAN_ROOTS`` out of
this file and asserts the two agree.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from packages.common.contracts.time_normalization import (
    ensure_utc,
    format_iso_utc,
    parse_iso_utc,
    utc_now,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CEILING_RELATIVE_PATH = "tests/time_helper_ceiling.json"
ALLOWLIST_RELATIVE_PATH = "tests/time_helper_allowlist.json"

# The production roots this ceiling scans. The selector mirrors them as
# TIME_HELPER_CONSOLIDATION_ROOTS; a meta-test keeps the two equal.
_SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)

_RULE = re.compile(
    r"_?utc_?now|_?now_iso|_?iso_now|_?parse_(iso|utc|time|iso8601|iso_utc)|_?coerce_datetime"
    r"|_?format_time|_?format_iso_utc|_?normalize_source(_id)?"
)

# The canonical modules: their definitions ARE the helpers, so they never count.
_CANONICAL_FILES: frozenset[str] = frozenset(
    {"packages/common/contracts/time_normalization.py", "packages/common/source_identity.py"}
)

_ALLOWLIST_KEYS = frozenset({"file", "name", "reason"})

_FIX_HINT = (
    "import the canonical helper from packages.common.contracts.time_normalization "
    "(or packages.common.source_identity); raising the ceiling is an explicit, reviewed ratchet change"
)


@dataclass(frozen=True, order=True)
class Hit:
    file: str
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.name}"


@dataclass(frozen=True)
class AllowlistEntry:
    file: str
    name: str
    reason: str


# --------------------------------------------------------------------------- #
# Scan, allowlist, ceiling, verdict
# --------------------------------------------------------------------------- #


def iter_python_sources(repo_root: Path) -> list[Path]:
    """Every scanned ``*.py`` under ``repo_root``, canonical modules excluded, in a stable order."""

    files: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(repo_root)
            if any(part in _EXCLUDED_DIR_PARTS for part in relative.parts):
                continue
            if relative.as_posix() in _CANONICAL_FILES:
                continue
            files.append(path)
    return files


def _module_level_definitions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]


def scan_hits(repo_root: Path) -> list[Hit]:
    """Every module-level definition under the scanned roots whose name fullmatches ``_RULE``.

    ``ast.parse`` errors propagate on purpose: a module the scan cannot parse is
    a module whose helpers it cannot count.
    """

    hits: list[Hit] = []
    for path in iter_python_sources(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(repo_root).as_posix()
        hits.extend(
            Hit(relative, node.lineno, node.name)
            for node in _module_level_definitions(tree)
            if _RULE.fullmatch(node.name)
        )
    return sorted(hits)


def parse_ceiling(raw: object) -> int:
    """The ceiling from a parsed ceiling file; ``ValueError`` naming the file on any other shape."""

    if not isinstance(raw, dict) or set(raw) != {"ceiling"}:
        shape = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
        raise ValueError(f'{CEILING_RELATIVE_PATH}: must be exactly {{"ceiling": <int>}}, got {shape}')
    value = raw["ceiling"]
    if type(value) is not int:
        raise ValueError(f'{CEILING_RELATIVE_PATH}: "ceiling" must be an int, got {type(value).__name__} {value!r}')
    if value < 0:
        raise ValueError(f'{CEILING_RELATIVE_PATH}: "ceiling" must be >= 0, got {value}')
    return value


def _load_json(repo_root: Path, relative: str) -> object:
    try:
        return json.loads((repo_root / relative).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{relative}: not valid JSON ({exc})") from exc


def load_ceiling(repo_root: Path) -> int:
    return parse_ceiling(_load_json(repo_root, CEILING_RELATIVE_PATH))


def _entry_error(index: int, entry: object, problem: str) -> ValueError:
    return ValueError(f"{ALLOWLIST_RELATIVE_PATH}: entry [{index}] {entry!r}: {problem}")


def parse_allowlist(raw: object, repo_root: Path) -> list[AllowlistEntry]:
    """Validated allowlist entries; ``ValueError`` naming the file and the offending entry."""

    if not isinstance(raw, list):
        raise ValueError(f"{ALLOWLIST_RELATIVE_PATH}: must be a top-level list, got {type(raw).__name__}")
    entries: list[AllowlistEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != _ALLOWLIST_KEYS:
            shape = sorted(item) if isinstance(item, dict) else type(item).__name__
            raise _entry_error(index, item, f'must have exactly the keys ["file", "name", "reason"], got {shape}')
        file, name, reason = item["file"], item["name"], item["reason"]
        if not isinstance(file, str) or not isinstance(name, str) or not isinstance(reason, str):
            raise _entry_error(index, item, '"file", "name" and "reason" must be strings')
        parts = file.split("/")
        if (
            "\\" in file
            or file.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or parts[0] not in _SCAN_ROOTS
        ):
            raise _entry_error(
                index, item, f'"file" must be a repo-relative POSIX path under one of {list(_SCAN_ROOTS)}'
            )
        if file in _CANONICAL_FILES:
            raise _entry_error(index, item, '"file" is a canonical module; its definitions are never counted')
        if not _RULE.fullmatch(name):
            raise _entry_error(index, item, '"name" does not fullmatch the helper rule')
        if not reason.strip():
            raise _entry_error(index, item, '"reason" must be a non-empty string')
        entry = AllowlistEntry(file, name, reason)
        if entries:
            previous = (entries[-1].file, entries[-1].name)
            if (file, name) == previous:
                raise _entry_error(index, item, "duplicate (file, name)")
            if (file, name) < previous:
                raise _entry_error(index, item, "entries must be sorted by (file, name)")
        path = repo_root / file
        if not path.is_file():
            raise _entry_error(index, item, "stale: the file does not exist")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not any(node.name == name for node in _module_level_definitions(tree)):
            raise _entry_error(index, item, f"stale: {file} defines no module-level {name}")
        entries.append(entry)
    return entries


def load_allowlist(repo_root: Path) -> list[AllowlistEntry]:
    return parse_allowlist(_load_json(repo_root, ALLOWLIST_RELATIVE_PATH), repo_root)


def ratchet_verdict(count: int, ceiling: int) -> str | None:
    """The failure message for ``(count, ceiling)``, or ``None`` when the ceiling holds.

    Integer arithmetic only: fails above the ceiling, and fails when the count is
    more than 5 % below it (``count * 20 < ceiling * 19``).
    """

    if count > ceiling:
        return (
            f"time-helper count {count} exceeds the ceiling {ceiling} in {CEILING_RELATIVE_PATH} "
            f"by {count - ceiling}: {_FIX_HINT}"
        )
    if count * 20 < ceiling * 19:
        return (
            f"time-helper count {count} is more than 5 % below the ceiling {ceiling}: "
            f'set "ceiling" to {count} in {CEILING_RELATIVE_PATH}'
        )
    return None


@dataclass(frozen=True)
class Census:
    hits: list[Hit]
    allowlist: list[AllowlistEntry]

    @property
    def unallowlisted(self) -> list[Hit]:
        allowed = {(entry.file, entry.name) for entry in self.allowlist}
        return [hit for hit in self.hits if (hit.file, hit.name) not in allowed]

    @property
    def count(self) -> int:
        return len(self.hits) - len(self.allowlist)


def take_census(repo_root: Path) -> Census:
    return Census(scan_hits(repo_root), load_allowlist(repo_root))


def check_time_helpers(repo_root: Path) -> str | None:
    """The verdict for ``repo_root``; above the ceiling it lists every non-allowlisted hit."""

    census = take_census(repo_root)
    ceiling = load_ceiling(repo_root)
    verdict = ratchet_verdict(census.count, ceiling)
    if verdict is not None and census.count > ceiling:
        verdict += "\nnon-allowlisted hits (file:line:name):\n" + "\n".join(f"  {hit}" for hit in census.unallowlisted)
    return verdict


# --------------------------------------------------------------------------- #
# Canonical helpers
# --------------------------------------------------------------------------- #

_PLUS_EIGHT = timezone(timedelta(hours=8))


def test_round_trip_is_exact_for_an_offset_aware_datetime_with_microseconds() -> None:
    dt = datetime(2026, 10, 17, 14, 30, 15, 123456, tzinfo=_PLUS_EIGHT)

    assert format_iso_utc(dt) == "2026-10-17T06:30:15.123456Z"
    assert parse_iso_utc(format_iso_utc(dt)) == ensure_utc(dt)
    assert parse_iso_utc(format_iso_utc(dt)).tzinfo is UTC


def test_utc_now_is_aware_utc() -> None:
    before = datetime.now(UTC)
    now = utc_now()
    after = datetime.now(UTC)

    assert now.tzinfo is UTC
    assert before <= now <= after


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-17T06:00:00Z", datetime(2026, 10, 17, 6, tzinfo=UTC)),
        ("2026-10-17T14:00:00+08:00", datetime(2026, 10, 17, 6, tzinfo=UTC)),
        ("2026-10-17T06:00:00", datetime(2026, 10, 17, 6, tzinfo=UTC)),
        ("  2026-10-17T06:00:00Z \n", datetime(2026, 10, 17, 6, tzinfo=UTC)),
        ("2026-10-17Z", datetime(2026, 10, 17, 0, tzinfo=UTC)),
        ("2026-10-17T06:00:00.000500Z", datetime(2026, 10, 17, 6, 0, 0, 500, tzinfo=UTC)),
    ],
)
def test_parse_iso_utc_returns_aware_utc(text: str, expected: datetime) -> None:
    parsed = parse_iso_utc(text)

    assert parsed == expected
    assert parsed.tzinfo is UTC
    assert parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("text", ["", "   ", "2026101700", "2026-10-17T06:00:00z", "not a time"])
def test_parse_iso_utc_rejects_empty_and_non_iso_strings(text: str) -> None:
    with pytest.raises(ValueError):
        parse_iso_utc(text)


@pytest.mark.parametrize("value", [123, None, b"2026-10-17T06:00:00Z", datetime(2026, 10, 17, tzinfo=UTC)])
def test_parse_iso_utc_rejects_non_str(value: object) -> None:
    with pytest.raises(TypeError):
        parse_iso_utc(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 17, 6, 0, 0), "2026-10-17T06:00:00Z"),
        (datetime(2026, 10, 17, 14, 0, 0, tzinfo=_PLUS_EIGHT), "2026-10-17T06:00:00Z"),
        (datetime(2026, 10, 17, 6, 0, 0, 0, tzinfo=UTC), "2026-10-17T06:00:00Z"),
        (datetime(2026, 10, 17, 6, 0, 0, 7, tzinfo=UTC), "2026-10-17T06:00:00.000007Z"),
    ],
)
def test_format_iso_utc_renders_utc_with_z(value: datetime, expected: str) -> None:
    assert format_iso_utc(value) == expected


@pytest.mark.parametrize("value", [None, "2026-10-17T06:00:00Z", 0])
def test_format_iso_utc_rejects_non_datetime(value: object) -> None:
    with pytest.raises(TypeError):
        format_iso_utc(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Real repository tree
# --------------------------------------------------------------------------- #


def test_repository_time_helper_count_is_within_the_ceiling() -> None:
    verdict = check_time_helpers(REPO_ROOT)
    assert verdict is None, verdict


def test_scan_roots_exist_and_are_scanned() -> None:
    # A renamed or missing root would make the count vacuously lower.
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in iter_python_sources(REPO_ROOT)}
    for root in _SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"scan root {root}/ is missing"
        assert any(file.startswith(f"{root}/") for file in scanned), f"no Python file scanned under {root}/"
    for canonical in _CANONICAL_FILES:
        assert (REPO_ROOT / canonical).is_file(), f"canonical module {canonical} is missing"


def test_real_ceiling_and_allowlist_files_are_well_formed() -> None:
    raw = json.loads((REPO_ROOT / CEILING_RELATIVE_PATH).read_text(encoding="utf-8"))

    assert set(raw) == {"ceiling"}
    assert type(raw["ceiling"]) is int
    assert load_ceiling(REPO_ROOT) == raw["ceiling"] >= 0
    load_allowlist(REPO_ROOT)


# --------------------------------------------------------------------------- #
# The (count, ceiling) verdict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("count", "ceiling"), [(69, 69), (68, 69), (66, 69), (0, 0), (19, 20), (20, 20)])
def test_verdict_passes_within_five_percent_at_or_below_the_ceiling(count: int, ceiling: int) -> None:
    assert ratchet_verdict(count, ceiling) is None


def test_verdict_fails_above_the_ceiling_with_count_ceiling_excess_and_file() -> None:
    assert ratchet_verdict(70, 69) == (
        "time-helper count 70 exceeds the ceiling 69 in tests/time_helper_ceiling.json by 1: "
        "import the canonical helper from packages.common.contracts.time_normalization "
        "(or packages.common.source_identity); raising the ceiling is an explicit, reviewed ratchet change"
    )
    assert ratchet_verdict(1, 0) is not None


def test_verdict_fails_more_than_five_percent_below_the_ceiling_asking_to_lower_it() -> None:
    assert ratchet_verdict(65, 69) == (
        'time-helper count 65 is more than 5 % below the ceiling 69: '
        'set "ceiling" to 65 in tests/time_helper_ceiling.json'
    )
    # floor(count / 0.95) + 1 is the smallest ceiling the count no longer satisfies.
    assert ratchet_verdict(69, 72) is None
    assert ratchet_verdict(69, 73) is not None


@pytest.mark.parametrize(
    "raw",
    [
        {"ceiling": 10, "note": "x"},
        {},
        {"limit": 10},
        [10],
        10,
        {"ceiling": True},
        {"ceiling": 10.0},
        {"ceiling": "10"},
        {"ceiling": None},
        {"ceiling": -1},
    ],
)
def test_malformed_ceiling_fails_naming_the_file(raw: object) -> None:
    with pytest.raises(ValueError, match="tests/time_helper_ceiling.json"):
        parse_ceiling(raw)


def test_ceiling_file_that_is_not_json_fails_naming_the_file(tmp_path: Path) -> None:
    _write(tmp_path, CEILING_RELATIVE_PATH, "{ceiling: 3}\n")

    with pytest.raises(ValueError, match="tests/time_helper_ceiling.json"):
        load_ceiling(tmp_path)


# --------------------------------------------------------------------------- #
# Synthetic trees (tmp_path)
# --------------------------------------------------------------------------- #


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_ceiling(root: Path, ceiling: int) -> None:
    _write(root, CEILING_RELATIVE_PATH, json.dumps({"ceiling": ceiling}) + "\n")


def _write_allowlist(root: Path, entries: list[object]) -> None:
    _write(root, ALLOWLIST_RELATIVE_PATH, json.dumps(entries) + "\n")


def _seed_tree(root: Path) -> None:
    """A small tree with 20 module-level hits and an empty allowlist."""

    for index in range(20):
        _write(root, f"packages/p{index:02d}.py", "def _format_time(value):\n    return value\n")
    _write_allowlist(root, [])


def test_scenario_new_local_helper_fails_and_names_it(tmp_path: Path) -> None:
    # Spec "New local helper fails": ceiling = the synthetic tree's original count.
    _seed_tree(tmp_path)
    _write_ceiling(tmp_path, take_census(tmp_path).count)
    assert check_time_helpers(tmp_path) is None

    _write(tmp_path, "services/x.py", "def _utcnow(): ...\n")
    message = check_time_helpers(tmp_path)

    assert message is not None
    assert "count 21" in message and "ceiling 20" in message and "by 1" in message
    assert CEILING_RELATIVE_PATH in message
    assert "packages.common.contracts.time_normalization" in message
    assert "raising the ceiling is an explicit, reviewed ratchet change" in message
    assert "  services/x.py:1:_utcnow" in message.splitlines()
    assert "  packages/p00.py:1:_format_time" in message.splitlines()


def test_scenario_cleanup_must_lower_the_ceiling(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    _write_ceiling(tmp_path, 20)

    (tmp_path / "packages/p00.py").unlink()
    assert check_time_helpers(tmp_path) is None
    (tmp_path / "packages/p01.py").unlink()

    assert check_time_helpers(tmp_path) == (
        'time-helper count 18 is more than 5 % below the ceiling 20: '
        'set "ceiling" to 18 in tests/time_helper_ceiling.json'
    )
    _write_ceiling(tmp_path, 18)
    assert check_time_helpers(tmp_path) is None


def test_allowlisted_hit_is_deducted(tmp_path: Path) -> None:
    _seed_tree(tmp_path)
    _write_ceiling(tmp_path, 20)
    _write(tmp_path, "services/x.py", "def _utcnow(): ...\n")
    assert check_time_helpers(tmp_path) is not None

    _write_allowlist(tmp_path, [{"file": "services/x.py", "name": "_utcnow", "reason": "patch seam"}])
    census = take_census(tmp_path)

    assert len(census.hits) == 21
    assert census.count == 20
    assert Hit("services/x.py", 1, "_utcnow") not in census.unallowlisted
    assert check_time_helpers(tmp_path) is None


def test_canonical_files_are_not_counted(tmp_path: Path) -> None:
    for canonical in sorted(_CANONICAL_FILES):
        _write(
            tmp_path, canonical, "def utc_now(): ...\ndef parse_iso_utc(text): ...\ndef normalize_source_id(v): ...\n"
        )
    _write(tmp_path, "packages/common/other.py", "def utc_now(): ...\n")

    assert scan_hits(tmp_path) == [Hit("packages/common/other.py", 1, "utc_now")]


def test_methods_nested_and_guarded_definitions_are_not_counted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "services/orchestrator/m.py",
        "class C:\n"
        "    def _utcnow(self): ...\n"
        "    async def _format_time(self): ...\n"
        "def outer():\n"
        "    def _utcnow(): ...\n"
        "    return _utcnow\n"
        "if True:\n"
        "    def _parse_time(v): ...\n"
        "try:\n"
        "    def _coerce_datetime(v): ...\n"
        "except ImportError:\n"
        "    pass\n"
        "_utc_now = lambda: None\n"
        "async def _parse_utc(v): ...\n",
    )

    assert scan_hits(tmp_path) == [Hit("services/orchestrator/m.py", 14, "_parse_utc")]


def test_rule_is_a_fullmatch(tmp_path: Path) -> None:
    names = [
        "_utcnow", "utcnow", "_utc_now", "utc_now", "_now_iso", "iso_now", "_parse_iso", "parse_utc",
        "_parse_time", "parse_iso8601", "_parse_iso_utc", "_coerce_datetime", "format_time",
        "_format_iso_utc", "_normalize_source", "normalize_source_id",
        # Not fullmatches: prefixes/suffixes around a rule name.
        "_format_time_window", "_parse_cycle_time", "now_utc", "_utcnow_iso", "_normalize_source_ids",
        "__utcnow", "_parse_timestamp", "_ensure_utc", "_format_utc",
    ]
    _write(tmp_path, "workers/w.py", "".join(f"def {name}(): ...\n" for name in names))

    assert [hit.name for hit in scan_hits(tmp_path)] == names[:16]


def test_tests_root_and_excluded_directories_are_not_scanned(tmp_path: Path) -> None:
    for relative in (
        "tests/test_x.py",
        "packages/__pycache__/a.py",
        "workers/.venv/lib/a.py",
        "apps/frontend/node_modules/x/a.py",
        "services/.mypy_cache/a.py",
        "scripts/.pytest_cache/a.py",
        "db/x.py",
    ):
        _write(tmp_path, relative, "def _utcnow(): ...\n")
    _write(tmp_path, "apps/api/conftest.py", "def _utcnow(): ...\n")
    _write(tmp_path, "scripts/test_helper.py", "async def utc_now(): ...\n")

    assert [str(hit) for hit in scan_hits(tmp_path)] == [
        "apps/api/conftest.py:1:_utcnow",
        "scripts/test_helper.py:1:utc_now",
    ]


def test_syntax_error_is_not_swallowed(tmp_path: Path) -> None:
    _write(tmp_path, "services/broken.py", "def broken(:\n")

    with pytest.raises(SyntaxError):
        scan_hits(tmp_path)


def test_stale_allowlist_entry_fails_naming_it(tmp_path: Path) -> None:
    _write(tmp_path, "services/x.py", "def _utcnow_renamed(): ...\nclass C:\n    def _utcnow(self): ...\n")

    _write_allowlist(tmp_path, [{"file": "services/x.py", "name": "_utcnow", "reason": "seam"}])
    with pytest.raises(ValueError, match=r"tests/time_helper_allowlist.json: entry \[0\].*_utcnow.*stale"):
        load_allowlist(tmp_path)

    _write_allowlist(tmp_path, [{"file": "services/gone.py", "name": "_utcnow", "reason": "seam"}])
    with pytest.raises(ValueError, match=r"entry \[0\].*services/gone.py.*stale: the file does not exist"):
        load_allowlist(tmp_path)


def _valid_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {"file": "services/x.py", "name": "_utcnow", "reason": "patch seam"}
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({"file": "services/x.py"}, "top-level list"),
        ([["services/x.py", "_utcnow", "r"]], "exactly the keys"),
        ([{"file": "services/x.py", "name": "_utcnow"}], "exactly the keys"),
        ([_valid_entry(extra=1)], "exactly the keys"),
        ([_valid_entry(reason=3)], "must be strings"),
        ([_valid_entry(reason="  ")], "non-empty"),
        ([_valid_entry(name="_format_time_window")], "does not fullmatch"),
        ([_valid_entry(file="tests/x.py")], "repo-relative POSIX path"),
        ([_valid_entry(file="/services/x.py")], "repo-relative POSIX path"),
        ([_valid_entry(file="services\\x.py")], "repo-relative POSIX path"),
        ([_valid_entry(file="services/../services/x.py")], "repo-relative POSIX path"),
        ([_valid_entry(file="packages/common/source_identity.py", name="normalize_source_id")], "canonical"),
        ([_valid_entry(), _valid_entry()], "duplicate"),
        ([_valid_entry(name="_utcnow"), _valid_entry(name="_format_time")], "sorted"),
        ([_valid_entry(file="services/y.py"), _valid_entry()], "sorted"),
    ],
)
def test_malformed_allowlist_fails_naming_the_entry(tmp_path: Path, raw: object, problem: str) -> None:
    _write(tmp_path, "services/x.py", "def _utcnow(): ...\ndef _format_time(v): ...\n")
    _write(tmp_path, "services/y.py", "def _utcnow(): ...\n")
    _write(tmp_path, "packages/common/source_identity.py", "def normalize_source_id(v): ...\n")

    with pytest.raises(ValueError, match="tests/time_helper_allowlist.json") as excinfo:
        parse_allowlist(raw, tmp_path)
    assert problem in str(excinfo.value)


def test_sorted_allowlist_entries_load(tmp_path: Path) -> None:
    _write(tmp_path, "services/x.py", "def _utcnow(): ...\ndef _format_time(v): ...\n")
    _write(tmp_path, "services/y.py", "async def _utcnow(): ...\n")

    entries = parse_allowlist(
        [_valid_entry(name="_format_time"), _valid_entry(), _valid_entry(file="services/y.py")], tmp_path
    )

    assert [(entry.file, entry.name) for entry in entries] == [
        ("services/x.py", "_format_time"),
        ("services/x.py", "_utcnow"),
        ("services/y.py", "_utcnow"),
    ]


def test_allowlist_file_that_is_not_json_fails_naming_the_file(tmp_path: Path) -> None:
    _write(tmp_path, ALLOWLIST_RELATIVE_PATH, "[{file: 1}]\n")

    with pytest.raises(ValueError, match="tests/time_helper_allowlist.json"):
        load_allowlist(tmp_path)
