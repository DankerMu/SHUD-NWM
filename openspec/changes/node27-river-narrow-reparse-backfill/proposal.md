## Why

The I8 re-forward (2026-09-15T02:28Z, #2374) left every run parsed before T0 routed `legacy`, with facts only in
`hydro.river_timeseries_legacy` (732 GB). The contract (#1988) was gated on retention emptying that table, about 21
days after the last legacy cycle. The SHUD `.rivqdown` artifacts of those runs are still in the object store, so the
production parser can rebuild their narrow facts. The contract gate can then open once no legacy-routed run is left
inside the retention window, instead of waiting for the last legacy chunk to age out (#2382).

## What Changes

- Add `scripts/node27_river_narrow_reparse_backfill.py` with `plan` (read-only), `run` (GO token, lifecycle mutex,
  per-run atomic route flip plus parse) and `verify` (read-only legacy-vs-narrow comparison).
- Only runs that are routed `legacy`, `published`/`superseded`, parsed, and have `end_time` inside the retention window
  are reparsed. Older runs stay `legacy` and are left for the contract to drop.
- Amend the parent `timeseries-narrow-store-expand-contract` contract gate and guard: from "legacy table holds zero
  chunks after fourteen daily receipts" to "backfill receipts + zero legacy-routed runs inside the retention window + no
  shape regression" (user decision 2026-09-15).
- Add runbook §4.10.6 with the pilot, full-run and verify procedure.

## Capabilities

### New Capabilities

- `node27-river-narrow-reparse-backfill`: historical reparse of in-window legacy-routed river runs into the narrow
  store.

### Modified Capabilities

None (the parent change's `timeseries-store-expand-contract` delta is amended in place; it is not yet archived).

## Impact

No migration, parser, reader, retention or compression code change. The runner imports the deployed parser
(`workers/output_parser/parser.py`, byte-identical between NEW `415cbd1e` and master). It writes only through the
parser's existing narrow path plus one route `UPDATE` per run. It may decompress compressed narrow chunks that overlap
the backfill range, as table owner under the lifecycle mutex. It never touches legacy facts, never demotes a route or
status, and runs no DELETE/DROP of its own. Production execution needs a separate explicit GO.
