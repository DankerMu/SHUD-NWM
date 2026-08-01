# Design: fix-retention-drill-snapshot-binding (#1220)

## D1 — Triage decision: requirement-set binding, NOT snapshot-identity
## binding (resolves the issue's needs-triage question)

The issue left one policy question open: strict snapshot identity (digest
equality, or refuse when the gate-time completeness receipt's
`generated_at` is newer than the drill's recorded one) vs requirement-set
binding. The arithmetic decides it:

- The completeness receipt is rewritten IN PLACE every day at 03:40 UTC
  (`nhms-node27-storage-inventory-audit.timer`, runbook
  `docs/runbooks/tier-node27-timeseries-storage.md:94-101`) and the gate
  requires it fresh within 26 h
  (`_DEFAULT_COMPLETENESS_MAX_AGE_HOURS = 26`).
- The drill budget is 30 days (`_DEFAULT_DRILL_MAX_AGE_DAYS = 30`).
- Therefore at virtually every gate tick the loaded completeness receipt
  is NEWER than the drill's snapshot. Digest equality or newer-refusal
  would refuse ~every tick after the first day → drill validity collapses
  from 30 days to <1 day → daily mandatory restore drills or permanent
  retention outage. Rejected.

Requirement-set binding refuses only when the drift MATTERS: a
salvage-backed target window exists at gate time that the drill's snapshot
did not contain. Daily regeneration with an unchanged db-export universe
keeps passing; the issue's v1/v2 replay (new subject B) refuses.

`completeness_generated_at` is still recorded — diagnostics only (which
snapshot did the drill consume), never a refusal input. This MUST be
stated in §7.5 so operators do not read it as an enforcement field.

## D2 — What the drill records: the FULL db-export universe, window-
## granular, unfiltered

`salvage_derivation.db_export_windows` = unique sorted `{start, end}`
string pairs of every completeness subject with `coverage == "db-export"`
AND `verdict == "complete"` in the receipt the drill consumed — the same
normalization as `derive_salvage_backed_windows`
(`scripts/node27_timeseries_retention.py:847-882`: string endpoints only,
dedup by exact pair, ascending sort) MINUS the drop-window overlap filter.

Why unfiltered — the honest reason (fixture-review P2-2 corrected an
inverted first draft): unfiltered is the SIMPLER record (no second filter
implementation on the emit side, matches the already-recorded
`candidate_count` universe semantics at
`scripts/node27_archive_rebuild_drill.py:381-386`) and is
filter-independent (survives future changes to the drill's own candidate
selection). It is NOT what makes the binding safe: an unfiltered record
can contain windows a narrowed drill filtered out and never
restore-verified, and such a window would pass the membership check and
fall back to the tuple union. That case is unreachable ONLY because
#1207's containment guard runs first and forces retention-drop ⊆
drill-drop, so every gate target overlaps the drill window and was
therefore in the drill's own candidate set. This is a real dependency on
the #1207 guard and is recorded as residual D5-(d).

Scope of the membership check (fixture-review P1-2, both directions
normative): the gate judges membership ONLY for the drop-window-filtered
TARGETS (`derive_salvage_backed_windows(current, drop_window)`), never
for the current receipt's whole db-export universe. Binding the whole
universe would refuse whenever ANY db-export subject appears anywhere
(e.g. a 2024 backfill selector far outside the drop window) — the exact
daily-outage direction D1 rejects. A dedicated test row pins a disjoint
new subject passing, and a mutation row kills the whole-universe variant.

Why window-granular (not subject identity): `derive_salvage_backed_windows`
itself dedups requirements to `{start, end}` pairs — the gate's requirement
granularity IS the window. Binding at the same granularity is coherent; a
new subject whose window is byte-identical to a verified one is
indistinguishable AT THIS GRANULARITY and remains layer-2's job (recorded
residual, §7.5).

## D3 — Gate rule and placement

New helper `_drill_snapshot_binds(receipt, targets) -> bool`:

