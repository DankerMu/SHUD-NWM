## Why

Epic #2919 (entropy-burndown-2026-10) left out-of-scope findings that were filed at epic end as #3025, #3027–#3032 (#3026 waits on #2930 and is not part of this change). Each one is small or medium and can be verified locally or in CI, so none waits on node-27. This change is the shared OpenSpec fixture for those seven issues. Each issue lands as its own PR, in this order: #3028, #3032, #3027, #3029, #3030, #3025, #3031.

## What Changes

- #3028: the CI selector routes journal facade and `file_journal_*` sibling edits to the 10 suites that import the facade in a function body and patch a facade global directly. A disk-globbed parametrised pin covers every sibling. Four further cases found in review (two redirect-suite nodes and two suites patching through `tests/file_journal_legacy_seed_helpers.py`) are tracked in #3034.
- #3032 (hybrid, see design.md): the 9 first-import failures that a pure import move or the single lazy `scheduler.ProductionSchedulerConfig` fixes are fixed. A guard test imports every `services/orchestrator` module first in a fresh interpreter and holds the remaining 8 in an exact ratcheting allowlist, which gets a follow-up issue. The import-order workaround fixture in `tests/test_preserve_final_component_loop_spelling.py` goes.
- #3027: restore the 10 selector comments corrupted by the `" mar"` deletion in a0b6ccf9d.
- #3029: fix 5 comments/docstrings that still name renamed or moved symbols.
- #3030: delete the dead `_mapping_has_runtime_root_fields` and its cluster-map row.
- #3025: store-level tests that pin `apiErrorCode` to the envelope `error.error.code`.
- #3031: make `tests/test_node27_working_set.py` independent of a host whose `/home` is a symlink.

## Triage (one block per issue)

```text
#3028  Issue type: test | Fixture level: compact | Upstream: absent
       Blast radius: PR-lane test selection for journal edits (CI only; no runtime); scope 2 → 10 suites per fixture review
       Risk packs: Resource limits / discovery (selected: disk-glob parameter source must be non-empty)
       Evidence floor: selector output per path + parametrised pin + red-when-glob-removed drill
#3032  Issue type: refactor | Fixture level: expanded | Upstream: absent
       Blast radius: import-time behaviour of the scheduler/chain facades used by every production entry point
       Risk packs: Public API / CLI / script entry; Legacy compatibility (facade names, monkeypatch targets); Concurrency / shared state / ordering (module init order)
       Evidence floor: fresh-interpreter import guard over all orchestrator modules + facade/entry-point imports + targeted scheduler/chain suites
#3027  Issue type: refactor (comments) | Fixture level: none | Evidence floor: grep empty + selector tests green
#3029  Issue type: refactor (comments) | Fixture level: none | Evidence floor: the issue's five greps + touched tests green
#3030  Issue type: refactor | Fixture level: compact | Blast radius: cluster-map/facade hygiene tests | Evidence floor: issue Verification line
#3025  Issue type: test | Fixture level: compact | Blast radius: frontend tests only | Evidence floor: new cases + top-level-code mutant red
#3031  Issue type: test | Fixture level: compact | Blast radius: one test fixture | Evidence floor: macOS 3/3 green + Linux CI green
```

`design.md` covers #3032 only. The other six are `none` or `compact` and need no design.

## Impact

- Code: `scripts/select_ci_tests_{tables,rules,imports}.py` (#3027 comments, #3028 one table), `services/orchestrator/{chain,scheduler}.py` and the failing split modules (#3032), `services/orchestrator/file_journal_{paths,retry_helpers}.py` (#3029 comment, #3030 deletion), `apps/api/response_models/best_available_selection.py` (#3029 docstring).
- Tests: `tests/test_select_ci_tests.py`, a new orchestrator import guard test, `tests/fixtures/file_journal_cluster_map.json`, `tests/test_node27_working_set.py`, frontend store tests, and docstrings in three test files.
- No runtime behaviour change, no schema/API change, no node-22 or node-27 action.
