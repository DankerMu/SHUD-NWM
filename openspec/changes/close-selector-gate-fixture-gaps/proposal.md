## Why

Five same-family `select_ci_tests` / selector meta-guard defects, batched into one PR by explicit user instruction:

- **#2260** — `services/orchestrator/scheduler_runtime.py` and `packages/common/copyback_guard.py` are the two load-bearing modules of the retention copyback mutex, yet neither single-file diff selects `tests/test_retention_copyback_mutex.py` in the targeted PR lane (measured: 22 and 9 selections, mutex suite absent from both). `scheduler_runtime.py` hits a `stop_on_match=True` file-journal rule that shadows the orchestrator tree rule; `copyback_guard.py` is named by no rule.
- **#2191** — `services/precip/**` does not select `tests/test_node27_raw_retention.py`, a one-hop importer suite (`scripts/node27_raw_retention.py` imports `services.precip.constants.FILE_CACHE_DIR_ENV`; the suite holds that env name as a bare literal), so a consistent rename is green in the PR lane and red on master.
- **#2198** — `PathTestRule.only_when_any_changed` is read only inside the `CHANGED_TEST_FILE_RULES` loop; on a `PATH_TEST_RULES` row it is silently inert, and no table-level guard rejects it (the sibling `SUPPORT_MODULE_TEST_RULES` already has one).
- **#2230** — the #1656 meta-guard `test_supplemental_invariant_routing_reds_when_a_root_is_dropped` monkeypatches `TIMESCALE_WRITE_GUARD_INVARIANT_ROOTS` but never observes the patched attribute (deleting the monkeypatch stays green); `_invariant_scan_roots` takes the *first* `ast.Return` while its comment says *final*.
- **#2183** — the #1913 registry-partition guards freeze the full test-definition set of the seven `tests/test_basins_registry_import*.py` partitions. Correction to the issue premise (measured): an *amendment* path already exists — #1903 hand-patched one helper member through the `issue_1903_mapping_transition` record. What is missing is an *addition* path: appending any new test to a partition reds the collection, per-definition identity and execution-count guards (plus the integration guard for an integration test), and no record form can legitimise it.

## What Changes

- Selector (`scripts/select_ci_tests.py`): extend the `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` rule site that matches `scheduler_runtime.py` with the mutex suite; add a non-stop path-exact rule for `packages/common/copyback_guard.py` targeting the mutex suite; widen the `services/precip/**` rule's own target tuple with `tests/test_node27_raw_retention.py`; document on `PathTestRule` / `_rule_activated` that `only_when_any_changed` is honoured only on `CHANGED_TEST_FILE_RULES`.
- Selector meta-suite (`tests/test_select_ci_tests.py`): routing pins for the three new edges; table-level guard rejecting `only_when_any_changed` on every `PATH_TEST_RULES` row; load-bearing `select_tests` assertions after the #1656 monkeypatch; exactly-one-top-level-return assertion in `_invariant_scan_roots` with corrected comment; #1913 guards re-based onto `frozen ∪ registered additions` via a new tracked additions ledger (frozen oracle bytes untouched), with constructive red/green proofs.
- Retire the stale `.workplans/issue-1913/` generator references in the #1913 rationale and document the addition procedure there.
- Update the two archived Known-limit entries of `route-retention-deletes-through-copyback-mutex` that #2260 closes.

## Impact

- Affected specs: `ci-contract-baseline` (three new/modified routing requirements, one selector-table requirement, one partition-addition requirement).
- Affected code: `scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`, new `tests/fixtures/basins_registry_partition_additions.json`, archived tasks.md note. No production runtime code; no CI workflow change.
