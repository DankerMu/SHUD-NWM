# Design

## Risk triage

- **Fixture level**: standard. One seam in the scheduler's failure-classification
  layer, guarded by an existing hardened probe tier; no new persisted state, no
  schema change, no migration.
- **Blast radius**: every candidate that reaches the warm-start retry path with
  `restart_stage == "forecast"` — i.e. the normal republish/recalibration
  rollout path for all 31 live basins. Getting this wrong either (a) lets the
  #1816 failure recur, or (b) blocks healthy candidates that have perfectly good
  forcing. Both are production-visible within one 00/12 UTC cycle.
- **Selected risk packs**: `Run manifest / QC provenance` (identity binding of a
  recorded artifact reference), `Slurm production lifecycle / mock-vs-real
  parity` (the wrong decision costs a real array submission),
  `Published NHMS artifacts / display identity` (a blocked candidate is a gap in
  the published series).
- **Not selected**: Geospatial/CRS (no geometry touched), PostGIS/Timescale (the
  scheduler runs DB-free on node-22), SHUD numerical runtime (the failure is
  file-not-found at ~1 s, before any solve), External providers (raw ingestion
  unchanged).

## What the evidence actually says

Reconstructed read-only from node-22 pass evidence
`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026082406_79c578ca3521.json`
(`started_at 2026-08-24T06:36:57Z`, `finished_at 2026-08-24T07:00:38Z`; note the
pass ran on 08-24 — issue #1826 says 08-23, which is the *cycle* date
`gfs_2026082300`, not the pass date).

For all 8 republished basins the candidate carried:

- `status: "selected"`
- `state_evidence.decision: "retry_strict_warm_start_retry_run_manifest_mismatch"`
- `state_evidence.restart_stage: "forecast"` / `restart_from_stage: "forecast"`
- `completed_stage_evidence`: `stage: "forecast"`, `status: "succeeded"`, whose
  `run_id` is `cycle_gfs_2026082300_convert_cohort_e258b60a5dfb` — a terminal
  cohort array whose task rows carry the **OLD** `model_id`s, identical across
  all 8 basins.

The scheduler then submitted a forecast-only cohort
`cycle_gfs_2026082300_forecast_cohort_16adadb258cf` (8 array tasks). All 8
`model_run_evidence` entries: `error_code: "ARTIFACT_NOT_FOUND"`, `exit_code: 1`,
`elapsed: "00:00:01"`–`"00:00:02"`.

Object-store mtimes confirm the artifact state at decision time: under
`forcing/gfs/2026082300/<basin_version_id>/`, the OLD `model_id` directories have
mtimes `2026-08-24 09:34–09:41 +0800` (before the pass), the NEW ones
`2026-08-24 21:30–21:31 +0800` (~6.5 h after it failed, from the #1825
remediation). At 14:36–15:00 CST the new ids held nothing.

## The defect, precisely

A per-model forcing witness already exists and is hardened (#1203 / #1365,
`services/orchestrator/scheduler_state_failure.py:589`
`_missing_upstream_forecast_artifact_evidence`). `scheduler_state_decision.py`
consults it at **six** separate emitting return points, deliberately — the
comment at `scheduler_state_decision.py:335-338` states the design:

> Failure-state guard, consulted AT the emitting return points below (never as
> an unconditional pre-pass): each branch is already gated by its own failure
> condition, so healthy/running candidates never compute or reach it.

The strict-warm-start decisions are emitting return points too, but they live in
a different module (`services/orchestrator/scheduler_candidates.py`) and
**consult nothing**:

```python
# scheduler_candidates.py:2444-2456  _strict_warm_start_run_manifest_retry_evidence
payload = {
    **dict(terminal_evidence),
    "decision": "retry_strict_warm_start_terminal_run_manifest_missing",
    "restart_stage": "forecast",
    "restart_from_stage": "forecast",
    ...
}
```

`_STRICT_WARM_START_TERMINAL_RESTART_STAGE = "forecast"`
(`scheduler_candidates.py:74`) is written straight into the evidence; no witness
is asked for.

**Proven, not inferred.** The guard emits a `forcing_provenance` annotation
through `_record_forcing_provenance` *even when it does not block* — that is its
documented out-channel (`scheduler_state_failure.py:595-599`). Dumping the full
`state_evidence` key set of an incident candidate
(`dg_0883c7e9c1006c6fd347df500315e9df`, `basins_qhh`) from
`scheduler_2026082406_79c578ca3521.json` yields:

