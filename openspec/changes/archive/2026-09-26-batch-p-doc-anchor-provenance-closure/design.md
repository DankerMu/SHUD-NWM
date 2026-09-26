## Context

This batch has six documentation-accuracy and ledger items. All of them share one invariant: **a citation must resolve to the thing it claims, and history must not be rewritten.**

## Decisions

### D1 — Re-derive, never offset (#2085, #2423, #2263 part 1)

For every coordinate:
1. Name the symbol it claims, from the surrounding prose.
2. Find it with `grep -n 'def <symbol>'` or the literal.
3. Read the line back with `sed -n '<N>p'`.
4. Rewrite the coordinate.

Arithmetic such as "old line + diff delta" is forbidden, because #2423's two false resolutions came from exactly that. Current-truth docs cite **symbols**. A line number may follow as a helper, as in `national_river_network_source_version()` (currently `:1922`). Historical measurements are **SHA-pinned**, as in `mvt.py@<sha>:603-651`, and are never moved to current lines.

### D2 — #2085 scope rule

The issue's AC asks for every `grep -rn 'mvt\.py' docs/ openspec/` hit to be checked. This fixture partitions the hits so that nothing is silently skipped:

- **Fixed (living, current-truth):**
  - the issue-listed docs at their current locations:
    - `docs/spec/03_database_design.md`, including the census entries now at about `:249-255` and the `:1773` / `:2004` / `:2044` anchors, which have drifted;
    - `openspec/glossary.md` (only the `mvt.py` anchors of the census row, plus the missing `_national_discharge_coverage_rows` entry);
    - `docs/plans/2026-09-03-display-v2-header-river-precip-timeline.md`, which declares itself the design-freeze oracle;
    - `docs/runbooks/production-ops/operating-scope.md`, where the old `current-production-ops.md` anchors moved; `current-production-ops.md` is now an index stub;
  - plus any other hit where a `:N` follows and no longer resolves, under `docs/spec/**`, `docs/runbooks/*.md` (not `receipts/`), `docs/runbooks/production-ops/**`, `docs/governance/**`, `openspec/specs/**` or `openspec/glossary.md`.
