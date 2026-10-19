"""Suite-importer derivation of the CI test selector (`scripts/select_ci_tests.py`).

pytest's collection rule mirrored for changed test files, the module-scope
import-graph derivation of #1561, and its per-file parse cache and parse-count
seam. Moved verbatim out of the facade by entropy-burndown task 6.4 (#2951).
This module imports only the standard library and never imports the facade.
The facade re-binds `_build_suite_importer_index` and `select_tests` looks it up
in the facade globals at call time, so tests keep patching
`scripts.select_ci_tests._build_suite_importer_index`; the parse cache and
`_SUITE_IMPORTER_PARSE_STATS` are not re-bound and are patched here.
"""

from __future__ import annotations

import ast
import fnmatch
import os
from pathlib import Path, PurePosixPath

# pytest's own collection rule, mirrored: BOTH default `python_files` patterns,
# matched against the BASENAME (which is how pytest itself matches a slash-free
# pattern). Two hand-derivations of this predicate were wrong in a row — first
# path-shaped (`tests/test_*.py`, which reads `tests/pkg/test_y.py` as a support
# module), then a single pattern (which reads `tests/x_test.py` as one). Both
# misclassifications cost a real suite its self-selection AND the meta-guard
# accumulation. tests/test_select_ci_tests.py now anchors this list to what
# pytest actually collects, so the next drift reddens instead of shipping.
CHANGED_TEST_SUITE_BASENAME_PATTERNS: tuple[str, ...] = ("test_*.py", "*_test.py")


def is_test_suite_path(path: str) -> bool:
    """True iff pytest would collect tests from ``path`` by name.

    The single classification decision in this module: it feeds changed-test
    self-selection AND the meta-guard accumulation, and the selector's test
    suite imports it rather than restating the patterns, so no caller can drift
    from pytest independently of the anchor test.
    """
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in CHANGED_TEST_SUITE_BASENAME_PATTERNS)


# #1561: the only auto-skip marker names pytest collection treats as file-level
# gates. A suite carrying a file-level `pytestmark` of either marker skips in the
# pull-request lane (tests/conftest.py's pytest_collection_modifyitems), so an
# importer suite so marked must not join the ordinary importer closure — the
# closure is for suites that RUN their assertions on the PR. Function-level
# marks do not gate the whole file and never appear here: the marker is read
# from a module-level `pytestmark` assignment only.
SUITE_FILE_GATING_MARKERS: frozenset[str] = frozenset({"integration", "e2e"})


def _test_module_name(path: str) -> str:
    """Dotted module a repo-relative ``tests/`` suite is imported under.

    ``tests/test_real_slurm_gateway.py`` -> ``tests.test_real_slurm_gateway``,
    and a package ``__init__.py`` is imported as the package itself (a
    ``tests/pkg/__init__.py`` is ``tests.pkg``, never ``tests.pkg.__init__``),
    so a suite that re-exports helpers through a package initializer is still
    matched by the names other suites actually import.
    """
    dotted = str(PurePosixPath(path).with_suffix("")).replace("/", ".")
    return dotted.removesuffix(".__init__")


def _top_level_imported_module_names(path: str, tree: ast.Module) -> set[str]:
    """Dotted module names imported by ``path``'s module-level statements only.

    Deliberately NOT ``ast.walk``: a function-body import runs when that one
    test runs, not at collection, so it does not make the file an importer
    suite of the imported module for selector-coverage purposes (#1561 keeps
    function-local imports out of the ordinary closure).
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(path, node)
            if base is None:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _import_from_base(path: str, node: ast.ImportFrom) -> str | None:
    """Dotted prefix an ``ImportFrom`` in ``path`` resolves against, or ``None``.

    Absolute imports (``level == 0``) keep their own module. Relative ones
    resolve against the importer's package derived from the repo-relative POSIX
    path — never the process CWD: ``tests/pkg/test_x.py`` with
    ``from . import helper`` sits in ``tests.pkg``. A level deeper than the
    path allows contributes nothing rather than raising, so the walk over the
    repository tree cannot crash on a malformed relative depth.
    """
    if node.level == 0:
        return node.module or None
    package_parts = list(PurePosixPath(path).parent.parts)
    strip = node.level - 1
    if strip > len(package_parts):
        return None
    parts = package_parts[: len(package_parts) - strip]
    if node.module:
        parts.append(node.module)
    return ".".join(parts) or None


def _file_level_gating_markers(tree: ast.Module) -> frozenset[str]:
    """File-level gating marker names a module-level ``pytestmark`` applies.

    Read from the AST, not the file text: a ``@pytest.mark.integration``
    decorator on one function gates that function, not the file, and a
    substring scan cannot tell the two apart. Scalar, list and tuple
    ``pytestmark`` spellings all collapse here, and a
    ``pytest.mark.X(...)`` call contributes ``X``.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if value is None:
            continue
        if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            continue
        for element in ast.walk(value):
            if (
                isinstance(element, ast.Attribute)
                and isinstance(element.value, ast.Attribute)
                and element.value.attr == "mark"
            ):
                names.add(element.attr)
    return frozenset(names & SUITE_FILE_GATING_MARKERS)


