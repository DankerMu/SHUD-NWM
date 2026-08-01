# Tasks: fix-retention-dbexport-refusal-localization

Fixture level: compact · Repair intensity: normal · Issue #1175

Triage note: payload-expressiveness change on an already-fail-closed
refusal path — no fail-direction risk (every input that refused before
refuses after, with the same code prefix; no input flips accept/refuse).
The risk axes are contract wire-format drift (three doc surfaces + one
registry convention) and test-oracle honesty (exact-string vs prefix
assertions). Compact fixture, 1-2 review rounds.

Change surface:
- scripts/node27_timeseries_retention.py — two emission points only
  (`:826-831` D2 branch, `:846-848` per-window/inverted-clip arm);
  `WIRE_CODES` and the code constant untouched
- tests/test_node27_timeseries_retention.py — 10 strict-equality sites
  (D3 list) + new rows (k-th window, D2 payload, inverted-interval,
  clip-bounds proof)
- docs/runbooks/tier-node27-timeseries-storage.md §8.2 entry + §7.5
  diagnostic guidance + §8.7 one-sentence clipped-vs-unclipped cross-note
- openspec/changes/tier-node27-timeseries-storage/design.md #855
  wire-format entry (~:1990)
- openspec spec delta: ADDED requirement, capability
  `timeseries-db-retention`

Must preserve:
- Gate judgment semantics byte-identical: same inputs refuse/pass, same
  code ORDER in the priority chain, single-shortfall early return
- Bare `DRILL_COVERAGE_DB_EXPORT_MISSING` remains a strict prefix of
  every emitted form; `WIRE_CODES` membership unchanged
- Receipt schema untouched; refused `oneOf` shape untouched
- H6 forward/reverse token-walk tests green without modification
- All other wire codes' emission byte-identical

Must add (per design D1):
- Per-window suffix `:<clipped_start>/<clipped_end>` via `_iso()`,
  first uncovered target in derivation order (incl. inverted clip,
  rendered verbatim)
- D2 branch suffix `:no-derivable-window`
- Docs on all three surfaces, same commit

## Implementation tasks

- [x] 1. Emission: compose the two suffixed forms at the two sites;
  no new constant; no other code paths touched.
- [x] 2. Tests:
  (a) update all 10 D3 equality sites to exact suffixed strings — NOTE
  `:1183` and `:2129` are D2-shape sites expecting
  `:no-derivable-window` (update in place; no duplicate D2 row);
  (b) k-th-window row: N ≥ 3 targets, only window k (1 < k < N)
  uncovered → suffix is exactly that clipped window (existing 2-window
  row's gap is in the LAST window and kills no last-window mutant);
  (c) inverted-clip row asserts the rendered inverted interval;
  (d) clip-bounds row: subject overrunning the drop window on both sides
  → suffix carries clipped bounds, not raw subject bounds;
  (e) H6 token-walk suites re-run green unmodified.
- [x] 3. Docs: runbook §8.2 entry gains both payload forms; §7.5 gains
  diagnostic guidance (suffix localizes the shortfall window; remedy
  STAYS the receipt-driven drill re-run — the pre-#1177 full-manifest
  wording is forbidden and must not be reintroduced); §8.7 gains the
  clipped-vs-unclipped cross-note (suffix will not string-match receipt
  entries); tier design.md #855 entry updated — all consistent with
  code. `archive-rebuild-drill` spec `:18,:23` and runbook `:2084-2090`
  intentionally untouched (bare code stays a strict prefix).
- [x] 4. D5(b) consumer audit at HEAD: grep for runtime equality
  dispatch on the bare code outside tests; record result.
  Result (grep over `scripts/ packages/ workers/ services/ apps/ infra/
  schemas/`, plus a repo-wide sweep excluding `tests/ docs/ openspec/`):
  the token appears ONLY in `scripts/node27_timeseries_retention.py` —
  constant definition `:131`, `WIRE_CODES` membership `:151`, and the two
  emission points `:833` / `:857`. No runtime consumer compares
  `refusal_reason` against this code by equality (the other
  `refusal_reason` hits — `scripts/node27_storage_inventory_audit.py:1159`,
  `scripts/audit_first_cycle_initial_state.py:750`,
  `scripts/scheduler_file_provider_refresh.py:2696+` — are unrelated
  emitters of their own receipt fields). Prefix-only readers stay correct.
- [x] 5. Mutation proof on a scratch copy:
  (i) suffix dropped (bare code emitted) → exact-string rows fail;
  (ii) suffix renders RAW subject bounds instead of clipped → clip-bounds
  row fails;
  (iii) loop emits LAST uncovered window instead of first → k-th-window
  row fails (k chosen != last);
  (iv) D2 branch emits bare code → D2 row fails.
  Result (rsync scratch copy, main venv pytest): (i) 12 failed;
  (ii) 4 failed; (iii) 3 failed — extra probe emitting the LAST
  uncovered window killed 2 more rows (the clipped-window-end/start
  pair, each with ≥2 uncovered windows), so the oracle genuinely
  distinguishes first-vs-last; (iv) 2 failed. Unmutated baseline green.
- [x] 6. node-27 live receipt per design D4 (hardened): explicit
  minimal env — NEVER source the deployed retention env —
  `NODE27_TIMESERIES_RETENTION_ENFORCE=0`, scratch receipt AND lock
  paths, READ-ONLY display DSN; eligible-chunk precondition checked
  read-only first; fabricated drill receipt WITHOUT `salvage_derivation`
  (so #1207/#1220 legs pass and the per-window db-export shortfall is
  the refusal actually reached); record verbatim suffixed
  `refusal_reason` + production receipt/lock mtimes unchanged.
  Result (2026-08-01T21:11:39Z, head 99fedbc6, scratch worktree
  `/home/nwm/pr1175-scratch`, `env -i` minimal env, ENFORCE=0, scratch
  receipt+lock; DEVIATION: no live RO `nhms_display_ro` DSN with
  credentials exists on node-27 — ran with the deployed RW DSN, so the
  RO second line was absent and ENFORCE=0 + refusal-before-drop was the
  only guard, which held): verbatim `refusal_reason` =
  `DRILL_COVERAGE_DB_EXPORT_MISSING:2026-05-28T00:00:00Z/2026-06-25T00:00:00Z`
  — fabricated subject was 2019..2031, so the suffix demonstrably
  renders the CLIPPED live drop window, not raw subject bounds.
  Production receipt mtimes byte-identical before/after; lock went to
  the scratch path (mtime match); scratch worktree removed. Full log:
  `.workplans/pr-1175/review/node27-evidence.md`.

## Required evidence

- `uv run pytest -q tests/test_node27_timeseries_retention.py
  tests/test_timeseries_storage_schemas.py` green
- `uv run ruff check .`; markdownlint on the runbook 0 issues
- `openspec validate fix-retention-dbexport-refusal-localization --strict
  --no-interactive` valid; `tier-node27-timeseries-storage` still valid
- Mutation outputs (i)-(iv)
- node-27 receipt path + verbatim `refusal_reason`

## Non-goals

- #1162 judgment semantics; forcing/runs legs; helper function bodies
- Refused-receipt schema shape; multi-shortfall aggregation
- Retention timer enablement / any live env edit (#1228 owns ops)
