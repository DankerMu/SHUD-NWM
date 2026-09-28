Fixture level: expanded
Project profile: NHMS
Upstream suggested level: absent
Issues: #2668
Minimal mergeable slice: 1-2 (the bind exit) is the fix; 3 (listing) makes it discoverable; 4 docs ride along.

## 1. Typed bind CAS (file journal)

- [x] 1.1 Add the operator-verified bind on `FileOrchestrationJournalRepository`, per design.md Decisions 1–3:
  - exact held-tuple CAS, attempt-window check, and the typed-commit refusal map;
  - reuse the #2655 durable bind tuple (no new token);
  - write the audit event in the same durable append;
  - write zero bytes on any refusal.

  Trace the unversioned forecast-master writers (`chain_forecast_orchestrator_cycle.py:716`, retry clones `file_orchestration_journal.py:11443`/`:11780`). If a clone can become `reserved` with `cohort_members` and no version, stop and report. Otherwise record the trace in the PR.
- [x] 1.2 Reuse `reconcile._submitline_comment_key` for the key and the #2655 key-narrowed claimant scan for exclusivity.

## 2. CLI `bind-reserved-job`

- [x] 2.1 `services/orchestrator/operator_reserved_bind.py` holds the shared callable, Click and argparse registration, `--confirm` gating, the journal-root authority, input validation, and a stable redacted JSON receipt. Register it in `cli.py` the same way demote is registered.

## 3. Evidence + listing

- [x] 3.0 Add `submission_attempt_started_at` to serialized reserved-unbound outcomes, and to the bounded key set so it survives compaction (design Decision 5).

- [x] 3.1 `operator_action_listing.py`: `held_reservation_unresolved` per design.md Decision 6:
  - the closed per-action table plus its closure pin test;
  - `(job_id, decision)` dedup;
  - `restart_reconcile_unscanned_passes`;
  - `LIST_OPERATOR_ACTIONS_HELP` updated.

  Keep the existing literal-decision contract and its tests unchanged.

## 4. Docs

- [x] 4.1 `failed-basin-retry.md` case 2 (including the genuine double-submission rule: the other master must be terminal or cancelled, and the note records it) ("no supported operator bind command") → the `bind-reserved-job` procedure: sacct query for `JobID,JobName,State,Submit,SubmitLine`, how to choose the master in a genuine double submission, preview of the CAS inputs, and post-bind expectations. Also cover (c) escalation.
- [x] 4.2 `scheduler-dbfree-typed-reasons.md` #2666 section: exit list gains `bind-reserved-job`; `list-operator-actions` shows `held_reservation_unresolved`. Add the row to the `node22-control-plane-manual-recovery.md` operator-action table.

- [x] 4.3 PR deviation record for (c). Open follow-up issues for (c) and for the forcing-lane siblings (#2674 for (c), #2675 for the forcing lane).

## 5. Verification

- [x] 5.1 Tests for every Required evidence row in design.md. New-behavior tests must be red before the change and green after. The (a)/(b) happy paths must drive the real file journal plus `reconcile_inflight_jobs` plus `_cycle_completion_verdict`.
- [x] 5.2 Register the new suites in `scripts/select_ci_tests.py` and `tests/test_select_ci_tests.py`.
- [ ] 5.3 Run `uv run ruff check .` and the focused pytest suites (new suites, `test_operator_action_listing.py`, `test_orchestrator_demote_*.py`, `test_scheduler_held_reservation_block.py`, `test_gateway_reconcile_*.py`). Run the full `uv run pytest -q` on node-27. Run `openspec validate operator-verified-reserved-bind --strict --no-interactive`.
- [ ] 5.4 node-22 scratch-journal rehearsal (design.md Rollout) with its receipt under `docs/runbooks/receipts/`.

## Risk packs considered (core)

- Public API / CLI / script entry: selected - new operator command, both entrypoints (5.1).
- Config / project setup: not selected - no env or config change; the 6h constant is module-local.
- File IO / path safety / overwrite: selected via journal writes - typed CAS, zero-byte refusals, journal-root authority (5.1).
- Schema / columns / units / field names: selected - new listing decision literal and fields; new audit event type; the durable bind tuple is deliberately unchanged (5.1).
- Auth / permissions / secrets: selected - operator evidence redaction in the receipt and the event (5.1).
- Concurrency / shared state / ordering: selected - race against reconcile bind under the cycle lock; Slurm-id claimant exclusivity (5.1).
- Resource limits / large input / discovery: not selected - bounded CLI inputs; the listing scans existing evidence.
- Legacy compatibility / examples: selected - legacy unversioned refusal; existing listing and demote tests unchanged (5.1, 5.3).
- Error handling / rollback / partial outputs: selected - refusal matrix, post-commit projection warnings, rollback-safe tuple (5.1).
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected - 4.1, 4.2.

## Domain risk packs considered

- Slurm production lifecycle / mock-vs-real parity: selected - SubmitLine parsing reuse; node-22 rehearsal (5.4).
- Run manifest / QC provenance: selected - a wrong bind attaches the wrong provenance; key plus claimant proof (5.1).
- Geospatial, hydro-met windows, SHUD numerics, PostGIS/Timescale, providers, published artifacts, alerting lanes: not selected - untouched.
