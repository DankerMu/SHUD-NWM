# Tasks: drill-receipt-derived-salvage

Issue: #1177

## 1. Derivation core

- [x] 1.1 Add completeness-receipt loading + subject selection
      (`coverage=="db-export" and verdict=="complete"`) to
      `scripts/node27_archive_rebuild_drill.py`, mapping each subject to
      `<archive_root>/db-export/<lane>/<identity>/manifest.json` exactly as
      `node27_db_export_salvage.py._paths_for_selector` (:617-627) does:
      `forcing` and `runs` lanes derive; `states` subjects are
      refused/skipped with evidence; identities pass a path-safety guard
      mirroring `_refuse_unsafe_identity` (:602-606) before any path join.
- [x] 1.2 Optional drop-window filter using the gate's closed-interval
      `_overlaps` convention (`node27_timeseries_retention.py:540-552`;
      boundary-touching/zero-length intersections stay in scope); the receipt
      records the drop window used for derivation (or its absence).
- [x] 1.3 Fail-closed gaps: derived subject with missing/unreadable
      manifest.json → drill FAIL naming the paths; derived manifest whose
      `selector.window` differs from the receipt subject's window → drill
      FAIL (stale-manifest divergence); both recorded in receipt
      `differences`.
- [x] 1.4 New derived-set bound (cardinality + aggregate decompressed bytes,
      fail-closed) — the drill today has only per-object
      `MAX_SALVAGE_OBJECT_BYTES` (:136); nothing bounds set size.

## 2. CLI, wrapper, receipt provenance

- [x] 2.1 `--completeness-receipt` (+ drop-window flags) wired through
      CLI/config assembly (~:1918-1945), including extending the
      no-manifests invocation refusal (:1941-1944) so a receipt-only
      invocation is valid; `--salvage-manifest` unioned with the derived set,
      deduped by resolved path.
- [x] 2.2 Receipt records per-input provenance (`derived` vs `explicit`);
      `coverage` tuple shape unchanged for `check_drill_gate`; update
      `schemas/archive_rebuild_drill_receipt.schema.json` in lockstep (strict
      `additionalProperties: false`, validated at drill :1241) — admit the
      new fields precisely, do not loosen the schema wholesale.
- [x] 2.3 Wrapper: prefer env-var config through `_config_from_env` (sibling
      precedent `NHMS_ARCHIVE_COMPLETENESS_RECEIPT_PATH`) — the
      `node27_archive_rebuild_drill_once.sh` bare `exec` passthrough then
      needs no change; touch it only if flag plumbing is unavoidable.

## 3. Tests (requirement-driven)

- [x] 3.1 Demand coverage: for a receipt with N db-export+complete subjects,
      every `derive_salvage_backed_windows` demand window's clip to the drop
      window is covered by the union of the drill's db-export tuples —
      including duplicate-window subject pairs (e.g. two basins sharing one
      window → one demand window, two tuples) and a boundary-touching window
      (subject end == drop start) that MUST stay in scope.
- [x] 3.2 Fail-closed: missing manifest file → FAIL (never PASS on the
      narrower set); selector window ≠ subject window → FAIL; unreadable
      receipt → refusal; unsafe identity → refusal; `states` subject →
      explicit refusal/skip evidence, never silent drop.
- [x] 3.3 Drop-window filter excludes non-overlapping subjects
      (closed-interval semantics asserted); derived-set bound trips at
      228-scale input; `runs`-lane subject derives the correct path.
- [x] 3.4 Union + provenance: explicit and derived inputs deduped, provenance
      recorded, receipt validates against the updated schema; no-flag
      invocation behavior byte-identical to today.

## 4. Docs

- [x] 4.1 `docs/runbooks/tier-node27-timeseries-storage.md` invocation example
      (~:1424) and §7.5 (~:1493-1498) rewritten to the executable
      receipt-derived procedure, byte-consistent with the implemented CLI.

## 5. Verification

- [x] 5.1 `uv run pytest -q tests/test_node27_archive_rebuild_drill.py
      tests/test_node27_timeseries_retention.py` green.
- [x] 5.2 `uv run ruff check .` green;
      `openspec validate drill-receipt-derived-salvage --strict
      --no-interactive` green.

## Evidence Floor (issue acceptance criteria)

- Drill derives salvage input from the completeness receipt (or fails before
  PASS when coverage is short) with no manual manifest enumeration.
- Unit tests: demand-coverage (not bijection), missing-manifest and
  selector-divergence fail-closed, drop-window filter (closed-interval),
  derived-set bound at 228 scale, lane handling, path safety.
- Runbook updated to an executable procedure consistent with the code.
- Local: targeted pytest + ruff green.
- Node-27 live: drill PASS receipt under the new procedure + retention
  dry-run receipt free of `DRILL_COVERAGE_DB_EXPORT_MISSING`; posted to
  #1177 before closing the issue. Scheduling is opportunistic (any node-27
  ops window qualifies — the #1164 campaign's node-27 phase is a convenient
  one, not a precondition).
