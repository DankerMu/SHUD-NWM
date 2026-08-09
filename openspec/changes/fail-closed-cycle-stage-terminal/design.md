# Design: fail-closed cycle-stage terminal handling (issue #1202)

## D1. Status-domain map (fixture-review round-1 corrected)

`StageRunResult.status` values that can enter `_run_cycle_chain`'s
per-stage judgment, with disposition:

| Status | Producer | Today | Post-change |
|---|---|---|---|
| `succeeded` | ArrayAggregation (`chain_types.py:186-191`), local publish (`chain_stage_execution.py:731`) | fall-through, advance | allowlist, advance (unchanged) |
| `complete`/`published` | NO current StageRunResult producer (P3-10) — allowlist superset by design | — | allowlist (inert) |
| `partially_failed` | ArrayAggregation | fall-through + `had_partial`/`last_partial_status` (`chain_stage_execution.py:907-909`) | unchanged (mechanics preserved verbatim) |
| `failed` | `chain_stage_execution.py:223`, durable overrides, poll timeout (`:1104`) | `:196` break | unchanged |
| `submission_failed` | `chain_forecast_submission.py:118` | `:196` break | unchanged |
| `reservation_lost` / `permanently_failed` | persisted rows via `resume_cycle_stage` (`chain_stage_execution.py:820`) | `:196` break | unchanged |
| `submit_result_ambiguous` / `reconcile_unverified` | `chain_stage_execution.py:391`, `:1091`, array accounting | `:213` break → `reconciling` | unchanged |
| `cancelled` | persisted/durable projection | `:223` break → failed | unchanged |
| `skipped_duplicate_submission` | `chain_forecast_submission.py:172` via `chain_stage_execution.py:238-244`; NEVER persisted as a job-row status (event only) — no resume-path source | **falls through, advances, cycle "succeeded"** | dedicated break → cycle terminal `skipped_duplicate_submission` |
| `reserved` (unbound, inside reconcile grace — `reconcile.py:1324-1356` keeps the row `reserved`; `reserved ∉ TERMINAL_JOB_STATUSES` `chain.py:206-214`; resume returns it verbatim) | resume path | **falls through, silently advances** (P2-6) | fail-closed backstop → `failed` + `UNRECOGNIZED_STAGE_STATUS` — disclosed intended delta 2 |
| anything else | none known (`pending`/`running` cannot escape `poll_cycle_stage_until_terminal`, `chain_stage_execution.py:960`) | would fall through silently | fail-closed backstop |

## D2. Loop rewrite shape (P2-7 corrected)

In `_run_cycle_chain` (`chain_forecast_execution.py:120-288`):

- Keep the three existing break branches and the `partially_failed`
  branch behavior-identical.
- Replace the implicit fall-through with:
  1. `skipped_duplicate_submission` → `pipeline_result =
     PipelineResult(status="skipped_duplicate_submission", ...)`;
     `break` — the SAME shape as existing terminal branches, so the
     span-counter population (`:255-258`) and `set_dispatch_ms`
     backfill (`:267-268`) still run, and the `:270-271` return fires.
     No retry scheduling, no downstream stage.
  2. status in the success allowlist
     (`TERMINAL_PIPELINE_SUCCESS_STATUSES` imported from
     `chain_runtime_utils.py:37`) → advance (today's implicit behavior
     made explicit).
  3. else → `pipeline_result = PipelineResult(status="failed",
     error_code="UNRECOGNIZED_STAGE_STATUS")`; `break`. Reachable
     today only via the `reserved`-unbound resume geometry (D1) —
     intended delta 2, plus future-leak guard.
- `final_status` computation (`:281-288`) untouched.

## D3. Counters, the invariant, and observability (P1-3 corrected)

- `_populate_stage_span_counters` (`:322-350`): explicit
  `skipped_duplicate_submission` arm → `submitted_count=0,
  failed_count=0`.
- **Counters exist ONLY when a `SchedulerPassTiming` collector is bound
  to the ContextVar** — done by `scheduler_execution.
  execute_candidate_cohort`, never by `orchestrate_cycle` alone
  (`scheduler_timing.py:66-83`). Therefore every counter/invariant
  anchor MUST bind a collector explicitly (idiom:
  `tests/test_scheduler_timing.py`) or drive through the scheduler
  seam; a bare `orchestrate_cycle` test cannot observe spans, and a
  counter red-proof stated against it would be unmeasurable, not red.
