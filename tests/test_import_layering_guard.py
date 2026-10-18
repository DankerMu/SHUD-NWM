"""Import layering guard (OpenSpec change ``entropy-burndown-2026-10``, task 1.1).

``packages/`` is the bottom layer and ``workers/`` sits on top of it.  This
module parses every ``*.py`` under ``packages/`` and ``workers/`` and fails on
an *upward* import -- an import whose target layer the importer's layer may not
depend on.  Exactly two layer rules exist:

* ``packages`` must not import ``workers``, ``services``, ``apps`` or ``scripts``;
* ``workers`` must not import ``services`` or ``apps`` (``workers -> scripts``
  is zero today and is deliberately neither forbidden nor widened here).

Only ``packages/`` and ``workers/`` are importers.  The six repository roots
``apps packages services workers scripts tests`` are used solely to decide
which layer an imported module belongs to, by its first dotted segment.

What counts as an import:

* every ``ast.Import`` / ``ast.ImportFrom`` reached by ``ast.walk`` over the
  whole module -- function bodies, ``if TYPE_CHECKING:`` blocks and
  ``try/except ImportError`` arms all count;
* relative imports (``level > 0``) are skipped: they never leave their own
  package, so they can never be upward;
* the recorded ``module`` is the dotted name as written: ``import workers.x.y
  as z`` records ``workers.x.y``, ``from workers.x import y`` records
  ``workers.x``, ``import a, workers.b`` records ``a`` and ``workers.b``
  separately;
* string annotations, ``importlib.import_module`` and ``__import__`` are out of
  scope.

``tests/import_layering_allowlist.json`` is the only tolerated exception: a
top-level list of ``{"file": <repo-relative POSIX path>, "module": <full
dotted module>}`` entries, one per ``(file, module)`` pair, sorted by that
pair.  It is checked in both directions -- an upward import missing from the
list fails, and an entry whose import no longer exists in that file's AST
(stale) fails too, as does any malformed entry.  A ``SyntaxError`` is left to
surface: a module the guard cannot parse is a module whose imports it cannot
see.

``scripts/select_ci_tests.py`` routes every changed ``*.py`` under the roots in
``_SCAN_ROOTS`` to this module (``IMPORT_LAYERING_GUARD_ROOTS``); a selector
meta-test parses ``_SCAN_ROOTS`` out of this file and asserts the two agree.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOWLIST_RELATIVE_PATH = "tests/import_layering_allowlist.json"

# The importer roots this guard scans. The selector mirrors them as
# IMPORT_LAYERING_GUARD_ROOTS; a meta-test keeps the two equal.
_SCAN_ROOTS: tuple[str, ...] = ("packages", "workers")

# The repository roots that name a layer. Used only to classify the TARGET of an
# import; tests/ and scripts/ are never scanned as importers.
_LAYER_NAMES: frozenset[str] = frozenset({"apps", "packages", "services", "workers", "scripts", "tests"})

# importer layer -> target layers it must not import.
_FORBIDDEN_TARGETS: Mapping[str, frozenset[str]] = {
    "packages": frozenset({"workers", "services", "apps", "scripts"}),
    "workers": frozenset({"services", "apps"}),
}

_EXCLUDED_DIR_PARTS: frozenset[str] = frozenset(
    {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
)

_ALLOWLIST_KEYS: frozenset[str] = frozenset({"file", "module"})


@dataclass(frozen=True, order=True)
class ImportSite:
    """One absolute import of ``module`` at ``lineno`` in ``file`` (repo-relative POSIX)."""

    file: str
    lineno: int
    module: str


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


def collect_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, module)`` for every absolute import anywhere in ``tree``."""

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0 or node.module is None:
                continue
            found.append((node.lineno, node.module))
    return sorted(found)


def scan_imports(repo_root: Path) -> dict[str, list[tuple[int, str]]]:
    """Every scanned file (repo-relative POSIX path) mapped to its absolute imports.

    ``ast.parse`` errors propagate on purpose (see the module docstring).
    """

    imports: dict[str, list[tuple[int, str]]] = {}
    for path in iter_python_sources(repo_root):
        text = path.read_text(encoding="utf-8")
        imports[path.relative_to(repo_root).as_posix()] = collect_imports(ast.parse(text, filename=str(path)))
    return imports


def is_upward(file: str, module: str) -> bool:
    """True iff ``file``'s layer may not import ``module``'s layer."""

    importer_layer = file.split("/", 1)[0]
    target_layer = module.split(".", 1)[0]
    if target_layer not in _LAYER_NAMES:
        return False
    return target_layer in _FORBIDDEN_TARGETS.get(importer_layer, frozenset())


