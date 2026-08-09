# Design: fix-retention-dbexport-refusal-localization (#1175)

## D1 — Suffix format and emission points

Two emission points, three refusal shapes, one code:

| Shape | Site | Payload |
|---|---|---|
| per-window coverage shortfall | `scripts/node27_timeseries_retention.py:846-848` | `DRILL_COVERAGE_DB_EXPORT_MISSING:<clipped_start>/<clipped_end>` |
| inverted clip (corrupt subject, #1162 guard) | same arm (`clipped.end < clipped.start`) | same format — the rendered interval is visibly inverted, which IS the diagnosis |
| D2 empty derivation (overlap but nothing derivable) | `:826-831` | `DRILL_COVERAGE_DB_EXPORT_MISSING:no-derivable-window` |

- Serialization: the module's existing `_iso()` (`:453-455`,
  `astimezone(UTC).isoformat().replace("+00:00","Z")`). NOT
  byte-identical with receipt window entries, on two axes the docs must
  state (fixture-review P2-5): the suffix carries the CLIPPED
  intersection while an enforced receipt's `salvage_backed_windows[]`
  echoes UNCLIPPED subject strings verbatim (runbook §8.7 `:2358-2364`
  pins that), and `_iso()` has no `timespec` while the completeness
  emitter uses `isoformat(timespec="seconds")` — grepping the suffix
  against receipt entries is NOT expected to match; §8.7 gains one
  cross-note sentence (fourth doc surface). Separator `/` (ISO-8601
  interval convention; cannot appear inside `_iso` output).
- "First uncovered target in ascending window order":
  `derive_salvage_backed_windows` sorts ascending by
  `(start, end)` STRING order and dedups (`:969`) — deterministic;
  single-shortfall early return preserved (multi-shortfall aggregation
  is a non-goal).
- `no-derivable-window` is lowercase-hyphen so it can never collide with
  an ISO interval, and the bare code remains a strict prefix of every
  emitted form (startswith-consumers keep working).
- REJECTED: putting the window into a new receipt field — the refused
  `oneOf` branch deliberately carries `refusal_reason` only; loosening it
  costs schema + `build_receipt` invariants + shape tests and diverges
  from the established suffix precedent.

## D2 — Registry and cross-surface consistency

- `WIRE_CODES` (`:137`) stays a bare-token registry;
  `CODE_DRILL_COVERAGE_DB_EXPORT_MISSING` constant unchanged. Emission
  composes `f"{CODE}:{detail}"` exactly like `RETENTION_DROP_FAILED`
  (composition site `:1544`; `:1272` is `build_receipt`'s definition
  area) — no new constant.
- Four-surface registration convention (main spec's existing wire-code
  requirements): code registry unchanged, so the obligation reduces to
  keeping runbook §8.2 (`docs/runbooks/tier-node27-timeseries-storage.md`
  ~`:2027` entry), runbook §7.5 diagnostic guidance, the pending
  `tier-node27-timeseries-storage` `design.md` #855 block entry
  (~`:1990`), and one §8.7 clipped-vs-unclipped cross-note consistent
  with the new payload forms, same commit.
- §7.5 framing (fixture-review P2-4): the issue's "blunt remedy"
  premise is STALE at HEAD — #1177/#1206 already rewrote §7.5 to forbid
  hand-narrowing and mandate `--completeness-receipt`-driven drills
  (`:1733-1735`, `:1794-1796`). The suffix's value is DIAGNOSTIC
  LOCALIZATION (which window fell short — verify the drill actually
  covered it, spot corrupt/inverted subjects, correlate with §8.7
  receipts), NOT a replacement remedy; the remedy text stays the
  receipt-driven drill re-run. Proposal Why is worded accordingly; no
  "full-manifest fallback" sentence may be (re)introduced.
- Intentionally untouched sibling surfaces (fixture-review P3-8,
  prefix-compatible, declared here so review does not re-litigate):
  `openspec/specs/archive-rebuild-drill/spec.md:18,23` ("refuses with
  `DRILL_COVERAGE_DB_EXPORT_MISSING`" — bare code remains a strict
  prefix, statements stay true) and runbook §8.2 ordering-subtlety
  paragraph `:2084-2090` (narrates the D2 branch by bare code; still
  true as prefix).
