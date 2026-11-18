# Proposal — retire-state-clone-activation-hook (#3189)

Closes #3189.

```text
Issue type: retirement (dead code + spec-to-reality)
Fixture level: expanded
Upstream suggested level: absent (override: activation transaction, five specs)
Blast radius: model lifecycle activation (API /models lifecycle routes, basin retirement script); CI selector tables
Selected risk packs: Legacy compatibility, Error handling / rollback, Concurrency / shared state (activation transaction)
Evidence floor: E1-E7 below
```

## Why

Owner decision (2026-11-17): **retire** the `state_clone` pre-activation hook. `PRE_ACTIVATION_HOOK_MOUNT_POINTS` is
`("state_clone",)`, but no production path registers a hook on it, so every real activation ran only the default no-op.
The specs still promise a fingerprint-gated clone at activation, which production never performs. State carry-over at a
cutover is actually done by the node-22 clone CLI `scripts/node22_clone_direct_grid_cutover_states.py`.

## What

### Part 1: retire the in-transaction chain, whole

With the last mount point gone, the whole chain is dead.

**Scope beyond the issue's literal list (recorded deviation):** the issue names the mount point, the dispatch,
`state_clone_hook.py` and its suite. Also delete every symbol that only serves the chain. An approval argument that is
accepted and then ignored is worse than having none.

1. `packages/common/model_registry_contracts.py`:
   - `PRE_ACTIVATION_HOOK_MOUNT_POINTS`, `PreActivationHook`, `_default_no_op_hook`;
   - `ModelActivationContext`, `ColdStartApprovalInput`;
   - `_build_activation_result_approval_block`, including its lazy marker import at `:442`;
   - `_would_be_already_current`, whose only caller is the hook gate at `model_registry_lifecycle.py:344`.

   `_extract_source_scope` **stays**: `PostCommitPublishContext` uses it at `:412`.
2. `packages/common/model_registry.py`: the re-exports and the `_pre_activation_hooks` initialisation (`:186-195`).
3. `packages/common/model_registry_lifecycle_support.py`: `register_pre_activation_hook`,
   `_dispatch_pre_activation_hooks` and `_record_state_clone_refusal_audit`.
4. `packages/common/model_registry_lifecycle.py`:
   - the `cold_start_approval` parameter;
   - the lazy `StateCloneCutoverRefusedError` import;
   - `activation_context` and the dispatch block (`:327-364`), including the unreachable `hook_target is None` guard;
   - the result's `cold_start_approval` block;
   - the `except StateCloneCutoverRefusedError` branch.
5. Delete `packages/common/state_clone_hook.py` and `tests/test_state_clone_cutover_hook.py`.
6. `tests/node27_retire_basin_helpers.py:517`: drop `cold_start_approval` from the fake's signature.

**Must preserve, load-bearing in the same block:**
- the scope lock (`_lock_basin_version_scope`), the `FOR UPDATE` fetches, preflight, `_apply_model_lifecycle_transition`
  and its already-current short-circuit, and the audit insert with its `ModelLifecycleAuditPersistenceError` rollback;
- the post-commit `publish_context` built from `_extract_source_scope`.

**Post-commit tail untouched.** `register_post_commit_manifest_publisher`, `register_post_commit_state_index_publisher`
and `state_clone_index_publisher.py` are not in this change. Neither seam has a production registration either, which is
a separate wire-vs-retire question, filed as #3204. Their code stays, with one comment-only exception: the `state_clone_index_publisher.py:28-42` docstring, which names
the hook.

The `atomic-cutover-transaction` index-publish requirement is REMOVED rather than restated. Its only production input
was DB clone rows written by the hook; the clone CLI writes the file state index. #3204 gets a comment: the state-index
publisher would find no rows even if it were wired.

**Kept:**
- `packages/common/state_clone.py` (the core clone, used by the CLI);
- `STATE_CLONE_COLD_START_APPROVAL_REQUIRED` (still produced by the core);
- `state_clone_index_publisher.py`.

### Part 2: stale "Change 8" ownership comments

Reword the comments listed in #3189 to the #3165 spec wording: "mirror activation is outside the producer; no
production code path sets `active_flag=true`". The edit is comment-only.
- `packages/common/forcing_domain_handoff_apply.py:747`, `:765`, `:1156`
- `workers/forcing_producer/file_store.py:872`
- `workers/model_registry/direct_grid_variant_registration.py:40`, `:568`, `:634`
- `tests/test_direct_grid_variant_registration.py:297`, `:346`, `:986`, `:1685`, `:1841`, `:1932`
- `tests/test_forcing_domain_handoff_apply.py:570`, `:589`
- `tests/test_forcing_producer.py:6069`

### Tests: keep the atomicity assertions, change the injection

