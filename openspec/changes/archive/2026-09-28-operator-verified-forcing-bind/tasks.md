## 1. Typed CAS

- [x] 1.1 Forcing branch in `bind_operator_verified_reserved_job` per design Decisions 1-3, dispatched before the contract checks, with its own locked writer; the owner rule is identical to inflight `_forcing_accounting_identity_matches` (design Decision 2). Add the new refusals `array_spec_mismatch` and `slurm_owner_mismatch` to `OPERATOR_BIND_REFUSALS` and the refusal map. The forecast path stays unchanged.
- [x] 1.2 `operator_verified_bind` audit event for forcing (lane, SubmitLine comment, array spec), written in the same append.

## 2. CLI + listing

- [x] 2.1 `operator_reserved_bind.py`: optional `--slurm-user` / `--slurm-account` on both entrypoints (required by the CAS for forcing rows with ownership); help and docstrings mention the forcing lane. Forecast inputs are unchanged.
- [x] 2.2 `operator_action_listing_held.py` forcing mapping (Decision 4) and `LIST_OPERATOR_ACTIONS_HELP`.

## 3. Docs

- [x] 3.1 `failed-basin-retry.md`: a forcing disposition subsection with:
  - the sacct query (`JobID,JobName,State,User,Account,Submit,SubmitLine`);
  - the mandatory `sacct --jobs=<id> --parsable2 -o JobID,JobName,User,Account,Submit,SubmitLine` re-read, taking every bind value from it (inflight does not catch a mistyped forcing id);
  - the double-submission rule (the other master must be terminal or cancelled);
  - the `array_spec_mismatch` and `slurm_owner_mismatch` refusal rows;
  - the listing rows for forcing;
  - the absence case → #2682.

  Update the #2666 section of `scheduler-dbfree-typed-reasons.md` and the table in `node22-control-plane-manual-recovery.md`.

## 4. Verification

- [x] 4.1 Tests F1-F12 plus F2b and F7b (design Required evidence), red before and green after, in the #2668 suites (`tests/test_orchestrator_bind_reserved_job_{cas,cli,lane}.py`, `tests/test_operator_action_listing_held_reservations.py`) or a new forcing suite registered in `scripts/select_ci_tests.py` / `tests/test_select_ci_tests.py`.
- [x] 4.2 `uv run ruff check .`; `openspec validate operator-verified-forcing-bind --strict --no-interactive`; the focused suites: the bind suites, listing suites (`tests/test_operator_action_listing.py`, `tests/test_operator_action_listing_held_reservations.py`), `tests/test_forcing_submit_ambiguity.py`, `tests/test_scheduler_held_reservation_block.py`, `tests/test_orchestrator_demote_core_cas.py`, `tests/test_gateway_reconcile_*.py`.
- [x] 4.3 node-27 focused at the PR head, plus the full `uv run pytest -q`. (focused at a2972fca6: 2021 passed; full at 989659520: 21291 passed, 365 skipped)
- [x] 4.4 node-22 scratch-journal rehearsal (design Rollout) with a receipt under `docs/runbooks/receipts/`.

## Risk packs considered (core)

- Public API / CLI / script entry: selected - the same command gains a lane (F10).
- Config / project setup: not selected.
- File IO / path safety / overwrite: selected - typed CAS, zero-byte refusals (F3-F8).
- Schema / columns / units / field names: selected - new refusal token, listing mapping, audit event fields; no new durable row token (F2, F11).
- Auth / permissions / secrets: selected - evidence redaction in the receipt and the event, as in #2668 (F2, F10).
- Concurrency / shared state / ordering: selected - claimant exclusivity and the race with the automatic bind (F7, F12).
- Resource limits / large input / discovery: not selected - bounded inputs; the scan is the #2668 bounded scan.
- Legacy compatibility / examples: selected - forecast path unchanged (F9).
- Error handling / rollback / partial outputs: selected - refusal matrix; rollback-safe tuple (F3-F8).
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected (3.1).

## Domain risk packs considered

- Slurm production lifecycle / mock-vs-real parity: selected - SubmitLine comment and array parsing against the gateway's real argv shape; node-22 rehearsal (4.4).
- Run manifest / QC provenance: selected - a wrong bind attaches wrong forcing products; attempt comment, array size and exclusivity (F5-F7).
- Geospatial, hydro-met windows, SHUD numerics, PostGIS/Timescale, providers, published artifacts, alerting lanes: not selected.