def scan_upward_imports(repo_root: Path) -> list[ImportSite]:
    """Every upward import under the scanned roots of ``repo_root``."""

    return sorted(
        ImportSite(file, lineno, module)
        for file, imports in scan_imports(repo_root).items()
        for lineno, module in imports
        if is_upward(file, module)
    )


def load_allowlist(path: Path) -> object:
    """The raw parsed allowlist; shape is checked by :func:`compare`."""

    return json.loads(path.read_text(encoding="utf-8"))


def _entry_label(entry: object) -> str:
    return json.dumps(entry, sort_keys=True)


def compare(repo_root: Path, allowlist: object) -> list[str]:
    """Every layering violation of ``repo_root`` against ``allowlist``; empty means clean.

    Covers both directions -- upward imports the allowlist does not name, and
    allowlist entries that are malformed, duplicated, out of order, point at a
    missing file, name an import the file no longer has (stale), or name an
    import that is not upward.  Each message names the file and the import.
    """

    imports = scan_imports(repo_root)
    upward = scan_upward_imports(repo_root)
    problems: list[str] = []

    if not isinstance(allowlist, list):
        return [f"{ALLOWLIST_RELATIVE_PATH}: top level must be a list, got {type(allowlist).__name__}"]

    pairs: list[tuple[str, str]] = []
    for index, entry in enumerate(allowlist):
        label = f"{ALLOWLIST_RELATIVE_PATH}[{index}] {_entry_label(entry)}"
        if not isinstance(entry, dict) or set(entry) != _ALLOWLIST_KEYS:
            keys = sorted(entry) if isinstance(entry, dict) else type(entry).__name__
            problems.append(f"{label}: an entry must have exactly the keys ['file', 'module'], got {keys}")
            continue
        file, module = entry["file"], entry["module"]
        if not isinstance(file, str) or not isinstance(module, str) or not file or not module:
            problems.append(f"{label}: 'file' and 'module' must be non-empty strings")
            continue
        pairs.append((file, module))
        parts = file.split("/")
        if "\\" in file or file.startswith("/") or ".." in parts or parts[0] not in _SCAN_ROOTS:
            problems.append(
                f"{label}: 'file' must be a repo-relative POSIX path under {', '.join(f'{r}/' for r in _SCAN_ROOTS)}"
            )
            continue
        if file not in imports:
            problems.append(f"{label}: {file} does not exist as a scanned Python file")
            continue
        if module not in {imported for _, imported in imports[file]}:
            problems.append(f"{label}: stale entry -- {file} no longer imports {module}")
            continue
        if not is_upward(file, module):
            problems.append(f"{label}: {file} imports {module}, but that import is not upward and needs no entry")

    for pair, count in sorted(Counter(pairs).items()):
        if count > 1:
            problems.append(f"{ALLOWLIST_RELATIVE_PATH}: duplicate entry file={pair[0]} module={pair[1]} ({count}x)")

    for previous, current in zip(pairs, pairs[1:], strict=False):
        if current < previous:
            problems.append(
                f"{ALLOWLIST_RELATIVE_PATH}: entries are not sorted by (file, module): "
                f"file={current[0]} module={current[1]} follows file={previous[0]} module={previous[1]}"
            )
            break

    allowed = set(pairs)
    for site in upward:
        if (site.file, site.module) not in allowed:
            problems.append(
                f"{site.file}:{site.lineno}: upward import of {site.module} "
                f"({site.file.split('/', 1)[0]} -> {site.module.split('.', 1)[0]}) "
                f"is not in {ALLOWLIST_RELATIVE_PATH}"
            )
    return problems


# --------------------------------------------------------------------------- #
# Real repository tree
# --------------------------------------------------------------------------- #


def test_repository_has_no_upward_import_outside_the_allowlist() -> None:
    problems = compare(REPO_ROOT, load_allowlist(REPO_ROOT / ALLOWLIST_RELATIVE_PATH))
    assert problems == [], "import layering violations:\n" + "\n".join(problems)


def test_scan_roots_exist_and_are_scanned() -> None:
    # A renamed or missing root would make the guard vacuously green.
    scanned = scan_imports(REPO_ROOT)
    for root in _SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"scan root {root}/ is missing"
        assert any(file.startswith(f"{root}/") for file in scanned), f"no Python file scanned under {root}/"


# --------------------------------------------------------------------------- #
# Synthetic trees (tmp_path)
# --------------------------------------------------------------------------- #


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(file: str, module: str) -> dict[str, str]:
    return {"file": file, "module": module}


