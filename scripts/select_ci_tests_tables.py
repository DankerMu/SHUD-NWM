"""Path-routing data tables of the CI test selector (`scripts/select_ci_tests.py`).

Every `*_TESTS` / `*_PATH` / `*_ROOTS` / `*_MODULES` constant the rule tables are
built from, the single-target constants those tuples reference, and the
`PathTestRule` row type. Moved out of the facade (source order kept; only comments
that pointed at rule rows "below" now name the rule table) by
entropy-burndown task 6.2 (#2949). This module never imports the facade; the
facade re-binds every name here, so `scripts.select_ci_tests.<NAME>` keeps
resolving and tests keep patching the facade binding.
"""

from __future__ import annotations

from dataclasses import dataclass

# The selector's own suite carries tracked-tree-derived meta-guards (same-name
# script pairs, container-contract closure, guarded-module importer closure). A
# PR that adds or moves a test file can invalidate any of them, so every changed
# test suite under `tests/` drags this suite along (~6s) instead of letting the
# guards go unrun on exactly the change class they exist for.
SELECTOR_META_GUARD_TEST = "tests/test_select_ci_tests.py"


CORE_SMOKE_TESTS: tuple[str, ...] = (
    "tests/test_api.py",
    "tests/test_gateway.py",
    "tests/test_migrations.py",
    "tests/test_orchestration_chain.py",
    "tests/test_production_scheduler.py",
)

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

# #2920: the guard's importer roots, mirroring its module-level `_SCAN_ROOTS`
# binding mapped to `<root>/**` globs; a selector meta-guard parses that binding
# and asserts the two sets are equal.
IMPORT_LAYERING_GUARD_ROOTS: tuple[str, ...] = (
    "packages/**",
    "workers/**",
)

# #2937: the ratchet's scan roots, mirroring its module-level `_SCAN_ROOTS`
# binding mapped to `<root>/**` globs; a selector meta-guard parses that binding
# and asserts the two sets are equal.
ANY_RATCHET_ROOTS: tuple[str, ...] = (
    "apps/**",
    "packages/**",
    "services/**",
    "workers/**",
    "scripts/**",
)
STATUS_VOCAB_PARITY_ROOTS: tuple[str, ...] = (
    "apps/**",
    "packages/**",
    "services/**",
    "workers/**",
    "scripts/**",
)

# entropy-burndown task 3.1: the ceiling's scan roots, mirroring its module-level
# `_SCAN_ROOTS` binding mapped to `<root>/**` globs; a selector meta-guard parses
# that binding and asserts the two sets are equal.
TIME_HELPER_CONSOLIDATION_ROOTS: tuple[str, ...] = (
    "apps/**",
    "packages/**",
    "services/**",
    "workers/**",
    "scripts/**",
)

# #2323: the scanner's input set, MIRRORED — the selector is stdlib-only and
# imports no repository module, and the roots are locals of
# `scripts/governance/entropy_audit/check_topology.py::_production_topology_scan_files`
# rather than constants the scanner exports (and the scanner is not edited). So
# tests/test_select_ci_tests.py pins the mirror BEHAVIOURALLY: the skip and
# extension constants equal `entropy_audit.constants`, a synthetic tree scans to
# exactly the set this mirror accepts, every path the scanner yields over the
# real tree is accepted here, and a dropped root reddens through `select_tests`.
#
# Deliberately NOT mirrored, because each can only OVER-select: the topology
# checker's own archive/receipt/`scripts/governance/**` exclusions, the
# MAX_SCANNED_TEXT_FILE_BYTES size cap and the symlink / regular-file checks
# (the last two need a stat of a path that may be a deletion — which changes
# the scan too, so no existence check is made either). Known limit: a scanner
# root added over a directory with no scannable file yet is invisible to the
# real-tree oracle until a file lands there; such a root scans nothing.
PRODUCTION_TOPOLOGY_SCAN_ROOTS: tuple[str, ...] = (
    "scripts",
    "infra/env",
    "instructions/agents",
    "docs/governance",
    "docs/runbooks",
    "openspec/changes",
    "openspec/specs",
)

# #2498: the forcing template-pair sweep's discovery set, MIRRORED from
# tests/forcing_ts_template_registry.py's DISCOVERY_ROOTS and
# _PRUNED_DIRECTORIES (pinned equal by tests/test_select_ci_tests.py; the
# selector imports no repository module). The per-path forcing riders and the
# register-derived wiring meta-test only cover paths that are ALREADY
# registered, so a brand-new file under these roots that constructs a
# ForcingTemplatePair escaped every forcing shape oracle until master. The
# supplemental import sniff in `select_tests` closes that: a changed `.py`
# under a root, with no pruned directory part, whose MODULE-LEVEL imports
# include `packages.common.forcing_ts_render` (the module a pair is built from)
# selects FORCING_SQL_SHAPE_ORACLE_TESTS. Routing every path under the six
# roots instead was rejected on cost by the #1990 cut (b) review (three suites,
# ~23s, on every backend path).
#
# Known limit — the sniff reuses `_top_level_imported_module_names`, which
# walks `tree.body` only on purpose (#1561), so it does NOT see a function-body
# import, an import nested under a module-level `if` / `try`, or attribute
# access through `from packages import common`. Those cases red on master, not
# in production: the registry sweep itself fails closed on a function-scope
# pair. The helper is not widened because its other consumers depend on the
# #1561 semantics — the same function-body blind spot #2390 records.
FORCING_TEMPLATE_DISCOVERY_ROOTS: tuple[str, ...] = ("packages", "workers", "scripts", "services", "apps", "db")
SQLALCHEMY_URL_HELPER_PATH = "packages/common/sqlalchemy_url.py"
SQLALCHEMY_DRIVER_GUARD_ROOTS: tuple[str, ...] = ("apps", "packages", "services", "workers", "scripts")

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
    # #2348: both oracle halves build samples from these doubles (the full-row
    # data-source/cycle/station fixtures and `_RetryGateway`).
    "tests/test_response_model_preservation.py",
    "tests/test_response_model_preservation_pipeline.py",
)

# #1611 partitioned tests/test_scheduler_state_index_copyback_replay.py (1378
# lines, 32 cases) into these four collectible suites plus one non-collectible
# helper. The monolith is GONE with no compatibility shim, so the same-name
# derivation from `scripts/scheduler_state_index_copyback_replay.py` stopped
# resolving — and a rule target that no longer exists is only a WARNING here, so
# the derivation could not simply be left to rot into an empty selection. Both
# routes therefore enumerate every partition explicitly: the owner route (PATH_TEST_RULES)
# for a change to the replay script, the support-module route (SUPPORT_MODULE_TEST_RULES) for a change to
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

