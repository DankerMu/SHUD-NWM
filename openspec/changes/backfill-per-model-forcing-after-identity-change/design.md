# Design

## Risk triage

**Fixture level**: standard. Two operator scripts, no production runtime code
touched. Blast radius is the node-22 object store and the DB-free file journal;
both are recoverable, but a wrong write is expensive (a bad forcing package
would feed SHUD silently, and a marker aimed at the wrong journal row would
restart work that is already correct).

**Risk surfaces selected**

| Surface | Why | Mitigation under test |
|---|---|---|
| Identity/provenance | The whole change exists because identity moved | D1, D2 |
| Destructive write | Producer writes into the live object store; the marker mutates the live journal | D3, D4 |
| Silent under-coverage | A scan that finds nothing looks exactly like "nothing to do" | D5 |

**Not selected**: concurrency (each work item writes its own `model_id`
directory, and the marker takes the journal's cycle write lock), schema
migration, auth (the marker uses the existing `pipeline.retry_run` policy gate
rather than introducing a channel).

**Must-preserve**: already-issued forecast history is never rewritten; the
producer's own output format is authoritative; no journal row is hand-edited.

## D1 — Replay, not copy, and not rewrite

Three candidate mechanisms:

1. **Copy** the old directory to the new id. Rejected: the old
   `model_input_package_id` / `binding_uri` / station ids are embedded in every
   member file (`forcing.tsd.forc` header, `forcing_debug.csv`'s `station_id`
   column, `payloads/*.json`, and the three JSON manifests), while
   `direct_grid_variant_registration.py:566` registers `met.met_station` under
   the NEW binding identity. The copy is internally checksum-consistent, so it
   would likely pass the runtime gate — it would just be lying.
2. **Rewrite** the ids in place. Rejected: it must then recompute nested
   checksums across five formats, and its correctness criterion is "equals what
   the producer would have written". If that is the criterion, run the producer.
3. **Replay** under the new id. Selected. Inputs are raw GRIB x station
   bindings; the bindings are physically identical (D2), so the output is
   numerically identical by construction, through the path production uses.

Replay is only available while the raw inputs survive retention. If they do not,
this tool must refuse rather than silently fall back to a copy — the fallback
would be exactly the mechanism rejected above.

## D2 — The equivalence oracle is free, and it is `shud/`

Because the bindings are physically identical, the replay must reproduce the
superseded package. That gives a bit-level acceptance check without inventing a
tolerance:

- `shud/*.csv` — what SHUD actually reads, keyed by filename and carrying no
  identity string — MUST match **byte for byte**.
- `forcing.tsd.forc`, `forcing_debug.csv`, `payloads/*.json` MUST match after
  `dg-<src>-<hex>` and `dg_<32hex>` are normalised away.
- The three JSON manifests are deliberately **excluded**. They carry member
  checksums, which must differ once the members' identity strings do. Including
  them would make a correct replay fail.

Measured on node-22 for all 16 pairs: 0 `shud` mismatches (8–295 files per
basin), 0 normalised mismatches.

## D3 — Refuse the shapes that are not a backfill

Two inputs look superficially like an id change and are not:

- **Key set diverged** — the two manifests do not describe the same
  `(sp_att_path, source_id)` set. That is an onboarding/retirement diff. Hard
  error, exit non-zero, nothing produced.
- **Bindings moved** — the normalised `station_bindings` still differ. That is a
  real re-binding and must go through normal provisioning. Recorded in
  `rebound_models_skipped`, skipped, non-zero exit.

A changed `basin_version_id` on a renamed model is also a hard error: the
forcing path root moved, so a replay would not land where the old artifacts are.

Both scripts default to a preview. The backfill's default is a dry run; the
marker's preview additionally **names the row it would act on**, because the
forecast stage carries a cohort-master job covering every model in the cycle and
a marker aimed at that row would restart the whole cohort. On the live incident
the preview resolved to `job_fcst_..._forecast_reconciled_34817_6`, the per-run
row — that distinction is worth seeing before writing, not after.

## D4 — The marker is the sanctioned channel, not a journal edit

`record_manual_repair` takes the cycle write lock, requires
`pipeline.retry_run` policy evidence, refuses on conflict (`run_active`) and on
absence (`no_retryable_failed_job`), and writes an evidence trail. The runbook's
prohibition is on **hand-editing journal rows**; using the typed, gated mutation
API is the alternative it points at, not a violation of it.

Measured: `classify_failure('ARTIFACT_NOT_FOUND', ...)` gives
`{'retryable': False, 'permanent': True}`; with `manual=True` it gives
`{'retryable': False, 'permanent': False, 'manual_retry_marker': True}`. That
flip, scoped to the marked run, is the entire mechanism.

## D5 — Silent under-coverage is the failure mode to design against

A scan keyed on a path that does not exist returns "no work" and looks identical
to success. This bit during development: the forcing path's source segment is
`normalize_source_id(x).lower()` (`producer.py:1959`), so canonical `IFS` lives
under `forcing/ifs/`. Scanning by the canonical id found 7 gfs items and **zero**
of the 8 IFS items, and the dry run reported that as a clean result.

Consequences for the design: the source segment is derived by a helper that
mirrors the producer's, with a comment naming the trap; the case is pinned by a
test; and the receipt reports `renamed_model_count` alongside `work_item_count`
so an operator can see that 16 renames produced 15 work items and ask why,
rather than reading a bare item list as complete.

## D6 — D5's failure mode has four instances, not one

Round-2 review found that the first implementation guarded only the instance D5
happened to describe (the lower-cased source segment). Three more shapes of the
same "looked like nothing to do" failure were live, and one shape of losing the
evidence entirely:

1. **A partial `target_dir`** — producer writes are atomic per FILE, never per
   directory (`producer.py:2101-2108`, `object_store.py:207-214`), so a killed
   producer leaves the directory present with only some members. Gating
   discovery on `is_dir()` made that item invisible in the receipt AND
   unreachable by every later run. Now the existing directory must pass the
   same oracle a replay must pass; if it does not, the item is reported
   (`existing_target_unverified`, non-zero exit) and left untouched. Replacing
   it is opt-in (`--replace-unverified-target`), because the tool did not write
   it — destroying another writer's output is the operator's decision, not a
   default. The replacement moves it to quarantine rather than deleting it.
2. **A wrong `--forcing-root`** — every rename's source directory is absent, so
   the receipt reads `renamed_model_count: N, work_item_count: 0`, which is
   *also* the steady state of a legitimately fully-covered rerun. The count pair
   cannot discriminate them; the probe can. `probe_coverage` records what was
   probed and what was found, and total under-coverage refuses
   (`BACKFILL_FORCING_ROOT_ABSENT` / `BACKFILL_FORCING_ROOT_UNCOVERED`). Partial
   under-coverage stays non-fatal — `--cycle` narrows the scan by design — but
   becomes legible in the receipt's `coverage` block.
3. **A live `verification_failed` artifact** — the forecast stage reads
   `<basin_version_id>/<model_id>/` directly, so a package that failed its own
   acceptance oracle must not stand there. It is moved to
   `_backfill_quarantine/quarantined-…` (leading underscore, and a name that
   cannot match `dg_<32hex>`), with the path in the receipt. Moving, not
   deleting: the bytes are the diagnosis. Same for a producer that exited
   non-zero and left debris.
4. **One item's exception discarding the whole receipt** — this is not a
   `ThreadPoolExecutor` problem; the serial path lost it identically. Guarded at
   three levels: `verify_item` treats an unreadable member as a mismatch rather
   than a raise, `run_item` records any escaping exception as `errored` on its
   own item, and the receipt build plus `--output` write sit outside the item
   loop.

The boundary between 1 and 3 is worth stating because it looks contradictory:
**this tool quarantines what this run produced or replaced; it reports, and
leaves in place, what it merely found.**

Round 3 found that the fix for 3 reopened 1. `quarantine_target` returned
`None` both when there was nothing to move and when the move FAILED, and no
call site checked; a failed rename therefore produced a `produce_failed` /
`verification_failed` status byte-identical to a successful quarantine, while
the unverified package went on standing on the path the forecast stage reads.
That is not hypothetical on NFS-backed `/scratch`: ESTALE, permission drift, and
a `mkdir` of the quarantine parent hitting quota all surface as `OSError`. The
boundary gets a third clause: **a quarantine that fails is its own outcome
(`quarantine_failed`), it says the artifact is still live and where, it exits
non-zero, and on the replace path it does not produce** -- producing into a
surviving partial package would overwrite the same-named members and leave the
strays, because writes are atomic per file and never clear the target
(`object_store.py:207`). Per-attempt reasons accumulate in a list rather than
under one key that a second attempt would overwrite.

The same round found the mirror-image error in the preview: dry run PLUS
`--replace-unverified-target` overwrote the `existing_target_unverified` status
with `dry_run` and so exited 0 for a state that exits 1 without the flag. The
spec delta's "unless an explicit opt-in replacement flag was given" is scoped to
*left as found*, not to *exits non-zero*. The preview now keeps the status and
the exit code and carries `would_replace_target` as its preview signal: **a
preview must never be a greener light than the state it previews.**

## Seams under test

- `resolve_renames(previous, current)` — pairing and the two refusals.
- `discover_work(renames, forcing_root, cycles)` — cycle selection, including
  the lower-cased source segment.
- `verify_item(item)` — the equivalence oracle, in both directions.
- `probe_coverage(renames, forcing_root, cycles)` — what was probed, what was
  found; `require_coverage` turns total under-coverage into a refusal.
- `run_item(item, argv, dry_run, replace_unverified_target)` — the replay, the
  opt-in replacement, the quarantine, and the per-item error containment.
- `_preview(service, run_id)` — per-run vs cohort-master row resolution.

## Evidence mapping

| Requirement | Evidence |
|---|---|
| Replay reproduces the superseded package | node-22 receipt: 16/16 `verified`, 0 mismatches |
| Refuses a real re-binding | unit test; live `rebound_models_skipped: 0` |
| Does not silently under-cover a source | unit test for the lower-cased segment; live 15 items |
| Marker targets the per-run row | unit test; live preview receipt |
| Restarted run actually succeeds | node-22 pass receipt after the marker |