| Test | Disposition |
|---|---|
| `tests/test_variant_activation_cutover.py` hook-only tests (`:61`, `:111`, `:144`, `:291`, `:332`, `:418`, `:431`) and the `_would_be_already_current` test (`:375`) | delete |
| `_extract_source_scope` test (`:395`) | keep |
| `test_single_transaction_swap_and_audit` (`:204`) | keep. Prove that the swap and the audit share one cursor with a recording cursor or store spy instead of a hook probe. |
| `tests/test_variant_activation_cutover_dispatch.py:363` | keep the no-second-transition, no-manifest and no-audit assertions; drop the hook spy; rename |
| `tests/test_variant_activation_cutover_manifest.py:283` | keep "rollback never re-publishes the manifest", injecting an `_insert_model_lifecycle_audit` failure; clean up `:319` |
| `tests/variant_activation_cutover_helpers.py` | delete `TWO_MOUNT_HOOK_CHAIN`, `install_two_mount_hook_chain` and the doc line `:14` |
| `tests/test_model_registration.py:3656-3692` | drop the hook injection and the `refused` case, which only the deleted hook produced. Keep the other parametrized cases. |
| `test_hooks_skipped_when_blocked` (`:251`) | **keep**, drop the spy and rename. It carries the only `["blocked"]` audit-row and no-state-update-on-blocked assertions. |
| manifest `:283` rewrite assertions | `status == "blocked"`, last blocker `LIFECYCLE_AUDIT_PERSISTENCE_FAILED`, `stub.call_count == 0`, `_transactions[-1]["committed"] is False`. Do not assert that `_state_updates` is empty: the harness does not roll it back. "Clean up `:319`" means the already-current test that follows, which registers hooks. |
| `tests/variant_activation_cutover_helpers.py` `_update_model_lifecycle_state` / `_insert_model_lifecycle_audit` (`:217-258`) | record the cursor argument, so `:204` can prove a single cursor; also pin `committed_cursor.statements == []` (no state-row write), which covers ADDED scenario 1 |
| **move**: the `state_manager` half of the lockstep test (`tests/test_state_clone_cutover_hook.py:1703-1745`, with `_upsert_sql_fragments`) | move to `tests/test_state_manager_clone_upsert.py`. It is the only static pin on `PsycopgStateSnapshotRepository.upsert_state_snapshot`'s `clone_gate_kind` column, VALUES count and param tuple. Confirm that the selector picks that suite for a `packages/common/state_manager.py` change. |
| docstrings `tests/test_state_clone_index_publish.py:600-610`, `:1004-1010`, `tests/test_direct_grid_display_cutover_history.py:46`, `tests/test_state_clone.py:682`, `:1314-1317` (comment naming `ColdStartApprovalInput`), `tests/variant_activation_cutover_helpers.py:7-14`, `:137-141`, `tests/test_variant_activation_cutover.py:1-30`, `tests/lineage_state_index_fixtures.py:14`, `:71` | reword (comment-only) |

### CI, guard and docs

- **Selector tables:**
  - `scripts/select_ci_tests_tables.py`: `STATE_CLONE_HOOK_TESTS` (`:501-507`), and its entries at `:1082`, `:1742`
    and the `:1708` comment;
  - `scripts/select_ci_tests_rules.py`: the `:125` import, the `:1788-1790` comment and the `PathTestRule` at
    `:1853-1857`;
  - `tests/test_select_ci_tests.py` pins (`:16118`, `:16253`, `:16721`). The `:16249-16266` function keeps its node-22
    half. The `:11863` comment loses its special case.
- `scripts/ci/full_test_durations.json:443`; `.large-file-guard.json:80`.
- `docs/adr/0005-recalibration-state-carryover.md:165-169`: append a dated superseding note. The second upsert copy is
  gone with the hook, and the lockstep test shrinks to a single-copy pin in `tests/test_state_manager_clone_upsert.py`.
  Do not rewrite the ADR.
- `docs/runbooks/production-ops/recalibration-and-archive.md:104`: "走冷启动 + approval" points at the deleted approval
  input. Reword it to the real options:
  - repair the package;
  - or, in `baseline_cutover` only, declare the basin cold;
  - otherwise a cold start by omission, with the §11.3 announcement as the operator's obligation.
- **Stale explanatory comments in kept code** (comment-only):
  - `packages/common/model_registry_lifecycle.py:176-191`, `:460-465`;
  - `packages/common/model_registry_lifecycle_support.py:1-4`, `:40`, `:91`, `:102`;
  - `packages/common/model_registry_contracts.py:1-5`, and the "raising hook" wording in the `PostCommitPublishContext`
    docstring;
  - `packages/common/model_registry.py:186-192`, `:201`;
  - `packages/common/state_clone.py:14-17`, which cites deleted requirement names;
  - `packages/common/state_manager.py:900`, `:1742`, `:4104`, and `services/orchestrator/replay_lineage.py:423`, where
    "state-clone hook writes clone rows" becomes "the clone CLI writes";
  - `scripts/select_ci_tests_tables.py:1712-1713` and `tests/test_select_ci_tests.py:16723-16724`, because the dispatch
    shard loses its facade import.
  - The base cutover shard must keep importing `_extract_source_scope` through the facade, or
    `tests/test_select_ci_tests.py:6529` goes red.

