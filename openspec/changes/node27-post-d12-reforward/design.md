## Context

Live read-only facts (2026-09-15T00:47Z, node-27, `READ ONLY` transactions, no table scans of the legacy store):

| Fact | Value |
|---|---|
| Catalog | `hydro.river_timeseries` OID 24541 (OLD, owner `nhms_ingest_rw`); `hydro.river_timeseries_narrow_rollback` OID 309763188 (owner `nhms_ingest_rw`); no `_legacy` |
| Ledger | contains `000059`; staged NEW pending set is empty |
| Routes | all 8177 runs `legacy` (D12 blanket update); column default still `'narrow'`, NOT NULL |
| Retained facts | one run, `fcst_gfs_2026081912_dg_945b…` (run_key 17892), 512 232 rows, `published`, `parsed_at` 2026-09-14 21:10:49Z, 8 uncompressed 1-day chunks |
| Since D12 (21:12:08Z) | 0 runs created, 0 runs parsed; 337 `succeeded` + unparsed runs exist, OLD timers active |
| Lifecycle | compression and retention target `hydro.river_timeseries` by name; the rollback table is not touched |
| Prior state | `issue2370-window-1b8b2b5b447d-retry-prepare`: `BLOCKED_FENCED` (historical; OLD restored manually), `old_oid` 24541, `narrow_oid` 309763188, `narrow-routes-before-reverse.json` |

The retained run is the only run whose facts live exclusively in the rollback table. Today it is a published run with no
visible facts under OLD. Reattaching makes it visible again without a reparse.

## Goals / Non-Goals

Goals: one explicit, re-admitted transition from the retained-D12 state to the expanded state, preserving both OIDs,
all facts, the ledger, SHUD artifacts and per-run route provenance; D12 from any failure point; repeatable (a second
D12 followed by a third attempt uses the same transition).

Non-goals: production execution; re-running `000059`; a general permutation solver; changing NEW `415cbd1e` (the
readiness fix lives in the executor, not in the deployed application); contract/#1988 work.

## Decisions

### D1 Reattach, not drop-and-re-expand

Rename the canonical OLD table to `river_timeseries_legacy` and the retained table back to `river_timeseries` in one
transaction. The alternative — DROP the rollback table, delete the `000059` ledger row and re-run the migration — needs
a ledger DELETE and a data DROP (both forbidden) and a reparse of every retained run. `000059` is safe against the
reattached shape anyway: its ledger row prevents rerun, and its DO body is idempotent under `to_regclass` guards.

### D2 Separate commands and phases; no phase-only bypass

`reprepare --state NEW_DIR --config PRIVATE.json` creates a fresh state (the directory must not exist) and ends in phase
`REPREPARED` with `mode: "reforward"`. `reforward --state NEW_DIR --go Danker` accepts only `REPREPARED` + `reforward`.
`window` accepts only `PREPARED` with no/initial mode. Neither command reads, resumes or writes the failed original state
as executor state; the original initial-rollout path is behaviorally unchanged.

`reprepare` shares every runtime admission of `prepare` (uid, frozen SHAs, container/PGDATA identity, clean OLD source,
restore refs, staged NEW, admission artifacts, proxy/public probe, unit snapshot, protected files, HOLD, schema-only
dump, role audit, baseline API, OLD legacy read) and replaces only the catalog/ledger block with D3.

### D3 Re-admission (read-only; before T0 and again inside the fence)

Config adds `reforward.provenance_state` (absolute prior state directory) and `reforward.provenance` =
`{state_sha256, routes_sha256}` pinning that directory's `state.json` and `narrow-routes-before-reverse.json`, plus
`reads.retained.request` for one retained run. Admission requires, refusing with a typed check before any mutation:

1. Provenance: private regular files match the pinned hashes; the prior directory differs from the new state; no
   executor holds the prior lock (shared non-blocking flock on a read-only descriptor); prior phase is
   `RECOVERED_OLD_RETAINED` or `BLOCKED_FENCED`; prior `old_oid`/`narrow_oid` are distinct integers.
2. Catalog: exactly `{river_timeseries: prior old_oid, river_timeseries_narrow_rollback: prior narrow_oid}` (no
   `_legacy`), both owned by `nhms_ingest_rw`. Extra, missing or foreign (same name, other OID) tables refuse.
3. Ledger: equals prior `ledger_before` ∪ {`000059`} exactly; staged NEW pending set is empty.
4. Route column: exists, default `'narrow'`, NOT NULL.
5. Narrow shape of the rollback table: the same columns, 1-day dimension, index keys and compression settings that
   `validate_expand` requires of the canonical narrow table.
6. Retained provenance: the distinct `run_key`s of the rollback table (loose index scan on the primary key) must each
   appear in the prior snapshot with a non-NULL `parsed_at`, and the live `hydro_run` row must match the snapshot's
   `run_id` and `parsed_at` exactly, with status `parsed` or `published` (publication does not reparse). A snapshot run with non-NULL `parsed_at` whose key has no retained facts
   also refuses. `reads.retained.request.run_id` must be a retained run.

