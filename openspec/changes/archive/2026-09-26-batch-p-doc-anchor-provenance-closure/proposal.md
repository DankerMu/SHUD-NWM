## Triage

```text
Issue type: documentation accuracy x4 (#2085, #2423, #2263 part 1, #2365) + test pin (#2263 part 2) + openspec ledger
  repair (#2326) + a K3 receipt completion requested by the batch owner
Fixture level: standard
Blast radius: docs, openspec archive/spec ledger, docstrings/comments only, plus two new unit tests; no runtime
  behaviour change
Selected risk packs: Documentation (anchor truth, SHA pins, no history rewrite), Legacy compatibility (archived
  evidence and fact-baseline tables stay byte-identical), Spec ledger (archive must not import unimplemented
  clauses), Provenance (read-only live evidence, no fabrication)
Evidence floor: tasks.md Evidence Floor; local (ruff, pytest for the new tests, openspec validate); #2365 evidence is
  read-only live data already captured
```

## Why

This is batch P, the last of the 10-batch serial run, on master after batch R (#2646/#2647). It closes these issues:

- **#2085:** `services/tiles/mvt.py:N` bare line anchors in living docs have drifted. The `active_flag` blast-radius census in `docs/spec/03_database_design.md` and its English mirror in `openspec/glossary.md` point at blank lines or unrelated code. The glossary is also missing the `_national_discharge_coverage_rows` entry.
- **#2423:** PR #2406 had already re-resolved most of the coordinates by symbol before it merged at `2ee1f53ed`: the archived `design.md` section "坐标漂移：本 PR 自己造成的，已逐符号重解". Read back at `2ee1f53ed`, 12 of the 13 coordinates are correct. Only `_event_matches_candidate_rows` (`:13434-13443`) is still stale; it should be def `:13706`, `forecast_cycle` branch `:13719-13729`. The closing line "检测工具与后续清理见 #2423" still points at this issue.
- **#2263:**
  - A shipped docstring in `services/tile_publisher/publisher.py` (`_copyback_batch_mutex`) carries a hard `` `:207-210` `` reference.
  - Two references measured stale: `scheduler_discovery.py` → `scheduler_generation_gate.py:349-376` (`COLD_DECLARED_CUTOVER`), and `scheduler_state_failure.py` → `retry.py:183` (the three-code set).
  - The `_resolve_runs_only_roots` docstring and body comment claim "non-db-free ⇒ unset" with no test pinning it. The batch owner asked for **two assertion tests** on `_resolve_runs_only_roots`.
- **#2365:** the #1816 republish replaced 16 model ids across 8 basins. The old→new map and the deferred first-pass `warm_continue` receipt exist only in an issue comment.
- **#2326:** `m11-popup-station-overlay-usability` cannot be archived. Its MODIFIED headers have drifted from canonical, it asserts a `/flood-alerts` route that does not exist, and `validate --strict` passes falsely. Its dependency `retire-basin-detail-lane` was archived on 2026-09-14.
- **Batch owner request:** paste the 20-FK inventory rows and the `pg_constraint` query into the K3 receipt at `openspec/changes/archive/2026-09-24-registry-lock-order-and-evidence-boundary/evidence/node27-live-receipt.md`. The receipt currently points only at gitignored `.workplans/k3/k3-catalog.out`.

## What Changes

- **#2085:**
  - Re-derive every `mvt.py` anchor in the in-scope living docs from a fresh census, never by offset. Cite code by symbol, with an optional "(currently `:N`)" helper.
  - Keep the two census copies in sync, and add the missing glossary entry.
  - Pin ADR 0002's historical measurement to its SHA.
  - Mark `feat-reach-geom-oq-findings.md` as a historical survey. Its symbols resolve at no SHA in this repository, so its deep links are neutralised.
  - Deviation from AC6: the `:1773` / `:2004` / `:2044` anchors that AC6 wanted frozen have drifted since then. They are re-derived by symbol like the rest.
- **#2423:** in the archived `design.md`, fix the one stale coordinate (`_event_matches_candidate_rows`) by symbol at `2ee1f53ed`, written with that SHA as prefix, and turn the "见 #2423" line into a closure note. The rest of the section is unchanged: the fact-baseline table and the existing re-point narrative. The PR includes a read-back of all 13 coordinates. The optional coordinate-check script is **not** built (YAGNI; recorded).
- **#2263:**
  - The three code references become symbol references:
    - the `publish_qdown_cycle` except-arm;
    - `scheduler_generation_gate.strict_warm_start_evidence`'s `COLD_NEW_MODEL` / `COLD_DECLARED_CUTOVER` branches;
    - `retry.failure_classifier`'s `"policy_blocked"` arm.
  - Two tests pin `_resolve_runs_only_roots`:
    1. `None`, empty and whitespace-only values are discarded **silently**: no resolved root and no skip entry.
    2. A relative value is discarded **loudly** (`EXTRA_ROOT_NOT_ABSOLUTE_REASON` skip entry) while an absolute value is admitted, so the silence is proven to depend on the value's shape.
  - The two topology sentences are reworded. They state what the code guarantees (a shape-keyed silent discard, pinned by those tests) and mark "unset on non-db-free deployments" as operational context the function cannot see.
  - The repository-wide line-reference gate is **not** adopted. The census numbers are recorded, and the decision is stated in the PR.
- **#2365:**
  - New receipt `docs/runbooks/receipts/2026-08-24-issue-1816-republish-identity.md`. It records the 16-row old→new map (basin, source, old/new `model_id`, old/new variant dir, package checksums) from the two manifest backups, with check commands.
  - It records the first-admitted-run transition evidence per new id: the run's `init_state_id` equals that id's `state_compatibility` clone row in `index-last.json`, and `state_clone.py` writes the target package identity into that row, so it is a same-generation predecessor, i.e. `warm_continue`.
  - It records the exceptions as observations, not causes: hetianhe's two #1816 ids have no `hydro_run` rows, and the backup `pre-hhe-subs-20260825` already carries different ids; shj_2shj's ids later left the manifest.
  - Runbook §5.7.1 gets a pointer line. The section body lives at `docs/runbooks/production-ops/recalibration-and-archive.md`.
- **#2326:** rebase the four delta specs onto today's canonical:
  - each MODIFIED body starts from today's canonical text verbatim, including its basin-detail *retirement* text;
  - it adds only the m11-popup clauses that code backs;
  - it re-introduces no basin-detail capability;
  - `/flood-alerts` and `flood-return-period` are removed.

  Then archive the change.
- **K3:** paste the query and its 20 rows into the K3 receipt as captured.

## Capabilities

### Modified Capabilities

- `doc-status-alignment`: living documents cite code by symbol, and historical measurements are SHA-pinned.
- `production-scheduler-orchestration`: unset or blank additional retention roots are discarded silently by shape, and relative ones are recorded.
- `model-asset-version-lineage`: a model-id replacement by republish is recorded in a living receipt with its first-pass transition evidence.
- The specs touched by archiving #2326 (`map-feature-popups`, `met-station-cluster-layer`, `frontend-navigation-state`, `single-map-shell-routing`) change through that change's own rebased delta.

## Impact

- **Docs:** `docs/spec/03_database_design.md`, `openspec/glossary.md`, `docs/plans/2026-09-03-display-v2-header-river-precip-timeline.md`, `docs/runbooks/production-ops/operating-scope.md` (where the old `current-production-ops.md` anchors now live), `docs/runbooks/production-ops/recalibration-and-archive.md` (§5.7.1 pointer), `docs/runbooks/feat-reach-geom-oq-findings.md`, `docs/adr/0002-*.md`, the new `docs/runbooks/receipts/2026-08-24-issue-1816-republish-identity.md`, the archived #2406 `design.md`, and the K3 receipt.
- **Code comments only:** `services/tile_publisher/publisher.py`, `services/orchestrator/scheduler_discovery.py`, `services/orchestrator/scheduler_state_failure.py`, `services/orchestrator/retention.py`.
- **Tests:** the extended `tests/test_retention_extra_roots.py`.
- **OpenSpec:** `m11-popup-station-overlay-usability` is rebased and archived.
- **No behaviour change anywhere.**
