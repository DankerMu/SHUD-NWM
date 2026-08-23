# Design

Fixture level: high (expanded + Invariant Matrix)
Upstream suggested level: absent (issue is hand-authored, not pipeline Stage 5) — set to `high` because the change moves an admission gate in the *silent* direction and modifies a spec requirement whose existing scenario mandates today's behavior.
Project profile: NHMS/NWM (`openspec/project-profile.md`)

## Change surface

- `services/orchestrator/scheduler_candidates.py`
  - `_terminal_decision_matches_strict_warm_start` (`:1951`) — the `hydro_run` leg
  - the reuse call site (`:470-515`) — routing of the new third state
  - `_warm_state_record_matches` (`:2018`) — **unchanged**, still the strict comparator
  - `_terminal_decision_run_manifest_matches_strict_warm_start` (`:2001`) — body
    unchanged, but **newly called from inside** the `hydro_run` leg (see
    "Required wiring" below); its existing call-site invocation stays where it is
- `services/orchestrator/scheduler_init_state_match.py` — consumed, not changed
- `openspec/specs/strict-warm-start/spec.md` — MODIFIED requirement
- `scripts/select_ci_tests.py` — routing for the touched source files (Evidence Floor)

## The decision table (this is the contract)

Evaluated only on the `hydro_run` leg, i.e. after the `candidate_state`
terminal-source branch and the `COLD_START_QUARANTINED` escape have already
short-circuited (both stay ahead of everything below, unchanged).