```
['candidate_identity', 'candidate_state', 'canonical_readiness',
 'completed_stage_evidence', 'decision', 'durable_output_reused',
 'durable_shud_output_reused', 'forcing_version', 'forecast_cycle',
 'generation', 'hydro_run', 'identity', 'manual_retry', 'native_shud_resubmitted',
 'nfs_raw_manifest', 'pipeline_events', 'pipeline_jobs',
 'production_identity_validation', 'ready', 'reason',
 'registry_cutover_transition', 'restart_from_stage', 'restart_stage', 'retry',
 'retry_policy', 'state_snapshot_index', 'status', 'strict_warm_start']
```

No `forcing_provenance`. No `artifact_guard`. **The witness was never consulted
for these candidates.** The `strict_warm_start` key is present, confirming which
branch minted the decision.

Note also `state_evidence.forcing_version.forcing_version_id =
forc_gfs_2026082300_dg_0883c7e9...` — the NEW model's forcing *version identity*
was minted, while no forcing *package* existed under that `model_id`. A version
id is not a witness.

A second hole sits behind the first. When the guard is reached it prefers a
recorded reference over the identity-derived one:

```python
forcing_uri = _first_artifact_uri(
    state, ("forcing_package_uri", "forcing_uri", "package_uri", "forcing_package_path"),
)
if forcing_uri in (None, "") or _is_withheld_uri_placeholder(forcing_uri):
    sidecar = _forcing_sidecar_provenance(candidate)   # identity-derived, safe
else:
    ...                                                # probes the recorded uri
```

Nothing checks that the recorded uri belongs to *this* candidate. On a
re-identification the inherited state is the superseded model's, so a non-empty
foreign `forcing_package_uri` would be probed, found present, and stand in as
this candidate's witness. This exact fail-open was already ruled out for the
sidecar tier, and the reason is written down at `_sidecar_manifest_probe_key`
(`scheduler_state_failure.py:1030-1051`):

> a sidecar copied or restored from elsewhere can name a FOREIGN manifest, which
> would then stand in as this candidate's witness and fail open

#1203 round-1 V2-C2 bound the sidecar tier's probe key to the candidate's own
identity. The recorded-uri tier never got the same treatment.

So the grain mismatch issue #1826 names is real, but it is not one missing check.
It is an existing per-model check that (1) is not consulted on the branch that
actually skips forcing, and (2) would accept a non-identity-bound reference if it
were.

## Scope correction, measured

Issue #1826 claims the defect also hits a newly onboarded basin joining
mid-history. Measured 2026-08-25 on the scheduler's own object store: 7 new
Yellow-River sub-basins registered 14:58:32 CST had full forcing for cycle
`2026081012`, both `gfs` and `ifs`, by 15:20:12 CST. A first-time `model_id` has
no terminal same-`(basin_id, cycle, source)` job for the strict-warm-start
reconcile to mismatch against, so the strict-warm-start branch is never entered
and the full convert -> forcing -> forecast chain runs. The reproducible trigger
is re-identification only.

(Caveat recorded: that pass's finished evidence artifact did not exist yet at
survey time, so the contrast case rests on object-store mtimes plus the defect
analysis, not on a finished `state_evidence` record. It does not affect the fix.)

## Decision

Two changes, one invariant:

> **No candidate state decision may be emitted with `restart_stage == "forecast"`
> without the per-model forcing witness having been consulted for that
> candidate's own `(source, cycle, basin_version_id, model_id)`.**

1. **Consult the existing guard at the strict-warm-start emitting points.** Same
   pattern `scheduler_state_decision.py` already applies six times: compute the
   decision, hand it to `_missing_upstream_forecast_artifact_evidence` as the
   planned retry, and return the blocker instead when one comes back. No new
   probe, no new derivation, no new persisted state.
