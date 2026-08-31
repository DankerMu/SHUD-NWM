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

# #1644: the published OpenAPI contract's assertion-level suites. `openapi/**`
# opens the backend gate via ci.yml's paths-filter and must reach real drift/type
# assertions, not the collect-only smoke; the runtime patch owner carries the
# drift suite as well as its existing API contract consumers.
# #1684 large-file guard repair: the 3.1-contract security half was physically
# partitioned into tests/test_slurm_gateway_openapi_security.py; every
# collectible partition replaces the single target.
OPENAPI_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/test_api_contract.py",
    "tests/test_openapi_31_contract.py",
    "tests/test_openapi_drift.py",
    "tests/test_slurm_gateway_openapi_security.py",
)

# #1646: the pytest warning-policy suite proves the SHIPPING config semantically
# (subprocess + removed-filter mutant + unrelated-warning control) and parses
# pyproject/uv.lock for the exact filter and the absence of a timeout
# dependency. Both the config file and the dependency lock select it (plus the
# selector meta-guard, which guards the selector's own rules), so a pyproject
# or lock change cannot ship without re-proving the policy.
THREAD_EXCEPTION_POLICY_TESTS: tuple[str, ...] = (
    "tests/test_pytest_thread_exception_policy.py",
)

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
STATE_CLONE_HOOK_TESTS: tuple[str, ...] = (
    "tests/test_state_clone_cutover_hook.py",
)
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
# Python path (the pin is a bare version file, shared.md a markdown instruction
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
# #1571 local-repair 1 (phase7-cand-01): the 997-line node-22 entrypoint owner
# uniquely asserts the exact-interpreter contracts of the two systemd units, the
# repair script's usage string, the QHH diagnostic README's Production
# Replacement lines, the shared instruction source's node-22 deferred-environment
# clause, and tests/conftest.py's skip-guidance pointer. The first five producers
# route through exact PATH_TEST_RULES (non-stop, additive); tests/conftest.py
# rides the SUPPORT_MODULE_TEST_RULES entry instead, because the `tests/**` branch
# handles conftest before PATH_TEST_RULES and a PATH row there would be dead.
NODE22_ENTRYPOINT_INVARIANT_TEST = "tests/test_node22_entrypoint_invariant.py"
NODE22_SLURM_GATEWAY_UNIT = "infra/systemd/nhms-slurm-gateway.service"
NODE22_RETENTION_UNIT = "infra/systemd/nhms-scheduler-evidence-retention.service"
NODE22_REPAIR_SCRIPT = "scripts/ops/node22_repair_placeholder_hydro_uris.py"

# #1860: the checked-in calibration declaration is a non-Python producer with no
# mechanically derivable import closure, so the route must be explicit and test
# its own continued existence. The three consumers are the package-manifest
# suite (owns `basins_calibration_overrides`' packaging contract), the
# scheduler-registry publisher suite (owns the declaration's default-load and
# exact-content oracles), and the selector meta-guard (holds the route pins).
# Exact set: no core-smoke fallback, no collect-only collapse.
CALIBRATION_OVERRIDES_PATH = "config/calibration_overrides.yaml"
CALIBRATION_OVERRIDES_CONSUMER_TESTS: tuple[str, ...] = (
    "tests/test_basins_package.py",
    "tests/test_publish_scheduler_file_registry.py",
    SELECTOR_META_GUARD_TEST,
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

# The literal rule rows are declared in PATH_TEST_RULES (after the dataclass);
# these constants are the single names the selector meta-suite pins.


@dataclass(frozen=True)
class PathTestRule:
    pattern: str
    tests: tuple[str, ...]
    stop_on_match: bool = False
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

DIRECT_GRID_E2E_TESTS: tuple[str, ...] = (
    "tests/test_direct_grid_e2e.py",
)

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
FORCED_RESUBMIT_SURFACE_TESTS: tuple[str, ...] = (
    "tests/test_forced_resubmit_veto.py",
)

SCHEDULER_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_publish_qdown.py",
    "tests/test_scheduler_backfill.py",
    "tests/test_scheduler_backfill_predecessor.py",
    "tests/test_scheduler_timing.py",
    "tests/test_source_scoped_dispatch.py",
)

