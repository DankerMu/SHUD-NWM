# canonical-source-grid-registry — Change-Close Receipt (2026-07-08)

Full verification suite for openspec change `canonical-source-grid-registry`
(Epic #897, 11 sub-issues). This receipt discharges §7.1 evidence floor and
closes the change.

## Sub-issue landing summary

| Sub | Issue | PR | Merge SHA | Fixture |
| --- | --- | --- | --- | --- |
| SUB-1 | [#898](https://github.com/DankerMu/SHUD-NWM/issues/898) | #928 | `6799d156` | expanded |
| SUB-2 | [#899](https://github.com/DankerMu/SHUD-NWM/issues/899) | #931 | (in-tree; migration `000043` + `000044`) | expanded |
| SUB-3 | [#900](https://github.com/DankerMu/SHUD-NWM/issues/900) | #934 | (in-tree) | expanded |
| SUB-4 | [#901](https://github.com/DankerMu/SHUD-NWM/issues/901) | #935 | (in-tree) | expanded |
| SUB-5 | [#902](https://github.com/DankerMu/SHUD-NWM/issues/902) | #937 | `30e33d53` | expanded |
| SUB-6 | [#903](https://github.com/DankerMu/SHUD-NWM/issues/903) | #938 | `f6dde13f` | expanded |
| SUB-7 | [#904](https://github.com/DankerMu/SHUD-NWM/issues/904) | #940 | `b676c4f9` | expanded |
| SUB-8 | [#905](https://github.com/DankerMu/SHUD-NWM/issues/905) | #941 | `c25a26ad` | expanded |
| SUB-9 | [#906](https://github.com/DankerMu/SHUD-NWM/issues/906) | #942 | `9d0e0a30` | expanded |
| SUB-10 | [#907](https://github.com/DankerMu/SHUD-NWM/issues/907) | #943 | `a3f014bd` | compact |
| SUB-11 | [#908](https://github.com/DankerMu/SHUD-NWM/issues/908) | this PR | pending | compact |

## §7.1 evidence commands

### 1. Full pytest suite on node-27 (real-DB integration)

Executed on node-27 primary PG `postgresql://nhms@127.0.0.1:55432/nhms` with
`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=$DATABASE_URL` on
branch `feat/issue-908-epic-897-change-close` at HEAD `a6f43f72`:

```bash
uv run --active pytest -q \
    tests/test_grid_signature.py \
    tests/test_grid_registry_migration.py \
    tests/test_grid_registry_store.py \
    tests/test_grid_snapshot_registration.py \
    tests/test_grid_registry_bbox.py \
    tests/test_grid_stability_verification.py \
    tests/test_shared_binding_eligibility.py \
    tests/test_grid_drift_lifecycle.py \
    tests/test_forcing_producer.py \
    tests/test_direct_grid_e2e.py \
    tests/test_canonical_converter.py
# → 630 passed, 1 warning in 88.29s (0:01:28)
```

### 2. Local ruff

```bash
uv run ruff check .
# → All checks passed!
```

### 3. Local openspec validate

```bash
uv run openspec validate canonical-source-grid-registry --strict --no-interactive
# → Change 'canonical-source-grid-registry' is valid
```

### 4. Local openspec status

```bash
uv run openspec status --change canonical-source-grid-registry
# → Change: canonical-source-grid-registry
# → Schema: spec-driven
# → Progress: 4/4 artifacts complete
# → [x] proposal
# → [x] design
# → [x] specs
# → [x] tasks
# → All artifacts complete!
```

## Test-fix carrier landed in SUB-11

Node-27 real-DB integration surfaced ONE semantic gap in SUB-10's
`test_backfill_ifs_gfs_share_key` that fixture-only skip masked:
- SUB-8 `evaluate_shared_binding_eligibility` check-4 requires both rows to
  ALREADY list both source ids in `applicable_source_ids` (fail-closed audit
  with idempotent finalize-write, not a "grant" operation).
- Fix landed at commit `a6f43f72` on this branch: added explicit opt-in via
  `store.extend_applicable_source_ids` between SUB-5 registration and SUB-8
  eligibility. Matches production flow where operator (or CLI opt-in flag)
  pre-writes the canonical pair.

## Follow-up carrier (from SUB-10 Phase 7)

`openspec/changes/canonical-source-grid-registry/proposal.md:3` still
describes `ifs_0p25`/`gfs_0p25` shared signature as `6c008901b8b7…` (which
SUB-4 test docstring at `tests/test_grid_snapshot_registration.py:913-915`
declares synthetic). Actual live signature at bbox 63-145°E / 8-64°N × 0.25°
axis_order (lat,lon) is
`0507ab4d0db2a311b680f8ed1a51b957a5b5e66913833a505a32c566085f1b10`. A
follow-up openspec change should replace or explicitly annotate the
synthetic value in proposal.md. Change-close does NOT block on this drift
(receipt at `docs/receipts/canonical-source-grid-registry/backfill-2026-07-06.md`
documents both values and the discrepancy).

## Live baseline

SUB-10 node-27 live backfill (2026-07-08):
- IFS `grid_snapshot_id`: `350f62a9-b897-4929-a7fc-d8e696509b79`
- GFS `grid_snapshot_id`: `fbb5417c-72a0-44e7-b7a9-679818e05ef9`
- shared `canonical_grid_key`:
  `10a00f25fa26c56bcace76caeeee126e1dcf4f1fa6a840728688809a715e0abf`
- shared `grid_signature`:
  `0507ab4d0db2a311b680f8ed1a51b957a5b5e66913833a505a32c566085f1b10`
- `met.canonical_grid_cell` COUNT(*) = 148,050

## Change closure

All four openspec artifacts complete; strict validate passes; §7.1 evidence
recorded; every `tasks.md` checkbox ticked; SUB-1 through SUB-11 landed in
master. `canonical-source-grid-registry` change is ready for openspec
archive after SUB-11 PR merges.
