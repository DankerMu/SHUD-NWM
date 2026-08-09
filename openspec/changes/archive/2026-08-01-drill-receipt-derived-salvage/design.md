# Design: drill-receipt-derived-salvage

## Risk triage

- Level: standard (M). Ops-tooling change on node-27 scripts; no DB schema,
  no orchestrator, no frontend. The failure mode being fixed is operational
  (expensive trial-and-error), and the new failure mode to guard against is
  a derivation bug that silently narrows the evidence set — hence fail-closed
  everywhere and parity tests against `derive_salvage_backed_windows`.
- Selected risk packs: data-integrity (receipt-driven derivation must be
  fail-closed and byte-consistent with the gate's demand derivation),
  ops-docs consistency (runbook must match code behavior exactly).
- Not selected: db-migration (no schema change), frontend/display (none),
  scheduler/orchestrator (drill is a standalone node-27 script), performance
  (drill cost dominated by restore, not derivation) — with reasons as stated.

## Must-preserve behavior

- Explicit `--salvage-manifest` invocations keep working unchanged (no flag →
  no behavior change; the new path is opt-in via `--completeness-receipt`).
- Gate-side functions (`derive_salvage_backed_windows`, `_drill_covers`,
  `_clip_to_drop` in `scripts/node27_timeseries_retention.py`) are read-only
  precedent, never edited.
- Salvage verification semantics per manifest (sha256 + decompressed row
  count vs manifest) unchanged; only the *set* of manifests fed in changes.
- Per-object `MAX_SALVAGE_OBJECT_BYTES` (drill :136) still binds; note the
  drill has NO set-size bound today — a new cardinality + aggregate-bytes
  bound on the derived set is part of this change, fail-closed.
- Receipt `coverage` tuple shape read by `check_drill_gate` is unchanged; the
  provenance field is a schema change too —
  `schemas/archive_rebuild_drill_receipt.schema.json` is strict
  (`additionalProperties: false`) and validated before write (drill :1241),
  so the schema is updated in lockstep, never loosened wholesale.

## Seams under test (consumed from issue, not renegotiated)

- Receipt subject → manifest path mapping:
  `<archive_root>/db-export/<lane>/<identity>/manifest.json`, exactly as
  `node27_db_export_salvage.py._paths_for_selector` builds it (:617-627):
  `lane` from `_TABLE_TO_LANE` (`forcing`|`runs`), `identity` from the
  subject's lane-specific identity key, guarded by `_refuse_unsafe_identity`
  (:602-606, `_SAFE_IDENTITY_RE` :89) before any path join. The
  receipt-as-sole-source refusal precedent is :209-214/:586-589.
- Demand-set coverage (not bijection — the gate dedups demand windows across
  subjects at `node27_timeseries_retention.py:803-822` while the drill emits
  one tuple per manifest export entry): for every demand window from
  `derive_salvage_backed_windows` for the same receipt + drop window, its
  clip to the drop window must be covered by the union of the drill's
  db-export tuples. A unit test pins this property.
- CLI/config assembly seam (`:1918-1945`) and salvage traversal seam
  (`:1480-1500`) as the only touch points in the drill script.

## Decisions

1. **Primary approach (derivation), not verify-only.** The issue's
   alternative (post-hoc coverage check that flips PASS→FAIL) still requires
   the operator to guess the manifest list and re-run the expensive drill;
   derivation removes the guessing entirely. The coverage property holds by
   construction (derived subjects ⊇ demand subjects for the same receipt and
   drop window) ONLY if the drill uses the gate's own overlap predicate —
   see decision 3.
2. **Union semantics for explicit + derived inputs**, deduped by resolved
   path, provenance recorded per input in the receipt. Rationale: strictly
   additive, keeps old invocations valid, and lets operators pin extra
   manifests during incident response without disabling derivation. The
   invocation gate is extended so `--completeness-receipt` +
   `--archive-manifest` without `--salvage-manifest` is valid; a
   receipt-ONLY invocation (no archive manifest) is refused AT CONFIG TIME
   with a message naming `--archive-manifest` — the pre-existing PASS rule
   requires ≥1 restored product cycle (out of scope to change), so accepting
   the shape and failing after the expensive salvage phase with no receipt
   would be a trap (verified finding, round 1).
2b. **Empty derivation is evidence, not an error.** A derivation that yields
   zero manifests (receipt has no db-export+complete subjects, or none
   overlap the drop window) mirrors the gate: `check_drill_gate` demands
   db-export coverage only when an overlapping db-export subject exists
   (`_completeness_has_db_export_overlap`), so the drill proceeds with the
   archive-manifest leg and records `derived_count: 0` + the drop window in
   `salvage_derivation` — never a config refusal (verified finding, round 1:
   the earlier refusal was strictly broader than gate demand and killed the
   standard runbook invocation on healthy archives). Self-contradictory
   receipts (duplicate identity with conflicting windows) still refuse.
3. **Drop-window filter uses the gate's closed-interval `_overlaps`**
   (`node27_timeseries_retention.py:540-552`): boundary-touching and
   zero-length intersections stay in scope, exactly as `check_drill_gate`
   keeps them (:693-701). A half-open convention here would re-create the
   original bug at window boundaries. The filter is optional but recommended;
   the receipt records the drop window used (or its absence) so a
   narrower-than-retention derivation is detectable.
4. **Fail-closed on gaps between receipt and disk**: derived subject with an
   absent/unreadable manifest → FAIL naming the paths; derived manifest whose
   `selector.window` ≠ the receipt subject's window → FAIL (stale-manifest
   divergence — the drill's tuple window comes from the manifest, so silent
   divergence would emit a tuple for the wrong window and still PASS). Lane
   handling is explicit: `forcing` and `runs` both derive; `states` subjects
   (no db-export lane) are refused/skipped with evidence.
