# Proposal — retire-post-commit-publishers (#3204)

Closes #3204.

```text
Issue type: retirement (dead code + spec-to-reality)
Fixture level: expanded
Upstream suggested level: absent (override: activation lifecycle, tested invariants, two specs)
Blast radius: model lifecycle operation (API /models lifecycle routes, basin retirement); CI selector tables
Selected risk packs: Legacy compatibility, Error handling / rollback, Concurrency / shared state (activation transaction)
Evidence floor: E1-E7 below
```

## Why

Owner decision (2026-11-18): **retire** both post-commit seams, the manifest publisher and the state-index publisher.
They are consistent with #3189, which retired the in-transaction hook. Neither seam was ever registered in production,
so every real transition ran the default no-op.

The production path that changes node-22's dispatch set is the operator procedure in
(`docs/runbooks/production-ops/recalibration-and-archive.md:134-146`). After #3189, no production path writes the DB
clone rows that `state_clone_index_publisher.py` reads.

## What

### Production deletions

1. Delete `packages/common/state_clone_index_publisher.py`. This also removes `StateIndexPublishFailedError`, which has
   no user outside this module and its test.
2. Delete from `packages/common/model_registry_contracts.py`:
   - `PostCommitPublishContext`, `PostCommitManifestPublisher`, `_default_no_op_manifest_publisher`;
   - `PostCommitStateIndexPublisher`, `_default_no_op_state_index_publisher`;
   - `_should_publish_manifest_after_commit`;
   - `_extract_source_scope`, whose only remaining caller is the `publish_context` build.
3. Delete from `packages/common/model_registry_lifecycle_support.py`:
   - `register_post_commit_manifest_publisher` and `_dispatch_post_commit_manifest_publish`;
   - `register_post_commit_state_index_publisher` and `_dispatch_post_commit_state_index_publish`;
   - their imports;
   - the "post-commit" wording in the module and class docstrings.
4. `packages/common/model_registry.py`: remove the `__post_init__` seeding, and the whole method if nothing else
   remains, plus the related imports and re-exports.
5. `packages/common/model_registry_lifecycle.py`: remove the `publish_context` declaration and comment, the staging
   block, and the post-commit dispatch tail. The function returns `result` directly.
6. **Orphan created by this change:** `get_latest_clone_row_for_model_source`
   (`packages/common/state_manager.py`, protocol stub and implementation). Its only production caller was the deleted
   publisher, so it is deleted, as #3189 did with `_would_be_already_current`.
   - Reword the docstrings of `get_earliest_clone_row_for_model_source` that cite it.
   - `get_earliest_clone_row_for_model_source` **stays**: it is the scheduling-time lineage resolver.

**Must preserve:**
- the activation transaction: scope lock, `FOR UPDATE`, preflight, transition, already-current, the audit insert and
  its `ModelLifecycleAuditPersistenceError` rollback;
- the lifecycle result dict, which has no publisher-derived field;
- `get_earliest_clone_row_for_model_source` and the lineage resolver;
- the node-22 operator tooling.

### Tests

