# Localize the db-export coverage refusal to the shortfall window (#1175)

## Why

`check_drill_gate`'s db-export leg judges PER salvage-backed target window
(#1162) — live cardinality 86 windows (first-enforce receipt
2026-07-25) — but on any shortfall it still appends the #855-era bare
token `DRILL_COVERAGE_DB_EXPORT_MISSING`
(`scripts/node27_timeseries_retention.py:830,847`). The refused receipt
cannot carry `salvage_backed_windows[]` (schema `oneOf` forbids it) and
stderr echoes the same bare code, so the operator must manually intersect
every `verdict=complete` / `coverage=db-export` subject with the drop
window and compare each against the drill's tuple union — O(86) interval
arithmetic per refusal. (§7.5's remedy at HEAD is already the
receipt-driven drill re-run — #1177/#1206 forbid hand-narrowing — so the
suffix's value is DIAGNOSTIC LOCALIZATION: which window fell short,
whether the drill actually judged it, whether the subject is corrupt —
not a replacement remedy.) Pure operability gap: fail-closed semantics
are correct and unchanged.

## What Changes

Follow the established detail-suffix convention
(`RETENTION_DROP_FAILED:<schema>.<chunk_name>`,
`RETENTION_UNCAUGHT_ERROR:<ClassName>: <str>`):

- Per-window shortfall (`scripts/node27_timeseries_retention.py:846-848`,
  including the inverted-clip fail-closed arm): emit
  `DRILL_COVERAGE_DB_EXPORT_MISSING:<clipped_start>/<clipped_end>` — the
  FIRST uncovered clipped target in derivation order, serialized with the
  module's `_iso()` (UTC `Z`), `/` interval separator. Single-shortfall
  early return preserved. An inverted clip renders its (inverted)
  interval verbatim — honest evidence of the corrupt subject window.
- D2 empty-derivation branch (`:826-831`): emit
  `DRILL_COVERAGE_DB_EXPORT_MISSING:no-derivable-window` —
  distinguishable from both the bare legacy token and any ISO interval.
- `WIRE_CODES` registry unchanged: the bare code stays the registered
  token; the suffix is detail (same treatment as `RETENTION_DROP_FAILED`).
  Receipt schema unchanged (`refusal_reason` has no pattern constraint).
- Tests: all strict-equality assertion sites for this code updated to the
  exact expected suffixed string (stronger than `startswith` — each test
  knows its windows); new k-th-window localization row; D2-branch payload
  row; H6 forward/reverse token-walk stays green.
- Docs, same commit: runbook §8.2 entry (both suffix forms), §7.5
  diagnostic guidance (the suffix localizes the shortfall window;
  remedy stays the receipt-driven drill re-run), §8.7 one-sentence
  cross-note (suffix = clipped, receipt entries = unclipped — they do
  not string-match), pending `tier-node27-timeseries-storage` design.md
  #855 wire-format entry.
- Spec: ADDED requirement in `timeseries-db-retention` (this delta).

## Non-goals

- Judgment semantics (#1162 per-window clip + fail-closed) — untouched.
- forcing/runs whole-window legs; `_drill_covers` /
  `derive_salvage_backed_windows` / `_completeness_has_db_export_overlap`
  bodies.
- Refused-receipt `oneOf` shape (no `salvage_backed_windows[]` /
  `uncovered_windows[]` on refused receipts — rejected alternative:
  richer but breaks the "refused = refusal_reason only" invariant and
  the suffix precedent).
- Multi-shortfall aggregation (single-shortfall early return stays).