# #1561: cross-invocation reuse for unchanged suite trees. The index builder
# walks and stats every suite file on each build (so added/deleted files are
# discovered), but the per-file derivation is cached by absolute path plus
# strong stat identity (mtime_ns + size, plus ctime_ns where the platform
# provides it) plus the repo-relative path (relative-import resolution depends
# on it), so repeated selection against an unchanged tree costs one stat per
# suite, not a reparse of ~1,600 files. Values are IMMUTABLE — a gating flag
# and a frozenset of dotted names, never the mutable ``ast.Module`` — so a
# parse shared across invocations cannot be corrupted by a consumer.
_SUITE_IMPORTER_PARSE_CACHE: dict[tuple[str, int, int, int, str], tuple[bool, frozenset[str]]] = {}

# Test seam: how many suite files the #1561 closure actually parsed, for the
# reuse/rewrite pins. Read and reset by tests; production never branches on it.
_SUITE_IMPORTER_PARSE_STATS: dict[str, int] = {"parses": 0}


def _suite_import_derivation(repo_root: Path, rel_path: str) -> tuple[bool, frozenset[str]]:
    """``(file-level-gated?, module-scope imported dotted names)`` for a suite.

    Reads through ``repo_root``, never the process CWD (the public CLI runs
    from any directory with ``--repo-root``). A cache hit on absolute path +
    stat identity skips the parse entirely; a rewrite changes mtime_ns/size
    (and ctime_ns where the filesystem reports it), so the same filename with
    new content is re-derived, while an identical file is never re-parsed.
    """
    abs_path = str((repo_root / rel_path).resolve())
    stat = os.stat(abs_path)
    ctime_ns = getattr(stat, "st_ctime_ns", 0)
    key = (abs_path, stat.st_mtime_ns, stat.st_size, ctime_ns, rel_path)
    cached = _SUITE_IMPORTER_PARSE_CACHE.get(key)
    if cached is not None:
        return cached
    tree = ast.parse((repo_root / rel_path).read_text(encoding="utf-8"), filename=rel_path)
    _SUITE_IMPORTER_PARSE_STATS["parses"] += 1
    result = (
        bool(_file_level_gating_markers(tree)),
        frozenset(_top_level_imported_module_names(rel_path, tree)),
    )
    _SUITE_IMPORTER_PARSE_CACHE[key] = result
    return result


def _build_suite_importer_index(repo_root: Path) -> dict[str, set[str]]:
    """Reverse index: dotted suite module -> its direct non-gated importer suites.

    Mechanically derived from the supplied ``repo_root`` filesystem — never
    the process CWD and never a Git call, so the selector keeps working from
    any directory against a bare checkout or a synthetic fixture tree. The
    domain is RECURSIVE: every ``tests/**/*.py`` file pytest would collect as a
    suite (``is_test_suite_path``, basename patterns, both ``test_*.py`` and
    ``*_test.py`` names, nested or top-level) is walked via ``os.walk``; the
    file-level ``integration``/``e2e`` suites are excluded (they skip in the PR
    lane), and each remaining suite's module-scope import edges are inverted
    into
    ``imported_dotted_module -> {importer_suite}``. ``from tests import X``
    contributes the package base ``tests`` as well as ``tests.X`` (the dotted
    names actually importable); the owner lookup only ever queries keys the
    tree genuinely produced, so the surplus key is inert. Self-import edges
    (a suite importing its own module) never enter the index.

    A malformed discovered suite propagates its ``SyntaxError`` here — the
    closure is built before any selection can proceed, so the shared selector
    fails loudly instead of silently returning a partial importer index. The
    CALLER decides when this runs: the ordinary changed-suite branch builds it
    lazily, at most once per ``select_tests`` invocation, so a
    production-only/support-module-only/redirect selection never parses the
    suite tree at all.
    """
    index: dict[str, set[str]] = {}
    tests_root = repo_root / "tests"
    if not tests_root.is_dir():
        return index
    for dirpath, dirnames, filenames in os.walk(tests_root):
        dirnames.sort()
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, filename), repo_root)
            if not is_test_suite_path(rel_path):
                continue
            gated, imported_names = _suite_import_derivation(repo_root, rel_path)
            if gated:
                continue
            my_module = _test_module_name(rel_path)
            for imported in imported_names:
                if imported == my_module:
                    # #1561: a suite importing its own module is not an
                    # importer edge; keep the closure free of self edges.
                    continue
                index.setdefault(imported, set()).add(rel_path)
    return index
