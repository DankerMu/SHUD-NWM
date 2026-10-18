"""``Any`` annotation ratchet (OpenSpec change ``entropy-burndown-2026-10``, task 4.2).

The number of ``Any`` occurrences inside annotations under the production roots
may only go down.  This module counts them and compares the count with
``tests/any_ratchet_ceiling.json`` (exactly ``{"ceiling": <int>}``):

* ``count > ceiling`` fails -- someone added ``Any`` annotations; raising the
  ceiling is an explicit, reviewed ratchet change;
* ``count * 20 < ceiling * 19`` (more than 5 % below, integer arithmetic) fails
  too -- a cleanup must lower the ceiling in the same PR so the ratchet does not
  slacken.

What is counted (the test's own method is the definition):

* files: ``sorted(root.rglob("*.py"))`` for every root in ``_SCAN_ROOTS``
  (``tests/`` is not a root; nothing inside a root is filtered by file name),
  skipping any path whose repo-relative ``parts`` contain one of
  ``_EXCLUDED_DIR_PARTS``; a ``SyntaxError`` propagates;
* annotation sites, found by ``ast.walk`` over the whole module (nested
  functions and ``if TYPE_CHECKING:`` blocks included): the ``.annotation`` of
  every ``posonlyargs``/``args``/``kwonlyargs``/``vararg``/``kwarg``, every
  ``returns``, and every ``AnnAssign.annotation``;
* inside each annotation subtree, 1 per ``Name(id="Any")`` and 1 per
  ``Attribute(attr="Any")`` whatever its base (``typing.Any``, ``t.Any``,
  ``_chain.Any``), so ``dict[str, Any] | Any`` counts 2;
* every ``str`` constant inside an annotation subtree is parsed with
  ``ast.parse(value, mode="eval")`` and walked by the same rules, except that
  strings inside a ``Literal[...]`` subscript are never parsed; any other string
  that fails to parse counts 0.

Not counted: ``Any`` outside annotations (alias assignments such as
``X = Callable[..., Any]``, ``TypeAlias`` values, ``cast(Any, ...)``,
``isinstance``) and ``# type:`` comments.  Aliases are not expanded, so moving
``Any`` into an alias evades the count -- an accepted residual risk.

``scripts/select_ci_tests.py`` routes every changed ``*.py`` under the roots in
``_SCAN_ROOTS`` to this module (``ANY_RATCHET_ROOTS``) and the ceiling file to
it as well; a selector meta-test parses ``_SCAN_ROOTS`` out of this file and
asserts the two agree.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

CEILING_RELATIVE_PATH = "tests/any_ratchet_ceiling.json"

# The production roots this ratchet scans. The selector mirrors them as
# ANY_RATCHET_ROOTS; a meta-test keeps the two equal.
_SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)


@dataclass
class AnyCount:
    """``Any`` occurrences in annotations, split by node kind, plus string-annotation bookkeeping."""

    name: int = 0
    attribute: int = 0
    strings_parsed: int = 0
    strings_skipped_in_literal: int = 0
    strings_unparsable: int = 0
    per_file: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.name + self.attribute

    def per_root(self) -> dict[str, int]:
        roots: dict[str, int] = {}
        for file, count in self.per_file.items():
            root = file.split("/", 1)[0]
            roots[root] = roots.get(root, 0) + count
        return roots


def iter_python_sources(repo_root: Path) -> list[Path]:
    """Every ``*.py`` under the scanned roots of ``repo_root``, in a stable order."""

    files: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if any(part in _EXCLUDED_DIR_PARTS for part in path.relative_to(repo_root).parts):
                continue
            files.append(path)
    return files


def _is_literal(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Literal") or (
        isinstance(node, ast.Attribute) and node.attr == "Literal"
    )


def _count_in_annotation(node: ast.AST, tally: AnyCount, *, in_literal: bool = False) -> None:
    """Add every ``Any`` in the annotation subtree ``node`` to ``tally``."""

    if isinstance(node, ast.Name) and node.id == "Any":
        tally.name += 1
    elif isinstance(node, ast.Attribute) and node.attr == "Any":
        tally.attribute += 1
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        if in_literal:
            tally.strings_skipped_in_literal += 1
            return
        try:
            parsed = ast.parse(node.value, mode="eval")
        except (SyntaxError, ValueError):
            tally.strings_unparsable += 1
            return
        tally.strings_parsed += 1
        _count_in_annotation(parsed.body, tally)
        return
    elif isinstance(node, ast.Subscript) and _is_literal(node.value):
        _count_in_annotation(node.value, tally, in_literal=in_literal)
        _count_in_annotation(node.slice, tally, in_literal=True)
        return

    for child in ast.iter_child_nodes(node):
        _count_in_annotation(child, tally, in_literal=in_literal)


def annotation_sites(tree: ast.AST) -> list[ast.expr]:
    """Every annotation expression anywhere in ``tree``."""

    sites: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            params = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
            params.extend(arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None)
            sites.extend(param.annotation for param in params if param.annotation is not None)
            if node.returns is not None:
                sites.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            sites.append(node.annotation)
    return sites


def count_module(tree: ast.AST, tally: AnyCount) -> int:
    """Add the ``Any`` occurrences of one parsed module to ``tally``; return that module's count."""

    before = tally.total
    for site in annotation_sites(tree):
        _count_in_annotation(site, tally)
    return tally.total - before


