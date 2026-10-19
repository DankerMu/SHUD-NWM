from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

# #2948 (entropy-burndown task 6.1): the selector is being split into
# `scripts/select_ci_tests_{tables,rules,imports}.py` siblings, imported as
# `from scripts.select_ci_tests_<x> import ...` (dotted only; a non-dotted
# sibling import breaks under pytest and creates a second module identity).
# `python scripts/select_ci_tests.py` (how ci.yml invokes it) puts `scripts/`
# on sys.path, not the repository root, so those imports are not resolvable
# without this -- same guard as publish_scheduler_file_registry.py.
# It compares the resolved absolute path, so it is a no-op whenever the root
# is already on sys.path in that exact form -- which covers `-m`, `uv run`
# and pytest. A root present only as an equivalent-but-different string (`''`
# under `python -c`, `'.'` after a manual insert) does not match and gets a
# redundant absolute entry; harmless, but it is not literal idempotence.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# #2950: the rule tables and the constants they reference live in select_ci_tests_rules.py; add new rule rows there.
from scripts.select_ci_tests_rules import (
    ANY_RATCHET_TEST,
    CHANGED_TEST_FILE_RULES,
    IMPORT_LAYERING_GUARD_TEST,
    LOOP_LOG_AUDIT_TEST,  # noqa: F401
    NATIVE_PROJ_ISOLATION_TEST,  # noqa: F401
    PATH_TEST_RULES,
    PRODUCTION_OPS_SUBRUNBOOK_GLOB,  # noqa: F401
    QHH_CYCLE_SBATCH,  # noqa: F401
    QHH_DIAGNOSTIC_README,  # noqa: F401
    SQLALCHEMY_URL_HELPER_TEST,  # noqa: F401
    STATUS_VOCAB_PARITY_TEST,
    SUPPORT_MODULE_TEST_RULES,
    TIME_HELPER_CONSOLIDATION_TEST,
)

