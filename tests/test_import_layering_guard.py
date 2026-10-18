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

Task 1.3 adds a second, independent check: the contract check.  It scans every
``*.py`` under ``_CONTRACT_SCAN_ROOTS`` (all six roots, tests included) and
fails on any use of a retired contract module (``_RETIRED_MODULES``) or of a
moved name at its old home (``_MOVED_NAMES``) -- see
:func:`collect_contract_violations` for the exact forms.  Unlike the layering
scan it resolves relative imports, and it has no allowlist.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
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


def iter_python_sources(repo_root: Path, roots: tuple[str, ...] = _SCAN_ROOTS) -> list[Path]:
    """Every ``*.py`` under ``roots`` (default: the layering scan roots) of ``repo_root``, in a stable order."""

    files: list[Path] = []
    for root_name in roots:
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


# --------------------------------------------------------------------------- #
# Contract check (task 1.3): the old homes of the contract layer stay retired
# --------------------------------------------------------------------------- #

# Task 1.2 moved the contract surfaces into ``packages/common/contracts/``; 1.3
# deleted the three pure shims and narrowed ``base.py`` / ``rewrite.py`` to what
# they use themselves.  This check keeps every old spelling out of the tree.
# It is a separate constant from ``_SCAN_ROOTS`` on purpose: the selector's
# meta-test parses ``_SCAN_ROOTS`` and must keep routing only packages/workers.
_CONTRACT_SCAN_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts", "tests")

# Whole modules that no longer exist -> the contract module that replaced them.
_RETIRED_MODULES: Mapping[str, str] = {
    "workers.forcing_producer.direct_grid_contract": "packages.common.contracts.direct_grid",
    "workers.data_adapters.region": "packages.common.contracts.region",
    "services.orchestrator.time_consistency": "packages.common.contracts.time_normalization",
}

# Modules that still exist but no longer own some names -> (contract module,
# moved names).  ``base.py`` still imports ``parse_cycle_time`` for its own use
# and ``rewrite.py`` still imports seven names for its own use, so the old
# spellings keep working at runtime -- only this check catches them.  The five
# private names ``rewrite`` imports back (``_sha256_file`` & co.) are its own
# seams and are not listed.
_MOVED_NAMES: Mapping[str, tuple[str, frozenset[str]]] = {
    "workers.data_adapters.base": (
        "packages.common.contracts.time_normalization",
        frozenset(
            {
                "cycle_id_for",
                "ensure_utc",
                "format_cycle_time",
                "generate_segmented_forecast_hours",
                "parse_cycle_date",
                "parse_cycle_time",
                "parse_resolution_segments",
                "valid_time_for",
                "validate_forecast_hours",
            }
        ),
    ),
    "workers.mapping_builder.rewrite": (
        "packages.common.contracts.hydrologic_fingerprint",
        frozenset(
            {
                "HYDROLOGIC_CORE_FINGERPRINT_LABELS",
                "NON_SP_ATT_CATEGORIES",
                "STATE_COMPATIBILITY_SURFACES",
                "HydrologicCoreFingerprint",
                "HydrologicCoreFingerprintMismatchError",
                "MissingCategoryError",
                "MissingPackageFileError",
                "SpAttRewriteError",
                "UnknownCategoryError",
                "compute_hydrologic_core_fingerprint",
                "verify_hydrologic_core_fingerprint_equal",
            }
        ),
    ),
}

# Call names whose first constant-string argument is a dotted patch target.
_PATCH_CALL_NAMES: frozenset[str] = frozenset({"patch", "setattr"})


@dataclass(frozen=True, order=True)
class ContractSite:
    """One use of a retired module or a moved name at ``lineno`` in ``file``."""

    file: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.file}:{self.lineno}: {self.detail}"


def resolve_import_from(file: str, node: ast.ImportFrom) -> str | None:
    """The absolute module a ``from ... import`` in ``file`` (repo-relative) reads from.

    Relative imports resolve against the package that contains ``file`` (for an
    ``__init__.py`` that is its own directory); ``None`` when the relative
    import climbs above the repository root.
    """

    if node.level == 0:
        return node.module
    package = file.removesuffix(".py").split("/")[:-1]
    climb = node.level - 1
    if climb > len(package):
        return None
    parts = package[: len(package) - climb]
    if node.module:
        parts = [*parts, *node.module.split(".")]
    return ".".join(parts) or None