- H6 forward/reverse token-walk tests
  (`tests/test_node27_timeseries_retention.py:355-361,397-413`): regex
  breaks at `:`, `RETENTION_DROP_FAILED:<...>` already coexists green —
  re-verify, do not assume.

## D3 — Test fallout (sized at HEAD; issue's line refs are stale)

Strict-equality sites for this code at HEAD (grep-verified):

- receipt-level `refusal_reason ==`: `tests/test_node27_timeseries_retention.py:938,1073,1103`
- reasons-list `==`: `:1183,1295,1332,1359,1422,1460,2129`

Every site updates to the EXACT expected suffixed string (each test
controls its windows; exact assertion kills format drift and a
wrong-window mutant — `startswith` would not). Two of the ten are
D2-SHAPE sites (fixture-review P3-7): `:1183`
(`test_drill_db_export_empty_salvage_derivation_refuses_fail_closed`)
and `:2129` (`test_snapshot_empty_derivation_still_refuses_db_export_missing_first`)
expect `:no-derivable-window`, not an interval — UPDATE them in place;
do not add a duplicate D2 row. New rows:

- (k-th window) N ≥ 3 targets where ONLY the k-th (k ≠ 1, k ≠ last)
  lacks db-export coverage → suffix renders exactly that clipped k-th
  window (the existing 2-window row `:1076-1103` has its gap in the
  LAST window and cannot kill a last-window mutant);
- (inverted clip) existing inverted-subject row asserts the rendered
  inverted interval;
- (clip proof) a target overrunning the drop window on both sides asserts
  the suffix carries the CLIPPED bounds, not the raw subject bounds.

## D4 — Live evidence (node-27, hardened per fixture review P1/P2-2/P2-3)

The refusal CANNOT be produced offline: `DATABASE_URL` is unconditionally
required (`:309-311`) and `fetch_display_watermark()`/`fetch_chunks()`
run BEFORE the drill gate (`:1629-1634` → `:1473` → `:1505`); with no
eligible chunk `drop_window is None` and the db-export leg is skipped
entirely (`:812-813`). Therefore:

- **Enforce hard-off (P1)**: NEVER source the deployed retention env
  wholesale — it may carry `NODE27_TIMESERIES_RETENTION_ENFORCE=1`
  (node-27 has genuinely run enforce; first-enforce receipt exists), and
  `--dry-run` is an argparse placeholder the config never reads
  (`:353-359`). The evidence run sets an explicit minimal environment
  with `NODE27_TIMESERIES_RETENTION_ENFORCE=0`, scratch receipt AND lock
  paths, and the READ-ONLY display DSN (`nhms_display_ro` — the refusal
  path only SELECTs). A fabricated pair that accidentally PASSES the
  gate must still be unable to drop: enforce=0 is the hard line, the RO
  role is the second line.
- **Precondition**: node-27 must currently have at least one
  \>window-days eligible chunk (else the leg is unreachable); check
  read-only first, and record the check.
- **Fabrication recipe (P2-3)**: the fabricated completeness receipt
  carries one db-export/complete subject overlapping the drop window;
  the fabricated drill receipt is PASS, fresh, WITHOUT a
  `salvage_derivation` section (both #1207 and #1220 legs then pass
  pre-#1206-style: `:668-669`, `:736-737`), with forcing+runs tuples
  covering the drop window and NO db-export tuple for the target — so
  the first refusal reached is exactly the per-window db-export
  shortfall, not `DRILL_DERIVATION_WINDOW_TOO_NARROW` or
  `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND`.
- Record: receipt path + verbatim suffixed `refusal_reason` + production
  receipt/lock mtimes unchanged.

## D5 — Residuals

- (a) Only the FIRST shortfall window is surfaced per tick; subsequent
  shortfalls appear on later ticks after the first is remedied. Accepted:
  single-shortfall early return is a frozen structure (issue scope).
- (b) Consumers parsing `refusal_reason` by exact equality against the
  bare token would break — grep shows no such consumer outside the tests
  updated here (docs describe, tests assert, nothing dispatches on
  equality of this code at runtime). Re-verify at implementation.
