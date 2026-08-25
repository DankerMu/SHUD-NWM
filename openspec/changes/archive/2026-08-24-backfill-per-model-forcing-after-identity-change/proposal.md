# Backfill per-model forcing after a model identity change

## Why

Forcing artifacts are stored **per model**:

```
<object-store>/forcing/<source>/<cycle>/<basin_version_id>/<model_id>/
```

`model_id` (`dg_<32hex>`) is derived from package content, so any republish that
changes package content changes the identity. Every cycle that already produced
forcing then holds artifacts under the OLD id only, and the new model has none.

The scheduler judges forcing completeness **per cycle**, so it does not re-enter
the forcing stage for such a cycle. It submits the forecast, which dies in 1–2
seconds. Measured on node-22 after the #1816 republish of 8 basins: all 8 new
`gfs_2026082300` models were `selected` (warm-start lineage was fine) and all 8
runs exited 1 with

```
ARTIFACT_NOT_FOUND: Object storage artifact not found:
s3://nhms/forcing/gfs/2026082300/basins_heihe_vbasins/dg_f6175cdb0f3825bec4807c386b5cbf38/
```

The completeness-judgement defect itself is tracked separately (#1826) and is
NOT in scope here. What is in scope is that basins will keep iterating and ids
will keep changing, so the operator-side repair must stop being improvised.

Two things had to be learned the hard way, and both belong in the procedure:

1. **The repair is a replay, not a copy.** Comparing the 48-row registry
   manifest before and after the republish: 16 `model_id`s changed, and inside
   `direct_grid_forcing` only `binding_uri`, `binding_checksum`,
   `model_input_package_id` and `station_bindings` moved — and the
   `station_bindings` rows are identical once the `dg-<src>-<hex>::` identity
   prefix is stripped, for all 16 pairs, with everything physical
   (`grid_cell_id`, coordinates, `shud_forcing_index`, `forcing_filename`)
   unchanged. Re-running the producer under the new id therefore reproduces the
   forcing numerically, with self-consistent ids and checksums, through the code
   path production already uses. Copying the old directory instead would bake the
   old `model_input_package_id` / `binding_uri` / station ids into every member
   file, while `met.met_station` is registered under the NEW binding identity.
2. **An already-failed run does not self-heal when the artifact reappears.**
   `ARTIFACT_NOT_FOUND` classifies as permanent, so the 8 runs stayed
   `blocked` / `permanent_failure_guard` after the backfill — retry budget
   untouched (`submission_attempt: 1` against a limit of 6). Restarting them
   requires the policy-gated manual-retry marker, whose
   `classify_failure(..., manual=True)` flips `permanent` to `False` for exactly
   the marked run.

## What Changes

- **ADDED**: `scripts/node22_backfill_forcing_for_model_ids.py` — pairs two
  registry manifests by `(sp_att_path, source_id)`, derives the id-change set,
  finds cycles holding the old id but not the new, replays the producer per
  `(model_id, cycle)`, and verifies equivalence.
- **ADDED**: `scripts/node22_manual_retry_failed_runs.py` — records one
  policy-gated manual-retry marker per named run through
  `FileJournalRetryService.record_manual_repair`, previewing the target row
  before writing.
- **ADDED** requirement: a per-model forcing backfill SHALL replay production,
  SHALL prove equivalence against the superseded package, and SHALL refuse when
  the bindings actually moved.
- **UPDATED**: `docs/runbooks/current-production-ops.md` — the new-basin
  onboarding procedure gains a fifth hop covering both halves.

## Non-goals

- Fixing the per-cycle forcing-completeness judgement (#1826).
- Decoupling package identity from calibration (#1813).
- Re-issuing any forecast produced before the republish. Already-issued history
  is not retroactively corrected.