def _retired_module(module: str) -> str | None:
    for retired in _RETIRED_MODULES:
        if module == retired or module.startswith(f"{retired}."):
            return retired
    return None


def _moved_target(dotted: str) -> tuple[str, str] | None:
    """``(old module, name)`` when ``dotted`` spells a moved name at its old home."""

    module, _, name = dotted.rpartition(".")
    if module in _MOVED_NAMES and name in _MOVED_NAMES[module][1]:
        return module, name
    return None


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _import_module_target(node: ast.AST) -> str | None:
    """The constant module name of an ``importlib.import_module("<name>")`` call."""

    if not isinstance(node, ast.Call) or _call_name(node) != "import_module" or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _retired_message(spelling: str, retired: str) -> str:
    return f"{spelling} uses retired module {retired}; import from {_RETIRED_MODULES[retired]}"


def _moved_message(spelling: str, module: str, name: str) -> str:
    return f"{spelling} reads {name} from {module}; import it from {_MOVED_NAMES[module][0]}"


def collect_contract_violations(file: str, tree: ast.AST) -> list[ContractSite]:
    """Every retired-module or moved-name use in one module's AST.

    Forms: ``import`` / ``from`` (relative imports resolved against ``file``),
    ``from parent import leaf``, aliases bound by ``import X as Y`` / ``from P
    import leaf as Y`` / ``Y = importlib.import_module("<old>")`` followed by
    ``Y.<moved>``, the dotted chain ``workers.data_adapters.base.<moved>``,
    ``importlib.import_module("<old>")`` itself and its ``.<moved>`` chain, and
    constant ``patch`` / ``setattr`` targets ``"<old>.<moved>"``.  Dynamic
    ``getattr(m, var)``, docstrings and comments are out of scope.
    """

    found: set[ContractSite] = set()
    module_aliases: dict[str, str] = {}

    def flag(node: ast.AST, detail: str) -> None:
        found.add(ContractSite(file, getattr(node, "lineno", 0), detail))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                retired = _retired_module(alias.name)
                if retired is not None:
                    flag(node, _retired_message(f"import {alias.name}", retired))
                elif alias.name in _MOVED_NAMES and alias.asname:
                    module_aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = resolve_import_from(file, node)
            if module is None:
                continue
            retired = _retired_module(module)
            if retired is not None:
                flag(node, _retired_message(f"from {module} import ...", retired))
                continue
            for alias in node.names:
                leaf = f"{module}.{alias.name}"
                retired = _retired_module(leaf)
                if retired is not None:
                    flag(node, _retired_message(f"from {module} import {alias.name}", retired))
                elif leaf in _MOVED_NAMES:
                    module_aliases[alias.asname or alias.name] = leaf
                elif _moved_target(leaf) is not None:
                    flag(node, _moved_message(f"from {module} import {alias.name}", module, alias.name))
        elif isinstance(node, ast.Assign):
            target = _import_module_target(node.value)
            if target in _MOVED_NAMES:
                for bound in node.targets:
                    if isinstance(bound, ast.Name):
                        module_aliases[bound.id] = target
        elif isinstance(node, ast.Call):
            target = _import_module_target(node)
            retired = _retired_module(target) if target is not None else None
            if retired is not None:
                flag(node, _retired_message(f'importlib.import_module("{target}")', retired))
            if _call_name(node) in _PATCH_CALL_NAMES:
                strings = [*node.args[:1], *(kw.value for kw in node.keywords if kw.arg == "target")]
                for value in strings:
                    if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                        continue
                    retired = _retired_module(value.value)
                    moved = _moved_target(value.value)
                    if retired is not None:
                        flag(node, _retired_message(f'patch target "{value.value}"', retired))
                    elif moved is not None:
                        flag(node, _moved_message(f'patch target "{value.value}"', *moved))

    # Attribute reads need every alias of the file first (ast.walk is breadth-first).
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        owner: str | None = None
        if isinstance(node.value, ast.Name) and node.value.id in module_aliases:
            owner = module_aliases[node.value.id]
        elif _dotted_name(node.value) in _MOVED_NAMES:
            owner = _dotted_name(node.value)
        elif _import_module_target(node.value) in _MOVED_NAMES:
            owner = _import_module_target(node.value)
        if owner is not None and node.attr in _MOVED_NAMES[owner][1]:
            spelling = _dotted_name(node) or f"<{owner}>.{node.attr}"
            flag(node, _moved_message(spelling, owner, node.attr))
    return sorted(found)