def count_any(repo_root: Path) -> AnyCount:
    """``Any`` occurrences in annotations under the scanned roots of ``repo_root``.

    ``ast.parse`` errors propagate on purpose: a module the ratchet cannot parse
    is a module whose annotations it cannot count.
    """

    tally = AnyCount()
    for path in iter_python_sources(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        tally.per_file[path.relative_to(repo_root).as_posix()] = count_module(tree, tally)
    return tally


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


def load_ceiling(repo_root: Path) -> int:
    path = repo_root / CEILING_RELATIVE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CEILING_RELATIVE_PATH}: not valid JSON ({exc})") from exc
    return parse_ceiling(raw)


def ratchet_verdict(count: int, ceiling: int) -> str | None:
    """The failure message for ``(count, ceiling)``, or ``None`` when the ratchet holds.

    Integer arithmetic only: fails above the ceiling, and fails when the count is
    more than 5 % below it (``count * 20 < ceiling * 19``).
    """

    if count > ceiling:
        return (
            f"Any-annotation count {count} exceeds the ceiling {ceiling} in {CEILING_RELATIVE_PATH} "
            f"by {count - ceiling}: remove `Any` annotations; "
            "raising the ceiling is an explicit, reviewed ratchet change"
        )
    if count * 20 < ceiling * 19:
        return (
            f"Any-annotation count {count} is more than 5 % below the ceiling {ceiling}: "
            f'set "ceiling" to {count} in {CEILING_RELATIVE_PATH}'
        )
    return None


def check_ratchet(repo_root: Path) -> str | None:
    return ratchet_verdict(count_any(repo_root).total, load_ceiling(repo_root))


# --------------------------------------------------------------------------- #
# Real repository tree
# --------------------------------------------------------------------------- #


def test_repository_any_count_is_within_the_ratchet() -> None:
    verdict = check_ratchet(REPO_ROOT)
    assert verdict is None, verdict


def test_scan_roots_exist_and_are_scanned() -> None:
    # A renamed or missing root would make the ratchet vacuously lower.
    scanned = count_any(REPO_ROOT).per_file
    for root in _SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"scan root {root}/ is missing"
        assert any(file.startswith(f"{root}/") for file in scanned), f"no Python file scanned under {root}/"


# --------------------------------------------------------------------------- #
# The (count, ceiling) verdict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("count", "ceiling"),
    [(9500, 9500), (9025, 9500), (9499, 9500), (0, 0), (19, 20), (20, 20)],
)
def test_verdict_passes_within_five_percent_at_or_below_the_ceiling(count: int, ceiling: int) -> None:
    assert ratchet_verdict(count, ceiling) is None


def test_verdict_fails_above_the_ceiling_with_count_ceiling_excess_and_file() -> None:
    message = ratchet_verdict(9501, 9500)

    assert message == (
        "Any-annotation count 9501 exceeds the ceiling 9500 in tests/any_ratchet_ceiling.json by 1: "
        "remove `Any` annotations; raising the ceiling is an explicit, reviewed ratchet change"
    )
    assert ratchet_verdict(1, 0) is not None