# #2532 partitioned tests/test_node22_refresh_timer_health.py (3456 lines, 202
# cases) into these five collectible suites plus one non-collectible helper. The
# monolith is GONE with no compatibility shim, so the same-name derivation from
# `scripts/node22_refresh_timer_health.py` stopped resolving (that path now has an
# explicit row) and every rule site that named the monolith by hand was
# re-pointed. A rule target that no longer exists is only a WARNING here, so a
# forgotten site would have degraded the probe lane to an empty selection in
# silence.
#
# Two reach classes, because the exact-set pins in tests/test_select_ci_tests.py
# name readers, not the corpus:
#   * NODE22_REFRESH_TIMER_HEALTH_TESTS -- the whole corpus. Every route that
#     used to carry the monolith by membership (the probe itself, its installer
#     and units, the refresh runner and its package modules, the refresh
#     `.service`, the consumer-bound module) carries all five, so each such path
#     still selects exactly the 202 cases it selected before the split.
#   * the two reader tuples below -- the partitions that literally read the env
#     template / the production-ops probe section, which ride exact-set rows.
# tests/test_select_ci_tests.py closes the corpus against the tracked tree
# (exactly five suites + one helper) and derives both reader tuples from the
# partitions' own source.
NODE22_REFRESH_TIMER_HEALTH_OWNER_PATH = "scripts/node22_refresh_timer_health.py"
NODE22_REFRESH_TIMER_HEALTH_HELPERS_PATH = "tests/node22_refresh_timer_health_helpers.py"
NODE22_REFRESH_TIMER_HEALTH_TESTS: tuple[str, ...] = (
    "tests/test_node22_refresh_timer_health_history.py",
    "tests/test_node22_refresh_timer_health_installer.py",
    "tests/test_node22_refresh_timer_health_runbook_and_env.py",
    "tests/test_node22_refresh_timer_health_thresholds_and_receipt.py",
    "tests/test_node22_refresh_timer_health_verdicts.py",
)
# `test_the_production_path_defaults_match_every_file_that_states_them` is the
# only case that `read_text`s infra/env/compute.scheduler-provider-refresh.env.example.
NODE22_REFRESH_TIMER_HEALTH_ENV_TEMPLATE_TESTS: tuple[str, ...] = (
    "tests/test_node22_refresh_timer_health_history.py",
)
# The partitions that read the production-ops probe section through the helper's
# `RUNBOOK` / `_probe_runbook_section`.
NODE22_REFRESH_TIMER_HEALTH_RUNBOOK_TESTS: tuple[str, ...] = (
    "tests/test_node22_refresh_timer_health_history.py",
    "tests/test_node22_refresh_timer_health_installer.py",
    "tests/test_node22_refresh_timer_health_runbook_and_env.py",
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
# #2294 added three suites and one helper to the corpus: the installer's
# failure-path and mutation suites, which run the installer against the fake
# `systemctl` of tests/scheduler_refresh_installer_harness.py, and the #2297
# restored-receipt-truth suite. The two installer suites ride the installer's
# own row (SCHEDULER_REFRESH_INSTALLER_TESTS below), not the deployment tuple:
# neither opens the units' contract, the env template or the runner wrapper.
#
# tests/test_select_ci_tests.py closes the corpus against the tracked tree
# (exactly eighteen suites + three helpers), so a nineteenth partition, a
# leftover shim or a helper renamed into a `test_*.py` suite reddens instead of
# falling out of the PR lane.
SCHEDULER_REFRESH_HELPERS_PATH = "tests/scheduler_refresh_helpers.py"
SCHEDULER_REFRESH_RECEIPT_HELPERS_PATH = "tests/scheduler_refresh_receipt_helpers.py"
SCHEDULER_REFRESH_INSTALLER_HARNESS_PATH = "tests/scheduler_refresh_installer_harness.py"
SCHEDULER_REFRESH_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_barrier_seam.py",
    "tests/test_scheduler_refresh_catalog_derivation.py",
    "tests/test_scheduler_refresh_classification_modes.py",
    "tests/test_scheduler_refresh_cutover_gate.py",
    "tests/test_scheduler_refresh_cutover_gate_audit.py",
    "tests/test_scheduler_refresh_cutover_round2.py",
    "tests/test_scheduler_refresh_deployment_contract.py",
    "tests/test_scheduler_refresh_emergency_receipts.py",
    "tests/test_scheduler_refresh_installer_failure_paths.py",
    "tests/test_scheduler_refresh_installer_mutations.py",
    "tests/test_scheduler_refresh_predicates_and_dry_run_reconciliation.py",
    "tests/test_scheduler_refresh_provider_atomic.py",
    "tests/test_scheduler_refresh_receipt_block_presence.py",
    "tests/test_scheduler_refresh_restored_receipt_truth.py",
    "tests/test_scheduler_refresh_retirement_declaration.py",
    "tests/test_scheduler_refresh_retirement_reconciliation.py",
    "tests/test_scheduler_refresh_terminability_probes.py",
    "tests/test_scheduler_refresh_worker_mirror_transactions.py",
)
SCHEDULER_REFRESH_DEPLOYMENT_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_deployment_contract.py",
)
# The two suites that import the installer harness -- its derived importer
# closure, and the harness row's targets.
SCHEDULER_REFRESH_INSTALLER_HARNESS_TESTS: tuple[str, ...] = (
    "tests/test_scheduler_refresh_installer_failure_paths.py",
    "tests/test_scheduler_refresh_installer_mutations.py",
)
# Every suite that runs the refresh installer: the deployment-contract lifecycle
# case plus the two harness suites. Only the installer's own row carries it.
SCHEDULER_REFRESH_INSTALLER_TESTS: tuple[str, ...] = (
    *SCHEDULER_REFRESH_DEPLOYMENT_TESTS,
    *SCHEDULER_REFRESH_INSTALLER_HARNESS_TESTS,
)
# The whole reach of the refresh RUNNER: the fifteen partitions plus the node-22
# probe suite, which both imports the runner and reads its source for the
# history-receipt filename shape. Named once because #1099 gave it eleven
# carriers (the facade and the ten package modules) instead of one. #2532: the
# probe suite is five partitions now, all carried.
SCHEDULER_REFRESH_RUNNER_TESTS: tuple[str, ...] = (
    *SCHEDULER_REFRESH_TESTS,
    *NODE22_REFRESH_TIMER_HEALTH_TESTS,
)
# #1099 split the 3639-line runner into this package; the historical path stayed
# an executable entrypoint and a re-export facade, so it keeps its own row in PATH_TEST_RULES
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
    "tests/test_scheduler_refresh_restored_receipt_truth.py",
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
# row in PATH_TEST_RULES is the sixth.
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
# #2737: the provision script and the registration module's read-only planning
# function are exercised end to end by one suite whose name neither path
# derives; the script also owns the IC-header gate suite that imports it. The
# receipt module is same-subject with the script (its only caller).
# #2753: the output-registry destination checks sit in a second file that
# imports the first one's workspace fixture.
PROVISION_DIRECT_GRID_DRY_RUN_TEST = "tests/test_provision_direct_grid_dry_run_and_receipt.py"
PROVISION_OUTPUT_REGISTRY_PARENT_TEST = "tests/test_provision_output_registry_parent.py"
PROVISION_DIRECT_GRID_SCRIPT_TESTS: tuple[str, ...] = (
    PROVISION_DIRECT_GRID_DRY_RUN_TEST,
    PROVISION_OUTPUT_REGISTRY_PARENT_TEST,
    "tests/test_provision_direct_grid_ic_header_gate.py",
)
# #2738: the merged-registry publish tool is an entry script (argparse, `main`,
# the public names) over the four modules of `scripts/merged_registry_publish/`,
# and its suite is three partitions sharing one tests/ support module. No path
# derives a partition name, so every route (PATH_TEST_RULES, SUPPORT_MODULE_TEST_RULES) is explicit. Each module gets
# its own row rather than a `scripts/merged_registry_publish/**` glob, for the
# reason recorded at PUBLISH_REGISTRY_PACKAGE_MODULES; every module and every
# shared helper carries all three partitions, because each partition drives
# `publish_merged_scheduler_registry` or `main` through all of them.
MERGED_REGISTRY_PUBLISH_OWNER_PATH = "scripts/node22_publish_merged_scheduler_registry.py"
MERGED_REGISTRY_PUBLISH_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/merged_registry_publish/apply.py",
    "scripts/merged_registry_publish/model.py",
    "scripts/merged_registry_publish/planning.py",
    "scripts/merged_registry_publish/receipts.py",
)
MERGED_REGISTRY_PUBLISH_HELPERS_PATH = "tests/merged_registry_publish_helpers.py"
MERGED_REGISTRY_PUBLISH_TESTS: tuple[str, ...] = (
    "tests/test_node22_publish_merged_scheduler_registry_apply_and_restore.py",
    "tests/test_node22_publish_merged_scheduler_registry_cli_and_db_free.py",
    "tests/test_node22_publish_merged_scheduler_registry_plan_and_checks.py",
)
SUCCESSION_RECEIPT_TESTS: tuple[str, ...] = (
    PROVISION_DIRECT_GRID_DRY_RUN_TEST,
    *MERGED_REGISTRY_PUBLISH_TESTS,
)
# #2739: the model succession tool is an entry script (argparse, `main`) over
# the seven modules of `scripts/model_succession/`, and its suite is six
# partitions sharing one tests/ support module (#2740 added the cold-start
# partition, #2756 the add-basin one, #2757 the remove-basin one). No path
# derives a partition name, so every route is explicit, one row per module, for
# the reason recorded at PUBLISH_REGISTRY_PACKAGE_MODULES. Every partition
# drives `main` or `run_step` through all seven modules, so each module carries
# all six.
MODEL_SUCCESSION_OWNER_PATH = "scripts/node22_model_succession.py"
MODEL_SUCCESSION_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/model_succession/copyback.py",
    "scripts/model_succession/model.py",
    "scripts/model_succession/plan.py",
    "scripts/model_succession/run.py",
    "scripts/model_succession/scheduler.py",
    "scripts/model_succession/systemd.py",
    "scripts/model_succession/tools.py",
)
MODEL_SUCCESSION_HELPERS_PATH = "tests/model_succession_helpers.py"
MODEL_SUCCESSION_TESTS: tuple[str, ...] = (
    "tests/test_node22_model_succession_add_basin.py",
    "tests/test_node22_model_succession_apply_and_resume.py",
    "tests/test_node22_model_succession_cold_start.py",
    "tests/test_node22_model_succession_copyback_and_dry_run.py",
    "tests/test_node22_model_succession_remove_basin.py",
    "tests/test_node22_model_succession_timer_and_refresh.py",
)
# #2757: the node-27 basin retirement tool is an entry script (argparse, `main`)
# over the six modules of `scripts/basin_retirement/`, and its suite is five
# partitions sharing one tests/ support module. No path derives a partition
# name, so every route is explicit, one row per module, as for the model
# succession tool above. Every partition drives `main` (or `run_step`) through
# all six modules, so each module carries all five.
BASIN_RETIREMENT_OWNER_PATH = "scripts/node27_retire_basin.py"
BASIN_RETIREMENT_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/basin_retirement/autopipe.py",
    "scripts/basin_retirement/database.py",
    "scripts/basin_retirement/envfile.py",
    "scripts/basin_retirement/model.py",
    "scripts/basin_retirement/run.py",
    "scripts/basin_retirement/steps.py",
)
BASIN_RETIREMENT_HELPERS_PATH = "tests/node27_retire_basin_helpers.py"
BASIN_RETIREMENT_TESTS: tuple[str, ...] = (
    "tests/test_node27_retire_basin_apply.py",
    "tests/test_node27_retire_basin_dry_run.py",
    "tests/test_node27_retire_basin_exclude_and_wait.py",
    "tests/test_node27_retire_basin_refusals.py",
    "tests/test_node27_retire_basin_steps.py",
)
# #2739: the succession tool calls the clone tool and the merged-registry
# publish tool in-process, real code against fake packages, and decides from
# their receipts and errors; a change to either tool must run its partitions.
MERGED_REGISTRY_PUBLISH_TOOL_TESTS: tuple[str, ...] = (
    *MERGED_REGISTRY_PUBLISH_TESTS,
    *MODEL_SUCCESSION_TESTS,
)
# The CLI environment helpers shared by BOTH recalibration CLI modules. A change
# to this support module must run both consumers; its suite names are not
# same-name derivable (no tests/state_clone_recalibration_cli_fixtures.py), so
# the route is explicit -- consistent with the shared-fixtures support rule
# in SUPPORT_MODULE_TEST_RULES.
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
JOURNAL_RETENTION_TESTS = (
    "tests/test_scheduler_journal_retention_planning.py",
    "tests/test_scheduler_journal_retention_archive.py",
)