# #2949: the *_TESTS/*_PATH/*_ROOTS/*_MODULES constants live in select_ci_tests_tables.py; add new ones there.
from scripts.select_ci_tests_tables import (
    ANY_RATCHET_ROOTS,
    BASIN_RETIREMENT_HELPERS_PATH,  # noqa: F401
    BASIN_RETIREMENT_OWNER_PATH,  # noqa: F401
    BASIN_RETIREMENT_PACKAGE_MODULES,  # noqa: F401
    BASIN_RETIREMENT_TESTS,  # noqa: F401
    BASINS_PACKAGE_HELPERS_CONSUMER_TESTS,  # noqa: F401
    BASINS_PACKAGE_HELPERS_PATH,  # noqa: F401
    BASINS_PACKAGE_PUBLICATION_TESTS,  # noqa: F401
    BASINS_REGISTRY_IMPORT_HELPERS_CONSUMER_TESTS,  # noqa: F401
    BASINS_REGISTRY_IMPORT_TESTS,  # noqa: F401
    C4_PRODUCTION_ACCEPTANCE_TESTS,  # noqa: F401
    CALIBRATION_OVERRIDES_PATH,  # noqa: F401
    CHAIN_IMPORTER_TESTS,  # noqa: F401
    CORE_SMOKE_TESTS,
    DATA_ADAPTERS_BASE_HELPER_TESTS,  # noqa: F401
    DATA_ADAPTERS_DIRECTORY_TESTS,  # noqa: F401
    DIRECT_GRID_CONTRACT_CANONICAL_PATH,  # noqa: F401
    DIRECT_GRID_CONTRACT_IMPORTER_TESTS,  # noqa: F401
    DIRECT_GRID_CONTRACT_TESTS,  # noqa: F401
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_ATOMIC_TEST,  # noqa: F401
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_HELPERS_PATH,  # noqa: F401
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,  # noqa: F401
    DIRECT_GRID_E2E_TESTS,  # noqa: F401
    DIRECT_GRID_SURFACE_TESTS,  # noqa: F401
    ENTROPY_AUDIT_HELPERS_PATH,  # noqa: F401
    ENTROPY_AUDIT_OWNER_PATH,  # noqa: F401
    ENTROPY_AUDIT_PACKAGE_MODULES,  # noqa: F401
    ENTROPY_AUDIT_TESTS,  # noqa: F401
    FILE_JOURNAL_READ_STATE_TESTS,  # noqa: F401
    FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS,  # noqa: F401
    FORCED_RESUBMIT_SURFACE_TESTS,  # noqa: F401
    FORCING_SQL_SHAPE_ORACLE_TESTS,
    FORCING_TEMPLATE_DISCOVERY_ROOTS,
    HYDROLOGIC_FINGERPRINT_CONTRACT_PATH,  # noqa: F401
    IMPORT_LAYERING_GUARD_ROOTS,
    LOOP_LOG_AUDIT_PATH,  # noqa: F401
    LOOP_LOG_PATH,  # noqa: F401
    MERGED_REGISTRY_PUBLISH_HELPERS_PATH,  # noqa: F401
    MERGED_REGISTRY_PUBLISH_OWNER_PATH,  # noqa: F401
    MERGED_REGISTRY_PUBLISH_PACKAGE_MODULES,  # noqa: F401
    MERGED_REGISTRY_PUBLISH_TESTS,  # noqa: F401
    MODEL_SUCCESSION_HELPERS_PATH,  # noqa: F401
    MODEL_SUCCESSION_OWNER_PATH,  # noqa: F401
    MODEL_SUCCESSION_PACKAGE_MODULES,  # noqa: F401
    MODEL_SUCCESSION_TESTS,  # noqa: F401
    NODE22_ENTRYPOINT_HELPERS_PATH,  # noqa: F401
    NODE22_ENTRYPOINT_INVARIANT_PYTHON_SCAN_TEST,  # noqa: F401
    NODE22_ENTRYPOINT_INVARIANT_TEST,  # noqa: F401
    NODE22_ENTRYPOINT_INVARIANT_TESTS,  # noqa: F401
    NODE22_REFRESH_TIMER_HEALTH_ENV_TEMPLATE_TESTS,  # noqa: F401
    NODE22_REFRESH_TIMER_HEALTH_HELPERS_PATH,  # noqa: F401
    NODE22_REFRESH_TIMER_HEALTH_OWNER_PATH,  # noqa: F401
    NODE22_REFRESH_TIMER_HEALTH_RUNBOOK_TESTS,  # noqa: F401
    NODE22_REFRESH_TIMER_HEALTH_TESTS,  # noqa: F401
    NODE27_MVT_CACHE_RETENTION_HELPERS_PATH,  # noqa: F401
    NODE27_MVT_CACHE_RETENTION_TESTS,  # noqa: F401
    NODE27_PGDATA_WORKLOAD_TESTS,  # noqa: F401
    OPENAPI_CONTRACT_TESTS,  # noqa: F401
    ORCHESTRATOR_CLI_IMPORTER_TESTS,  # noqa: F401
    ORCHESTRATOR_MANIFEST_SURFACE_TESTS,  # noqa: F401
    PATH_CANONICALIZATION_FAMILY_GUARD_ROOTS,
    PRODUCTION_OPS_RUNBOOK_HELPERS_PATH,  # noqa: F401
    PRODUCTION_OPS_RUNBOOK_TESTS,  # noqa: F401
    PRODUCTION_TOPOLOGY_SCAN_ROOTS,
    PUBLISH_REGISTRY_OWNER_PATH,  # noqa: F401
    PUBLISH_REGISTRY_PACKAGE_MODULES,  # noqa: F401
    PUBLISH_SCHEDULER_REGISTRY_HELPERS_PATH,  # noqa: F401
    PUBLISH_SCHEDULER_REGISTRY_TESTS,  # noqa: F401
    READONLY_DB_VALIDATION_TESTS,  # noqa: F401
    REGION_CONTRACT_PATH,  # noqa: F401
    RELEASED_RESERVATION_RECOVERY_TESTS,  # noqa: F401
    RETENTION_COPYBACK_MUTEX_HELPERS_PATH,  # noqa: F401
    RETENTION_COPYBACK_MUTEX_OWNER_PATH,  # noqa: F401
    RETENTION_COPYBACK_MUTEX_OWNER_TESTS,  # noqa: F401
    RETENTION_COPYBACK_MUTEX_TESTS,  # noqa: F401
    REVIEW_GATE_CLI_PATH,  # noqa: F401
    REVIEW_GATE_CLI_TEST,  # noqa: F401
    REVIEW_GATE_ISSUE_MEMORY_PATH,  # noqa: F401
    REVIEW_GATE_ISSUE_MEMORY_TEST,  # noqa: F401
    RIVER_SEGMENT_WRITE_SURFACE_ROOTS,
    RIVER_SEGMENT_WRITE_SURFACE_SQL_ROOTS,
    SCHEDULER_IMPORTER_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_DEPLOYMENT_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_HELPER_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_HELPERS_PATH,  # noqa: F401
    SCHEDULER_REFRESH_INSTALLER_HARNESS_PATH,  # noqa: F401
    SCHEDULER_REFRESH_INSTALLER_HARNESS_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_INSTALLER_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_OWNER_PATH,  # noqa: F401
    SCHEDULER_REFRESH_PACKAGE_MODULES,  # noqa: F401
    SCHEDULER_REFRESH_RECEIPT_HELPER_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH,  # noqa: F401
    SCHEDULER_REFRESH_RUNNER_TESTS,  # noqa: F401
    SCHEDULER_REFRESH_TESTS,  # noqa: F401
    SELECTOR_META_GUARD_TEST,
    SQL_SHAPE_ORACLE_TESTS,  # noqa: F401
    SQLALCHEMY_DRIVER_GUARD_ROOTS,
    SQLALCHEMY_URL_HELPER_PATH,
    STATE_INDEX_COPYBACK_REPLAY_HELPERS_PATH,  # noqa: F401
    STATE_INDEX_COPYBACK_REPLAY_OWNER_PATH,  # noqa: F401
    STATE_INDEX_COPYBACK_REPLAY_TESTS,  # noqa: F401
    STATUS_VOCAB_PARITY_ROOTS,
    THREAD_EXCEPTION_POLICY_TESTS,  # noqa: F401
    TIME_HELPER_CONSOLIDATION_ROOTS,
    TIME_NORMALIZATION_CONTRACT_PATH,  # noqa: F401
    TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS,
    PathTestRule,
)

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