def test_verdict_fails_more_than_five_percent_below_the_ceiling_asking_to_lower_it() -> None:
    message = ratchet_verdict(9024, 9500)

    assert message == (
        'Any-annotation count 9024 is more than 5 % below the ceiling 9500: '
        'set "ceiling" to 9024 in tests/any_ratchet_ceiling.json'
    )
    assert ratchet_verdict(18, 20) is not None
    # floor(count / 0.95) + 1 is the smallest ceiling the count no longer satisfies.
    assert ratchet_verdict(9500, 10000) is None
    assert ratchet_verdict(9500, 10001) is not None


# --------------------------------------------------------------------------- #
# The ceiling file shape
# --------------------------------------------------------------------------- #


def test_real_ceiling_file_is_exactly_one_int_key() -> None:
    raw = json.loads((REPO_ROOT / CEILING_RELATIVE_PATH).read_text(encoding="utf-8"))

    assert set(raw) == {"ceiling"}
    assert type(raw["ceiling"]) is int
    assert load_ceiling(REPO_ROOT) == raw["ceiling"] >= 0


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
    with pytest.raises(ValueError, match="tests/any_ratchet_ceiling.json"):
        parse_ceiling(raw)


def test_ceiling_file_that_is_not_json_fails_naming_the_file(tmp_path: Path) -> None:
    _write(tmp_path, CEILING_RELATIVE_PATH, "{ceiling: 3}\n")

    with pytest.raises(ValueError, match="tests/any_ratchet_ceiling.json"):
        load_ceiling(tmp_path)


def test_zero_ceiling_is_valid() -> None:
    assert parse_ceiling({"ceiling": 0}) == 0


# --------------------------------------------------------------------------- #
# Synthetic trees (tmp_path)
# --------------------------------------------------------------------------- #


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_ceiling(root: Path, ceiling: int) -> None:
    _write(root, CEILING_RELATIVE_PATH, json.dumps({"ceiling": ceiling}) + "\n")


def _module_with_any(n: int) -> str:
    lines = ["from typing import Any", ""]
    lines.extend(f"x{index}: Any = {index}" for index in range(n))
    return "\n".join(lines) + "\n"


def _count_source(tmp_path: Path, source: str) -> AnyCount:
    _write(tmp_path, "packages/m.py", source)
    return count_any(tmp_path)


def test_scenario_added_any_fails_with_count_and_ceiling(tmp_path: Path) -> None:
    # Spec "Added `Any` fails": one annotation beyond the ceiling.
    _write(tmp_path, "packages/common/a.py", _module_with_any(15))
    _write(tmp_path, "workers/w/b.py", _module_with_any(5))
    _write_ceiling(tmp_path, 20)
    assert check_ratchet(tmp_path) is None

    _write(tmp_path, "workers/w/b.py", _module_with_any(6))
    message = check_ratchet(tmp_path)

    assert message is not None
    assert "count 21" in message and "ceiling 20" in message and "by 1" in message
    assert CEILING_RELATIVE_PATH in message
    assert "raising the ceiling is an explicit, reviewed ratchet change" in message


def test_scenario_cleanup_must_lower_the_ceiling(tmp_path: Path) -> None:
    # Spec "Cleanup must lower the ceiling": 1 of 20 removed is exactly 5 % and
    # passes; 2 of 20 is more than 5 % and fails without a ceiling edit.
    _write(tmp_path, "services/s.py", _module_with_any(20))
    _write_ceiling(tmp_path, 20)

    _write(tmp_path, "services/s.py", _module_with_any(19))
    assert check_ratchet(tmp_path) is None

    _write(tmp_path, "services/s.py", _module_with_any(18))
    message = check_ratchet(tmp_path)

    assert message == (
        'Any-annotation count 18 is more than 5 % below the ceiling 20: '
        'set "ceiling" to 18 in tests/any_ratchet_ceiling.json'
    )
    _write_ceiling(tmp_path, 18)
    assert check_ratchet(tmp_path) is None


def test_name_and_attribute_any_both_count(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "import typing\nimport typing as t\nfrom typing import Any\nfrom services import chain as _chain\n"
        "a: Any\nb: typing.Any\nc: t.Any\nd: _chain.Any\n",
    )

    assert (tally.name, tally.attribute, tally.total) == (1, 3, 4)


def test_every_annotation_position_counts(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "from typing import Any\n"
        "def f(a: Any, /, b: Any, *args: Any, c: Any, **kwargs: Any) -> Any:\n"
        "    inner: Any = 1\n"
        "    return inner\n"
        "async def g(x: Any) -> Any: ...\n"
        "class C:\n"
        "    attr: Any\n"
        "    def method(self, y: Any) -> None:\n"
        "        def nested(z: Any) -> Any: ...\n"
        "lam = lambda q: q\n",
    )

    # f: 5 params + return + inner = 7; g: 2; C.attr: 1; method: 1; nested: 2.
    assert tally.total == 13