def test_new_upward_import_fails_naming_file_and_import(tmp_path: Path) -> None:
    _write(tmp_path, "packages/common/thing.py", "import os\n\nfrom workers.x import y\n")

    problems = compare(tmp_path, [])

    assert problems == [
        "packages/common/thing.py:3: upward import of workers.x (packages -> workers) "
        f"is not in {ALLOWLIST_RELATIVE_PATH}"
    ]


def test_allowlisted_upward_import_passes(tmp_path: Path) -> None:
    _write(tmp_path, "packages/common/thing.py", "from workers.x import y\nfrom workers.x import z\n")

    # Two statements, one (file, module) pair, one entry.
    assert compare(tmp_path, [_entry("packages/common/thing.py", "workers.x")]) == []


def test_stale_entry_fails_naming_the_entry(tmp_path: Path) -> None:
    _write(tmp_path, "packages/common/thing.py", "import os\n")

    problems = compare(tmp_path, [_entry("packages/common/thing.py", "workers.x")])

    assert len(problems) == 1
    assert "stale entry" in problems[0]
    assert "packages/common/thing.py" in problems[0]
    assert "workers.x" in problems[0]


def test_stale_comparison_is_exact_on_the_full_dotted_name(tmp_path: Path) -> None:
    # The file imports workers.x.y, not workers.x: the entry is stale AND the
    # real import is unlisted.
    _write(tmp_path, "packages/common/thing.py", "from workers.x.y import z\n")

    problems = compare(tmp_path, [_entry("packages/common/thing.py", "workers.x")])

    assert any("stale entry" in p and "workers.x" in p for p in problems), problems
    assert any(p.startswith("packages/common/thing.py:1: upward import of workers.x.y") for p in problems), problems


def test_downward_import_needs_no_entry(tmp_path: Path) -> None:
    _write(tmp_path, "packages/common/contracts/region.py", "import os\n")
    _write(
        tmp_path,
        "workers/data_adapters/region.py",
        "from packages.common.contracts.region import GeoBBox\n\n__all__ = ['GeoBBox']\n",
    )

    assert compare(tmp_path, []) == []


def test_layer_rules_are_exactly_the_two_pinned_rules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "packages/common/a.py",
        "import workers\nimport services.s\nimport apps.api\nimport scripts.t\n"
        "import packages.common.b\nimport tests.helpers\nimport numpy\n",
    )
    _write(
        tmp_path,
        "workers/w/a.py",
        "import services.s\nimport apps.api\nimport scripts.t\nimport packages.common.b\nimport workers.v\n",
    )
    # Not importers: scripts/ and tests/ are never scanned.
    _write(tmp_path, "scripts/t.py", "import apps.api\n")
    _write(tmp_path, "tests/test_x.py", "import services.s\n")

    found = {(site.file, site.module) for site in scan_upward_imports(tmp_path)}

    assert found == {
        ("packages/common/a.py", "workers"),
        ("packages/common/a.py", "services.s"),
        ("packages/common/a.py", "apps.api"),
        ("packages/common/a.py", "scripts.t"),
        ("workers/w/a.py", "services.s"),
        ("workers/w/a.py", "apps.api"),
    }


def test_import_node_rules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "packages/common/a.py",
        "from __future__ import annotations\n"
        "import typing\n"
        "import workers.x.y as z\n"  # full dotted name
        "import os, services.b\n"  # split per alias
        "from . import sibling\n"  # relative: skipped
        "from .rel import thing\n"  # relative: skipped
        "if typing.TYPE_CHECKING:\n"
        "    from apps.api import main\n"
        "try:\n"
        "    from scripts.s import t\n"
        "except ImportError:\n"
        "    pass\n"
        "def f():\n"
        "    from workers.inner import g\n"
        "    return g\n"
        "x: 'workers.Str'\n"  # string annotation: out of scope
        "import importlib\n"
        "importlib.import_module('workers.dyn')\n"  # dynamic: out of scope
        "__import__('services.dyn')\n"
        "from .workers import x\n",  # relative with a layer-named module: still skipped
    )

    found = sorted((site.lineno, site.module) for site in scan_upward_imports(tmp_path))

    assert found == [
        (3, "workers.x.y"),
        (4, "services.b"),
        (8, "apps.api"),
        (10, "scripts.s"),
        (14, "workers.inner"),
    ]