# #1102: the scheduler-registry publisher corpus is seven collectible partitions
# plus one non-collectible helper. The deleted 3218-line monolith was the ONLY
# same-name suite of `scripts/publish_scheduler_file_registry.py`, so that
# derivation died with it and every route (PATH_TEST_RULES, SUPPORT_MODULE_TEST_RULES) has to name the partitions.
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


# #1823: the entropy-audit corpus is sixteen collectible partitions plus one
# non-collectible helper. The deleted 9860-line monolith was the ONLY target of
# the three governance rules in PATH_TEST_RULES and it had no same-name source pair to fall
# back on, so every route has to name the partitions. Explicit sorted tuple,
# never a `tests/test_entropy_audit_*.py` glob resolved at import time (same
# reason as QHH_PRODUCTION_BOOTSTRAP_TESTS / PUBLISH_SCHEDULER_REGISTRY_TESTS):
# a glob silently adopts a seventeenth partition nobody reviewed, while an
# unlisted partition reddens the tracked-tree guard in
# tests/test_select_ci_tests.py instead of dropping out of the PR lane.
ENTROPY_AUDIT_TESTS: tuple[str, ...] = (
    "tests/test_entropy_audit_baseline_writer_safety.py",
    "tests/test_entropy_audit_baseline_writer_summary.py",
    "tests/test_entropy_audit_facade_guard_aliases.py",
    "tests/test_entropy_audit_facade_guard_forwarders.py",
    "tests/test_entropy_audit_instruction_inventory.py",
    # #2648: the `hard-line-reference` family and its frozen baseline.
    "tests/test_entropy_audit_line_references.py",
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
# helper tail, so ALL sixteen partitions import it at module scope and the
# routed set IS the derived importer closure. Not collectible: the filename is
# deliberately not `test_*`, so `is_test_suite_path` rejects it and it reaches
# SUPPORT_MODULE_TEST_RULES.
ENTROPY_AUDIT_HELPERS_PATH = "tests/entropy_audit_helpers.py"

# #3042: the compatibility facade guard's inputs, MIRRORED from
# `COMPATIBILITY_FACADE_CONFIGS` in scripts/governance/entropy_audit/schema.py
# (each governed facade's `relative_path` and `inventory_path`; the selector
# imports no repository module, and tests/test_select_ci_tests.py pins the two
# equal, naming any unwired path). The guard's two "current repo" assertions
# build a full-repo report at run time instead of importing the facades, so no
# import edge reaches them: PR #3041 grew `scheduler.py`, passed the PR lane and
# reddened the full run. `select_tests` adds the two NODES as a supplemental
# rider in the #2323 shape (set union only; skipped per node when its whole
# partition is already selected; disregarded by `meta_guard_only`). Known limit:
# `docs/**` does not open the ci.yml backend lane, so an inventory-only PR runs
# them on master, not in the PR lane.
COMPATIBILITY_FACADE_GUARD_INPUT_PATHS: tuple[str, ...] = (
    "services/orchestrator/scheduler.py",
    "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
    "services/orchestrator/chain.py",
    "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
)
COMPATIBILITY_FACADE_GUARD_CURRENT_REPO_TESTS: tuple[str, ...] = (
    "tests/test_entropy_audit_baseline_writer_summary.py"
    "::test_entropy_baseline_writer_preserves_v1_trend_semantics_for_current_repo",
    "tests/test_entropy_audit_report_contract.py::test_compatibility_facade_guard_current_repo_passes_with_inventories",
)

# #1842 split the 9000-line audit enforcer into this package; the historical
# path stayed the console entrypoint (it owns the `__main__` guard) and an
# attribute-broadcast facade, so it keeps its own row in PATH_TEST_RULES and is NOT a member
# of this tuple. Each module gets an explicit row rather than a
# `scripts/governance/entropy_audit/**` glob, for the reason #1099 recorded for
# `scripts/scheduler_refresh/` and #1100 for `scripts/publish_registry/`: a glob
# routes a twenty-third module nobody reviewed, while an unlisted module
# reddens the tracked-tree guard in tests/test_select_ci_tests.py instead of
# silently dropping out of the PR lane. None of these basenames has a
# `tests/test_<basename>.py`, so there is no same-name derivation to fall back
# on -- measured before the rows landed, each of the twenty-one selected only
# the five generic core-smoke riders plus the two `scripts/**` supplemental-scan
# suites and ZERO entropy partitions. Every module carries the whole
# sixteen-partition corpus for the same reason the owner row does: every
# partition drives `build_report` or the CLI through all of them.
ENTROPY_AUDIT_OWNER_PATH = "scripts/governance/audit_repo_entropy.py"
ENTROPY_AUDIT_PACKAGE_MODULES: tuple[str, ...] = (
    "scripts/governance/entropy_audit/archive_status.py",
    "scripts/governance/entropy_audit/check_env_and_tokens.py",
    # #2648: the twenty-second module, the `hard-line-reference` family.
    "scripts/governance/entropy_audit/check_line_references.py",
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
# attribute-broadcast facade, so it keeps its own row in PATH_TEST_RULES and is NOT a member
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
# The tracked writer's CLI suite joins the data route: a memory diff also
# re-proves that the committed file round-trips through its only writer.
REVIEW_GATE_CLI_PATH = "scripts/review_gate.py"
REVIEW_GATE_CLI_TEST = "tests/test_review_gate_cli.py"
REVIEW_GATE_ISSUE_MEMORY_CONSUMER_TESTS: tuple[str, ...] = (
    REVIEW_GATE_ISSUE_MEMORY_TEST,
    REVIEW_GATE_CLI_TEST,
    SELECTOR_META_GUARD_TEST,
)

# #2477: the tracked loop-log attribution audit and the frozen ledger it reads.
# The suite pins the audit's arithmetic and a real-log guard over the ledger,
# so both paths route to it explicitly (a `.jsonl` under `docs/` has no import
# closure). The log rule carries the meta-guard for the same reason as the
# review-gate memory: it is a non-`tests/` data path that holds route pins.
LOOP_LOG_AUDIT_PATH = "scripts/governance/loop_log_audit.py"
LOOP_LOG_PATH = "docs/review-loop-log.jsonl"

# #2648: the frozen `hard-line-reference` baseline is a JSON data file with no
# import closure; its only assertion-level consumer is the family's own
# partition (cap and fixed-group pins), so the route is explicit.
LINE_REFERENCE_BASELINE_PATH = "scripts/governance/entropy_audit/line_reference_baseline.json"

# #2044 / #2710: the master full-regression watcher. Its suite reads ci.yml's
# top-level name, the `unit-test` job name, its shard matrix (the watcher derives
# the shard job names `Unit Tests (full) (<shard>)` from it) and the per-shard
# timeout-minutes, so a ci.yml change re-proves the wiring too.
FULL_REGRESSION_WATCH_WORKFLOW_PATH = ".github/workflows/full-regression-watch.yml"
FULL_REGRESSION_WATCH_SCRIPT_PATH = "scripts/ci/full_regression_watch.py"

# #2710: the full regression runs as shards. The partition script, its checked-in
# duration table (a JSON data file with no import closure) and the ci.yml matrix
# wiring are proven by one suite, which also asserts on the real tree that the
# shards are disjoint and cover every collected test file.
FULL_REGRESSION_SHARD_SCRIPT_PATH = "scripts/ci/shard_tests.py"
FULL_REGRESSION_SHARD_DURATIONS_PATH = "scripts/ci/full_test_durations.json"

# #2709: the weekly floating-resolve report lane (schedule + manual dispatch
# only, never a merge gate). Its workflow and its report script share one suite.
FLOATING_RESOLVE_WORKFLOW_PATH = ".github/workflows/floating-resolve-report.yml"
FLOATING_RESOLVE_SCRIPT_PATH = "scripts/ci/floating_resolve_report.py"

# #2602: the governance workflow's separate hard-gate job and the untouched
# report-only job are pinned by this meta-test.
GOVERNANCE_WORKFLOW_PATH = ".github/workflows/governance.yml"

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
    *NODE22_REFRESH_TIMER_HEALTH_RUNBOOK_TESTS,
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

# Non-gated top-level importers of the direct-grid contract
# (packages/common/contracts/direct_grid.py; workers/forcing_producer/
# direct_grid_contract.py until entropy-burndown 1.3) that the focused
# DIRECT_GRID_SURFACE_TESTS node ids never reach (#1455). The contract module is
# owned by a stop_on_match rule, so no broader rule is reachable for it and these
# have to ride the stop rule itself. Kept as a separate tuple appended at the rule
# site so DIRECT_GRID_SURFACE_TESTS keeps meaning "the compact e2e fixture" for
# every other reader.
DIRECT_GRID_CONTRACT_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_direct_grid_variant_registration.py",
    "tests/test_legacy_reactivation_guard.py",
    "tests/test_mapping_builder_binding.py",
    "tests/test_mapping_builder_cli.py",
    "tests/test_mapping_builder_integration.py",
)

# entropy-burndown 1.2 (#2921): the direct-grid contract's canonical home. 1.3
# deleted the workers/forcing_producer/direct_grid_contract.py shim together
# with its rule and its DIRECT_GRID_SURFACE_PATH_PATTERNS entry.
DIRECT_GRID_CONTRACT_CANONICAL_PATH = "packages/common/contracts/direct_grid.py"

# The `workers/data_adapters/**` and `workers/data_adapters/base.py` rule
# targets, extracted (#2922) so the time-normalization contract row in PATH_TEST_RULES
# shares them instead of copying them.
DATA_ADAPTERS_DIRECTORY_TESTS: tuple[str, ...] = (
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
)

DATA_ADAPTERS_BASE_HELPER_TESTS: tuple[str, ...] = (
    "tests/test_state_clone_cutover_hook.py",
    "tests/test_scheduler_journal_retention_archive.py",
    "tests/test_pipeline_job_provenance_importer.py",
    "tests/test_pipeline_job_provenance_publisher.py",
    "tests/test_pipeline_ops_identity_envelope.py",
)

# entropy-burndown 1.2 (#2922): the time-normalization contract's canonical
# home. 1.3 deleted the services/orchestrator/time_consistency.py shim and
# narrowed base.py to its own classes; base.py's rules stay.
TIME_NORMALIZATION_CONTRACT_PATH = "packages/common/contracts/time_normalization.py"

# entropy-burndown 1.2 (#2964): the region contract's canonical home. 1.3
# deleted the workers/data_adapters/region.py shim.
REGION_CONTRACT_PATH = "packages/common/contracts/region.py"

# entropy-burndown 1.2 (#2923): the hydrologic-core fingerprint contract's
# canonical home. 1.3 narrowed workers/mapping_builder/rewrite.py to its own
# names; the `workers/mapping_builder/**` row keeps owning rewrite.py.
HYDROLOGIC_FINGERPRINT_CONTRACT_PATH = "packages/common/contracts/hydrologic_fingerprint.py"

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
# to ONE rule in PATH_TEST_RULES with `(*SHARED_TESTS, *THIS)`; the shared constants
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
    # #2584: the ambiguous state_save_qc submit suite top-level-imports the
    # chain facade (ForecastOrchestrator, HttpSlurmGatewayClient) and drives the
    # real stage loop through it. Stop-rule owned module, so the addition rides
    # this at-site tuple. DB-free, sub-second.
    "tests/test_state_save_submit_ambiguity.py",
    # #2570 A: the poll-isolation suite top-level-imports the chain facade's
    # OrchestratorError and drives the real stage loop through it. Stop-rule
    # owned module, so the addition rides this at-site tuple. DB-free, ~4s.
    "tests/test_chain_stage_poll_isolation.py",
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
    # itself a test file (the changed-test importer-closure branch of `select_tests`), and a production module
    # path is routed by PATH_TEST_RULES alone.
    "tests/test_operator_action_listing.py",
    # #2401: the newest-truth terminal-skip suite top-level-imports
    # `services.orchestrator.scheduler`, drives full `ProductionScheduler`
    # passes through it and observes the decision at the facade's
    # `_candidate_state_decision` seam, so a facade or seam edit must run it.
    # DB-free, 21 tests in ~10s: a rule, not a rule-gap exclusion.
    "tests/test_scheduler_terminal_recency.py",
    # #2453: the root-check loop-convergence suite top-level-imports
    # `services.orchestrator.scheduler` and drives `_scheduler_root_check` and both
    # root preflights through the facade (the check itself calls back into the
    # facade for every helper), plus a real `ProductionSchedulerConfig`. A facade
    # edit must run it. DB-free, 51 tests in ~0.4s.
    "tests/test_scheduler_root_check_loop_convergence.py",
    "tests/test_scheduler_timing.py",
    "tests/test_source_scoped_dispatch.py",
    # #2603: the cohort-membership attribution suite top-level-imports
    # `services.orchestrator.scheduler`, takes its cohort run ids from the
    # facade's `_candidate_execution_cohort_run_id` and reads every decision at
    # its `_candidate_state_decision` seam. DB-free, 15 tests in ~6s.
    "tests/test_cohort_membership_attribution.py",
    # #2600: the manual-retry restart-point suite top-level-imports
    # `services.orchestrator.scheduler` for the facade's restart-stage reader and
    # cohort run ids, and reads every decision at its `_candidate_state_decision`
    # seam. DB-free, ~5s.
    "tests/test_manual_retry_failed_stage_restart.py",
    # #2546: the convert-members suite top-level-imports
    # `services.orchestrator.scheduler` for the facade's execution-unit run ids
    # and reads decisions at its `_candidate_state_decision` seam. DB-free, ~3s.
    "tests/test_convert_cohort_members.py",
    # #2666: the held-reservation block suite top-level-imports
    # `services.orchestrator.scheduler`, takes candidate and cohort run ids from the
    # facade and drives `_candidate_state_decision` plus the planner's
    # `_build_candidates` / `_execute_candidates`. DB-free, 19 tests in ~7s.
    "tests/test_scheduler_held_reservation_block.py",
    # #2668: the bind-reserved-job lane suite top-level-imports
    # `services.orchestrator.scheduler` and judges the bound cohort at the facade's
    # `_candidate_state_decision` and `_cycle_completion_status` seams. DB-free,
    # 3 tests in ~4s.
    "tests/test_orchestrator_bind_reserved_job_lane.py",
)