2. **Bind the recorded reference to the candidate's identity.** A recorded
   `forcing_package_uri` is admissible as this candidate's witness only when its
   key path ends with this candidate's own `<basin_version_id>/<model_id>`
   segment pair. When it does not, it is treated exactly like an absent
   reference — the identity-derived sidecar tier runs — and the rejection is
   named in `forcing_provenance` rather than vanishing silently.

   The comparison must first remove exactly two trailing shapes, and nothing
   else: a trailing `/`, and a final segment equal to the package manifest
   filename. Both are documented as occurring in production — the producer
   records a directory uri while the handoff lane stores the same reference with
   the slash stripped (`_needs_package_manifest_witness` docstring,
   `scheduler_state_failure.py:1073-1100`), and a recorded reference may already
   be the manifest FILE key, one segment deeper
   (`_sidecar_manifest_probe_key`, `:1030-1051`). A bare `endswith` would reject
   the candidate's OWN reference in either shape and block a healthy candidate —
   the precise failure design.md's blast-radius note warns about. Prefix
   normalisation stays forbidden: that is the hazard, not the fix.

Alternatives considered and rejected:

- **A new per-(cycle, model_id) completeness ledger.** node-22 runs DB-free;
  this would become new persisted state in the file journal, whose record budget
  (`MAX_FILE_JOURNAL_RECORDS = 100_000`,
  `services/orchestrator/file_orchestration_journal.py:150`) was already blown
  once in #1810. The witness object on disk is the ledger; it needs no mirror.
- **A probe in the chain stage loop**, mirroring
  `cycle_download_success_missing_raw_manifest`
  (`chain_forecast_cycle.py:507`). The evidence shows these candidates never
  reach the forcing stage iteration — they are restarted at `forecast` and
  dispatched into a forecast-only cohort. A stage-loop probe is dead code for
  this failure.
- **Scoping `query_pipeline_jobs_for_cycle_context` harder**
  (`chain_forecast_cycle.py:490` unscoped cycle-wide fallback,
  `:523` model-blind `job_matches_stage`). A real latent hazard, but not what
  fired: the 8 candidates carried distinct per-model `run_id`s
  (`fcst_gfs_2026082300_dg_<model_id>`) and the bad inheritance happened upstream
  in the strict-warm-start reconcile. Reported, not fixed.
- **Automatically re-entering the forcing stage** (emit `restart_stage:
  "forcing"`, as the operator-authorized repair already does at
  `scheduler_candidates.py:1750`) instead of blocking. It would remove the
  "someone must remember to run the backfill" cost issue #1826 names — but it
  hands an unattended lane the authority to re-run production forcing, and its
  degradation when raw has been pruned rests entirely on the
  `canonical_readiness` gate (`scheduler_candidates.py:839-871`) holding. That is
  a separate decision with a different blast radius; filed as a follow-up, named
  in Non-goals.

With this change the incident shape lands in the existing **stable `blocked`**
state (`_decision_is_stable_missing_forcing_blocker`,
`scheduler_candidates.py:1574-1594`) with reason `missing_forcing_package_uri` or
`forcing_version_row_absent`, drained by
`scripts/node22_backfill_forcing_for_model_ids.py` (#1825). Eight silent 1-second
SHUD burns become one named, operator-visible block — the #1832 floor that an
unattended lane's failure must leave a name.

## Must-preserve behavior

- A candidate whose recorded `forcing_package_uri` **does** name its own
  `<basin_version_id>/<model_id>` and exists is unaffected — no extra probe cost
  beyond the segment comparison, no change of decision.
- Every existing containment stays: an unreadable probe object remains
  "cannot determine" and lands on `forcing_version_row_absent`, never on
  `missing_forcing_package_uri` (#1203 round-2 V5-C2); a withheld
  `[object-uri]` placeholder keeps taking the recovery path (#1203 round-1 C1);
  no probe fault escapes to abort the pass (#1365 round-1).
- The manifest-file key derivation stays single-sourced through
  `_package_manifest_probe_uri`; no caller hand-joins the manifest filename.
- Runs that already failed `ARTIFACT_NOT_FOUND` stay permanently classified and
  are restarted only through the #1825 manual-retry marker.

## Seams under test

- `_missing_upstream_forecast_artifact_evidence`
  (`services/orchestrator/scheduler_state_failure.py:589`) — the decision seam.
- `_forcing_sidecar_provenance` (`:966`) — the identity-derived fallback tier the
  non-bound case must reach.
- `_package_manifest_probe_uri` (`:1053`) — the single witness-object derivation.
- `_artifact_uri_missing_status` — probe containment, unchanged.

## Non-goals

Carried from `proposal.md`; plus: the unscoped cycle-wide job fallback at
`chain_forecast_cycle.py:490` and the model-blind `job_matches_stage` at `:523`
are reported, not fixed.