Rule 6 avoids scanning the TB-scale OLD store (a `run_key` probe decompresses every compressed chunk). It is sound
because every OLD parse — first parse or the autopipe recompute of an already-published run (`a8db554d`
`workers/output_parser/parser.py` `mark_run_parsed`, #1789) — stamps `parsed_at = now()` unconditionally in the parse
transaction. An OLD reparse of a retained run after D12 therefore changes `parsed_at` and refuses
(`RETAINED_RUN_CHANGED_SINCE_D12`) instead of reattaching a run whose facts now exist in both stores; resolving such a
run is an operator decision outside this transition.

The state saves `old_oid`, `narrow_oid`, `ledger_before`, `retained_runs` (`run_key`, `run_id`, `status`,
`parsed_at`), copies of both provenance files, and the live route inventory. Re-forward repeats checks 2–6 before T0
(pre-admission refusal: no service stop) and again after the drain, immediately before reattach.

### D4 Reattach transaction and route derivation

One psql transaction, `lock_timeout 5s`: a DO block re-verifies both OIDs and `_legacy` absence (`REATTACH_FENCE`),
renames OLD → `river_timeseries_legacy`, rollback → `river_timeseries`, then sets every run's route:

- `run_key` in the admitted retained set → `narrow` (facts exist only in the reattached table);
- otherwise `parsed_at IS NOT NULL` or status `parsed`/`published` → `legacy` (includes OLD-window parses of runs that
  were created after D12 with the column default `narrow`);
- otherwise `narrow` (unparsed; NEW parses into the reattached table).

The ledger must be unchanged afterwards. `validate_expand` runs with the reforward rules: `_legacy` OID equals
`old_oid`, canonical OID equals the admitted `narrow_oid`, and routes match the derivation above. Nothing is ever
silently assigned to data that does not exist: a run only routes `narrow` when it is retained (proved to have facts)
or unparsed.

### D5 Proofs after reattach

Same as the initial window: role provisioning/audit, real NEW parse of `parse_run_id` into the canonical narrow table,
narrow read of that run, **retained read** of `reads.retained` (NEW reader, route `narrow`, values equal to SQL facts in
the reattached table), legacy read equal to the admitted OLD digest, readiness-bounded start, national API proof,
ingress audit, timer restore, `WINDOW_VALIDATED` milestone only.

### D6 Recovery

Any failure after `ADMITTED` runs the existing nested `recover`. Before reattach commits the catalog is
canonical=`old_oid` + rollback=`narrow_oid` (existing branch); after it, `_legacy`=`old_oid` + canonical (existing
reverse branch), which in reforward mode additionally requires canonical = admitted `narrow_oid`. The blanket `legacy`
route update, OLD source switch, OLD read proof and restart are unchanged. The new state's
`narrow-routes-before-reverse.json` becomes provenance for any later attempt; retained facts now also include rows
parsed during the failed re-forward, and D3 admits them from that snapshot. A failure before reattach commits takes the
existing branch, which writes no snapshot: the new state is not usable as provenance (a later `reprepare` citing it
fails on the missing file, untyped), and the original D12 state remains the correct provenance because nothing was
written before reattach. The D7 interruption cases are all post-reattach; this path is not exercised by the oracle.

### D7 Oracle

The disposable matrix gains `reforward`, `reforward-rename` (SIGKILL after the reattach transaction commits),
`reforward-restart` (SIGKILL at display start) and `reforward-readiness` (health non-200 → in-process nested D12).
Each builds the retained-D12 state for real (expand, NEW parse, D12), then performs OLD-window writes (a new run created
with the default route and parsed by the real OLD parser into the canonical OLD table; one more succeeded run), then
admits a fresh state and drives the real `reforward` method. `reforward` also proves typed refusals with zero catalog,
ledger, route or service change: extra table, missing rollback, foreign rollback OID, owner change, divergent ledger,
provenance hash change, retained run changed since D12, foreign `run_key` in the rollback table, source drift before T0,
and phase/mode gates in both directions; then success, a second D12, and read-only re-admission from the second D12
state. SQL, OIDs, ledger, parser rows and reader values are real; systemd, process inspection, git selection, role
provisioning script and national HTTP transport are simulated.

## Risks / Trade-offs

- [Reattached table joins lifecycle] Once canonical again, compression/retention scripts target it by name. Expected:
  it is the NEW store; the 8 uncompressed chunks fall under normal policy.
- [Retained-set discovery cost] Loose index scan over the primary key is O(distinct runs × log n); large retained sets
  after long windows remain cheap.
- [Provenance tampering] Provenance is hash-pinned in the private GO-bound config and cross-checked against the live
  catalog and `hydro_run`; mismatch refuses.
- [Out-of-band OLD fact write without `parsed_at`] Not produced by the OLD parser, which stamps `parsed_at` in the parse
  transaction. A manual write outside the parser is recorded as residual risk, not mitigated by a TB scan.
- [NEW stays `415cbd1e`] Master has moved; catching up is ordinary post-rollout deployment, outside I8.