# #1561: the only auto-skipker names pytest collection treats as file-level
# gates. A suite carrying a file-level `pytestmark` of eitherker skips in the
# pull-request lane (tests/conftest.py's pytest_collection_modifyitems), so an
# importer suite soked must not join the ordinary importer closure — the
# closure is for suites that RUN their assertions on the PR. Function-level
# ks do not gate the whole file and never appear here: theker is read
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
    """File-level gatingker names a module-level ``pytestmark`` applies.

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

# #1656: the structural write-site invariant suite. It AST-scans every Python
# file under the four roots it derives from _scan_roots (workers/**,
# packages/common/**, scripts/**, db/**) for unwired DELETEs against guarded
# hypertables, so a future source under any of those roots that touches a
# guarded table must route to it. Routed SUPPLEMENTALLY (set union only), never
# through ordinary PATH_TEST_RULES: it does not set `matched`, does not
# participate in stop rules, and cannot shadow the unknown-backend fallback or
# any other rule's targets.
TIMESCALE_WRITE_GUARD_INVARIANT_TEST = "tests/test_timescale_write_guard_wire_site_invariant.py"

# #2185: the river-segment write-surface scan. It AST-parses every Python file
# under the six directories it walks (plus, since #2154, every `db/**/*.sql`
# statement) and pins that exactly one in-place
# `UPDATE core.river_segment` exists in production code, that it lives in
# workers/model_registry/basins_registry_import.py, and that it bumps
# core.river_network_version.geometry_generation in the same transaction. A
# second in-place rewrite added anywhere under those roots would move geometry
# with no cache-key rotation, so every path the scan reads must route to it.
# Routed SUPPLEMENTALLY (set union only) in the shape #1656 established: no
# `matched`, no stop rules, no effect on the unknown-backend fallback.
RIVER_SEGMENT_WRITE_SURFACE_TEST = "tests/test_river_segment_write_surface_scan.py"

# #1627 / ADR 0009 (docs/adr/0009-path-canonicalization-dereference-doctrine.md):
# the path-canonicalisation family guard. It AST-scans every Python file under
# the four published trees for `(module, qualified function)` pairs that call
# `os.path.realpath` and pins that each such member resolves strictly at least
# once, unless it is a named exemption. A NEW canonicalisation site added
# anywhere under those roots with only the non-strict call must red the MERGE
# GATE, not the post-merge master run, so every path the scan reads must route
# to it. Routed SUPPLEMENTALLY (set union only) in the shape #1656 established
# and #2185 repeated: it does not set `matched`, does not participate in stop
# rules, and cannot shadow the unknown-backend fallback or any other rule's
# targets.
PATH_CANONICALIZATION_FAMILY_GUARD_TEST = "tests/test_path_canonicalization_family_guard.py"

# #2452 / ADR 0009 已知限制 2: the `.resolve()`-surface guard. It walks the SAME
# four trees -- it imports the family guard's own file walk rather than restating
# `_SCAN_ROOTS` -- and asserts that no strict `.resolve()` sits behind an
# OSError-only handler. A new such site anywhere under those roots must red the
# merge gate, so it rides the family guard's supplemental loop over
# PATH_CANONICALIZATION_FAMILY_GUARD_ROOTS: same roots, same set-union-only shape.
RESOLVE_SURFACE_GUARD_TEST = "tests/test_resolve_surface_guard.py"

# #2323: the entropy production-topology hard gate. It builds one full-repo
# entropy report and asserts zero production-topology findings, and that report
# reads every scannable text file under the scanner's seven roots plus four
# root-level files. No rule routed those inputs to it, so PR #2321's
# `openspec/changes/**/evidence/*.json` edit went green in the PR lane and
# reddened master run 34787384045 after merge. The route targets the ONE node
# rather than the whole ~97s partition — measured call time 70.71s on node-27,
# 20.13s on macOS, paid by nearly every PR (any `openspec/changes/**` edit is a
# scan input) — and is skipped when the whole file is already selected (every
# entropy scanner-module rule selects ENTROPY_AUDIT_TESTS), so the same test is
# never emitted twice. The #2323 AC names tests/test_entropy_audit_script.py,
# which the #1823 split retired; the gate lives here now.
ENTROPY_AUDIT_REPORT_CONTRACT_TEST = "tests/test_entropy_audit_report_contract.py"
PRODUCTION_TOPOLOGY_HARD_GATE_TEST = (
    f"{ENTROPY_AUDIT_REPORT_CONTRACT_TEST}"
    "::test_entropy_audit_current_repo_hard_gate_has_zero_production_topology_findings"
)
PRODUCTION_TOPOLOGY_SCAN_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "infra/README.two-node-docker.md",
    "openspec/project-profile.md",
)
# `repo_files._repo_relative_path_is_skipped`: any part in the skip dirs, any
# part carrying a skip prefix, or a first part in the skip root dirs. The
# root-dir arm is unreachable under the seven roots above; it is kept for
# fidelity and covered by the constant pin only.
PRODUCTION_TOPOLOGY_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".cache",
    }
)
PRODUCTION_TOPOLOGY_SCAN_SKIP_PREFIXES: tuple[str, ...] = (".nhms-",)
PRODUCTION_TOPOLOGY_SCAN_SKIP_ROOT_DIRS: frozenset[str] = frozenset({"artifacts", "data"})
# `repo_files._has_scannable_text_name`: a TEXT_EXTENSIONS suffix, one of the
# four exact names, or a `.env.` name prefix.
PRODUCTION_TOPOLOGY_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".cfg",
        ".css",
        ".env",
        ".example",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".lock",
        ".md",
        ".mjs",
        ".py",
        ".rst",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
PRODUCTION_TOPOLOGY_TEXT_NAMES: frozenset[str] = frozenset({"Makefile", ".gitignore", ".dockerignore", ".env"})
PRODUCTION_TOPOLOGY_TEXT_NAME_PREFIX = ".env."
FORCING_TEMPLATE_PRUNED_DIRECTORIES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", "dist", "build", ".mypy_cache"}
)
FORCING_TEMPLATE_RENDER_MODULE = "packages.common.forcing_ts_render"

# #2632: the SQLAlchemy driver guard. It AST-scans every `.py` under the five
# roots below (plus tests/integration_helpers.py) and fails, naming `path:line`,
# on a `create_engine` call whose URL is not `sqlalchemy_url(...)` or a literal
# sqlite URL — the drift that would let SQLAlchemy 2.1 pick psycopg (v3) for a
# plain `postgresql://` URL. Routed SUPPLEMENTALLY by a content sniff, in the
# #2498 shape: a changed `.py` under these roots whose AST calls `create_engine`
# (the only files that can introduce a bypass) adds the guard; a helper change
# adds it too, because the guard keys on the helper's name. A root-wide rider
# would add the guard to every backend diff for no signal. The roots and pruned
# directories mirror the guard's own SCAN_ROOTS / PRUNED_DIRECTORIES bindings; a
# selector meta-test asserts equality, so a root added to the scan without
# wiring it here reddens by name. Known limit: `tests/integration_helpers.py`
# stays on its #1487 carve-out collapse (meta-guard only), so a diff to it runs
# the guard on master, not in the PR lane.
SQLALCHEMY_DRIVER_GUARD_TEST = "tests/test_sqlalchemy_driver_explicit.py"
SQLALCHEMY_DRIVER_GUARD_PRUNED_DIRECTORIES: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", "dist", "build", ".mypy_cache"}
)


def normalize_changed_paths(changed_paths: Iterable[str]) -> list[str]:
    """Normalize changed paths exactly as ``select_tests`` consumes them.

    Single normalization authority so the selection loop and the
    collection-smoke provenance computation cannot diverge: strip whitespace
    and translate Windows separators to POSIX. Empty entries are dropped.
    """
    return [path.strip().replace("\\", "/") for path in changed_paths if path.strip()]


def _collection_smoke_required(changed: Sequence[str], *, meta_guard_only: bool) -> bool:
    """Provenance-independent answer to "must the full-tree collect smoke run?".

    True when the final selection is exactly the selector meta-guard (the
    #1454 shape: deleted test file, unrouted support module, or a selector-test
    PR) — with the supplemental production-topology hard-gate node
    (``PRODUCTION_TOPOLOGY_HARD_GATE_TEST``) disregarded, because that rider
    rides on nearly every PR (any ``openspec/changes/**`` edit is a scan input)
    and must never mask the collapse — OR when the changed-file set touches
    the selector itself (``scripts/select_ci_tests.py``, its split-out siblings
    ``scripts/select_ci_tests_tables.py``, ``scripts/select_ci_tests_rules.py``
    and ``scripts/select_ci_tests_imports.py`` (#2948), or
    ``tests/test_select_ci_tests.py``; exact-string membership, never a
    prefix match) — the class of diff that rewrites the
    gate and must not silently lose the full-tree collection oracle, even when
    supplemental routing makes the final selection non-collapsed (e.g. a
    selector-source PR also selects the Timescale invariant). Deliberately
    independent of the final-list shape so a supplemental target can never mask
    the provenance requirement.
    """
    if meta_guard_only:
        return True
    return any(
        path
        in (
            "scripts/select_ci_tests.py",
            "scripts/select_ci_tests_tables.py",
            "scripts/select_ci_tests_rules.py",
            "scripts/select_ci_tests_imports.py",
            SELECTOR_META_GUARD_TEST,
        )
        for path in changed
    )


def select_tests(changed_paths: Iterable[str], *, repo_root: Path = Path(".")) -> list[str]:
    selected: set[str] = set()
    changed = normalize_changed_paths(changed_paths)
    unknown_backend_path = False
    # #1561: built LAZILY — a production-only/support-module-only/redirect
    # selection never parses the suite tree. The first ordinary changed suite
    # that actually reaches self-selection builds it (failing loudly on a
    # malformed discovered suite at that point), and later ordinary changed
    # suites reuse the same index within this invocation.
    suite_importer_index: dict[str, set[str]] | None = None

    def importer_index() -> dict[str, set[str]]:
        nonlocal suite_importer_index
        if suite_importer_index is None:
            suite_importer_index = _build_suite_importer_index(repo_root)
        return suite_importer_index

    for path in changed:
        if path.startswith("tests/") and path.endswith(".py"):
            is_test_suite = is_test_suite_path(path)
            matched_changed_test = False
            for rule in CHANGED_TEST_FILE_RULES:
                if not _rule_activated(rule, path, changed):
                    continue
                if fnmatch.fnmatch(path, rule.pattern):
                    selected.update(rule.tests)
                    matched_changed_test = True
                    if rule.stop_on_match:
                        break
            if not matched_changed_test:
                matched_support_module = False
                # Support-module routing (#1487) is reachable only here: after
                # the CHANGED_TEST_FILE_RULES loop found nothing, and only for a
                # non-suite path. The two domains are disjoint today — every
                # CHANGED_TEST_FILE_RULES pattern is a `test_*.py` basename, so
                # a path that reaches this branch never matched one anyway — but
                # the ordering is what keeps the redirect contract above the
                # authority if that ever stops being true.
                if not is_test_suite:
                    for rule in SUPPORT_MODULE_TEST_RULES:
                        if fnmatch.fnmatch(path, rule.pattern):
                            selected.update(rule.tests)
                            # The meta-guard rider is not cargo: a routed
                            # support-module PR can invalidate the tree-derived
                            # meta-guards (including the closure guard that
                            # governs this very rule), and that suite exists to
                            # run on exactly the PR class that can.
                            selected.add(SELECTOR_META_GUARD_TEST)
                            matched_support_module = True
                if not matched_support_module:
                    # #1561: the ordinary changed-suite branch — the ONLY place
                    # the importer closure applies. A suite that changed reaches
                    # self-selection plus every direct non-gated importer suite
                    # that imports its dotted module at module scope (renaming
                    # or removing a top-level helper then breaks the importer
                    # during PR-lane collection, not after merge). Redirects
                    # matched above never arrive here, so their focused target
                    # sets are untouched by the closure.
                    if is_test_suite:
                        selected.add(path)
                        selected.update(importer_index().get(_test_module_name(path), set()))
                    else:
                        # A `tests/` Python file that `is_test_suite_path` does
                        # not call a suite (conftest.py, integration_helpers.py,
                        # a fixtures/ builder) is not collectible: `pytest -q
                        # <it>` returns NO_TESTS_COLLECTED (exit 5), which
                        # ci.yml's `check=True` renders as a misleading red
                        # carrying zero assertion information (#1453). Such a
                        # path maps to the meta-guard suite instead, so every
                        # emitted target is a collectible test file; the
                        # meta-guard-only collapse then arms ci.yml's full-tree
                        # collect-only smoke (#1454) over the import surface
                        # such a support module can break.
                        selected.add(SELECTOR_META_GUARD_TEST)
            # Unconditional, redirect or not: a redirect fires exactly when a
            # changed test file is swapped for focused nodes, which is also when
            # the meta-guards most need to run.
            if is_test_suite:
                selected.add(SELECTOR_META_GUARD_TEST)
            continue
        matched = False
        for rule in PATH_TEST_RULES:
            if fnmatch.fnmatch(path, rule.pattern):
                selected.update(rule.tests)
                matched = True
                if rule.stop_on_match:
                    break

        same_name_test = _same_name_backend_python_test(path)
        if same_name_test is not None and _test_target_exists(same_name_test, repo_root=repo_root):
            selected.add(same_name_test)
            # A source-only PR can ADD a second colliding source that maps to an
            # existing same-name suite, so the PR lane must run the collision
            # contract now rather than first failing after merge (where the
            # tracked-tree guards re-derive from `git ls-files`). Routed support
            # modules ride the same meta-guard rider for the same reason.
            selected.add(SELECTOR_META_GUARD_TEST)
            matched = True

        if (_is_backend_python_path(path) or _is_backend_shell_path(path)) and not matched:
            unknown_backend_path = True

    if unknown_backend_path:
        selected.update(CORE_SMOKE_TESTS)

    # #1744 path B: shared-library additivity. For EVERY changed backend Python
    # path under packages/common/**, the core-smoke baseline is retained IN
    # ADDITION to any explicit/same-name/supplemental targets — a narrow rule
    # for a shared module must never silently remove scheduler/API coverage.
    # Implemented OUTSIDE the ordinary PATH_TEST_RULES stop-rule loop and
    # independently of the unknown-backend fallback check (no ordering claim
    # between the two: the add is unconditional over the changed set, so no
    # stop rule and no fallback state can shadow it). Other backend roots keep
    # today's known-rule suppression and unknown-path fallback semantics
    # unchanged.
    if any(_is_shared_common_python_path(path) for path in changed):
        selected.update(CORE_SMOKE_TESTS)

    # #1656: supplemental monotonic invariant routing. Every Python path under
    # the four roots scanned by the write-site invariant suite selects that
    # suite IN ADDITION to its ordinary selection. Purely additive: does not
    # set `matched`, does not participate in stop rules, and does not change
    # whether a path is known for fallback purposes. The root match is the only
    # gate — the scan itself walks `*.py` under these roots regardless of the
    # backend-prefix classification, so `db/` (not a backend prefix) is covered
    # exactly as the invariant scans it.
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS):
            selected.add(TIMESCALE_WRITE_GUARD_INVARIANT_TEST)

    # #2185: supplemental river-segment write-surface routing, same shape as the
    # #1656 loop above. Every Python path under the roots the write-surface
    # scan walks selects that scan IN ADDITION to its ordinary selection. Purely
    # additive: no `matched`, no stop-rule participation, no effect on whether a
    # path counts as known for the unknown-backend fallback. The root match is
    # the only gate — the scan parses `*.py` under these roots regardless of the
    # backend-prefix classification, so `apps/` outside `apps/api/` (not a
    # backend prefix) is covered exactly as the scan reads it. #2154: `*.sql`
    # under the SQL roots routes the same way, because the scan reads those
    # statements too.
    for path in changed:
        if (path.endswith(".py") and _any_path_matches([path], RIVER_SEGMENT_WRITE_SURFACE_ROOTS)) or (
            path.endswith(".sql") and _any_path_matches([path], RIVER_SEGMENT_WRITE_SURFACE_SQL_ROOTS)
        ):
            selected.add(RIVER_SEGMENT_WRITE_SURFACE_TEST)

    # #1627 / ADR 0009: supplemental path-canonicalisation family routing, same
    # shape as the two loops above. Every Python path under the four roots the
    # family guard scans selects that guard IN ADDITION to its ordinary
    # selection. Purely additive: no `matched`, no stop-rule participation, no
    # effect on whether a path counts as known for the unknown-backend
    # fallback. The root match is the only gate — the guard parses `*.py` under
    # these roots regardless of the backend-prefix classification, so `apps/`
    # outside `apps/api/` (not a backend prefix) is covered exactly as the
    # guard reads it.
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], PATH_CANONICALIZATION_FAMILY_GUARD_ROOTS):
            selected.add(PATH_CANONICALIZATION_FAMILY_GUARD_TEST)
            # #2452: the `.resolve()`-surface guard scans the same file walk.
            selected.add(RESOLVE_SURFACE_GUARD_TEST)

    # #2920: supplemental import-layering routing, same additive shape as the
    # #1627 loop above (no `matched`, no stop rules, no fallback effect).
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], IMPORT_LAYERING_GUARD_ROOTS):
            selected.add(IMPORT_LAYERING_GUARD_TEST)

    # #2937: supplemental `Any`-ratchet routing, same additive shape as the
    # #2920 loop above (no `matched`, no stop rules, no fallback effect).
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], ANY_RATCHET_ROOTS):
            selected.add(ANY_RATCHET_TEST)

    # entropy-burndown task 2.1a: supplemental status-vocabulary parity routing,
    # same additive shape as the #1656 loop (no `matched`, no stop rules, no
    # fallback effect).
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], STATUS_VOCAB_PARITY_ROOTS):
            selected.add(STATUS_VOCAB_PARITY_TEST)

    # entropy-burndown task 3.1: supplemental time-helper consolidation routing,
    # same additive shape as the #1656 loop (no `matched`, no stop rules, no
    # fallback effect).
    for path in changed:
        if path.endswith(".py") and _any_path_matches([path], TIME_HELPER_CONSOLIDATION_ROOTS):
            selected.add(TIME_HELPER_CONSOLIDATION_TEST)

    # #2498: supplemental forcing-template import sniff, same additive shape as
    # the loops above (no `matched`, no stop-rule participation, no effect on
    # the unknown-backend fallback). A NEW module that imports the forcing
    # renderer at module level is the one place a ForcingTemplatePair can
    # appear that no per-path rider knows about yet, so it runs the shape
    # oracles on the PR that adds it. Unreadable or missing paths fall through
    # (see `_imports_forcing_template_renderer`).
    for path in changed:
        if _imports_forcing_template_renderer(path, repo_root=repo_root):
            selected.update(FORCING_SQL_SHAPE_ORACLE_TESTS)

    # #2632: supplemental SQLAlchemy driver-guard sniff, same additive shape as
    # the #2498 sniff above (no `matched`, no stop-rule participation, no effect
    # on the unknown-backend fallback). See `_calls_create_engine`.
    for path in changed:
        if path == SQLALCHEMY_URL_HELPER_PATH or _calls_create_engine(path, repo_root=repo_root):
            selected.add(SQLALCHEMY_DRIVER_GUARD_TEST)

    # #2323: supplemental production-topology reader route. Every changed path
    # the scanner mirror accepts (existing or deleted) adds the ONE hard-gate
    # node, additively: no `matched`, no stop-rule participation, no effect on
    # the unknown-backend fallback. Decided LAST and over the final selection,
    # so a diff that already runs the whole report-contract file (a scanner
    # module rule, or the suite itself changed) never gets the node twice.
    # Cost: the node's call time is 70.71s on node-27 (20.13s on macOS). It is
    # a supplemental RIDER: `_write_github_output` disregards it when deciding
    # `meta_guard_only`, so it never masks the #1454 meta-guard collapse.
    if any(_is_production_topology_scan_input(path) for path in changed):
        if ENTROPY_AUDIT_REPORT_CONTRACT_TEST not in selected:
            selected.add(PRODUCTION_TOPOLOGY_HARD_GATE_TEST)

    selected_paths = sorted(selected)
    # A selected target pointing at a deleted/renamed test file used to vanish
    # here in silence, so the selection could shrink (even to empty) with no
    # trace. Dropping stays the behavior; the drop is now announced. The target
    # can come from a rule OR from a changed test file that self-selects (a
    # routine deletion), so the wording stays provenance-neutral.
    missing = [path for path in selected_paths if not _test_target_exists(path, repo_root=repo_root)]
    # Several `::`-qualified node ids can pin the same missing file; announce
    # once per file. Return-list filtering below stays per-target.
    warned: set[str] = set()
    for path in missing:
        test_file = path.split("::", 1)[0]
        if test_file in warned:
            continue
        warned.add(test_file)
        message = f"selected test target does not exist and was dropped: {test_file}"
        print(f"select_ci_tests: WARNING: {message}", file=sys.stderr)
        # stdout carries the selected test list (consumed by `pytest -q $(...)`
        # command substitution locally), so the annotation is emitted only under
        # a real Actions runner, where ci.yml passes data via --github-output.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Dropped CI test target::{message}")
    dropped = set(missing)
    return [path for path in selected_paths if path not in dropped]


def changed_paths_from_git(base_ref: str) -> list[str]:
    subprocess.run(
        ["git", "fetch", "--no-tags", "origin", f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}"],
        check=True,
    )
    result = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


# The single authority for which tree prefixes count as backend Python surface.
# It feeds BOTH backend classification (`_is_backend_python_path`) and the
# same-name suite derivation (`_same_name_backend_python_test`), so the two
# cannot drift apart: a path classified as backend gets same-name routing, and
# only those paths do.
#
# This tuple is the one production/runtime authority. The selector's test suite
# pins the exact five-prefix membership through an independent assert-only
# behavioral oracle (target-present selection matrix), so a prefix removed or
# added here reddens there before the tracked-tree guards can shrink with it.
BACKEND_PYTHON_SOURCE_PREFIXES: tuple[str, ...] = (
    "apps/api/",
    "packages/",
    "services/",
    "workers/",
    "scripts/",
)


def _is_backend_python_path(path: str) -> bool:
    return path.endswith(".py") and path.startswith(BACKEND_PYTHON_SOURCE_PREFIXES)


def _is_shared_common_python_path(path: str) -> bool:
    """True iff ``path`` is a backend Python file under the shared library root.

    The #1744 path-B additivity predicate. Deliberately a strict prefix on the
    POSIX-normalized path (the caller normalizes `\\` to `/` before this runs),
    scoped to `packages/common/**` only — no other backend prefix participates
    in the shared-baseline add-on, so non-shared known-rule suppression semantics
    are untouched.
    """
    return path.endswith(".py") and path.startswith("packages/common/")


def _is_backend_shell_path(path: str) -> bool:
    # scripts/**/*.sh is backend surface since #1138: the ci.yml `backend`
    # paths-filter matches it, so an unmapped wrapper must arm the core-smoke
    # fallback here instead of yielding an empty (collect-only) selection.
    # Deliberately scoped to scripts/: other .sh surfaces (infra/, frontend)
    # keep their own filters and have no pytest guard convention.
    return path.endswith(".sh") and path.startswith("scripts/")


def _same_name_backend_python_test(path: str) -> str | None:
    """Derive the same-name test file for a changed backend Python path.

    Applies to every backend Python prefix (`BACKEND_PYTHON_SOURCE_PREFIXES`):
    a tracked `tests/test_<stem>.py` under any of them is the path's own suite.
    Returns the candidate target only; the caller must confirm it exists before
    treating the path as a known mapping.
    """
    if not _is_backend_python_path(path):
        return None
    return f"tests/test_{PurePosixPath(path).stem}.py"


def _rule_activated(rule: PathTestRule, path: str, changed: Sequence[str]) -> bool:
    """Pure activation predicate for a ``CHANGED_TEST_FILE_RULES`` rule.

    A single source of truth for whether ``rule`` may fire for changed file
    ``path`` given the whole ``changed`` set: a rule with a non-empty
    ``only_when_any_changed`` surface fires only when at least one changed path
    matches that surface. No match against ``rule.pattern`` is attempted here —
    the caller keeps the exact existing ordering, ``stop_on_match``, and
    first-match semantics. Production and tests both call this predicate, so the
    live-tree ordinary-domain classification in
    tests/test_select_ci_tests.py (which must skip only an ACTUALLY active
    redirect) cannot drift from the loop that applies the redirects.

    Scope (#2198): ``only_when_any_changed`` is honoured only on
    ``CHANGED_TEST_FILE_RULES``; the ``PATH_TEST_RULES`` and
    ``SUPPORT_MODULE_TEST_RULES`` loops never call this predicate, and a
    table-level guard in tests/test_select_ci_tests.py rejects the field on
    both of those tables rather than letting it sit there inert.
    """
    if rule.only_when_any_changed and not _any_path_matches(changed, rule.only_when_any_changed):
        return False
    return True


def _any_path_matches(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns)


def _is_production_topology_scan_input(path: str) -> bool:
    """True iff the #2323 mirror says the topology scanner reads ``path``.

    Pure over the repo-relative POSIX string — no stat, no existence check, so
    a deleted input routes exactly like an edited one. Roots match by path
    PARTS, not string prefix, so `docs/governance-x/` is not under
    `docs/governance`. The module globals are read at call time, which is what
    lets the meta-suite's root-drop monkeypatch prove they are the authority.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    in_scope = path in PRODUCTION_TOPOLOGY_SCAN_FILES or any(
        len(parts) > len(root_parts) and parts[: len(root_parts)] == root_parts
        for root_parts in (PurePosixPath(root).parts for root in PRODUCTION_TOPOLOGY_SCAN_ROOTS)
    )
    if not in_scope:
        return False
    if any(part in PRODUCTION_TOPOLOGY_SCAN_SKIP_DIRS for part in parts):
        return False
    if any(part.startswith(PRODUCTION_TOPOLOGY_SCAN_SKIP_PREFIXES) for part in parts):
        return False
    if parts[0] in PRODUCTION_TOPOLOGY_SCAN_SKIP_ROOT_DIRS:
        return False
    name = parts[-1]
    return (
        PurePosixPath(name).suffix in PRODUCTION_TOPOLOGY_TEXT_EXTENSIONS
        or name in PRODUCTION_TOPOLOGY_TEXT_NAMES
        or name.startswith(PRODUCTION_TOPOLOGY_TEXT_NAME_PREFIX)
    )


def _imports_forcing_template_renderer(path: str, *, repo_root: Path) -> bool:
    """True iff changed ``path`` is a #2498 forcing-template candidate.

    A `.py` under FORCING_TEMPLATE_DISCOVERY_ROOTS with no pruned directory part
    whose module-level imports (all four spellings resolve through
    `_top_level_imported_module_names`, relative ones included) name
    FORCING_TEMPLATE_RENDER_MODULE. Anything that cannot be read as such a
    module — missing (a deletion or rename cannot construct a pair), not a
    regular file, unreadable, not UTF-8, unparsable — is False, never an error:
    this is an additive route, and the census/registry suites own the
    fail-closed contract for undecodable sources. `ValueError` is caught
    alongside `SyntaxError` because Python 3.11's `ast.parse` raises it for a
    source holding a NUL byte.
    """
    if not path.endswith(".py"):
        return False
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] not in FORCING_TEMPLATE_DISCOVERY_ROOTS:
        return False
    if any(part in FORCING_TEMPLATE_PRUNED_DIRECTORIES for part in parts[:-1]):
        return False
    source_path = repo_root / path
    try:
        if not source_path.is_file():
            return False
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False
    return FORCING_TEMPLATE_RENDER_MODULE in _top_level_imported_module_names(path, tree)


def _calls_create_engine(path: str, *, repo_root: Path) -> bool:
    """True iff changed ``path`` is a #2632 driver-guard candidate.

    A `.py` under SQLALCHEMY_DRIVER_GUARD_ROOTS with no pruned directory part
    whose AST holds a `create_engine(...)` / `<x>.create_engine(...)` call —
    the guard's own callee predicate. A deleted, unreadable or unparsable file
    is False, never an error (the guard owns the fail-closed parse), and a
    deletion cannot introduce an engine that bypasses the helper.
    """
    if not path.endswith(".py"):
        return False
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[0] not in SQLALCHEMY_DRIVER_GUARD_ROOTS:
        return False
    if any(part in SQLALCHEMY_DRIVER_GUARD_PRUNED_DIRECTORIES for part in parts[:-1]):
        return False
    source_path = repo_root / path
    try:
        if not source_path.is_file():
            return False
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=path)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "create_engine") or (
            isinstance(func, ast.Attribute) and func.attr == "create_engine"
        ):
            return True
    return False


def _test_target_exists(target: str, *, repo_root: Path) -> bool:
    test_path = target.split("::", 1)[0]
    return (repo_root / test_path).is_file()


def _write_github_output(
    tests: Sequence[str],
    *,
    output_path: Path,
    changed_paths: Sequence[str],
) -> None:
    # `meta_guard_only` is a SHAPE property of the FINAL (post missing-target
    # filter) selection, not a claim about evidence provenance: it is true iff
    # the only target left is the selector's own suite. That covers the PR whose
    # single backend change deletes a `tests/test_*.py` (self-selection dropped
    # by the filter, meta-guard survives) and the #1453 support-module mapping —
    # both lost the full-tree import smoke they used to get — and it also fires
    # for selector-development PRs whose diff-specific target simply IS this
    # suite. That last class is accepted rather than special-cased: the cost is
    # one extra collection pass on exactly the PR class that changes the gate.
    #
    # #2323: the production-topology hard-gate node is a supplemental RIDER and
    # is disregarded here. Nearly every PR carries an `openspec/changes/**`
    # edit, which is a topology scan input, so counting the node would turn
    # every deleted-test / unrouted-support-module collapse into
    # `[NODE, META]` and silently drop the full-tree collect-only smoke. The
    # node-ONLY shape (diff-specific selection empty, e.g. `schemas/foo.json`
    # + tasks.md -> `[NODE]`) is deliberately NOT a collapse. It follows the
    # `apps/__init__.py` precedent (a supplemental target moves an empty
    # selection onto the targeted branch without re-arming the smoke), and the
    # zero-selection class here is non-importable data, so no cross-test import
    # surface is at risk and the flag stays false.
    meta_guard_only = [test for test in tests if test != PRODUCTION_TOPOLOGY_HARD_GATE_TEST] == [
        SELECTOR_META_GUARD_TEST
    ]
    # `collection_smoke_required` is INDEPENDENT provenance, not a restatement
    # of the final-list shape: a selector-development PR stays collection-
    # required even when supplemental routing makes the final selection
    # non-collapsed (selector source + Timescale invariant, #1744/#1656).
    # `changed_paths` is the already-normalized set the selection loop ran on;
    # no ambient git state is inspected and no diff is re-run.
    collection_smoke_required = _collection_smoke_required(changed_paths, meta_guard_only=meta_guard_only)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"count={len(tests)}\n")
        handle.write(f"tests={' '.join(tests)}\n")
        handle.write(f"tests_json={json.dumps(list(tests), separators=(',', ':'))}\n")
        handle.write(f"meta_guard_only={'true' if meta_guard_only else 'false'}\n")
        handle.write(f"collection_smoke_required={'true' if collection_smoke_required else 'false'}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select focused pytest files for CI from changed paths.")
    parser.add_argument("--base-ref", help="Base branch name used to compute changed paths.")
    parser.add_argument("--changed-file", type=Path, help="File containing one changed path per line.")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--github-output", type=Path, help="Write count/tests fields for GitHub Actions.")
    args = parser.parse_args(argv)

    if args.changed_file:
        changed = args.changed_file.read_text(encoding="utf-8").splitlines()
    elif args.base_ref:
        changed = changed_paths_from_git(args.base_ref)
    else:
        changed = sys.stdin.read().splitlines()

    normalized = normalize_changed_paths(changed)
    tests = select_tests(normalized, repo_root=args.repo_root)
    for test in tests:
        print(test)
    if args.github_output:
        _write_github_output(
            tests,
            output_path=args.github_output,
            changed_paths=normalized,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
