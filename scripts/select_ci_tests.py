from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The selector's own suite carries tracked-tree-derived meta-guards (same-name
# script pairs, container-contract closure, guarded-module importer closure). A
# PR that adds or moves a test file can invalidate any of them, so every changed
# test suite under `tests/` drags this suite along (~6s) instead of letting the
# guards go unrun on exactly the change class they exist for.
SELECTOR_META_GUARD_TEST = "tests/test_select_ci_tests.py"
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


CORE_SMOKE_TESTS: tuple[str, ...] = (
    "tests/test_api.py",
    "tests/test_gateway.py",
    "tests/test_migrations.py",
    "tests/test_orchestration_chain.py",
    "tests/test_production_scheduler.py",
)

# #1656: the structural write-site invariant suite. It AST-scans every Python
# file under the four roots it derives from _scan_roots (workers/**,
# packages/common/**, scripts/**, db/**) for unwired DELETEs against guarded
# hypertables, so a future source under any of those roots that touches a
# guarded table must route to it. Routed SUPPLEMENTALLY (set union only), never
# through ordinary PATH_TEST_RULES: it does not set `matched`, does not
# participate in stop rules, and cannot shadow the unknown-backend fallback or
# any other rule's targets.
TIMESCALE_WRITE_GUARD_INVARIANT_TEST = "tests/test_timescale_write_guard_wire_site_invariant.py"

# #1656: the four source roots scanned by the write-site invariant suite,
# expressed as the same root->glob authority shape the invariant's _scan_roots
# uses. A selector meta-test derives the REQUIRED root set from the invariant
# suite's own _scan_roots AST (tests/test_select_ci_tests.py), so adding a root
# to the scan without wiring it here reddens that meta-test by name.
TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS: tuple[str, ...] = (
    "workers/**",
    "packages/common/**",
    "scripts/**",
    "db/**",
)

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

# #2185: the roots the write-surface scan walks, mirroring its own
# module-level PRODUCTION_DIRS binding (tests/test_river_segment_write_surface_scan.py)
# mapped to `<dir>/**` globs. A selector meta-guard parses that binding out of
# the scan's source and asserts it equals this set, so adding a directory
# to the scan without wiring it here reddens that meta-guard by name. #2154
# added `db/**` (the scan now reads db/seeds and every migration).
# `apps/**` and `packages/**` are deliberately at full width — the scan walks
# both directories whole. TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS above is not a
# precedent for narrowing them: only ITS packages root is `packages/common/**`,
# because that is the width of the scan it routes.
RIVER_SEGMENT_WRITE_SURFACE_ROOTS: tuple[str, ...] = (
    "apps/**",
    "services/**",
    "workers/**",
    "packages/**",
    "scripts/**",
    "db/**",
)

# #2154: the roots whose `*.sql` files the write-surface scan reads as statement
# text, mirroring its module-level SQL_DIRS binding the same way (a meta-guard
# parses it). Only `.sql` under these roots routes to the scan: the scan does
# not read `.sql` anywhere else, so a `tests/fixtures/*.sql` or an
# `openspec/**/*.sql` receipt stays out.
RIVER_SEGMENT_WRITE_SURFACE_SQL_ROOTS: tuple[str, ...] = ("db/**",)

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

# #1627: the four roots the family guard scans, mirroring its own module-level
# `_SCAN_ROOTS` binding in tests/test_path_canonicalization_family_guard.py
# mapped to `<root>/**` globs. A selector meta-guard parses that binding out of
# the guard's source and asserts it equals this set, so adding a fifth root to
# the scan without wiring it here reddens that meta-guard by name.
# `apps/**` and `packages/**` are deliberately at full width — the guard's
# `_iter_python_sources` walks both directories whole, exactly as
# RIVER_SEGMENT_WRITE_SURFACE_ROOTS above does.
# TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS is not a precedent for narrowing them:
# only ITS packages root is `packages/common/**`, because that is the width of
# the scan it routes.
PATH_CANONICALIZATION_FAMILY_GUARD_ROOTS: tuple[str, ...] = (
    "services/**",
    "workers/**",
    "packages/**",
    "apps/**",
)

# #2074: the API-contract corpus, physically partitioned out of the 2,132-line
# `tests/test_api_contract.py` monolith (38 cases). The retained base path keeps
# the cases that read the CONTRACT ARTEFACTS — the committed
# `openapi/nhms.v1.yaml`, the runtime `app.openapi()` document and the generated
# `apps/frontend/src/api/types.ts`; the two partitions hold the route-behaviour
# cases (control plane / job lifecycle, and the registry-backed resource routes).
# All three load `openapi/nhms.v1.yaml` at assertion level, so all three are
# oracles of the published document and the #1684 rule applies: every collectible
# partition replaces the single target wherever the corpus as a whole is the
# oracle (`openapi/**` and the broad `apps/api/**` consumer rule). Where the
# oracle is narrower than the corpus the target stays measured, not widened —
# the `services/tiles/mvt.py`, `apps/api/routes/hydro_display*.py`,
# `apps/api/openapi_patching*.py` and `PRECIP_SURFACE_TESTS` rules name only the
# base path, because only the base partition imports the display modules or reads
# the patched runtime document (the other two never call `app.openapi()`).
# Explicit sorted tuple, never derived at import time (same reason as
# MAPPING_BUILDER_TESTS / BASINS_PACKAGE_PUBLICATION_TESTS). The shared doubles
# live in the non-collectible `tests/api_contract_helpers.py`, routed by
# SUPPORT_MODULE_TEST_RULES.
API_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/test_api_contract.py",
    "tests/test_api_contract_pipeline_ops.py",
    "tests/test_api_contract_resources.py",
)
# The single home of every mock store, gateway double and private assertion
# helper the three partitions share. A helper-only diff must run all three plus
# `tests/test_openapi_response_conformance.py`, which imports `_ModelRegistryStore`
# and `_RunStore` from it at module scope.
API_CONTRACT_HELPERS_PATH = "tests/api_contract_helpers.py"
API_CONTRACT_HELPERS_CONSUMER_TESTS: tuple[str, ...] = (
    *API_CONTRACT_TESTS,
    "tests/test_openapi_response_conformance.py",
)

# #1611 partitioned tests/test_scheduler_state_index_copyback_replay.py (1378
# lines, 32 cases) into these four collectible suites plus one non-collectible
# helper. The monolith is GONE with no compatibility shim, so the same-name
# derivation from `scripts/scheduler_state_index_copyback_replay.py` stopped
# resolving — and a rule target that no longer exists is only a WARNING here, so
# the derivation could not simply be left to rot into an empty selection. Both
# routes below therefore enumerate every partition explicitly: the owner route
# for a change to the replay script, the support-module route for a change to
# the shared fixtures. tests/test_select_ci_tests.py closes the set against the
# tracked tree (exactly four suites + one helper), so a fifth partition or a
# leftover shim reddens instead of silently falling out of the PR lane.
STATE_INDEX_COPYBACK_REPLAY_OWNER_PATH = "scripts/scheduler_state_index_copyback_replay.py"
STATE_INDEX_COPYBACK_REPLAY_HELPERS_PATH = "tests/scheduler_state_index_copyback_replay_helpers.py"
STATE_INDEX_COPYBACK_REPLAY_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_state_index_copyback_replay_commit_uncertainty.py",
    "tests/test_scheduler_state_index_copyback_replay_reason_owners.py",
    "tests/test_scheduler_state_index_copyback_replay_refusals.py",
    "tests/test_scheduler_state_index_copyback_replay_selection.py",
)

# #1101 partitioned tests/test_scheduler_file_provider_refresh.py (9614 lines,
# 315 cases) into these fifteen collectible suites plus two non-collectible
# helpers. The monolith is GONE with no compatibility shim, so the same-name
# derivation from `scripts/scheduler_file_provider_refresh.py` stopped
# resolving and EIGHT existing rule sites that named the monolith by hand had to
# be re-pointed. A rule target that no longer exists is only a WARNING here, so
# leaving any of them to rot would have degraded the refresh lane to an empty
# selection -- i.e. the zero-assertion `--collect-only` smoke -- in silence.
#
# Two target sets, because the routes are not interchangeable:
#   * SCHEDULER_REFRESH_TESTS -- the whole corpus. Used by the routes whose
#     subject is the refresh RUNNER itself (`scripts/scheduler_file_provider_refresh.py`
#     and the broad `services/orchestrator/**` rule): any of its behaviour can
#     move under any of the fifteen.
#   * SCHEDULER_REFRESH_DEPLOYMENT_TESTS -- only the partition that `read_text`s
#     tracked deployment paths. The two systemd units, the env template and the
#     two shell wrappers are each read by exactly one test
#     (`test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`,
#     the installer lifecycle case and the wrapper execution cases), all of which
#     live in `tests/test_scheduler_refresh_deployment_contract.py`. Widening
#     those five rows to the whole corpus would make a unit-only PR pay for 315
#     cases and would break the exact-set pins in tests/test_select_ci_tests.py.
#
# tests/test_select_ci_tests.py closes the corpus against the tracked tree
# (exactly fifteen suites + two helpers), so a sixteenth partition, a leftover
# shim or a helper renamed into a `test_*.py` suite reddens instead of falling
# out of the PR lane.
SCHEDULER_REFRESH_HELPERS_PATH = "tests/scheduler_refresh_helpers.py"
SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH = "tests/scheduler_refresh_receipt_helpers.py"
SCHEDULER_REFRESH_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_barrier_seam.py",
    "tests/test_scheduler_refresh_catalog_derivation.py",
    "tests/test_scheduler_refresh_classification_modes.py",
    "tests/test_scheduler_refresh_cutover_gate.py",
    "tests/test_scheduler_refresh_cutover_gate_audit.py",
    "tests/test_scheduler_refresh_cutover_round2.py",
    "tests/test_scheduler_refresh_deployment_contract.py",
    "tests/test_scheduler_refresh_emergency_receipts.py",
    "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
    "tests/test_scheduler_refresh_provider_atomic.py",
    "tests/test_scheduler_refresh_receipt_block_presence.py",
    "tests/test_scheduler_refresh_retirement_declaration.py",
    "tests/test_scheduler_refresh_retirement_reconciliation.py",
    "tests/test_scheduler_refresh_terminability_probes.py",
    "tests/test_scheduler_refresh_worker_mirror_transactions.py",
)
SCHEDULER_REFRESH_DEPLOYMENT_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_deployment_contract.py",
)
# The whole reach of the refresh RUNNER: the fifteen partitions plus the node-22
# probe suite, which both imports the runner and reads its source for the
# history-receipt filename shape. Named once because #1099 gave it eleven
# carriers (the facade and the ten package modules) instead of one.
SCHEDULER_REFRESH_RUNNER_TESTS: tuple[str, ...] = (
    *SCHEDULER_REFRESH_TESTS,
    "tests/test_node22_refresh_timer_health.py",
)
# #1099 split the 3639-line runner into this package; the historical path stayed
# an executable entrypoint and a re-export facade, so it keeps its own row below
# and is NOT a member of this tuple. Each module gets an explicit row rather than
# a `scripts/scheduler_refresh/**` glob: a glob routes an eleventh module nobody
# reviewed, while an unlisted module reddens the tracked-tree guard in
# tests/test_select_ci_tests.py instead of silently dropping out of the PR lane.
# There is no same-name derivation to fall back on -- none of these basenames has
# a `tests/test_<basename>.py` -- so a missing row here means NO route at all,
# which degrades the lane to the zero-assertion `--collect-only` smoke in
# silence. Every module carries the whole corpus for the same reason the runner
# row does: a behaviour change in any of them can land in any partition.
SCHEDULER_REFRESH_OWNER_PATH = "scripts/scheduler_file_provider_refresh.py"
SCHEDULER_REFRESH_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/scheduler_refresh/classification.py",
    "scripts/scheduler_refresh/config.py",
    "scripts/scheduler_refresh/constants.py",
    "scripts/scheduler_refresh/cutover_declaration.py",
    "scripts/scheduler_refresh/identity.py",
    "scripts/scheduler_refresh/precommit_gate.py",
    "scripts/scheduler_refresh/providers.py",
    "scripts/scheduler_refresh/receipt.py",
    "scripts/scheduler_refresh/receipt_validation.py",
    "scripts/scheduler_refresh/runner.py",
)
# The two helper modules are imported by a SUBSET of the corpus, and the
# support-module closure guard derives that subset from the tracked tree, so
# these tuples are the derived sets -- not the whole corpus. The four suites
# that import neither (barrier seam, catalog derivation, provider atomic,
# terminability probes) kept their fixtures local because nothing else uses
# them; listing them here would be a route the guard cannot justify.
SCHEDULER_REFRESH_HELPER_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_classification_modes.py",
    "tests/test_scheduler_refresh_cutover_gate.py",
    "tests/test_scheduler_refresh_cutover_gate_audit.py",
    "tests/test_scheduler_refresh_cutover_round2.py",
    "tests/test_scheduler_refresh_deployment_contract.py",
    "tests/test_scheduler_refresh_emergency_receipts.py",
    "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
    "tests/test_scheduler_refresh_receipt_block_presence.py",
    "tests/test_scheduler_refresh_retirement_declaration.py",
    "tests/test_scheduler_refresh_retirement_reconciliation.py",
    "tests/test_scheduler_refresh_worker_mirror_transactions.py",
)
SCHEDULER_REFRESH_RECEIPT_HELPER_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_classification_modes.py",
    "tests/test_scheduler_refresh_cutover_gate.py",
    "tests/test_scheduler_refresh_cutover_gate_audit.py",
    "tests/test_scheduler_refresh_cutover_round2.py",
    "tests/test_scheduler_refresh_deployment_contract.py",
    "tests/test_scheduler_refresh_emergency_receipts.py",
    "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
    "tests/test_scheduler_refresh_receipt_block_presence.py",
    "tests/test_scheduler_refresh_retirement_declaration.py",
    "tests/test_scheduler_refresh_retirement_reconciliation.py",
)

# #2259 partitioned tests/test_retention_copyback_mutex.py (1119 lines, 25
# cases) into these two collectible suites and moved its fixture preamble into
# the #1872 helper; the monolith is GONE with no compatibility shim. Neither
# partition has a same-name source, so every route that used to name the
# monolith has to name both by hand -- five existing rule sites do (the helper
# support rule, the cli.py and scheduler_runtime.py stop rules, the broad
# orchestrator directory rule and the copyback_guard.py rule), and the owner
# row below is the sixth.
#
# The owner is new: #2259 also extracted `_CopybackLockBudget` / `_delete_entry`
# / `_remove_tree_under_copyback_mutex` out of retention.py into
# retention_copyback_mutex.py, taking the acquire/release/remove call sites with
# them. Its same-name derivation would resolve to the deleted monolith, so it
# has no derived route at all; `services/orchestrator/**` would carry it, but
# that rule is not where a reader looks for the mutex lane and a stop rule added
# above it later would shadow it silently. The targets are this module's
# requirement oracles plus the three sibling suites that monkeypatch it by name
# (D1 moved their patch targets here in the same commit).
# tests/test_select_ci_tests.py closes the corpus against the tracked tree
# (exactly two suites + one helper), so a third partition or a leftover shim
# reddens instead of falling out of the PR lane.
RETENTION_COPYBACK_MUTEX_OWNER_PATH = "services/orchestrator/retention_copyback_mutex.py"
RETENTION_COPYBACK_MUTEX_HELPERS_PATH = "tests/retention_test_helpers.py"
RETENTION_COPYBACK_MUTEX_TESTS: tuple[str, ...] = (
    "tests/test_retention_copyback_mutex_budget.py",
    "tests/test_retention_copyback_mutex_protocol.py",
)
RETENTION_COPYBACK_MUTEX_OWNER_TESTS: tuple[str, ...] = (
    *RETENTION_COPYBACK_MUTEX_TESTS,
    "tests/test_retention.py",
    "tests/test_retention_copyback_lock_signal.py",
    "tests/test_retention_extra_roots.py",
)

# #1644: the published OpenAPI contract's assertion-level suites. `openapi/**`
# opens the backend gate via ci.yml's paths-filter and must reach real drift/type
# assertions, not the collect-only smoke; the runtime patch owner carries the
# drift suite as well as its existing API contract consumers.
# #1684 large-file guard repair: the 3.1-contract security half was physically
# partitioned into tests/test_slurm_gateway_openapi_security.py; every
# collectible partition replaces the single target.
# #2074 applied the same rule to the API-contract corpus: see API_CONTRACT_TESTS.
OPENAPI_CONTRACT_TESTS: tuple[str, ...] = (
    *API_CONTRACT_TESTS,
    "tests/test_openapi_31_contract.py",
    "tests/test_openapi_drift.py",
    "tests/test_openapi_response_conformance.py",
    "tests/test_slurm_gateway_openapi_security.py",
    "tests/test_pipeline_ops_identity_envelope.py",
)


# #1646: the pytest warning-policy suite proves the SHIPPING config semantically
# (subprocess + removed-filter mutant + unrelated-warning control) and parses
# pyproject/uv.lock for the exact filter and the absence of a timeout
# dependency. Both the config file and the dependency lock select it (plus the
# selector meta-guard, which guards the selector's own rules), so a pyproject
# or lock change cannot ship without re-proving the policy.
THREAD_EXCEPTION_POLICY_TESTS: tuple[str, ...] = ("tests/test_pytest_thread_exception_policy.py",)

# #1711: every tracked `tests/test_mapping_builder_*.py` suite. Explicit sorted
# tuple — deliberately NOT derived at import time: deriving it would run
# `git ls-files` in the process CWD, which breaks the public CLI when invoked
# from a temp directory with `--repo-root` (import fails before argparse
# parses --repo-root). The meta-suite remains the tree-derived authority:
# tests/test_select_ci_tests.py's `_tracked_mapping_builder_suites()` asserts
# this tuple EQUALS the tracked `tests/test_mapping_builder_*.py` set, so a
# ninth suite reddens the guard instead of silently falling out of the rule.
MAPPING_BUILDER_TESTS: tuple[str, ...] = (
    "tests/test_mapping_builder_algorithm.py",
    "tests/test_mapping_builder_binding.py",
    "tests/test_mapping_builder_cli.py",
    "tests/test_mapping_builder_evidence.py",
    "tests/test_mapping_builder_integration.py",
    "tests/test_mapping_builder_integrity.py",
    "tests/test_mapping_builder_rewrite.py",
    "tests/test_mapping_builder_z_policy_verdict.py",
)

# #1711: irregular file-to-suite mappings whose suite names are deliberately NOT
# same-name derivable. state_clone_hook.py has no tests/test_state_clone_hook.py
# (its consumer suite is the cutover-hook suite), and the node-22 clone script
# has no tests/test_node22_clone_direct_grid_cutover_states.py (its four suites
# are the recalibration core, the recalibration CLI end-to-end, the recalibration
# CLI validation split, and the baseline-cutover CLI suite). Kept as explicit
# constants so the rule site and the meta-tests read one authority.
STATE_CLONE_HOOK_TESTS: tuple[str, ...] = ("tests/test_state_clone_cutover_hook.py",)
NODE22_CLONE_CUTOVER_STATES_TESTS: tuple[str, ...] = (
    "tests/test_state_clone_recalibration.py",
    "tests/test_state_clone_recalibration_cli.py",
    "tests/test_state_clone_recalibration_cli_validation.py",
    "tests/test_state_clone_baseline_cutover_cli.py",
)
# The CLI environment helpers shared by BOTH recalibration CLI modules. A change
# to this support module must run both consumers; its suite names are not
# same-name derivable (no tests/state_clone_recalibration_cli_fixtures.py), so
# the route is explicit -- consistent with the shared-fixtures support rule
# below.
RECALIBRATION_CLI_FIXTURES_TESTS: tuple[str, ...] = (
    "tests/test_state_clone_recalibration_cli.py",
    "tests/test_state_clone_recalibration_cli_validation.py",
)

# #1571: the repository default Python pin and its instruction source are the
# producer pair for the Python-environment truth oracle. Neither is a backend
# Python path (the pin is a bare version file, shared.md akdown instruction
# source), so without these rules a pin/instruction-only PR would never run the
# suite that locks 3.11 (the ci.yml backend filter does start the lane, but the
# selector would yield an empty list and CI would fall to collect-only with
# zero assertions).
PYTHON_ENVIRONMENT_TRUTH_TEST = "tests/test_python_environment_truth.py"
# #1571: the two-node Docker runbook is the current deployment-docs entry whose
# repo-Python commands must use the exact checkout interpreter; its suite is
# not same-name derivable. The producer is `infra/**`, which opens the backend
# lane, but without a rule a runbook-only PR would select nothing and drop to
# collect-only.
TWO_NODE_DOCKER_RUNBOOK_ENV_TEST = "tests/test_two_node_docker_runbook_environment_invariant.py"
# #1571: the QHH diagnostic README and its Slurm sbatch wrapper are two current
# producers that previously neither started the backend lane nor selected their
# QHH-static owner. Exact ci.yml paths now start the lane; these rules attach
# its assertions. The existing `scripts/run_qhh_backend_smoke.sh` rule routes to
# tests/test_qhh_scripts_static.py, so these two exact producers share the
# same owner via the same additive (non-stop) rule shape.
QHH_DIAGNOSTIC_README = "scripts/diagnostic/qhh/README.md"
QHH_CYCLE_SBATCH = "scripts/run_qhh_cycle.sbatch"
# #1948: the QHH production-bootstrap corpus is three collectible partitions plus one
# non-collectible helper, so every boundary that used to name the single historical
# monolith path must name all three. Explicit sorted tuple, never derived at import time
# (same reason as MAPPING_BUILDER_TESTS / BASINS_PACKAGE_PUBLICATION_TESTS). The
# partition roles are frozen by openspec/changes/partition-qhh-production-bootstrap-tests
# and pinned by tests/fixtures/qhh_bootstrap_partition_oracle.json: only
# `tests/test_qhh_production_bootstrap_scheduler.py` carries `@pytest.mark.integration`,
# which is why it — not A or B — is the path the ci.yml `database:` filter carries.
QHH_PRODUCTION_BOOTSTRAP_TESTS: tuple[str, ...] = (
    "tests/test_qhh_production_bootstrap.py",
    "tests/test_qhh_production_bootstrap_scheduler.py",
    "tests/test_qhh_production_bootstrap_state.py",
)
# The helper owns the shared builders, the seeded scheduler-readiness rows and the
# `qhh_scheduler_canonical_readiness` fixture the scheduler owner imports, so a helper-only
# diff must run all three consumers. Not collectible: the filename is deliberately not
# `test_*`, so `is_test_suite_path` rejects it and it reaches SUPPORT_MODULE_TEST_RULES.
QHH_PRODUCTION_BOOTSTRAP_HELPERS_PATH = "tests/qhh_production_bootstrap_helpers.py"
# #1571 local-repair 1 (phase7-cand-01): the 997-line node-22 entrypoint owner
# uniquely asserts the exact-interpreter contracts of the two systemd units, the
# repair script's usage string, the QHH diagnostic README's Production
# Replacement lines, the shared instruction source's node-22 deferred-environment
# clause, and tests/conftest.py's skip-guidance pointer. The first five producers
# route through exact PATH_TEST_RULES (non-stop, additive); tests/conftest.py
# rides the SUPPORT_MODULE_TEST_RULES entry instead, because the `tests/**` branch
# handles conftest before PATH_TEST_RULES and a PATH row there would be dead.
NODE22_ENTRYPOINT_INVARIANT_TEST = "tests/test_node22_entrypoint_invariant.py"
# #1103 partitioned that owner: at 1003 lines it was three lines over the
# large-file guard's threshold and had never been excluded, so repointing its
# runbook readers required splitting it first. Two collectible partitions plus
# one non-collectible helper. Explicit tuple, never a
# `tests/test_node22_entrypoint_invariant*.py` glob resolved at import time
# (same reason as ENTROPY_AUDIT_TESTS / PUBLISH_SCHEDULER_REGISTRY_TESTS): a
# glob silently adopts a third partition nobody reviewed, while an unlisted one
# reddens the tracked-tree guard in tests/test_select_ci_tests.py instead of
# quietly dropping out of the PR lane.
NODE22_ENTRYPOINT_INVARIANT_PYTHON_SCAN_TEST = (
    "tests/test_node22_entrypoint_invariant_python_scan.py"
)
NODE22_ENTRYPOINT_INVARIANT_TESTS: tuple[str, ...] = (
    NODE22_ENTRYPOINT_INVARIANT_TEST,
    NODE22_ENTRYPOINT_INVARIANT_PYTHON_SCAN_TEST,
)
# The helper owns the monolith's module prefix (repo root, the node-22/node-27
# root constants and the surface reader); both partitions import it at module
# scope, so the routed set IS the importer closure. Not collectible: the
# filename is deliberately not `test_*`, so `is_test_suite_path` rejects it and
# it reaches SUPPORT_MODULE_TEST_RULES.
NODE22_ENTRYPOINT_HELPERS_PATH = "tests/node22_entrypoint_helpers.py"
NODE22_SLURM_GATEWAY_UNIT = "infra/systemd/nhms-slurm-gateway.service"
NODE22_RETENTION_UNIT = "infra/systemd/nhms-scheduler-evidence-retention.service"
NODE22_JOURNAL_RETENTION_SERVICE = "infra/systemd/nhms-scheduler-journal-retention.service"
NODE22_JOURNAL_RETENTION_TIMER = "infra/systemd/nhms-scheduler-journal-retention.timer"
NODE22_REPAIR_SCRIPT = "scripts/ops/node22_repair_placeholder_hydro_uris.py"
JOURNAL_RETENTION_TESTS = (
    "tests/test_scheduler_journal_retention_planning.py",
    "tests/test_scheduler_journal_retention_archive.py",
)

# #1102: the scheduler-registry publisher corpus is seven collectible partitions
# plus one non-collectible helper. The deleted 3218-line monolith was the ONLY
# same-name suite of `scripts/publish_scheduler_file_registry.py`, so that
# derivation died with it and every route below has to name the partitions.
# Explicit sorted tuple, never derived at import time (same reason as
# QHH_PRODUCTION_BOOTSTRAP_TESTS / BASINS_PACKAGE_PUBLICATION_TESTS); checked
# against the tracked tree by the selector meta-suite.
PUBLISH_SCHEDULER_REGISTRY_TESTS: tuple[str, ...] = (
    "tests/test_publish_registry_calibration_overrides.py",
    "tests/test_publish_registry_manifest_audit.py",
    "tests/test_publish_registry_manual_cli.py",
    "tests/test_publish_registry_package_contexts.py",
    "tests/test_publish_registry_radiation_repair.py",
    "tests/test_publish_registry_refresh_lane.py",
    "tests/test_publish_registry_skip_refusals.py",
)
# The helper owns the monolith's module-wide autouse source-identity stub, so
# EVERY partition imports it at module scope and the routed set IS the derived
# importer closure. Not collectible: the filename is deliberately not `test_*`,
# so `is_test_suite_path` rejects it and it reaches SUPPORT_MODULE_TEST_RULES.
PUBLISH_SCHEDULER_REGISTRY_HELPERS_PATH = "tests/publish_registry_helpers.py"


# #1823: the entropy-audit corpus is fifteen collectible partitions plus one
# non-collectible helper. The deleted 9860-line monolith was the ONLY target of
# the three governance rules below and it had no same-name source pair to fall
# back on, so every route has to name the partitions. Explicit sorted tuple,
# never a `tests/test_entropy_audit_*.py` glob resolved at import time (same
# reason as QHH_PRODUCTION_BOOTSTRAP_TESTS / PUBLISH_SCHEDULER_REGISTRY_TESTS):
# a glob silently adopts a sixteenth partition nobody reviewed, while an
# unlisted partition reddens the tracked-tree guard in
# tests/test_select_ci_tests.py instead of dropping out of the PR lane.
ENTROPY_AUDIT_TESTS: tuple[str, ...] = (
    "tests/test_entropy_audit_baseline_writer_safety.py",
    "tests/test_entropy_audit_baseline_writer_summary.py",
    "tests/test_entropy_audit_facade_guard_aliases.py",
    "tests/test_entropy_audit_facade_guard_forwarders.py",
    "tests/test_entropy_audit_instruction_inventory.py",
    "tests/test_entropy_audit_report_contract.py",
    "tests/test_entropy_audit_retired_paths.py",
    "tests/test_entropy_audit_route_authority_caching.py",
    "tests/test_entropy_audit_route_authority_context.py",
    "tests/test_entropy_audit_route_authority_lists.py",
    "tests/test_entropy_audit_scan_boundaries.py",
    "tests/test_entropy_audit_structural_budget.py",
    "tests/test_entropy_audit_structural_ownership.py",
    "tests/test_entropy_audit_topology_authority.py",
    "tests/test_entropy_audit_topology_db_boundary.py",
)
# The helper owns the monolith's module prefix (the repository/baseline path
# constants and the memoized `build_report` accessor) and its whole private
# helper tail, so ALL fifteen partitions import it at module scope and the
# routed set IS the derived importer closure. Not collectible: the filename is
# deliberately not `test_*`, so `is_test_suite_path` rejects it and it reaches
# SUPPORT_MODULE_TEST_RULES.
ENTROPY_AUDIT_HELPERS_PATH = "tests/entropy_audit_helpers.py"

