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
- [x] 3.2 Leave `_verified_accepted_submit_forecast_retry` (`chain_forecast_orchestrator_cycle.py:923-931`) and the reclaim predicate (`file_orchestration_journal.py:2169-2222`) **byte-identical** — no widening, weakening, or reordering.
- [x] 3.3 The recovered attempt participates in ordinary candidate selection; do **not** carry the released row's member set forward.

## 4. Operator Signal

- [x] 4.1 Emit `IDENTITY_RELEASED_RESERVATION_NEEDS_OPERATOR` **once**, at the single release write point `release_identity_blocked_reservation` (`file_orchestration_journal.py:3346-3482`, decision at `:3417`). Already delivered in `54714525` — keep it.
- [x] 4.2 Do **not** also instrument the `reconcile.py:2135` caller (sole caller — would double-emit).
- [x] 4.3 Record names job id, cohort digest, and `identity_blocked_streak`.

## 4b. Operator Entry Point (who invokes this, and can they?)

This section exists because it was absent from the first three drafts, and that
absence is the root cause recorded in `.workplans/pr-1802/review/retro-round-3.md`.

- [x] 4b.1 Name the intended invoker of every mechanism this change adds, and show the path from that invoker to the effect. Mechanisms: the recovery API (invoker: a human operator on node-22), the consuming disjunct (invoker: the ordinary scheduler pass), the release signal (invoker: reconcile; audience: a human reading the journal).
- [x] 4b.2 Add a supported operator entry point in `services/orchestrator/cli.py`, following the existing operator-maintenance subcommand convention.
- [x] 4b.3 Cover **discovery**: the operator can enumerate rows in the released identity-blocked shape together with the CAS values the action requires. Without this the operator is stopped one step earlier — there is today no supported way to read `expected_submission_attempt` / `expected_submission_attempt_started_at` off a wedged row.
- [x] 4b.4 Cover **action**: perform the attestation; idempotent on repeat; refusals name which precondition failed.
- [x] 4b.5 Help text states plainly that no Slurm-side liveness check is performed and that invoking it is an operator attestation, not a proof.
- [x] 4b.6 No HTTP/API route; CLI only.

## 5. Regression Evidence

- [x] 5.1 **Red-first, the decisive one**: after recovery, an ordinary pass mints `_retry_<n>`, creates the reservation (`created=True`, not `already_inflight`), and reaches the submission call. This is the oracle whose absence let the first, inert implementation pass 2377 tests.
- [x] 5.2 Test: recovery writes no row and leaves the `_retry_<n>` identity free.
- [x] 5.3 Test: without the attestation, the stage behaves exactly as today and no submission occurs.
- [x] 5.4 Test: refusal for each non-owned shape; repeated attestation idempotent.
- [x] 5.5 Test: both release prior-state shapes emit the token exactly once (keep from `54714525`).
- [x] 5.6 `tests/test_production_scheduler.py:48713` and `:48762` pass **unweakened** — assertions unchanged.
- [x] 5.7 The **operator-facing path itself** is pinned by a test that goes through the entry point, not only the method behind it.
- [x] 5.8 CLI tests cover: attest + idempotent repeat (both entrypoints), each refusal naming its failing precondition, help text carrying the attestation-not-a-proof non-goal, `--attest` without `--job-id` refused, and the listing carrying the CAS values.
- [x] 5.9 The signal names the **invocable command**, not only the Python method, at every emission point — including the degraded failure trace, which is exactly when a human is reading by hand.

## 6. Verification (Evidence Floor)

- [x] 6.1 `uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py` PASS.
- [x] 6.2 `uv run ruff check .` PASS.
- [x] 6.3 `openspec validate recover-released-identity-blocked-reservation --strict --no-interactive` PASS.
- [x] 6.5 Re-anchor and content-verify every `file:line` in this fixture **as the last action before push** — open each cited line and confirm it says what the citation asserts. Range-checking is not verification (see design D4c: drift caused by this PR's own commits bit four times, and every range-only check passed while the citations were wrong).
- [ ] 6.4 node-22 runtime receipt: scheduler behavior changed, so a post-deploy pass SHALL be observed on node-22 (or an explicit statement that no released row occurred in the window — the shape is rare, 4 in 4487 rows).