### Specs

The change carries spec deltas for five capabilities:
- `variant-activation-cutover`: the hook-chain requirement is REMOVED, and the re-publish requirement is MODIFIED.
- `atomic-cutover-transaction`:
  - all 4 requirements are REMOVED, including index publish (its DB clone-row input is gone; see #3204);
  - ADDED: "Model activation SHALL NOT clone state". It says plainly that the never-activated-but-not-transferred
    guarantee leaves the spec, and that state transfer is an operator procedure through the clone CLI;
- `fingerprint-gated-state-clone`: requirement 1 is MODIFIED (no activation rollback; the cold start is the CLI
  declaration), and the per-source-across-activation requirement is REMOVED.
- `no-rollback-state-semantics`: two requirements are MODIFIED.
- `ci-contract-baseline`: the irregular-routes requirement is REMOVED and re-ADDED without the hook mapping.

The `variant-activation-cutover` Purpose line, which mentions the hook chain, is not editable through a delta. Fix it in
the archive PR.

## Non-goals

- The post-commit manifest and state-index publisher seams (#3204).
- `state_clone.py`, the clone CLI, the recalibration path.
- Pruning the API/OpenAPI status enums. None mention `refused` for lifecycle (checked: zero hits in `apps/`, `schemas/`
  and `openapi/`).

## Evidence Floor

- [x] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`.
- [x] E2 Zero hits, word-matched so the kept constant `state_clone_cold_start_approval_required` does not match:
  - `git grep -nwE "register_pre_activation_hook|_dispatch_pre_activation_hooks|_pre_activation_hooks|PRE_ACTIVATION_HOOK_MOUNT_POINTS|PreActivationHook|_default_no_op_hook|ModelActivationContext|ColdStartApprovalInput|cold_start_approval|_build_activation_result_approval_block|_record_state_clone_refusal_audit|_would_be_already_current|StateCloneCutoverRefusedError|STATE_CLONE_SPIN_UP_DISTORTION_ANNOUNCEMENT_MARKER|install_two_mount_hook_chain|TWO_MOUNT_HOOK_CHAIN|STATE_CLONE_HOOK_TESTS|state_clone_hook|test_state_clone_cutover_hook" -- . ':!openspec' ':!docs/review-loop-log.jsonl' ':!docs/adr/0005-recalibration-state-carryover.md'`;
  - prose: `git grep -nE "pre-activation|hook-aborted|raising hook|state-clone hook|clone hook" -- packages services scripts tests docs/runbooks`
    has zero hits, apart from any hit the PR lists and justifies;
  - for part 2, `git grep -n "Change 8" -- packages workers tests/test_direct_grid_variant_registration.py tests/test_forcing_domain_handoff_apply.py tests/test_forcing_producer.py`
    no longer finds an ownership claim for the flip.
- [x] E3 Mutation reds on the retained atomicity tests, each restored:
  - make `_insert_model_lifecycle_audit` failure not roll back (for example, swallow it): the manifest test (`:283`) and
    the model-registration audit case go red;
  - wrap the audit insert in a second `with self._transaction()`: `test_single_transaction_swap_and_audit` goes red;
  - drop the `clone_gate_kind` column from `PsycopgStateSnapshotRepository.upsert_state_snapshot`: the moved lockstep
    pin goes red;
  - remove the already-current short-circuit in `_apply_model_lifecycle_transition`: the dispatch repeat test goes red.
- [x] E4 Local: `uv run pytest -q` over:
  - `tests/test_variant_activation_cutover.py`, `tests/test_variant_activation_cutover_dispatch.py`,
    `tests/test_variant_activation_cutover_manifest.py`;
  - `tests/test_model_registration.py`, `tests/test_state_clone_index_publish.py`, `tests/test_state_clone*.py`,
    `tests/test_direct_grid_display_cutover_history.py`;
  - the basin-retirement suites importing `node27_retire_basin_helpers`, `tests/test_select_ci_tests.py`;
  - the selector set for the diff.
- [ ] E5 node-27 temporary worktree: the E4 set plus the selector set, with the real-DB integration env,
  `leftover_it_dbs=0`.
- [ ] E6 `openspec validate retire-state-clone-activation-hook --strict --no-interactive`.
- [ ] E7 The PR body names:
  - the scope beyond the issue's list;
  - the dropped guarantee;
  - the filed post-commit-tail issue;
  - the archive-PR Purpose task.