- Invariant (anchored, P2-8 guarded; span record field names are
  `stage_name` / `basin_count` — r2 note): for every stage span with
  `basin_count > 0`, `failed_count == basin_count` implies the cycle
  terminal is NOT in `TERMINAL_PIPELINE_SUCCESS_STATUSES`.

## D4. Evidence-plane alignment and wiring (r1 P1-1 + P2-9, re-derived per r2 P1-1/P1-2/P2-3/P2-4)

1. **Quality predicate**:
   `_is_non_submitted_terminal_or_unavailable_status`
   (`scheduler_candidate_quality.py:374-386`) gains exact member
   `skipped_duplicate_submission`. FIVE consumers, each with a ruled
   effect (r2 P1-1 — the r1 draft audited only three):
   - `final_candidate_success: False`
     (`scheduler_candidate_execution_evidence.py:714-716`) — intended;
   - `quality_flag` non-ok (`scheduler_candidate_quality.py:190`) —
     intended;
   - `candidate_not_successful` residual blocker restored
     (`:230-243`) — intended;
   - `_is_partial_candidate_evidence`
     (`scheduler_candidate_runtime.py:644-648`) — item counts as
     partial → behavior delta 3, ruled INTENDED (see D4.2);
   - `_candidate_status_from_outcome` path
     (`scheduler_candidate_execution_evidence.py:762`) — provably
     inert: task-outcome statuses are confined to
     `{active, failed, cancelled, unavailable}`
     (`chain_array_accounting.py:126-138`), the skip status never
     appears there (r3 note — the r2 draft mislabeled `:743`, which
     consumes `_is_partial_candidate_evidence`, not the predicate).
