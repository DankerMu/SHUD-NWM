## 1. Supported State-Index Repair Boundary

- [x] 1.1 Add one public state-manager repair helper for the fixed private/reference and shared/destination index topology: root alias/overlap guards, reference-then-destination lock order, bounded per-lane snapshots, production structural validation with rebuilt checksum, per-lane pre-images/archives, source-first CAS, canonical publication, and production read-back.
- [x] 1.2 Preserve each lane's own raw surviving entry mappings/order; implement explicit-lane checksum-only repair and coordinated exactly-one removal by `state_id`, `run_id`, or complete `(model_id, source_id, valid_time)` selector; absent/multiple/cross-lane-divergent matches must be stable zero-index-write refusals unless an exact missing-lane flag records the one-lane disposition.
- [x] 1.3 Add `scripts/scheduler_state_index_repair.py` with two production roots and fixed derived index paths, dry-run default, explicit enforce, owner-private archive/receipt roots, bounded per-lane JSON output, and exit `0` success / `2` proven zero-index-write refusal / `3` partial, committed, or uncertain incomplete.

## 2. Requirement-Driven Tests

- [x] 2.1 Add state-manager tests proving the checksum-invalid payload is accepted only through repaired production checksum semantics, structural/schema/URI/limit defects still refuse, and existing publish defaults/object verification remain unchanged.
- [x] 2.2 Add CLI tests for no-mutation two-lane dry-run; explicit-lane checksum preservation with sibling bytes unchanged; each unique selector across two lane-specific entry sets; missing-lane opt-in; zero/multiple/cross-lane mismatch refusal; both archives before first write; and stable private per-lane receipt/archive evidence.
- [x] 2.3 Inject reference/destination archive failures, first/second CAS precommit and replace-uncertain failures, lock release, per-lane post-write read-back, and receipt failures; assert source-first partial outcomes are exit 3, no false refusal follows any possible mutation, and no undeclared half-result or copyback resurrection remains.
- [x] 2.4 Add a concurrency regression proving repair and copyback share reference-then-destination lock order and terminate without deadlock or stale CAS publication.
- [x] 2.5 Run the new-behavior tests red against pre-change source in one batched red-proof, restore immediately, and leave no `red-proof` stash.

## 3. #1482 Terminal Disposition And Operations

- [x] 3.1 Finalize option (c) in the active retry spec/source wording: projection annotations remain non-durable, the production marker writer does not emit their gate keys, and the paired conservative over-pin is an accepted permanent limitation.
- [x] 3.2 Verify existing paired disclosure and legacy marker tests still pass without changing retry runtime code, closed row schema, marker bytes, or decision semantics.
- [x] 3.3 Extend `docs/runbooks/current-production-ops.md` with checksum-mismatch symptoms, the private/reference versus shared/destination roles, two-root dry-run, coordinated removal or explicit-lane checksum enforce, both-lane archive/read-back flow, source-first partial-completion recovery, exit-code handling, all-writer freeze requirement, and a strict prohibition on hand-editing or whole-file cross-lane copying.

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_state_manager.py tests/test_scheduler_state_index_repair.py tests/test_scheduler_state_index_copyback_replay.py` passes (202 passed).
- [x] 4.2 `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py` passes for #1482 contract/legacy coverage (2563 passed, 1 skipped).
- [x] 4.3 `uv run ruff check .` passes.
- [x] 4.4 `openspec validate scheduler-state-index-repair --strict --no-interactive` passes.
- [ ] 4.5 On node-27, run the focused backend tests as the backend oracle after ff-only deployment. No live DB or display receipt is required because this change touches no DB/display surface.
- [ ] 4.6 On node-22, using the checked-in wrapper or exact active `.venv/bin/python` only (never `uv sync` or bare `uv run` before the maintenance window), create disposable private/reference and shared/destination roots with lane-distinct valid indexes, corrupt only one checksum, record a zero-mutation two-lane preview, then enforce explicit-lane repair and record the receipt plus byte-unchanged sibling proof. No Slurm submission or production index write is required.