# #1842 split the 9000-line audit enforcer into this package; the historical
# path stayed the console entrypoint (it owns the `__main__` guard) and an
# attribute-broadcast facade, so it keeps its own row below and is NOT a member
# of this tuple. Each module gets an explicit row rather than a
# `scripts/governance/entropy_audit/**` glob, for the reason #1099 recorded for
# `scripts/scheduler_refresh/` and #1100 for `scripts/publish_registry/`: a glob
# routes a twenty-second module nobody reviewed, while an unlisted module
# reddens the tracked-tree guard in tests/test_select_ci_tests.py instead of
# silently dropping out of the PR lane. None of these basenames has a
# `tests/test_<basename>.py`, so there is no same-name derivation to fall back
# on -- measured before the rows landed, each of the twenty-one selected only
# the five generic core-smoke riders plus the two `scripts/**` supplemental-scan
# suites and ZERO entropy partitions. Every module carries the whole
# fifteen-partition corpus for the same reason the owner row does: every
# partition drives `build_report` or the CLI through all of them.
ENTROPY_AUDIT_OWNER_PATH = "scripts/governance/audit_repo_entropy.py"
ENTROPY_AUDIT_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/governance/entropy_audit/archive_status.py",
    "scripts/governance/entropy_audit/check_env_and_tokens.py",
    "scripts/governance/entropy_audit/check_paths_and_api.py",
    "scripts/governance/entropy_audit/check_stale_routes.py",
    "scripts/governance/entropy_audit/check_topology.py",
    "scripts/governance/entropy_audit/constants.py",
    "scripts/governance/entropy_audit/facade_guard.py",
    "scripts/governance/entropy_audit/findings.py",
    "scripts/governance/entropy_audit/repo_files.py",
    "scripts/governance/entropy_audit/report.py",
    "scripts/governance/entropy_audit/route_governing_text.py",
    "scripts/governance/entropy_audit/route_mentions.py",
    "scripts/governance/entropy_audit/schema.py",
    "scripts/governance/entropy_audit/scoped_context.py",
    "scripts/governance/entropy_audit/structural_budget.py",
    "scripts/governance/entropy_audit/structural_growth.py",
    "scripts/governance/entropy_audit/structural_sources.py",
    "scripts/governance/entropy_audit/structural_surface.py",
    "scripts/governance/entropy_audit/topology_context_rules.py",
    "scripts/governance/entropy_audit/topology_display_env.py",
    "scripts/governance/entropy_audit/topology_predicates.py",
)

# #1100 split the 1495-line manual publisher into this package; the historical
# path stayed the console entrypoint (it owns argparse + `main`) and an
# attribute-broadcast facade, so it keeps its own row below and is NOT a member
# of this tuple. Each module gets an explicit row rather than a
# `scripts/publish_registry/**` glob, for the reason #1099 recorded for
# `scripts/scheduler_refresh/`: a glob routes a tenth module nobody reviewed,
# while an unlisted module reddens the tracked-tree guard in
# tests/test_select_ci_tests.py instead of silently dropping out of the PR lane.
# None of these basenames has a `tests/test_<basename>.py`, so there is no
# same-name derivation to fall back on -- measured before the rows landed, each
# of the nine selected only the generic core-smoke riders and ZERO publisher
# partitions. Every module carries the whole seven-suite corpus for the same
# reason the owner row does: every partition drives
# `publish_all_basin_scheduler_registry` or `main` through all of them.
PUBLISH_REGISTRY_OWNER_PATH = "scripts/publish_scheduler_file_registry.py"
PUBLISH_REGISTRY_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/publish_registry/calibration.py",
    "scripts/publish_registry/cli.py",
    "scripts/publish_registry/constants.py",
    "scripts/publish_registry/model_version.py",
    "scripts/publish_registry/publisher.py",
    "scripts/publish_registry/radiation.py",
    "scripts/publish_registry/registry_rows.py",
    "scripts/publish_registry/selection.py",
    "scripts/publish_registry/workspace.py",
)

# #1860: the checked-in calibration declaration is a non-Python producer with no
# mechanically derivable import closure, so the route must be explicit and test
# its own continued existence. The three consumers are the package-manifest
# suite (owns `basins_calibration_overrides`' packaging contract), the
# scheduler-registry publisher suite (owns the declaration's default-load and
# exact-content oracles), and the selector meta-guard (holds the route pins).
# Exact set: no core-smoke fallback, no collect-only collapse.
# #1102 kept the set at three: of the seven publisher partitions, only the
# calibration-overrides one reads `config/calibration_overrides.yaml` (the
# default-load fixture and the exact-content pin both live there). Every other
# partition either passes `_NO_DECLARATION` or writes its own declaration file.
CALIBRATION_OVERRIDES_PATH = "config/calibration_overrides.yaml"
CALIBRATION_OVERRIDES_CONSUMER_TESTS: tuple[str, ...] = (
    "tests/test_basins_package.py",
    "tests/test_publish_registry_calibration_overrides.py",
    SELECTOR_META_GUARD_TEST,
)

# #2261: the committed review-gate round-ceiling memory. It is hand-edited when
# sessions conflict (that is how two bare top-level keys got in), and its only
# assertion-level consumer is the structural guard below — a JSON data file has
# no import closure, so the route must be explicit. The meta-guard rides along
# because `select_tests` only adds it for changed `tests/` paths, and this path
# is a root JSON file; it also holds this route's own pins.
REVIEW_GATE_ISSUE_MEMORY_PATH = ".review-gate-issues.json"
REVIEW_GATE_ISSUE_MEMORY_TEST = "tests/test_review_gate_issue_memory.py"
REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS: tuple[str, ...] = (
    REVIEW_GATE_ISSUE_MEMORY_TEST,
    SELECTOR_META_GUARD_TEST,
)

# #1912/#1903: the Basins package publication corpus has six frozen baseline
# partitions plus one additive river/segment mapping owner, all below the 1,000-line
# structural limit and sharing one non-collectible helper. A model-registry change must
# select all seven; the explicit sorted tuple is checked against the tracked tree by the
# selector meta-suite and is never derived at import time.
BASINS_PACKAGE_PUBLICATION_TESTS: tuple[str, ...] = (
    "tests/test_basins_migration_report.py",
    "tests/test_basins_package_forcing_identity.py",
    "tests/test_basins_package_publication.py",
    "tests/test_basins_package_publication_failures.py",
    "tests/test_basins_package_publication_refusal.py",
    "tests/test_basins_package_publication_rivseg.py",
    "tests/test_basins_package_publication_toctou.py",
)
# A helper-only change must run all seven partitions and the sibling package suite.
BASINS_PACKAGE_HELPERS_PATH = "tests/basins_package_helpers.py"
BASINS_PACKAGE_HELPERS_CONSUMER_TESTS: tuple[str, ...] = (
    *BASINS_PACKAGE_PUBLICATION_TESTS,
    "tests/test_basins_package.py",
)

# #1913: the Basins registry-import corpus was physically partitioned out of the
# 3,931-line `tests/test_basins_registry_import.py` monolith into seven collectible
# suites below the 1,000-line structural limit, with one shared non-collectible helper.
# The retained historical path alone is NO LONGER the corpus: a
# `workers/model_registry/**` change that reaches only it runs 18 of the 94 registry
# definitions and leaves the six moved partitions blind in the PR lane — the same
# failure class #1912 closed for the publication corpus. This tuple is the single
# seven-owner route authority, and tests/test_select_ci_tests.py derives the tracked
# `tests/test_basins_registry_import*.py` suite set against it, so an eighth partition
# reddens the meta-guard instead of falling out. Explicit sorted tuple, never derived
# at import time (same reason as MAPPING_BUILDER_TESTS / BASINS_PACKAGE_PUBLICATION_TESTS).
BASINS_REGISTRY_IMPORT_TESTS: tuple[str, ...] = (
    "tests/test_basins_registry_import.py",
    "tests/test_basins_registry_import_auth.py",
    "tests/test_basins_registry_import_cli.py",
    "tests/test_basins_registry_import_db.py",
    "tests/test_basins_registry_import_parser.py",
    "tests/test_basins_registry_import_qhh.py",
    "tests/test_basins_registry_import_security.py",
)
# The helper owns all 19 support functions, `_FakeRiverSegmentCursor` and the four
# private constants of the former monolith, so a helper-only diff must run its eleven
# direct collectible importers — the seven registry suites plus the four #1102 publisher
# partitions that import `_write_registry_fixture` / `_make_valid_model` at module scope
# (package-contexts, radiation-repair, calibration-overrides, refresh-lane; before #1102
# this was the single `tests/test_publish_scheduler_file_registry.py` entry).
# SUPPORT_MODULE_TEST_RULES does not recursively expand one helper rule through another,
# and there are now TWO support-to-support importers, both invisible to the non-recursive
# walk, so their collectible reach is named explicitly here:
#   * `tests/qhh_production_bootstrap_helpers.py` imports this helper at module and
#     function scope while its three collectible partitions never import it directly —
#     hence the QHH A/B/C partitions;
#   * `tests/publish_registry_helpers.py` (#1102) imports `_make_valid_model` at module
#     scope for `_write_healthy_basin_pair` — the ONE publisher partition that reaches
#     that builder without importing the registry helper itself is the skip-refusals one,
#     hence that single extra entry. The manual-CLI and manifest-audit partitions import
#     the #1102 helper too but touch no bridged name, so they are deliberately absent.
# Fifteen collectible suites total. The selector meta-guard rider is added by the
# support-module branch itself, deliberately not repeated.
BASINS_REGISTRY_IMPORT_HELPERS_PATH = "tests/basins_registry_import_helpers.py"
BASINS_REGISTRY_IMPORT_HELPERS_CONSUMER_TESTS: tuple[str, ...] = (
    *BASINS_REGISTRY_IMPORT_TESTS,
    "tests/test_publish_registry_calibration_overrides.py",
    "tests/test_publish_registry_package_contexts.py",
    "tests/test_publish_registry_radiation_repair.py",
    "tests/test_publish_registry_refresh_lane.py",
    "tests/test_publish_registry_skip_refusals.py",
    *QHH_PRODUCTION_BOOTSTRAP_TESTS,
)

# #1684 shared-auth owner-to-focused-suite mappings (EVID-01). These shared
# modules have no same-name derivable suite and their consumers are the focused
# partitioned Slurm auth suites, not the broad core-smoke/API/orchestrator
# suites: an owner-only PR previously selected only generic riders and none of
# the focused contracts. Each is an exact additive (non-stop) rule so existing
# intentional supplemental routing (core-smoke baseline for packages/common/**,
# #1656 timescale rider) survives.
#
# `packages/common/auth_policy.py` owns the canonical RBAC action matrix and is
# asserted by the dedicated matrix suite; `packages/common/request_auth.py` owns
# the service-token contract (reader/matcher/client/preflight) and
# `packages/common/openapi_auth_security.py` owns the published scheme/security
# metadata; `apps/api/auth.py` is the facade whose drift is caught by the
# shared-auth contract suites plus the role-boundary static suite; the two
# orchestrator modules (client + scheduler preflight) are owned by their
# focused auth-client/deployment suites.
AUTH_POLICY_TEST = "tests/test_auth_policy_matrix.py"
SLURM_AUTH_CLIENT_TEST = "tests/test_slurm_gateway_auth_client.py"
SLURM_AUTH_DEPLOYMENT_TEST = "tests/test_slurm_gateway_auth_deployment.py"
SLURM_AUTH_CORE_TEST = "tests/test_slurm_gateway_auth.py"
# #1684 EVID-02 partition: the full compute/dev mount matrix lives in its own
# module (the core suite is at the repo 1,000-line limit); every owner that
# selects the core suite must select the partition too.
SLURM_AUTH_FULLMOUNT_TEST = "tests/test_slurm_gateway_auth_fullmount.py"
SLURM_OPENAPI_SECURITY_TEST = "tests/test_slurm_gateway_openapi_security.py"
# Static producer/consumer oracle for the tracked unit / env examples / runbook
# wiring (active EnvironmentFile, same secret path, 8090, executable rollback,
# no inline credential). Distinct from SLURM_AUTH_DEPLOYMENT_TEST (bind-guard +
# preflight behavior).
SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST = "tests/test_slurm_gateway_deployment_contract.py"

# #1103: `docs/runbooks/current-production-ops.md` is an index landing page now
# and its body lives in the `docs/runbooks/production-ops/` sub-runbooks. Both
# halves route to the SAME reader set: every one of these suites `read_text`s
# the tree (commands, §11 failure codes, the probe section, the §3.2.2 rollout
# block, the capacity check, the pinned terminal stage, the topology
# sentences), so a sub-runbook-only diff has to select exactly what an
# index-only diff selects. Without the second rule the body would be editable
# with zero readers selected -- the shape #2195 and #2472 were both filed for.
PRODUCTION_OPS_RUNBOOK_TESTS: tuple[str, ...] = (
    SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,
    "tests/test_env_templates.py",
    "tests/test_node22_refresh_timer_health.py",
    "tests/test_node27_coverage_freshness_alert.py",
    *NODE22_ENTRYPOINT_INVARIANT_TESTS,
    PYTHON_ENVIRONMENT_TRUTH_TEST,
    "tests/test_role_boundary_static.py",
)
# The helper owns the production-ops surface set (index page plus the
# sub-runbook tree, pinned by index/tree mutual agreement). Every reader above
# that scans the whole document imports it, so a helper-only diff must run
# them. Not collectible: the filename is deliberately not `test_*`.
PRODUCTION_OPS_RUNBOOK_HELPERS_PATH = "tests/production_ops_runbook.py"
PRODUCTION_OPS_SUBRUNBOOK_GLOB = "docs/runbooks/production-ops/**"

# The literal rule rows are declared in PATH_TEST_RULES (after the dataclass);
# these constants are the single names the selector meta-suite pins.


@dataclass(frozen=True)
class PathTestRule:
    pattern: str
    tests: tuple[str, ...]
    stop_on_match: bool = False
    # #2198: honoured ONLY on CHANGED_TEST_FILE_RULES (via `_rule_activated`).
    # The PATH_TEST_RULES and SUPPORT_MODULE_TEST_RULES loops never read it, so
    # a gate set there would be silently inert; tests/test_select_ci_tests.py
    # rejects it on both tables by table-level guard.
    only_when_any_changed: tuple[str, ...] = ()


ORCHESTRATOR_MANIFEST_SURFACE_TESTS: tuple[str, ...] = (
    "tests/test_orchestration_chain.py::test_static_chain_type_module_import_resolves_hints_without_heavy_runtime_imports",
    "tests/test_orchestration_chain.py::test_chain_type_exports_preserve_legacy_identity_and_dataclass_contracts",
    "tests/test_orchestration_chain.py::test_model_run_forcing_package_manifest_identity_reaches_runtime_manifest",
    "tests/test_orchestration_chain.py::test_psycopg_find_forcing_context_populates_package_manifest_metadata",
    "tests/test_production_scheduler.py::test_scheduler_routes_ready_canonical_candidate_to_slurm_forcing_without_local_producer",
    "tests/test_production_scheduler.py::test_scheduler_does_not_replace_candidate_identity_from_local_forcing_result",
    "tests/test_production_scheduler.py::test_runtime_manifest_assembly_uses_shud_output_count_not_gis_segment_count",
)


ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS: tuple[str, ...] = (
    "services/orchestrator/chain_types.py",
    "services/orchestrator/chain_manifests.py",
    "services/orchestrator/chain.py",
    "services/orchestrator/scheduler.py",
)

DIRECT_GRID_E2E_TESTS: tuple[str, ...] = ("tests/test_direct_grid_e2e.py",)

DIRECT_GRID_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/test_forcing_producer.py::test_direct_grid_contract_valid_nested_manifest_still_parses",
    "tests/test_forcing_producer.py::test_direct_grid_contract_rejects_explicit_root_direct_grid_when_root_authority_disabled",
    "tests/test_forcing_producer.py::test_direct_grid_contract_missing_manifest_field_raises_structured_error",
    "tests/test_forcing_producer.py::test_direct_grid_contract_missing_station_field_raises_structured_error",
    "tests/test_forcing_producer.py::test_direct_grid_contract_duplicate_shud_forcing_index_is_rejected",
    "tests/test_forcing_producer.py::test_direct_grid_contract_duplicate_forcing_filename_is_rejected",
    "tests/test_forcing_producer.py::test_direct_grid_contract_source_scope_must_be_nonempty_and_apply_to_current_source",
    "tests/test_forcing_producer.py::test_direct_grid_contract_station_coordinates_must_be_in_wgs84_bounds",
    "tests/test_forcing_producer.py::test_direct_grid_contract_station_longitude_is_normalized_for_shud_output",
    "tests/test_forcing_producer.py::test_direct_grid_contract_unsupported_top_level_mode_fails_before_nested_direct_grid",
)

DIRECT_GRID_SURFACE_TESTS: tuple[str, ...] = DIRECT_GRID_E2E_TESTS + DIRECT_GRID_CONTRACT_TESTS

# Non-gated top-level importers of workers/forcing_producer/direct_grid_contract.py
# that the focused DIRECT_GRID_SURFACE_TESTS node ids never reach (#1455). The
# contract module is owned by a stop_on_match rule, so the
# `workers/forcing_producer/**` rule below is unreachable for it and these have
# to ride the stop rule itself. Kept as a separate tuple appended at the rule
# site so DIRECT_GRID_SURFACE_TESTS keeps meaning "the compact e2e fixture" for
# every other reader.
DIRECT_GRID_CONTRACT_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_direct_grid_variant_registration.py",
    "tests/test_legacy_reactivation_guard.py",
    "tests/test_mapping_builder_binding.py",
    "tests/test_mapping_builder_cli.py",
    "tests/test_mapping_builder_integration.py",
)

DIRECT_GRID_SURFACE_PATH_PATTERNS: tuple[str, ...] = (
    "workers/forcing_producer/direct_grid_contract.py",
    "openspec/changes/direct-grid-forcing/**",
)

FILE_JOURNAL_READ_STATE_TESTS: tuple[str, ...] = (
    "tests/test_file_orchestration_journal.py",
    "tests/test_file_orchestration_migration.py",
    "tests/test_scheduler_journal_retention_planning.py",
    "tests/test_scheduler_journal_retention_archive.py",
    "tests/test_orchestration_chain.py::test_psycopg_candidate_state_limits_jobs_and_reads_events_for_candidate_scope",
    "tests/test_orchestration_chain.py::test_psycopg_candidate_state_latest_truth_timestamp_selects_terminal_success",
    "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_cycle_run_array_job_for_filtered_model",
    "tests/test_orchestration_chain.py::test_psycopg_active_slurm_jobs_includes_queued_pipeline_rows",
    "tests/test_orchestration_chain.py::test_psycopg_has_active_pipeline_includes_queued_pipeline_rows",
    "tests/test_orchestration_chain.py::test_psycopg_find_forcing_context_populates_package_manifest_metadata",
    "tests/test_production_scheduler.py::test_fresh_cycle_with_active_slurm_job_does_not_double_submit",
    "tests/test_production_scheduler.py::test_db_free_injected_collaborators_plan_without_unimplemented_provider_blocker",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_ready_candidate_submit_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_journal_write_block_forces_retention_dry_run_before_deletion",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_active_slurm_status_sync_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_injected_factory_cancel_active_slurm_blocks_without_factory_call",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_ready_canonical_zero_submits_convert_without_download_source_cycle",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_missing_blocks_canonical_zero_without_submission",
    "tests/test_production_scheduler.py::test_db_free_from_env_raw_invalid_blocks_without_submission",
    "tests/test_production_scheduler.py::test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url",
    "tests/test_source_cycle_raw_manifest.py",
)

# #1455 at-site extensions for the four orchestrator modules whose stop rules
# make the `services/orchestrator/**` rule unreachable. Each tuple is appended
# to ONE rule below with `(*SHARED_TESTS, *THIS)`; the shared constants
# themselves stay untouched, because they also serve patterns outside the nine
# audited directories (FILE_JOURNAL_READ_STATE_TESTS is used by
# packages/common/safe_fs.py) where selection must not move.
#
# chain.py is the widest importer surface in the audit. Everything here is a
# non-gated top-level importer whose subject IS the chain (~193s measured all
# together); the chain's cross-surface importers — production-closure,
# slurm-gateway, model-registry and forcing-producer suites — stay with the
# rules that own them and are recorded as `edge-consumer` in
# tests/test_select_ci_tests.py.
CHAIN_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_analysis_pipeline.py",
    "tests/test_chain_repository_nfs_raw_manifest.py",
    "tests/test_e2e.py",
    "tests/test_e2e_ifs.py",
    "tests/test_e2e_m3.py",
    "tests/test_file_orchestration_journal.py",
    "tests/test_forcing_submit_ambiguity.py",
    # #1581: chain.py aliases DURABLE_HYDRO_SUCCESS_STATUSES as
    # COMPLETED_HYDRO_STATUSES and the parity lock asserts that alias IS the
    # shared object; a chain-only edit that rebinds it must run this suite.
    # Stop-rule owned module, so the addition rides this at-site tuple rather
    # than the `services/orchestrator/**` list. 9 tests in 0.29s.
    "tests/test_hydro_status_set_parity.py",
    "tests/test_ifs_forecast_integration.py",
    "tests/test_orchestrator.py",
    "tests/test_partial_success.py",
    "tests/test_pipeline_logs_artifacts.py",
    "tests/test_warm_start.py",
    "tests/test_warm_start_chaining.py",
)

# #1562 structural split: the forced-resubmit evaluator/evidence owner
# (chain_forced_resubmit.py) and the candidate-outcome/evidence owner
# (chain_array_evidence.py) each have one dedicated focused suite. The broad
# `services/orchestrator/**` rule already selects the integration suites that
# drive these owners (test_orchestration_chain.py, test_production_scheduler.py,
# test_warm_start_chaining.py); this additive non-stop rule attaches the focused
# suite so an owner-only PR runs its own assertions instead of falling to
# integration-only coverage.
FORCED_RESUBMIT_SURFACE_TESTS: tuple[str, ...] = ("tests/test_forced_resubmit_veto.py",)

SCHEDULER_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_publish_qdown.py",
    "tests/test_scheduler_backfill.py",
    "tests/test_scheduler_backfill_predecessor.py",
    # #1943: the journal-root authority suite top-level-imports
    # `ProductionScheduler`/`ProductionSchedulerConfig` and pins the typed
    # `FILE_JOURNAL_INVALID_ROOT` refusal `from_env` now raises before any
    # repository read. It measures 17 tests in 0.36s, so it joins the rule
    # rather than riding an exclusion token.
    "tests/test_scheduler_journal_root_authority.py",
    # #1555/#1768: the operator re-entry confirmation suite top-level-imports
    # `services.orchestrator.scheduler` and drives the REAL scheduler seams the
    # one-shot confirmation gates (breaker candidate re-entry, the strict
    # warm-start budget arm). A gating or seam edit in the facade must run it.
    # DB-free, 23 tests in 46.71s — an order of magnitude heavier than this
    # tuple's other members, but far under the ~5 min per-module line the
    # runtime-budget token is reserved for, and the suite's subject IS this
    # module, so a rule is the honest disposition rather than an exclusion.
    "tests/test_operator_reentry_confirmation.py",
    # #1186: the operator-action listing suite couples to `scheduler.py` two ways.
    # It imports it transitively — `operator_action_listing.py` aliases
    # `SCOPE_COMPLETE_SOURCES`/`SCOPE_COMPLETE_CYCLE_HOURS_UTC` straight from
    # `DEFAULT_PRODUCTION_SOURCES`/`DEFAULT_ALLOWED_CYCLE_HOURS_UTC` rather than
    # keeping copies — and it also reads `cli.py`'s independent `resolved_sources`
    # fallback with `ast`. Either way an edit to those tuples is precisely what
    # has to run this suite. Without this row the coupling edge is invisible to
    # the selector: the importer closure only applies when the CHANGED path is
    # itself a test file (`select_ci_tests.py:3809-3811`), and a production module
    # path is routed by PATH_TEST_RULES alone.
    "tests/test_operator_action_listing.py",
    # #2401: the newest-truth terminal-skip suite top-level-imports
    # `services.orchestrator.scheduler`, drives full `ProductionScheduler`
    # passes through it and observes the decision at the facade's
    # `_candidate_state_decision` seam, so a facade or seam edit must run it.
    # DB-free, 21 tests in ~10s: a rule, not a rule-gap exclusion.
    "tests/test_scheduler_terminal_recency.py",
    "tests/test_scheduler_timing.py",
    "tests/test_source_scoped_dispatch.py",
)

ORCHESTRATOR_CLI_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_cleanup_frontier.py",
    "tests/test_cli_publish_qdown.py",
    "tests/test_orchestrator_demote_cli_security.py",
    "tests/test_retention_frontier.py",
    "tests/test_scheduler_backfill.py",
    # #1943/#1944: both new journal suites top-level-import `cli` and drive it
    # through BOTH entrypoints (`_click_main`/`_argparse_main`) — the root
    # authority suite for the refusal's operator-facing exit, the census suite
    # for the read-only `census_job_id_scope` command registration. A register
    # order or exit-code change in cli.py must run them; measured together they
    # are 50 tests in 1.50s.
    "tests/test_scheduler_journal_root_authority.py",
    "tests/test_scheduler_journal_scope_census.py",
)

# #1748 recovery-CLI helper extraction: the shared
# released-identity-blocked-reservation body is exercised through both CLI
# entrypoints by the journal suite's operator-channel tests, and the signal/
# command e2e pair lives in the production-scheduler suite. The demote CLI
# security suite shares the register boundary in _click_main/_argparse_main,
# so a register-order change must run it too.
RELEASED_RESERVATION_RECOVERY_TESTS: tuple[str, ...] = (
    "tests/test_file_orchestration_journal.py",
    "tests/test_production_scheduler.py",
    "tests/test_orchestrator_demote_cli_security.py",
)

FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_file_orchestration_journal_read_cache.py",
    "tests/test_forcing_submit_ambiguity.py",
    # #1581: the journal's completed-pipeline probes decide on its own
    # `COMPLETED_HYDRO_STATUSES` from-import binding, which the parity lock
    # pins as the one shared object. Stop-rule owned module, so the addition
    # rides this at-site tuple rather than the `services/orchestrator/**` list.
    # 9 tests in 0.29s.
    "tests/test_hydro_status_set_parity.py",
    # #1825: the node-22 manual-retryker suite top-level-imports the journal
    # repository and pins theker contract (per-run row vs cohort master) the
    # operator channel depends on. It runs in well under a second, so a rule is
    # the right disposition rather than a rule-gap exclusion.
    "tests/test_node22_manual_retry_failed_runs.py",
    "tests/test_orchestrator_demote_cli_security.py",
    "tests/test_orchestrator_demote_core_cas.py",
    "tests/test_orchestrator_demote_projection_faults.py",
    "tests/test_orchestrator_demote_reclaim_lifecycle.py",
    "tests/test_scheduler_backfill.py",
    # #1999: the predecessor-emission suite top-level-imports
    # `FileOrchestrationJournalRepository` and pins that a predecessor hydro row
    # sitting at `pending` on the REAL file journal makes the emitter skip with
    # `predecessor_backfill_active_pipeline` and emit zero candidates — so the
    # repository's active-pipeline probe decides that lane. Stop-rule owned
    # module, so the addition rides this at-site tuple rather than the
    # `services/orchestrator/**` list. 20 tests in 1.57s, hence a rule rather
    # than a rule-gap exclusion.
    "tests/test_scheduler_backfill_predecessor.py",
    # #1944: the job-id scope census reads the journal tree directly and mints
    # its divergent rows through the PUBLIC `reserve_pipeline_job` writer, so a
    # change to the repository's on-disk layout or writer path silently changes
    # what the census observes. 33 tests in 1.21s, hence a rule not an exclusion.
    "tests/test_scheduler_journal_scope_census.py",
    # #1953: the whole-tree budget contract is ABOUT this module — the read
    # lane its `_RecordBudget` tags, and the synthetic blocked row the five
    # query entrypoints return when the budget refuses. Its static pins read
    # the constant and the sibling sentinels straight out of this file, so a
    # budget, lane or sentinel edit must run it. DB-free, 11 tests in 1.85s,
    # hence a rule rather than a rule-gap exclusion.
    "tests/test_file_journal_full_tree_budget_contract.py",
    # #2404: the retry-mint-floor suite asserts what this module's cohort
    # reconcile write charges each member (the per-model reconciled row's
    # ``retry_count``), and its imports are all function-local, so no importer
    # derivation reaches it. DB-free, ~4s, hence a rule not an exclusion.
    "tests/test_retry_mint_floor.py",
    # #1555/#1768: the operator re-entry confirmation suite seeds REAL file
    # journals through this repository and reads the one-shot confirmation event
    # back through it, so its whole precondition geometry rests on this module's
    # write and read paths. DB-free, 23 tests in 46.71s — heavier than the rest
    # of this tuple but well under the ~5 min per-module line, and the journal IS
    # part of its subject, so a rule rather than a rule-gap exclusion.
    "tests/test_operator_reentry_confirmation.py",
    # #1955: the journal-root lane-adoption suite builds this repository on
    # every verified root it pins and asserts that a refused root leaves zero
    # bytes anywhere the lane could have written — a claim only this module's
    # write paths can break. DB-free, 76 tests in 0.70s.
    "tests/test_journal_root_lane_adoption.py",
    # #2420: the provenance publisher reads the source-owned publication view
    # and fail-closes on blocked journal rows, so a journal-only PR must run it.
    "tests/test_pipeline_job_provenance_publisher.py",
    # #2397: the §8.7 identity authority (completed hydro_run vs a newer
    # accepted-submit master) lives in this module; its requirement suite drives
    # real journals through the real lifecycle and is named after neither file.
    # DB-free, 7 tests in ~13s.
    "tests/test_quarantine_identity_authority.py",
    # #2385/#2387: the read-blocked sentinel's coupling pin. Its whole subject is
    # this module -- the `_blocked_query_job` row shape across all five query
    # lanes, the one `_is_blocked_query_job` discriminator both retry-lane
    # consumer ends key on, the manual-retry source selector's refusal and the
    # two runtime-root provenance readers' degrade. Every import is
    # function-local, so no importer derivation reaches it; this stop-rule site
    # is its route for the journal. DB-free, 17 tests in ~1s.
    "tests/test_file_journal_read_blocked_consumers.py",
)

