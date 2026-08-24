## Context

Issue #1564 is the deliberate operational complement to #1116. When `AccountingStoreFlags` lacks `job_comment`, reconcile records a current accepted-submit cohort master as `reserved`, unbound, `submit_result_ambiguous`, `slurm_exact_comment`, `accounting_unavailable`, and `comment_accounting_unproven`. Treating the empty query as absence would risk duplicate submission, so automation must remain fail-closed. After an operator independently proves the job dead with name/time/user/account evidence, however, the file journal has no typed way to record that proof; generic transition, HTTP manual retry, and reclaim all reject the held row.

**Fixture level:** expanded because this changes a production CLI and a persisted state transition. **Repair intensity:** high because a false positive can double-submit a forecast cohort and a partial write can separate durable state from its audit evidence. The NHMS project profile already covers orchestration, Slurm mock-vs-real parity, persisted state, and node-22 runtime evidence; no profile update is needed. Runtime evidence is production-safe: absent a naturally occurring eligible row, node-22 validation stops after a read-only census and does not manufacture a scheduler incident.

## Goals / Non-Goals

**Goals:**

- Provide one explicit, row-scoped, file-journal-only operator recovery command.
- Reject stale, bound, wrong-state, wrong-decision, wrong-reason, wrong-attempt, wrong-anchor, or unconfirmed requests with a non-zero CLI exit and byte-identical journal state.
- Atomically persist the terminal master transition, matching hydro-member failure fan-out, and a durable audit event.
- Preserve manual and automatic provenance as distinct decisions while making both legitimate absence decisions reclaimable.
- Make the next scheduler pass take the existing reclaim/resubmit path instead of `PIPELINE_ALREADY_ACTIVE`.

**Non-Goals:**