| terminal `hydro_run` shape | `run_manifest_initial_state` | decision | budget |
|---|---|---|---|
| all four fields present and equal | any | reuse exit as today (run-manifest gate still applies) | unchanged |
| `init_state_id` equal, other fields **absent** | four-field match | **reuse exit (new)**, subject to the same `successor_state` readiness gate as the wide-row path | n/a |
| `init_state_id` equal, some non-id fields present and **agreeing**, remainder absent | four-field match | **reuse exit (new)**, same gate — see "Partial-but-agreeing rows" below | n/a |
| `init_state_id` equal, other fields **absent** | absent | `strict_warm_start_terminal_init_state_mismatch` | **budgeted (#1173)** |
| `init_state_id` equal, other fields **absent** | any field disagrees | `strict_warm_start_terminal_init_state_mismatch` | **budgeted (#1173)** |
| any present field disagrees (`conflict`) | any | `strict_warm_start_terminal_init_state_mismatch` | **budgeted (#1173)** |
| no identity fields at all (`absent`) | any | `strict_warm_start_terminal_init_state_mismatch` | **budgeted (#1173)** |
| `init_state_id` missing but other fields present | any | `strict_warm_start_terminal_init_state_mismatch` | **budgeted (#1173)** |

### Partial-but-agreeing rows

The discriminator is **absence vs disagreement**, not how many fields a row
happens to record. A row carrying `init_state_id` plus, say, an agreeing
`init_state_checksum`, with `uri`/`valid_time` still absent, fails
`_warm_state_record_matches` (the selected state names a `uri` the record does
not) but has no *disagreeing* present field, so `terminal_init_state_match`
answers `match` and the run manifest decides it exactly as it decides the
id-only row. That is the governing invariant applied literally — a four-field
proof exists in the recorded evidence and nothing on the record contradicts it —
and it keeps the leg agreeing with the verdict side on a shape where the two
must agree. Narrowing the leg to "every non-id field must be missing" would
reopen that divergence for no safety gain. The moment any present field
disagrees the row is `conflict` and no manifest can upgrade it (the
`any present field disagrees` row).

## Required wiring (not merely the outcomes)

The decision table above is a statement about outcomes; this section pins the
*only* wiring that produces them, because the natural first implementation
produces the wrong one.

The call site (`:478-524`) is already shaped like this:

```python
if _terminal_decision_matches_strict_warm_start(...):
    if not _terminal_decision_run_manifest_matches_strict_warm_start(...):
        -> retry "strict_warm_start_terminal_run_manifest_missing"   # UNBUDGETED
    elif successor_state is not ready:
        -> retry "strict_warm_start_successor_checkpoint_missing"
    else:
        -> skipped (reuse)
else:
    -> _strict_warm_start_terminal_mismatch_decision(...)             # BUDGETED (#1173)
```

Relaxing the `hydro_run` leg to return `True` for every id-only row whose
`init_state_id` matches — the obvious edit — sends **both** negative-pin cases
into the `True` branch, where the inner run-manifest check converts them into
`strict_warm_start_terminal_run_manifest_missing`: the unbudgeted retry this
change exists to keep unreachable. That implementation is wrong even though it
stops the recompute, and it is wrong in a way the outcome table alone does not
forbid.

Therefore: **the run-manifest four-field proof SHALL be evaluated inside
`_terminal_decision_matches_strict_warm_start`'s `hydro_run` leg, before that
function returns `True` for an id-only row.** For an id-only row the leg returns
`True` only when the manifest already proves all four fields; in every other
id-only case it returns `False`, so control falls through to the existing
`else` branch and reaches the budgeted decision unchanged. The call site's own
`_terminal_decision_run_manifest_matches_strict_warm_start` check SHALL NOT be
what decides the id-only + absent-manifest or id-only + disagreeing-manifest
outcome; for id-only rows that reach the `True` branch it is a redundant
re-check that passes by construction.

The call site's structure — including the `successor_state` readiness gate
between the manifest check and the skip exit — SHALL remain as it is. The new
reuse row is subject to that gate exactly like the wide-row path: an id-only row
with a proving manifest whose `successor_state` is not ready still yields
`strict_warm_start_successor_checkpoint_missing`, not a skip.

Two rows carry the whole risk of this change and are the load-bearing negative pins:

1. **id-only + manifest absent ⇒ still mismatch.** This is the corner
   `3b587c55` and the current spec scenario protect. Reusing on the id alone
   would reopen the repaired-checkpoint hole and would additionally reroute a
   budgeted decision onto the unbudgeted
   `strict_warm_start_terminal_run_manifest_missing` retry — the exact rerouting
   the requirement forbids. The new path therefore never enters the true branch
   for id-only rows; it either produces the reuse exit directly (manifest
   proves four fields) or leaves the existing false branch untouched.
2. **id-only + manifest checksum disagrees ⇒ still mismatch and recompute**,
   with the same `state_id`. This is the repaired-checkpoint case itself.

## Direction asymmetry (why the negative pins are the critical tests)

Today's defect is **loud and wasteful**: work that was already done is done
again, and the results agree. The fix moves toward **reuse**, which is the
silent direction: a wrong reuse skips required forecast replay and nothing
alarms. Positive tests only prove the waste stopped; the negative pins are what
prove no silent skip was bought. Reviewers weight them accordingly.

## Verdict/candidate parity guard

The two sides are intentionally *not* the same function — the candidate side
keeps the special branches and the budget routing the verdict side must never
inherit. The guard therefore pins agreement only where agreement is mandatory:
for a terminal `hydro_run` row whose `init_state_id` matches and whose run
manifest proves the four fields, `scheduler_discovery`'s verdict path and the
candidate ladder SHALL both treat the row as current. Divergence outside that
shape (the `candidate_state` branch, the `COLD_START_QUARANTINED` escape,
strict resolutions without `candidate_state`) is contract, not drift, and the
guard asserts the divergence rather than forbidding it.

## Dead dedup gates verdict (issue acceptance item 5)

Three duplicate-pipeline dedup gates in `build_candidates`
(`scheduler_candidates.py`) each carry a `not callable(state_provider)`
conjunct. They sit in the discovery loop **ahead of** the strict-warm-start
resolution, and are identified by their guards, not by line number:

1. `has_active_orchestration and not cancel_active_slurm and not callable(state_provider)` → `active_duplicate_pipeline`
2. `not cancel_active_slurm and not callable(state_provider) and active_repository.has_active_pipeline(...)` → `active_duplicate_pipeline`
3. `completed_provider(...) and strict_warm_start is None and not callable(state_provider) and _successor_state_terminal_can_skip(...)` → `completed_duplicate_pipeline`

A **fourth** site (`cycle_active_blocks_candidate`) also combines
`not callable(state_provider)` with the `active_duplicate_pipeline` reason
string, but sits **after** the strict-warm-start resolution and is guarded
additionally by `candidate_state_scoped_retry_detector`. It is a different gate,
explicitly **out of scope** here — neither audited nor changed. `state_provider` is
`getattr(active_repository, "candidate_state", None)` (`:239`). Both production
planes implement it — DB `chain_repository.py:113`, file
`file_orchestration_journal.py:824` / `scheduler_file_providers.py:535` — so all
three gates are unreachable in every production deployment, not only db-free.

**Decision rule (not a free choice):** the implementer audits every
`active_repository` shape reachable in production *and in the test suite*. If no
supported deployment lacks `candidate_state`, the gates are dead in the
strictest sense and are deleted, with the audit recorded. If any supported
deployment or exercised test fixture relies on them, they are kept and pinned by
a test that names that deployment, and the issue's "delete" option is refused in
writing. Either way the verdict and its evidence land in `tasks.md` and the PR
body — silence is not an outcome.

## Must preserve

- `_warm_state_record_matches` semantics and all its other call sites.
- Every decision shape, reason string, and evidence key currently emitted for
  non-id-only rows, byte-identical.
- The #1173 budget routing: every not-match outcome still reaches
  `_strict_warm_start_terminal_mismatch_decision`, never the unbudgeted
  run-manifest-missing retry.
- The `candidate_state` terminal-source branch and `COLD_START_QUARANTINED`
  escape order and effect.
- Verdict-side `terminal_init_state_match` unchanged (#1183).

## Seams under test

- `services.orchestrator.scheduler_candidates.build_candidates` — the public
  admission boundary; all decision-table rows are exercised through it, not
  through the private predicates.
- `services.orchestrator.scheduler_discovery.cycle_completion_status` — the
  verdict boundary for the parity guard.

## Selected risk packs

- **Concurrency / shared state / ordering**: selected — the predicate reads
  persisted pipeline-job/hydro_run state and decides whether to resubmit Slurm
  work; retry-attempt accounting (#1173) is shared state.
- **Legacy compatibility / examples**: selected — the entire defect is legacy
  row shape (id-only) meeting a comparator written for wide rows; backlog rows
  are never rewritten.
- **Error handling / rollback / partial outputs**: selected — the not-match
  branch resubmits native SHUD and declares `durable_output_reused: False`; a
  wrong reuse leaves a cycle claiming success it never recomputed.
- **Schema / columns / units / field names**: selected — the change is entirely
  about which init-state identity fields are present/absent/disagreeing.

## Risk packs considered (core)

- Public API / CLI / script entry: not selected — no CLI or route changes; the
  scheduler entrypoint signature is untouched.
- Config / project setup: not selected — no new config key; no env surface.
- File IO / path safety / overwrite: not selected — no writer changes; the
  journal writers stay as-is by explicit non-goal.
- Schema / columns / units / field names: selected (above).
- Auth / permissions / secrets: not selected — no credential or role surface.
- Concurrency / shared state / ordering: selected (above).
- Resource limits / large input / discovery: not selected as a *change* surface;
  noted as beneficiary — shrinking the cohort is expected to drop pass evidence
  back under `MAX_EVIDENCE_BYTES` (#1748), but no limit logic is touched.
- Legacy compatibility / examples: selected (above).
- Error handling / rollback / partial outputs: selected (above).
- Release / packaging / dependency compatibility: not selected — no dependency
  or packaging change.
- Documentation / migration notes: not selected — behavior converges toward the
  documented verdict-side rule; no operator procedure changes.

Domain packs (NHMS/NWM profile):

- Warm-start / state lineage: selected — the predicate decides whether a warm
  state is honored; the repaired-checkpoint protection is the core invariant.
- Slurm dispatch / job identity: not selected as a change surface — no
  submission or identity code is touched; submission *volume* changes.
- Time-series / forcing: not selected — forcing admission is a different rung.

## Invariant Matrix

- **Governing invariant**: a terminal-success candidate is reused instead of
  recomputed if and only if the state that produced it is proven — by full
  four-field identity somewhere in the recorded evidence — to be the state
  strict warm start selected; absence of proof always recomputes through the
  budgeted path, never through the unbudgeted one.
- **Source-of-truth identity/contract**: the init-state identity quadruple
  `(state_id, checksum, uri, valid_time)`, read via `init_state_field` aliases.
- **Producers**: `file_orchestration_journal.create_hydro_run` /
  `create_hydro_run_from_basin` (id-only
  `hydro_run` rows); run manifest writer supplying
  `run_manifest_initial_state`; `chain_repository_state.candidate_state`.
- **Validators/preflight**: `_terminal_decision_matches_strict_warm_start`,
  `_terminal_decision_run_manifest_matches_strict_warm_start`,
  `_warm_state_record_matches`, `terminal_init_state_match`.
- **Storage/cache/query**: file journal jsonl rows and the DB `hydro_run` table;
  neither is written by this change.
- **Public routes/entrypoints**: `build_candidates`, `cycle_completion_status`.
- **Frontend/downstream consumers**: none — decisions surface only in scheduler
  pass evidence.
- **Failure paths/rollback/stale state**:
  `_strict_warm_start_terminal_mismatch_decision` (budgeted retry then
  `blocked_strict_warm_start_init_state_mismatch`);
  `strict_warm_start_terminal_run_manifest_missing` (unbudgeted — must stay
  unreachable from the new path).
- **Evidence/audit/readiness**: pass evidence `candidates[].state_evidence`,
  `counts.skipped_candidate_count`, `counts.submitted_count`; node-22 live
  receipt.
- **Regression rows**:
  - id-only `hydro_run` with matching `state_id` + four-field-matching run
    manifest -> candidate `skipped` with the terminal reason, no
    `retry_strict_warm_start_terminal_init_state_mismatch`, no submission.
  - id-only `hydro_run` with matching `state_id` + four-field-matching run
    manifest + `successor_state` **not ready** ->
    `strict_warm_start_successor_checkpoint_missing`, as the wide-row path
    already does; the new row does not bypass that gate.
  - id-only `hydro_run` with matching `state_id` + run manifest whose
    `checksum` disagrees -> `retry_strict_warm_start_terminal_init_state_mismatch`
    with the budget block unchanged, native resubmission.
  - id-only `hydro_run` with matching `state_id` + **no** run manifest ->
    `retry_strict_warm_start_terminal_init_state_mismatch` (budgeted), **not**
    `strict_warm_start_terminal_run_manifest_missing`.
  - wide `hydro_run` row that fully matches -> byte-identical to today,
    including the run-manifest-missing route.
  - `candidate_state` terminal-source branch and `COLD_START_QUARANTINED`
    escape -> unchanged, still candidate-side only.
  - verdict path for every shape above -> unchanged from today (#1183).

## Review focus

1. Can any id-only shape reach the reuse exit without a four-field run-manifest
   proof? (silent-skip regression)
2. Can any not-match outcome reach the unbudgeted
   `strict_warm_start_terminal_run_manifest_missing` retry that could not
   reach it before? (budget bypass — check the wiring, not just the tests: the
   manifest proof must gate the leg's own return value per "Required wiring")
3. Are the wide-row and special-branch paths byte-identical, evidence keys
   included?
4. Does the parity guard assert the mandatory agreement without forbidding the
   intentional divergences?
5. Is the dead-gate verdict backed by an actual audit, not an assumption?