FILE_JOURNAL_READ_STATE_PATH_PATTERNS: tuple[str, ...] = (
    "packages/common/safe_fs.py",
    "services/orchestrator/chain_repository_state.py",
    "services/orchestrator/file_orchestration_journal.py",
    "services/orchestrator/file_orchestration_migration.py",
    "services/orchestrator/scheduler_journal_archive.py",
    "services/orchestrator/scheduler_journal_restore.py",
    "services/orchestrator/scheduler_journal_retention.py",
    "services/orchestrator/scheduler_journal_retention_types.py",
    "services/orchestrator/cli.py",
    "services/orchestrator/scheduler.py",
    "services/orchestrator/scheduler_core.py",
    "services/orchestrator/scheduler_runtime.py",
)


# tests/test_sql_shape_helpers.py is both a test module and the SQL-shape
# ORACLE the #1341 read-path negative pins are written against
# (`strip_scalar_subqueries`). Its own self-tests run because it self-selects,
# but a helper-only diff would otherwise leave the consumer files unselected —
# and a silently over-eager stripper makes those pins vacuous without failing
# anything here. The rule pulls the consumers in with it.
#
# #1442 added a fourth consumer, tests/test_river_ts_text_identity_cleanup.py,
# and moved two more pieces of shared vocabulary into the helper
# (`assert_text_fact_columns`, `strip_all_subqueries`), so a helper-only diff can
# now blunt the out-of-boundary cleanup oracle too.
SQL_SHAPE_ORACLE_TESTS: tuple[str, ...] = (
    "tests/test_sql_shape_helpers.py",
    "tests/test_river_ts_read_path_surrogate_keys.py",
    "tests/test_river_ts_text_identity_cleanup.py",
    "tests/test_display_coverage_refresh.py",
    "tests/test_migrations.py",
    # Fifth consumer (#1442 round-2): the latest-product fallback's scan-guard
    # fold-away pins moved from split substrings to whole-guard verbatim ones,
    # which only stay readable through `outer_predicates`.
    "tests/test_qhh_latest_fallback_pushdown.py",
    # #1980 (epic #1979) added two more members of the SAME oracle rather than a
    # separate narrow group: `strip_*`, `outer_predicates` and the text-identity
    # vocabulary now live in packages/common/river_ts_render.py, and these two
    # suites are that module's contract and its equivalence proof. Joining the
    # group means every rule that already routes a reader to the oracle routes it
    # to them too, and a helper-only diff still runs the whole set.
    "tests/test_river_ts_render.py",
    # #1980 round 5 (fixture decision 18): the committed §4.1 reference-lexer
    # differential. It is the ONLY suite that can see the module's scanner
    # disagreeing with PostgreSQL on a construct no hand-written pin happens to
    # name, and its subject is `packages/common/river_ts_render.py` — so a
    # helper-only diff that never touches this file must still run it, which is
    # exactly what membership of this group buys.
    "tests/test_river_ts_render_reference_lexer.py",
    "tests/test_river_ts_template_golden.py",
)


# I11 #1990 task 7.2 — the FORCING counterpart of the group above, and
# deliberately a SEPARATE tuple rather than three more members of it.
#
# The two sets have different subjects and different lifetimes: `tasks.md` 6.3
# deletes the river renderer's legacy path and collapses its oracles while the
# forcing transition is still open (`tasks.md` 8.3 is what finally retires this
# one). Folding them together would route every river reader diff at three
# forcing suites for nothing.
#
# Separate does NOT mean uncoupled, and the coupling that exists is deliberately
# visible here: `tests/test_forcing_read_path_store_routing.py` imports four
# private helpers of `tests/test_qhh_latest_fallback_pushdown.py` — a member of
# the river group above — at MODULE scope, so `_build_suite_importer_index`
# carries the edge. A 6.3 PR that touches those helpers therefore selects the
# forcing suite and goes red on that PR, which is the correct outcome: the
# dependency is real, and the PR that breaks it is the one that should see it
# rather than the post-merge master run.
#
# WHAT THIS RIDER IS FOR. The forcing discovery-set census pins a mention count
# for sixteen production files, but none of their own rules routed the census
# suite — so a new `met.forcing_station_timeseries` mention in, say,
# `workers/forcing_producer/store.py` was red only on the POST-MERGE master run,
# not on the PR that introduced it. River carries the identical rider on every
# registered path plus a wiring meta-test; this is the forcing half of both.
# `tests/test_select_ci_tests.py` derives the path set from the census and the
# register, so a file entering either is routed or red.
FORCING_SQL_SHAPE_ORACLE_TESTS: tuple[str, ...] = (
    "tests/test_forcing_ts_render.py",
    "tests/test_forcing_ts_template_census.py",
    # Task 7.2's own oracle: byte identity against the pre-wiring snapshot, the
    # "store is the literal legacy" AST sweep (must-preserve M6) and the narrow
    # variants' shape invariants. It reads every wired reader module, so a diff
    # to one of them must run it.
    "tests/test_forcing_read_path_store_routing.py",
)


# Canonical readonly-boundary corpus. The three partitions import the generic
# validator directly; the identity-envelope suite is the fourth consumer of the
# route-smoke identity extractor/comparison. Retired selective-cold acceptance
# wrappers are not consumers of this surviving contract.
READONLY_DB_VALIDATION_TESTS: tuple[str, ...] = (
    "tests/test_readonly_db_validation.py",
    "tests/test_readonly_db_validation_probes.py",
    "tests/test_readonly_db_validation_routes.py",
    "tests/test_pipeline_ops_identity_envelope.py",
)


# #2074: `tests/test_hydro_display_mvt_scaling.py` (4888 lines, 210 cases) was
# partitioned by topic to retire its `.large-file-guard.json` exemption. The base
# path SURVIVES as one partition on purpose — three registries pin that literal
# string: `openspec/specs/ci-contract-baseline/spec.md`, the
# `infra/systemd/nhms-display-api.service` rule below (whose only reader is
# `test_systemd_workers_receive_shared_file_cache_default`, which stayed there),
# and the `services/tiles/mvt.py` closure anchor in
# `tests/test_select_ci_tests.py`.
#
# TWO tuples, not one, because the two guarded families have different derived
# closures (#1455/#1672 derive them from the tracked tree, so this is a
# transcription of the derivation and not a hand-curated list):
#
# * every partition is a non-gated importer of `services.tiles.mvt` — directly,
#   or one hop through `apps/api/routes/hydro_display.py` in the case of
#   `..._catalog_cache.py`;
# * `..._discovery.py` and `..._national_sql.py` import no `apps.api.routes`
#   module at all (they drive `services/tiles/mvt.py` helpers through the shared
#   fakes in `tests/hydro_display_mvt_helpers.py`), so they are absent from the
#   facade closure. They stay in the PR lane through the mvt rule; a partition
#   that starts importing the facade reds in the closure guard rather than
#   silently dropping out of the lane.
HYDRO_DISPLAY_MVT_SCALING_TESTS: tuple[str, ...] = (
    "tests/test_hydro_display_mvt_scaling.py",
    "tests/test_hydro_display_mvt_scaling_catalog.py",
    "tests/test_hydro_display_mvt_scaling_catalog_cache.py",
    "tests/test_hydro_display_mvt_scaling_coverage_order.py",
    "tests/test_hydro_display_mvt_scaling_discovery.py",
    "tests/test_hydro_display_mvt_scaling_feature_budget.py",
    "tests/test_hydro_display_mvt_scaling_instants.py",
    "tests/test_hydro_display_mvt_scaling_national_routes.py",
    "tests/test_hydro_display_mvt_scaling_national_sql.py",
)

HYDRO_DISPLAY_MVT_SCALING_FACADE_TESTS: tuple[str, ...] = tuple(
    test
    for test in HYDRO_DISPLAY_MVT_SCALING_TESTS
    if test
    not in {
        "tests/test_hydro_display_mvt_scaling_discovery.py",
        "tests/test_hydro_display_mvt_scaling_national_sql.py",
    }
)


# #1895 R1.6 C4 production-acceptance corpus. The public freeze/bind/verify
# suite and the boundary-parameter/identity/closed-stdout partition are ONE
# contract: the boundary module imports helpers from the core suite at module
# scope. Explicit sorted tuple, never derived at import time.
C4_PRODUCTION_ACCEPTANCE_TESTS: tuple[str, ...] = (
    "tests/test_node27_c4_production_acceptance.py",
    "tests/test_node27_c4_production_acceptance_boundaries.py",
)

# #1895 R1.4 retained PGDATA SQL/API workload owner. Producer, CLI, capture,
# plan and IO modules share one assertion-bearing suite plus the shipping
# forecast named-binding and selector meta suites.
NODE27_PGDATA_WORKLOAD_TESTS: tuple[str, ...] = (
    "tests/test_node27_pgdata_workload.py",
    "tests/test_node27_pgdata_workload_plan.py",
    "tests/test_node27_pgdata_workload_io.py",
    "tests/test_forecast_api.py",
    "tests/test_forecast_store_routing.py",
    "tests/test_select_ci_tests.py",
)


CHANGED_TEST_FILE_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        "tests/test_sql_shape_helpers.py",
        SQL_SHAPE_ORACLE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_c4_production_acceptance_boundaries.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload_plan.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_workload_io.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),

    PathTestRule(
        "tests/test_orchestration_chain.py",
        FILE_JOURNAL_READ_STATE_TESTS,
        only_when_any_changed=FILE_JOURNAL_READ_STATE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_production_scheduler.py",
        FILE_JOURNAL_READ_STATE_TESTS,
        only_when_any_changed=FILE_JOURNAL_READ_STATE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_orchestration_chain.py",
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
        only_when_any_changed=ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS,
    ),
    PathTestRule(
        "tests/test_production_scheduler.py",
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
        only_when_any_changed=ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS,
    ),
)