def test_excluded_directories_are_not_scanned(tmp_path: Path) -> None:
    _write(tmp_path, "packages/__pycache__/a.py", "import workers\n")
    _write(tmp_path, "workers/.venv/lib/a.py", "import services\n")

    assert scan_upward_imports(tmp_path) == []


def test_syntax_error_is_not_swallowed(tmp_path: Path) -> None:
    _write(tmp_path, "packages/common/broken.py", "def broken(:\n")

    try:
        scan_imports(tmp_path)
    except SyntaxError:
        return
    raise AssertionError("a SyntaxError under packages/ was swallowed")


def _malformed_problems(tmp_path: Path, allowlist: object) -> list[str]:
    _write(tmp_path, "packages/common/a.py", "from workers.x import y\n")
    _write(tmp_path, "packages/common/b.py", "from workers.x import y\nfrom packages.common.a import q\n")
    return compare(tmp_path, allowlist)


def test_well_formed_allowlist_is_clean(tmp_path: Path) -> None:
    allowlist = [_entry("packages/common/a.py", "workers.x"), _entry("packages/common/b.py", "workers.x")]

    assert _malformed_problems(tmp_path, allowlist) == []


def test_malformed_allowlist_duplicate_pair(tmp_path: Path) -> None:
    allowlist = [
        _entry("packages/common/a.py", "workers.x"),
        _entry("packages/common/a.py", "workers.x"),
        _entry("packages/common/b.py", "workers.x"),
    ]

    assert _malformed_problems(tmp_path, allowlist) == [
        f"{ALLOWLIST_RELATIVE_PATH}: duplicate entry file=packages/common/a.py module=workers.x (2x)"
    ]


def test_malformed_allowlist_extra_key(tmp_path: Path) -> None:
    allowlist = [
        {"file": "packages/common/a.py", "module": "workers.x", "reason": "x"},
        _entry("packages/common/b.py", "workers.x"),
    ]

    problems = _malformed_problems(tmp_path, allowlist)

    assert len(problems) == 2, problems
    assert "exactly the keys ['file', 'module']" in problems[0]
    assert "packages/common/a.py" in problems[0] and "workers.x" in problems[0]
    # The malformed entry allows nothing, so the import it meant to cover is reported.
    assert problems[1].startswith("packages/common/a.py:1: upward import of workers.x")


def test_malformed_allowlist_unsorted(tmp_path: Path) -> None:
    allowlist = [_entry("packages/common/b.py", "workers.x"), _entry("packages/common/a.py", "workers.x")]

    assert _malformed_problems(tmp_path, allowlist) == [
        f"{ALLOWLIST_RELATIVE_PATH}: entries are not sorted by (file, module): "
        "file=packages/common/a.py module=workers.x follows file=packages/common/b.py module=workers.x"
    ]


def test_malformed_allowlist_missing_file(tmp_path: Path) -> None:
    allowlist = [
        _entry("packages/common/a.py", "workers.x"),
        _entry("packages/common/b.py", "workers.x"),
        _entry("packages/common/gone.py", "workers.x"),
    ]

    problems = _malformed_problems(tmp_path, allowlist)

    assert len(problems) == 1, problems
    assert "packages/common/gone.py does not exist" in problems[0]


def test_malformed_allowlist_file_outside_scanned_roots(tmp_path: Path) -> None:
    _write(tmp_path, "services/s.py", "import apps.api\n")
    allowlist = [
        _entry("packages/common/a.py", "workers.x"),
        _entry("packages/common/b.py", "workers.x"),
        _entry("services/s.py", "apps.api"),
    ]

    problems = _malformed_problems(tmp_path, allowlist)

    assert len(problems) == 1, problems
    assert "services/s.py" in problems[0] and "under packages/, workers/" in problems[0]


def test_malformed_allowlist_entry_not_upward(tmp_path: Path) -> None:
    allowlist = [
        _entry("packages/common/a.py", "workers.x"),
        _entry("packages/common/b.py", "packages.common.a"),
        _entry("packages/common/b.py", "workers.x"),
    ]

    problems = _malformed_problems(tmp_path, allowlist)

    assert problems == [
        f"{ALLOWLIST_RELATIVE_PATH}[1] "
        '{"file": "packages/common/b.py", "module": "packages.common.a"}: '
        "packages/common/b.py imports packages.common.a, but that import is not upward and needs no entry"
    ]


def test_malformed_allowlist_top_level_not_a_list(tmp_path: Path) -> None:
    problems = _malformed_problems(tmp_path, {"file": "packages/common/a.py", "module": "workers.x"})

    assert problems == [f"{ALLOWLIST_RELATIVE_PATH}: top level must be a list, got dict"]
