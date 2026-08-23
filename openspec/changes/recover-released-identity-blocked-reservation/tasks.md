## 1. Fixture And Contract

- [ ] 1.1 Add the OpenSpec delta requiring an operator-gated recovery path and a mandatory release-time operator signal.
- [ ] 1.2 Review the fixture for the selected risk packs (Slurm production lifecycle / mock-vs-real parity; run manifest / QC provenance).
- [ ] 1.3 `openspec validate recover-released-identity-blocked-reservation --strict --no-interactive` PASS.

## 2. Recovery Path

- [x] 2.1 Add the typed recovery API on the journal, minting through the existing `_next_current_master_retry_identity` helper (no second derivation site).
- [x] 2.2 CAS-guard it on expected submission attempt + attempt anchor, mirroring `release_identity_blocked_reservation` (`file_orchestration_journal.py:3267-3346`).
- [x] 2.3 Preserve `cohort_digest` and `cohort_members` onto the successor unchanged.
- [x] 2.4 Refuse every shape outside released/unbound/current-contract-master.
- [x] 2.5 Do not consult `should_auto_retry`; do not write `error_code`; leave no automatic caller.
- [x] 2.6 Refuse a **repeat** invocation on an already-recovered row (the CAS guard does not cover it — minting leaves the source row's attempt fields untouched), so one released row can never yield two successors.
- [x] 2.7 Perform no Slurm-side liveness/absence check; the call is an operator attestation (stated non-goal).

## 3. Operator Signal

- [x] 3.1 Emit the `IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR` record **once**, at the single release write point `release_identity_blocked_reservation` (`file_orchestration_journal.py:3267-3346`, decision at `:3338`).
- [x] 3.2 Do **not** also instrument the `reconcile.py:2135` caller — it is the sole caller, so a second emission there double-emits on every production release. (See D8: the "two write points" premise of the first draft was false.)
- [x] 3.3 Record names job id, cohort digest, and `identity_blocked_streak`.

## 4. Regression Evidence

- [x] 4.1 Red-first test: recovery mints exactly one successor with the next `_retry_<n>` identity and preserved cohort identity.
- [x] 4.2 Test: refusal for each non-owned shape.
- [x] 4.3 Test: CAS mismatch makes no write.
- [x] 4.4 Test: the token is emitted for a released row arriving from a **fresh** reservation and for one arriving from a `reclaim_pipeline_job_reservation` **re-seed** — the two prior-state shapes `tests/test_production_scheduler.py:48632`/`:48681` distinguish — and exactly once per release.
- [x] 4.6 Test: a second recovery invocation on an already-recovered row is refused and writes nothing.
- [x] 4.5 `tests/test_production_scheduler.py:48632` and `:48681` pass **unweakened** — assertions unchanged.

## 5. Verification (Evidence Floor)

- [x] 5.1 `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py` PASS.
- [x] 5.2 `uv run ruff check .` PASS.
- [x] 5.3 `openspec validate recover-released-identity-blocked-reservation --strict --no-interactive` PASS.
- [ ] 5.4 node-22 runtime receipt: scheduler behavior changed, so a post-deploy pass SHALL be observed on node-22 with the release signal reachable (or an explicit statement that no released row occurred in the window, since the shape is rare — 4 in 4487 rows).
