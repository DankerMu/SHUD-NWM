"""Duplicate status literal scan (entropy-burndown task 2.1b).

Every status literal outside ``packages/common/status_vocab.py`` must be
adjudicated in ``tests/status_literal_allowlist.json``. Split out of
``tests/test_status_vocab_parity.py`` (#3026), which keeps the canonical parity
pins of task 2.1a; the scan rule below is unchanged.
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from packages.common import status_vocab as vocab

_REPO_ROOT = Path(__file__).resolve().parents[1]

# --- Duplicate status literal scan (entropy-burndown task 2.1b) ----------------------
#
# Scan rule. Every `ast.Set` and `ast.Tuple` node anywhere in a module (`ast.walk`;
# `ast.List` is not scanned, and `frozenset({...})` / `frozenset((...))` count once,
# through their inner literal). A literal's members are its `str` constant elements,
# case-sensitive; starred elements and every other non-`str` element are ignored and
# the constants are still compared (so `{*BASE, "succeeded", "failed"}` is scanned as
# `{"failed", "succeeded"}`). The literal is a hit when, for some vocabulary set V,
# it shares at least two members with V and equals, is a subset of, or is a superset
# of V; or when it contains all of `succeeded`, `complete` and `published`. The
# vocabulary is every public `frozenset`/`tuple` of `str` in `status_vocab`.
#
# Scanned files: git-tracked `*.py` under the five roots below, minus any path with
# a `tests` component and minus `packages/common/status_vocab.py` itself. A hit is
# keyed by `(file, sorted members)`, so a line move never stales an entry.

_STATUS_LITERAL_ALLOWLIST = "tests/status_literal_allowlist.json"
_STATUS_LITERAL_SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")
_STATUS_LITERAL_SCAN_EXCLUDED_FILES = frozenset({"packages/common/status_vocab.py"})
_STATUS_LITERAL_REASONS = frozenset({"pending-migration", "distinct-vocabulary", "not-a-status-set"})
_STATUS_LITERAL_ENTRY_KEYS = frozenset({"file", "members", "reason"})
_SUCCESS_TRIPLE = frozenset({"succeeded", "complete", "published"})


def _scanned_vocabulary() -> dict[str, frozenset[str]]:
    return {
        name: frozenset(value)
        for name, value in vars(vocab).items()
        if not name.startswith("_")
        and isinstance(value, (frozenset, tuple))
        and value
        and all(isinstance(member, str) for member in value)
    }


def _status_literal_matches(members: frozenset[str], vocabulary: dict[str, frozenset[str]]) -> list[str]:
    """The vocabulary names (or the success-triple marker) that make ``members`` a hit."""

    reasons = sorted(
        name
        for name, value in vocabulary.items()
        if len(members & value) >= 2 and (members <= value or members >= value)
    )
    if _SUCCESS_TRIPLE <= members:
        reasons.append("{succeeded, complete, published}")
    return reasons


def _literal_str_members(node: ast.Set | ast.Tuple) -> frozenset[str]:
    return frozenset(
        element.value for element in node.elts if isinstance(element, ast.Constant) and isinstance(element.value, str)
    )


def _is_scanned_status_file(path: str) -> bool:
    parts = path.split("/")
    return (
        path.endswith(".py")
        and parts[0] in _STATUS_LITERAL_SCAN_ROOTS
        and "tests" not in parts
        and path not in _STATUS_LITERAL_SCAN_EXCLUDED_FILES
    )


def _tracked_status_scan_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *_STATUS_LITERAL_SCAN_ROOTS],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return sorted(path for path in result.stdout.decode("utf-8").split("\0") if path and _is_scanned_status_file(path))


def scan_status_literals(repo_root: Path, files: list[str]) -> list[tuple[str, int, tuple[str, ...], list[str]]]:
    """``(file, line, sorted members, matched vocabulary names)`` for every hit, in file/line order."""

    vocabulary = _scanned_vocabulary()
    hits: list[tuple[str, int, tuple[str, ...], list[str]]] = []
    for file in files:
        tree = ast.parse((repo_root / file).read_text(encoding="utf-8"), filename=file)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Set, ast.Tuple)):
                continue
            members = _literal_str_members(node)
            matched = _status_literal_matches(members, vocabulary)
            if matched:
                hits.append((file, node.lineno, tuple(sorted(members)), matched))
    return sorted(hits)


def compare_status_literals(repo_root: Path, files: list[str], allowlist: object) -> list[str]:
    """Every problem of the scanned ``files`` against ``allowlist``; empty means clean.

    Both directions: a hit the allowlist does not name (``file:line``), and an
    allowlist entry that is malformed, duplicated, out of order, names a file that
    is not scanned, or names members no hit in that file has (stale; reported as
    ``tests/status_literal_allowlist.json[<index>]`` plus the file, since a stale
    entry has no source line).
    """

    if not isinstance(allowlist, list):
        return [f"{_STATUS_LITERAL_ALLOWLIST}: top level must be a list, got {type(allowlist).__name__}"]
    hits = scan_status_literals(repo_root, files)
    hit_keys = {(file, members) for file, _, members, _ in hits}
    scanned = set(files)
    problems: list[str] = []
    keys: list[tuple[str, tuple[str, ...]]] = []
    for index, entry in enumerate(allowlist):
        label = f"{_STATUS_LITERAL_ALLOWLIST}[{index}] {json.dumps(entry, sort_keys=True)}"
        if not isinstance(entry, dict) or set(entry) != _STATUS_LITERAL_ENTRY_KEYS:
            got = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
            problems.append(f"{label}: an entry must have exactly the keys ['file', 'members', 'reason'], got {got}")
            continue
        file, members, reason = entry["file"], entry["members"], entry["reason"]
        if not isinstance(file, str) or not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            problems.append(f"{label}: 'file' must be a string and 'members' a list of strings")
            continue
        if reason not in _STATUS_LITERAL_REASONS:
            problems.append(f"{label}: reason must be one of {sorted(_STATUS_LITERAL_REASONS)}")
        if members != sorted(set(members)):
            problems.append(f"{label}: 'members' must be sorted and free of repeats")
        key = (file, tuple(members))
        keys.append(key)
        if file not in scanned:
            problems.append(f"{label}: stale entry -- {file} is not a scanned Python file")
        elif key not in hit_keys:
            problems.append(f"{label}: stale entry -- no status literal in {file} has the members {members}")
    for key, count in sorted(Counter(keys).items()):
        if count > 1:
            problems.append(f"{_STATUS_LITERAL_ALLOWLIST}: duplicate entry file={key[0]} members={list(key[1])}")
    for previous, current in zip(keys, keys[1:], strict=False):
        if current < previous:
            problems.append(
                f"{_STATUS_LITERAL_ALLOWLIST}: entries are not sorted by (file, members): "
                f"{current[0]} {list(current[1])} follows {previous[0]} {list(previous[1])}"
            )
            break
    allowed = set(keys)
    for file, line, members, matched in hits:
        if (file, members) not in allowed:
            problems.append(
                f"{file}:{line}: status literal {list(members)} matches {matched} "
                f"and is not in {_STATUS_LITERAL_ALLOWLIST}"
            )
    return problems


def test_repository_status_literals_match_the_allowlist() -> None:
    allowlist = json.loads((_REPO_ROOT / _STATUS_LITERAL_ALLOWLIST).read_text(encoding="utf-8"))
    problems = compare_status_literals(_REPO_ROOT, _tracked_status_scan_files(_REPO_ROOT), allowlist)
    assert not problems, "\n".join(problems)


def test_status_literal_scan_covers_its_roots_and_skips_tests_and_the_vocabulary() -> None:
    files = _tracked_status_scan_files(_REPO_ROOT)
    assert "services/orchestrator/chain_types.py" in files
    assert "apps/api/routes/pipeline.py" in files
    assert "packages/common/status_vocab.py" not in files
    assert not [path for path in files if "tests" in path.split("/")]
    assert {path.split("/", 1)[0] for path in files} == set(_STATUS_LITERAL_SCAN_ROOTS)


def _write_sources(root: Path, sources: dict[str, str]) -> list[str]:
    for relative, text in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(sources)


def _status_entry(file: str, members: list[str], reason: str = "distinct-vocabulary") -> dict[str, object]:
    return {"file": file, "members": members, "reason": reason}


def test_status_literal_scan_names_an_unlisted_copy_by_file_and_line(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {"services/x/mod.py": 'import os\n\nTERMINAL = frozenset({"succeeded", "failed", "cancelled"})\n'},
    )
    problems = compare_status_literals(tmp_path, files, [])
    assert len(problems) == 1, problems
    assert problems[0].startswith("services/x/mod.py:3: status literal ['cancelled', 'failed', 'succeeded'] matches ")
    assert "PIPELINE_JOB_TERMINAL" in problems[0]
    entry = _status_entry("services/x/mod.py", ["cancelled", "failed", "succeeded"])
    assert compare_status_literals(tmp_path, files, [entry]) == []


def test_status_literal_scan_rules(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {
            "services/x/rules.py": (
                'AS_LIST = ["succeeded", "failed", "cancelled"]\n'  # List: not scanned
                'ONCE = frozenset({"pending", "queued"})\n'  # one hit, not two
                'UPPER = ("PASS", "BLOCKED")\n'  # case-sensitive: no vocabulary member
                'MIXED = {"Succeeded", "FAILED", "cancelled"}\n'  # one lowercase member only
                'STARRED = {*BASE, "submitted", "running"}\n'  # starred ignored, constants compared
                'SUCCESS = ("succeeded", "complete", "published", "other")\n'  # success triple
                'SINGLE = {"COMPLETED"}\n'  # one member in common: never a hit
                'UNRELATED = ("alpha", "pending")\n'  # one member in common: not a hit
                'MAPPING = {"succeeded": 1, "failed": 2}\n'  # dict keys: not a Set/Tuple
            )
        },
    )
    assert [(line, members) for _, line, members, _ in scan_status_literals(tmp_path, files)] == [
        (2, ("pending", "queued")),
        (5, ("running", "submitted")),
        (6, ("complete", "other", "published", "succeeded")),
    ]


def test_status_literal_scan_superset_and_subset_both_hit(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {
            "workers/x/sets.py": (
                'SUPER = {"pending", "queued", "unrelated"}\n'
                'SUB = ("parsed", "published")\n'
                'OVERLAP = {"pending", "parsed", "unrelated"}\n'  # neither subset nor superset of any set
            )
        },
    )
    hits = scan_status_literals(tmp_path, files)
    assert [(line, members) for _, line, members, _ in hits] == [
        (1, ("pending", "queued", "unrelated")),
        (2, ("parsed", "published")),
    ]
    assert "PIPELINE_JOB_PENDING" in hits[0][3]
    assert "DISPLAY_PRODUCT_READY_STATUSES" in hits[1][3]


def test_status_literal_allowlist_stale_entries_fail(tmp_path: Path) -> None:
    files = _write_sources(tmp_path, {"apps/x/mod.py": 'S = {"pending", "queued"}\n'})
    problems = compare_status_literals(
        tmp_path,
        files,
        [
            _status_entry("apps/x/gone.py", ["pending", "queued"]),
            _status_entry("apps/x/mod.py", ["pending", "queued"]),
            _status_entry("apps/x/mod.py", ["pending", "queued", "running"]),
        ],
    )
    assert problems == [
        f'{_STATUS_LITERAL_ALLOWLIST}[0] {{"file": "apps/x/gone.py", "members": ["pending", "queued"], '
        '"reason": "distinct-vocabulary"}: stale entry -- apps/x/gone.py is not a scanned Python file',
        f'{_STATUS_LITERAL_ALLOWLIST}[2] {{"file": "apps/x/mod.py", "members": ["pending", "queued", "running"], '
        '"reason": "distinct-vocabulary"}: stale entry -- no status literal in apps/x/mod.py has the members '
        "['pending', 'queued', 'running']",
    ]


@pytest.mark.parametrize(
    ("allowlist", "expected"),
    [
        pytest.param({"file": "x"}, "top level must be a list, got dict", id="not-a-list"),
        pytest.param(
            [{"file": "apps/x/mod.py", "members": ["pending", "queued"], "reason": "distinct-vocabulary", "line": 1}],
            "got ['file', 'line', 'members', 'reason']",
            id="extra-key",
        ),
        pytest.param(
            [{"file": "apps/x/mod.py", "members": ["pending", "queued"]}],
            "an entry must have exactly the keys ['file', 'members', 'reason'], got ['file', 'members']",
            id="missing-key",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", ["pending", "queued"], reason="migrate-later")],
            "reason must be one of ['distinct-vocabulary', 'not-a-status-set', 'pending-migration']",
            id="bad-reason",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", ["queued", "pending"])],
            "'members' must be sorted and free of repeats",
            id="unsorted-members",
        ),
        pytest.param(
            [_status_entry("apps/x/mod.py", "pending,queued")],  # type: ignore[arg-type]
            "'members' a list of strings",
            id="members-not-a-list",
        ),
        pytest.param(
            [
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
            ],
            "duplicate entry file=apps/x/mod.py members=['pending', 'queued']",
            id="duplicate",
        ),
        pytest.param(
            [
                _status_entry("apps/x/mod.py", ["running", "submitted"]),
                _status_entry("apps/x/mod.py", ["pending", "queued"]),
            ],
            "entries are not sorted by (file, members): apps/x/mod.py ['pending', 'queued'] follows "
            "apps/x/mod.py ['running', 'submitted']",
            id="unsorted-list",
        ),
    ],
)
def test_malformed_status_literal_allowlist_fails(tmp_path: Path, allowlist: object, expected: str) -> None:
    files = _write_sources(tmp_path, {"apps/x/mod.py": 'A = {"pending", "queued"}\nB = {"submitted", "running"}\n'})
    problems = compare_status_literals(tmp_path, files, allowlist)
    assert any(expected in problem for problem in problems), problems


def test_well_formed_status_literal_allowlist_is_clean(tmp_path: Path) -> None:
    files = _write_sources(
        tmp_path,
        {"apps/x/mod.py": 'A = {"pending", "queued"}\nB = {"submitted", "running"}\nC = ("queued", "pending")\n'},
    )
    allowlist = [
        _status_entry("apps/x/mod.py", ["pending", "queued"], reason="pending-migration"),
        _status_entry("apps/x/mod.py", ["running", "submitted"], reason="not-a-status-set"),
    ]
    assert compare_status_literals(tmp_path, files, allowlist) == []