def scan_contract_violations(repo_root: Path) -> list[ContractSite]:
    """Every retired-module or moved-name use under ``_CONTRACT_SCAN_ROOTS``.

    A retired module that exists again as a file is a violation by itself.
    ``ast.parse`` errors propagate, as for the layering scan.
    """

    found: list[ContractSite] = []
    for retired, replacement in _RETIRED_MODULES.items():
        stem = retired.replace(".", "/")
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if (repo_root / candidate).exists():
                found.append(ContractSite(candidate, 0, f"retired module {retired} exists again; use {replacement}"))
    for path in iter_python_sources(repo_root, _CONTRACT_SCAN_ROOTS):
        file = path.relative_to(repo_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(collect_contract_violations(file, tree))
    return sorted(found)


def test_repository_has_no_retired_contract_path() -> None:
    violations = scan_contract_violations(REPO_ROOT)
    assert violations == [], "retired contract paths in use:\n" + "\n".join(map(str, violations))


def test_contract_scan_roots_exist_and_are_scanned() -> None:
    # A renamed or missing root would make the contract check vacuously green.
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in iter_python_sources(REPO_ROOT, _CONTRACT_SCAN_ROOTS)}
    for root in _CONTRACT_SCAN_ROOTS:
        assert (REPO_ROOT / root).is_dir(), f"contract scan root {root}/ is missing"
        assert any(file.startswith(f"{root}/") for file in scanned), f"no Python file scanned under {root}/"


def test_retired_modules_are_gone() -> None:
    for retired in _RETIRED_MODULES:
        stem = REPO_ROOT / retired.replace(".", "/")
        assert not stem.with_suffix(".py").exists(), f"{retired} exists again"
        assert not (stem / "__init__.py").exists(), f"{retired} exists again as a package"
        assert importlib.util.find_spec(retired) is None, f"{retired} is importable again"


def test_narrowed_modules_do_not_reexport_moved_names() -> None:
    for module_name, (_contract, moved) in _MOVED_NAMES.items():
        exported = set(importlib.import_module(module_name).__all__)
        assert not exported & moved, f"{module_name}.__all__ re-exports moved names: {sorted(exported & moved)}"


def test_moved_names_and_replacements_live_in_their_contract_modules() -> None:
    # Anti-vacuity: a misspelled moved name would never match and never fail.
    for contract in _RETIRED_MODULES.values():
        assert importlib.util.find_spec(contract) is not None, contract
    for module_name, (contract, moved) in _MOVED_NAMES.items():
        contract_module = importlib.import_module(contract)
        missing = sorted(name for name in moved if not hasattr(contract_module, name))
        assert missing == [], f"{module_name} -> {contract}: moved names missing from the contract: {missing}"


_CONTRACT_RED_SAMPLES: tuple[tuple[str, str, str], ...] = (
    # (file, source, expected detail fragment)
    (
        "services/s.py",
        "from workers.data_adapters.base import ensure_utc\n",
        "reads ensure_utc from workers.data_adapters.base",
    ),
    (
        "workers/data_adapters/x.py",
        "from .base import cycle_id_for\n",
        "from workers.data_adapters.base import cycle_id_for reads cycle_id_for from workers.data_adapters.base",
    ),
    (
        "tests/test_x.py",
        "import workers.data_adapters.base as b\n\nvalue = b.parse_cycle_time\n",
        "b.parse_cycle_time reads parse_cycle_time from workers.data_adapters.base",
    ),
    (
        "tests/test_x.py",
        'import importlib\n\nregion = importlib.import_module("workers.data_adapters.region")\n',
        "uses retired module workers.data_adapters.region",
    ),
    (
        "scripts/s.py",
        "from workers.mapping_builder.rewrite import SpAttRewriteError\n",
        "reads SpAttRewriteError from workers.mapping_builder.rewrite",
    ),
)


