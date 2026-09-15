## Why

The 2026-09-14T21:10:39Z I8 window applied `000059`, parsed the real narrow run, then failed display readiness (#2373,
fixed). D12 restored canonical OLD OID 24541 and retained narrow OID 309763188 as
`hydro.river_timeseries_narrow_rollback`; ledger `000059` and all data are retained. The executor has no supported way
forward from that state: `prepare` requires a sole canonical table and pending `[000059]`, the migration worker skips
the applied `000059`, `window` accepts only an original `PREPARED` state, and `recover` only restores OLD. Rerunning
prepare or migration is not an approved reattempt (#2374, part of #1987).

## What Changes

- Add one explicit post-D12 transition to the existing I8 executor: `reprepare` admits a fresh state from the live
  retained-D12 catalog plus hash-pinned D12 provenance; `reforward` runs the ordinary window with the migration step
  replaced by a fenced, single-transaction **reattach** of the retained narrow table.
- Route reclassification is derived from retained facts plus captured provenance, never from the post-D12 blanket
  `legacy` column value.
- Recovery after any re-forward failure reuses D12 unchanged apart from pinning the reattached narrow OID.
- Extend the disposable real-DB oracle with success, refusal and interruption cases; the initial-rollout matrix stays.
- Amend the parent expand-contract rollback text (DROP before re-expand) and runbook §4.10.3 to name reattach as the
  approved reattempt; DROP remains the only path before re-running `000059` from scratch, which this change never does.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `node27-window-admission`: adds post-D12 re-admission and re-forward requirements.

## Impact

The executable owner becomes this change's `tools/` (copied from the archived
`2026-09-15-refresh-node27-window-admission/tools/` at `507c1e112`; the archive stays byte-identical as the published
`20eb5b874` record). No migration SQL, parser/reader, retention or compression code changes. No ledger DELETE,
rollback-table DROP, implicit `000059` rerun, or reuse/overwrite of the failed original state. Frozen OLD `a8db554d`
and NEW `415cbd1e` stay. Node-27 hosts the isolated qualification only; production re-forward is parent #1987/#2280
and requires a separate explicit GO.