2. **Pass-status plane (behavior delta 3, disclosed)**:
   `_scheduler_pass_status_from_execution`
   (`scheduler_candidate_runtime.py:609-620`) itself unchanged, but
   through `_is_partial_candidate_evidence` a MIXED-geometry pass
   (earlier stages submitted, forecast skipped — the #1164 shape) now
   projects `submitted_partial` with `partial_count ≥ 1`.
   `submitted_partial ∈ SCHEDULER_REVIEW_BLOCKED_STATUSES`
   (`readiness_scheduler_evidence.py:69-85`): the pass becomes
   review-visible. RULED INTENDED: it submitted real work, then
   deferred its terminal stage — that IS partial, and silently-green
   was the #1202 disease. A WHOLLY-skipped pass reports
   `skipped_duplicate_submission` at pass level (no submitted item;
   item status confirmed r3 — `candidate_outcomes` leaves active
   basins `"active"`, `chain_array_accounting.py:157`, and the
   `→failed` remap `:158-159` is gated on `final_status=="failed"`,
   which the dedicated terminal avoids) and is not in
   `SCHEDULER_LIVE_WORK_STATUSES` — it did no work; via D4.3(a) it
   joins `SCHEDULER_REVIEW_BLOCKED_STATUSES`, so it too is
   review-visible rather than vocabulary-rejected.
   **Live-proof ripple (run-2 r1 P2-3, ruled INTENDED)**: because
   `submitted_partial` and the skip status are both outside
   `SCHEDULER_LIVE_WORK_STATUSES` (`readiness_scheduler_evidence.py:
   125`), a skip-carrying pass also (a) trips
   `scheduler_status_not_live_eligible` on the live-proof channel
   (`readiness_scheduler_live_proof.py:236-239`; semantics already
   pinned by `tests/test_production_readiness_validation.py:3567/
   :3606`) and (b) silences the live-count channel
   (`readiness_scheduler_evidence.py:1028`). Both
   correct: a pass that deferred to another pass's live job is not
   itself live-green evidence.
3. **Readiness plane — pass-status VOCABULARY + partial recognizers,
   NOT the compatibility map (r2 P1-2, completed per r3 P1-1)**: the
   new terminal manufactures THREE readiness errors, with two distinct
   roots:
   - `status_not_allowed` (`readiness_scheduler_evidence.py:486-490`):
     the brand-new pass-level status is in neither
     `SCHEDULER_REVIEW_PASSED_STATUSES` (`:57-68`) nor
     `SCHEDULER_REVIEW_BLOCKED_STATUSES` (`:69-85`), and this check
     has no `endswith` fallback. Root: vocabulary.
   - `partial_count_exceeds_model_run_evidence` (wholly-skipped): a
     CAPACITY error, not a recognizer error (r3 probe) —
     `_scheduler_pass_uses_model_run_count_capacity` (`:994-995`)
     keys off REVIEW_BLOCKED membership / `_blocked`/`_failed`
     suffixes, so a skip-status pass gets
     `capacity = submitted_count = 0` and `:937-938` `continue`s
     before any recognizer runs. Root: vocabulary.
   - `partial_count_status_cardinality_mismatch` (mixed): the
     validators' recount finds zero partial rows. Root: recognizers.
   Fix, both edits (r3 probe: together they clear all three, `[]`
   from `_scheduler_count_cardinality_errors` and no
   `status_not_allowed`):
   (a) `SCHEDULER_REVIEW_BLOCKED_STATUSES` (`:69-85`) gains
   `skipped_duplicate_submission` — a skip-carrying pass is
   review-visible, extending behavior delta 3 to the wholly-skipped
   shape (ripples audited: `_scheduler_pass_uses_producer_partial_
   count` `:1012-1017` now honors producer partial counts for such
   passes; `_scheduler_readiness_status` `:513` maps them to the
   blocked/review readiness state — both INTENDED, silently-green was
   the disease);
   (b) `_scheduler_model_run_partial_status` /
   `_scheduler_model_run_producer_partial_status` (`:1158-1174`)
   learn the status, keeping producer partial-counting and readiness
   recount in agreement for the mixed shape.
   `SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY` (`:126-145`) is
   deliberately NOT extended: its derived set
   (`SCHEDULER_LIVE_COMPATIBLE…`, `:146-150`) feeds the
   submitted-inference at `:1070`; adding the skip status would let a
   bare skip row infer `submitted=True` — weakening a real guard
   (probe-verified r2, re-confirmed r3: the vocabulary route does not
   touch it). An anti-weakening anchor pins the guard (D5.5c-iii).
   **Disclosed residual (r3, failure mode corrected run-2 r1
   P2-4)**: readiness re-validation of a HISTORICAL live pass
   artifact (pre-change producer: pass status `submitted` with a
   stage-level skip entry in `stage_statuses` — today's normal
   overlap shape) flips from `[]` to THREE acceptance errors
   (`partial_count_status_cardinality_mismatch`,
   `live_status_model_run_blocked_outcome`,
   `submitted_status_model_run_status_mismatch`), turning the item
   `passed → blocked` and release readiness false. RULED ACCEPTED:
   such an artifact is precisely the #1164-shaped overlap whose
   green was unearned; blocking it forces re-review/regeneration
   instead of trusting stale evidence. No such fixtures exist
   in-repo (grep-verified) — live-artifact-only; the PR discloses
   this with the corrected failure mode and the operator remedy
   (regenerate the pass artifact under the new producer).
4. **Skip-evidence transport — from the returned `PipelineResult`,
   never the shared instance attribute (r2 P2-3/P2-4)**: the
   orchestrator instance is shared across `ThreadPoolExecutor` workers
   (default bound 4); a clear-at-entry design is exactly the
   attribute-stash race deleted in #861
   (`scheduler_execution.py:518-527` documents it). Instead the
   evidence projection reads the cycle's returned `PipelineResult`
   stage results — entries with the skip status yield
   `{stage, job_type, pipeline_job_id}` dicts. Thread-safe by
   construction (each cohort call owns its result), and scope is
   honest: `orchestrate_cycle` runs ONCE PER COHORT, so the key is
   cohort-scoped and fans out to every candidate of the cohort —
   identical duplication semantics to the existing `stage_statuses`
   projection (r2 P2-4: per-candidate attribution was a fiction; we
   do not claim it). The in-memory `duplicate_submission_skips` list
   stays append-only, never cleared, no new production reader — the
   issue-AC4 "production consumer" is the artifact projection above;
   recorded deviation (tasks).
5. **Wiring seam**: `scheduler_execution.py:716` (orchestrator handle
   live) → `:771-775` evidence build; `candidate_execution_evidence`
   is an injectable Callable (`scheduler_execution.py:121`, sole
   production wiring `scheduler_core.py:386`) — and NO signature
   change is needed (r3 note): the cohort `PipelineResult` is already
   argument 1, and `stage_statuses` is precedent-derived INSIDE the
   callee from `result.stages`
   (`scheduler_candidate_execution_evidence.py:564-565`); the skip
   projection is derived the same way (field name is
   `PipelineResult.stages`, `chain_types.py:145-150`). Projected
   dicts `{stage, job_type, pipeline_job_id}` deliberately drop
   `idempotency_key`/`reservation_status` — those stay on the
   in-memory dicts and the `submission_skipped` event
   (`chain_forecast_submission.py:141-148`); disclosed. Item key
   `duplicate_submission_skips`: SCOPED to cycle-derived items
   (run-2 r2 P1-1 — the r1 "all four shapes" ruling was wrong
   against the code). The real `model_run_evidence` item-producer
   inventory is ~10 shapes across 3 files: seven callers of
   `_candidate_model_run_review_evidence` (3 in
   `scheduler_execution.py` incl. the OUTPUT_URI_UNAVAILABLE shape
   `:578-600`, 4 across `scheduler_candidate_execution_evidence.py`
   `:298-445` / `scheduler_evidence.py:534-609`), the two forcing
   builders (`scheduler_candidate_execution_evidence.py:590-659` —
   their `ForcingProductionResult` has NO `stages` field,
   `workers/forcing_producer/producer.py:295-306`), and
   `sync_candidate_evidence_write_blocked_evidence`
   (`scheduler_evidence.py:611-641`, bypasses the shared helper).
   Ruling: the key appears ONLY on the cycle-derived shape —
   `_candidate_execution_evidence` (`:558-587`) /
   `_candidate_execution_evidence_item` (`:662-756`), derived inside
   the callee from `result.stages` exactly like `stage_statuses`
   (`:564-565`); list, empty when the cycle had no skips. ALL
   non-cycle shapes (preflight/secret-manifest blocked,
   evidence-write/sync-write blocked, forcing, output-uri) OMIT the
   key — they never had a cycle result, and no readiness consumer
   requires the key (additive, named-field validators). Single edit
   site; `scheduler_execution.py` is therefore NOT in the affected
   set. **Contract decision (P2-9)**: additive scoped-optional key
   WITHOUT
   bumping `MODEL_RUN_EVIDENCE_SCHEMA_VERSION`
   (`scheduler_evidence.py:22`) — no `schemas/` JSON-schema governs
   the pass artifact and readiness validators check named fields only;
   recorded here as the explicit ruling.
6. **Durable cycle status (P2-9)**: the skip terminal does NOT call
   `update_forecast_cycle_status` — the reservation-holding pass owns
   durable cycle progress; a deferring pass writing failure would
   fight the active pass. Recorded non-change.

## D5. Test plan (P1-2/P1-3 corrected geometries)

1. **Chain-level skip terminal (AC1/AC2, red pre-change)** — geometry
   note (P1-2): a naively preloaded reservation row is intercepted by
   `_find_existing_stage_job` (`chain_forecast_execution.py:134` →
   `chain_forecast_cycle.py:474-478` prefers non-terminal matches) and
   resumes the OTHER pass's job — never reaching the reserve gate; the
   single-point test survives only because it calls
   `_submit_and_wait_cycle_stage` directly. The anchor must therefore
   construct a reservation that COLLIDES on the idempotency key while
   staying INVISIBLE to the per-cycle job query (e.g. reserve the key
   in the store WITHOUT binding a queryable job row for this
   cycle/run; `_reserve_cycle_stage` consults the reservation store,
   `_find_existing_stage_job` consults job rows — split state is
   constructible with `_pipeline_store`/`_RaceSemanticsCycleRepository`
   helpers, `tests/test_orchestration_chain.py:8517/:12401`). The
   PIPELINE_ALREADY_ACTIVE preflight (`chain_forecast_control.py:
   118-130`) must also be avoided (the cited idiom's `model_id=None`
   short-circuit is one path; an explicit geometry is better). The
   test MUST positively prove the skip path executed (e.g.
   `duplicate_submission_skips` non-empty / the skip event recorded)
   so a false-red cannot masquerade. Assert: cycle
   `result.status == "skipped_duplicate_submission"` (RED pre-change:
   status is a member of `TERMINAL_PIPELINE_SUCCESS_STATUSES` —
   literal `"complete"` under the default harness, r2 note); no
   downstream submit calls (RED pre-change).
2. **No successor state**: same geometry — `state_save_qc` never runs.
3. **Counter + invariant anchor (AC3)**: bind a
   `SchedulerPassTiming` collector (idiom
   `tests/test_scheduler_timing.py` / scheduler seam), drive the skip
   geometry, assert the skipped stage span records `0/0` (pre-change,
   with collector bound, records `failed_count == basin_count` — the
   measurable red) and the D3 invariant over all spans (field names
   `stage_name`/`basin_count`).
4. **Fail-closed backstop (delta-2 anchor)**: reserved-unbound resume
   geometry (or a monkeypatched unknown status) → cycle terminal
   `failed` + `UNRECOGNIZED_STAGE_STATUS`, no downstream advancement.
   RED pre-change (silent advance). Geometry caveat (r2 note): the
   PIPELINE_ALREADY_ACTIVE preflight (`chain_forecast_control.py:
   118-130`) fires before the loop for an active row — construct with
   the `model_id=None` short-circuit or a row shape the preflight
   query does not match.
5. **Evidence-plane anchors (re-keyed r2 P1-2; 5(c) anchors are
   HOSTED in `tests/test_production_readiness_validation.py`, the
   readiness guard suite — run-2 r1 P1-1)**: (a) candidate item
   for a skipped cycle: `final_candidate_success is False`, non-ok
   quality flag, `candidate_not_successful` residual present,
   `duplicate_submission_skips` non-empty; (b) normal pass: key
   present and empty, AND a non-cycle item shape (e.g. a
   preflight-blocked or forcing item) omits the key — the scoped
   absence is anchored, not accidental (run-2 r2 P1-1); (c)
   readiness, three sub-anchors keyed to what
   the producer ACTUALLY emits post-delta-3:
   (i) mixed geometry → pass `submitted_partial`, `partial_count=1`,
   and readiness reports NO `partial_count_status_cardinality_
   mismatch` (RED pre-fix — recognizer root);
   (ii) wholly-skipped → NO `status_not_allowed` and NO
   `partial_count_exceeds_model_run_evidence` (RED pre-fix on BOTH —
   vocabulary root, cleared by the `SCHEDULER_REVIEW_BLOCKED_STATUSES`
   membership, r3 probe);
   (iii) anti-weakening: a bare `skipped_duplicate_submission`
   model-run row still infers `submitted=False` (`:1070` guard
   intact, GREEN both sides — proves the compatibility map was not
   extended).
6. **Reserve-gate single-point regression (AC5)**: existing
   `test_overlapping_pass_does_not_double_submit_real_submit_path`
   (`:12427-12494`) green untouched.
7. **Partial-success non-regression**: existing `:4662`/`:4688` green
   — proves the inversion preserved the `had_partial` path.

Red-proof protocol: extract the pre-change tree (`git archive` of the
branch-point SHA), fresh `uv sync --all-extras --dev` (worktree venv
finder leaks — never reuse), overlay new tests, record: anchor 1 red
on both assertions (pre-change literal `"complete"`), anchor 3 red on
the counter (collector bound), anchor 4 red (silent advance), anchor
5(a) red (`final_candidate_success is True` pre-change), 5(c)(i)/(ii)
red pre-fix (cardinality mismatch; status_not_allowed + capacity
overflow); 5(c)(iii) green BOTH sides (guard-preservation anchor, not
a red-proof); anchors 6/7 green.

## Evidence floor

- `uv run pytest -q tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_scheduler_timing.py tests/test_production_readiness_validation.py`
  (chain + artifact + counters + readiness; run-2 r1 P1-1 fix — the
  readiness guard suite is `test_production_readiness_validation.py`
  per `services/production_closure/AGENTS.md:87-92` and
  `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md:257`;
  `test_production_slurm_validation.py` tests the slurm_validation
  CLI and never touches `readiness_scheduler_evidence` — it was the
  wrong file. Baseline: 338 passed / 2 skipped.)
- `uv run ruff check .`
- `openspec validate fail-closed-cycle-stage-terminal --strict --no-interactive`
- Sweep: `grep -rn "skipped_duplicate_submission"` — every hit mapped
  to a disposition.

## Non-goals

Reserve-gate logic; retry machinery; `TERMINAL_PIPELINE_SUCCESS_
STATUSES` membership (five copies unchanged);
`_scheduler_pass_status_from_execution`;
`SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY` (`:1070`
submitted-inference guard preserved — r2 P1-2); any clearing or
re-scoping of the orchestrator's in-memory
`duplicate_submission_skips` list (r2 P2-3); the `submission_skipped`
event swallow; `run_chain`/trigger loops (already fail-closed,
`chain_forecast_orchestrator_runtime.py:63-76` — NOT covered by this
change); `production_status_for` alias table (pre-existing gap, 5.0);
#1205; state_save_qc missing-output-dir publishing gap; the
`multibasin-state-idempotency` OOM-prose contradiction (5.0).