- `salvage_derivation` key entirely ABSENT → `True` (no-derivation-section
  receipt: pre-#1206 AND explicit-manifest drills — behavior unchanged,
  same population and same neutral naming as #1207's D2).
- Section present (Mapping — non-Mapping shapes are already refused
  upstream by #1207's `_drill_derivation_window_contains`, which runs
  first) but `db_export_windows` key ABSENT → `True` (post-#1206
  pre-binding receipts; recorded residual in §7.5 — the guard is dormant
  for them exactly like #1207's guard is for section-less receipts).
- Key present but unusable (not a list; an entry not a Mapping; `start`/
  `end` missing or non-string) → `False`, refuse
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`. Unreachable through
  `load_drill_receipt` once the schema types the field (defence-in-depth
  at the pure-function seam, symmetric with #1207 D2/D3).
- Else: `True` iff every target's exact `(start, end)` pair is a member of
  the recorded set. Subset direction is deliberate: requirement SHRINK
  (subject removed at gate time) passes; only unseen ADDITIONS refuse.

Placement: inside the db-export leg of `check_drill_gate`, AFTER the
existing empty-derivation refusal (`not targets` →
`DRILL_COVERAGE_DB_EXPORT_MISSING`, #1162 D2 — unchanged first) and BEFORE
the per-target coverage loop. Rationale: binding is a property of the
requirement set vs the drill's snapshot and must be judged before any
tuple-union evidence is consulted for those targets — otherwise the exact
substitution this change kills would decide first. Not whole-gate: with an
empty requirement set there is nothing to bind, and the forcing/runs legs
have whole-window semantics with no snapshot-derived requirement.

Gate order becomes: STALE → FAIL → derivation-window (#1207) → forcing →
runs → db-export[empty-derivation → SNAPSHOT_UNBOUND → per-target
coverage]. §8.2 priority chain inserts the new code between
`DRILL_COVERAGE_RUNS_MISSING` and `DRILL_COVERAGE_DB_EXPORT_MISSING`, with
the note that the empty-derivation case still surfaces
`DRILL_COVERAGE_DB_EXPORT_MISSING` first (it precedes the binding check).

## D4 — Schema and emit

`schemas/archive_rebuild_drill_receipt.schema.json` `salvage_derivation`
block gains two OPTIONAL properties (`required` list unchanged):
`db_export_windows` (array of `#/definitions/window`) and
`completeness_generated_at` (string, `format: date-time`). Optional is
load-bearing for compatibility: making them required would flip
post-#1206 pre-binding receipts from "pass-through" to a
`DRILL_RECEIPT_MISSING` load failure — a behavior change the issue
explicitly forbids for legacy receipts.

Emit side (`scripts/node27_archive_rebuild_drill.py`): the completeness
receipt object is alive only inside `_derivation_from_config` (~:2551);
`_salvage_provenance_fields` (~:2092-2107) assembles the receipt mapping
from the frozen `SalvageDerivation` dataclass (~:262-271, single
construction site ~:461-478). Implementation therefore extends that
dataclass with the two new fields, computes the D2 universe (unfiltered,
same normalization) from the loaded receipt at the construction site, and
threads them through. Explicit-manifest drills continue to omit the whole
section (unchanged). The drill-side derivation filter (its own candidate
selection, incl. lane filtering) is NOT reused for this field — record
every `coverage == "db-export"` / `verdict == "complete"` subject window,
no lane condition (matching the gate predicate exactly). No `maxItems`
bound: the set is deduped subject windows, bounded in practice by the
audit's subject count; the asymmetry with `MAX_DERIVED_SALVAGE_MANIFESTS`
is accepted (receipt-size only, no correctness impact).

Rollback note (risk pack): rolling back the SCHEMA alone while new-format
receipts exist on disk flips them to `DRILL_RECEIPT_MISSING` at load
(`additionalProperties: false`) — fail-closed, no data loss; remedy is
rerunning the drill or rolling back together.

## D5 — Residual after this change (recorded, not fixed here)

- (a) Receipts without `salvage_derivation` (explicit-manifest drills,
  pre-#1206) and receipts with the section but without
  `db_export_windows` (post-#1206 pre-binding): guard skipped entirely.
  Same dormancy class as #1207's D5-(a); tightening waits for the receipt
  population to migrate (#1177 owns live adoption of derivation mode).
- (b) Window-granularity blind spot: a NEW subject whose window is
  byte-identical to a window the drill verified passes the binding check —
  at the gate's own requirement granularity the two are indistinguishable.
  Layer-2 per-subject attribution is the root fix (already separately
  scheduled per #1207/#1220 meta).
- (c) `fix-retention-drill-window-guard` D5-(b) (this issue) is CLOSED at
  window granularity by this change; its §7.5 residual paragraph is
  updated accordingly — no "fully closed" phrasing, (b) above remains.
- (d) Dependency on the #1207 containment guard (D2): with an unfiltered
  recorded universe, "recorded ⇒ was in the drill's candidate set" holds
  only because the containment guard forces retention-drop ⊆ drill-drop
  BEFORE the binding check. Weakening or reordering #1207's guard silently
  degrades this one; the precedence-pin row (narrowed drill + drift →
  `DRILL_DERIVATION_WINDOW_TOO_NARROW` first) is the tripwire.

## Alternatives rejected

- Digest / `generated_at`-newer refusal: D1 arithmetic — daily outage.
- Layer-2 now: schema break + all existing receipts' db-export evidence
  degrades to undecidable → live retention outage until a new drill runs
  (issue records the same trade-off).
- Recording the drill's FILTERED derivation set instead of the full
  universe: functionally equivalent for every case that survives #1207's
  containment guard (targets ⊆ drill-window-overlapping subjects = the
  filtered set), but requires a second filter implementation on the emit
  side and couples the record to the drill's candidate-selection details —
  rejected for simplicity, not safety (D2 states the honest trade-off and
  D5-(d) the shared dependency).
