# Fix node-27 raw retention lane-root locality (#2104)

## Why

`scripts/node27_raw_retention.py` promises that one lane's failure retires only
that lane (`_resolve_lane_root` docstring: "Locality is the promise this
function makes"). The promise has a hole one syscall past the gate: every probe
in `_resolve_lane_root` / `_safe_resolved_dir` stats the lane root **from its
parent**, so it needs `x` on `<object-store>` and never on `canonical/` itself.
The first syscall that needs `x` on the lane root — `_collect_mapped_lane`'s
`source_root.is_symlink() or not source_root.is_dir()` — and the
`entry.is_dir()` comprehension in `_iter_dirs` sit outside any `try`. On the
pinned CPython 3.11 an `EACCES` there escapes `collect_targets`, which runs to
completion **before** the first `rmtree`, so all three lanes end with zero
deletions and `main()` never reaches `_write_summary`: the receipt at
`NODE27_RAW_RETENTION_SUMMARY_PATH` silently stays the previous tick's file.

Not reachable on node-27's measured tree today (`object-store` 775,
`canonical` 755, `nwm` traverses it). #2100's permission remedy is the event
that could make it reachable, so this change MUST merge before #2100's mode
change lands.

## What Changes

- `_collect_mapped_lane`: the per-source root probe is wrapped; an `OSError`
  retires **that source of that lane** with a skip entry shaped like the
  lane-root `path_unavailable` entry, and the run continues.
- `_iter_dirs`: the `is_dir()/is_symlink()` comprehension moves inside the
  existing `try`, and the helper reports the failure to its caller so the
  caller records a skip instead of reading an empty listing as an empty
  directory.
- `*_root_unsafe` / `path_unavailable` skip entries (lane roots and the new
  per-source entries) carry `error` and `error_type` — additive fields; the
  `(reason, detail)` shape the existing tests pin is unchanged.
- `infra/env/node27-raw-retention.example`: clause 4 of the documented
  operator `jq` widens from `endswith("_root_unsafe")` to `endswith("_unsafe")`
  so the new per-source skips exit non-zero instead of turning a crashed tick
  (caught after 26h by the freshness clause) into a permanently green one; a
  per-source `skipped[]` reading is added and every sentence describing
  clause 4's match set or the item-1 crash path is corrected.
- Item 4 of #2104 (stale-receipt residue) needs no code: route (a) — the
  `finished_at` freshness assertion in the documented operator check — already
  landed in the same example file. This change records the proof (stale file
  → non-zero, fresh file → zero) as evidence, not as a change.

## Capabilities

### New Capabilities
- `node27-raw-retention`: the node-27 raw/canonical/precip-cache retention
  runner's lane locality contract and the diagnostic payload of its skip
  entries.

### Modified Capabilities
- (none)

## Impact

- `scripts/node27_raw_retention.py` (error handling + skip payload).
- `tests/test_node27_raw_retention.py`: new regression tests, pinned to 3.11
  semantics like the existing ancestor test; one executes the example file's
  own `jq` program.
- `infra/env/node27-raw-retention.example`: operator check + prose (no env
  key added or changed); an appended supersession pointer in
  `openspec/changes/display-v2-national-timeline-precip-overlay/tasks.md`
  where it describes the shipped clause.
- No unit, wrapper, exit-code, schema-version, or deletion-path change.
