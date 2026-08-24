## Context

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

The master baseline at `c681fe859b2a9dc62bdc8de568317ae771bb87f6` has one 14,184-line module,
238 top-level test functions, and 538 collected cases; the full suite passes. The 1,000-line guard has
no exemption for this file and no exemption may be added. The issue's “do not change selector rules”
boundary conflicts textually with its selector acceptance criterion: this design preserves selector
semantics but replaces the deleted target path with its partitions, because leaving the stale path
would drop the suite from the PR lane.

## Goals / Non-Goals

**Goals:**
- Keep every collectible and support module at or below 1,000 lines.
- Preserve the sorted `::test_name[param-id]` suffix multiset exactly: 538 unique old suffixes
  map one-to-one to 538 new node IDs.
- Preserve test bodies, decorators, parameter order/IDs, assertions, fixtures, monkeypatch ownership,
  import-order-sensitive behavior, and full-suite results.
- Preserve targeted-CI ownership: Slurm gateway changes select every partition; `reconcile.py` and
  `persistence.py` changes do not gain the expensive suite.

**Non-Goals:**
- Production source changes, assertion/oracle changes, pytest discovery configuration, guard
  exemptions, test renames, new runtime behavior, or cleanup of unrelated oversized suites.

## Decisions

1. **Use flat responsibility modules.** Partition into responsibility lanes, adjusting boundaries only
   to keep each file at or below 1,000 lines without moving tests across responsibilities. The lanes
   are `file_cohort_comment`, `file_cohort_projection`, `file_cohort_authority`,
   `file_cohort_identity`, `inflight_identity`, `idempotency_barrier`, `reservation_lifecycle`,
   `grace_guard`, `comment_accounting`, `inventory`, `writer_prepare`, `writer_rollforward`,
   `writer_launch`, `writer_quiescence`, `writer_receipts`, `master_transitions`,
   `file_submit_barrier`, `comment_capability`, `comment_sacct_bounds`, `store_reset`, `round10`,
   `identity_release`, and `identity_invariants`. The original collectible module is deleted; no
   re-export shim remains.
2. **Split support by ownership, not convenience.** Use non-collectible
   `tests/gateway_reconcile_helpers.py` for store/cohort/reconcile/identity fixtures and, if needed
   for the line budget and ownership, `tests/gateway_reconcile_writer_helpers.py` for writer/barrier
   utilities. The helper names must not match pytest's `test_*.py` or `*_test.py` patterns. Update
   all checked-in imports and `SUPPORT_MODULE_TEST_RULES`/routing anchors.
3. **Keep source-inspection ownership local.** `_test_function_source` must read the module that
   defines its target test. Keep or parameterize the tiny helper separately in the idempotency- and
   file-submit-barrier modules; it must never inherit a support module's `__file__`.
4. **Preserve runtime binding.** The idempotency worker, `_StoreRepo`, and SQLAlchemy `Session` must
   share one owning module; monkeypatch targets move from `tests.test_gateway_reconcile.*` to that
   owner. The chain-cycle mixin import remains function-local and in the same order.
5. **Use suffix identity as the mapping oracle.** Full pytest node IDs necessarily change module
   prefix. Compare sorted unique text after the first `::`, retain all explicit/default parameter
   IDs and decorator order, and persist a TSV from each old suffix to its new full node ID as review
   evidence.
6. **Change selector inventory, not policy.** Replace only `tests/test_gateway_reconcile.py` in the
   existing `services/slurm_gateway/**` tuple with all collectible partitions. Do not add them to
   `services/orchestrator/**` or the narrow `real_backend.py` extension. Retain per-partition
   `runtime-budget` dispositions for `persistence.py` and `reconcile.py`; helper-only routing selects
   real consumer suites plus the selector meta-guard without pulling the oversized
   production-scheduler suite.
7. **Keep compatibility references executable.** Update checked-in imports, entropy/inventory
   command strings, active OpenSpec commands, canonical owner references, and three runbook node IDs
   to the owning new module. Correct the stale comment to state the accepted decisions are
   `{absence_retry_permitted, operator_verified_absence}`; executable behavior remains untouched.

## Invariant Matrix

- Governing invariant: Physical partitioning changes only module paths; every pre-change
  gateway-reconcile assertion remains collected exactly once, runs unchanged, and stays selected on
  the same CI ownership surface.
- Source-of-truth identity/contract: baseline sorted `::test_name[param-id]` suffixes (538 unique
  entries), original test bodies/decorators/assertions, and selector ownership for
  `services/slurm_gateway/**`.
- Producers: pytest collection from the split `tests/test_gateway_reconcile_*.py` modules.
- Validators/preflight: node-id suffix comparison, duplicate check, line-count guard, selector
  tests, entropy tests, Ruff, and strict OpenSpec validation.
- Storage/cache/query: test-only stores and repositories move without behavior changes; no
  production storage changes.
- Public routes/entrypoints: `scripts/select_ci_tests.py` CLI/function selection output; no
  application API changes.
- Frontend/downstream consumers: CI targeted-test job, compatibility inventories, active OpenSpec
  commands, canonical owner references, runbook node IDs, and test modules importing shared fixtures.
- Failure paths/rollback/stale state: missing/duplicate collection, stale selector targets, broken
  monkeypatch bindings, wrong `__file__`, and import-order drift must fail focused tests; rollback is
  a revert to the original module.
- Evidence/audit/readiness: baseline and final collection artifacts, old-to-new TSV, focused/full
  pytest output, selector probes, line-count report, review reports, and CI.
- Regression rows:
  - baseline 538 suffixes -> final 538 unique identical suffixes with one new full node ID each.
  - Slurm gateway or real-backend changed path -> every split suite remains selected and the prior non-suite targets remain unchanged.
  - `reconcile.py`/`persistence.py` changed path -> split suites remain excluded under the recorded runtime budget.
  - helper-only changed path -> routed collectible consumers plus selector meta-guard, never the support file itself.
  - source-inspection and monkeypatch cases -> target the new owning module and pass without a compatibility shim.
  - unchanged demotion/scheduler consumers -> import the moved helper and retain existing behavior.

## Boundary-Surface Checklist

- Shared helper roots: both gateway-reconcile support modules and existing demotion helper consumers.
- Public entrypoints: `select_tests` and its rule tables.
- Read surfaces: source-inspection tests using `Path(__file__)`, compatibility inventories, runbook node IDs.
- Write/delete/overwrite surfaces: delete the monolith and add partitions only; no runtime filesystem behavior.
- Producer/consumer evidence boundaries: pytest collection output to suffix-map evidence, selector rules to targeted-CI output.
- Stale-state/idempotency boundaries: idempotency/barrier tests retain module bindings and exact assertions.
- Unchanged downstream consumers: demotion suites, production scheduler test imports, and existing Slurm gateway selector targets.

## Risks / Trade-offs

- **Accidental test loss or duplication** -> compare unique suffix sets and counts, then run the complete partition glob.
- **Helper extraction changes global binding** -> keep patched symbols with their worker and run the affected concurrency tests.
- **Selector path replacement broadens or narrows CI** -> compare four frozen selector outputs and assert exact ownership boundaries.
- **Mechanical files approach the line ceiling** -> target headroom and run an all-changed-files line-count gate before commit.
- **Module path changes external node IDs** -> update all tracked references and publish the explicit suffix-to-new-node mapping.
- **The 9,115-line entropy guard test cannot accept its five expected-command edits** -> keep its old
  literals only as explicitly non-executable inventory provenance and route their removal to #1823;
  current human-run commands use the new glob.
