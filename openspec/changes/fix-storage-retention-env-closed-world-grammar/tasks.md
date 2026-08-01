# Tasks: fix-storage-retention-env-closed-world-grammar

Fixture level: compact · Repair intensity: normal · Issue #1230

Triage note: fail-direction hardening on the #1227 guard — converts a
verifier-CONFIRMED fail-open class (8 differentially-reproduced shell
shapes silently defaulting to 14 while the runner exports larger) into
refusals. The change is monotone (acceptance set strictly shrinks; no
refuse→accept flip), which caps the risk at FALSE REFUSALS — the risk
axes are (1) false-refusal against shipped templates and the DEPLOYED
retention env, (2) test-oracle fallout precision (which layer refuses
which row), (3) spec/runbook claim honesty (closed-world wording may
not overclaim past the recorded residuals). Compact fixture, 1-2
rounds.

Change surface:
- `packages/common/storage.py` — `_scan_env_assignment` grammar refusal
  (replace the `matched is None: continue`), `mentioned_line` in
  `_EnvAssignmentScan`, mention message + docstrings
- `tests/test_storage.py` — 8 new shape rows + 6 match-string updates +
  2 mention-layer rows + grammar-message offending-line assertions +
  xfail re-record + new D5(a2) strict-xfail tripwire +
  template-conformance test
- `docs/runbooks/tier-node27-timeseries-storage.md` residual paragraph
  (~:204-215)
- main spec `openspec/specs/timeseries-product-archive/spec.md`
  "Unreadable window source fails closed" scenario (delta MODIFIED)

Must preserve:
- Every input refused at HEAD still refuses; every input accepted after
  the change was accepted before with the same value (monotone
  shrink) — the differential oracle plus the unchanged locks
  (shipped retention example == 14, both archive templates refused,
  wrong-file/pointer rows) pin this
- The runner-equivalent-default rule, family recognition (pointer
  exclusion), value parsing (`_strip_env_trailing_comment` /
  `_unquote_env_value`) byte-identical
- Both call sites (`node27_product_archive.py:4356`,
  `node27_storage_inventory_audit.py:1073`) untouched

Must add (design D1/D2/D3):
- Closed-world line grammar with immediate first-offending-line refusal
  naming path + `{candidate!r}`
- Mention refusal message gains the offending line
- 8 shape rows, two mention-layer rows (value-embedding + KEY-suffix
  decoy), grammar-message offending-line assertions, xfail re-record
  (no XPASS residue) PLUS the new D5(a2) strict-xfail class tripwire,
  template-conformance test via the public helper (>= 15 files, zero
  grammar-class refusals)
- Runbook + spec closed-world rewording; residual list narrows to the
  multi-line-quote class with BOTH variants recorded — bare-closing
  -quote over-strict (fail-closed) AND all-conforming fail-open
  (D5(a1)/(a2)) — plus the value-expansion note (D5(b)). NEVER claim
  the fail-open class is fully closed.

## Implementation tasks

- [x] 1. `_scan_env_assignment`: grammar refusal + `mentioned_line`
  capture; caller mention message gains the line; docstrings rewritten
  (no "detectable substring" framing left in code).
- [x] 2. Tests per D3: 8 new rows; 6 existing rows' match updated to the
  grammar message (ids/bodies unchanged); TWO mention-layer rows
  (value-embedding + KEY-suffix decoy) asserting message + offending
  line; dedicated grammar-refusal test asserting `repr(offending_line)`
  in the message (>= 2 bodies); `_MULTILINE_QUOTED_BODY` moved into
  `_UNSUPPORTED_SHAPE_ROWS`, the old xfail append deleted, and the
  D5(a2) all-conforming body added as a NEW strict-xfail differential
  row; new template-conformance test via the public helper.
- [x] 3. Docs: runbook residual paragraph rewritten (8 enumerated
  shapes now refused; file-format constraint enforced by the guard;
  residual = multi-line-quote class in BOTH directions, incl. the
  still-fail-open all-conforming variant — quoted values MUST NOT span
  lines); spec delta applied. Archived #1229 artifacts
  (`openspec/changes/archive/2026-08-01-fix-archive-min-age-live-window/**`)
  are historical records and are NOT rewritten (fixture-review P3-2).
- [x] 4. Full-file oracle: `uv run pytest -q tests/test_storage.py
  tests/test_node27_product_archive.py
  tests/test_node27_storage_inventory_audit.py` green (differential
  oracle runs with real bash; the two consumer suites pin the call
  sites); `uv run ruff check .` clean; markdownlint on the runbook 0
  issues; issue's inline template-scan Verification script run once
  as-is.
- [x] 5. Mutation proof on a scratch copy:
  (i) revert grammar refusal to `continue` → the 8 new rows fail;
  (ii) grammar message drops `{candidate!r}` → the dedicated
  offending-line assertions fail;
  (iii) mention layer deleted outright → mention-layer rows fail
  (proves the second layer is still load-bearing);
  (iv) template test's grammar-fragment filter inverted/removed →
  a seeded non-conforming template body is no longer caught (oracle is
  real).
  Result (rsync scratch, main venv pytest): (i) 34 failed (8 shape
  rows in both the shape test and the bash differential oracle, plus
  re-pointed rows and offending-line tests); (ii) 4 failed; (iii) 4
  failed; (iv) seeded `readonly SEEDED_BAD=1` in the retention
  template → unmutated test FAILED (caught), filter mutated to
  `assert True` → passed (slipped) — filter is load-bearing.
  Unmutated baseline 210 passed + 1 xfailed at 71fdcadc; after the
  Phase-7 P2 fix (a36152f0, second strict-xfail tripwire added) the
  baseline is 210 passed + 2 xfailed — mutation counts unaffected.
- [x] 6. node-27 live receipt per design D4: new helper vs deployed
  retention env file → assert it returns the positive integer actually
  assigned in the deployed file (no hardcoded expectation) with no
  grammar refusal; record value + line count; mutation scope limited
  to the scratch worktree (no DB, no receipts, no locks, no env
  edits), worktree removed afterwards.
  Result (head 4f170485, scratch worktree `/home/nwm/pr1230-scratch`,
  cwd-based import of the PR code): deployed
  `infra/env/node27-timeseries-retention.env` (16 lines, 1 window
  assignment) → helper returned 21, equal to the deployed assignment;
  no grammar refusal. First attempt imported the MAIN worktree's older
  storage.py via cwd (`sys.path[0]`) and failed with ImportError —
  rerun from the scratch worktree cwd; worktree removed after.

## Required evidence

- pytest/ruff/markdownlint/openspec outputs (task 4)
- Mutation outputs (task 5)
- node-27 helper-vs-deployed-env value (task 6)
- `openspec validate fix-storage-retention-env-closed-world-grammar
  --strict --no-interactive` valid

## Non-goals

- #1227 min-age comparison semantics; retention runner parsing; real
  bash sourcing in the helper
- Unbalanced-quote detection (D5(a) residual stays recorded)
- Archive template pinned refusals (round-2 C1) — unchanged
- Any live env edit on node-27 (#1228 owns ops)