ORCHESTRATOR_CLI_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_cli_cleanup_frontier.py",
    "tests/test_cli_publish_qdown.py",
    "tests/test_orchestrator_demote_cli_security.py",
    "tests/test_retention_frontier.py",
    # #2596: drives `cleanup` through both entrypoints for an unexpandable
    # `OBJECT_STORE_ROOT` (structured result, exit 0, no traceback). ~0.3s.
    "tests/test_retention_unexpandable_roots.py",
    "tests/test_scheduler_backfill.py",
    # #1943/#1944: both new journal suites top-level-import `cli` and drive it
    # through BOTH entrypoints (`_click_main`/`_argparse_main`) — the root
    # authority suite for the refusal's operator-facing exit, the census suite
    # for the read-only `census_job_id_scope` command registration. A register
    # order or exit-code change in cli.py must run them; measured together they
    # are 50 tests in 1.50s.
    "tests/test_scheduler_journal_root_authority.py",
    "tests/test_scheduler_journal_scope_census.py",
    # #2668: `bind-reserved-job` is registered in cli.py for both entrypoints;
    # the CLI suite drives `_click_main`/`_argparse_main`, the lane suite binds
    # through `cli.main`, and the held-listing suite drives
    # `list-operator-actions` through both legs. DB-free, ~80 tests in ~8s.
    "tests/test_orchestrator_bind_reserved_job_cli.py",
    "tests/test_orchestrator_bind_reserved_job_lane.py",
    "tests/test_operator_action_listing_held_reservations.py",
    # #2675: the forcing-lane bind suites top-level-import `cli`; the CAS suite
    # drives `_click_main`/`_argparse_main` for its forcing CLI cases and the
    # lane suite binds through `cli.main`. DB-free, together ~70 tests in ~6s.
    "tests/test_orchestrator_bind_reserved_job_forcing.py",
    "tests/test_orchestrator_bind_reserved_job_forcing_lane.py",
    # #2682: the forcing absence-exit suite top-level-imports `cli` and drives
    # `demote-reserved-job` through both entrypoints. DB-free, ~57 tests in ~3s.
    "tests/test_orchestrator_demote_reserved_job_forcing.py",
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
    # #2584: the ambiguous state_save_qc submit suite reads the row, its
    # submission event and the retry decision through this repository and
    # `FileJournalRetryService`, and drives the in-stage resubmit on it. Stop-rule
    # owned module, so the addition rides this at-site tuple. DB-free, sub-second.
    "tests/test_state_save_submit_ambiguity.py",
    # #2570 A / #2603 / #2559: the poll-isolation, cohort-membership and
    # forecast-projection suites drive the real stage loop against this
    # repository and read rows, candidate states and cohort projections back
    # through it (the membership classification lives in `candidate_state`).
    # Stop-rule owned module, so the additions ride this at-site tuple.
    # DB-free, together ~12s.
    "tests/test_chain_stage_poll_isolation.py",
    "tests/test_cohort_membership_attribution.py",
    "tests/test_forecast_cohort_projection_restart_stage.py",
    # #2666: the held-reservation block suite writes the journal through the real
    # stage loop and this repository's reserve / accepted-submit / release writers
    # and reads every decision back through `candidate_state` (the E13b-stripped
    # held master is only visible there). DB-free, ~7s.
    "tests/test_scheduler_held_reservation_block.py",
    # #2668: the bind CAS suite's subject is this module's
    # `bind_operator_verified_reserved_job` and the shared locked commit body it
    # reuses; the lane suite drives the real journal through the stage loop,
    # reconcile, the bind and inflight projection. DB-free, ~43 tests in ~6s.
    "tests/test_orchestrator_bind_reserved_job_cas.py",
    "tests/test_orchestrator_bind_reserved_job_lane.py",
    # #2675: the forcing bind suites' subject is this module's forcing branch of
    # `bind_operator_verified_reserved_job` (its own locked writer and claimant
    # scan); the lane suite drives the real journal through the stage loop,
    # restart reconcile, the bind and inflight projection. DB-free, ~6s.
    "tests/test_orchestrator_bind_reserved_job_forcing.py",
    "tests/test_orchestrator_bind_reserved_job_forcing_lane.py",
    # #2682: the forcing absence-exit suite's subject is this module's forcing
    # branch of `demote_operator_verified_reserved_job` (CAS, named refusals,
    # the row + audit-event append). DB-free, ~3s.
    "tests/test_orchestrator_demote_reserved_job_forcing.py",
    # #2674: the shape (c) writer-guard suite's subject is this module's
    # `_write_pipeline_job_unlocked` entry refusal and its record-level append
    # funnels, driven through every public writer. DB-free, ~46 tests in ~1s.
    "tests/test_file_journal_legacy_unversioned_reserved_guard.py",
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
    # #2542 / #2546 / #2557: the per-member cohort charge suite (this module's
    # reconcile write, closed constructor, reclaim and manual-retry clone), the
    # convert-members suite (`has_active_pipeline` and `candidate_state`) and the
    # hydro-attempt triage suite (the three accepted-submit release entrypoints)
    # all drive this repository.  The charge and triage suites import
    # function-locally; the convert suite top-level-imports it.  DB-free, ~6s.
    "tests/test_cohort_member_charge.py",
    "tests/test_convert_cohort_members.py",
    "tests/test_hydro_run_attempt_triage.py",
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
    # #2566/#2567: the runtime-root walk's blocked-read accounting and
    # environment exclusion live in this module (`_file_retry_runtime_root_candidates`,
    # `_resolve_file_retry_runtime_roots`); the suite imports the service
    # function-locally, so this stop-rule site is its journal route. #2600: the
    # manual-retry restart-point suite top-level-imports this repository and
    # `FileJournalRetryService` (the marker writer). DB-free, together ~6s.
    "tests/test_retry_runtime_root_blocked_reads.py",
    "tests/test_manual_retry_failed_stage_restart.py",
    # #2306: the file lane's manual-retry root evidence is rendered ONCE, in
    # this module -- `_manual_retry_submission_failure_details` and
    # `_record_manual_retry_submission_success` -- and its oracles outside the
    # journal's own same-name suite are these six tests in tests/test_retry.py,
    # whose bare name appears solely on the broad `services/orchestrator/**`
    # rule that this stop rule shadows. The suite has no top-level journal
    # import either, so no importer derivation reaches it: this at-site tuple is
    # the journal's only route to them.
    # Not all six discriminate the render.
    # `..._db_free_runtime_evidence_values_survive_the_single_render` stays
    # GREEN under a pure revert of the #2306 writer-side pre-render -- its
    # values are `[local-path]`/`file`/`true`, which the anti-laundering strip
    # never touched -- so it rides as the requirement oracle for the
    # `db_free_runtime.resolved.*.value` clause rewritten in the same commit,
    # not as a render-once discriminator; likewise
    # `..._local_root_event_bytes_are_unchanged_by_the_single_render`, the
    # byte-identity guard whose literal was captured from pre-change source and
    # is green on both sides by construction (fixture task 5.1b). They route
    # because a journal-only PR must run the clause's oracles, not because they
    # bite on the revert.
    # Node ids rather than the whole file -- not a runtime argument (the whole
    # suite is DB-free, 175 tests in 2.23s; these six run in 0.86s) but a
    # subject one: the other 169 are the retry route's own contract and already
    # route through `services/orchestrator/retry.py`'s same-name row, so the
    # journal's rule stays at what the journal decides. The
    # `test_production_scheduler.py::` node ids on the scheduler_runtime.py rule
    # are the same shape.
    "tests/test_retry.py::test_retry_api_file_lane_503_renders_uri_roots_like_the_database_lane",
    "tests/test_retry.py::test_retry_api_file_lane_uri_roots_stay_public_placeholders_and_reach_private_recovery",
    "tests/test_retry.py::test_retry_api_file_lane_successful_submission_event_renders_uri_roots_once",
    "tests/test_retry.py::test_retry_api_file_lane_local_root_event_bytes_are_unchanged_by_the_single_render",
    "tests/test_retry.py::test_retry_api_file_lane_503_classifies_whitespace_bearing_uri_roots_whole",
    "tests/test_retry.py::test_retry_api_file_lane_db_free_runtime_evidence_values_survive_the_single_render",
    # #2938 (entropy-burndown task 5.0): the cluster map test parses this module's
    # top-level definitions and their callers, so a journal edit that adds,
    # renames or moves a definition must redden it on the PR. Stop-rule owned
    # module, so like #1581 the addition rides this at-site tuple. AST-only, ~2s.
    "tests/test_file_journal_cluster_map.py",
    # #2939 (entropy-burndown task 5.1): the facade hygiene test parses this
    # module's `__all__` and needs every listed name to keep a consumer. AST-only.
    "tests/test_file_orchestration_journal_facade.py",
    "tests/test_file_journal_late_lookup_seams.py",  # #2944 (task 5.4): facade patches reach moved siblings
    # #3028: function-body facade import + facade-global patch, so importer derivation misses them. ~30s.
    "tests/test_gateway_reconcile_claimant_exclusivity.py",
    "tests/test_gateway_reconcile_file_cohort_comment.py",
    "tests/test_gateway_reconcile_file_submit_barrier.py",
    "tests/test_gateway_reconcile_identity_release.py",
    "tests/test_gateway_reconcile_inventory.py",
    "tests/test_gateway_reconcile_round10.py",
    "tests/test_gateway_reconcile_writer_launch.py",
    "tests/test_gateway_reconcile_writer_quiescence.py",
    "tests/test_gateway_reconcile_writer_receipts.py",
    "tests/test_safe_fs_lock.py",
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
# The two sets have different subjects. Folding them together would route every
# river reader diff at three forcing suites for nothing.
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
    # Task 7.2's own oracle: the "store is the literal narrow" AST sweep, the
    # #2511 fact-table scan predicate and the registered templates' shape
    # invariants. It reads every wired reader module, so a diff to one of them
    # must run it.
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
    # #2484: per-field echo verdicts over real envelopes, discovery SQL + lane
    # statuses, the real-DB discovery seed, and the two-node consumer of the
    # route records these producers write.
    "tests/test_readonly_db_route_identity.py",
    "tests/test_readonly_db_discovery_and_lane_statuses.py",
    "tests/test_readonly_db_discovery_integration.py",
    "tests/test_readonly_db_two_node_consumer.py",
)