ORCHESTRATOR_CLI_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_cleanup_frontier.py",
    "tests/test_cli_publish_qdown.py",
    "tests/test_orchestrator_demote_cli_security.py",
    "tests/test_retention_frontier.py",
    "tests/test_scheduler_backfill.py",
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
    # #1825: the node-22 manual-retry marker suite top-level-imports the journal
    # repository and pins the marker contract (per-run row vs cohort master) the
    # operator channel depends on. It runs in well under a second, so a rule is
    # the right disposition rather than a rule-gap exclusion.
    "tests/test_node22_manual_retry_failed_runs.py",
    "tests/test_orchestrator_demote_cli_security.py",
    "tests/test_orchestrator_demote_core_cas.py",
    "tests/test_orchestrator_demote_projection_faults.py",
    "tests/test_orchestrator_demote_reclaim_lifecycle.py",
    "tests/test_scheduler_backfill.py",
)

FILE_JOURNAL_READ_STATE_PATH_PATTERNS: tuple[str, ...] = (
    "packages/common/safe_fs.py",
    "services/orchestrator/chain_repository_state.py",
    "services/orchestrator/file_orchestration_journal.py",
    "services/orchestrator/file_orchestration_migration.py",
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
)


CHANGED_TEST_FILE_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        "tests/test_sql_shape_helpers.py",
        SQL_SHAPE_ORACLE_TESTS,
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
            NODE22_ENTRYPOINT_INVARIANT_TEST,
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
        "tests/river_identity_backfill_fakes.py",
        (
            "tests/test_node27_river_identity_backfill.py",
            "tests/test_node27_river_identity_backfill_receipt.py",
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
            "tests/test_scheduler_file_provider_refresh.py",
            "tests/test_scheduler_state_index_repair.py",
            "tests/test_state_manager.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_source_cycle_raw_manifest.py",
            "tests/test_publish_scheduler_file_registry.py",
        ),
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
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        "tests/cold_residency_fakes.py",
        (
            "tests/test_compressed_chunk_cold_runtime.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
            "tests/test_node27_cold_residency_publication.py",
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
        # The #1872 retention partition's shared constants/helpers. The four
        # collectible retention partitions import it at module scope (a design
        # requirement: selector importer derivation must see the dependency), so
        # a fixture edit breaks all four during PR-lane collection. They are the
        # derived importer set; the meta-guard rider covers the tree-derived
        # guards this very routing can invalidate.
        "tests/retention_test_helpers.py",
        (
            "tests/test_retention.py",
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
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
)


PATH_TEST_RULES: tuple[PathTestRule, ...] = (
    PathTestRule(
        ORCHESTRATOR_MANIFEST_SURFACE_PATH_PATTERNS[0],
        ORCHESTRATOR_MANIFEST_SURFACE_TESTS,
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
        # safe_fs.py owns tests/test_safe_fs.py, but the constant also serves the
        # seven journal patterns below, whose selection must not move. A separate
        # stop_on_match rule for safe_fs.py would instead SHIFT selection --
        # first match wins, so the journal closure would drop out entirely. The
        # result here is the union: today's journal closure PLUS the helper's own
        # suite, which a safe_fs-only change previously could not reach at all.
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[0],
        (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_safe_fs.py"),
        stop_on_match=True,
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
        FILE_JOURNAL_READ_STATE_PATH_PATTERNS[3],
        FILE_JOURNAL_READ_STATE_TESTS,
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
        # `edge-consumer` routings). test_state_clone_cutover_hook.py is the one
        # with no owning rule anywhere, so a base.py change would otherwise never
        # run it. Narrow on purpose — only a base.py PR pays for it.
        "workers/data_adapters/base.py",
        ("tests/test_state_clone_cutover_hook.py",),
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
            "tests/test_basins_package_publication.py",
            "tests/test_basins_registry_import.py",
            "tests/test_basins_reingest.py",
            "tests/test_direct_grid_variant_registration.py",
            "tests/test_hhe_mvt_binding.py",
            # #1813: this suite top-level-imports `basins_package` because it
            # owns the parity test binding the packager's forcing checksum
            # material to production-closure's reconstruction of it.  A change
            # to one implementation must run the test that pins both.
            "tests/test_production_object_store_validation.py",
            "tests/test_publish_scheduler_file_registry.py",
            "tests/test_qhh_production_bootstrap.py",
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
            "tests/test_production_scheduler.py",
            "tests/test_scheduler_backfill.py",
            "tests/test_warm_start_chaining.py",
            # #1850: the accepted-submit-identity binding-provenance and
            # claimant-exclusivity suites top-level-import
            # services/orchestrator/accepted_submit_identity.py and are
            # sub-second fixtures beside the gateway-reconcile lane they join.
            "tests/test_gateway_reconcile_binding_provenance.py",
            "tests/test_gateway_reconcile_claimant_exclusivity.py",
            "tests/test_cli_cleanup_frontier.py",
            "tests/test_cli_publish_qdown.py",
            "tests/test_orchestrator_demote_cli_security.py",
            "tests/test_orchestrator_demote_core_cas.py",
            "tests/test_orchestrator_demote_projection_faults.py",
            "tests/test_orchestrator_demote_reclaim_lifecycle.py",
            "tests/test_file_orchestration_journal.py",
            "tests/test_file_orchestration_journal_read_cache.py",
            "tests/test_file_orchestration_migration.py",
            "tests/test_live_monitoring.py",
            "tests/test_monitoring_api.py",
            "tests/test_pipeline_persistence.py",
            "tests/test_publish_scheduler_file_registry.py",
            "tests/test_reconcile_sacct_parse.py",
            "tests/test_replay_lineage.py",
            "tests/test_retention.py",
            # #1872: the retention corpus is physically partitioned; the
            # production owner rule must select every collectible partition so
            # a retention change never blinds targeted CI to moved cases.
            "tests/test_retention_extra_roots.py",
            "tests/test_retention_frontier.py",
            "tests/test_retention_pipeline_frontier.py",
            "tests/test_retention_root_admission.py",
            "tests/test_retry.py",
            "tests/test_retry_cancel_consistency.py",
            "tests/test_run_identity.py",
            "tests/test_run_tree_copyback.py",
            "tests/test_scheduler_backfill_predecessor.py",
            "tests/test_scheduler_file_provider_refresh.py",
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
            "tests/test_static_serving.py",
            "tests/test_cli_publish_qdown.py",
            # #1442: publisher.py (B) and forcing_copyback_backfill.py (C) are
            # both in the zero-text-identity oracle's register, and both ride
            # this directory rule. The four suites above assert behaviour, not
            # the SQL identity shape, so the oracle joins the directory list
            # rather than getting two per-file entries for the same targets.
            "tests/test_river_ts_text_identity_cleanup.py",
        ),
    ),
    PathTestRule(
        "services/tiles/mvt.py",
        (
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
            "tests/test_hydro_display_mvt_scaling.py",
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
        "apps/api/routes/hydro_display.py",
        (
            "tests/test_api_contract.py",
            "tests/test_direct_grid_display_cutover_flip.py",
            "tests/test_direct_grid_display_cutover_history.py",
            "tests/test_direct_grid_display_cutover_model_resolution.py",
            "tests/test_display_publish_status_only.py",
            "tests/test_hhe_mvt_binding.py",
            "tests/test_hydro_display_mvt_scaling.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_openapi_31_contract.py",
            # #1684 large-file guard repair: the 3.1-contract security half is
            # a one-hop importer via tests/test_openapi_31_contract.py.
            "tests/test_slurm_gateway_openapi_security.py",
            "tests/test_openapi_drift.py",
            "tests/test_river_ts_read_path_surrogate_keys.py",
            "tests/test_runtime_mode.py",
        ),
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
            "tests/test_production_slurm_validation.py",
            "tests/test_production_scale_validation.py",
            "tests/test_production_e2e_validation.py",
            "tests/test_production_met_validation.py",
            "tests/test_readonly_db_validation.py",
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
        "packages/common/forecast_store.py",
        (
            "tests/test_forecast_api.py",
            "tests/test_list_search_contract.py",
            "tests/test_migrations.py",
            "tests/test_model_registry_list_basins.py",
            "tests/test_qhh_latest_fallback_pushdown.py",
            # #1442: this file carries nine of the zero-text-identity oracle's
            # registered statements. None of the suites above assert the
            # pushdown-aid pairing or the statement census, so without this
            # entry a diff that reintroduces a text fact predicate here reaches
            # CI green unchallenged (same at-site reasoning as #1341's
            # mvt.py / display_coverage.py entries).
            "tests/test_river_ts_text_identity_cleanup.py",
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
            "tests/test_node27_external_contract_snapshot.py",
            "tests/test_node27_timeseries_compression_benchmark.py",
            "tests/test_node27_timeseries_compression_capture.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
            "tests/test_node27_timeseries_compression_supervisor.py",
            "tests/test_node27_timeseries_decompression_replay.py",
            "tests/test_node27_timeseries_lifecycle_lock.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_external_contract_snapshot.json",
        ("tests/test_node27_external_contract_snapshot.py",),
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
        "apps/api/openapi_patching.py",
        (
            "tests/test_api.py",
            "tests/test_api_contract.py",
            "tests/test_monitoring_api.py",
            "tests/test_openapi_31_contract.py",
            "tests/test_openapi_drift.py",
            "tests/test_slurm_gateway_openapi_security.py",
        ),
    ),
    PathTestRule(
        "apps/api/**",
        (
            "tests/test_api.py",
            "tests/test_api_contract.py",
            "tests/test_monitoring_api.py",
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
        ("tests/test_river_ts_text_identity_cleanup.py",),
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
    PathTestRule(
        "docs/runbooks/current-production-ops.md",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
    ),
    PathTestRule(
        "infra/env/compute.example",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
    ),
    PathTestRule(
        "infra/env/compute.scheduler-dbfree.env.example",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
    ),
    PathTestRule(
        "infra/env/README.md",
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST,),
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
        ("tests/test_readonly_db_validation.py",),
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
        # nothing about their SQL. One narrow rule each, one target each: the
        # cleanup oracle is the suite that pins these files' SQL shapes. The
        # wire-site invariant suite also scans them, but is not PR-selected for
        # scripts/** — see issue #1656.
        "scripts/summarize_qhh_smoke_results.py",
        ("tests/test_river_ts_text_identity_cleanup.py",),
    ),
    PathTestRule(
        "scripts/reset_qhh_smoke_db.py",
        ("tests/test_river_ts_text_identity_cleanup.py",),
    ),
    PathTestRule(
        # No same-name tests/test_node27_autopipeline.py exists, so without this
        # rule the autopipe script falls through to the core-smoke fallback and
        # none of its own suites run.
        "scripts/node27_autopipeline.py",
        (
            "tests/test_node27_autopipeline_preflight.py",
            "tests/test_node27_autopipeline_handoff.py",
            "tests/test_display_publish_status_only.py",
            # #1442/#1789: the publish criterion is a registered statement of
            # the zero-text-identity oracle (group D, no sanctioned aid at all),
            # and that oracle also censuses this file so a NEW fact-table
            # statement -- or the deleted ingest join coming back -- turns red.
            # It additionally pins the ingest criterion's authority-state gate.
            # Nothing above would notice any of it.
            "tests/test_river_ts_text_identity_cleanup.py",
        ),
    ),
    PathTestRule(
        # The cron wrapper is a shell script, not a backend python path, so the
        # core-smoke fallback never arms for it; without this rule a
        # wrapper-only PR selects nothing and CI degrades to --collect-only.
        "scripts/node27_autopipe_cron.sh",
        ("tests/test_node27_autopipeline_preflight.py",),
    ),
    # Shell wrappers with committed guard suites (#1138). Like the autopipe
    # cron rule above, none of these are backend python paths, so without an
    # explicit mapping a wrapper-only PR would select nothing; targets were
    # derived from `grep -rln '<script>.sh' tests/` and must track real
    # references. Wrappers with no guard suite intentionally have no rule here
    # and arm the core-smoke fallback via _is_backend_shell_path.
    PathTestRule(
        "scripts/scheduler_file_provider_refresh_once.sh",
        ("tests/test_scheduler_file_provider_refresh.py",),
    ),
    PathTestRule(
        "scripts/install_node22_scheduler_file_provider_refresh.sh",
        ("tests/test_scheduler_file_provider_refresh.py",),
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
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_compression.py",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_cold_residency.py",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
            "tests/test_node27_cold_residency_publication.py",
            "tests/test_node27_connection_attribution.py",
            "tests/test_node27_connection_attribution_delegated.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_cold_residency_once.sh",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-compression.service",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
        ),
    ),
    PathTestRule(
        "infra/systemd/nhms-node27-timeseries-retention.timer",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_retention.py",
        ),
    ),
    PathTestRule(
        "schemas/timeseries_compression_receipt.schema.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_compression_live_evidence.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_compression_receipt.example.json",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_sequential_budget.py",
        ),
    ),
    PathTestRule(
        "infra/env/node27-timeseries-compression.example",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
        ),
    ),
    PathTestRule(
        "infra/env/node27-cold-residency.example",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
        ),
    ),
    PathTestRule(
        "docs/runbooks/tier-node27-timeseries-storage.md",
        (
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
        ),
    ),
    PathTestRule(
        "schemas/timeseries_cold_residency_receipt.schema.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_cold_residency_receipt.example.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_cold_residency_receipt.noop.example.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_cold_residency_receipt.intent.example.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_cold_residency_receipt.partial.example.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "schemas/examples/timeseries_cold_residency_receipt.error.example.json",
        (
            "tests/test_timeseries_storage_schemas.py",
            "tests/test_node27_cold_residency.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_sequential_budget.py",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_runner_config.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "scripts/node27_timeseries_budget_preflight.py",
        (
            "tests/test_node27_timeseries_sequential_budget.py",
            "tests/test_node27_timeseries_sequential_wrappers.py",
            "tests/test_node27_wrapper_pythonpath.py",
        ),
    ),
    PathTestRule(
        "packages/common/node27_timeseries_lifecycle_lock.py",
        (
            "tests/test_node27_timeseries_lifecycle_lock.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_timeseries_compression.py",
            "tests/test_node27_timeseries_retention.py",
            "tests/test_node27_timeseries_decompression_replay.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_runtime.py",
        (
            "tests/test_compressed_chunk_cold_runtime.py",
            "tests/test_compressed_chunk_cold_runtime_proof.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_runtime_catalog.py",
        (
            "tests/test_compressed_chunk_cold_runtime.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_receipt.py",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_publication.py",
            "tests/test_node27_cold_residency_phase2.py",
            "tests/test_timeseries_storage_schemas.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_target.py",
        (
            "tests/test_compressed_chunk_cold_target.py",
            "tests/test_compressed_chunk_cold_runtime.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_tick.py",
        (
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
            "tests/test_node27_cold_residency_publication.py",
        ),
    ),
    PathTestRule(
        "packages/common/compressed_chunk_cold_runtime_timing.py",
        (
            "tests/test_compressed_chunk_cold_runtime.py",
            "tests/test_node27_cold_residency.py",
            "tests/test_node27_cold_residency_phase2.py",
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
        (PYTHON_ENVIRONMENT_TRUTH_TEST, NODE22_ENTRYPOINT_INVARIANT_TEST),
    ),
    PathTestRule(
        # #1571: the two-node Docker runbook is `infra/**`, which already opens
        # the backend lane; the rule converts that collect-only lane into real
        # assertions. Exact path, deliberately NOT a glob over infra markdown —
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
        ("tests/test_qhh_scripts_static.py", NODE22_ENTRYPOINT_INVARIANT_TEST),
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
        (SLURM_GATEWAY_DEPLOYMENT_CONTRACT_TEST, NODE22_ENTRYPOINT_INVARIANT_TEST),
    ),
    PathTestRule(
        # #1571: the retention unit's single exact ExecStart (deferred-venv
        # interpreter + absolute script) is uniquely asserted by the node-22
        # owner. Same collect-only conversion as the gateway unit.
        NODE22_RETENTION_UNIT,
        (NODE22_ENTRYPOINT_INVARIANT_TEST,),
    ),
    PathTestRule(
        # #1571: the repair script's usage string is uniquely asserted by the
        # node-22 owner. A rule suppresses the unknown-backend core-smoke
        # fallback (matched=True), so the script's CURRENT CORE_SMOKE selection
        # is preserved EXPLICITLY here — without these targets an exact rule
        # would silently drop them. scripts/** adds the #1656 timescale rider
        # supplementally. Owner joins additively, never replacing core smoke.
        NODE22_REPAIR_SCRIPT,
        (*CORE_SMOKE_TESTS, NODE22_ENTRYPOINT_INVARIANT_TEST),
    ),
    PathTestRule(
        "scripts/ops/node22-run-cycle-once.sh",
        ("tests/test_production_scheduler.py",),
    ),
    PathTestRule(
        "scripts/ops/start-display-api.sh",
        (
            "tests/test_two_node_docker_runtime.py",
            "tests/test_entropy_audit_script.py",
        ),
    ),
    PathTestRule(
        "scripts/governance/audit_repo_entropy.py",
        ("tests/test_entropy_audit_script.py",),
    ),
    PathTestRule(
        "scripts/governance/write_entropy_baseline.py",
        ("tests/test_entropy_audit_script.py",),
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
        # #1646: a pytest-config change must re-prove the thread-exception
        # policy (the file carries the exact filter and the no-timeout
        # decision) and still keep core smoke plus the selector meta-guard.
        "pyproject.toml",
        (*CORE_SMOKE_TESTS, *THREAD_EXCEPTION_POLICY_TESTS, SELECTOR_META_GUARD_TEST),
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
    PathTestRule(
        # #1892/#1900: probe-support modules have no same-name suite and are not
        # imported by the residency unit suite at module scope, so a support-only
        # PR previously selected core-smoke plus the #1656 rider and skipped the
        # focused probe contract. Ownership/cleanup lives in a sibling suite so a
        # support-only PR cannot skip the created-container marker. Additive: the
        # #1744 shared-library baseline remains outside this rule.
        "packages/common/compressed_chunk_cold_probe/**",
        (
            "tests/test_probe_compressed_chunk_cold_tablespace.py",
            "tests/test_probe_compressed_chunk_cold_tablespace_cleanup.py",
        ),
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
    return any(
        path in ("scripts/select_ci_tests.py", SELECTOR_META_GUARD_TEST) for path in changed
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
    collection_smoke_required = _collection_smoke_required(
        changed_paths, meta_guard_only=meta_guard_only
    )
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
