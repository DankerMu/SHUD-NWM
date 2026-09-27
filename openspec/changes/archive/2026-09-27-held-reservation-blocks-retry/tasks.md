Fixture level: expanded
Project profile: NHMS
Upstream suggested level: absent
Issues: #2666
Minimal mergeable slice: 1 is the production fix; 2 runbook note rides along.

## 1. Held reservation blocks the candidate-state decision

- [x] 1.1 Add a held-reservation predicate: status `persistence.RESERVED_STATUS` AND no real Slurm binding, over every pipeline row present in the provider-filtered candidate state (`non_member` is already excluded there). Do NOT gate on `cohort_member_row_is_attributed` / `COHORT_MEMBER_ATTRIBUTED_STAGES`, which exclude `forecast`. `member`, `unwitnessed`, and `incomplete` rows all block. Reuse the existing constant; add no new status set.
- [x] 1.2 In `_candidate_state_decision_evaluated`, return `skip` / `active_duplicate_pipeline` (`decision: skip_active`, `active_status: reserved`, `held_reservations` evidence (at most `candidate_state_job_limit` rows, `_job_state_evidence` projection), `replacement_submitted: False`). Evaluate it after `active_slurm_job` and before every supersession, completed-stage, terminal, failure, and manual-retry branch. Make sure the row is visible in the view the predicate reads.
- [x] 1.3 Leave `ACTIVE_PIPELINE_STATUSES`, the journal, reconcile, and evidence schemas unchanged.

## 2. Operations docs

- [x] 2.1 `docs/runbooks/scheduler-dbfree-typed-reasons.md`: `active_duplicate_pipeline` with `active_status: reserved` means a held accepted-submit reservation. The exit is restart reconcile (bind / demote / release), never a manual retry marker. Cite #2666.

## 3. Verification

- [x] 3.1 Tests for every Required evidence row in design.md. New-behavior tests are red on pre-change source and green after.
- [x] 3.2 `uv run ruff check .`; focused `uv run pytest -q` over the scheduler state-decision suites, `tests/test_orchestration_chain.py`, and `tests/test_gateway_reconcile_*.py`; full `uv run pytest -q` on node-27; `openspec validate held-reservation-blocks-retry --strict --no-interactive`. — node-27 full @7aa8a591: 20904 passed / 365 skipped rc=0; local focused 4171 passed; CI green @d5618e3.
- [x] 3.3 node-22 scratch-journal rehearsal on the PR head (`7aa8a591`; receipt `docs/runbooks/receipts/2026-09-27-issue2666-held-reservation-rehearsal.md`): the held IFS 0925 12Z row binds to 57553 and no member is selected for forecast resubmission; zero sbatch.
- [x] 3.4 After merge (timer stopped since 2026-09-27T08:38:21Z): deploy, run the basin-18 manual retry, start the timer, and record the first live pass receipt: no duplicate forecast sbatch, and the IFS/gfs 0926 cycles are selected. — done 2026-09-27: deploy 56f2f745, first pass bound the held IFS row to 57553 with zero forecast resubmission, basin-18 marker re-ran only that basin, timer restarted 13:26:40Z, IFS/gfs 0926 00Z selected (receipt on #2666).

## Risk packs considered (core)

- Public API / CLI / script entry: not selected - no entry-point change.
- Config / project setup: not selected - no config or env.
- File IO / path safety / overwrite: not selected - no new IO; the collision it prevents is covered by the decision tests (3.1).
- Schema / columns / units / field names: not selected - additive evidence key only (`held_reservations`), reason literal unchanged.
- Auth / permissions / secrets: not selected.
- Concurrency / shared state / ordering: selected - ambiguous submit vs next-pass planner race; chain-level reproduction (3.1).
- Resource limits / large input / discovery: not selected - bounded evidence list.
- Legacy compatibility / examples: selected - bound, released, non_member, and real-binding rows keep pre-change decisions (3.1).
- Error handling / rollback / partial outputs: selected - fail-closed across every reconcile reason class (3.1).
- Release / packaging / dependency compatibility: not selected.
- Documentation / migration notes: selected - 2.1.

## Domain risk packs considered

- Slurm production lifecycle / mock-vs-real parity: selected - node-22 rehearsal + live receipt (3.3, 3.4).
- Run manifest / QC provenance, geospatial, hydro-met windows, SHUD numerics, PostGIS/Timescale, providers, published artifacts, alerting lanes: not selected - untouched surfaces.