# #2074: `tests/test_hydro_display_mvt_scaling.py` (4888 lines, 210 cases) was
# partitioned by topic to retire its `.large-file-guard.json` exemption. The base
# path SURVIVES as one partition on purpose — three registries pin that literal
# string: `openspec/specs/ci-contract-baseline/spec.md`, the
# `infra/systemd/nhms-display-api.service` rule in PATH_TEST_RULES (whose only reader is
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

# #2527 partitioned tests/test_direct_grid_display_cutover_flip.py (1814 lines,
# 15 cases) into these two collectible suites plus one non-collectible helper.
# The monolith is GONE with no compatibility shim, so every rule site that named
# it by hand was re-pointed; a rule target that no longer exists is only a
# WARNING here, so a forgotten site would have dropped the station-flag flip lock
# out of the PR lane in silence.
#
# ONE tuple, carried whole by both display rules (`services/tiles/mvt.py` and the
# `apps/api/routes/hydro_display*.py` glob), so each route still selects the 15
# cases it selected before the split. This is deliberately wider than the
# derived closure: only the `_atomic` partition imports
# `apps.api.routes.hydro_display` (for `_station_source_version`, whose on-disk
# body in hydro_display_identity.py it structurally locks), while the `_mvt_set`
# partition's `_mvt_station_set` is a model of exactly that row-selection
# predicate and imports no display module. A predicate edit that the lock
# catches must also run the model that mirrors it. The `_atomic` partition is
# therefore the anti-vacuity anchor in tests/test_select_ci_tests.py; the
# tracked-tree guard there pins exactly two suites + one helper.
DIRECT_GRID_DISPLAY_CUTOVER_FLIP_HELPERS_PATH = "tests/direct_grid_display_cutover_flip_helpers.py"
DIRECT_GRID_DISPLAY_CUTOVER_FLIP_ATOMIC_TEST = "tests/test_direct_grid_display_cutover_flip_atomic.py"
DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS: tuple[str, ...] = (
    DIRECT_GRID_DISPLAY_CUTOVER_FLIP_ATOMIC_TEST,
    "tests/test_direct_grid_display_cutover_flip_mvt_set.py",
)