- Changing #1116's comment-capability probe or automatic fail-closed outcome.
- Adding `reserved` to HTTP/manual retry source statuses.
- Adding a comment-less automatic matching heuristic (#1565).
- Changing Slurm configuration or claiming comment storage is retroactive.
- Adding PostgreSQL columns or mirroring this operator token into the coarse PostgreSQL reclaim implementation.
- Making `identity_mismatch_released` or any generic transition reclaimable.

## Decisions

### D1: One dedicated typed file-journal CAS API

Add a dedicated repository method rather than using `transition_pipeline_job_submit_evidence`, `permit_pipeline_job_retry`, or a generic status write. It accepts the versioned master `job_id`, exact `submission_attempt`, exact `submission_attempt_started_at`, `checked_by`, timezone-aware `checked_at`, and a bounded non-empty verification note. Under the cycle lock it re-reads durable authority and succeeds only when all of these hold:

- current accepted-submit master and matching `job_id`;
- `status == reserved`, `slurm_job_id` empty, and `matched_slurm_job_id` empty;
- `submit_outcome == submit_result_ambiguous`;
- `reconciliation_source == slurm_exact_comment`;
- `reconciliation_decision == accounting_unavailable`;
- `reconciliation_reason_class == comment_accounting_unproven`;
- persisted attempt and normalized UTC attempt anchor exactly equal the expected CAS values.

A mismatch returns zero and writes no bytes. Invalid input types/enums raise `FileOrchestrationJournalError`. The generic versioned-decision whitelist remains closed to both absence retry tokens.

### D2: Distinct post-state with no reason class

Successful demotion writes `status=reservation_lost`, `reconciliation_decision=operator_verified_absence`, `reconciliation_source=slurm_exact_comment`, `submit_outcome=submit_result_ambiguous`, `matched_slurm_job_id=None`, and `reconciliation_reason_class=None`. Clearing the reason class is required by the accepted-submit invariant: reason classes belong only to `accounting_unavailable`. The prior `comment_accounting_unproven` value is retained only inside audit-event details.

`operator_verified_absence` joins `ACCEPTED_RECONCILIATION_DECISIONS`, but not the generic transition whitelist or identity-streak decisions.

### D3: State and audit event are one append transaction

The typed method writes, under one lock and one `_append_journal_records_unlocked` call:

- active member `hydro_run` rows for the same attempt to `failed` / `SLURM_RESERVATION_LOST`, matching the existing automatic absence transition;
- the cohort master transition;
- a `pipeline_event` such as `operator_verified_absence`, with `status_from=reserved`, `status_to=reservation_lost`, and bounded details containing `checked_by`, normalized `checked_at`, verification note, expected attempt/anchor, and prior reason/decision.

State without audit, audit without state, or only part of the hydro cohort must never become durable in the authority journal batch. That append is the explicit commit point. Direct-job and latest files are derived projections written after it and cannot be transactionally rolled back with the journal: if either projection fails after the append, the method SHALL contain the projection fault, attempt the remaining independent projections, and return a committed typed receipt with bounded non-secret projection warnings. It SHALL NOT raise a false operation failure after authority state and audit evidence are durable; replay remains the source of truth and a repeated operator request still loses CAS without appending a duplicate decision. The event identity and materialization follow the existing journal record validators and sequence allocation; no new entity type is introduced.

### D4: Two reclaim doors, all other terminal shapes closed

Both `reclaim_pipeline_job_reservation` and `_verified_accepted_submit_forecast_retry` widen only their accepted decision set to `{absence_retry_permitted, operator_verified_absence}`. Their surrounding guards remain distinct rather than being conflated: the cycle door keeps the caller's `reservation_lost`/unbound check plus the shortcut's outcome, exact-comment source, null matched id, and cohort-identity checks (including compatibility with the existing marker-free automatic-absence mapping), while file-journal reclaim additionally requires a current master/idempotency match, null reason/match, exact durable attempt/anchor expectations, and immutable master identity. `identity_mismatch_released` remains non-reclaimable. For a current master, reclaim derives attempt+1 solely from the durable row and captures a fresh lock-owned anchor; neither the request's proposed new attempt nor its timestamp can become authority.

### D5: One command, both CLI entrypoints, explicit non-interactive confirmation

Register `demote-reserved-job` in click and argparse with identical required inputs:

- `--journal-root`
- `--job-id`
- `--expected-attempt`
- `--expected-attempt-started-at` (ISO-8601 with timezone)
- `--checked-by`
- `--checked-at` (ISO-8601 with timezone)
- `--verification-note`
- `--confirm`

No stdin prompt is used because node-22 automation and receipts must be reproducible. Missing `--confirm` must fail before repository construction/write. Success prints a stable sorted JSON receipt including the job id, prior/new status, decision, attempt/anchor, operator fields, and written-record count. CAS refusal or validation error prints to stderr and exits 2.

### D6: Tests use the highest existing seams

Prefer a focused new CLI test module if it makes click/argparse parity readable; if added, include it in `ORCHESTRATOR_CLI_IMPORTER_TESTS` so a `cli.py`-only PR selects it. Journal tests compare bytes before/after every refusal, inspect the exact master/hydro/event durable rows on success, and drive the real demote → cycle verified-retry → reclaim path. Tests do not hand-build a post-state that bypasses the typed transition.

### D7: Release evidence must not manufacture a production incident

The rare held pre-state is an incident to recover from, not a fixture that release validation must create. Node-22 validation first performs a read-only census of the active journal and Slurm context. If no naturally occurring exact held row exists, that census plus the deterministic full-chain/fault evidence is sufficient for release. Validation SHALL NOT quiesce the production scheduler, force gateway failure, seed or rewrite journal authority, or submit a real cohort merely to create a receipt. When a naturally occurring held row later exists, the operator procedure remains mandatory for that incident: independently prove absence, use the exact typed CAS, retain success/audit/refusal evidence, and verify one reclaim/resubmission. This conditional receipt is operational evidence, not a prerequisite whose absence incentivizes fault injection.

### D8: Writer-authority closed world for the operator decision (Round 3)

`operator_verified_absence` remains valid inside `ACCEPTED_RECONCILIATION_DECISIONS` for replay and normalization, but enum membership is not write authority. Every current-contract accepted-submit writer that can receive or synthesize a reconciliation decision is inventoried and must reject the token unless it is the dedicated typed demotion: the submit-attempt commit writer (an accepted transition may carry the decision), the cohort projection defer writer (a raw caller-supplied decision string), the cohort task projection writer (a raw decision), the ordinary pipeline-job upsert writer (including a marker-free row upgraded to the current contract in one merge), and the already-gated generic versioned transition. Each non-dedicated writer raises the typed-authority error before row construction, lock acquisition, durable mutation, or event. Legitimate automatic decisions and non-token legacy upgrades keep their existing writers and behavior; the dedicated demotion stays the sole writer of the operator decision with its confirmation/CAS/audit batch. The distinct legacy transition/reconciliation writer paths are outside this current-contract change and tracked in #1805.

### D9: Committed reclaim completion (Round 3)

The authority append inside `reclaim_pipeline_job_reservation` is the commit point of the new attempt. Once it commits on the operator old-ID route, a derived direct/inventory projection failure SHALL NOT be reported as an uncommitted failure that strands a pre-sbatch live `reserved` row. The reclaim boundary SHALL either return a committed typed result the stage submission path can honor (continuing to the single sbatch/bind), or transition the durable row to a non-live retryable authority state under the same lock — mirroring the dedicated demotion's committed-warning principle. The next public pass after any such fault SHALL NOT fail with `PIPELINE_ALREADY_ACTIVE` for this flow, and `#1116` fail-closed reconcile must not convert the wedged shape back into a held row. The identical pre-existing boundary defect reachable through automatic `absence_retry_permitted` and other reservation writes is explicitly out of this change's scope and is tracked in #1796 (pre-existing @master; independently verified against merge-base `23d774bb`).

### D10: One journal-root authority (Round 3)

Repository construction, authority reads/writes, and the public receipt locator derive their root from one safe-FS expansion/no-follow canonicalization owner. The command SHALL NOT call bare `Path.resolve()` on operator input. A hostile root (symlink loop) maps to the typed operational error before the authority append — exit 1, no traceback, zero authority bytes. A literal unexpanded `~` root yields a receipt locator that equals the actual expanded authority/replay root used by repository I/O. All fallible canonicalization happens before commit.

## Selected Risk Packs

- Public API / CLI / script entry: selected — new `nhms-pipeline` command; both entrypoints, output, stderr, and exits are contract surfaces.
- File IO / path safety / overwrite: selected — append-only file journal, lock/sequence/materialization, atomic multi-record durability, explicit journal root.
- Auth / permissions / secrets: selected — operator attribution and confirmation gate an action that can cause resubmission; no secrets may enter event or CLI output.
- Concurrency / shared state / ordering: selected — stale CAS and cycle lock determine whether a concurrent bind/reclaim wins.
- Schema / fields: selected — a new decision token and event details must satisfy accepted-submit normalization and durable record validation.
- Legacy compatibility / examples: selected — automatic absence, identity release, manual retry, PostgreSQL backend, and both CLI dispatchers stay unchanged.
- Error handling / rollback / partial outputs: selected — refusal is zero-write and state/event/hydro fan-out is one transaction.
- Documentation / migration notes: selected — node-22 operator procedure and provenance distinction are user-facing; no data migration.
- Config / project setup: not selected — no new configuration key or dependency.
- Resource limits / large input / discovery: not selected — one bounded row and bounded note; no directory discovery expansion.
- Release / packaging / dependency compatibility: not selected — existing Python entrypoint and dependencies only.
- NHMS domain packs: Slurm production lifecycle / mock-vs-real parity selected for deterministic full-chain tests, a read-only node-22 census, and a conditional receipt from a naturally occurring safe target; geospatial, forcing windows, numerical runtime, database domain behavior, provider snapshots, and published display identity are not selected.

## Invariant Matrix

**Governing invariant:** only an explicitly confirmed operator request that still matches the exact durable comment-unobservable reservation attempt may atomically convert it into an audited reclaimable absence; no other row or concurrent successor may become retryable.

**Source-of-truth identity/contract:** current accepted-submit master `job_id`, contract version, submission attempt and UTC attempt anchor, plus the persisted held accounting tuple.

**Surfaces:**

- Producers: #1116 reconcile writes the held `accounting_unavailable/comment_accounting_unproven` row; unchanged.
- Validators/preflight: CLI timestamp/confirmation/note validation; accepted-submit token/tuple normalization; typed CAS predicate.
- Storage/cache/query: file-journal locked append, pipeline-job direct materialization, hydro latest materialization, pipeline event stream.
- Public routes/entrypoints: click and argparse `nhms-pipeline demote-reserved-job`.
- Frontend/downstream consumers: scheduler cycle verified-retry shortcut, reservation reclaim, existing sbatch path; no frontend.
- Failure/rollback/stale state: wrong row shape, stale attempt/anchor, concurrent bind/permit/reclaim, append failure, event validation failure.
- Evidence/audit/readiness: success JSON, durable operator event, runbook, discriminating deterministic tests, final-head CI, read-only node-22 census, and a conditional live receipt when a naturally occurring safe target exists.

**Regression rows:**

- Exact held master + valid confirmation/operator evidence → one terminal master + member fan-out + audit event; cycle shortcut true; reclaim mints a new attempt and fresh anchor.
- Any mismatch in job id/master kind/status/binding/outcome/source/decision/reason/attempt/anchor or missing confirmation/operator evidence → exit 2 / zero journal byte change.
- Automatic `absence_retry_permitted` → existing reclaim behavior unchanged.
- `identity_mismatch_released`, manual retry, generic transition, and PostgreSQL repository → remain outside the new operator reclaim contract.
- Append/event fault before commit → neither state nor audit becomes durable.
- Any non-dedicated current-contract accepted-submit writer receiving `operator_verified_absence` (submit-attempt commit, cohort defer, cohort task projection, ordinary upsert including marker-free contract upgrade, generic versioned transition) → typed-authority refusal with byte-identical journal and zero events; legitimate automatic decisions and non-token legacy upgrades still apply through their existing writers. Legacy transition/reconciliation writers remain routed to #1805.
- Post-commit projection failure during the public old-ID reclaim → no stranded pre-sbatch live `reserved` row; the flow completes the unique submit path or leaves a non-live retryable state, and the next pass never reports `PIPELINE_ALREADY_ACTIVE`.
- Hostile or unexpanded journal roots → receipt locator equals the safe-FS authority root; loop roots fail typed and pre-commit with zero authority bytes.
- No naturally occurring eligible node-22 row → record a read-only census and perform no live demotion/resubmission; deterministic full-chain evidence remains the release oracle. A later natural incident → follow the guarded procedure and retain its receipt as operational evidence.

## Boundary-Surface Checklist

- Shared helper roots: accepted-submit decision normalization and journal record validation.
- Public entrypoints: click and argparse command parity.
- Read surfaces: current master/attempt lookup and event queries.
- Write surfaces: one locked multi-record append plus direct/latest materialization; every current-contract accepted-submit writer enumerated against the operator-token closed world (submit-attempt commit, cohort defer, cohort task projection, ordinary upsert/marker upgrade, generic versioned transition, permit, release, demotion, reclaim). Legacy transition/reconciliation compatibility writers are routed to #1805.
- Producer/consumer evidence: reconcile held tuple → operator event/decision → cycle shortcut → reclaim.
- Stale/idempotency: repeat request and concurrent successor reject without bytes; post-demotion repeat also rejects.
- Unchanged consumers: HTTP manual retry, generic transitions, identity release, PG reclaim, automatic reconcile.

## Risks / Trade-offs

- **[Operator falsely attests absence]** → explicit confirmation plus named/time-stamped bounded evidence; runbook requires `sacct`/`squeue` checks. The command cannot itself prove external absence.
- **[CAS succeeds after concurrent bind]** → re-read under the cycle lock and compare all durable fields.
- **[Master changes without event or hydro fan-out]** → one validated append transaction; fault-injection tests.
- **[New token accidentally broadens generic/manual paths]** → negative whitelist tests and unchanged manual retry set.
- **[CLI drift]** → parameterized click/argparse tests and selector importer ownership.

## Migration Plan

1. Deploy code only after the deterministic held → demote → verified-retry → reclaim/resubmit chain, refusal/fault matrix, lint, strict OpenSpec validation, and final-head CI pass.
2. Inspect node-22 read-only for a naturally occurring held row. If none exists, record the census and stop: release validation SHALL NOT stop the production scheduler, make the gateway unreachable, seed a synthetic held row, rewrite journal authority, or submit a real cohort merely to create evidence.
3. If a naturally occurring held row exists and an operator independently proves it dead with the documented Slurm queries, invoke the command with the exact persisted attempt/anchor and operator evidence.
4. After that real operational use, capture the durable event, stale/repeat refusal, reclaim/new attempt, disappearance of `PIPELINE_ALREADY_ACTIVE`, and one cohort resubmission. This receipt validates the incident response in situ but is not a release prerequisite when the required pre-state does not naturally exist.
5. Rollback is a code rollback only before use. After a successful operator demotion/reclaim, do not rewrite journal history; recover through normal scheduler semantics and the audit trail.

## Open Questions

None. The issue defines the human trust decision; this design constrains how that assertion becomes durable and reclaimable.
