## 1. Runtime Guard And File Lock

- [x] 1.1 Add canonical DB-free scheduler backend env parsing.
  Evidence floor: scheduler config parses the canonical env matrix from
  `design.md`: state, lock, registry, canonical readiness, journal, state
  index, all required paths, and `NHMS_SCHEDULER_DB_FREE_REQUIRED`.
  Test rows:
  - Input: `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, every scheduler backend
    selector set to `file`, required manifest/index/journal paths configured
    safely, and no `DATABASE_URL`.
    Expected: config exposes DB-free mode, backend names, and configured path
    fields without raw secrets.
  - Input: DB-free required is absent/false and legacy postgres config is used.
    Expected: legacy config remains valid where existing postgres-mode tests
    expect it.
- [x] 1.2 Fail closed on DB-free mixed backend or scheduler `DATABASE_URL`.
  Evidence floor: DB-free preflight blocks before lock acquisition/submission
  when `DATABASE_URL` is present, any selector is unset/non-file/postgres-like,
  or a required file path is missing/unsafe; evidence records the exact field.
  Test rows:
  - Input: DB-free env plus scheduler `DATABASE_URL`.
    Expected: pre-lock blocker `database_url_forbidden`; no lock acquisition,
    no model discovery, no DB-backed factory, no Slurm submission.
  - Input: each selector one at a time unset, blank, `postgres`, `psycopg`, or
    another non-file value.
    Expected: pre-lock blocker names the exact selector field; no mutation.
  - Input: each required manifest/index/journal path missing, blank, outside
    allowed workspace/object boundary, or unsafe.
    Expected: pre-lock blocker names the exact path field; evidence remains
    bounded and redacted.
- [x] 1.3 Move DB-free scheduler locking to `FileSchedulerLease`.
  Evidence floor: focused local tests show `lock_type=file`; concurrent passes
  cannot both mutate and contention evidence is bounded. Node-22 bounded pass
  proof remains part of deployment task 6.4 and post-stop task 7.4.
  Test rows:
  - Input: valid DB-free config.
    Expected: `_build_scheduler_lease()` returns/uses `FileSchedulerLease` and
    lock evidence has `lock_type=file`.
  - Input: two DB-free scheduler passes compete for the same file lock.
    Expected: at most one acquires mutation rights; the contended pass records
    bounded file-lock contention and submits nothing.
- [x] 1.4 Add DB-free scheduler evidence fields and factory guardrails.
  Evidence floor: evidence records backend names and configured manifest/index
  paths; tests fail if DB-free factories call `PsycopgModelRegistryStore`,
  `PsycopgMetStore`, `PsycopgOrchestratorRepository`,
  `PsycopgStateSnapshotRepository`, DB-backed `_retry_service_from_env`,
  SQLAlchemy `PipelineStore`, DB-backed `ForcingProducer.from_env`, or
  equivalent PostgreSQL paths.
  Test rows:
  - Input: valid DB-free config with every listed DB-backed factory monkeypatch
    set to raise.
    Expected: DB-free construction/preflight still succeeds or returns the
    intended file-mode blocker without invoking any patched DB-backed factory.
  - Input: DB-free pass evidence.
    Expected: evidence includes `database_url_configured=false`, selected
    backend names, canonical DB-free field names, redacted configured paths,
    and no PostgreSQL host/port/advisory-lock/psycopg dependency.
  - Input: non-DB-free legacy mode.
    Expected: existing postgres-backed registry/repository/readiness/reconcile
    reachability remains available where current tests intentionally cover it.

## 2. File Model Registry And Canonical Readiness

- [x] 2.1 Define the production scheduler registry manifest schema.
  Evidence floor: schema validates version, model identity, basin identity,
  package URI, checksum, resource profile, capabilities, segment counts, source
  policy, duplicate IDs, and bounded evidence.
- [x] 2.2 Add a registry publisher for production scheduler manifests.
  Evidence floor: publisher verifies referenced model manifests/checksums,
  writes manifest-last, records checksum/generated_at/source evidence, and can
  produce a real node-22-consumable registry fixture.
- [x] 2.3 Wire scheduler model discovery to file registry in DB-free mode.
  Evidence floor: `ProductionScheduler.from_env()` does not call
  `PsycopgModelRegistryStore.from_env()` in DB-free mode; discovery evidence
  lists selected model IDs and registry checksum.
- [x] 2.4 Define the canonical readiness index schema and publisher.
  Evidence floor: publisher produces schema version, checksum, generated_at,
  source/cycle/model/basin identity, forecast-hour coverage, product counts,
  product URIs, and object existence evidence using authoritative canonical
  product artifacts or node-27 data-plane state.
- [x] 2.5 Wire scheduler canonical readiness to file index in DB-free mode.
  Evidence floor: scheduler uses file/object-store product evidence and
  existing canonical readiness semantics without `PsycopgMetStore`; missing,
  stale, checksum mismatch, object-missing, and identity mismatch cases fail
  closed.
- [x] 2.6 Preserve node-27 raw handoff and no-download fallback semantics.
  Evidence floor: focused tests prove missing/invalid NFS raw blocks, ready raw
  plus canonical-zero starts at `restart_stage=convert`, staging is
  manifest-last, and Slurm requests do not include `download_source_cycle`.

## 3. File Orchestration Journal Contract And Read State

- [x] 3.1 Define the DB-free orchestration repository method contract.
  Evidence floor: contract lists scheduler-required read methods for active
  orchestration, active pipeline, completed pipeline, active Slurm jobs,
  candidate state, model/forcing context reads, and query helpers. This
  read-side boundary was superseded by the section 4 write-side tasks, which
  now carry the final lifecycle, pipeline write, retry/cancel, and status-sync
  evidence floor.
- [x] 3.2 Define append-only journal and materialized latest schemas.
  Evidence floor: schemas cover candidate/job/event state, active Slurm job
  evidence, reservation, binding, retry attempt, Slurm job ID, stage, status,
  error code, redacted runtime roots, sequence IDs, and replay metadata.
- [x] 3.3 Implement file-backed read-side scheduler state.
  Evidence floor: file repository answers active orchestration, active
  pipeline, completed pipeline, active Slurm jobs, candidate state, and
  model/forcing context reads without PostgreSQL. Trusted replay also validates
  nested latest/journal identity, strict direct
  `pipeline-jobs` schema, sidecar `pipeline-events` source/cycle schema,
  no-follow scanned entries, file/depth/JSON complexity limits, source alias
  canonicalization, envelope/payload/run identity consistency, append-only
  sequence ordering, valid direct-only `pipeline-jobs` snapshots, directory
  entry bounds, and blocked query redaction before state is trusted.
- [x] 3.4 Add read-side repository contract tests.
  Evidence floor: shared fixtures cover duplicate prevention, active Slurm job
  skip/cancel evidence, completed skip, candidate identity filtering, and
  model/forcing context reads. Regression fixtures also cover malformed
  embedded rows, invalid cycle timestamps, newer terminal direct job masking,
  sidecar-event schema/cycle mismatch, resource limits, DB-compatible
  equal-timestamp job/event tie-breaks, scheduler-level active Slurm evidence,
  public-safe blocked query sentinels, source alias/casing consistency,
  envelope/payload/run mismatch blockers, append-only sequence precedence,
  valid direct-only pipeline-job reads, JSONL record limits, unknown record
  types, and non-matching directory-entry limits. Write-side lifecycle, retry,
  and migration behavior is covered by section 4 tasks.

## 4. File Orchestration Journal Writes, Retry, And Migration

- [x] 4.1 Implement file-backed lifecycle and pipeline writes.
  Evidence floor: file repository supports `ensure_forecast_cycle`,
  `create_hydro_run`, hydro/forecast status updates, reservation/bind,
  pipeline job upsert/status update, and pipeline event insertion with
  durable per-cycle locking, journal-first reservation writes, and atomic
  materialization.
- [x] 4.2 Replace DB-backed retry service in DB-free mode.
  Evidence floor: DB-free orchestrator construction does not call
  `_retry_service_from_env` or `PipelineStore`; retry attempts, retry-limit
  exhaustion, manual repair, and permanent-failure state are represented in the
  file journal with manual policy evidence, active-retry conflict guards, and
  DB-compatible hydro-run reset semantics.
- [x] 4.3 Add historical scheduler-state export/import migration.
  Evidence floor: exporter reads active/completed/candidate/job/event/retry and
  permanent-failure rows from node-22 `:55433`, writes append-only journal
  events preserving historical event identity/order, records cutoff time, row
  counts, checksums, replay status, stale `download_source_cycle` supersession
  evidence, and writes no-follow receipts under the journal/evidence root.
- [x] 4.4 Add write-side and migration contract tests.
  Evidence floor: fixtures for active, completed, permanent failure, manual
  repair, retry exhaustion, stale `download_source_cycle`, and migrated journal
  replay produce decisions equivalent to DB-backed state, including repeated
  migration idempotency and write-failure/concurrency guard regressions.
- [x] 4.5 Wire scheduler submission path to file journal in DB-free mode.
  Evidence floor: fake Slurm submission with no `DATABASE_URL` writes file
  reservation/job/event evidence and does not call
  `PsycopgOrchestratorRepository.from_env()`.

## 5. File State Snapshot Index And DB-Free State Save

- [x] 5.1 Define the file-backed state snapshot index contract.
  Evidence floor: index maps `model_id + source_id + valid_time + cycle_id +
  lead_hours` to usable state URI, checksum, source/cycle identity, schema
  version, generated_at, model package lineage, and object existence evidence.
- [x] 5.2 Add exact-match strict warm-start lookup.
  Evidence floor: exact successor checkpoint succeeds; missing, stale, checksum
  mismatch, wrong source/model/time, and unusable state fail closed without
  latest-state fallback or `PsycopgStateSnapshotRepository`.
- [x] 5.3 Make `state_save_qc` produce DB-free state-index records.
  Evidence floor: state-save command runs without `DATABASE_URL`, writes or
  stages index records with checksum/identity evidence, and does not instantiate
  `StateRunRepository.from_env()` or `PsycopgStateSnapshotRepository`.
- [x] 5.4 Add scheduler warm-start integration tests.
  Evidence floor: scheduler candidate construction carries file-index state
  evidence and blocks candidates when strict state is unavailable.

Scenario evidence rows for section 5:

- Valid state-index entry with matching `model_id`, normalized `source_id`,
  `valid_time`, expected `cycle_id`, required `lead_hours`, model package
  checksum, usable flag, and object checksum -> strict lookup returns ready
  `candidate_state` evidence with state URI, checksum, lineage, schema version,
  and entry/object evidence.
- Missing exact entry for `model_id + source_id + valid_time` -> scheduler
  candidate blocks with `state_snapshot_index_exact_checkpoint_missing`; latest
  usable fallback is not called.
- Entry for the same `model_id + source_id + valid_time` but missing/wrong
  expected `cycle_id` or wrong `lead_hours` -> scheduler candidate blocks with
  stable lineage evidence; latest usable fallback is not called.
- Overlapping state checkpoints for the same `valid_time` but different
  producing cycles/leads -> state IDs/object keys/index identities stay
  distinct and strict lookup selects the requested lead's expected producer
  cycle.
- Missing state object after index publish -> lookup blocks with
  `state_snapshot_index_object_missing`.
- Mutated state object checksum -> lookup blocks with
  `state_snapshot_index_object_checksum_mismatch`.
- `usable_flag=false` -> lookup blocks with
  `state_snapshot_index_checkpoint_unusable`.
- Non-boolean `usable_flag` -> lookup fails closed with
  `state_snapshot_index_usable_flag_invalid`.
- Unsafe, encoded traversal, cross-prefix, or local absolute state object URI
  -> lookup fails closed with state-index object URI evidence and no local root
  leakage.
- Concurrent DB-free file-index upserts for distinct state identities ->
  serialized update preserves both entries.
- Wrong model/source/time/package or stale/unsupported/malformed index ->
  lookup fails closed with state-index evidence and no
  `PsycopgStateSnapshotRepository`.
- DB-free `state_cli save` with `DATABASE_URL` absent and manifest-index run
  context -> writes a usable file-index record with state checksum, source,
  cycle, valid-time, and model package evidence; `StateRunRepository.from_env()`
  and `PsycopgStateSnapshotRepository.from_env()` are not constructed.
- DB-free `state_cli save` with missing required `NHMS_*` lineage env -> exits
  before DB repository construction or object upload, and stderr names the
  missing fields.
- Same-checksum DB-free state save rerun with missing older lineage/package
  metadata -> repairs the record metadata and requires QC before strict
  readiness.
- Legacy non-DB-free `state_cli save --manifest-index --task-id` with old
  manifest entries containing only `run_id` -> still resolves `run_id` and
  delegates to the legacy repository path.
- DB-free strict scheduler candidate with ready exact state index -> candidate
  carries `state_snapshot_index` and `candidate_state` evidence.
- DB-free strict scheduler candidate with empty or unavailable state index ->
  candidate blocks before orchestrator/Slurm mutation and does not use latest
  fallback.

## 6. DB-Free Runtime Integration And Deployment Compatibility

- [x] 6.1 Update DB-free Slurm preflight policy.
  Evidence floor: with DB-free mode and Slurm enabled, missing `DATABASE_URL`
  does not produce `SLURM_PREFLIGHT_DATABASE_URL_MISSING`; non-DB root,
  manifest, secret, and template safety checks still run.
- [x] 6.2 Update compute deployment entrypoints for DB-free scheduler mode.
  Evidence floor: `infra/env/compute.example`, DB-free env template/runbook,
  systemd guidance, and `infra/compose.compute.yml` support scheduler runtime
  without mandatory `DATABASE_URL` while preserving DB requirements for
  non-DB-free lanes.
- [x] 6.3 Run local focused no-DB integration tests.
  Evidence floor: with `DATABASE_URL` absent, scheduler planning and fake
  submission use file lock, registry, readiness, raw handoff, journal, retry,
  and state index; static guardrails prove no psycopg factories are called.
- [x] 6.4 Freeze node-22 scheduler timer and deploy DB-free env with `:55433`
  still online.
  Evidence floor: receipt records stopped/frozen timer state, env/unit backup,
  DB-free env, migration artifacts, rollback commands, and a bounded scheduler
  pass with no scheduler `DATABASE_URL` and `lock_type=file`.
- [x] 6.5 Observe live GFS and IFS cycles through `convert-or-later`.
  Evidence floor: one GFS and one IFS cycle use node-27 raw, node-22 file-backed
  scheduler state, downstream Slurm submission, no scheduler PostgreSQL, and no
  `download_source_cycle` submission.

## 7. Archive And Stop Node-22 Historical PostgreSQL

- [x] 7.1 Capture pre-stop listener and session attribution.
  Evidence floor: receipt records `ss -ltnp`, PID, process owner, unit/service
  metadata where available, command line, active client/session snapshot, and
  proof `:55433` is historical rollback state rather than scheduler-owned.
  Evidence: `docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`
  records `pre-stop-metadata.json`, Docker owner attribution, active sessions,
  scheduler timer/service status, and the `sudo -n` limitation for root-owned
  socket PID metadata. It also links the 2026-06-28 #836 GFS/IFS live proof
  receipt that satisfied the pre-stop DB-free scheduler gate.
- [x] 7.2 Archive node-22 `:55433`.
  Evidence floor: receipt records dump/archive path, checksum, service/unit
  metadata, env backup, process owner, listener snapshot, and rollback commands
  without secrets.
  Evidence: archive root
  `/ghdc/data/nwm/operator-archives/node22-postgres-55433/20260629T025421Z`,
  `SHA256SUMS`, `sha256sum-check.txt`, `pg-restore-list.txt`, redacted env
  backups, `archive-permissions.txt`, owner-only archive/evidence permission
  checks, and secret-free rollback/cleanup commands are listed in the
  2026-06-29 receipt.
- [x] 7.3 Stop and disable node-22 historical PostgreSQL.
  Evidence floor: stop action is performed by the owning account or an
  administrator; `ss -ltnp | grep 55433` is empty.
  Evidence: `docker stop nhms-22-e2e-db` completed on 2026-06-29; post-stop
  Docker state is `exited` with restart policy `no`, `docker ps` has no running
  matching container, and `ss_55433_after` is empty in
  `post-stop-health.txt`.
- [x] 7.4 Run post-stop scheduler and service health verification.
  Evidence floor: bounded no-DB scheduler pass succeeds, compute API and Slurm
  gateway are healthy, and latest evidence still shows `lock_type=file`.
  Evidence: bounded GFS post-stop dry-run records `status=planned`,
  `database_url_configured=false`, `scheduler_db_free_required=true`,
  all scheduler backends as `file`, `lock_type=file`, root preflight ready, and
  no mutation; compute API and `/api/v1/slurm/health` both pass.
- [x] 7.5 Update topology guardrails and receipts.
  Evidence floor: active docs/env examples no longer present node-22 `:55433`
  as business DB; historical references are explicitly marked archive/rollback.
  Evidence: this task update, the 2026-06-29 receipt,
  `docs/runbooks/node22-db-retirement-runbook.md`, active topology docs, and
  env examples now describe `:55433` as archived/stopped rollback state only.

## 8. Generation-Aware Cutover Consumer For Cold Start And Backfill (#1081)

- [x] 8.1 Consume the `nhms.scheduler.registry_package_cutover.v1` declaration
  channel emitted by the registry publisher (schema landed by #1080) at
  scheduler planning time.
  Evidence floor: scheduler loads the declaration from the configured channel
  (env or manifest reference) and binds each declared cutover to (`model_id`,
  `old_checksum`, `new_checksum`, `generation`, `effective_cycle_utc`,
  `transition_mode`); mismatched checksums/generation, missing declaration
  when a `package_changed` transition would be required, or a candidate cycle
  outside the declared window fail closed with a typed reason and no candidate
  submission.

- [x] 8.2 Derive a generation token from the registry `package_checksum` and
  thread it through candidate construction, state lookup, backfill selection,
  and evidence.
  Evidence floor: every candidate carries a `generation` token derived
  deterministically from `package_checksum`; state/backfill queries filter by
  generation; evidence records generation for candidate, cold/warm decision,
  selected state predecessor, and any refusal reason.

- [x] 8.3 Warm continuation for unchanged generation.
  Evidence floor: when the candidate's model generation equals the current
  registry-canonical generation AND an exact state checkpoint exists at the
  expected predecessor cycle/lead, scheduler selects warm start; missing exact
  checkpoint blocks with
  `state_snapshot_index_prior_checkpoint_missing_after_history` and identifies
  the required predecessor identity in evidence.

- [x] 8.4 Cold start for truly new model.
  Evidence floor: when a `model_id` has no prior state-index history across
  all generations, scheduler cold-starts once at its earliest selected source
  cycle; subsequent cycles of the same generation require exact predecessor
  checkpoints and fail closed if absent.

- [x] 8.5 Cold start for a declared existing-model package cutover only at
  `effective_cycle_utc`.
  Evidence floor: a valid declaration admits cold start ONLY at
  `effective_cycle_utc`; earlier cycles keep the old generation's warm
  requirement; later cycles must find exact new-generation predecessors; a
  cycle earlier than `effective_cycle_utc`, or an absent / stale / mismatched
  declaration, fails closed with a typed reason
  (`registry_cutover_cold_start_out_of_window` /
  `registry_cutover_declaration_missing` /
  `registry_cutover_declaration_stale`). Old-generation state history remains
  readable for audit and rollback but does not count as current-generation
  history.

- [x] 8.6 Predecessor-aware backfill within one generation.
  Evidence floor: when cycle T is blocked because the required predecessor
  (T-12h for 00/12 sources, or T-6h if the source uses a 6h cadence) is
  missing AND the raw manifest for the predecessor exists, scheduler emits a
  predecessor-select candidate for the predecessor before retrying T;
  T is deferred with a `predecessor_pending` reason and is NOT submitted or
  permanently failed while the predecessor is pending. A candidate
  predecessor from a different generation is refused with
  `generation_mismatch`.

- [x] 8.7 Generation-lineage quarantine for stale journal / output evidence.
  (partial: state-index side landed via
  `expected_key_predecessor_quarantined` observability flag + BLOCK_WRONG_
  GENERATION admit-matrix branch; full journal-side predecessor_identity
  filter beyond `init_state_id` mismatch routing is tracked in #1107.)
  Evidence floor: completed / failed journal entries whose recorded
  predecessor identity does not match the required predecessor for the
  current generation are quarantined from canonical readiness scoring **at
  scoring time** (the journal itself remains immutable — no journal writes
  are performed by the quarantine path) while remaining readable as
  immutable audit entries; correct backfill selection is not suppressed by
  their presence.

  §8 follow-ups filed:
  - #1107 — full §8.7 journal-side predecessor_identity filter.
  - #1108 — A4 orphaned pre-§8 `strict_warm_start_successor_checkpoint_
    missing` branch fixture (this PR's shift to
    `strict_warm_start_terminal_init_state_mismatch` no longer covers it).

- [x] 8.8 Evidence emission for every decision path.
  Evidence floor: scheduler evidence records `model_id`, `generation`,
  `transition_decision` ∈ {`warm_continue`, `cold_new_model`,
  `cold_declared_cutover`, `block_predecessor_pending`,
  `block_declaration_missing`, `block_declaration_stale`,
  `block_cold_start_out_of_window`, `block_wrong_generation`},
  `selected_predecessor` identity (or `null`), `cold_start_reason`, and any
  typed block reason. The 13→19 matrix, both 00/12 UTC transitions, GFS/IFS,
  retry / restart, and concurrent bounded submission are covered.

- [x] 8.9 No broad bypass through `NHMS_REQUIRE_FORECAST_WARM_START=false`.
  Evidence floor: the existing env variable only affects optional warm-start
  hints; it does NOT admit a declaration-less cutover, a missing predecessor,
  or a wrong-generation checkpoint. Verified by targeted regression test.

- [x] 8.10 Test matrix + Slurm-oracle proof.
  Evidence floor: `uv run pytest -q` covers all Acceptance Criteria
  (13 continuing + 6 new models; both 00 and 12 transitions **for each of
  GFS and IFS**; a package cutover with declaration; retry / restart across
  a cutover; concurrent bounded submission); `uv run ruff check .` clean;
  `openspec validate node22-db-free-scheduler-state --strict --no-interactive`
  valid; node-22 Slurm-oracle **dry-run** (planning + fake submission — no
  live backfill execution, that is #1072/#856 scope) proves scheduler does
  not submit outside the declared window and defers `predecessor_pending`
  cycles instead of permanently failing them.
  Local pytest coverage of the 8 `transition_decision` enum values landed in
  `tests/test_scheduler_generation.py` (28 tests, incl. the D8.9
  env-override regression) and
  `tests/test_state_manager_generation_history.py` (5 generation-scoped
  history-signal cases).  Node-22 Slurm-oracle dry-run proof is a deployment
  step deferred to #1081 PR review — the planning code path itself carries
  the ``registry_cutover_transition`` evidence field consumed by that
  oracle, and existing DB-free scheduler tests (`test_production_scheduler`
  covering strict + non-strict, GFS/IFS, retry / restart) all pass with the
  new logic.

## 9. Accepted Cohort Reconcile And Restart-Stage Preservation (#1112)

- [x] 9.1 Persist deterministic forecast-cohort reservation, ordered member/task map,
  idempotency key, exact Slurm comment, restart stage, and submission-attempt
  evidence before the Gateway call.
  Evidence floor: a real `FileOrchestrationJournalRepository` test observes the
  reserved-unbound cohort before fake Gateway submission and a transport
  timeout records `submit_result_ambiguous`, not permanent candidate/hydro
  failure. The fake Gateway itself reopens the real file journal and observes
  the reservation before performing or simulating the external side effect;
  strict directory durability failures are injected through the real
  orchestrator path and prove the Gateway spy is never invoked.

- [x] 9.2 Reconcile reserved-unbound file-journal forecast cohorts by exact Slurm comment
  and identity.
  Evidence floor: unique match binds; zero match defers within the bounded
  window and permits one idempotent attempt afterward; multiple, mismatch, and
  accounting-unavailable outcomes do not bind/cancel/resubmit and emit distinct
  bounded evidence using the fixture's fixed `reconciliation_decision` values.
  A pre-outcome reservation interruption is first classified ambiguous; an
  owner/account collision remains mismatch-blocked, and owner-scoped zero alone
  cannot prove authoritative absence. A Gateway timeout carries no accounting
  decision before the first query, and an exact match without independent
  runtime rows remains blocked. Reclaiming a retry atomically starts a clean
  pre-outcome attempt and cannot inherit the prior attempt's accounting tuple,
  including across immediate reopen. Every branch is reopened from disk and
  proves the persisted tuple equals the emitted tuple. Attempt-scoped CAS keeps
  stale concurrent transitions from overwriting a bound row, and normal success
  plus accounting adoption each atomically bind the complete evidence tuple;
  same-ID replay is idempotent and different-ID collision is blocked. Only a
  typed proven pre-acceptance rejection becomes `rejected`; post-request
  parse/malformed/unknown failures remain ambiguous. Proven rejection commits
  the master and every member hydro failure atomically with reopen parity.

- [x] 9.3 Project terminal array-task accounting to candidate-scoped
  pipeline/hydro state.
  Evidence floor: exact successful tasks clear only their stale
  `SLURM_GATEWAY_UNAVAILABLE` state and resume at `state_save_qc`; failed tasks
  remain failed/retry-eligible; successful siblings are not recomputed. The
  canonical digest and every projection-to-member identity are validated on all
  write/replay surfaces; malformed members fail closed, projection inputs are
  bounded before persistence, and task-accounting state does not extend the
  closed reconciliation-decision enum.

- [x] 9.4 Preserve canonical `restart_stage` through scheduler grouping,
  deterministic cohort identity, basin manifest/run context, and stage
  execution.
  Evidence floor: a recovered `state_save_qc` candidate submits no native SHUD
  forecast, and mixed `forecast`/`state_save_qc` candidates form distinct
  cohorts with distinct durable identities.

- [x] 9.5 Add highest-feasible-seam regressions for accepted-submit timeout and
  restart recovery.
  Evidence floor: `ProductionScheduler.run_once()` and
  `ForecastOrchestrator.orchestrate_cycle()` use the real file journal with a
  fake Gateway/Slurm boundary to prove one array for 18 members, exact-comment
  bind after process restart, GFS/IFS parity, partial-task isolation, no cancel
  before ownership proof, and required reconciliation/restart evidence.
  The matrix also proves two concurrent post-window passes permit one retry,
  over-limit accounting blocks with bounded evidence, runtime roots/credentials
  are redacted, generic and non-DB-free reconcile callers remain compatible,
  and recovered success preserves initial-state/run-manifest/checkpoint/QC
  lineage. Non-forecast array failures and stale cohort-like rows are also
  proven unable to create forecast or `state_save_qc` projections. Ordinary
  inflight task accounting is byte/row/time bounded before materialization, and
  aggregate per-model latest output grows approximately linearly for 18 and 256
  members with warm-cache/reopen parity. Executable process-boundary tests cover
  byte, row, and timeout termination/reap paths rather than mocking the bounded
  reader.
  Exact-comment discovery proves global visibility at preflight, uses bounded
  time pages at the 256-member production cadence, and counts a final
  unterminated record. Page boundaries are frozen per reconcile session so
  advancing wall time cannot rescan/starve later cohort keys. Scheduler restart
  evidence contains the bounded submit/accounting tuple, restart/native-SHUD
  fields, and candidate/task outcomes for reserved and inflight branches.
  Successful candidate summaries report projected `state_save_qc`, while
  failed/unverified summaries retain `forecast`. Version-marked new rows and
  marker-free legacy rows replay compatibly, DB-free visibility proof does not
  affect generic reconciliation, and 256-member historical cycles do not grow
  a globally scanned direct namespace toward its hard file limit. A zero-match
  proof covers the current attempt anchor before releasing retry; older or
  coverage-unproven attempts remain unavailable. Versioned master mutations use
  exact/cycle-scoped reads despite unrelated malformed or over-limit history,
  and raw page row/byte saturation emits bounded unavailable reason classes
  rather than fabricating a multiple-match proof. Versioned masters require an
  immutable aware-UTC attempt anchor across reserve/direct/journal/latest;
  reclaim creates a new lock-owned anchor, retry CAS compares it, and the
  consumer rejects completeness declarations whose bounds do not actually
  contain the durable anchor while still allowing a proven exact match to bind.
  Current-version master classification is sticky across ordinary upserts; an
  adversarial identity-field matrix and multi-step stage/classification detour
  fail on the first mutation with reopen parity, while typed reclaim and valid
  candidate/legacy updates remain compatible.

- [ ] 9.6 Complete local, CI, and node-22 live verification.
  Evidence floor: the issue-targeted pytest command, `uv run ruff check .`, and
  strict OpenSpec validation pass; CI is green at the frozen PR SHA; node-22
  injects a bounded accepted-submit response timeout and proves automatic exact
  comment recovery, one 18-task array, shared-NFS downstream copyback, no native
  SHUD resubmission for `state_save_qc`, and no scheduler `DATABASE_URL`, with
  explicit rollback receipt.