# #2490 partitioned three oversized suites with no behaviour change. Each
# original path stays one partition (openspec/specs/ci-contract-baseline/spec.md
# pins the retention base path), and every rule that selected an original path
# selects ALL of its partitions, so an owner path runs exactly the cases it ran
# before the split.
#
# The MVT cache-retention suite: three collectible partitions plus the
# non-collectible tests/node27_mvt_cache_retention_helpers.py (routed in
# SUPPORT_MODULE_TEST_RULES; all three partitions import it, `_clean_env` autouse
# included).
NODE27_MVT_CACHE_RETENTION_HELPERS_PATH = "tests/node27_mvt_cache_retention_helpers.py"
NODE27_MVT_CACHE_RETENTION_TESTS: tuple[str, ...] = (
    "tests/test_node27_mvt_cache_retention.py",
    "tests/test_node27_mvt_cache_retention_enumeration.py",
    "tests/test_node27_mvt_cache_retention_wrapper.py",
)
# The #1341 read-path surrogate-key proof: two file-level `integration`-gated
# partitions. Their shared helper
# (tests/river_ts_read_path_surrogate_keys_integration_helpers.py) and the
# national identity probe's (tests/mvt_national_identity_probe_integration_helpers.py)
# deliberately get NO SUPPORT_MODULE_TEST_RULES entry: the closure guard's
# importer derivation skips file-level integration-gated importers, so their
# derived set is empty and a rule would red it. A helper-only diff collapses to
# the meta-guard in the PR lane, and the `integration` in both helper names keeps
# them inside ci.yml's `database` filter glob `tests/*integration*.py`.
RIVER_TS_READ_PATH_SURROGATE_KEYS_INTEGRATION_TESTS: tuple[str, ...] = (
    "tests/test_river_ts_read_path_surrogate_keys_integration.py",
    "tests/test_river_ts_read_path_surrogate_keys_coverage_integration.py",
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
    # #2418 D4: the receipt `server` block, its SERVER_IDENTITY_MISSING refusal
    # and the archived-1.0 byte pins, driven through the same CLI.
    "tests/test_node27_pgdata_workload_server_identity.py",
    "tests/test_forecast_api.py",
    "tests/test_forecast_store_routing.py",
    "tests/test_select_ci_tests.py",
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
# #2348: the response-model preservation oracle. It captures the object each
# JSON handler returns and proves the route's `response_model` serializes it
# exactly as the pre-#2348 implicit `dict[str, Any]` field did, so a handler
# edit on any of the six modelled route modules (or a model edit) that changes
# a wire value reds it. Both halves ride together: the pipeline half imports
# the oracle core from the base file.
RESPONSE_MODEL_PRESERVATION_TESTS: tuple[str, ...] = (
    "tests/test_response_model_preservation.py",
    "tests/test_response_model_preservation_pipeline.py",
)
# #2612: the model registry facade's DIRECT non-gated importer suites (the
# selector suite's GUARDED_MODULE_CLOSURES derives this set from the tracked tree
# in direct-only mode and reds on a new importer). Merged into the rule the
# CONNECTION_ATTRIBUTION_STORE_PATHS tuple expands to, so the facade and every
# `model_registry_*.py` owner select it: the owners carry the store method bodies
# these suites exercise, and #2617 pins every owner to the facade's selection.
# tests/test_basins_registry_import_db.py is included although all 5 of its tests
# are function-level `integration` (the guard filters file-level marks only);
# that costs 5 skips, not a new exemption ledger.
# The two cutover-flip partitions are listed on top of the derived set: they
# reach this module only through tests/direct_grid_display_cutover_flip_helpers.py,
# a `tests/` helper the guard's one-hop domain cannot see, and #2612 requires them.
MODEL_REGISTRY_IMPORTER_TESTS: tuple[str, ...] = (
    "tests/test_basins_registry_import_db.py",
    "tests/test_basins_registry_import_qhh.py",
    "tests/test_legacy_reactivation_guard.py",
    "tests/test_list_search_contract.py",
    "tests/test_model_registration.py",
    "tests/test_model_registry_basin_versions.py",
    "tests/test_model_registry_evidence_only_boundary.py",
    "tests/test_model_registry_list_basins.py",
    "tests/test_node27_connection_attribution.py",
    "tests/test_qhh_production_bootstrap_scheduler.py",
    "tests/test_replay_lineage.py",
    "tests/test_state_clone_cutover_hook.py",
    "tests/test_state_clone_index_publish.py",
    "tests/test_variant_activation_cutover.py",
    *DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS,
)
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