5. **New derived-set bound** (cardinality + aggregate decompressed bytes,
   fail-closed) since only per-object `MAX_SALVAGE_OBJECT_BYTES` exists.
6. **Receipt-path env var, zero wrapper change, drill-scoped name**: the
   wrapper is a bare `exec` passthrough and config flows via
   `_config_from_env`; the env var is
   `NHMS_ARCHIVE_REBUILD_DRILL_COMPLETENESS_RECEIPT_PATH` — NOT the
   sibling's `NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH`, which the salvage
   tool's env file ships uncommented-and-mandatory: a leaked export from a
   `set -a`-sourced salvage env would silently switch flag-less drill
   invocations into derivation mode, violating the "no flag → no behavior
   change" must-preserve (verified finding, round 1).
7. **Drill drop window must contain the retention drop window.** The gate's
   union-coverage check never reads `salvage_derivation.drop_window`, so a
   narrower drill window can let one subject's wide tuple mask another
   subject's never-verified evidence. In-PR remedy is the runbook constraint
   (window ⊇ retention window, with the masking warning, replacing the
   "narrowing is legitimate" wording); machine enforcement in
   `check_drill_gate` is a #1162 gate-semantics extension routed to its own
   issue (gate functions are out of scope here).

## Evidence mapping

- Unit tests (`tests/test_node27_archive_rebuild_drill.py`): demand coverage
  (every `derive_salvage_backed_windows` window's drop-clip covered by the
  drill tuple union, incl. duplicate-window subjects and boundary-touching
  windows), missing-manifest fail-closed, selector-window-divergence
  fail-closed, `runs`-lane derivation + `states` refusal, unsafe-identity
  refusal, drop-window filtering excludes non-overlapping subjects
  (closed-interval), derived-set bound at 228-scale input, explicit+derived
  union provenance, receipt schema round-trip with the provenance field.
- `uv run pytest -q tests/test_node27_archive_rebuild_drill.py
  tests/test_node27_timeseries_retention.py` and `uv run ruff check .` local.
- Node-27 live (post-merge, Evidence Floor): one drill run with
  `--completeness-receipt`, PASS receipt path + subsequent retention dry-run
  receipt with no `DRILL_COVERAGE_DB_EXPORT_MISSING`.