# Support modules under `tests/` (fixtures, helpers, fakes) are not collectible,
# so they map to the meta-guard suite plus ci.yml's full-tree collect-only smoke
# — import/syntax only, zero assertions (#1453/#1454). For a support module that
# real suites import at file level, that lane is blind to exactly the breakage a
# fixture edit causes, so #1487 routes such a module to its non-gated top-level
# importer suites instead. Exact paths, no globs: the rule table is closed
# against the tracked importer tree by tests/test_select_ci_tests.py (required
# sets are derived, never frozen), so a new importer suite reddens naming the
# module and missing suite. `tests/integration_helpers.py` remains deliberately
# absent under issue #1487's measured partial-coverage carve-out;
# `tests/conftest.py` left that carve-out in #1571 because its skip-guidance
# contract also requires the node-22 invariant owner.
# #1442 note: `tests/integration_helpers.py` also owns a statement registered in
# tests/test_river_ts_text_identity_cleanup.py, so a diff to it should ideally
# select that oracle. It does not, because of the carve-out above: the file maps
# to the meta-guard suite only. The oracle still guards it on every OTHER path —
# it is a consumer of tests/test_sql_shape_helpers.py, so the SQL_SHAPE_ORACLE
# rule runs it on a helper diff, and it self-selects on its own diff. Closing the
# gap belongs to #1487's carve-out, not here.
SUPPORT_MODULE_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        # #1571 local-repair: tests/conftest.py is a non-collectible support
        # module, so without a SUPPORT_MODULE_TEST_RULES entry it collapses to
        # the meta-guard only. It has two file-level non-gated importer suites
        # (tests/test_integration_gate.py, tests/test_grid_stability_verification.py)
        # a fixture edit breaks, and the #1487 carve-out's exact skip-guidance
        # clause is asserted by the node-22 owner (test_conftest_skip_guidance_
        # points_to_runbook). This rule is reached through the `tests/**`
        # changed-test branch — BEFORE PATH_TEST_RULES — so it preserves the
        # selector meta-guard rider and adds the node-22 owner. Deliberately
        # NOT a PATH_TEST_RULES row: the `tests/**` branch handles conftest and
        # a PATH row would be dead. The `database:`-filter carve-out remains
        # recorded and pinned elsewhere; this routing is additive to it.
        "tests/conftest.py",
        (
            "tests/test_grid_stability_verification.py",
            "tests/test_integration_gate.py",
            "tests/test_node27_docker_collection_gate.py",
            *NODE22_ENTRYPOINT_INVARIANT_TESTS,
        ),
    ),
    PathTestRule(
        "tests/fixtures/mapping_builder/in_memory_grid_snapshot.py",
        (
            "tests/test_mapping_builder_algorithm.py",
            "tests/test_mapping_builder_binding.py",
            "tests/test_mapping_builder_cli.py",
            "tests/test_mapping_builder_evidence.py",
            "tests/test_mapping_builder_integration.py",
        ),
    ),
    PathTestRule(
        "tests/slurm_template_helpers.py",
        (
            "tests/test_production_slurm_validation.py",
            "tests/test_slurm_array_contract.py",
        ),
    ),
    PathTestRule(
        # The recalibration carry-over package fixtures, fakes AND the
        # independent fingerprint oracle (#1697). The oracle is why this rule
        # matters more than a fixture-builder rule usually does: it re-implements
        # the documented hash format from the fixture bytes, so a change here can
        # flip the gate suites from "gate proven" to "gate agreeing with itself"
        # without touching a line of production code. After the CLI suite split
        # this rule lists the recalibration core suite plus BOTH recalibration
        # CLI modules (the end-to-end and the validation split); the baseline
        # CLI suite ALSO top-level-imports `_write_package`, the calibration
        # constants and `_IC_V1`/`_PARA_V1`, so it is a fourth direct consumer
        # and is listed here too. All are sub-second.
        "tests/state_clone_recalibration_fixtures.py",
        (
            "tests/test_state_clone_recalibration.py",
            "tests/test_state_clone_recalibration_cli.py",
            "tests/test_state_clone_recalibration_cli_validation.py",
            "tests/test_state_clone_baseline_cutover_cli.py",
        ),
    ),
    PathTestRule(
        # The CLI environment helpers shared by both recalibration CLI modules
        # (extracted at the §6.8 split). A change here can silently alter what
        # either module's dispatch/apply tests build, so both consumers must run;
        # the suite names are not same-name derivable, hence the explicit route.
        "tests/state_clone_recalibration_cli_fixtures.py",
        RECALIBRATION_CLI_FIXTURES_TESTS,
    ),
    PathTestRule(
        # The #1735 lineage index builders: every lineage suite publishes REAL
        # index entries through `publish_state_snapshot_index`, so a change to
        # the builder shape (clone provenance pass-through, `usable_flag`)
        # silently changes what the resolver reads. `test_scheduler_generation.
        # py` and `test_state_manager_generation_history.py` import the builders
        # inside a function body, so the derived non-gated closure (which sees
        # module-level imports only) does not require them — but they are routed
        # anyway: the closure is a FLOOR, not a ceiling, and this PR's own fix
        # changed `index_entry`'s signature (a keyword-only `usable_flag`), which
        # is exactly the class of change a function-body caller breaks on while
        # the floor stays green. Cost: +12.2s for the two, against a fixture
        # whose whole purpose is to be the shared index-entry shape.
        "tests/lineage_state_index_fixtures.py",
        (
            "tests/test_scheduler_backfill.py",
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_generation.py",
            "tests/test_state_manager_generation_history.py",
        ),
    ),
    PathTestRule(
        # #2074: the single home of every double the nine hydro-display MVT
        # partitions share. Without this entry the module is a non-collectible
        # `tests/` support module, so a fixture-only diff would collapse to the
        # selector meta-guard and none of the 210 cases that depend on these
        # fakes would run. The fakes are not passive: `_dual_patch` decides
        # WHETHER a patch bites at all (both the facade and
        # `hydro_display_catalog` homes, #2026), `_NationalRouteSession`
        # classifies statements by SQL landmark and so decides which branch each
        # route case exercises, and `_TILE_ROUTE_LOGGER` is the literal logger
        # name the `#2030` negative caplog assertions filter on — a typo there
        # makes them pass vacuously. Every partition imports it at module scope,
        # so the routed set IS the derived closure.
        "tests/hydro_display_mvt_helpers.py",
        HYDRO_DISPLAY_MVT_SCALING_TESTS,
    ),
    PathTestRule(
        # #2074: the single home of the six mock stores, the retry gateway double
        # and the eight private assertion helpers the three API-contract
        # partitions share. Without this entry the module is a non-collectible
        # `tests/` support module, so a doubles-only diff would collapse to the
        # selector meta-guard and none of the 38 cases that depend on these stubs
        # would run. The stubs decide what the assertions see: `_RunStore` and
        # `_ModelRegistryStore` are the response shapes three suites compare
        # against the published schemas, `_assert_success_envelope` is what makes
        # the envelope claim at all, and `_bounded_qhh_latest_reflected_value` is
        # the reflected-value bound the QHH-latest identity cases hang on. The
        # routed set is the derived closure: the three partitions plus
        # `tests/test_openapi_response_conformance.py`, whose module-scope
        # `from tests.api_contract_helpers import _ModelRegistryStore, _RunStore`
        # is the fourth edge.
        API_CONTRACT_HELPERS_PATH,
        API_CONTRACT_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # Pins the modes `provider_atomic`'s two fail-closed gates inspect, for
        # tests that PRE-create a lock parent or a provider destination (#1513).
        # Its whole purpose is to make those tests independent of the ambient
        # umask, so the breakage it prevents is invisible on the umask-0022 CI
        # runner and shows up only on node-27 (umask 0002) -- exactly the class
        # the meta-guard collapse cannot catch, since import/syntax succeeds
        # either way.
        "tests/provider_mode_helpers.py",
        (
            "tests/test_production_scheduler.py",
            # #1943: the journal-root authority suite builds its symlinked and
            # mode-sensitive journal roots through
            # `make_directory_with_explicit_mode`, so a helper change moves what
            # its refusal cases actually construct.
            "tests/test_scheduler_journal_root_authority.py",
            # #1101: four of the fifteen refresh partitions import this helper at
            # module scope (the provider-atomic, terminability-probe,
            # worker-mirror and predicate suites). The other eleven reach it
            # only through `tests/scheduler_refresh_helpers.py`, and the
            # support-module closure guard derives DIRECT importers only, so
            # listing them here would be routing noise the guard cannot justify
            # -- the helper's own rule below carries them instead.
            "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
            "tests/test_scheduler_refresh_provider_atomic.py",
            "tests/test_scheduler_refresh_terminability_probes.py",
            "tests/test_scheduler_refresh_worker_mirror_transactions.py",
            "tests/test_scheduler_state_index_repair.py",
            "tests/test_state_manager.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_source_cycle_raw_manifest.py",
            # #1102: two of the seven publisher partitions import this helper at
            # module scope — the manual-CLI one pre-creates a provider
            # destination and a mode-sensitive parent, the manifest-audit one
            # builds its registry destination through
            # `make_directory_with_explicit_mode`. The other five never open a
            # gated surface, so listing them would be routing noise.
            "tests/test_publish_registry_manifest_audit.py",
            "tests/test_publish_registry_manual_cli.py",
        ),
    ),
    PathTestRule(
        # #1611: the four replay partitions share their whole fixture surface
        # through this helper (the `fixture` / `private_umask_fixture` factories,
        # the alias-identity and fsync-failure seams, the index-entry builder).
        # Each partition imports it at module scope, so all four are its derived
        # importer closure and a helper-only diff must run all four.
        STATE_INDEX_COPYBACK_REPLAY_HELPERS_PATH,
        STATE_INDEX_COPYBACK_REPLAY_TESTS,
    ),
    PathTestRule(
        # #1101: the runtime fixture surface of the fifteen refresh partitions --
        # the RefreshConfig factory, both provider-pipeline stubs, the four-lane
        # tracked transaction fixture and the #1080 gate drivers. Eleven of the
        # fifteen partitions import it at module scope; those eleven are its
        # derived importer closure and a helper-only diff must run all of them.
        SCHEDULER_REFRESH_HELPERS_PATH,
        SCHEDULER_REFRESH_HELPER_TESTS,
    ),
    PathTestRule(
        # #1101: the static receipt/classification corpora the same corpus
        # shares, including the ONE `_receipt_schema_validator` factory (the
        # monolith defined that name twice; only the later binding was ever
        # live). Five partitions import no name from it, so its targets are the
        # ten derived importers rather than the whole corpus -- the
        # support-module closure guard derives that set from the tracked tree, so
        # an eleventh importer reddens there instead of rotting.
        SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH,
        SCHEDULER_REFRESH_RECEIPT_HELPER_TESTS,
    ),
    PathTestRule(
        # A 0-byte package file with a rule looks wrong until you follow the
        # import: `from tests import X` contributes the base name `tests`, and
        # the repo's derivation authority deliberately aliases a package
        # `__init__.py` to the package itself (tests/test_select_ci_tests.py,
        # _dotted_module_name + test_dotted_module_name_maps_a_package_init_to_the_package).
        # These three suites therefore ARE its derived importers, and a PR that
        # turns this file into a real package surface would otherwise run none of
        # them. Measured 454 passed in 40.18 s locally — inside the lane budget.
        "tests/__init__.py",
        (
            "tests/test_integration_gate.py",
            "tests/test_node27_docker_collection_gate.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        # A mock SHUD CLI nothing imports: workers/shud_runtime/runtime.py runs it
        # as `[sys.executable, <path>, *args]`, so the consumption edge is the
        # exact literal `"tests/mock_shud_omp.py"` in the consumer's source, not
        # an import. An import-only derivation reads this module as 0-importer and
        # collapses it to the meta-guard, running none of the assertions that
        # depend on the mock's output contract (#1498). The three suites below are
        # its derived literal-path consumers; measured 251 passed in ~27 s for the
        # first two, inside the lane budget. HONEST LIMIT: test_e2e.py's
        # consumption sits inside function-level `@pytest.mark.e2e` tests that
        # auto-skip in the PR lane, so that file contributes ZERO mock assertions
        # there — it is routed because the edge exists in the tree and closure
        # integrity is what the guard derives, not because it executes the mock.
        "tests/mock_shud_omp.py",
        (
            "tests/test_shud_runtime.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
        ),
    ),
    PathTestRule(
        # The #1872 retention partition's shared constants/helpers. The five
        # collectible retention partitions that import it do so at module scope
        # (a design requirement: selector importer derivation must see the
        # dependency), so a fixture edit breaks all five during PR-lane
        # collection. They are the derived importer set; the meta-guard rider
        # covers the tree-derived guards this very routing can invalidate.
        # #2238's copyback-mutex partition joined that set: it imports
        # EXTRA_CONFIG/NOW/_seed_cycle/_pass_scheduler from here at top level,
        # so it is a derived importer like the other four, not a rider.
        # 25 tests in 5.27s (median of three runs of the then-single
        # `tests/test_retention_copyback_mutex.py`: 5.27/5.24/5.30s).  The same
        # 25 cases now collect from `tests/test_retention_copyback_mutex_budget.py`
        # plus `tests/test_retention_copyback_mutex_protocol.py`, which is what
        # `RETENTION_COPYBACK_MUTEX_TESTS` below expands to.
        # #2259 split that partition in two and moved its 99-line fixture
        # preamble into this helper, so the derived importer set is SIX, not
        # five, and the helper now also owns the `_forbid_acquisitions` /
        # `_observe_removals` monkeypatch seams. Dropping either partition here
        # reds the support-module closure guard, which derives importers from
        # the tracked tree rather than from this tuple.
        RETENTION_COPYBACK_MUTEX_HELPERS_PATH,
        (
            "tests/test_retention.py",
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
        ),
    ),
    PathTestRule(
        # Shared journal-retention fixtures are top-level imported by both
        # behavioral partitions, so fixture-only changes must execute each.
        "tests/scheduler_journal_retention_fixtures.py",
        (
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
        ),
    ),
    PathTestRule(
        # The #1564 split-demote suites' shared fixture module. The four split
        # suites import it at file level, and the public operator-recovery cycle
        # tests import it through a local (function-scope) import, which the
        # derived importer scan does not see — so the rule names all five
        # consumers explicitly.
        "tests/orchestrator_demote_reserved_job_helpers.py",
        (
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_orchestration_chain.py",
        ),
    ),
    PathTestRule(
        # #1809: the shared store/cohort/identity fixtures extracted from the
        # gateway-reconcile monolith. The 22 file-level importing partitions are
        # named exactly (derived-set members, all sub-second to import);
        # store_reset is not a consumer — its shells build duck-typed stores and
        # import nothing here. The five demote/chain suites below are the known
        # ultimate consumers reached through the demote helper
        # (tests/orchestrator_demote_reserved_job_helpers.py imports
        # `_file_cohort_repository` from this module at file level; the four
        # split-demote suites import that helper at file level and the public
        # operator-recovery chain suite at function scope) — a
        # support-to-support edge the derived AST scan cannot see, so they are
        # listed explicitly here. tests/test_production_scheduler.py stays
        # excluded on the deliberate 1870-test runtime-budget boundary: its only
        # consumption is a function-local import that would buy a fixture edit
        # the whole suite lane.
        "tests/gateway_reconcile_helpers.py",
        (
            "tests/test_gateway_reconcile_comment_accounting.py",
            "tests/test_gateway_reconcile_comment_capability.py",
            "tests/test_gateway_reconcile_comment_sacct_bounds.py",
            "tests/test_gateway_reconcile_file_cohort_authority.py",
            "tests/test_gateway_reconcile_file_cohort_comment.py",
            "tests/test_gateway_reconcile_file_cohort_identity.py",
            "tests/test_gateway_reconcile_file_cohort_projection.py",
            "tests/test_gateway_reconcile_file_submit_barrier.py",
            "tests/test_gateway_reconcile_grace_guard.py",
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_identity_invariants.py",
            "tests/test_gateway_reconcile_identity_release.py",
            "tests/test_gateway_reconcile_inflight_identity.py",
            "tests/test_gateway_reconcile_inventory.py",
            "tests/test_gateway_reconcile_master_transitions.py",
            "tests/test_gateway_reconcile_reservation_lifecycle.py",
            "tests/test_gateway_reconcile_round10.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
            # #1850: the binding-provenance and claimant-exclusivity suites
            # top-level-import this helper at file level and are sub-second
            # beside the partitions above, so they join the exact rule rather
            # than riding the closure guard as a rule-gap exclusion.
            "tests/test_gateway_reconcile_binding_provenance.py",
            "tests/test_gateway_reconcile_claimant_exclusivity.py",
            # The five ultimate consumers via the demote helper (see above).
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_orchestration_chain.py",
        ),
    ),
    PathTestRule(
        # #1809: the writer/barrier utilities extracted from the monolith. Its
        # six file-level importing partitions are the derived consumer set.
        "tests/gateway_reconcile_writer_helpers.py",
        (
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
        ),
    ),
    PathTestRule(
        # #1912: the non-collectible helper owning the Basins publication corpus's
        # shared fixture builders (`_write_valid_inventory`, `_make_valid_model`,
        # `_object_store_env`, the canonical required-file/manifest/CLI helpers and
        # the identity snapshot). All seven collectible partitions import it at module
        # scope, and so does `tests/test_basins_package.py`, whose two helpers moved
        # here from the former monolith. A builder edit can therefore flip publication
        # oracles from "refused" to "published" without touching a single test file, so
        # the whole consumer set must run — the selector's meta-guard rider is added by
        # the support-module branch itself and is deliberately NOT repeated here.
        BASINS_PACKAGE_HELPERS_PATH,
        BASINS_PACKAGE_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #1948: the QHH bootstrap helper owns the registry-fixture builder, the
        # scheduler-readiness seeds/teardown, the four readiness constants and the
        # `qhh_scheduler_canonical_readiness` fixture. All three partitions import it at
        # module scope, so a helper-only diff must run the whole consumer set — and it
        # must ALSO open the real-database lane, which SUPPORT_MODULE_TEST_RULES cannot do
        # on its own: that is why the same path is an exact `database:` trigger in ci.yml.
        # The meta-guard rider is added by the support-module branch, deliberately not here.
        QHH_PRODUCTION_BOOTSTRAP_HELPERS_PATH,
        QHH_PRODUCTION_BOOTSTRAP_TESTS,
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the non-collectible register of river read
        # templates. Every SQL-shape oracle in the group derives its coverage
        # FROM it — which templates exist, how many aids each carries, how many
        # times each file names the fact table — so an edit here silently changes
        # what four suites assert without touching any of them. The forecast
        # store-routing and hydro-display MVT suites also import the register
        # and must run on changes.
        # #2208: the national probe's file-level integration gate makes it a
        # database-lane consumer, not a unit target. ci.yml routes this support
        # path there; retain only the non-gated importers here.
        # #2074: the hydro-display MVT suite was partitioned, and exactly ONE
        # partition imports this register — `..._instants.py`, whose
        # `hydro_display:mvt_source_identity_probe` cases render the registered
        # template and compare it to the probe's statement. The other eight
        # partitions never touch it, so listing them here would buy suites that
        # cannot red on a register edit.
        "tests/river_ts_template_registry.py",
        (
            *SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forecast_store_routing.py",
            "tests/test_hydro_display_mvt_scaling_instants.py",
        ),
    ),
    PathTestRule(
        # I11 #1990 forcing_ts_render (cut a): the forcing counterpart of the
        # river register above — the non-collectible module that owns the
        # discovery roots, the exemption/unwired-reader ledgers, the renderer
        # constant declarations and the mention counter the forcing
        # discovery-set census judges the tree with. Everything that census
        # asserts (which files may name `met.forcing_station_timeseries`, how
        # many times each does, which mentions are registered reads) is DERIVED
        # from this module, so an edit here rewrites the verdict without
        # touching the suite that renders it.
        # Its non-gated importer closure is exactly one suite,
        # tests/test_forcing_ts_template_census.py, derived over both edge kinds
        # (module-scope import + literal-path consumption). Nothing is
        # deliberately excluded here: unlike tests/river_ts_template_registry.py
        # (#2208) this module has no integration-gated importer at all, so no
        # `database:` entry in ci.yml is owed for it.
        # Cut (a) landed the register EMPTY of reader entries; cut (b) (task
        # 7.2) populated it with the nine and brought a SECOND importer,
        # tests/test_forcing_read_path_store_routing.py, which parametrises its
        # byte-identity / M6 / narrow-shape assertions over this register — so
        # an entry added or dropped here rewrites what that suite asserts too.
        # Both importers are listed; both are non-gated.
        "tests/forcing_ts_template_registry.py",
        (
            "tests/test_forcing_ts_template_census.py",
            # Cut (b)'s second importer: the register is now the parametrisation
            # source of the byte-identity / routing / narrow-shape oracle too, so
            # an entry added or dropped here changes what that suite asserts
            # without touching it.
            "tests/test_forcing_read_path_store_routing.py",
            # #1991 task 7.3's third importer: the renderer suite takes this
            # module's REPO_ROOT to read
            # `db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`
            # and hold the flipped FORCING_TABLE_LEGACY constant against the
            # `ALTER TABLE … RENAME TO` that made it true. A one-name import, but
            # the closure check is over edges and not over how much is imported.
            "tests/test_forcing_ts_render.py",
        ),
    ),
    PathTestRule(
        # #2451: the segment-index measurement bench's pass criteria. This module
        # owns the three `design.md` criteria a measured plan must satisfy, the
        # branch identity columns and the shared-hit floors — so an edit here
        # moves what every cell of the cross product is judged against without
        # touching a line of the suites that judge. Its two non-gated importers
        # (the synthetic-plan proof, which is also the same-name owner, and the
        # offline capture/digest suite) import it at module scope.
        # The integration bench (tests/test_river_timeseries_stats_index_choice_
        # integration.py) is a third module-scope importer and is deliberately
        # ABSENT: its file-level `pytestmark = pytest.mark.integration` would skip
        # it in the PR lane, so routing it buys constant skips (#1447, and the
        # same call #2208 made for tests/river_ts_template_registry.py). This
        # table cannot open the `database:` lane for it either; that is ci.yml's.
        # 52 passed in 0.50 s for both routed suites together.
        "tests/river_ts_plan_criteria.py",
        (
            "tests/test_river_ts_plan_criteria.py",
            "tests/test_river_ts_stats_harness_offline.py",
        ),
    ),
    PathTestRule(
        # #2451: the bench's condition seed — the scenario table, the step count
        # and the array-literal parser the recorded fact rows are digested
        # through. The offline suite is its only non-gated module-scope importer
        # and asserts exactly those shapes against the real producer, so a seed
        # edit that the suite does not run is an unjudged change to the measured
        # input. The integration bench imports it too and is excluded for the
        # file-level gating reason recorded on the rule above.
        "tests/river_ts_stats_matrix_seed.py",
        ("tests/test_river_ts_stats_harness_offline.py",),
    ),
    PathTestRule(
        # #1913: the registry-import helper owns the former monolith's 19 support
        # functions, `_FakeRiverSegmentCursor` and the four private constants. Its eleven
        # direct collectible importers are the seven registry suites plus the four #1102
        # publisher partitions that name `_write_registry_fixture` / `_make_valid_model`,
        # all at module scope. The TWO support-to-support edges are invisible to the
        # non-recursive support-rule walk, so their collectible reach is routed
        # explicitly here — `tests/qhh_production_bootstrap_helpers.py` (module-scope
        # `_write_registry_fixture`, function-scope `_package_manifest_for_model`) brings
        # the three QHH bootstrap partitions, and `tests/publish_registry_helpers.py`
        # (module-scope `_make_valid_model`) brings the one publisher partition that
        # reaches `_write_healthy_basin_pair` without importing this helper itself.
        # Fifteen collectible suites total, meta-guard rider added by the support-module
        # branch itself.
        BASINS_REGISTRY_IMPORT_HELPERS_PATH,
        BASINS_REGISTRY_IMPORT_HELPERS_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #1102: the shared fixture surface of the seven publisher partitions — the
        # module-wide autouse `basins_package_source_identity` stub and its two readers,
        # the canonical catalog seeder, the healthy/broken/radiation basin-pair builders,
        # the fake inventory/packager/sources triple and the calibration-declaration
        # builders. The autouse stub decides what EVERY case in the corpus sees (without
        # it the real content-addressed identity runs and the synthetic inventories stop
        # being stable), so all seven import it at module scope and the routed set IS the
        # derived closure.
        PUBLISH_SCHEDULER_REGISTRY_HELPERS_PATH,
        PUBLISH_SCHEDULER_REGISTRY_TESTS,
    ),
    PathTestRule(
        # #1823: the shared surface of the fifteen entropy-audit partitions --
        # REPO_ROOT/BASELINE path constants, the memoized whole-repository
        # `build_report` accessor (a ~20s call the corpus asks for from a dozen
        # places), the finding selectors and every fixture builder. All fifteen
        # import it at module scope, so the routed set IS the derived closure;
        # without this row a helper-only diff collapses to the meta-guard,
        # because the filename is not `test_*` and never reaches the `tests/**`
        # suite branch.
        ENTROPY_AUDIT_HELPERS_PATH,
        ENTROPY_AUDIT_TESTS,
    ),
    PathTestRule(
        # #1103: the shared surface of the two node-22 entrypoint partitions --
        # the repo root, the node-22/node-27 root constants and the `_read`
        # both partitions call on every governed file. Neither partition can
        # run without it, so a helper-only diff must select both; the filename
        # is not `test_*` and never reaches the `tests/**` suite branch.
        NODE22_ENTRYPOINT_HELPERS_PATH,
        NODE22_ENTRYPOINT_INVARIANT_TESTS,
    ),
    PathTestRule(
        # #1103: the production-ops surface set. It decides WHICH files every
        # whole-document runbook guard scans, so a change here can silently
        # shrink six suites' reach to the (command-free) index page -- exactly
        # the vacuous pass the split had to avoid. Routed to the full reader
        # set, not just its importers, because the readers that pin a single
        # sub-runbook by path are governed by the same surface decision.
        PRODUCTION_OPS_RUNBOOK_HELPERS_PATH,
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
)


# #1728: the connection-attribution guards. Before this issue only
# apps/api/routes/hydro_display.py and the delegated-helper modules routed them,
# because those were the only registered surfaces. The unit-level guard added
# for #1728 is rooted at apps/api/route_registry.py and walks the six other
# business routers plus the packages/common stores they build, so a diff that
# drops a `fallback_application_name`, renames a surface, registers a new router
# or adds a connect-owning module to the unit's import closure now reddens these
# two suites — and NO other suite asserts any of that. Both are pure-AST plus a
# handful of monkeypatched connect probes (~4s together), so they are cheap
# enough to ride every rule that owns an attribution seam.
CONNECTION_ATTRIBUTION_TESTS: tuple[str, ...] = (
    "tests/test_node27_connection_attribution.py",
    "tests/test_node27_connection_attribution_delegated.py",
)
# The route modules the unit-level guard walks from the registry: each declares
# a module-level `_APPLICATION_NAME` and injects it into its store factories.
# #2078: apps/api/routes/forecast.py is deliberately absent — it gained an exact
# rule (tests/test_forecast_api.py) and these suites are MERGED into that entry
# instead, exactly like forecast_store.py / state_manager.py below: a duplicate
# pattern splits the module's ownership across two rules
# (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
# #2098: apps/api/route_registry.py — the walk's root — is deliberately absent
# for the same reason. It gained an exact rule (see the precipitation
# composition owners near the end of PATH_TEST_RULES) and these suites are
# MERGED into that entry. It never satisfied the `_APPLICATION_NAME` sentence
# above either: the registry declares no such name, it only composes the
# routers that do.
CONNECTION_ATTRIBUTION_ROUTE_PATHS: tuple[str, ...] = (
    "apps/api/routes/best_available.py",
    "apps/api/routes/data_sources.py",
    "apps/api/routes/models.py",
    "apps/api/routes/state_snapshots.py",
)

# The packages/common stores that carry the #1728 injection seam
# (`application_name=` through `from_env` down to the connect call). Two more —
# forecast_store.py and state_manager.py — already have exact rules, so the
# suites are MERGED into those instead of listed here: a duplicate pattern
# splits a module's ownership across two rules
# (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
# #1990 removed best_available.py from this tuple and gave it an exact rule (see
# the forcing-census block in PATH_TEST_RULES): it holds a registered forcing
# read template, so it needs the forcing oracle rider, and a duplicate pattern
# would split its ownership across two rules. CONNECTION_ATTRIBUTION_TESTS is
# MERGED into that entry — the same disposition forecast_store.py and
# state_manager.py already have, for the same reason.
CONNECTION_ATTRIBUTION_STORE_PATHS: tuple[str, ...] = (
    "packages/common/model_registry.py",
    "packages/common/object_store_forcing.py",
)
# #1704: the only suite that asserts the error-response log line exists, is
# redacted, is bounded, and that the request-id is not client-forgeable. Both
# halves of that contract live in apps/api/errors.py (the chokepoint) and
# apps/api/main.py (the handler install + the request-id middleware); the
# `apps/api/**` rule's three broad API suites assert none of it.
API_ERROR_LOGGING_TEST = "tests/test_api_errors_logging.py"
# #2010 precipitation raster service. The suite is the ONLY oracle for
# `services/precip/**` (window resolution, accumulation, PNG structure, file
# cache) and for the two `/api/v1/precip` routes; the three OpenAPI/contract
# suites are the hand-maintained-yaml and generated-frontend-types oracles the
# routes' public shape rides on. Shared by the directory rule and the exact
# route rule so the two cannot drift.
# #2098: also shared by the two application-composition owner rules
# (apps/api/route_registry.py, apps/api/main.py), for the same no-drift reason —
# four rules now name this tuple, so an edit to it moves all four together and
# the literal-string pins in tests/test_select_ci_tests.py are what catch it.
# #2074: `tests/test_api_contract.py` here is the RETAINED BASE path of the
# partitioned API-contract corpus, deliberately not `*API_CONTRACT_TESTS`. This
# rule's stated role is the hand-maintained-yaml / generated-frontend-types
# oracle, and every whole-document comparison of the corpus (static vs runtime,
# generated types vs committed schema, the declared-4XX census over all
# operations) stayed on the base path. The two partitions assert pipeline and
# resource route shapes at no precipitation path and never call `app.openapi()`,
# so widening this shared tuple would make every precip-tree diff pay for 27
# cases that cannot red on it — the padding the #2211 measurement rejects.
PRECIP_SURFACE_TESTS: tuple[str, ...] = (
    "tests/test_precip_overlay.py",
    "tests/test_openapi_drift.py",
    "tests/test_openapi_31_contract.py",
    "tests/test_api_contract.py",
)


PATH_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[0],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, "tests/test_pipeline_job_provenance_publisher.py"),
        stop_on_match=True,
    ),
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[1],
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[2],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, *CHAIN_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # scheduler.py is the allowlisted duplicate pattern: this non-stop entry
        # plus the stop-on-match FILE_JOURNAL entry below. #1455's additions
        # extend THIS existing entry rather than adding a third — the duplicate
        # allowlist records a two-entry layering, and a third would split the
        # module's ownership again.
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[3],
        (*ORCHESTRATOR_MANIFEST_SURFACE_TESTS, *SCHEDULER_IMPORTER_TESTS),
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[0],
        # Extended AT THE RULE SITE, not by editing DIRECT_GRID_SURFACE_TESTS:
        # the shared constant also serves the openspec-change pattern below,
        # whose selection must not move (#1455 scopes to the nine directories).
        (*DIRECT_GRID_SURFACE_TESTS, *DIRECT_GRID_CONTRACT_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        DIRECT_GRID_SURFACE_PATH_PATTERNS[1],
        DIRECT_GRID_SURFACE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1192), not by editing the shared constant:
        # safe_fs.py owns tests/test_safe_fs.py, while the journal mappings below
        # retain the file-journal closure. A separate stop_on_match rule would
        # shift selection because first match wins.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[0],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_safe_fs.py", *C4_PRODUCTION_ACCEPTANCE_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # The dedicated no-clobber publication owner is used by archive
        # publication and restore. It needs both its filesystem contract and
        # the two journal-retention behavioral partitions.
        "packages/common/safe_fs_publication.py",
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            "tests/test_safe_fs.py",
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
        ),
        stop_on_match=True,
    ),
    # C4 reads pinned evidence through evidence_io's descriptor-bound
    # byte/JSON identity primitives. This exact owner has no same-name suite,
    # so route the surviving C4 acceptance partitions without pulling retired
    # C1/C2/C3 suites; shared core/invariant riders remain additive outside
    # PATH_TEST_RULES.
    PathTestRule(
        "packages/common/evidence_io.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[1],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[2],
        (*FILE_JOURNAL_READ_STATE_TESTS, *FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1955), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves safe_fs.py and every other
        # journal pattern, whose selection must not move. This module owns the
        # create-capable import lane and `_rollback_execution_lock` — the two
        # places design D2/D3 decide whether a root may be created and which
        # tree the lock lands in — and the lane-adoption suite is their
        # requirement oracle, so this stop rule is where its importer gap
        # closes. DB-free, 76 tests in 0.70s, hence a rule rather than a
        # rule-gap exclusion.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_journal_root_lane_adoption.py"),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[4],
        (*FILE_JOURNAL_READ_STATE_TESTS, *ORCHESTRATOR_CLI_IMPORTER_TESTS),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[5],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[6],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[7],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#2238), not by editing the shared constant:
        # ORCHESTRATOR_CLI_IMPORTER_TESTS is also spliced into pattern[4]
        # (scheduler_journal_archive.py), which the copyback-mutex suite does not
        # import and whose selection must not move. cli.py is stop-rule owned, so
        # the broad `services/orchestrator/**` list that carries the other four
        # retention partitions (and the independent frontier suite, which is not
        # a partition) is unreachable here — this is where the fifth
        # partition's importer gap closes. The suite drives `cli._run_cleanup`,
        # the out-of-pass entrypoint that reads NHMS_OBJECT_STORE_COPYBACK_ROOT
        # from the environment and thereby decides which roots the deleter locks,
        # so an env-read or root-assembly edit here must run it. DB-free, 25
        # tests in 5.27s, hence a rule rather than a rule-gap exclusion.
        #
        # #1955/#1953 added two more at-site targets for the same reason. Both
        # drive `_click_main` AND `_argparse_main` and both own an exit-code and
        # stderr-rendering contract that lives HERE: the lane-adoption suite
        # pins the typed `FILE_JOURNAL_INVALID_ROOT` line and exit 2 on five
        # commands (`migrate-scheduler-state` among them, where the refusal is
        # inherited and the `except OrchestratorError` arm ordering decides
        # whether the code prefix survives), the budget-contract suite pins the
        # one typed line the recovery command renders when the whole-tree replay
        # raises. An arm-ordering or exit-code edit in cli.py must run them.
        # DB-free, 87 tests in 2.07s together.
        #
        # #1555/#1768 added a fourth at-site target for the same reason: the
        # operator re-entry confirmation suite's whole write side goes through
        # the shipped `cli.main(["confirm-operator-reentry", ...])` entry, so the
        # command's registration, argument parsing and exit codes live HERE. It
        # does not import scheduler_journal_archive.py, so it rides the rule site
        # rather than ORCHESTRATOR_CLI_IMPORTER_TESTS (spliced into pattern[4]).
        # DB-free, 23 tests in 46.71s — heavier than the three targets above but
        # far under the ~5 min per-module line, so a rule not an exclusion.
        #
        # #1186 added a fifth, on the same ground and for a cheaper suite: every
        # test in the operator-action listing suite drives the shipped
        # `cli.main(["list-operator-actions", ...])` entry, so the command's
        # registration, `--evidence-root` / `--passes` parsing and the 0/1/2/3
        # exit-code contract are decided HERE — the argparse fallback leg is
        # pinned against the click leg in the same file. It does not import
        # scheduler_journal_archive.py, so it rides the rule site rather than
        # ORCHESTRATOR_CLI_IMPORTER_TESTS (spliced into pattern[4]). DB-free, 75
        # tests in 0.34s, the cheapest target on this list.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[8],
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            *ORCHESTRATOR_CLI_IMPORTER_TESTS,
            # #2259 split the copyback-mutex partition in two; the EF-15 case
            # that drives `cli._run_cleanup` is in the budget half, but the
            # protocol half rides with it because the two share every fixture.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_journal_root_lane_adoption.py",
            "tests/test_file_journal_full_tree_budget_contract.py",
            "tests/test_operator_reentry_confirmation.py",
            "tests/test_operator_action_listing.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[9],
        FILE_JOURNAL_READ_STATE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#1943), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves packages/common/safe_fs.py
        # and the other journal patterns, whose selection must not move. The
        # root-authority suite top-level-imports
        # `_db_free_orchestration_repository_from_config` from scheduler_core.py
        # — the factory that must now refuse a symlinked journal root — so this
        # stop rule is where its importer gap closes.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[10],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_scheduler_journal_root_authority.py"),
        stop_on_match=True,
    ),
    PathTestRule(
        # Extended AT THE RULE SITE (#2260), not by editing the shared constant:
        # FILE_JOURNAL_READ_STATE_TESTS also serves every other journal pattern,
        # whose selection must not move. scheduler_runtime.py is stop-rule owned,
        # so the broad `services/orchestrator/**` list that carries the
        # copyback-mutex partition is unreachable here. It is the scheduler call
        # site that names the shared copyback root the retention deleter locks,
        # one of the mutex's two load-bearing modules (the other is
        # packages/common/copyback_guard.py, routed below), so this stop rule is
        # where its gap closes. The sibling tests/test_retention_extra_roots.py
        # gap stays open (issue boundary, recorded known limit).
        #
        # #1186 round 2 adds the second at-site target. scheduler_runtime.py
        # writes most of the top-level keys of a pass evidence payload (it keeps
        # adding to the dict `base_evidence` returned, `counts` and
        # `duplicate_exclusions` among them), and the scope-dimension closure pin
        # runs a REAL pass and asserts every published key carries a disposition.
        # A new key here without a disposition is exactly the silent false-exit-0
        # this PR exists to close, so that one node id — not the whole 2000-test
        # suite — rides this rule. Measured at 0.04s of call time.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[11],
        (
            *FILE_JOURNAL_READ_STATE_TESTS,
            # #2259: both halves of the split copyback-mutex partition.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            "tests/test_production_scheduler.py::test_every_dimension_a_real_pass_publishes_has_a_scope_disposition",
            # #1905/#2402 adds the third at-site target. scheduler_runtime.py is
            # the pass writer that reaches the evidence-size ladder (it hands the
            # payload to `_write_evidence` and takes the post-write status back
            # into SchedulerPassResult), and the non-blocking summary tier is
            # exactly what keeps that status TRUE instead of rewriting it to
            # `resource_limit_blocked`. The decidability suite is the oracle for
            # that agreement, and being module-scope-import-free it has no
            # importer derivation, so this stop rule is its only route for this
            # module. DB-free, ~20 tests in ~20s.
            "tests/test_scheduler_evidence_decidability.py",
            # #2405 adds the fourth at-site target on the same grounds: this
            # module registers the lease heartbeat's touch path on the
            # reservation it just reserved, and the reservation-lease suite is
            # the oracle for "registered only after `reserved`". Also
            # module-scope-import-free, so this stop rule is its only route for
            # this module. DB-free, 15 tests in ~1s.
            "tests/test_operator_action_reservation_lease.py",
            # #2442 adds the fifth at-site target. This module is the PASS
            # WRITER whose `run_once` status literals the closure pin reads with
            # `ast`: a status added or moved here is exactly what the pin has to
            # go red on, and the pin imports nothing at module scope, so this
            # stop rule is its only route for this module. DB-free, 4 tests in
            # ~0.3s.
            "tests/test_operator_action_status_closure.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        # #1748 recovery-CLI helper extraction.  Stop rule on
        # purpose: without it the broad `services/orchestrator/**` rule would
        # drag the full directory list for a module whose only production
        # consumer is cli.py.  The two suites that drive the operator-facing
        # channel and the signal/command e2e are named exactly, plus the demote
        # CLI security suite which shares the register boundary.
        "services/orchestrator/operator_released_reservation_recovery.py",
        RELEASED_RESERVATION_RECOVERY_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # The three object-store-root additions (#1455) are non-gated top-level
        # importer suites of the adapters that this rule already owns:
        # test_object_store_roots.py builds ERA5/GFS/IFS adapters directly, and
        # the bbox pair consumes workers/data_adapters/region.py. They are cheap
        # (0.8-2.4s) and every adapter shares the object-store root convention,
        # so they ride the directory list rather than three narrow rules.
        "workers/data_adapters/**",
        (
            "tests/test_gfs_adapter.py",
            "tests/test_ifs_adapter.py",
            "tests/test_era5_adapter.py",
            "tests/test_data_adapter_resolution.py",
            "tests/test_production_scheduler.py",
            "tests/test_object_store_roots.py",
            "tests/test_grid_registry_bbox.py",
            "tests/test_producer_bbox_preflight.py",
            "tests/test_e2e.py",
            "tests/test_e2e_ifs.py",
        ),
    ),
    PathTestRule(
        # base.py's remaining non-gated importers are orchestration-layer suites
        # borrowing its cycle-identity helpers (cycle_id_for / format_cycle_time
        # / CycleDiscovery); those stay with the rules that own them (#1455
        # `edge-consumer` routings). The archive contract independently rebuilds
        # cycle identity through cycle_id_for, so its focused suite belongs here.
        # #2420: the provenance publisher/importer and Ops identity envelope mint
        # cycle ids through cycle_id_for, so a helper-only PR must run them.
        "workers/data_adapters/base.py",
        (
            "tests/test_state_clone_cutover_hook.py",
            "tests/test_scheduler_journal_retention_archive.py",
            "tests/test_pipeline_job_provenance_importer.py",
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_ops_identity_envelope.py",
        ),
    ),
    PathTestRule(
        # #1455: the producer/cli/store/package surface has same-subject suites
        # that no rule reached. All are seconds-scale; the heavier e2e-named
        # importers are dispositioned in tests/test_select_ci_tests.py instead.
        "workers/forcing_producer/**",
        (
            "tests/test_forcing_producer.py",
            "tests/test_production_met_validation.py",
            "tests/test_forcing_producer_cli.py",
            "tests/test_grid_signature.py",
            "tests/test_grid_snapshot_registration.py",
            "tests/test_source_identity.py",
            "tests/test_timescale_write_guard_wired.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_producer_bbox_preflight.py",
            "tests/test_object_store_roots.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
            "tests/test_e2e_ifs.py",
            "tests/test_ifs_forecast_integration.py",
        ),
    ),
    PathTestRule(
        "workers/shud_runtime/**",
        (
            "tests/test_shud_runtime.py",
            "tests/test_runtime_mode.py",
            "tests/test_runtime_ic_header.py",
        ),
    ),
    PathTestRule(
        # #1455: the warm-start pair top-level-imports runtime.py and nothing
        # selected it. Narrow rather than on the directory list — neither
        # `__init__.py` nor `cli.py` imports warm-start behavior.
        "workers/shud_runtime/runtime.py",
        (
            "tests/test_warm_start.py",
            "tests/test_warm_start_chaining.py",
            "tests/test_direct_grid_e2e.py",
            "tests/test_e2e.py",
        ),
    ),
    PathTestRule(
        # #1455: nine cheap (0.6-4.6s) basin/registry suites top-level-import
        # modules of this small directory and none were selected. They ride the
        # directory list because the precision loss is nil — every one of them
        # is a basin-registry suite, and the whole added set runs in ~20s.
        "workers/model_registry/**",
        (
            "tests/test_model_registration.py",
            "tests/test_model_registry_basin_versions.py",
            "tests/test_model_registry_list_basins.py",
            "tests/test_basins_discovery.py",
            # #1832: `tests/test_basins_package.py` top-level-imports
            # `basins_discovery` (and `basins_calibration_overrides`), and is
            # the suite that owns the packaging contract those modules feed.
            # It rides the directory list for the same reason as the rest.
            "tests/test_basins_package.py",
            # #1912/#1903: the single publication core target is replaced by the seven-way
            # partition authority. Only `tests/test_basins_package_publication.py`
            # is same-name derivable from a Basins production module, and even that
            # derivation is not this rule's leg — so every one of the seven is proven
            # load-bearing by tests/test_select_ci_tests.py's per-edge RED rows.
            *BASINS_PACKAGE_PUBLICATION_TESTS,
            # #1913: the single registry monolith literal is replaced by the
            # seven-way partition authority. Only the retained core is same-name
            # derivable from a Basins production module, and even that derivation is
            # not this rule's leg for a probe like
            # `workers/model_registry/basins_reingest.py` — so every one of the seven
            # is proven load-bearing by tests/test_select_ci_tests.py's per-edge RED
            # rows.
            *BASINS_REGISTRY_IMPORT_TESTS,
            "tests/test_basins_reingest.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_hhe_mvt_binding.py",
            # #1813: this suite top-level-imports `basins_package` because it
            # owns the parity test binding the packager's forcing checksum
            # material to production-closure's reconstruction of it.  A change
            # to one implementation must run the test that pins both.
            "tests/test_production_object_store_validation.py",
            # #1102: the single publisher monolith literal expands to its seven
            # partitions. The whole corpus rides the directory list, not a
            # subset: `publish_all_basin_scheduler_registry` calls into
            # basins_discovery / basins_package / basins_radiation_template /
            # basins_calibration_overrides on every one of them, and the whole
            # added set is the same ~5s the monolith was.
            *PUBLISH_SCHEDULER_REGISTRY_TESTS,
            # #1948: the single QHH bootstrap monolith literal expands to the three
            # partitions; no prior target of this rule is dropped (the retained
            # historical path is QHH_PRODUCTION_BOOTSTRAP_TESTS[0]).
            *QHH_PRODUCTION_BOOTSTRAP_TESTS,
            "tests/test_qhh_scripts_static.py",
        ),
    ),
    PathTestRule(
        # #1711: every tracked module under workers/mapping_builder/ is owned by
        # this one directory rule selecting all tracked `tests/test_mapping_builder_*.py`
        # suites (MAPPING_BUILDER_TESTS — an explicit tuple today, guarded
        # against drift by the selector meta-suite's tree-derived
        # `_tracked_mapping_builder_suites()` equality assertion). Deliberately
        # NOT a stop rule: nothing earlier shadows the directory, and the
        # same-name derivation still adds tests/test_<module>.py where one
        # exists. The directory also joins DIRECTORY_RULE_AUDIT_PATHS so future
        # module/importer growth is dispositioned by the importer-gap guard
        # instead of silently falling out of the PR lane.
        #
        # The rule carries ONLY the mapping-builder package suites. rewrite.py's
        # three non-gated importer suites OUTSIDE the package (tests/test_state_clone.py,
        # tests/test_state_clone_cutover_hook.py, tests/test_state_clone_recalibration.py)
        # are deliberately NOT carried here: each already has an independent
        # owning surface (services/orchestrator/**, the state_clone_hook and
        # data_adapters/base rules, the node22-clone-script rule), so routing
        # them on every mapping-builder change would contaminate the lane. They
        # are dispositioned as `edge-consumer` pairs in
        # tests/test_select_ci_tests.py's INTENTIONAL_RULE_GAP_EXCLUSIONS.
        "workers/mapping_builder/**",
        MAPPING_BUILDER_TESTS,
    ),
    PathTestRule(
        # #1711: state_clone_hook.py's suite name is deliberately not same-name
        # derivable (no tests/test_state_clone_hook.py; its consumer suite is
        # the cutover-hook suite). Explicit irregular mapping.
        "packages/common/state_clone_hook.py",
        STATE_CLONE_HOOK_TESTS,
    ),
    PathTestRule(
        # #1711: the node-22 clone script's four suites are the recalibration
        # core, the recalibration CLI end-to-end, the recalibration CLI
        # validation split, and the baseline-cutover CLI suite, not same-name
        # derivable. Explicit irregular mapping.
        "scripts/node22_clone_direct_grid_cutover_states.py",
        NODE22_CLONE_CUTOVER_STATES_TESTS,
    ),
    PathTestRule(
        # #1455: `tests/test_output_parser.py` was the only target, so the cli
        # and dual-write suites — the ones that actually exercise the parser
        # package's entry points — never ran on an output_parser PR. Both are
        # sub-second and every module in this three-file directory is reachable
        # through them.
        "workers/output_parser/**",
        (
            "tests/test_output_parser.py",
            "tests/test_output_parser_cli.py",
            "tests/test_output_parser_dual_write.py",
            "tests/test_e2e.py",
            # #1714: parser.py is a registered connect-owning surface in the
            # attribution guard's per-file AST registry, and the suite
            # top-level-imports both it and the package, so BOTH directory
            # members' importer gaps close here. None of the four above assert
            # the component-level fallback_application_name identity, so a diff
            # that drops or renames it would otherwise reach CI green.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
        ),
    ),
    PathTestRule(
        # #1455: parser.py alone carries these two importers (46s + 3s), so they
        # stay off the directory list and are paid only by a parser.py PR.
        "workers/output_parser/parser.py",
        (
            "tests/test_analysis_pipeline.py",
            "tests/test_timescale_write_guard_wired.py",
            # #1442: the replace chain's three statements plus the dual-write
            # INSERT are registered in the zero-text-identity oracle. It sits on
            # the narrow parser.py rule rather than the package directory rule
            # because parser.py is the only module in the package the oracle
            # reads.
            "tests/test_river_ts_text_identity_cleanup.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #1455 closed 23 importer gaps here at once. Each addition is a
        # non-gated top-level importer of at least one module this rule owns,
        # and the whole added set measured ~55s locally — next to
        # test_orchestration_chain.py, which this rule already carried, that is
        # noise. They ride the directory list rather than 23 narrow per-module
        # rules because the alternative triples the rule table for suites nobody
        # would object to running on an orchestrator change; the modules with
        # genuinely distinct or expensive gaps (chain.py, scheduler.py, cli.py,
        # file_orchestration_journal.py) are owned by stop rules above and are
        # extended at THEIR sites instead.
        "services/orchestrator/**",
        (
            "tests/test_orchestrator.py",
            "tests/test_orchestration_chain.py",
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
            "tests/test_pipeline_job_provenance_copyback.py",
            "tests/test_pipeline_ops_identity_envelope.py",
            # The delegated attribution suite now top-level-imports
            # services.orchestrator (package __init__) and
            # pipeline_job_provenance.py so the provenance importer seam is
            # classified. Those importer gaps close on this directory rule,
            # the same disposition display_coverage / hydro_display already use.
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
            # #1850: the accepted-submit-identity binding-provenance and
            # claimant-exclusivity suites top-level-import
            # services/orchestrator/accepted_submit_identity.py and are
            # sub-second fixtures beside the gateway-reconcile lane they join.
            "tests/test_gateway_reconcile_binding_provenance.py",
            "tests/test_gateway_reconcile_claimant_exclusivity.py",
            "tests/test_forcing_submit_ambiguity.py",
            "tests/test_cli_cleanup_frontier.py",
            "tests/test_cli_publish_qdown.py",
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_file_orchestration_journal_read_cache.py",
            "tests/test_file_orchestration_migration.py",
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
            # #1943/#1944: the journal-root authority and job-id scope census
            # suites top-level-import `services.orchestrator` itself plus the
            # two new owner modules (journal_root_authority.py,
            # journal_scope_census.py) and operator_reserved_demotion.py /
            # scheduler_config/db_free.py — modules no stop rule owns, so the
            # directory rule is where those importer gaps close. Both are
            # orchestrator-journal suites; measured together, 50 tests in 1.50s.
            "tests/test_scheduler_journal_root_authority.py",
            "tests/test_scheduler_journal_scope_census.py",
            # #1955/#1953: the lane-adoption and full-tree budget suites
            # top-level-import `services.orchestrator` itself plus
            # journal_root_authority.py (the seam under test) and
            # chain_types.py / chain_runtime_utils.py (the terminal-status sets
            # the blocked sentinel must stay outside of) — modules no stop rule
            # owns, so the directory rule is where those importer gaps close.
            # Their other importer pairs (cli.py, file_orchestration_journal.py,
            # file_orchestration_migration.py) are stop-rule owned and ride
            # THEIR sites, per this rule's #1455 note above. Both are
            # orchestrator-journal suites; measured together, 87 tests in 2.07s.
            "tests/test_journal_root_lane_adoption.py",
            "tests/test_file_journal_full_tree_budget_contract.py",
            # #1555/#1768: the operator re-entry confirmation suite
            # top-level-imports `services.orchestrator` itself plus
            # journal_root_authority.py (the refusal message it pins) — modules
            # no stop rule owns, so the directory rule is where those two
            # importer gaps close. Its other importer pairs (cli.py,
            # scheduler.py, file_orchestration_journal.py) are stop-rule owned
            # and ride THEIR sites, per this rule's #1455 note above. This route
            # is also the only one that reaches the suite for a PR touching its
            # own subject module, services/orchestrator/operator_reentry_
            # confirmation.py, which the suite drives through the CLI rather
            # than importing. DB-free, 23 tests in 46.71s.
            "tests/test_operator_reentry_confirmation.py",
            # #2401: the newest-truth terminal-skip suite top-level-imports
            # `services.orchestrator` itself (via `scheduler`), a module no stop
            # rule owns, so the directory rule is where that importer gap closes.
            # Its `scheduler.py` pair is stop-rule owned and rides THAT site
            # (SCHEDULER_IMPORTER_TESTS), per this rule's #1455 note above; its
            # subject modules route it through their own per-file rows. DB-free,
            # 21 tests in ~10s.
            "tests/test_scheduler_terminal_recency.py",
            # #2397: the §8.7 identity-authority suite top-level-imports
            # `services.orchestrator` itself, scheduler_candidates.py (the
            # journal-predecessor quarantine it drives), scheduler_discovery.py
            # (the discovery-side §8.7 scoring it asserts through) and
            # scheduler_state_types.py (the decision type it builds) — none
            # stop-rule owned, so those four importer gaps close here, the same
            # disposition the re-entry confirmation suite above uses. Its journal
            # pair is stop-rule owned and rides FILE_ORCHESTRATION_JOURNAL_
            # IMPORTER_TESTS. DB-free, 7 tests in ~13s.
            "tests/test_quarantine_identity_authority.py",
            # #2396/#2407/#2408: the forecast-restart guard suite top-level-imports
            # only scheduler_candidates.py (the terminal-skip dispatch and repair
            # policy it drives) and scheduler_state_types.py — neither stop-rule
            # owned — so this directory rule is its route. Its scheduler-suite
            # helpers are imported function-locally. DB-free, 10 tests in ~3s.
            "tests/test_quarantine_forecast_restart_guards.py",
            # #2404: the retry-mint-floor suite drives the strict warm-start
            # budget (scheduler_candidates.py), the chain's cycle-stage mint
            # (chain_forecast_execution.py), the shared floor field
            # (retry_identity.py) and the cohort reconcile write
            # (file_orchestration_journal.py) end to end. All its imports are
            # function-local, so no importer derivation can reach it; this
            # directory rule is its route. DB-free, 6 tests in ~3s.
            "tests/test_retry_mint_floor.py",
            # #2393/#1845/#2416/#2394: the cross-stage state-residue suite drives
            # the forecast chain stage loop (chain_forecast_execution.py,
            # chain_forecast_cycle.py, chain_forecast_orchestrator_cycle.py), the
            # cohort restart read (chain_runtime_utils.py) and the run-manifest
            # restart write (scheduler_candidate_manifest.py) -- none stop-rule
            # owned and named after none of them, so this directory rule is its
            # route. DB-free, sub-second.
            "tests/test_chain_cross_stage_state.py",
            # #2387: the read-blocked sentinel's chain consumer end. The suite
            # drives `chain_forecast_execution._retry_job_for_stage_result`
            # through its real caller `_schedule_cycle_stage_retry`
            # (chain_forecast_orchestrator_cycle.py) to prove the classified
            # refusal precedes `handle_failed_job` -- neither module is
            # stop-rule owned and it is named after neither, so this directory
            # rule is its route for them. Its journal pair IS stop-rule owned
            # and rides FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS, per this
            # rule's #1455 note above. Every import is function-local, so no
            # importer derivation can reach it. DB-free, 17 tests in ~1s.
            "tests/test_file_journal_read_blocked_consumers.py",
            # #1186: the operator-action listing suite top-level-imports
            # `services.orchestrator` itself and scheduler_evidence_payload.py
            # (it writes every size-fallback fixture through the REAL
            # `bounded_evidence_payload`, so the listing's undecidability rule
            # rests on that writer's shape) — neither is stop-rule owned, so the
            # directory rule is where those two importer gaps close. Its third
            # importer pair, cli.py, is stop-rule owned and rides THAT site, per
            # this rule's #1455 note above. This route is also the only one that
            # reaches the suite for a PR touching its own subject module,
            # services/orchestrator/operator_action_listing.py, which the suite
            # drives through the CLI rather than importing. DB-free, 75 tests in
            # 0.34s.
            "tests/test_operator_action_listing.py",
            # #1905/#2402: the evidence-size decidability suite is the oracle for
            # the non-blocking summary tier and the bounded breaker-released
            # source-cycle projection. It imports NOTHING at module scope (every
            # `services.*`/`tests.*` import is function-local, so the frozen
            # `tests.test_production_scheduler` importer closure does not move),
            # which means no importer derivation can reach it: this directory
            # rule is its route for scheduler_evidence_payload.py,
            # scheduler_evidence.py, scheduler_discovery.py, scheduler_lease.py
            # and its reader subject operator_action_listing.py. Its sixth
            # module, scheduler_runtime.py, is stop-rule owned and rides THAT
            # site, per this rule's #1455 note above. DB-free, ~20 tests in ~20s
            # (one real breaker `run_once()` pass, module-scoped).
            "tests/test_scheduler_evidence_decidability.py",
            # #2405: the reservation-lease suite, on the same footing and for the
            # same reason -- named after none of its modules and importing
            # nothing at module scope. This directory rule is its route for
            # scheduler_lease.py (the heartbeat touch), scheduler_evidence.py
            # (the reservation `lease` block) and operator_action_listing.py (the
            # orphan rule); scheduler_runtime.py rides the stop rule above.
            # DB-free, 15 tests in ~1s.
            "tests/test_operator_action_reservation_lease.py",
            # #2442: the writer-bound status closure pin. It reads five writer
            # sources as TEXT (it must not import them), so it has no importer
            # derivation at all: this directory rule is its route for
            # scheduler_candidate_runtime.py, scheduler_evidence_proofs.py,
            # scheduler_candidate_execution_evidence.py,
            # scheduler_evidence_payload.py and the reader whose exit code the
            # whitelist decides, operator_action_listing.py. Its fifth writer,
            # scheduler_runtime.py, is stop-rule owned and rides THAT site, per
            # this rule's #1455 note above. DB-free, 4 tests in ~0.3s.
            "tests/test_operator_action_status_closure.py",
            # #1581 (+#1999): the hydro-status parity lock top-level-imports
            # eight modules of this package plus `services.orchestrator` itself,
            # so nine importer pairs land here. Seven close on this list
            # (__init__.py, chain_forecast_trigger.py, chain_repository.py,
            # scheduler_state_decision.py, scheduler_state_failure.py,
            # scheduler_state_manual_retry.py, scheduler_state_types.py);
            # chain.py and file_orchestration_journal.py are stop-rule owned
            # and are extended at THEIR sites, per this rule's #1455 note
            # above. 9 tests in 0.29s, DB-free, so a rule rather than a
            # rule-gap exclusion.
            "tests/test_hydro_status_set_parity.py",
            "tests/test_live_monitoring.py",
            "tests/test_monitoring_api.py",
            "tests/test_pipeline_persistence.py",
            # #1102: the publisher corpus is physically partitioned; the broad
            # orchestrator rule must select every collectible partition so a
            # scheduler_file_providers change never blinds targeted CI to moved
            # cases. Three of the seven top-level-import that module directly
            # (package-contexts, manifest-audit, refresh-lane); the other four
            # reach the same providers through `registry_script`, and the rule
            # carried the undivided monolith before the split.
            *PUBLISH_SCHEDULER_REGISTRY_TESTS,
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_replay_lineage.py",
            "tests/test_retention.py",
            # #1872: the retention corpus is physically partitioned; the
            # production owner rule must select every collectible partition so
            # a retention change never blinds targeted CI to moved cases.
            # #2238 added the fifth partition — the sixth retention suite,
            # counting #1872's independent frontier suite, which is not itself
            # a partition. It is the requirement oracle for retention.py's
            # copyback-mutex lane (one batch-lock acquisition per removed tree
            # under the shared copyback root, a pass-level wait budget,
            # `failed` rather than abort on contention), and it
            # top-level-imports `services.orchestrator` itself, so BOTH
            # directory members' importer gaps close here — cli.py's third one is
            # stop-rule owned and rides THAT site, per this rule's #1455 note
            # above. DB-free, local, 25 tests in 5.27s, so a rule rather than a
            # rule-gap exclusion.
            # #2259 partitioned it into these two halves and deleted the
            # monolith; this rule is where `services/orchestrator/retention.py`
            # and the extracted `retention_copyback_mutex.py` both reach them.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            # #2262: retention.py's typed lock-failure signal and the
            # compaction that must keep it (scheduler_evidence_payload.py rides
            # this rule too). DB-free, local, sub-second.
            "tests/test_retention_copyback_lock_signal.py",
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_frontier.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
            "tests/test_retry.py",
            "tests/test_retry_cancel_consistency.py",
            "tests/test_run_identity.py",
            "tests/test_run_tree_copyback.py",
            # #2237: run_tree_copyback.py's backup lifecycle oracle. Sub-second.
            "tests/test_run_tree_copyback_backup_lifecycle.py",
            "tests/test_scheduler_backfill_predecessor.py",
            # #1101 partitioned the 9614-line refresh monolith into fifteen
            # suites and deleted it; this directory rule was one of the eight
            # sites that named it by hand. The whole corpus rides here because
            # the rule's subject is every orchestrator module the runner calls.
            *SCHEDULER_REFRESH_TESTS,
            "tests/test_scheduler_generation.py",
            # #1735: the lineage resolver suite imports `services.orchestrator`
            # (hence `__init__.py`, which has no same-name suite of its own), so
            # the directory rule is where its importer gap closes. It IS an
            # orchestrator suite and it is sub-second, so it rides the directory
            # list rather than earning a narrow rule.
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_timing.py",
            "tests/test_source_cycle_raw_manifest.py",
            "tests/test_source_scoped_dispatch.py",
            "tests/test_state_clone.py",
            "tests/test_variant_activation_cutover.py",
            "tests/test_e2e_m3.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/**",
        (
            "tests/test_gateway.py",
            # #1809: the 14k-line gateway-reconcile monolith was physically
            # partitioned into flat responsibility modules; every collectible
            # partition replaces the deleted single target here, sorted.
            "tests/test_gateway_reconcile_comment_accounting.py",
            "tests/test_gateway_reconcile_comment_capability.py",
            "tests/test_gateway_reconcile_comment_sacct_bounds.py",
            "tests/test_gateway_reconcile_file_cohort_authority.py",
            "tests/test_gateway_reconcile_file_cohort_comment.py",
            "tests/test_gateway_reconcile_file_cohort_identity.py",
            "tests/test_gateway_reconcile_file_cohort_projection.py",
            "tests/test_gateway_reconcile_file_submit_barrier.py",
            "tests/test_gateway_reconcile_grace_guard.py",
            "tests/test_gateway_reconcile_idempotency_barrier.py",
            "tests/test_gateway_reconcile_identity_invariants.py",
            "tests/test_gateway_reconcile_identity_release.py",
            "tests/test_gateway_reconcile_inflight_identity.py",
            "tests/test_gateway_reconcile_inventory.py",
            "tests/test_gateway_reconcile_master_transitions.py",
            "tests/test_gateway_reconcile_reservation_lifecycle.py",
            "tests/test_gateway_reconcile_round10.py",
            "tests/test_gateway_reconcile_store_reset.py",
            "tests/test_gateway_reconcile_writer_launch.py",
            "tests/test_gateway_reconcile_writer_prepare.py",
            "tests/test_gateway_reconcile_writer_quiescence.py",
            "tests/test_gateway_reconcile_writer_receipts.py",
            "tests/test_gateway_reconcile_writer_rollforward.py",
            "tests/test_slurm_gateway_app.py",
            "tests/test_slurm_gateway_auth.py",
            "tests/test_slurm_gateway_auth_fullmount.py",
            "tests/test_slurm_gateway_auth_client.py",
            "tests/test_slurm_gateway_auth_deployment.py",
            # #1684 large-file guard repair: the auth suite was physically
            # partitioned; every partition replaces the single target.
            "tests/test_slurm_route_contract.py",
            "tests/test_slurm_route_security_contract.py",
            "tests/test_real_slurm_gateway.py",
            "tests/test_slurm_array_contract.py",
            "tests/test_job_array.py",
        ),
    ),
    # #1455: three narrow rules rather than four more entries on the directory
    # list above. Only app.py, config.py and gateway.py have these importer
    # gaps, and putting them on the directory rule would also change what a
    # `services/slurm_gateway/real_backend.py` PR selects — the exact output
    # issue #1455's own Verification command pins, and a surface PR #1486 just
    # closed. Narrow keeps that output byte-identical.
    PathTestRule(
        "services/slurm_gateway/app.py",
        (
            "tests/test_role_boundary_static.py",
            "tests/test_monitoring_api.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/config.py",
        (
            "tests/test_role_boundary_static.py",
            "tests/test_m24_gateway_proof.py",
        ),
    ),
    PathTestRule(
        "services/slurm_gateway/gateway.py",
        (
            "tests/test_monitoring_api.py",
            "tests/test_retry_cancel_consistency.py",
        ),
    ),
    PathTestRule(
        # One-hop extension of the guarded-module closure (#1455): three tracked
        # non-test modules import real_backend at file level
        # (services/orchestrator/reconcile.py,
        # services/production_closure/slurm_validation.py,
        # services/slurm_gateway/mock_backend.py), and their own non-gated
        # top-level importer suites were not selected by the
        # `services/slurm_gateway/**` rule above. This rule is deliberately
        # narrow — only a real_backend.py PR pays the extra ~20s; every other
        # slurm_gateway path keeps today's seven targets. The set is DERIVED
        # from the tracked tree by tests/test_select_ci_tests.py, never frozen
        # there, so a new one-hop importer suite reddens the guard.
        # The #1809 gateway-reconcile partitions are one-hop members too but
        # already ride the `services/slurm_gateway/**` rule; they are not
        # repeated here.
        # #1564: the split demote suites are one-hop members via
        # services/orchestrator/reconcile.py (see #1455 above) and are not
        # covered by either slurm_gateway rule, so they join this narrow rule.
        "services/slurm_gateway/real_backend.py",
        (
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            "tests/test_production_object_store_validation.py",
            "tests/test_production_ops_validation.py",
            "tests/test_production_readiness_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_forcing_submit_ambiguity.py",
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
        ),
    ),
    PathTestRule(
        # #1455: test_cli_publish_qdown.py top-level-imports the package and
        # publisher.py — the directory's only two importer gaps, both closed by
        # one 1.5s target, so the directory list is the right home.
        "services/tile_publisher/**",
        (
            "tests/test_tile_publisher.py",
            "tests/test_forcing_copyback_backfill.py",
            # #2236: forcing_copyback_backfill.py's `--apply` lock-scope oracle.
            "tests/test_forcing_copyback_backfill_lock_scope.py",
            "tests/test_static_serving.py",
            "tests/test_cli_publish_qdown.py",
            # #1442: publisher.py (B) and forcing_copyback_backfill.py (C) are
            # both in the zero-text-identity oracle's register, and both ride
            # this directory rule. The four suites above assert behaviour, not
            # the SQL identity shape, so the oracle joins the directory list
            # rather than getting two per-file entries for the same targets.
            "tests/test_river_ts_text_identity_cleanup.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
            "tests/test_river_ts_read_path_surrogate_keys_integration.py",
        ),
    ),
    PathTestRule(
        "services/tiles/mvt.py",
        (
            # #2074: the RETAINED BASE path of the partitioned API-contract
            # corpus, not the whole corpus. It is the only partition that imports
            # `apps.api.routes.hydro_display*` and asserts the tile routes'
            # published shape, so it is the one this module's derived closure
            # requires; the other two never reach a tile.
            "tests/test_api_contract.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_migrations.py",
            "tests/test_openapi_drift.py",
            # The #1341 surrogate-key / transitional-pushdown shape pins live
            # here; an mvt.py diff that quietly drops a predicate pairing must
            # not reach CI green without them.
            "tests/test_river_ts_read_path_surrogate_keys.py",
            # #1597: the eight below are DERIVED by the #1455 importer-closure
            # guard in tests/test_select_ci_tests.py
            # (test_guarded_module_rules_cover_their_non_gated_importer_closure,
            # now covering services.tiles.mvt), not hand-curated — the guard
            # owns the required set, so a future importer reds it here
            # instead of silently falling out of the PR lane. Of the eight
            # added entries, four are direct non-gated importers that assert
            # the postgis_tile_sql() output shape (hhe_mvt_binding,
            # hydro_display_mvt_scaling, the two node27_timeseries_compression
            # suites); the three direct_grid_display_cutover_* suites plus
            # test_openapi_31_contract.py are the one-hop additions via
            # apps/api/routes/hydro_display.py and
            # apps/api/openapi_patching.py respectively. The two
            # `integration`-marked importers stay out per the #1447 ruling:
            # they auto-skip without NHMS_RUN_INTEGRATION (tests/conftest.py),
            # so requiring them buys constant skips and zero assertions.
            "tests/test_direct_grid_display_cutover_flip.py",
            "tests/test_direct_grid_display_cutover_history.py",
            "tests/test_direct_grid_display_cutover_model_resolution.py",
            "tests/test_hhe_mvt_binding.py",
            # #2074: the hydro-display MVT suite is now nine partitions and ALL
            # of them are in this closure (direct importers, plus
            # `..._catalog_cache.py` one hop through
            # apps/api/routes/hydro_display.py). Listing the tuple rather than
            # the nine literals keeps this rule and the facade rule below from
            # drifting apart.
            *HYDRO_DISPLAY_MVT_SCALING_TESTS,
            # #2121-A: real QueuePool admission regression imports the shared
            # TileInput/TileResponse/cache-key contract at module scope.
            "tests/test_display_mvt_cold_admission.py",
            # #2032: guard-derived, not hand-curated — both new suites import
            # services.tiles.mvt at file level. The lock suite drives
            # tile_generation_lock / _open_live_lock_file directly; the
            # retention suite pins its three path regexes against
            # _file_cache_path / _file_cache_lock_path / _write_file_cache, so
            # a layout change here must red there rather than after merge.
            # Neither suite imports apps.api.routes.hydro_display, so that
            # rule is deliberately left alone.
            "tests/test_mvt_tile_generation_lock.py",
            "tests/test_node27_mvt_cache_retention.py",
            # #2550: guard-derived — the basemap proxy suite imports
            # apps.api.routes.basemap, which takes MVT_FILE_CACHE_DIR_ENV from
            # this module: both caches share one root, so renaming the env here
            # must red the proxy's cache-path assertions in the PR lane.
            "tests/test_basemap_proxy.py",
            # #2013: same guard-derived provenance — the prewarm suite imports
            # NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS from services.tiles.mvt
            # at file level (issue #2013's planned-envelope assertion steps the
            # published valid-time grid with it, so a stride change reds the
            # envelope count), which makes it a DIRECT non-gated importer here.
            # Its own entry, not one of the "eight below" above: the list is
            # sorted, so it is INTERPOSED among them by position only and the
            # #1597 census stays true.
            "tests/test_node27_mvt_prewarm.py",
            # #2156 (D-2): guard-derived, not hand-curated — the run/river-
            # network geometry-identity suite imports TileInput, cache_key and
            # display_ready_run from services.tiles.mvt at file level, so it is
            # a DIRECT non-gated importer here. Unlike the #2032 pair above it
            # ALSO imports apps.api.routes.hydro_display at file level (the
            # _river_network_source_version / _run_row / _run_source_version
            # SQL and the route cache keys), so it sits on that rule too. Its
            # own entry, so the #1597 "eight below" census stays true.
            "tests/test_mvt_run_and_river_network_geometry_identity.py",
            # #2017: the coordinate-budget harness imports
            # postgis_tile_sql / collection_coordinate_limit / MVT_MAX_COORDINATES
            # from services.tiles.mvt at module level, so its suite is a DIRECT
            # non-gated importer of this guarded module too.
            "tests/test_node27_river_tile_coordinate_evidence.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_openapi_31_contract.py",
            # #1684 large-file guard repair: the 3.1-contract security half is
            # a one-hop importer via tests/test_openapi_31_contract.py, so it
            # joins the derived closure here (and in the hydro_display rule).
            "tests/test_slurm_gateway_openapi_security.py",
            # #1714: same guard-derived provenance as the #1597 batch above,
            # not hand-curated — a one-hop importer reached through
            # apps/api/routes/hydro_display.py. Kept as its own entry so the
            # "eight below" census stays true.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #1704: same guard-derived provenance — the API error-logging
            # suite imports apps/api/routes/hydro_display.py at file level,
            # which imports services.tiles.mvt, so it is a one-hop importer
            # here (and a DIRECT importer on the hydro_display rule below).
            "tests/test_api_errors_logging.py",
            # #2010: same guard-derived provenance — the precip suite imports
            # services.tiles.mvt (layer_metadata / MVT_FILE_CACHE_DIR_ENV) at
            # file level, so it is a DIRECT non-gated importer here.
            "tests/test_precip_overlay.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    # The other two #1341 switched surfaces. Both are covered by broad rules
    # (packages/common/** and the API route rules) that do not include the
    # read-path shape pins, so without these entries a diff that drops a
    # pushdown pairing or reintroduces a text fact predicate in either file
    # reaches CI green unchallenged.
    PathTestRule(
        # #1672: hydro_display.py joins GUARDED_MODULE_CLOSURES and its rule is
        # extended to the current mechanically derived direct UNION one-hop
        # non-gated importer closure (tests/test_select_ci_tests.py derives the
        # required set from the tracked tree, never frozen). The three cutover
        # suites, display status-only, HHE/MVT, node-27 compression and
        # attribution suites are direct importers; the 3.1-contract and
        # runtime-mode suites are the one-hop contributions via
        # apps/api/openapi_patching.py and apps/api/route_registry.py. The two
        # `integration`-marked importers (test_display_coverage_residual_debt_
        # integration.py, test_mvt_national_identity_probe_integration.py) stay
        # out per the #1447 ruling — they auto-skip in the PR lane. The #1341
        # read-path shape pin rides along as an exact at-site entry.
        # #2026: the pattern is a glob over the whole `hydro_display*` family.
        # The facade was split into hydro_display_{constants,models,instants,
        # catalog,identity,postgis}.py and the SQL, the budget signals and the
        # layer catalog now live in the owner modules, so an exact-path entry
        # would leave an owner-module-only diff selecting nothing here. One
        # entry, not seven: the selector's duplicate-pattern guard forbids a
        # second PATH_TEST_RULES entry for an already-owned module, and every
        # suite below imports the facade, which imports all six owners.
        "apps/api/routes/hydro_display*.py",
        (
            # #2074: the RETAINED BASE path. Guard-derived, like every other
            # entry here — it is the API-contract corpus's only direct module-
            # scope importer of this family (it patches
            # `hydro_display_catalog` and rebinds two facade names), which is
            # also why the `apps/api/routes/hydro_display_catalog.py` row of
            # GUARDED_MODULE_CLOSURES still anchors on this exact path.
            "tests/test_api_contract.py",
            # #1704: guard-derived, not hand-curated — the API error-logging
            # suite imports get_hydro_display_session from this module at file
            # level, so it is a direct non-gated importer.
            "tests/test_api_errors_logging.py",
            "tests/test_direct_grid_display_cutover_flip.py",
            "tests/test_direct_grid_display_cutover_history.py",
            "tests/test_direct_grid_display_cutover_model_resolution.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_hhe_mvt_binding.py",
            # #2074: seven of the nine hydro-display MVT partitions. The
            # `..._discovery.py` and `..._national_sql.py` two import no
            # apps.api.routes module, so they are outside this family's derived
            # closure and stay routed by the services/tiles/mvt.py rule above.
            *HYDRO_DISPLAY_MVT_SCALING_FACADE_TESTS,
            # #2156 (D-2): guard-derived — the run/river-network geometry-
            # identity suite imports this module at file level and runs the real
            # SQL of _river_network_source_version / _run_row /
            # _run_source_version plus the route cache keys, so it is a DIRECT
            # non-gated importer (and on the services/tiles/mvt.py rule too).
            "tests/test_mvt_run_and_river_network_geometry_identity.py",
            "tests/test_display_mvt_cold_admission.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #2017: the checked-in 2.4/2.5 coordinate-budget harness imports
            # apps.api.routes.hydro_display._postgis_tile_params at module level
            # (the whole point of the harness is that the binds match production
            # by construction), and its suite imports the harness, so the suite
            # is a DIRECT non-gated importer here. Without this entry a
            # hydro_display-only diff could change the bind dict and leave the
            # harness asserting the old shape, green in the PR lane.
            "tests/test_node27_river_tile_coordinate_evidence.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_openapi_31_contract.py",
            # #1684 large-file guard repair: the 3.1-contract security half is
            # a one-hop importer via tests/test_openapi_31_contract.py.
            "tests/test_slurm_gateway_openapi_security.py",
            "tests/test_openapi_drift.py",
            # #2010: guard-derived — the precip suite imports
            # apps/api/routes/hydro_display.py at file level (the shared
            # `Rfc3339Instant` / seconds-precision gate and the DB-dependency
            # assertion), so it is a DIRECT non-gated importer.
            "tests/test_precip_overlay.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
            "tests/test_runtime_mode.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #2010: the precipitation raster service's own tree. `services/**` has
        # no broad rule, so before this entry a mirror/window/render/cache diff
        # selected only the same-name derivation (which finds nothing: there is
        # no tests/test_mirror.py) and reached the PR lane with zero assertions.
        # #2122: PR #2117 made scripts/node27_mvt_prewarm.py:57 import
        # services.precip.mirror.horizon_valid_times at module level, and
        # tests/test_node27_mvt_prewarm.py:17 imports that script at module
        # level, so the prewarm suite is a one-hop importer suite of this tree.
        # It is the sole holder of the clamped-first-valid-time / PNG-horizon
        # contract, asserted by
        # test_a_clamped_first_valid_time_is_warmed_from_that_entry_not_from_the_cycle
        # (referenced by case name, never by line range, per openspec/changes/
        # display-v2-national-timeline-precip-overlay/tasks.md:548), and
        # PRECIP_STEP_HOURS feeds that grid — before this target the PR lane
        # could not reach that oracle at all. It is NOT the only suite a step
        # flip reds: tests/test_precip_overlay.py, already selected by this
        # rule, discriminates on the step too (measured 31 failed at step 6).
        # The union is spelled in place rather than appended to
        # PRECIP_SURFACE_TESTS so the shared tuple (and with it the
        # apps/api/routes/precip.py rule below) does not inherit the prewarm
        # suite: a route-only diff must not pay for it.
        # #2191: scripts/node27_raw_retention.py imports
        # services.precip.constants.FILE_CACHE_DIR_ENV at module level and
        # tests/test_node27_raw_retention.py imports that script at module level,
        # so the raw-retention suite is a second one-hop importer suite of this
        # tree. It is not redundant with tests/test_precip_overlay.py: that suite
        # catches a first-order rename of the env name, but a CONSISTENT rename
        # that also updates its pin leaves the raw-retention suite's bare literal
        # stale — green in the PR lane, red only on master. Spelled in place for
        # the same shared-tuple reason as the prewarm suite.
        "services/precip/**",
        (*PRECIP_SURFACE_TESTS, "tests/test_node27_mvt_prewarm.py", "tests/test_node27_raw_retention.py"),
    ),
    PathTestRule(
        # #2010: the two public routes. `apps/api/**` below buys the three broad
        # API suites, none of which exercises a precip route; the exact entry
        # adds the behavioural oracle plus the public-contract suites. Exact path
        # beside the broad rule is the same shape the hydro_display.py rule uses.
        "apps/api/routes/precip.py",
        PRECIP_SURFACE_TESTS,
    ),
    PathTestRule(
        # #2550: the Tianditu basemap proxy route. Same shape as the precip
        # route rule: `apps/api/**` buys only the broad API suites, none of
        # which calls this route, so the exact entry adds its behavioural oracle
        # plus the public-contract suites its OpenAPI entry is pinned by.
        "apps/api/routes/basemap.py",
        (
            "tests/test_basemap_proxy.py",
            "tests/test_openapi_drift.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_api_contract.py",
        ),
    ),
    PathTestRule(
        "services/production_closure/readonly_db_validation.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        "services/production_closure/readonly_db_route_smoke.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        "services/production_closure/__init__.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
    ),
    PathTestRule(
        "services/production_closure/c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "services/production_closure/c4_production_acceptance_io.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_c4_production_acceptance.py",
        C4_PRODUCTION_ACCEPTANCE_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_types.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_query.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_http.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_plan.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_measure.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_workload_io.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_pgdata_workload.py",
        NODE27_PGDATA_WORKLOAD_TESTS,
        stop_on_match=True,
    ),
    PathTestRule(
        # #1455: the directory's 25 importer gaps collapse onto four suites, all
        # of which are production-closure suites that other rules happened to own
        # (real_backend.py, forcing_producer, the readonly-db script). The
        # `two_node_e2e_*` lane family in particular ALL points at
        # tests/test_two_node_e2e_evidence.py — one target closes thirteen
        # modules' gaps. Measured in the PR lane, not assumed from the name:
        # test_two_node_e2e_evidence.py runs 844 real assertions in ~2.5 min.
        "services/production_closure/**",
        (
            "tests/test_production_readiness_validation.py",
            "tests/test_production_ops_validation.py",
            # #1684 large-file guard repair: the ops-validation suite was
            # physically partitioned into auth/dependency/hardening modules;
            # every collectible partition replaces the single target so a
            # production_closure change never blinds targeted CI to moved
            # cases.
            "tests/test_slurm_gateway_ops_auth_evidence.py",
            "tests/test_slurm_gateway_ops_dependency_closure.py",
            "tests/test_slurm_gateway_ops_dependency_hardening.py",
            "tests/test_production_object_store_validation.py",
            # #1911: every historical-facade and split-owner change must run the
            # direct compatibility contract oracle, not merely the production
            # behavior suite that happens to exercise portions of the facade.
            "tests/test_object_store_validation_facade_contract.py",
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            *READONLY_DB_VALIDATION_TESTS,
            "tests/test_two_node_e2e_evidence.py",
        ),
    ),
    PathTestRule(
        "packages/common/object_store.py",
        (
            "tests/test_object_store_roots.py",
            "tests/test_storage.py",
        ),
    ),
    PathTestRule(
        # #2260: the retention copyback mutex's lock semantics live here, yet no
        # rule named this module, so a guard-only diff reached only its same-name
        # suite plus the packages/common/** supplemental routes and never the
        # mutex suite. Path-exact with neither flag: no stop rule matches this
        # path, so the existing selections accumulate unchanged beside it.
        "packages/common/copyback_guard.py",
        (
            # #2259: both halves of the split copyback-mutex partition.
            *RETENTION_COPYBACK_MUTEX_TESTS,
            # #2252/#2262 (harden-copyback-mutex-residuals): the guard gained the
            # `posix` primitive, the non-finite timeout refusal, the budget
            # constant and the lock-failure classifier. Its own primitive suite
            # and every lane suite whose contract reads those names ride here, so
            # a guard-only diff reaches each lane's requirement oracle.
            "tests/test_copyback_guard_primitive.py",
            "tests/test_forcing_copyback_backfill_lock_scope.py",
            "tests/test_node27_raw_retention_copyback_mutex.py",
            "tests/test_retention_copyback_lock_signal.py",
            "tests/test_run_tree_copyback_backup_lifecycle.py",
        ),
    ),
    PathTestRule(
        # #2259: the extracted copyback-mutex owner. Its same-name derivation
        # points at the deleted monolith, so without this row its only route is
        # the broad `services/orchestrator/**` rule -- which any later stop rule
        # on this path would shadow without a sound. Path-exact with neither
        # flag, so that directory selection still accumulates beside it.
        RETENTION_COPYBACK_MUTEX_OWNER_PATH,
        RETENTION_COPYBACK_MUTEX_OWNER_TESTS,
    ),
    PathTestRule(
        # #2252: the node-27 canonical lane's copyback-mutex oracle. Not a
        # same-name suite, so derivation never reaches it. Path-exact with
        # neither flag, so the same-name suite still accumulates beside it.
        "scripts/node27_raw_retention.py",
        ("tests/test_node27_raw_retention_copyback_mutex.py",),
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the shared per-store renderer. It owns the
        # text-identity vocabulary and the table-scoped attribution every oracle
        # in the group now imports, so a diff to it can blunt all of them at once
        # — the #1442 failure mode one level down. It is also a registered source
        # of tests/test_river_ts_text_identity_cleanup.py (census 2, the two
        # table-name constants), which that oracle's wiring meta-test requires to
        # be routed here.
        "packages/common/river_ts_render.py",
        SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # I11 #1990 forcing_ts_render: the forcing per-store renderer, and the
        # same gap as the river rule above one layer up from the register.
        # No pattern matched this path before — `packages/common/**` is not a
        # broad rule, it only re-adds the core-smoke baseline (#1744 path B) —
        # so a renderer diff selected its same-name suite and nothing else.
        # It is a registered source of the forcing discovery-set census
        # (FORCING_TABLE_CENSUS pins it at 2: `FORCING_TABLE` and
        # `FORCING_TABLE_LEGACY`, the same two-constant shape river_ts_render.py
        # carries), so a third table-name mention here, or a mention deleted,
        # reds a suite this path did not run — post-merge, exactly the failure
        # river's wiring meta-test exists to prevent. The census also imports
        # FORCING_STORES and render_forcing_ts_sql at module scope and renders
        # every registered template through them.
        # The same-name suite is named explicitly rather than left to same-name
        # derivation, as the river rule does through SQL_SHAPE_ORACLE_TESTS: the
        # rule should read as the renderer's full unit closure, not as the half
        # that derivation misses. Both targets are non-gated.
        "packages/common/forcing_ts_render.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    # I11 #1990 task 7.2 — the forcing census's remaining unrouted files.
    #
    # Each of these carries mentions the census pins a NUMBER for, and none of
    # them reached a rule that runs it: `packages/common/**`,
    # `workers/**` and `services/**` are not broad backend rules (they only
    # re-add the core-smoke baseline, #1744 path B), and four of the seven have no
    # same-name suite to fall back to either. So a forcing table mention added or
    # removed in any of them was red on the post-merge master run and nowhere
    # else — exactly the failure river's per-site rider exists to prevent.
    #
    # Each rule pairs the rider with the file's own owning suites where those
    # were also unrouted, so the rule reads as the path's real closure rather
    # than as a census appendage.
    PathTestRule(
        "packages/common/forcing_domain_handoff.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_domain_handoff_contract.py",
            "tests/test_forcing_domain_handoff_apply.py",
        ),
    ),
    PathTestRule(
        "packages/common/forcing_domain_handoff_apply.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_domain_handoff_contract.py",
            # #1991 task 7.3: this module is one of the two narrow-only writers,
            # and the refusal-BEFORE-any-DELETE ordering (must-preserve M3, the
            # one data-loss property in that task's surface) is asserted there.
            # Its own same-name suite covers the apply report; the ordering does
            # not live in it.
            "tests/test_timescale_write_guard_wired.py",
        ),
    ),
    PathTestRule(
        "workers/forcing_producer/store.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_producer.py",
        ),
    ),
    PathTestRule(
        # #1991 task 7.3: the per-version store routing both writers, all nine
        # readers, the seed and the autopipeline's non-failing tick outcome now
        # depend on. No pattern matched it before -- `packages/common/**` is not
        # a broad rule (#1744 path B) and it has no same-name suite -- so a diff
        # to the store names, the refusal code or the narrow INSERT template
        # would have been red only on the post-merge master run.
        "packages/common/forcing_store_routing.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_timescale_write_guard_wired.py",
            "tests/test_forcing_domain_handoff_apply.py",
            "tests/test_forcing_producer.py",
            "tests/test_node27_autopipeline_handoff.py",
            "tests/test_seed.py",
        ),
    ),
    PathTestRule(
        "workers/forcing_producer/file_store.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forcing_producer.py",
        ),
    ),
    PathTestRule(
        # A wired reader (the QHH bootstrap forcing-state count) with no rule of
        # its own: its templates live here, so the byte-identity and M6 pins have
        # to run on a diff to it.
        "workers/model_registry/qhh_production_bootstrap.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_qhh_scripts_static.py",
        ),
    ),
    PathTestRule(
        # A wired reader (the forcing-inputs listing) AND a #1728
        # connection-attribution store. Both sets live on this one rule rather
        # than on two rows with the same pattern — see
        # CONNECTION_ATTRIBUTION_STORE_PATHS, which this path was removed from.
        "packages/common/best_available.py",
        (
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_best_available.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_live_evidence.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        "services/production_closure/two_node_e2e_readonly_db_lane.py",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # I1 #1980 river_ts_render: the captured golden of every registered read
        # template — the ONE artefact that can void #1980's whole equivalence
        # argument. It is data, not Python, so the `tests/**.py` branch above
        # never sees it and it reached no rule at all: a PR that regenerated the
        # golden after editing a template — certifying the edit against itself —
        # selected zero backend tests and merged on a collect-only smoke
        # (review #1996, C10). Globbed on the capture SHA so a re-capture at a
        # new base is routed the same way.
        "tests/fixtures/river_ts_templates_*.json",
        SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # I11 #1990 task 7.2's forcing counterpart of the rule above, for the
        # identical reason (review #1996, C10). This is the PRE-WIRING snapshot
        # the byte-identity pins compare against — captured by executing the nine
        # readers in a worktree at the cut-(a) merge, which is the whole reason
        # those pins are evidence rather than a golden certifying its own source.
        # It is data, not Python, so the `tests/**.py` branch never sees it and it
        # reached no rule at all: a PR that re-captured it after editing a
        # template would select zero backend tests. Globbed on the capture SHA so
        # a re-capture at a new base routes the same way.
        "tests/fixtures/forcing_read_path_pre_wiring_*.json",
        FORCING_SQL_SHAPE_ORACLE_TESTS,
    ),
    PathTestRule(
        # #2183: the #1913 registry-partition additions ledger is a hand-edited
        # guard input read only by the meta-suite; as data it reaches no other
        # rule, so a ledger-only PR would select nothing and fail on master.
        "tests/fixtures/basins_registry_partition_additions.json",
        ("tests/test_select_ci_tests.py",),
    ),
    PathTestRule(
        "packages/common/forecast_store.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_forecast_api.py",
            "tests/test_forecast_store_routing.py",
            "tests/test_node27_pgdata_workload.py",
            "tests/test_node27_pgdata_workload_plan.py",
            "tests/test_node27_pgdata_workload_io.py",
            "tests/test_list_search_contract.py",
            "tests/test_migrations.py",
            "tests/test_model_registry_list_basins.py",
            # The benchmark captures this owner's named bindings; live evidence
            # independently verifies the serialized name/value pairs.
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_qhh_latest_fallback_pushdown.py",
            # #1442: this file carries nine of the zero-text-identity oracle's
            # registered statements. None of the suites above assert the
            # pushdown-aid pairing or the statement census, so without this
            # entry a diff that reintroduces a text fact predicate here reaches
            # CI green unchallenged (same at-site reasoning as #1341's
            # mvt.py / display_coverage.py entries).
            "tests/test_river_ts_text_identity_cleanup.py",
            # #1728: this module carries the connection-attribution injection
            # seam (`application_name=` through `from_env` to the connect call)
            # for BOTH nhms-api-forecast and nhms-api-data-sources. MERGED into
            # this rule rather than added as a second exact entry — a duplicate
            # pattern splits the module's ownership across two rules
            # (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
            *CONNECTION_ATTRIBUTION_TESTS,
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # No rule covered this module before, and no broad `packages/common/**`
        # rule exists, so a display-coverage-only PR fell through to the
        # core-smoke fallback — which imports none of it. The three targets are
        # its non-gated top-level importer suites. The fourth importer,
        # tests/test_display_coverage_residual_debt_integration.py, is
        # deliberately excluded: it is `integration`-marked and therefore skipped
        # in the PR lane, so listing it buys constant skips and zero assertions.
        # tests/test_select_ci_tests.py derives this closure from the tracked
        # tree and reddens if a new non-gated importer suite appears here.
        #
        # #1443 merge consolidation: this module also carries the #1341
        # read-path shape pins. That surface's broad rules (packages/common/**
        # and the API route rules) do not include them, so a diff dropping a
        # pushdown pairing or reintroducing a text fact predicate here would
        # reach CI green unchallenged. The pins joined this rule instead of
        # getting a second entry for the same pattern — a duplicate pattern
        # splits the module's ownership across two rules with nothing saying so
        # (test_path_rule_duplicate_patterns_are_allowlisted_decisions).
        "packages/common/display_coverage.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_display_coverage_refresh.py",
            "tests/test_display_coverage_parallel.py",
            "tests/test_forecast_api.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
            # #1714: this module is a registered connect-owning surface in the
            # attribution guard's DELEGATED_CONNECT_CLOSURE — it opens the
            # connection a registered component delegates to, which is exactly
            # the shape that shipped unattributed once. The four above assert
            # coverage behaviour and read-path shape, not the component-level
            # fallback_application_name identity.
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            # #1446: the CLI suite is a one-hop importer via
            # scripts/node27_refresh_coverage.py. It owns the operator-facing
            # half of the overwrite guard (exit 3 + the structured refusal
            # line, `refused` in the --all report) — behaviour none of the
            # suites above assert.
            "tests/test_node27_refresh_coverage_cli.py",
            # I1 #1980 river_ts_render: this file's river read templates are
            # registered in tests/river_ts_template_registry.py and rendered per
            # timeseries store by the whole SQL-shape oracle group, so a layout
            # or predicate edit here must run all of it — the golden equivalence
            # oracle included, which is what proves the edit was behaviour-free.
            # (One block per site rather than one new rule per path: the
            # selector's own duplicate-pattern guard forbids a second
            # PATH_TEST_RULES entry for an already-owned module.)
            *SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # #1446: the refusal exit code and stderr line are this script's own
        # contract (the autopipeline branches on the rc), and no other rule
        # covers this file — a CLI-only diff fell through to the core-smoke
        # fallback, which imports none of it.
        "scripts/node27_refresh_coverage.py",
        (
            "tests/test_node27_refresh_coverage_cli.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
        ),
    ),
    PathTestRule(
        "packages/common/state_manager.py",
        (
            "tests/test_state_manager.py",
            "tests/test_state_qc.py",
            # #1735: the clone-lineage read path (`get_earliest_clone_row_for_
            # model_source`, `clone_lineage_signal`, `_clone_entries_for_model_
            # source`) lives in this module, but the two suites routed above
            # assert NOTHING about it — every assertion, the DB-plane SQL shape
            # included, sits in the scheduler suites. Without these two, the
            # negative pin written to guard that SQL (`test_earliest_clone_row_
            # query_is_ascending_and_clone_scoped`, which asserts `usable_flag`
            # never enters the statement) was not in this module's lane:
            # injecting `AND usable_flag = true` into the query left the routed
            # lane green. 24 tests in 0.09s and 52 in 0.82s — under a second.
            "tests/test_scheduler_lineage.py",
            "tests/test_scheduler_backfill.py",
            # NODE IDS, not the file: `test_production_scheduler.py` names none
            # of these symbols directly — it drives the read path through a
            # duck-typed fake in its cohort suppression, which is the seam a
            # signature or ordering change breaks. The whole file is 1870 tests
            # in 186s — far past what this lane can carry. These three are the
            # only tests in it that exercise that seam, 0.34s together. Node ids
            # are first-class targets here: `_test_target_exists` splits on
            # `::`, ci.yml passes the selection straight to `pytest -q`, and the
            # meta-guard re-checks every pinned node id still names a live
            # `def`, so a rename reds instead of silently dropping the route.
            "tests/test_production_scheduler.py::test_build_candidates_suppresses_a_model_before_its_lineage_cutover",
            "tests/test_production_scheduler.py::test_build_candidates_admits_a_model_at_its_lineage_cutover",
            "tests/test_production_scheduler.py::test_build_candidates_without_lineage_is_unchanged",
            # #1728: this module carries the connection-attribution injection
            # seam for nhms-api-state-snapshots (StateManager.from_env ->
            # PsycopgStateSnapshotRepository -> connect). MERGED here for the
            # same duplicate-pattern reason as forecast_store.py above.
            *CONNECTION_ATTRIBUTION_TESTS,
        ),
    ),
    PathTestRule(
        "packages/common/state_cli.py",
        (
            "tests/test_state_manager.py",
            "tests/test_state_qc.py",
        ),
    ),
    PathTestRule(
        "packages/common/redaction.py",
        ("tests/test_redaction.py",),
    ),
    PathTestRule(
        "packages/common/node27_container_contract.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_decompression_replay.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_discovery.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_external_contract_snapshot.json",
        (
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_pgdata_container.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
    ),
    PathTestRule(
        # #1644: the committed OpenAPI snapshot is the drift oracle's subject, so
        # an OpenAPI-only PR must run the drift + API-contract + 3.1-contract
        # suites. Exact set, no core-smoke fallback, no other suites.
        "openapi/**",
        OPENAPI_CONTRACT_TESTS,
    ),
    PathTestRule(
        # #1644: the runtime schema owner injects every nullable node and the
        # security metadata, so a patch-owner PR must reach the drift + 3.1
        # contract suites in addition to the broad API consumers it already
        # carried.
        # #2211 quantified this module's 13 `_patch_*_openapi` implementations
        # against the six targets above. Only ONE capability suite was both
        # unreached and able to observe the module:
        # tests/test_hydro_display_mvt_scaling.py, which asserts the PATCHED
        # runtime document directly (`main.create_app().openapi()` in
        # `test_runtime_openapi_documents_the_national_identity_tile_route` and
        # `test_runtime_openapi_documents_both_424_codes_on_the_canonical_
        # national_route_only`). Measured: no-op'ing `_patch_mvt_tile_openapi`
        # reds exactly those two, and the suite was not selected before.
        # Cost +22.6s.
        # #2074: that suite is now nine partitions, and the two #2211 pins landed
        # in exactly two of them —
        # `test_runtime_openapi_documents_the_national_identity_tile_route` in
        # `..._national_routes.py` and
        # `test_runtime_openapi_documents_both_424_codes_on_the_canonical_national_route_only`
        # in `..._coverage_order.py`. Only those two are listed, for the same
        # measured reason the other #2211 candidates were rejected: the remaining
        # seven partitions never read the document this module produces, so they
        # cannot red on a patch change however much tile surface they cover.
        #
        # The rest of the #2211 candidates were REJECTED on measured evidence,
        # not on cost. This module's only observable output is the OpenAPI
        # document (its sole production importer is apps/api/main.py's schema
        # hook), so a suite that never reads that document cannot be an oracle
        # for it however public its routes are: no-op mutants of
        # `_patch_precip_openapi`, `_patch_runtime_openapi` and the combined
        # layer-metadata / forecast-series / station-series trio red only
        # tests/test_openapi_drift.py and tests/test_openapi_31_contract.py —
        # tests/test_precip_overlay.py (reads the STATIC openapi/nhms.v1.yaml),
        # tests/test_forecast_api.py and
        # tests/test_forecast_api_met_station_series.py (never read the schema)
        # and tests/test_runtime_mode.py (reads /openapi.json but asserts route
        # PRESENCE, which registration decides) all stay green. Routing them
        # would buy assertions that run but cannot observe the change — a cousin
        # of the #1447 ruling on constant skips. The runtime-vs-static
        # comparison the drift target already carries is the residual oracle
        # for the other twelve implementations.
        #
        # Per-capability literals, never a directory pattern: every rule here is
        # an explicit tuple so the exact-set anchor in
        # tests/test_select_ci_tests.py can pin it.
        #
        # #2074: the pattern is a glob over the whole `openapi_patching*` family.
        # The facade was split into openapi_patching_{nullable,security,
        # envelopes,parameters,ops_schemas,display_schemas,pipeline}.py, and the
        # component schemas, the parameter builders, the nullable finalizer and
        # the whole pipeline patch family now live in the owner modules, so an
        # exact-path entry would leave an owner-module-only diff selecting
        # nothing here. One entry, not eight: the selector's duplicate-pattern
        # guard forbids a second PATH_TEST_RULES entry for an already-owned
        # module, and every suite below reaches the owners through the facade,
        # which imports all seven. The glob is deliberately `openapi_patching*`
        # and not `openapi_*`: apps/api/openapi_restored_schemas.py is a
        # separate module this rule does not own.
        "apps/api/openapi_patching*.py",
        (
            "tests/test_api.py",
            # #2074: the RETAINED BASE path. Per the #2211 criterion ("reads the
            # document this module produces") it is the only API-contract
            # partition that qualifies: all four `app.openapi()` call sites of
            # the former monolith are in it. The two route-behaviour partitions
            # read the committed file only, so naming them here is exactly the
            # padding this rule's requirement forbids. They still reach this
            # module through the broad `apps/api/**` rule.
            "tests/test_api_contract.py",
            "tests/test_hydro_display_mvt_scaling_coverage_order.py",
            "tests/test_hydro_display_mvt_scaling_national_routes.py",
            "tests/test_monitoring_api.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_openapi_drift.py",
            "tests/test_slurm_gateway_openapi_security.py",
            "tests/test_pipeline_ops_identity_envelope.py",
        ),
    ),
    PathTestRule(
        # The broad fallback for every `apps/api/**` path without a narrower
        # owner. #2074: the API-contract corpus is named here in FULL. This rule
        # is the only thing that runs the resource-route contracts (model
        # lifecycle / active / list / detail, basin version redaction, the
        # river-segment GeoJSON budget, met stations and data sources) on a diff
        # to the routes that serve them, so naming only the retained base path
        # would drop 27 of the corpus's 38 cases out of the PR lane — the #1684
        # "every collectible partition replaces the single target" failure this
        # rule cannot afford, since none of those routes has a second oracle here.
        "apps/api/**",
        (
            "tests/test_api.py",
            *API_CONTRACT_TESTS,
            "tests/test_monitoring_api.py",
        ),
    ),
    PathTestRule(
        # #2079. The catalog cache's own suite is named after the CAPABILITY, not
        # after the module, so same-name derivation (`tests/test_display_cache.py`)
        # cannot reach it, and the `apps/api/**` rule above only buys the three
        # generic API suites -- none of which exercise `_force_refresh`.
        "apps/api/display_cache.py",
        ("tests/test_display_catalog_cache.py",),
    ),
    PathTestRule(
        # #2078. `/api/v1/runs` response body (envelope + `total`/`total_count`
        # duplication) is pinned only by this suite; the `apps/api/**` rule above
        # buys the three generic API suites, none of which call `list_runs`, so a
        # diff that changed the runs page shape reached CI green.
        "apps/api/routes/forecast.py",
        (
            "tests/test_forecast_api.py",
            # #1728's connection-attribution guards, MERGED here rather than left
            # in CONNECTION_ATTRIBUTION_ROUTE_PATHS: this module now has an exact
            # rule, and a duplicate pattern splits its ownership across two.
            *CONNECTION_ATTRIBUTION_TESTS,
        ),
    ),
    PathTestRule(
        "db/**",
        ("tests/test_migrations.py",),
    ),
    PathTestRule(
        # #1442 (group E). The seed's two river verification counts are
        # registered statements of the zero-text-identity oracle. `db/**` above
        # only buys tests/test_migrations.py, which never reads this module, so
        # a seed-only diff that reintroduced a text identity predicate reached
        # CI green. Narrow pattern on purpose: no other file under db/seeds/ is
        # in the register.
        "db/seeds/seed_demo.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_river_ts_text_identity_cleanup.py",
            "tests/test_seed.py",
            "tests/test_river_ts_dual_write_integration.py",
        ),
    ),
    # #1774 node-27 write-path least-privilege roles. `db/**` above only buys
    # tests/test_migrations.py, which never reads the role SQL; the runner is a
    # shell script with no same-name suite; and `infra/env/**` only buys the
    # two-node docker runtime suite. Without these three rows a change to the
    # provision SQL, the runner or a node-27 env template would ship with the
    # write-role guards unexecuted. The fourth producer, the autopipe stats
    # guard, is covered by an extra target MERGED into the existing
    # `scripts/node27_autopipeline.py` rule below, not by a row here.
    PathTestRule(
        "db/roles/node27_write_roles.sql",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "scripts/node27_provision_write_roles.sh",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "infra/env/node27-*.example",
        ("tests/test_node27_write_roles.py",),
    ),
    # #1774 round 4. Leg (iv) of the stored-expression sweep is an ALLOW-list of
    # (schema, name) pairs, and the promise made in the SQL comment, the test
    # docstring and runbook 9.6 is that a migration referencing a NEW function
    # reddens tests/test_node27_write_roles.py at PR time instead of the live
    # node-27 audit. That promise needs this row: `db/**` above only buys
    # tests/test_migrations.py, which never reads the allow-list, so a
    # migration-only PR that added e.g. `DEFAULT upper('x')` would have gone
    # green here and failed the strict audit on the node instead. The matcher is
    # fnmatch, whose `*` crosses `/`, so this pattern also covers a migration
    # parked in a subdirectory -- which is what the test-side rglob reads.
    PathTestRule(
        "db/migrations/*.sql",
        (
            "tests/test_node27_write_roles.py",
            # #1581: the parity lock derives the `hydro.run_status` member table
            # by sweeping this very glob as text, so a migration that adds an
            # enum member changes what that suite asserts -- and the "no member
            # outside the enum but `complete`" claim can only red on the
            # migration's own PR if the suite runs there. `db/**` above buys only
            # tests/test_migrations.py, which never parses the enum. 9 tests in
            # 0.29s, DB-free.
            "tests/test_hydro_status_set_parity.py",
        ),
    ),
    # the converted lanes with no pre-existing rule to merge into. The
    # superuser-gated-READ guard scans these sources, and such a read fails
    # SILENTLY under the new non-superuser role.
    PathTestRule(
        "scripts/node27_timeseries_retention.py",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "scripts/node27_download_cycles.py",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "scripts/node27_ingest_run.py",
        ("tests/test_node27_write_roles.py",),
    ),
    PathTestRule(
        "infra/compose.compute.yml",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "infra/compose.display.yml",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "infra/env/**",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    # #1684 EVID-05/F: the rollout producers must select the static deployment
    # contract suite. The runbook is `docs/**` (no backend lane by default) and
    # the env examples would otherwise select only the two-node runtime suite;
    # each is an exact additive rule so the runbook/env contract reddens on the
    # PR that rewrites the wiring.
    # #2075 widened this row by one: `tests/test_env_templates.py` reads THIS
    # runbook by path (`test_the_runbook_states_the_same_pinned_terminal_stage`
    # asserts the pinned `NHMS_ORCHESTRATOR_TERMINAL_STAGE=forecast_state_save_qc`
    # and `NHMS_REQUIRE_FORECAST_WARM_START=true` appear in it), so the
    # README/runbook/template mutual-consistency guard is selected whenever
    # this runbook is in the diff. `docs/**` does not open the ci.yml backend
    # lane, so the selection takes effect only when the lane opens for another
    # reason; a runbook-only PR relies on the master full run.
    # #2146 round 2 widened this row by one again: the probe suite `read_text`s
    # this runbook and asserts its probe section states every `VERDICT_*` name,
    # each threshold's default AND ceiling, the receipt field set taken from a
    # real `build_receipt` call, the receipt root, and the probe timer's own
    # steady-state row. Same lane caveat: selected when the backend lane runs
    # and this runbook is in the diff.
    # #2473 widened it by one more: the coverage freshness alert suite reads
    # this runbook's §11 (between the `## 11.` and `## 12.` headings) and fails
    # when a `COVERAGE_FRESHNESS_*` code the script emits is not named there.
    # #2472/#2473 round 1 added the three content readers the row had been
    # missing: the node-22 entrypoint invariant suite scans every `uv run` /
    # `uv sync` line of this runbook for a node-27 marker (a bare `uv sync`
    # added here reddens it), the Python environment truth suite pins the
    # `df -h / /home /data/GHDC` capacity check in it, and the role boundary
    # static suite pins its node-27/node-22 topology sentences.
    # #1103 split this file into an index landing page plus the
    # `docs/runbooks/production-ops/` sub-runbooks and repointed every reader
    # above at the whole tree. The two rows below carry the identical reader
    # set: the index keeps the historical path (and every anchor) alive, the
    # glob carries the body that moved.
    PathTestRule(
        "docs/runbooks/current-production-ops.md",
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
    PathTestRule(
        PRODUCTION_OPS_SUBRUNBOOK_GLOB,
        PRODUCTION_OPS_RUNBOOK_TESTS,
    ),
    # #2426 fix pass 1: the manual-recovery runbook has a literal reader on the
    # same footing as the rows above. `tests/test_operator_reentry_confirmation
    # .py::test_the_refusal_reasons_are_exactly_the_reachable_ones_the_runbook_lists`
    # `read_text`s this file and asserts the `<!-- reentry-refusal-reasons -->`
    # anchored block names exactly the `_refused` literals the CLI can still
    # emit, so a runbook-side edit to that list is precisely what the pin exists
    # to redden. Exact and additive. Same lane caveat as the rows above:
    # `docs/**` does not open the ci.yml backend lane, so the selection takes
    # effect only when the lane opens for another reason; a runbook-only PR
    # relies on the master full run.
    PathTestRule(
        "docs/runbooks/node22-control-plane-manual-recovery.md",
        ("tests/test_operator_reentry_confirmation.py",),
    ),
    PathTestRule(
        "infra/env/compute.example",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
    ),
    # #2075: `tests/test_env_templates.py` reads BOTH of the next two files by
    # path -- it parses the `nhms-required-keys: compute.scheduler-dbfree`
    # block out of `infra/env/README.md` and asserts the template satisfies
    # every entry, key and pinned value. Without these two targets a
    # template-only or README-only PR selected a non-empty set that held ZERO
    # readers of the changed file (the #2195 shape), so the guard that exists
    # to catch the missing `NHMS_ORCHESTRATOR_TERMINAL_STAGE` would not have
    # run on the PR that removed it.
    PathTestRule(
        "infra/env/compute.scheduler-dbfree.env.example",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST, "tests/test_env_templates.py"),
    ),
    PathTestRule(
        "infra/env/README.md",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST, "tests/test_env_templates.py"),
    ),
    # #2195: this template's owner suite reads it BY PATH and asserts its
    # content -- `tests/test_scheduler_refresh_deployment_contract.py`'s
    # `test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`
    # `read_text`s `infra/env/compute.scheduler-provider-refresh.env.example`
    # and asserts two groups: `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true` is
    # PRESENT, and none of `DATABASE_URL=`, `PIPELINE_DATABASE_URL=`, `PGHOST=`
    # or `PGPORT=` appears. Before this row the template matched only the
    # `infra/env/**` rule above, whose sole target
    # `tests/test_two_node_docker_runtime.py` never opens this file: the
    # selection was non-empty yet held ZERO readers of the changed file, so the
    # #1182 zero-assertion warning stayed silent as well. Not folded into the
    # #1684 group above because that group's target is the static deployment
    # contract suite, and that suite's template list does not include this file
    # -- it does not read it. This row does NOT make the selection
    # reader-complete: `tests/test_node27_write_roles.py` also `read_text`s this
    # template (its `_env_templates()` globs `infra/env/*.example`) and stays
    # unselected here, because its rule glob is `infra/env/node27-*.example`
    # and widening it is out of scope for #2195.
    # #2146 widened this row by one: `tests/test_node22_refresh_timer_health.py`
    # `read_text`s this template too, and pins its
    # `NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT=` line to the parent of the
    # probe's `DEFAULT_REFRESH_RECEIPT` -- the receipt the probe grades. A
    # template-only PR that moves the receipt root must run that pin, or the
    # probe watches a path nothing writes and reports `manifest_unavailable`.
    # #1101 re-pointed this row: the reading case moved to
    # `tests/test_scheduler_refresh_deployment_contract.py` when the monolith was
    # partitioned. Only that partition reads the template, so the row stays a
    # two-target row and the exact-set pin in tests/test_select_ci_tests.py stays
    # an exact set.
    PathTestRule(
        "infra/env/compute.scheduler-provider-refresh.env.example",
        (
            *SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
            "tests/test_node22_refresh_timer_health.py",
        ),
    ),
    PathTestRule(
        "scripts/validate_two_node_docker_runtime.py",
        ("tests/test_two_node_docker_runtime.py",),
    ),
    PathTestRule(
        "scripts/validate_two_node_docker_source_trust.py",
        ("tests/test_two_node_docker_source_trust.py",),
    ),
    PathTestRule(
        "scripts/validate_readonly_db_boundary.py",
        READONLY_DB_VALIDATION_TESTS,
    ),
    PathTestRule(
        # #1571: the continuous entrypoint's dedicated current-authority owner
        # joins its explicit suite, same-name selector meta-suite and #1656
        # timescale rider — extended AT THE RULE SITE so the old target and the
        # supplemental routing above stay exactly as they were. The owner is
        # asserted additively (membership), never as an exact set, because
        # supplemental selection is intentional.
        "scripts/run_qhh_continuous.py",
        ("tests/test_run_qhh_continuous.py", "tests/test_qhh_entrypoint_authority_invariant.py"),
    ),
    PathTestRule(
        # #1442 (group E). Both qhh smoke scripts own a registered
        # river_timeseries statement and had no rule at all, so they fell
        # through to the core-smoke fallback — which imports neither and asserts
        # nothing about their SQL. The cleanup oracle pins SQL shapes and the
        # QHH owner now exercises catalog-driven reset/summary entrypoints.
        # wire-site invariant suite also scans them, but is not PR-selected for
        # scripts/** — see issue #1656.
        "scripts/summarize_qhh_smoke_results.py",
        ("tests/test_river_ts_text_identity_cleanup.py", "tests/test_qhh_scripts_static.py"),
    ),
    PathTestRule(
        "scripts/reset_qhh_smoke_db.py",
        (
            "tests/test_river_ts_text_identity_cleanup.py",
            "tests/test_qhh_scripts_static.py",
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        # No same-name tests/test_node27_autopipeline.py exists, so without this
        # rule the autopipe script falls through to the core-smoke fallback and
        # none of its own suites run.
        "scripts/node27_autopipeline.py",
        (
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_autopipeline_handoff.py",
            # #1647: the `_connect` bounds and the stats-guard flag parser live
            # in their own suite, which the same-name fallback cannot find.
            "tests/test_node27_autopipeline_connection_bounds.py",
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
            "tests/test_pipeline_job_provenance_copyback.py",
            "tests/test_pipeline_ops_identity_envelope.py",
            # Autopipeline now injects _attributed_connect into the provenance
            # importer; the delegated attribution suite is the classification
            # oracle for that seam.
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_display_publish_status_only.py",
            # #1442/#1789: the publish criterion is a registered statement of
            # the zero-text-identity oracle (group D, no sanctioned aid at all),
            # and that oracle also censuses this file so a NEW fact-table
            # statement -- or the deleted ingest join coming back -- turns red.
            # It additionally pins the ingest criterion's authority-state gate.
            # Nothing above would notice any of it.
            "tests/test_river_ts_text_identity_cleanup.py",
            # #1774: the stats-guard ANALYZE legs are what force the writer
            # role to OWN the relations, so the write-role guards must run.
            "tests/test_node27_write_roles.py",
        ),
    ),
    PathTestRule(
        "scripts/backfill_pipeline_job_provenance.py",
        (
            "tests/test_pipeline_job_provenance_publisher.py",
            "tests/test_pipeline_job_provenance_importer.py",
        ),
    ),
    PathTestRule(
        # The cron wrapper is a shell script, not a backend python path, so the
        # core-smoke fallback never arms for it; without this rule a
        # wrapper-only PR selects nothing and CI degrades to --collect-only.
        "scripts/node27_autopipe_cron.sh",
        (
            "tests/test_node27_autopipeline_preflight.py",
            # #2013: a SECOND reader of the same wrapper, added by the prewarm
            # suite -- it parses the `${AUTOPIPE_MVT_PREWARM_WORKERS:-8}` and
            # `${AUTOPIPE_MVT_PREWARM_ZOOMS:-3,4,5}` fallbacks and asserts they
            # equal the module defaults, which is the only in-repo guard against
            # cron/module drift. The preflight suite carries no such assertion,
            # so before this entry a wrapper-only PR flipping `:-8` to `:-2`
            # merged green. Same discipline as the comment below: targets track
            # real references found by `grep -rln 'node27_autopipe_cron.sh'
            # tests/`, filtered to suites that actually READ the file. That grep
            # has two further hits, both correctly out: one names the wrapper in
            # a docstring only, the other is this row's own complement pin in
            # tests/test_select_ci_tests.py.
            "tests/test_node27_mvt_prewarm.py",
        ),
    ),
    # Shell wrappers with committed guard suites (#1138). Like the autopipe
    # cron rule above, none of these are backend python paths, so without an
    # explicit mapping a wrapper-only PR would select nothing; targets were
    # derived from `grep -rln '<script>.sh' tests/` and must track real
    # references. Wrappers with no guard suite intentionally have no rule here
    # and arm the core-smoke fallback via _is_backend_shell_path.
    # #1101: both wrappers are `read_text`-ed by the deployment-contract
    # partition (the systemd contract case reads the runner wrapper, the
    # installer lifecycle case reads the installer), so the split moved these two
    # targets rather than widening them.
    PathTestRule(
        "scripts/scheduler_file_provider_refresh_once.sh",
        SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    ),
    PathTestRule(
        "scripts/install_node22_scheduler_file_provider_refresh.sh",
        SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    ),
    # #2146 round 2: the refresh RUNNER had no explicit row -- only the
    # same-name derivation, which cannot know about a second reader. The probe
    # suite `read_text`s it for the `run_id` filename shape its history
    # fallback filters on, and imports it to pin `SCHEMA_VERSION` against the
    # probe's `REFRESH_RECEIPT_SCHEMA_VERSION`: the probe rejects any receipt
    # whose `schema_version` differs, so an unpinned runner schema bump would
    # kill the whole manifest arm silently. Round 4 adds two more imports: the
    # probe's trusted-outcome set is pinned as a subset of `OUTCOMES`, and its
    # history listing cap as above `MAX_HISTORY`.
    # #1101: this row was the runner's only surviving named route once the
    # same-name derivation died with the monolith, so it carries the WHOLE
    # fifteen-suite corpus -- a runner change can land in any partition.
    # #1099: the path is now a re-export facade over scripts/scheduler_refresh/.
    # It keeps its own row (it is still the `-m` entrypoint, still holds the
    # argparse surface and `main`, and is still the patch target the whole
    # corpus writes to), and the ten owner modules get the identical reach
    # immediately below.
    PathTestRule(
        SCHEDULER_REFRESH_OWNER_PATH,
        SCHEDULER_REFRESH_RUNNER_TESTS,
    ),
    # #1099: one row per owner module -- see SCHEDULER_REFRESH_PACKAGE_MODULES
    # for why these are enumerated rather than globbed, and why each carries the
    # runner's whole reach. The probe suite rides along because it reads the
    # history-receipt literals that now live in runner.py and receipt.py.
    PathTestRule("scripts/scheduler_refresh/classification.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/config.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/constants.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/cutover_declaration.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/identity.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/precommit_gate.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/providers.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/receipt.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/receipt_validation.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule("scripts/scheduler_refresh/runner.py", SCHEDULER_REFRESH_RUNNER_TESTS),
    PathTestRule(
        # #1102: the manual publisher had NO row of its own -- its entire route was
        # the selector's `tests/test_<module>.py` derivation onto the 3218-line
        # monolith. That derivation died with the monolith (no partition is named
        # `test_publish_scheduler_file_registry.py`), so without this row a
        # publisher-only diff would select nothing from the corpus and degrade to
        # the zero-assertion `--collect-only` smoke. It carries the WHOLE seven-suite
        # corpus: every partition drives `publish_all_basin_scheduler_registry` or
        # `main` out of this module, and it is the `monkeypatch.setattr` target of
        # 50 of the corpus's 52 patch sites (the other two name the refresh
        # facade), so a change to any of its seams can land in any partition.
        # #1100 will split this module; its owner
        # rows inherit this reach the same way scripts/scheduler_refresh/ did above.
        "scripts/publish_scheduler_file_registry.py",
        PUBLISH_SCHEDULER_REGISTRY_TESTS,
    ),
    # #1100: one row per owner module -- see PUBLISH_REGISTRY_PACKAGE_MODULES for
    # why these are enumerated rather than globbed, and why each carries the
    # whole publisher corpus.
    PathTestRule("scripts/publish_registry/calibration.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/cli.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/constants.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/model_version.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/publisher.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/radiation.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/registry_rows.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/selection.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    PathTestRule("scripts/publish_registry/workspace.py", PUBLISH_SCHEDULER_REGISTRY_TESTS),
    # #1611: the state-index copyback replay tool's own suite was partitioned
    # into four files and the 1378-line monolith deleted with no shim, which
    # killed the same-name derivation that had been this script's ONLY route.
    # An explicit row is the replacement: a dropped target here is a warning,
    # but a dropped ROUTE would have been silent.
    PathTestRule(
        STATE_INDEX_COPYBACK_REPLAY_OWNER_PATH,
        STATE_INDEX_COPYBACK_REPLAY_TESTS,
    ),
    # C3 verifies the scheduler manifest's shipping schema/checksum primitives.
    # After cold-family retirement that consumer contract is gone, while the
    # node-22 refresh-timer probe still copies `DEFAULT_MAX_MANIFEST_AGE_HOURS`
    # as its own `CONSUMER_MAX_MANIFEST_AGE_HOURS` (D4 forbids the probe importing
    # repo packages) and derives both threshold ceilings from it. The probe suite
    # asserts the two constants are equal, so a drop in the consumer's bound must
    # run it -- otherwise the probe keeps grading `ok` for a manifest the consumer
    # has already fail-closed on.
    PathTestRule(
        "services/orchestrator/scheduler_file_providers.py",
        ("tests/test_node22_refresh_timer_health.py",),
    ),
    # #1627 / ADR 0009: the loop-spelling measurement is the backing the
    # array-runner spec names for admitting path_modes.py's db-free
    # `_safe_preserve_final_component` arm — it measures, with real symlinks and
    # a real `os.lstat`, that the <=3.12 and 3.13+ spellings of a parent-chain
    # loop are NEUTRAL at the dereference. That claim is a property of THIS
    # module's function, so an edit to it must re-measure rather than wait for
    # the post-merge master run. A per-file row, not a widening of the broad
    # `services/orchestrator/**` list: the suite's subject is one function in
    # this one module, so routing it from the directory list would make every
    # orchestrator PR pay for a measurement of an unrelated PR class.
    # DB-free, 2 tests in 0.25s.
    PathTestRule(
        "services/orchestrator/scheduler_config/path_modes.py",
        ("tests/test_preserve_final_component_loop_spelling.py",),
    ),
    # #2401: the newest-truth guard on the completed-type terminal skips lives in
    # its own module and is consumed by the candidate-state decision.  Its
    # requirement suite (real-journal loops + decision-level recency rules) is
    # named after neither file, so same-name derivation cannot reach it. Per-file
    # rows, not a widening of the broad `services/orchestrator/**` list.
    # DB-free, 21 tests in ~10s.
    PathTestRule(
        "services/orchestrator/scheduler_state_terminal_recency.py",
        ("tests/test_scheduler_terminal_recency.py",),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_state_decision.py",
        ("tests/test_scheduler_terminal_recency.py",),
    ),
    # #2188: these two rows are systemd units, NOT `#1138` shell wrappers (that
    # block's targets were derived by grepping tests/ for `*.sh` references;
    # `infra/systemd/**` is a different surface, and the wrapper run resumes
    # just below with `scripts/node27_download_once.sh`). They sit next to the
    # wrapper/installer rows because they are the same refresh family with the
    # same owner suite.
    # `tests/test_scheduler_refresh_deployment_contract.py`
    # (`test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`)
    # `read_text`s BOTH files: on the `.service` it asserts
    # `ExecStart=/scratch/frd_muziyao/NWM/scripts/scheduler_file_provider_refresh_once.sh`,
    # `TimeoutStartSec=7200`, that `PrivateTmp=true` is ABSENT,
    # `UnsetEnvironment=DATABASE_URL PIPELINE_DATABASE_URL`, and the
    # `Before=` / `ExecCondition=` scheduler-independence pair (:3599-3611); on
    # the `.timer` it asserts `OnCalendar=*-*-* 02:15:00 UTC`,
    # `RandomizedDelaySec=30m` and `Persistent=false` (:3603-3604, :3633).
    # Both are outside the `#2173` glob `infra/systemd/nhms-node27-*.service`
    # (node-22 units), so neither row carries the sibling lane pin.
    # #2146 widened this row by one: `tests/test_node22_refresh_timer_health.py`
    # `read_text`s THIS unit too and asserts the probe unit's
    # `UnsetEnvironment=` line is byte-equal to this one's -- node-22 is
    # permanently DB-free and the probe must clear the same libpq selector set.
    # Editing this service's selector list without editing the probe's would
    # red that assertion, so the probe suite is a literal reader of this path.
    # It also reads this unit's `TimeoutStartSec=` and pins the probe's default
    # stopped-dwell at three times it.
    PathTestRule(
        "infra/systemd/nhms-scheduler-file-provider-refresh.service",
        (
            *SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
            "tests/test_node22_refresh_timer_health.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-scheduler-file-provider-refresh.timer",
        SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    ),
    # #2146: the refresh-lane health probe's installer and its two units.
    # `tests/test_node22_refresh_timer_health.py` reads all three by path --
    # it `read_text`s both units (`Type=oneshot`, `TimeoutStartSec=`, no
    # `PrivateTmp` directive, the `ExecStart` script path, the
    # `UnsetEnvironment=` line byte-equal to the refresh service's,
    # `OnCalendar=hourly`, `Persistent=true`) and it runs the installer as a
    # subprocess against a fake systemctl, asserting the four protected units
    # are only ever read. The probe's own `scripts/node22_refresh_timer_health.py`
    # needs no row -- the same-name rule already routes it.
    PathTestRule(
        "scripts/install_node22_refresh_timer_health.sh",
        ("tests/test_node22_refresh_timer_health.py",),
    ),
    PathTestRule(
        "infra/systemd/nhms-node22-refresh-timer-health.service",
        ("tests/test_node22_refresh_timer_health.py",),
    ),
    PathTestRule(
        "infra/systemd/nhms-node22-refresh-timer-health.timer",
        ("tests/test_node22_refresh_timer_health.py",),
    ),
    PathTestRule(
        "scripts/node27_download_once.sh",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_once.sh",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_discovery.py",
        (
            "tests/test_node27_timeseries_discovery.py",
            "tests/test_node27_lifecycle_contract.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            # #1647: `_CHUNK_IDENT_RE` is pinned byte-equal to the autopipeline
            # `_STATS_GUARD_IDENT_RE` from that suite, so loosening the pattern
            # here reds there — mirror of the autopipeline row above.
            "tests/test_node27_autopipeline_connection_bounds.py",
            # #1774: this lane runs as a non-superuser; a superuser-gated
            # READ added here would fail SILENTLY.
            "tests/test_node27_write_roles.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_supervisor.py",
        ("tests/test_node27_lifecycle_contract.py",),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression_capture.py",
        (
            "tests/test_node27_timeseries_discovery.py",
            # #1990 task 7.2: this path is in the forcing discovery-set
            # census (or holds a registered forcing read template), so a new
            # met.forcing_station_timeseries mention here must redden the
            # census on THIS PR rather than on the post-merge master run.
            *FORCING_SQL_SHAPE_ORACLE_TESTS,
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-compression.service",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-retention.timer",
        (
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        # #2013: same shape as the retention `.timer` row above. A timer-only
        # diff matched NOTHING before this rule -- `infra/**` is not a backend
        # python path and `_is_backend_shell_path` is scoped to `scripts/**.sh`,
        # so not even the core-smoke fallback armed and CI degraded to
        # --collect-only. Both targets really read this file: the preflight
        # suite asserts `OnUnitActiveSec=10min` is present, and the prewarm
        # suite parses the same directive into seconds and pins the budget
        # inequality `DEFAULT_DEADLINE_SECONDS + DEFAULT_TIMEOUT_SECONDS <=
        # tick`, so shortening the tick must red here rather than after merge.
        "infra/systemd/nhms-node27-autopipe.timer",
        (
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_mvt_prewarm.py",
        ),
    ),
    PathTestRule(
        # #1712: the unit file carries the `StandardError=journal` lane and the
        # `OnFailure=` alert wiring, both pinned by unit-file tests. Without an
        # explicit row it is infra/** non-python, matches nothing, and a
        # unit-only PR selected zero tests. Narrow on purpose — the `.timer`
        # sibling is timer-schedule coverage, not something the unit body can break.
        "infra/systemd/nhms-node27-timeseries-retention.service",
        ("tests/test_node27_timeseries_retention.py",),
    ),
    PathTestRule(
        # #2032: same shape as the retention `.service` row above. `infra/**`
        # is not a backend python path and `_is_backend_shell_path` is scoped
        # to `scripts/**.sh`, so a unit-only diff matches NOTHING and CI
        # degrades to --collect-only. The suite really reads this file: the
        # `ExecStartPre` < `ExecStart` ordering and the append log paths.
        # #2170: the sibling-lane pin in `tests/test_node27_timeseries_retention.py`
        # is a glob reader over `infra/systemd/nhms-node27-*.service`, so a
        # path-exact unit rule that does not target it makes targeted PR CI
        # constructively skip the pin -- it only reds on master's full run.
        "infra/systemd/nhms-node27-mvt-cache-retention.service",
        (
            "tests/test_node27_mvt_cache_retention.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        # #2173: the general case of the two rows above. The sibling-lane pin in
        # `tests/test_node27_timeseries_retention.py` is a GLOB reader over
        # exactly `infra/systemd/nhms-node27-*.service`, so the rule is aligned
        # to the same glob instead of chasing it one path-exact row at a time --
        # eight of ten units had no row naming the pin, six had no row at all
        # and degraded to --collect-only. Matches accumulate (`selected.update`
        # below, no `stop_on_match`), so every path-exact unit rule above and
        # below keeps its own targets and the retention `.service` selection
        # stays exactly the pin suite. A unit created later is covered without a
        # per-unit rule. `.timer` files are outside the pin's glob and are
        # deliberately not covered here.
        "infra/systemd/nhms-node27-*.service",
        ("tests/test_node27_timeseries_retention.py",),
    ),
    PathTestRule(
        # #2180: the glob row above gives every unit the sibling-lane pin, but
        # that pin asserts ONLY the `StandardError=append:…systemd.err` lane
        # set -- it never reads this unit's directives, so before this row an
        # autopipe-unit-only diff ran zero assertions about the unit's body.
        # `tests/test_node27_autopipeline_preflight.py:19` resolves this exact
        # path and `:1111-1119` asserts the `scripts/node27_autopipe_cron.sh`
        # ExecStart, `NODE27_AUTOPIPE_BOOTSTRAP_LOG=…/bootstrap.log`, and that
        # `infra/env/display.env` is absent (the data-plane / display-plane
        # boundary). Matches accumulate (`selected.update`, no `stop_on_match`),
        # so the pin suite arrives from the glob row and is deliberately not
        # repeated here.
        "infra/systemd/nhms-node27-autopipe.service",
        ("tests/test_node27_autopipeline_preflight.py",),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_download_cycles.py:18` resolves this exact
        # path and `:495` asserts the `scripts/node27_download_once.sh`
        # ExecStart -- swapping the wrapper the unit runs is invisible to the
        # lane pin. Pin suite comes from the glob row by accumulation, not
        # repeated here.
        "infra/systemd/nhms-node27-download.service",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_frontier_stall_alert.py:1427` resolves this
        # exact path and `:1553-1562` asserts
        # `Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`,
        # `EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env` and
        # `TimeoutStartSec=900` -- deleting the sentinel `Environment=` line is
        # invisible to the lane pin. Pin suite comes from the glob row by
        # accumulation, not repeated here.
        "infra/systemd/nhms-node27-frontier-alert.service",
        ("tests/test_node27_frontier_stall_alert.py",),
    ),
    PathTestRule(
        # #2180: `tests/test_node27_raw_retention.py:317-322` resolves this
        # exact path and `:325-341` asserts
        # `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`,
        # `StandardOutput=append:…/systemd.log`, and that the `ExecStartPre=`
        # line precedes `ExecStart=` (reordering them breaks the log-dir
        # bootstrap and the lane pin cannot see it). Pin suite comes from the
        # glob row by accumulation, not repeated here.
        "infra/systemd/nhms-node27-raw-retention.service",
        (
            "tests/test_node27_raw_retention.py",
            # #2360: asserts this unit's `OnFailure=` line.
            "tests/test_node27_raw_retention_canonical_deployment.py",
        ),
    ),
    PathTestRule(
        # #2360: the canonical system unit's timer must tick with this one (one
        # cutoff date for a mirror and its PNGs); the deployment suite compares
        # the two `OnCalendar=` lines. No other suite reads this timer.
        "infra/systemd/nhms-node27-raw-retention.timer",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: the system alert template must run the SAME handler; the
        # deployment suite compares its `ExecStart=` with this template's. The
        # retention suite (its other reader) arrives from the `#2173` glob row.
        "infra/systemd/nhms-node27-unit-failure-alert@.service",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: node-27 SYSTEM units (canonical retention + its timer + the
        # system alert template). Outside the `#2173` glob on purpose: they are
        # not nwm user units and carry no `systemd.err` lane. The deployment
        # suite reads every file here and pins the directory's exact file set,
        # so the row is a directory glob, not per-file.
        "infra/systemd/system/*",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: root installer for the canonical system unit. `scripts/**.sh`
        # only arms core smoke; the deployment suite drives its refusal path and
        # its env rendering.
        "scripts/node27_canonical_retention_install.sh",
        ("tests/test_node27_raw_retention_canonical_deployment.py",),
    ),
    PathTestRule(
        # #2360: the unit-failure alert handler (user + system journal scope).
        # Before this row a handler-only diff fell to core smoke: its real
        # readers are the retention suite's alert-wrapper rows (incl. the
        # journal-scope switch) and the working-set suite's governance run.
        "scripts/node27_unit_failure_alert_once.sh",
        (
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_raw_retention_canonical_deployment.py",
        ),
    ),
    PathTestRule(
        # #2180: two suites read this unit by path.
        # `tests/test_node27_timeseries_compression.py:31` + `:1964-1976`
        # asserts the supervisor `--enforce` invocation and its
        # `--run-plan-path` / `--ledger-path` / `--receipt-path` /
        # `--finalizer-state-path` / `--wall-seconds 900` options, the
        # `ExecStopPost=` + `--finalize-only` finalizer lane and
        # `TimeoutStartSec=920`;
        # `tests/test_node27_timeseries_compression_supervisor.py:1233-1238`
        # asserts the
        # `EnvironmentFile=/home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env`
        # digest pin. Both are targets. Pin suite comes from the glob row by
        # accumulation, not repeated here.
        "infra/systemd/nhms-node27-timeseries-compression-replay.service",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
        ),
    ),
    PathTestRule(
        # #2032: the same suite parses `OnCalendar=*-*-* 04:05:00 UTC` and
        # `Persistent=true` out of the timer, so the schedule is assertable at
        # PR time rather than at `systemctl --user list-timers`.
        "infra/systemd/nhms-node27-mvt-cache-retention.timer",
        ("tests/test_node27_mvt_cache_retention.py",),
    ),
    PathTestRule(
        # #2180: same shape as the mvt-cache-retention `.timer` row above and
        # the same zero-selection hole -- `infra/**` is not a backend python
        # path, `_is_backend_shell_path` is scoped to `scripts/**.sh`, and the
        # `#2173` pin glob is `*.service`, so a timer-only diff matched NOTHING
        # and CI degraded to --collect-only. The suite really reads this file:
        # `tests/test_node27_download_cycles.py:19` resolves the timer by path
        # and `:496` asserts `OnUnitActiveSec=30min`, so lengthening the tick
        # reds at PR time instead of at `systemctl --user list-timers`.
        "infra/systemd/nhms-node27-download.timer",
        ("tests/test_node27_download_cycles.py",),
    ),
    PathTestRule(
        # #2180: the compression suite reads this timer by path and asserts
        # `OnCalendar=*-*-* 04:25:00 UTC` plus
        # `Unit=nhms-node27-timeseries-compression.service`. Compression.timer
        # never selects the retention suite. Live-evidence/capture only
        # `read_bytes` this timer into a fixture and assert nothing about its
        # content -- not targets. The `#2173` pin glob is `*.service`, so this
        # row is the whole selection for a timer-only diff.
        "infra/systemd/nhms-node27-timeseries-compression.timer",
        (
            "tests/test_node27_timeseries_compression.py",
        ),
    ),
    PathTestRule(
        # #2188: the display API unit runs on node-27 but is named OUTSIDE the
        # `#2173` pin glob `infra/systemd/nhms-node27-*.service`, so it matched
        # nothing at all (`infra/**` is not a backend python path and
        # `_is_backend_shell_path` is scoped to `scripts/**.sh`) and a
        # unit-only diff degraded to a zero-assertion --collect-only smoke.
        # `tests/test_hydro_display_mvt_scaling.py:184-190` (#2074 renumbered the
        # citation when the suite was partitioned; the reading case stayed on this
        # path, which is one reason the base path survives the split)
        # (`test_systemd_workers_receive_shared_file_cache_default`) `read_text`s
        # this exact path and asserts the two directives that carry the public
        # display entrypoint's cache/worker contract:
        # `export NHMS_MVT_FILE_CACHE_DIR="${NHMS_MVT_FILE_CACHE_DIR:-/home/nwm/.cache/nhms/mvt}"`
        # and `--workers "${NHMS_DISPLAY_WORKERS:-2}"`.
        # Not in the `#2173` glob => this unit takes NO sibling lane pin, so
        # `tests/test_node27_timeseries_retention.py` must NOT appear in this
        # row's targets (the lane pin never reads this unit's body anyway).
        "infra/systemd/nhms-display-api.service",
        ("tests/test_hydro_display_mvt_scaling.py",),
    ),
    PathTestRule(
        "schemas/timeseries_compression_receipt.schema.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_compression_receipt.example.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "schemas/timeseries_retention_receipt.schema.json",
        ("tests/test_node27_lifecycle_contract.py",),
    ),
    PathTestRule(
        # #2032: the `infra/env/node27-*.example` glob above only buys
        # tests/test_node27_write_roles.py, which never reads this template.
        # This row is what makes the cache-root warning, the health `jq`
        # criterion and the two rollback gates assertable at PR time. Additive:
        # the glob row still matches and both target sets are unioned.
        "infra/env/node27-mvt-cache-retention.example",
        ("tests/test_node27_mvt_cache_retention.py",),
    ),
    PathTestRule(
        # #2104, same ground as the #2032 row above: the two globs that match
        # this template (`infra/env/node27-*.example` -> write-roles, which
        # only scans every template for DSN users and credential placeholders;
        # `infra/env/**` -> the docker runtime pin, which never opens it) assert
        # nothing about this file's body, so a template-only PR selected a
        # non-empty set with zero readers of the documented criterion -- the
        # #2195 shape. Both targets below really read this path:
        # `tests/test_node27_raw_retention.py::_documented_operator_jq_program`
        # EXTRACTS the documented `jq` program out of this file and runs it
        # against a real summary, so a typo in the program reds there and
        # nowhere else, and
        # `tests/test_node27_mvt_cache_retention.py::test_the_raw_retention_env_example_points_at_this_runner`
        # pins the sibling-runner cross-reference in it. Additive: the glob rows
        # still match and every target set is unioned.
        "infra/env/node27-raw-retention.example",
        (
            "tests/test_node27_raw_retention.py",
            "tests/test_node27_mvt_cache_retention.py",
        ),
    ),
    PathTestRule(
        "infra/env/node27-timeseries-compression.example",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        "docs/runbooks/tier-node27-timeseries-storage.md",
        (
            *NODE27_PGDATA_WORKLOAD_TESTS,
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_lifecycle_contract.py",
        ),
    ),
    PathTestRule(
        # #2417 fix pass 1: the curve statement's derivation from its public owner
        # moved out of scripts/node27_timeseries_compression_benchmark.py so the
        # OFFLINE verifier stops importing a capture CLI to get at it. Same-name
        # derivation cannot see the new module (there is no
        # tests/test_forecast_curve_capture.py) and the shared `packages/common/**`
        # add-on is core-smoke only, so without this entry a change to the derivation
        # would reach CI with NEITHER of its two real consumers executed — a
        # narrowing against what the benchmark script's same-name rule used to give.
        "packages/common/forecast_curve_capture.py",
        (
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_compression_budget.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_budget_preflight.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_budget.py",
            "tests/test_node27_timeseries_compression_runner_config.py",
            "tests/test_node27_timeseries_compression_wrappers.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_lifecycle_lock.py",
        (
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_timeseries_decompression_replay.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_retention_once.sh",
        (
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_raw_retention_once.sh",
        ("tests/test_node27_wrapper_pythonpath.py",),
    ),
    PathTestRule(
        # #2032: the MVT cache-retention wrapper. `scripts/**.sh` only arms the
        # core-smoke fallback when no rule matches, so without this row a
        # wrapper-only diff would reach the PR lane with the env-file refusal,
        # flock-skip and rc-propagation assertions unexecuted. Deliberately not
        # routed to tests/test_node27_wrapper_pythonpath.py: this wrapper
        # exports no PYTHONPATH (its runner imports nothing from the repo).
        "scripts/node27_mvt_cache_retention_once.sh",
        ("tests/test_node27_mvt_cache_retention.py",),
    ),
    PathTestRule(
        "scripts/node27_frontier_stall_alert_once.sh",
        ("tests/test_node27_frontier_stall_alert.py",),
    ),
    PathTestRule(
        "scripts/run_qhh_backend_smoke.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        # #1571: the cycle wrapper's dedicated current-authority owner joins the
        # three pre-existing targets additively (asserted by membership, never
        # as an exact set — supplemental selection is intentional).
        "scripts/run_qhh_cycle.sh",
        (
            "tests/test_run_qhh_continuous.py",
            "tests/test_role_boundary_static.py",
            "tests/test_qhh_scripts_static.py",
            "tests/test_qhh_entrypoint_authority_invariant.py",
        ),
    ),
    PathTestRule(
        # #1571: `.python-version` is the single producer for the repository
        # default-Python oracle. Not a backend Python path, so without this rule
        # a pin-only PR selects nothing and CI degrades to collect-only.
        ".python-version",
        (PYTHON_ENVIRONMENT_TRUTH_TEST,),
    ),
    PathTestRule(
        # #1571: the generated-root instruction SOURCE (instructions/agents/
        # shared.md) is the producer that governs the byte-exact CLAUDE.md /
        # AGENTS.md projection too; a source-only diff must reach the oracle
        # that pins both semantic clauses. Non-stop: `docs/**`-style generated
        # roots themselves remain unrouted by design.
        # #1571 local-repair: the shared source's node-22 deferred-environment
        # clause is asserted by the node-22 owner, so it joins additively
        # alongside the existing Python-environment owner.
        "instructions/agents/shared.md",
        (PYTHON_ENVIRONMENT_TRUTH_TEST, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        # #1571: the two-node Docker runbook is `infra/**`, which already opens
        # the backend lane; the rule converts that collect-only lane into real
        # assertions. Exact path, deliberately NOT a glob over infrakdown —
        # other runbooks must not start the backend lane (inventory scope).
        "infra/README.two-node-docker.md",
        (TWO_NODE_DOCKER_RUNBOOK_ENV_TEST,),
    ),
    PathTestRule(
        QHH_CYCLE_SBATCH,
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        QHH_DIAGNOSTIC_README,
        # #1571 local-repair: the Production Replacement lines carry node-22
        # exact-interpreter semantics that QHH-static does not assert, so the
        # node-22 owner joins additively alongside the existing QHH-static
        # target. Extended AT THE RULE SITE, non-stop: the README keeps both
        # owners and no other producer's selection moves.
        ("tests/test_qhh_scripts_static.py", *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        "scripts/local_pg.sh",
        ("tests/test_qhh_scripts_static.py",),
    ),
    PathTestRule(
        # #1571: the gateway unit's deferred-venv ExecStart is uniquely asserted
        # by the node-22 owner. infra/** already starts the backend lane, so
        # without this rule a unit-only PR selected nothing and CI degraded to
        # collect-only; the exact rule converts that lane into real assertions.
        # #1684 EVID-05: the unit's active EnvironmentFile / no-inline-secret
        # contract is asserted by the static deployment suite, which joins the
        # rule alongside the node-22 owner.
        NODE22_SLURM_GATEWAY_UNIT,
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        # #1571: the evidence-retention unit's single exact ExecStart is
        # uniquely asserted by the node-22 owner.
        NODE22_RETENTION_UNIT,
        NODE22_ENTRYPOINT_INVARIANT_TESTS,
    ),
    PathTestRule(
        # The journal archive service and timer jointly define one mutation
        # entrypoint. Each changed unit must run its active-runtime invariant
        # and both archive/retention behavioral partitions.
        NODE22_JOURNAL_RETENTION_SERVICE,
        (*NODE22_ENTRYPOINT_INVARIANT_TESTS, *JOURNAL_RETENTION_TESTS),
    ),
    PathTestRule(
        NODE22_JOURNAL_RETENTION_TIMER,
        (*NODE22_ENTRYPOINT_INVARIANT_TESTS, *JOURNAL_RETENTION_TESTS),
    ),
    PathTestRule(
        # #1571: the repair script's usage string is uniquely asserted by the
        # node-22 owner. A rule suppresses the unknown-backend core-smoke
        # fallback (matched=True), so the script's CURRENT CORE_SMOKE selection
        # is preserved EXPLICITLY here — without these targets an exact rule
        # would silently drop them. scripts/** adds the #1656 timescale rider
        # supplementally. Owner joins additively, never replacing core smoke.
        NODE22_REPAIR_SCRIPT,
        (*CORE_SMOKE_TESTS, *NODE22_ENTRYPOINT_INVARIANT_TESTS),
    ),
    PathTestRule(
        "scripts/ops/node22-run-cycle-once.sh",
        ("tests/test_production_scheduler.py",),
    ),
    PathTestRule(
        # #1823: the display-API wrapper's entropy reach is now the fifteen
        # partitions, spliced in rather than globbed so the routed set is the
        # reviewed tuple.
        "scripts/ops/start-display-api.sh",
        (
            "tests/test_two_node_docker_runtime.py",
            *ENTROPY_AUDIT_TESTS,
        ),
    ),
    PathTestRule(
        # #1823: the audit owner's corpus. Every partition drives `build_report`
        # or the CLI against this module, so the whole corpus rides the rule.
        # #1842 sank the twenty-one owner modules out of it; this path stays the
        # console entrypoint and the attribute-broadcast facade the whole corpus
        # still patches, and the owner modules get the identical reach below.
        ENTROPY_AUDIT_OWNER_PATH,
        ENTROPY_AUDIT_TESTS,
    ),
    # #1842: one row per owner module -- see ENTROPY_AUDIT_PACKAGE_MODULES for
    # why these are enumerated rather than globbed, and why each carries the
    # whole fifteen-partition corpus.
    PathTestRule("scripts/governance/entropy_audit/archive_status.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_env_and_tokens.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_paths_and_api.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_stale_routes.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/check_topology.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/constants.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/facade_guard.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/findings.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/repo_files.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/report.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/route_governing_text.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/route_mentions.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/schema.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/scoped_context.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_budget.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_growth.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_sources.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/structural_surface.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_context_rules.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_display_env.py", ENTROPY_AUDIT_TESTS),
    PathTestRule("scripts/governance/entropy_audit/topology_predicates.py", ENTROPY_AUDIT_TESTS),
    PathTestRule(
        # #1823: the baseline writer's own cases live in the two
        # `..._baseline_writer_*` partitions, but its output is the baseline the
        # report/allowlist/budget partitions read, so the rule keeps the whole
        # corpus the monolith gave it rather than narrowing reach at the split.
        "scripts/governance/write_entropy_baseline.py",
        ENTROPY_AUDIT_TESTS,
    ),
    PathTestRule(
        # The checked-in node-22 executable is a compatibility CLI; its owner
        # modules and split contract suites are routed explicitly because no
        # same-name test file exists.
        "scripts/node22_scheduler_journal_retention.py",
        (
            "tests/test_scheduler_journal_retention_planning.py",
            "tests/test_scheduler_journal_retention_archive.py",
        ),
    ),
    PathTestRule(
        "scripts/select_ci_tests.py",
        (SELECTOR_META_GUARD_TEST,),
    ),
    # #1650 self-routing: ci.yml's top-level concurrency and the backend
    # paths-filter ARE the contract tests/select_ci_tests.py pins, so a
    # workflow-only PR must select the meta-guard suite and not collapse to
    # core smoke. Backed by ci.yml's own `backend` paths-filter entry; both
    # legs together make a workflow-only PR run these assertions.
    PathTestRule(
        ".github/workflows/ci.yml",
        (SELECTOR_META_GUARD_TEST,),
    ),
    PathTestRule(
        # #1860: the calibration declaration's assertion-level consumers. The
        # exact backend filter entry starts the targeted gate; this rule
        # converts that lane into real assertions — never the core-smoke
        # fallback or a zero-assertion collect-only run.
        CALIBRATION_OVERRIDES_PATH,
        CALIBRATION_OVERRIDES_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #2261: the review-gate issue memory's structural guard. The exact
        # ci.yml backend filter entry starts the targeted gate for an
        # accounting-only PR; this rule turns that lane into real assertions
        # instead of the zero-assertion collect-only collapse.
        REVIEW_GATE_ISSUE_MEMORY_PATH,
        REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS,
    ),
    PathTestRule(
        # #1646: a pytest-config change must re-prove the thread-exception
        # policy (the file carries the exact filter and the no-timeout
        # decision) and still keep core smoke plus the selector meta-guard.
        # #1894 additionally registers the dedicated disposable-Dockerker,
        # whose gate contract is asserted without executing its real rows.
        "pyproject.toml",
        (
            *CORE_SMOKE_TESTS,
            *THREAD_EXCEPTION_POLICY_TESTS,
            "tests/test_node27_docker_collection_gate.py",
            # #1765: `tmp_path_retention_policy` lives in the same table and is
            # asserted here (parsed key + the running session's resolved ini).
            PYTHON_ENVIRONMENT_TRUTH_TEST,
            SELECTOR_META_GUARD_TEST,
        ),
    ),
    PathTestRule(
        # #1646: a dependency-lock change could add pytest-timeout, so the lock
        # rule must also run the policy suite (which asserts no such package is
        # resolved) alongside core smoke and the selector meta-guard.
        "uv.lock",
        (*CORE_SMOKE_TESTS, *THREAD_EXCEPTION_POLICY_TESTS, SELECTOR_META_GUARD_TEST),
    ),
    # #1562 structural split owners.  Additive (non-stop) on purpose: the broad
    # `services/orchestrator/**` rule below the stop rules already carries the
    # integration suites for these owners, and these narrow rules only attach
    # the dedicated focused suite.  Without them an owner-only PR would run the
    # integration suites but never this suite's own assertions.
    PathTestRule(
        "services/orchestrator/chain_forced_resubmit.py",
        FORCED_RESUBMIT_SURFACE_TESTS,
    ),
    PathTestRule(
        "services/orchestrator/chain_array_evidence.py",
        FORCED_RESUBMIT_SURFACE_TESTS,
    ),
    # #1684 shared-auth owner-to-focused-suite mappings (EVID-01). Additive
    # (non-stop) on purpose: the broad `apps/api/**` / `services/orchestrator/**`
    # rules keep their existing riders, `packages/common/**` keeps its #1744
    # core-smoke baseline and the #1656 timescale rider; these rows only attach
    # the focused contracts an owner-only PR previously could not reach.
    PathTestRule(
        "packages/common/auth_policy.py",
        (AUTH_POLICY_TEST,),
    ),
    PathTestRule(
        "packages/common/request_auth.py",
        (
            SLURM_AUTH_CORE_TEST,
            SLURM_AUTH_FULLMOUNT_TEST,
            SLURM_AUTH_CLIENT_TEST,
            SLURM_AUTH_DEPLOYMENT_TEST,
        ),
    ),
    PathTestRule(
        "packages/common/openapi_auth_security.py",
        (SLURM_OPENAPI_SECURITY_TEST,),
    ),
    # Retained PGDATA owners keep only their real command, migration, oracle,
    # evidence, and C4 acceptance consumers.
    PathTestRule(
        "packages/common/node27_pgdata_command.py",
        (
            "tests/test_node27_pgdata_command.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
            *C4_PRODUCTION_ACCEPTANCE_TESTS,
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_container.py",
        (
            "tests/test_node27_pgdata_container.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_evidence.py",
        (
            "tests/test_node27_pgdata_evidence.py",
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_host.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_pgdata_migrate.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "scripts/node27_pgdata_migrate.py",
        (
            "tests/test_node27_pgdata_migrate.py",
            "tests/test_node27_pgdata_migrate_oracle.py",
        ),
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_migrate.py",
        ("tests/test_node27_pgdata_migrate.py",),
        stop_on_match=True,
    ),
    PathTestRule(
        "tests/test_node27_pgdata_migrate_oracle.py",
        ("tests/test_node27_pgdata_migrate_oracle.py",),
        stop_on_match=True,
    ),
    PathTestRule(
        "packages/common/node27_resource_governance_collection.py",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_maintenance_output_integration.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_resource_governance.py",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
            "tests/test_node27_maintenance_output_integration.py",
        ),
    ),
    PathTestRule(
        "infra/env/node27-resource-governance.example",
        (
            "tests/test_node27_resource_governance.py",
            "tests/test_node27_working_set.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-resource-governance.service",
        ("tests/test_node27_resource_governance.py",),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-resource-governance.timer",
        ("tests/test_node27_resource_governance.py",),
    ),
    PathTestRule(
        "scripts/node27_resource_governance_once.sh",
        (*CORE_SMOKE_TESTS, "tests/test_node27_resource_governance.py"),
    ),
    PathTestRule(
        "apps/api/auth.py",
        (
            AUTH_POLICY_TEST,
            "tests/test_role_boundary_static.py",
        ),
    ),
    PathTestRule(
        "services/orchestrator/chain_slurm_client.py",
        (SLURM_AUTH_CLIENT_TEST,),
    ),
    PathTestRule(
        "services/orchestrator/scheduler_gateway.py",
        (SLURM_AUTH_DEPLOYMENT_TEST,),
    ),
    PathTestRule(
        "apps/api/routes/pipeline.py",
        (
            *CONNECTION_ATTRIBUTION_TESTS,
            "tests/test_pipeline_ops_identity_envelope.py",
            # #2385/#2387: the read-blocked sentinel suite drives
            # `POST /runs/{run_id}/retry` through TestClient for both consumer
            # ends -- the 409 `RETRY_EVIDENCE_INVALID` refusal body and the
            # recorded-failure delta on the provenance walk -- so a change to
            # this route's except table or its 503 assembly must run it.
            "tests/test_file_journal_read_blocked_consumers.py",
        ),
    ),
    PathTestRule(
        # #2385: the manual-retry source selector's THIRD caller. `_preview`
        # runs outside `main()`'s try, so the selector's new refusal has to be
        # turned into a receipt entry here; the sentinel suite is the only
        # oracle for that receipt shape and exit code.
        "scripts/node22_manual_retry_failed_runs.py",
        ("tests/test_file_journal_read_blocked_consumers.py",),
    ),
    *(
        PathTestRule(path, CONNECTION_ATTRIBUTION_TESTS)
        for path in CONNECTION_ATTRIBUTION_ROUTE_PATHS + CONNECTION_ATTRIBUTION_STORE_PATHS
    ),

    PathTestRule(
        "apps/api/errors.py",
        (API_ERROR_LOGGING_TEST, "tests/test_display_mvt_cold_admission.py"),
    ),
    PathTestRule(
        # #2098: the route-reachability composition owner. This module imports
        # `precip_router` into `_BUSINESS_ROUTERS`, and `register_role_aware_routes`
        # walks that tuple to `include_router` each one; drop the entry and both
        # published endpoints — /api/v1/precip/{source}/{cycle}/index and
        # .../{valid_time}.png — leave the route table entirely.
        # tests/test_precip_overlay.py is the most direct behavioural oracle (see its
        # `test_dropping_precip_router_...` mutation proof); the cut also reds
        # tests/test_openapi_drift.py (its whole-document static/runtime comparison — the
        # committed openapi/nhms.v1.yaml carries both precip paths) and
        # tests/test_openapi_31_contract.py (its BASELINE_NULLABLE_COUNT counts the two
        # routes' typed 404s, as that constant's own comment says). Naming all three is
        # over-justification, not under-coverage. The three broad `apps/api/**` suites
        # exercise no precip route, so before this entry a registry-only diff reached
        # the targeted lane with a plausible five-suite selection and no precipitation
        # oracle at all (#1182's zero-assertion warning cannot fire on a non-empty
        # selection).
        # #1728's connection-attribution guards are MERGED here rather than left in
        # CONNECTION_ATTRIBUTION_ROUTE_PATHS: this module now has an exact rule, and
        # a duplicate pattern splits its ownership across two
        # (test_path_rule_duplicate_patterns_are_allowlisted_decisions). Same shape
        # #2078 used for apps/api/routes/forecast.py.
        "apps/api/route_registry.py",
        (*CONNECTION_ATTRIBUTION_TESTS, *PRECIP_SURFACE_TESTS),
    ),
    PathTestRule(
        "apps/api/main.py",
        (
            API_ERROR_LOGGING_TEST,
            "tests/test_display_mvt_cold_admission.py",
            # #2098: the runtime-OpenAPI composition owner. `_patch_openapi_schema`
            # calls `_patch_precip_openapi(schema)`, which pops the generated
            # `PrecipIndexResponse` component and rewrites the index operation onto
            # the shared `allOf: [SuccessEnvelope, {data}]` envelope that the
            # hand-maintained openapi/nhms.v1.yaml carries. Drop that call site and
            # the runtime schema drifts from the committed document at both places,
            # and of the suites this rule selects only tests/test_openapi_drift.py
            # reds (see its `test_dropping_the_precip_openapi_patch_...` mutation
            # proof). That is a claim about THIS call site, not "the repo's only
            # static/runtime oracle": tests/test_api_contract.py compares
            # openapi/nhms.v1.yaml against `app.openapi()` too, but at no
            # precipitation path. Neither the #1704 error-logging rider above nor the
            # three broad `apps/api/**` suites assert on the precipitation schema, and
            # tests/test_openapi_31_contract.py applies openapi_patching's patch
            # functions directly — a list that omits `_patch_precip_openapi`.
            *PRECIP_SURFACE_TESTS,
        ),
    ),
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
    PR) OR when the changed-file set touches the selector itself
    (``scripts/select_ci_tests.py`` or ``tests/test_select_ci_tests.py``) —
    the class of diff that rewrites the gate and must not silently lose the
    full-tree collection oracle, even when supplemental routing makes the
    final selection non-collapsed (e.g. a selector-source PR also selects the
    Timescale invariant). Deliberately independent of the final-list shape so a
    supplemental target can never mask the provenance requirement.
    """
    if meta_guard_only:
        return True
    return any(path in ("scripts/select_ci_tests.py", SELECTOR_META_GUARD_TEST) for path in changed)


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
    meta_guard_only = list(tests) == [SELECTOR_META_GUARD_TEST]
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
