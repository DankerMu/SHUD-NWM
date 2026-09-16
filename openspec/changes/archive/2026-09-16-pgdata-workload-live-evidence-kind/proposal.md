# Let the PGDATA workload CLI produce a live-labelled measurement receipt

## Why

`scripts/node27_pgdata_workload.py:105` hard-codes `evidence_kind="isolated"` when it calls `measure_workload`, and
`measure`'s parser (`:42-57`) exposes no way to choose otherwise. The library already accepts `"isolated"` or
`"live"` (`packages/common/node27_pgdata_workload.py:89`) and derives the receipt's `evidence_kind` / `isolated` /
`live` triple from it (`:137-139`). So every receipt this CLI has ever been able to emit says `live: false`.

The runbook makes that fatal for #1987 task 5.2: SQL/API measurement is owned by this CLI
(`docs/runbooks/tier-node27-timeseries-storage.md` ~`:698-702`), and isolated receipts never imply live acceptance.
Task 5.2's D11 curve gate (two network pins × three storage states, ≥5 warm samples, SQL P95 ≤300 ms, API warm P95
≤500 ms, `EXPLAIN (ANALYZE, BUFFERS)` shape bounds) therefore has no shipped tool that can produce its evidence.
#1987 gates #1988.

The query layer is already narrow-store aware (`packages/common/node27_pgdata_workload_plan.py:33-34,44,397` bind
both `hydro.river_timeseries` and `hydro.river_timeseries_legacy`), so only the evidence-kind switch is missing.

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: public CLI surface, evidence-semantics field consumed as acceptance
  authority, credential-bound admission, and receipt file publication)
Blast radius: a receipt that claims `live: true` without the admission it asserts would let disposable numbers
  authorize the #1988 contract, i.e. dropping the 732 GB legacy table
Selected risk packs: Public API / CLI / script entry; Auth / permissions / secrets; File IO / path safety /
  overwrite; Error handling / rollback / partial outputs; Documentation / migration notes
Evidence floor: receipt triple correct for each kind; default unchanged; each refusal path writes no file; the
  runbook states how a live receipt is obtained
```

## What Changes

- `measure` gains an explicit `--evidence-kind {isolated,live}`, defaulting to `isolated`. The default path keeps
  its current behaviour literally.
- `live` is admitted only when both hold, else fail-closed with no file written:
  - the existing read-only session proof (`packages/common/node27_pgdata_workload_io.py:199-219`: session is
    `transaction_read_only` and `current_user` is `nhms_display_ro`);
  - `--reviewed-sha` equals the HEAD of the checkout the CLI runs from. Today `validate_sha`
    (`node27_pgdata_workload_io.py:52-57`) only checks the 40-hex shape, i.e. the SHA is stamped, not bound.
- The runbook section that owns this CLI gains the live-receipt procedure.

## Out of Scope

- Captured query semantics: the named query, digest normalization, typed `query.parameters` envelope, EXPLAIN
  binding.
- Receipt publication safety (staged private sibling, mode 0600 from the first byte, no clobber) — preserved, not
  redesigned.
- Sample counts and thresholds (one discarded warmup plus 20 serial samples).
- Producing #1987's D11 curve receipts. This change delivers the capability; #1987 collects the evidence.