def test_nested_any_counts_each_occurrence(tmp_path: Path) -> None:
    tally = _count_source(tmp_path, "from typing import Any\nx: dict[str, Any] | Any\n")

    assert tally.total == 2


def test_string_annotations_are_parsed(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "from typing import Any\n"
        "a: 'Any'\n"
        "b: 'dict[str, Any]'\n"
        "c: list['typing.Any']\n"
        "def f(x: 'Callable[..., Any]') -> 'Any | None': ...\n",
    )

    assert (tally.name, tally.attribute) == (4, 1)
    assert tally.strings_parsed == 5


def test_literal_strings_are_not_parsed(tmp_path: Path) -> None:
    # `class` would raise on parse; strings inside Literal[...] are never parsed,
    # including a Literal written inside a string annotation.
    tally = _count_source(
        tmp_path,
        "import typing\nfrom typing import Any, Literal\n"
        "a: Literal['class', 'Any']\n"
        "b: typing.Literal['Any']\n"
        "c: \"Literal['Any'] | Any\"\n",
    )

    assert tally.total == 1
    assert tally.strings_skipped_in_literal == 4
    assert tally.strings_parsed == 1
    assert tally.strings_unparsable == 0


def test_unparsable_string_counts_zero_and_continues(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "from typing import Annotated, Any\n"
        "a: 'not valid ( Any'\n"
        "b: Annotated[Any, 'free text with Any in it']\n"
        "c: 'Any'\n",
    )

    assert tally.total == 2
    assert tally.strings_unparsable == 2
    assert tally.strings_parsed == 1


def test_any_outside_annotations_is_not_counted(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "from typing import Any, Callable, TypeAlias, cast\n"
        "Handler = Callable[..., Any]\n"
        "Alias: TypeAlias = dict[str, Any]\n"
        "value = cast(Any, 1)\n"
        "ok = isinstance(value, object) or Any is None\n"
        "y = 1  # type: Any\n"
        "def f(h: Handler) -> Alias:\n"
        "    return cast(Any, h)\n",
    )

    # `Alias: TypeAlias = ...` annotates with TypeAlias; its value is not an annotation.
    assert tally.total == 0


def test_type_checking_block_is_counted(tmp_path: Path) -> None:
    tally = _count_source(
        tmp_path,
        "from typing import TYPE_CHECKING, Any\n"
        "if TYPE_CHECKING:\n"
        "    x: Any\n"
        "    def f() -> Any: ...\n",
    )

    assert tally.total == 2


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
        _write(tmp_path, relative, _module_with_any(3))
    _write(tmp_path, "apps/api/conftest.py", _module_with_any(1))
    _write(tmp_path, "scripts/test_helper.py", _module_with_any(1))

    tally = count_any(tmp_path)

    # No file-name filtering inside a root: conftest.py / test_*.py under a root count.
    assert tally.per_file == {"apps/api/conftest.py": 1, "scripts/test_helper.py": 1}
    assert tally.total == 2


def test_per_root_breakdown_sums_to_the_total(tmp_path: Path) -> None:
    _write(tmp_path, "apps/a.py", _module_with_any(2))
    _write(tmp_path, "workers/b.py", _module_with_any(3))
    _write(tmp_path, "workers/c/d.py", _module_with_any(4))

    tally = count_any(tmp_path)

    assert tally.per_root() == {"apps": 2, "workers": 7}
    assert tally.total == 9


def test_adding_one_any_annotation_raises_the_count_by_one(tmp_path: Path) -> None:
    _write(tmp_path, "packages/m.py", "from typing import Any\n\ndef f(a: Any) -> int: ...\n")
    before = count_any(tmp_path).total

    _write(tmp_path, "packages/m.py", "from typing import Any\n\ndef f(a: Any) -> int: ...\nx: Any = 1\n")

    assert count_any(tmp_path).total == before + 1


def test_syntax_error_is_not_swallowed(tmp_path: Path) -> None:
    _write(tmp_path, "services/broken.py", "def broken(:\n")

    with pytest.raises(SyntaxError):
        count_any(tmp_path)
