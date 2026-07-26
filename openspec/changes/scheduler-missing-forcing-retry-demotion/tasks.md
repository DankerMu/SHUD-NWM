# Tasks — scheduler-missing-forcing-retry-demotion (#1160)

Fixture level: expanded (M scale: three coupled subsystem layers —
runtime receipt writer, accounting resolver, scheduler decision guard,
retry-attempt derivation on two sides — with proven cross-layer
ordering traps; production incident class = unbounded compute spin +
dead ops-repair channel).

Risk triage: risk axis 1 is fail-closed direction — the guard must
DEMOTE to blocked, never promote blocked states back to retry, and the
`--repair-missing-forcing` acceptance contract
(`_decision_is_stable_missing_forcing_blocker`,
`services/orchestrator/scheduler_candidates.py:1428-1445`) must hold
for the newly-emitted blockers or the ops channel stays dead. Risk
axis 2 is regression on the retry state machine (attempt derivation
changes backoff and limit behavior for ALL failed candidates, not just
missing-forcing ones). Seams under test are upstream-declared per
layer below. Risk packs: contract pack (decision vocabulary, repair
contract, fail-closed direction, blast radius on retry semantics) +
test-integrity pack (red proofs per AC, mutation on guard placement
and resolver fallback). Not selected: perf (file read per failed task
only), security (no trust-boundary change; receipt lives inside the
existing containment root and object-store prefix).

Minimal mergeable slice: all three layers in one PR. The hard
constraint is asymmetric (fixture-review note): L1 MUST NOT land
before L2 (alone it reroutes decisions to `permanent_failure_guard`
and keeps the repair channel dead); L2+L3 without L1 would be a
shippable stop-bleed (issue §5 alternative) but leaves error codes
nominally false — this change delivers all three as one slice per the
issue's recommended option, with L2 the load-bearing layer.

## 1. Implementation (implementer)

- [ ] 1.1 **L1 writer** `workers/shud_runtime/runtime.py`: in the
  `execute()` except hook (`:400-414`, alongside `_write_failure_log`
  `:1554-1560`), write `logs/task_outcome.json`:
  `{"schema_version": "nhms.shud_task_outcome.v1", "run_id",
  "error_code", "error_message", "failed_at"}` (failed_at from the
  runtime's existing clock source; message truncated ≤512 chars). Use
  the same `_write_text_no_follow(..., containment_root=log_dir)`
  discipline. MUST be written BEFORE the `upload_logs` call in the
  except hook (fixture-review P2-2 — written after, the object-store
  mirror never carries it and AC1 silently dies in production). Both
  the log and the JSON ride the existing `upload_logs` mirror
  (`:820-823`) — no new upload call.
- [ ] 1.2 **L1 reader** `services/orchestrator/chain_array_accounting.py`:
  one resolver (e.g. `_resolve_task_error_code(object_store, basin,
  fallback="NODE_FAILURE")`) that reads
  `runs/<basin_run_id>/logs/task_outcome.json` from the object store,
  validates schema_version + error_code is a non-empty str, returns
  the code; absent/unreadable/invalid → fallback (fail-safe, never
  raise). Key construction (fixture-review P2-3): at each stamp site
  rebuild the task→basin mapping from `context.active_basins` by the
  same `int(basin.get("task_id", index))` rule the accounting module
  already uses (`basins_by_task` at `:88-90` is a local of
  `record_array_task_outcomes` — do not reach for it from the stamp
  sites); use the member dict's `run_id` (re-indexed cohort caveat —
  NOT `context.run_id`), normalized identically to the writer's
  `_safe_path_component(run_id)` or the key never matches; when
  `context` or `object_store` is unavailable at the call site →
  straight fallback. Route BOTH stamp sites through it: `:598`
  (stdout path, currently no honor branch) and `:655-658` (dict path
  — gateway-supplied `error_code` keeps priority, resolver is the new
  middle preference, `NODE_FAILURE` last).