| Test | Disposition |
|---|---|
| `tests/test_state_clone_index_publish.py` | delete (publisher legs only) |
| `tests/test_variant_activation_cutover_manifest.py` publisher legs: `test_manifest_republished_on_*`, the deactivate-of-non-active pair with `_AllowedDeactivateHarnessStore`, the per-operation and idempotent republish tests, the predicate matrix, the no-op default test, and the preflight-blocked and already-current `not_republished` tests (covered elsewhere) | delete |
| `test_manifest_not_republished_when_audit_persistence_fails` | **move** to `tests/test_variant_activation_cutover.py` and rename (for example `test_audit_persistence_failure_rolls_back_the_transition`). Keep `status == "blocked"`, last blocker `LIFECYCLE_AUDIT_PERSISTENCE_FAILED`, and `_transactions[-1]["committed"] is False`. Drop `stub.call_count`. |
| `test_manifest_not_republished_on_supersede_of_active_blocked` | **move** and rename. Keep `status == "blocked"`, `MISSING_ACTIVE_RISK` in the codes, and `_state_updates == []`. |
| `tests/test_variant_activation_cutover_manifest.py` | delete once it is empty |
| dispatch `test_repeat_already_current_no_second_transition_no_manifest_no_audit` | keep every non-publisher assertion, drop `publisher.call_count` and the stub import; rename it to `..._no_second_transition_no_audit` |
| `tests/test_variant_activation_cutover.py` `_extract_source_scope` test and import | delete; update the module docstring |
| `tests/variant_activation_cutover_helpers.py` `_ManifestPublisherStub` and the `PostCommitPublishContext` import | delete; update the docstring |
| `tests/test_legacy_reactivation_guard.py` `_publisher_stub`, `PostCommitPublishContext` import, unused `publisher_calls`, `publisher_contexts` lines in 6 tests | delete those lines only; all other assertions stay |
| `tests/test_scheduler_lineage.py`: `test_db_plane_never_consults_the_publishers_latest_row_reader` (`:643`, the only user of `_LatestOnlyCloneRowRepo`), the fake itself, and `test_publisher_latest_clone_row_query_keeps_the_fingerprint_filter` | delete. The reader no longer exists. The kept rule against a latest-first resolution source is still exercised by the ASC text pin at `tests/test_scheduler_lineage.py:740` and by the real-DB earliest leg below. Reword the stale docstrings at `:21-22`, `:572`, `:623` and `:756-757`. |
| `tests/test_real_database_integration.py:674` `test_real_clone_row_readers_disagree_about_a_null_fingerprint_row` | delete **only** the `get_latest` assertions (`:739-746`, `:761-765`) and the D2 latest half of the docstring (`:680-683`, `:694`, `:698-699`). **Keep** the second insert (the later fingerprinted sibling, `:748-753`), `earliest_with_sibling` (`:755-759`) and the end-to-end resolver chain (`:767-773`): they are the only real-SQL oracle for "latest-first SHALL NOT be the resolution source". Rename, for example `test_real_earliest_clone_row_reader_admits_a_null_fingerprint_row_over_a_later_sibling`. |
| a new test in `tests/test_variant_activation_cutover.py` | assert that the store has no `register_post_commit_*` attribute and that a committed activation returns its result with no publisher plumbing (ADDED scenario 1) |

### CI, guard and docs

- **Selector:**
  - `scripts/select_ci_tests_tables.py`: the manifest entry in `VARIANT_ACTIVATION_CUTOVER_TESTS` and the
    `tests/test_state_clone_index_publish.py` entry in `MODEL_REGISTRY_IMPORTER_TESTS`;
  - the `scripts/select_ci_tests_rules.py` `PathTestRule` for the manifest file;
  - the `tests/test_select_ci_tests.py` pins: the directory-rule selected list (`:2215-2216`), `:16725` and `:16731`;
    `assert len(partitions) == 3` at `:16791` becomes 2; rename
    `test_variant_activation_cutover_tracked_tree_is_exactly_three_suites_and_one_helper` (`:16777`) to match.
  - Stale selector comments: `scripts/select_ci_tests_rules.py:316-317` and `:607-608`;
    `scripts/select_ci_tests_tables.py:1705-1709` ("three partitions", "`_manifest` also imports").
- `scripts/ci/full_test_durations.json`; `.large-file-guard.json` (the publish-test entry); the per-file-ignore for the
  manifest file in `pyproject.toml`.
- `docs/runbooks/production-ops/file-provider-refresh.md:13`: drop "model lifecycle" from the writers list. Check the
  `expected_preimage` paragraph after it too.
- `docs/runbooks/production-ops/operating-scope.md:86-87`: state that a lifecycle transition never moves node-22's
  dispatch set, and that the file plane is published by node-22 tooling.
- **Stale prose in kept code:**
  - `services/orchestrator/scheduler_lineage.py:247`, which says "NOT the publisher's ``DESC`` reader";
  - the `packages/common/model_registry_contracts.py:2` and `packages/common/model_registry.py:5` module docstrings;
  - `tests/variant_activation_cutover_helpers.py:277-278`.

### Specs

- `variant-activation-cutover`: the re-publish requirement is REMOVED, because all four of its scenarios change meaning.
  ADDED: "A model lifecycle transition SHALL NOT publish the scheduler file plane", with scenarios for no publish, the
  supersede-of-active preflight block, and the audit-failure rollback. Fix the Purpose bullet in the archive PR.
