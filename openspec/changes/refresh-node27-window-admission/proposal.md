## Why

The live I8 preparation rejects seven legitimate historical ledger entries even though the only pending migration is
`000059_river_timeseries_narrow_expand.sql`. Those SQL files were intentionally removed by
`b97c16e28b1b4ddd7a55c93d7b9682f764c5d8a3`; their applied history must remain intact.

The previously approved tools freeze runtime `1a32ebb7`. The final pre-fixture master snapshot is
`415cbd1e9d0eee39ba0dfb623a586b02cbb340f2`; later master movements are not adopted automatically. Window admission and
governance pin removal must agree on this target without rewriting ownership state.

## What Changes

- Make both window preparation and the migration worker use the migration owner's exact pending-file contract,
  preserving the complete ledger before, after, and across rollback.
- Requalify the frozen target and governance handoff against the changed runtime interfaces, retaining the healthy
  `1a32ebb7` governance pin until the actual cutover succeeds.
- Deliver two ordered small PRs: historical-ledger compatibility first; frozen-runtime and governance handoff second.
  The first slice is independently mergeable and is not folded into the second.
- Preserve the original five-round review history at tool revision `f24c3fb37be392300052c0d0ebf0ee35366a4599`. These are
  explicit child changes, not a renamed sixth ordinary round.

## Capabilities

### New Capabilities

- `node27-window-admission`: exact migration admission, immutable runtime qualification, and owned governance handoff
  for the I8 deployment.

### Modified Capabilities

None. The migration SQL, parser/reader contract, retention policy, and production window's safety invariants are
unchanged.

## Impact

The executable owner is this change's `tools/` directory. The four necessary Python tools are adopted from immutable f24
receipts with original hashes and a separate semantic delta; historical receipts remain unchanged. No matching execution
owner exists in the frozen target's scripts, services or OpenSpec tree.

Node-27 hosts the isolated and read-only live qualification. There are no node-22 operations, shared permission edits,
retention executions, migration-ledger deletions, new environment keys, or general-purpose deployment framework changes.
Actual T0, migration, production pin removal, #2280 response validation, and the remainder of #1987 task 5.2 follow only
after qualification and the merge gate.