- [ ] 1.3 **L2 guard** `services/orchestrator/scheduler_state_decision.py`
  — guard consulted AT the emitting return points, never as an
  unconditional pre-pass (fixture-review P1-1). Build one lazy local
  (e.g. closure `_missing_forcing_block()` that computes
  `_retry_failure_evidence(...)` + runs
  `_missing_upstream_forecast_artifact_evidence(candidate,
  decision_state, evidence, retry_failure)` at most once) and consult
  it at exactly THREE return points, each already gated by its own
  branch condition: (a) the model-package refresh retry (`:300-302` —
  post-L1 this branch newly intercepts permanent failures and would
  re-emit a forecast retry: the spin's other door); (b) the
  permanent-failure block (`:304-310` — otherwise
  `permanent_failure_guard` wins and the repair policy rejects it);
  (c) the `retry_failed_candidate` fallback (`:320-327`). On guard
  hit, return the blocked decision; on miss, each branch returns its
  current result byte-identically. ORDERING RULING (recorded):
  `_cancelled_state_evidence` (`:312-318`) keeps its exact current
  position and priority — cancelled states that today reach
  `manual_retry_required_after_cancelled` still do; a cancelled state
  that today already returns at `:304` (permanent wins) now returns
  the missing-forcing block only when the guard fires, which is the
  intended demotion, not a new interception. Healthy/running
  candidates (no failure signal, no permanent classification) never
  reach any of the three consult points — decision stays `None`.
  URI-ABSENT RULING (fixture-review P1-2, recorded): the guard's
  existing semantics — forecast restart stage with NO recorded
  forcing URI returns the blocker with `artifact_uri=None`
  (`scheduler_state_failure.py:362-372`) — apply UNCHANGED at the new
  consult points. This is deliberate: the live incident state records
  `forcing_version: null` (no URI — forcing never produced), so the
  URI-absent shape IS the incident geometry; demoting it is the fix,
  and it matches the three existing planned-retry sites' behavior.
  Consequence accepted and pinned by test 2.1b: ANY forecast-stage
  failure without recorded forcing provenance now blocks
  repair-eligible instead of retrying — fail-closed by design.
  The three existing guard call sites (`:210-221,:249-260,:269-280`)
  stay byte-identical. No new helper that duplicates guard logic.
- [ ] 1.4 **L3 shared helper + both sides**: add ONE suffix-aware
  helper (location: `services/orchestrator/retry.py` or a small
  neighbor — implementer picks, no third copy left behind):
  `effective_retry_attempt(job_id, recorded_count) -> int` using
  last-`_retry_<n>`-suffix semantics (`rsplit`, stacked-suffix ids
  like `..._retry_1_retry_2` take the LAST); refactor
  `file_orchestration_journal.py:7070-7083` and
  `chain_forecast_orchestrator_cycle.py:198-209` to consume it.
  Feed it into (a) `chain_forecast_execution.py:389`
  (`job.retry_count = effective_retry_attempt(job_id,
  record.get("retry_count"))` — in-memory only; NEVER persist a
  nonzero count onto a reservation, clean-reservation invariant
  `file_orchestration_journal.py:1471-1485` is write-side and stays
  untouched) and (b) `scheduler_state_rows.py:415-423`
  `_state_retry_attempt` — STAGE-SCOPED (fixture-review P2-1,
  mechanism per round-2 P2-B): stage identity comes from the job
  projection's authoritative `stage` field
  (`file_orchestration_journal.py:3290`), normalized via
  `_canonical_downstream_stage`; the job-id `_retry_<n>` suffix
  supplies ONLY the attempt number, never stage identity (production
  ids embed multiple stage tokens, e.g.
  `..._convert_model_0_forecast_retry_1_retry_2_retry_3` —
  substring matching would reintroduce the pollution). Stage source:
  add an optional `stage` parameter to `_state_retry_attempt` passed
  by the failure-module consumers (which hold `_failed_stage(state)`
  at `scheduler_state_failure.py:100,:658,:1080,:1097`) — the
  import direction rows ← failure MUST NOT be reversed
  (`scheduler_state_failure.py:24-36` imports rows). A completed
  `stage="forcing"` job with `_retry_3` must NOT exhaust the
  forecast budget. Existing max-over-`retry_count` inputs stay
  as-is; the five consumers' semantics
  (`scheduler_state_failure.py:100,:658,:1080,:1097`,
  `scheduler_state_evidence_owner.py:110`,
  `scheduler_state_manual_retry.py:53` `default_attempt=+1`) are
  pinned by tests, not silently shifted.
- [ ] 1.5 Constraints: `blocked_missing_upstream_artifact` evidence
  shape unchanged (`_artifact_blocker_evidence`
  `scheduler_state_failure.py:427-468`);
  `_decision_is_stable_missing_forcing_blocker` passes for the newly
  emitted blockers (this is AC-critical — the repair channel);
  no receipt/journal schema changes beyond the new additive
  task_outcome.json; no change to `TRANSIENT_ERROR_CODES`.

## 2. Tests (implementer; red-provable; seams upstream-declared)

- [ ] 2.1 **AC4 core red tests — BOTH geometries** (seam:
  `scheduler_module._candidate_state_decision(candidate, state)` +
  `run_once()`, factories `_scheduler_candidate_fixture` /
  `_production_identity_fixture` / `_config` / `RawCandidateStateRepository`
  / `FakeProductionOrchestrator` in `tests/test_production_scheduler.py`):
  (a) URI-recorded-but-missing: failure signal
  (`pipeline_status="failed"`, `failed_stage="forecast"`,
  `error_code="NODE_FAILURE"`) + a `forcing_package_uri` key under an
  EMPTY `OBJECT_STORE_ROOT` (compose `:6149-6195` and `:5017-5057`).
  (b) URI-ABSENT — the incident geometry (live journal records
  `forcing_version: null`): same failure signal, NO forcing uri
  anywhere in state. BOTH: RED today
  (`("retry", "retry_failed_candidate")`), GREEN after
  (`("blocked", "missing_forcing_package_uri")`), full
  `artifact_guard` block ((b) with `artifact_uri=None`),
  `evidence["restart_stage"]=="forecast"`, AND
  `_decision_is_stable_missing_forcing_blocker(decision)` is True;
  `run_once()` submits nothing (`orchestrator.calls == []`).
- [ ] 2.1.1 **Non-regression pins for guard activation (P1-1;
  reshaped per round-2 P2-A — a `pipeline_status="running"` state
  never reaches the fallback region, it returns
  `("skip", "active_duplicate_pipeline")` at `:153-164`)**:
  (a) healthy candidate that actually reaches the fallback region —
  NO `pipeline_status` (or a non-active value), state carries a stage
  hint (`"stage": "forecast"`), no forcing uri, no error_code, no
  failure signal → decision stays `None` and `run_once()` submits
  normally; optionally also pin the running shape with expectation
  `("skip", "active_duplicate_pipeline")` (NOT `None`);
  (b) cancelled candidate → still
  `manual_retry_required_after_cancelled` (`:312-318` priority
  intact; existing coverage at `tests/test_production_scheduler.py:
  13747,:14253,:14390` may be reused/extended). Both green BEFORE
  and AFTER the change.
- [ ] 2.2 **L1-ordering trap test** (kills the coupling): same missing
  forcing state but `error_code="ARTIFACT_NOT_FOUND"` (post-L1 shape,
  permanent) → MUST still be `("blocked",
  "missing_forcing_package_uri")` (repair-eligible), NOT
  `permanent_failure_guard`. Red-provable against a guard wired only
  at `:320`.
- [ ] 2.3 **L1 writer test** (seam: `tests/test_shud_runtime.py`
  `test_workspace_failure_marks_run_failed` region `:2489-2500`;
  harness `LocalObjectStore` `:202-215`): drive the
  `ARTIFACT_NOT_FOUND` failure, assert `logs/task_outcome.json`
  exists with schema_version, the code, and truncated message — AND
  (P2-2, kills the write-after-upload mutation) that the object-store
  mirror `object-store/runs/<run_id>/logs/task_outcome.json` exists
  with identical content. RED today (file absent both sides).
- [ ] 2.4 **L1 reader tests** (seam: `tests/test_orchestration_chain.py`
  fakes — `FakeCycleSlurmClient.get_array_task_results` `_task_field`
  pattern `:122-157`, harness `_orchestrator` `:8038`, `_basins`
  `:8213`): (a) receipt present in fake object store under the
  member's `run_id` → task + aggregation error_code =
  `ARTIFACT_NOT_FOUND` on BOTH provider paths (stdout `:598` and dict
  `:655-658`); (b) receipt absent → `NODE_FAILURE` fallback intact;
  (c) gateway-supplied `error_code` still wins on the dict path;
  (d) `context=None` (or absent object_store) at a stamp site →
  straight `NODE_FAILURE` fallback, no raise (P2-3).
  Adjust the intended-breakage anchors with the TRUE per-anchor
  break reason recorded (fixture-review P1-2: the
  `tests/test_production_scheduler.py:5056` and
  `tests/test_gateway_reconcile.py:1135` anchors are URI-ABSENT
  states — they break via the URI-absent ruling in 1.3, not via
  "URI under an empty store"; the accounting anchors `:1355,:1358,
  :4631` break via the resolver; enumerate the full breakage list
  in the fix commit, not just these) — no silent weakening: `:4631`'s
  redaction assertion keeps asserting redaction, only the code value
  changes if its fixture gains a receipt.
- [ ] 2.5 **L3 tests**: (a) gate honors suffix — DB-free job built
  from a journal record with id `..._forecast_retry_65`,
  `retry_count=None` → `should_retry` False (limit 3) and no
  resubmission; RED today (spins). (b) stacked suffix
  `..._retry_1_retry_2` → attempt 2 (rsplit semantics; guards the
  production id shape at `tests/test_orchestration_chain.py:3187,
  :3359`). (c) `_state_retry_attempt` suffix-aware AND stage-scoped:
  state whose jobs carry `..._forecast_retry_65` → attempt ≥ 65 →
  next-pass `_failure_policy_payload` shows `limit_exhausted=True`
  (no per-pass respin); cross-stage pollution pin (P2-1/P2-B): a
  completed job whose projection carries `stage="forcing"` and a
  `_retry_3` suffix plus a FIRST forecast failure → still
  `retry_failed_candidate` (forecast budget not exhausted); plus a
  dual-stage-token id shape copied from
  `tests/test_orchestration_chain.py:3187`
  (`..._convert_model_0_forecast_retry_...`) proving stage identity
  comes from the `stage` field, not id substrings.
  (d) refactored journal helper: existing
  `_next_current_master_retry_identity` behavior byte-stable
  (existing tests in `tests/test_file_orchestration_journal.py:1989+`
  keep passing unmodified). (e) manual-retry semantics pin (P2-1):
  `scheduler_state_manual_retry.py:53` `default_attempt` unchanged
  for states without stage-matching retry suffixes.
- [ ] 2.6 **Copyback-half activation test** (L2 disclosed coupling):
  failed candidate whose state requires a copyback source that is
  missing → now `("blocked", "missing_copyback_source")` instead of
  retry; assert fail-closed direction and record as intended.
- [ ] 2.7 Red proof capture: 2.1(a), 2.1(b), 2.2, 2.3, 2.5(a),
  2.5(c) each demonstrably red on pre-change code for the right
  reason (captured output → PR body). 2.1.1 and the 2.5(c)
  cross-stage pin are green-before-and-after pins, not red proofs.
  PR body MUST attach the live node-22 journal snippet showing
  `forcing_version: null` for `gfs_2026072000` (round-2 note: it is
  the factual basis of the URI-absent ruling and lives nowhere in
  the repo; the orchestrator captured it on 2026-07-26 and supplies
  it at PR time).

## 3. Verification (orchestrator)

- [ ] 3.1 `uv run pytest -q tests/test_production_scheduler.py
  tests/test_orchestration_chain.py tests/test_shud_runtime.py
  tests/test_file_orchestration_journal.py tests/test_gateway_reconcile.py`
  green locally (authoritative backend oracle is node-27 per CLAUDE.md).
- [ ] 3.2 `uv run ruff check .` clean.
- [ ] 3.3 `openspec validate scheduler-missing-forcing-retry-demotion
  --strict --no-interactive` valid.

## 4. Spec delta (orchestrator, this fixture)

- [ ] 4.1 ADDED requirement (new title) in `job-retry-mechanism`
  covering: stable classifier survives the DB-free path; failure-state
  decisions demote to the stable missing-forcing blocker (both
  fallback and permanent branches); durable attempt derivation caps
  in-stage and cross-pass retries.

## 5. Ops follow-through (post-merge, node-22; not merge-gating)

- [ ] 5.1 Deploy: node-22 `git pull --ff-only` on
  `/scratch/frd_muziyao/NWM` (timer currently STOPPED by design —
  restart only after repair). Then the issue §7 recovery: with the
  fix live, the next single-cycle run should classify
  `gfs/ifs_2026072000` as the stable missing-forcing blocker; run
  `scripts/ops/node22-run-cycle-once.sh --cycle-time
  2026-07-20T00:00:00Z --source gfs --repair-missing-forcing --plan`,
  confirm `state_evidence.missing_forcing_repair.status ==
  "authorized"`, then `--submit`; repeat for `--source ifs`
  (equivalent IFS source id per config); verify forcing objects
  appear and forecast completes; `systemctl --user start
  nhms-compute-scheduler.timer`; confirm subsequent passes are not
  `resource_limit_blocked` on these cycles.

## Evidence mapping (issue AC → tasks)

- AC1 (real classifier, DB-free channel) → 1.1 + 1.2 + 2.3 + 2.4
- AC2 (guard on fallback; stable blocker; repair channel live) →
  1.3 + 2.1 + 2.1.1 + 2.2 + 2.6
- AC3 (effective attempt cap, both sides) → 1.4 + 2.5
- AC4 (red-provable regression, both geometries) → 2.1 + 2.2 + 2.7
- spec.md:28-35 both THENs → 4.1 + 2.1 + 2.4
- ruff/validate → 3.2/3.3
