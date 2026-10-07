# Design

## Decisions

- **Weights only.** Station rows stay (see proposal); after the list change they are unreachable from the
  API's basin list and, once their weights are gone, from the model list too.
- **One transaction per model**, not one for all: 606k rows in one transaction is one long lock and one
  large WAL burst on a degraded array; per model the largest unit is 124,572 rows (`basins_lh_gl_shud`), the
  median a few hundred. The pause spreads the writes.
- **Backup and delete are one statement** (`COPY (DELETE … RETURNING …) TO STDOUT`): under READ COMMITTED a
  separate COPY and DELETE read different snapshots, and equal counts would not prove the same rows (the
  retirement tool's supersede step documents this trap). The writers' advisory locks are taken first so a
  producer or a domain handoff cannot replace a scope of the model while it is being purged.
- **Rule is identity-free.** No `dg_` prefix test and no `active_flag`: the legacy baseline rows are `active`
  by design while producing nothing. Protection comes from runs, the manifest and age.
- **Manifest as a second, independent protection.** The run-based rule alone would purge a generation that
  the scheduler was just switched to and that has not produced a displayable run yet; `created_at` covers the
  first 30 days, the manifest covers a model that stays configured without displayable runs.
- **Backups on the receipt root** (object store, NFS), not on the database volume. CSV of 606k rows is on the
  order of 100–200 MB.
- **No VACUUM.** Autovacuum reclaims the space on its own schedule; the tool issues none (standing node-27
  constraint).
- **IO estimate for the apply** (the dry-run prints the row totals it is based on): about 0.6 M heap tuples and their entries in
  four indexes are marked dead — the order of the table's own size (838 MB) in dirtied pages and WAL, spread
  over 63 transactions with pauses. For comparison the forcing timeseries ingest writes on the same volume
  continuously.

## Restore

Per model: delete the rows the model has again (a producer may have rebuilt some; the unique key would
reject the copy), then client-side `\copy met.interp_weight (<column list>) FROM '<backup>' WITH CSV HEADER`.
`weight_id` is a BIGSERIAL, so written-back ids are accepted. The forcing producer also rebuilds a model's
weights (delete + insert) when it produces for that model.

## Failure states

- Failure before a model's commit: that model untouched (rollback), earlier models stay purged, receipts say so.
- Commit outcome unknown: backup kept; the rerun's classification shows whether the rows are gone.