- `fingerprint-gated-state-clone`: two MODIFIED requirements, each a full copy with the same scenarios.
  - "cloned_from provenance SHALL have a scheduling-time consumer" drops the named latest reader and keeps the rule
    against a latest-first resolution source.
  - "Both persistence planes SHALL normalise cloned_from_model_id identically before judging it" drops its last sentence
    about the publisher's latest-clone-row reader.
- `atomic-cutover-transaction`: no change; #3189 already removed the index-publish requirement.
- **Non-goal:** the "no manifest re-publish" mentions in `legacy-reactivation-guard`, `model-version-switch-rollback`
  and `direct-grid-variant-registration` stay. They remain true statements.

## Non-goals

- `variant-activation-cutover` "Cutover reuses the existing single-transaction activation lifecycle" (`:13`) and
  "Permanent retirement" (`:65-69`) stay. They describe the DB active-model authority, and the consumer-side
  `lifecycle_state` filter on manifest rows. Both remain true, because the node-22 tooling publishes a manifest derived
  from that DB state.

- The node-22 succession and merged-publish tooling, provider refresh, and refresh timers.
- `get_earliest_clone_row_for_model_source` and the lineage resolver.

## Evidence Floor

- [x] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`.
- [ ] E2 Zero hits for:

  ```
  git grep -nwE "register_post_commit_manifest_publisher|register_post_commit_state_index_publisher|_dispatch_post_commit_manifest_publish|_dispatch_post_commit_state_index_publish|_post_commit_manifest_publisher|_post_commit_state_index_publisher|_default_no_op_manifest_publisher|_default_no_op_state_index_publisher|PostCommitPublishContext|PostCommitManifestPublisher|PostCommitStateIndexPublisher|_should_publish_manifest_after_commit|_extract_source_scope|build_default_state_index_publisher|StateIndexPublishFailedError|state_clone_index_publisher|get_latest_clone_row_for_model_source|_ManifestPublisherStub|test_state_clone_index_publish|test_variant_activation_cutover_manifest|_LatestOnlyCloneRowRepo|_publisher_stub|_RollbackReadyHarnessStore|_AllowedDeactivateHarnessStore|test_real_clone_row_readers_disagree_about_a_null_fingerprint_row|test_db_plane_never_consults_the_publishers_latest_row_reader" -- . ':!openspec' ':!docs/review-loop-log.jsonl'
  ```

  Prose grep, zero hits apart from any hit the PR lists and justifies:

  ```
  git grep -nE "post-commit (manifest|state-index|publish)|state-index post-commit|publisher'?s .{0,3}DESC|DESC.{0,3} (reader|sibling)|latest-clone-row|manifest publisher stub|model lifecycle、|readers_disagree|re-publish cases|_manifest\` also" -- packages services scripts tests docs/runbooks
  ```
- [x] E3 Mutation reds on the retained tests, each restored:
  - swallow the audit-insert failure: the moved audit-rollback test goes red;
  - drop `MISSING_ACTIVE_RISK` for supersede of the active model: the moved supersede test goes red;
  - remove the already-current short-circuit: the dispatch repeat test goes red;
  - re-add a `register_post_commit_manifest_publisher` attribute: the new absence test goes red.
- [x] E4 Local `uv run pytest -q` over:
  - `tests/test_variant_activation_cutover.py`, `tests/test_variant_activation_cutover_dispatch.py`;
  - `tests/test_legacy_reactivation_guard.py`, `tests/test_scheduler_lineage.py`, `tests/test_model_registration.py`;
  - `tests/test_state_manager*.py`, `tests/test_select_ci_tests.py`;
  - the selector set for the diff.
- [ ] E5 node-27 temporary worktree: the E4 set plus the selector set plus `tests/test_real_database_integration.py`
  and `tests/test_model_activation_audit_integration.py` (the only remaining committed `rollback_version` coverage),
  with the real-DB integration env, `leftover_it_dbs=0`.
- [ ] E6 `openspec validate retire-post-commit-publishers --strict --no-interactive`.
- [ ] E7 The PR body names:
  - the orphan reader deletion, which goes beyond the issue's list;
  - the issue's stale spec reference (`atomic-cutover-transaction` was already handled by #3189);
  - the archive-PR Purpose task;
  - the E2 prose-grep hits.