- **SHA-pinned, not re-anchored (historical line measurements):** `docs/adr/**` and dated `docs/plans/**` other than the one named above (`docs/governance/DOC_STATUS.md` treats dated plans as historical). The ADR 0002 measurement (the #1338 pre-drop baseline Q1/Q8 text) is pinned at `af49ced22`, where `:530` = `source_identity_stats_sql` and `:603` = `typed_values AS (`. Read back at the pinned SHA with `git show <sha>:services/tiles/mvt.py | sed -n`.
- **Excluded, and listed in the PR with a reason:**
  - `docs/runbooks/receipts/**`: dated evidence, historical by construction.
  - `openspec/changes/archive/**`, `.workplans/**`, `docs/review-loop-log.jsonl`: pinned history.
  - In-flight `openspec/changes/<active>/**`: owned by that change's own PR. Stale anchors there are reported, not edited.
- `feat-reach-geom-oq-findings.md` gets a historical header marker. It is a dated 2026-06-19 survey whose `build_layer_metadata` / `resolve_tile_layer_identity` symbols resolve at no SHA in this repository: the root commit `af49ced22` is dated 2026-07-25 and already lacks them. The marker states this. Its three `#L` deep links become plain text naming the current counterpart (`layer_metadata`, `_ensure_tile_layer`, `_cache_layer_metadata`). The file stays under `docs/runbooks/**` as dated evidence.

### D3 — #2423 on an archived change (mostly already done by #2406)

The archived design's section "坐标漂移：本 PR 自己造成的，已逐符号重解" already re-resolved the coordinates by symbol.

- **Read back at `2ee1f53ed`:** 12 of the 13 are correct.
  - `file_orchestration_journal.py`: `:870`, `:9069`, `:13959`, `:14002`, `:12619`.
  - `scheduler_backfill_predecessor.py`: `:543`, `:573`.
  - `scheduler_candidates.py`: `:2499`, `:2522`, `:1883`, `:2017`, `:2436`, `:2482-2486`, `:1729`.
  - `:1836` / `:1970` is already unified to `:2017`.
- **Two edits only:**
  1. The stale `_event_matches_candidate_rows`（`:13434-13443`） line becomes `2ee1f53ed:services/orchestrator/file_orchestration_journal.py` def `:13706`, `forecast_cycle` branch `:13719-13729`. It is fixed by symbol and read back.
  2. "检测工具与后续清理见 #2423" becomes a closure note: the last coordinate was re-pointed by #2423, and the detection script was declined (YAGNI).
- **Zero diff everywhere else in that section,** from the fact table through the re-point narrative, whose unprefixed old→new pairs are history. The PR proves this with `git diff`.
- **Read-back table:** the PR carries a read-back of all 13 coordinates at `2ee1f53ed`.
- **Validation:** an archived change cannot be targeted by `openspec validate`, so validation is by read-back and diff.

### D4 — #2263 docstring truth plus two tests

- The batch owner chose tests.
  - Both tests extend `tests/test_retention_extra_roots.py`, which already routes the `(None, "", "   ")` shape through `run_retention` (about `:342`) but never asserts `skipped == []`.
  - **Test 1:** `_resolve_runs_only_roots([None, "", "   "], primary=...)` returns no resolved root and **no** skip entry.
  - **Test 2:** `_resolve_runs_only_roots(["relative/runs", "/abs/root"], primary=...)` returns exactly one skip entry. The assertion is on a subset of keys, `key == RUNS_PREFIX` and `reason == EXTRA_ROOT_NOT_ABSOLUTE_REASON`, because the entry also carries `root`. The absolute root is admitted.

  Test 2 shows that silence depends on the value's shape, not on some ambient deployment property.
- The docstring and the body comment are reworded. The tests pin the shape-keyed silent discard, and the text names them for that claim only. "Normally unset on deployments that are not db-free" stays, but is marked as unverified operational context that the function cannot see and the tests do not pin. The PR records the resulting `grep -n "db-free" services/orchestrator/retention.py` output.
- The three code references become symbols:
  - `publish_qdown_cycle`'s `except (SQLAlchemyError, OSError, ValueError)` arm;
  - in `scheduler_discovery.py`, the "READY but naming no state" docstring points at `scheduler_generation_gate.strict_warm_start_evidence`'s `COLD_NEW_MODEL` / `COLD_DECLARED_CUTOVER` branches. `evaluate_transition_decision` holds neither literal; the old `:349-376` pointed at its header;
  - in `scheduler_state_failure.py`, the reference points at `retry.failure_classifier`'s `"policy_blocked"` arm, which is the inline `{"POLICY_BLOCKED", "PERMISSION_DENIED", "TEMPLATE_NOT_ALLOWED"}` set. There is no constant with that name; `NON_TRANSIENT_ERROR_CODES` is a larger set.
- **No repository-wide gate.** 111+ references already exist, and building a baseline or allowlist is out of proportion to this batch. The census numbers go in the PR, and a follow-up issue is filed through issue-scribe unless one already exists (task 3.4).

### D5 — #2365 receipt (read-only evidence already captured, 2026-09-26)

- **Sources**, read through the node-27 NFS mount; nothing was run on node-22:
  - the manifest backups `manifest-last.json.pre-republish-1816-20260824T062347Z` and `manifest-last.20260824T225718Z.bak.json`;
  - the current `manifest-last.json`;
  - `scheduler/state-index/index-last.json`;
  - node-27 `hydro.hydro_run`, read with `BEGIN READ ONLY` through the display role.
- **Decision label.** The scheduler does not persist the `warm_continue` label in the DB or the index. The receipt therefore states the inference and its basis:
  - the **first admitted run** of each new id (a blocked pass leaves no `hydro_run` row) has an `init_state_id` equal to that id's `state_compatibility` clone row in `index-last.json`;
  - `packages/common/state_clone.py` writes the target (new) package identity into the clone row, so the row is same-generation history for the new id;
  - that is the `TransitionDecision.WARM_CONTINUE` condition in `scheduler_generation.py`.
- **Clone provenance source.** It comes only from `index-last.json`. The `hydro.state_snapshot` join returned NULL for those rows (`1816-first-runs.txt`), and the receipt says so.
- **Timing.** These first admitted runs are the backfill cycles 2026-08-23 00Z (GFS) and 12Z (IFS), executed after the 2026-08-24T06:24Z republish.
- **Exceptions:** neither is a cold start. Each records what was observed, not an inferred cause.
  - hetianhe: its two #1816 ids have no `hydro_run` rows. The `pre-hhe-subs-20260825` backup, dated 2026-08-25T02:25Z, already carries different ids (`dg_9d9432bd…`, `dg_2e09e663…`, the later #1832 republish).
  - shj_2shj: its first admitted runs were warm, and the basin later left the manifest.
- **Reproducibility.** The receipt includes the scripts, commands **and their output**.

### D6 — #2326 rebase rules

Follow the issue's recommended steps 1-5:
1. Copy MODIFIED headers exactly from today's canonical, or add a RENAMED block.
2. Each MODIFIED body starts from today's canonical requirement text **verbatim**. That includes its basin-detail / `basinId` *retirement* text and scenarios, and any content later changes added. Only the m11-popup additions that code backs are layered on top. No basin-detail *capability* is re-introduced.
3. Remove `/flood-alerts` and `layer=flood-return-period`.
4. Keep a retained clause only when code backs it, citing the `apps/frontend/src` file, symbol and current line in the PR (issue AC4 asks for line numbers; they are given as helpers after the symbol, per D1).
5. Run a dry archive in a temporary copy before the real archive.

After archiving, `git diff master -- openspec/specs/{frontend-navigation-state,map-feature-popups,met-station-cluster-layer,single-map-shell-routing}` must remove no canonical line without a stated reason. It must add no `flood-alerts` or `flood-return-period`, and no basin-detail capability clause. `openspec validate --all --strict` is measured on master and on HEAD, and HEAD may not have more failures. Pre-existing failures are out of scope and the counts are recorded.

## Risks / Trade-offs

- **Editing an archived design:** justified in D3, and the diff is kept small and provable.
- **The #2085 exclusion of in-flight changes** leaves some stale anchors in those changes. They are listed in the PR, so they are known rather than hidden.
- **#2365 infers `warm_continue`** instead of reading a persisted label. The inference and its rule are stated.