def test_contract_check_red_samples_name_file_and_line(tmp_path: Path) -> None:
    for index, (file, source, fragment) in enumerate(_CONTRACT_RED_SAMPLES):
        root = tmp_path / str(index)
        _write(root, file, source)
        violations = scan_contract_violations(root)
        assert len(violations) == 1, (source, violations)
        site = violations[0]
        assert site.file == file, (source, site)
        assert site.lineno == source.count("\n"), (source, site)
        assert fragment in site.detail, (source, site)


def test_contract_check_flags_a_resurrected_retired_module(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "workers/data_adapters/region.py",
        "from packages.common.contracts.region import GeoBBox\n\n__all__ = ['GeoBBox']\n",
    )

    assert [str(site) for site in scan_contract_violations(tmp_path)] == [
        "workers/data_adapters/region.py:0: retired module workers.data_adapters.region exists again; "
        "use packages.common.contracts.region"
    ]


def test_contract_check_green_samples(tmp_path: Path) -> None:
    _write(tmp_path, "services/a.py", "from workers.data_adapters.base import CycleDiscovery\n")
    _write(
        tmp_path,
        "tests/test_b.py",
        "from workers.mapping_builder import rewrite as rewrite_module\n\nseam = rewrite_module._sha256_file\n",
    )
    _write(tmp_path, "scripts/c.py", "from workers.mapping_builder import HydrologicCoreFingerprint\n")
    _write(tmp_path, "workers/d.py", "from packages.common.contracts.region import GeoBBox\n")

    assert scan_contract_violations(tmp_path) == []


def test_contract_check_covers_every_pinned_form(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/test_forms.py",
        "import importlib\n"  # 1
        "from unittest import mock\n"  # 2
        "import workers.data_adapters.base\n"  # 3: not itself a violation
        "from workers.forcing_producer import direct_grid_contract\n"  # 4: parent import leaf
        "from workers.mapping_builder import rewrite as rw\n"  # 5: alias binding only
        "base_mod = importlib.import_module('workers.data_adapters.base')\n"  # 6: alias binding only
        "a = workers.data_adapters.base.valid_time_for\n"  # 7: dotted chain
        "b = rw.MissingCategoryError\n"  # 8: from-leaf alias
        "c = base_mod.format_cycle_time\n"  # 9: import_module alias
        "d = importlib.import_module('workers.data_adapters.base').cycle_id_for\n"  # 10: import_module chain
        "mock.patch('workers.data_adapters.base.parse_cycle_date')\n"  # 11: patch target
        "mock.patch(target='services.orchestrator.time_consistency.check_three_way_time_consistency')\n"  # 12
        "def f(monkeypatch):\n"  # 13
        "    monkeypatch.setattr('workers.mapping_builder.rewrite.NON_SP_ATT_CATEGORIES', ())\n"  # 14
        "    import services.orchestrator.time_consistency as tc\n"  # 15: function-body import
        "e = rw._sha256_file\n"  # 16: rewrite's own private seam: fine
        "g = base_mod.CycleDiscovery\n"  # 17: not moved: fine
        "from workers.forcing_producer.direct_grid_contract.sub import x\n",  # 18: submodule of retired
    )

    assert [site.lineno for site in scan_contract_violations(tmp_path)] == [4, 7, 8, 9, 10, 11, 12, 14, 15, 18]


def test_relative_imports_resolve_against_the_importing_package() -> None:
    def resolve(file: str, source: str) -> str | None:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.ImportFrom)
        return resolve_import_from(file, node)

    assert resolve("workers/data_adapters/x.py", "from .base import a") == "workers.data_adapters.base"
    assert resolve("workers/forcing_producer/__init__.py", "from .producer import a") == (
        "workers.forcing_producer.producer"
    )
    assert resolve("workers/data_adapters/x.py", "from .. import data_adapters") == "workers"
    assert resolve("workers/data_adapters/x.py", "from ..forcing_producer import a") == "workers.forcing_producer"
    assert resolve("workers/x.py", "from workers.y import a") == "workers.y"
    assert resolve("x.py", "from .. import a") is None
