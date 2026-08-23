## 1. Fixture And Contract

- [x] 1.1 Add OpenSpec delta requiring an operator-gated recovery attestation, end-to-end liveness, and a mandatory release-time operator signal.
- [x] 1.2 Review the fixture for the selected risk packs (Slurm production lifecycle / mock-vs-real parity; run manifest / QC provenance).
- [x] 1.3 `openspec validate recover-released-identity-blocked-reservation --strict --no-interactive` PASS.

## 2. Recovery Attestation

- [x] 2.1 Recovery API records a durable operator attestation on the released row and writes **no** successor pipeline-job row.
- [x] 2.2 Leave the `_retry_<n>` identity the ordinary path would mint **unoccupied**.
- [x] 2.3 Refuse every shape outside released / unbound / current-contract-master.
- [x] 2.4 Repeated attestation on the same row is idempotent.
- [x] 2.5 Do not consult `should_auto_retry`; do not write `error_code`; leave no automatic caller and no automatic way to set the attestation.
- [x] 2.6 Perform no Slurm-side liveness/absence check; the call is an operator attestation (stated non-goal).

## 3. Consuming The Attestation

- [x] 3.1 Admit the attestation as an **additive disjunct** at `_terminal_stage_needs_manual_retry` (`chain_forecast_orchestrator_cycle.py:171-183`).
- [x] 3.2 Leave `_verified_accepted_submit_forecast_retry` (`chain_forecast_orchestrator_cycle.py:923-931`) and the reclaim predicate (`file_orchestration_journal.py:2117-2170`) **byte-identical** — no widening, weakening, or reordering.
- [x] 3.3 The recovered attempt participates in ordinary candidate selection; do **not** carry the released row's member set forward.

## 4. Operator Signal

- [x] 4.1 Emit `IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR` **once**, at the single release write point `release_identity_blocked_reservation` (`file_orchestration_journal.py:3294-3400`, decision at `:3365`). Already delivered in `54714525` — keep it.
- [x] 4.2 Do **not** also instrument the `reconcile.py:2135` caller (sole caller — would double-emit).
- [x] 4.3 Record names job id, cohort digest, and `identity_blocked_streak`.

## 5. Regression Evidence

- [x] 5.1 **Red-first, the decisive one**: after recovery, an ordinary pass mints `_retry_<n>`, creates the reservation (`created=True`, not `already_inflight`), and reaches the submission call. This is the oracle whose absence let the first, inert implementation pass 2377 tests.
- [x] 5.2 Test: recovery writes no row and leaves the `_retry_<n>` identity free.
- [x] 5.3 Test: without the attestation, the stage behaves exactly as today and no submission occurs.
- [x] 5.4 Test: refusal for each non-owned shape; repeated attestation idempotent.
- [x] 5.5 Test: both release prior-state shapes emit the token exactly once (keep from `54714525`).
- [x] 5.6 `tests/test_production_scheduler.py:48632` and `:48681` pass **unweakened** — assertions unchanged.

## 6. Verification (Evidence Floor)

- [x] 6.1 `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py` PASS.
- [x] 6.2 `uv run ruff check .` PASS.
- [x] 6.3 `openspec validate recover-released-identity-blocked-reservation --strict --no-interactive` PASS.
- [ ] 6.4 node-22 runtime receipt: scheduler behavior changed, so a post-deploy pass SHALL be observed on node-22 (or an explicit statement that no released row occurred in the window — the shape is rare, 4 in 4487 rows).
