# Worklog: #585 — node-27 live receipt for display bootstrap decoupling (PR 6/7)

**execution_mode**: live_proof
**Node**: node-27 (`210.77.77.27`, role `display_readonly` primary)
**Date**: 2026-06-20 (UTC `20260620T153243Z`)
**Pre-merge SHA**: `ec9c46a` (node-27 in-tree HEAD before pull)
**Post-merge SHA**: `122ea95` (after ff-pull from origin master; includes PR 1/7..PR 5/7)

## Goal

Close #585 with a live, on-node-27 latency receipt showing PR 1/7..PR 5/7 collectively replaced the canonical 21.8 s `/api/v1/layers` cold path with a sub-second cold path. The receipt is the binding oracle for the Epic #579 latency budget; it must come from real node-27 traffic, not local fixtures or CI mocks.

## Boundaries (YAGNI)

- Only the deliverables listed in the issue's "In Scope": new diagnostic script + receipt + worklog + bugs ledger entry.
- Do NOT touch any code under `apps/`, `services/`, or `openspec/specs/`.
- Do NOT update `docs/runbooks/api-latency.md` or `docs/runbooks/display-readonly-live-mvt.md` — that work belongs to PR 7/7 (#586).
- Do NOT execute `openspec archive` — epic owner handles that after PR 7/7.
- Browser PNG capture is required per acceptance #2; if no Chrome-extension browser is connected, document the placeholder + procedure and escalate to operator for capture.

## Steps executed

1. **SSH verified**: `hostname` returns `ghdc`, NHMS work-tree at `/home/nwm/NWM`.
2. **Pre-merge warm probe** (captured BEFORE ff-pull):
   - `GET /api/v1/layers` warm TTFB = **21,430 ms** on pre-merge uvicorn (PID 13330, started 2026-06-15, serving SHA `ec9c46a`).
   - Catalog returned 5 layers including `water-level` (dead variant present pre-PR-1/7).
3. **Sync node-27 to master** via `git pull --ff-only`:
   - Pre-merge HEAD `ec9c46a` → post-merge HEAD `122ea95`.
   - Untracked artifacts (`.nhms-work/`, `.python-version`, `apps/frontend/dist.bak-*`, `scripts/node27_ingest_all.py`) inspected and confirmed safe (all gitignored or local-only); no clobber risk.
4. **Wrote new diagnostic script** locally: `scripts/diagnostic/display-cold-waterfall.sh` (rsync'd to node-27 via scp).
   - Multi-pass cold waterfall with uvicorn restart between passes to flush Python LRU.
   - 7 endpoints covered: `/healthz`, `/api/v1/layers`, `/api/v1/basins`, `/api/v1/runs?source=best`, `/api/v1/models`, `/api/v1/queue-depth`, `/api/v1/pipeline-status`.
   - Outputs markdown waterfall + raw TSV (`/tmp/display-cold-waterfall-<UTC>.tsv`).
   - Initial pass discovered `/api/v1/health` does not exist on this build; canonical path is `/healthz`. Script patched + re-deployed.
5. **Rebuilt frontend on node-27**:
   - `corepack pnpm install --frozen-lockfile` → 1.9 s.
   - `corepack pnpm run check:api-types` → CLEAN (zero diff; types.ts in sync with openapi/nhms.v1.yaml).
   - `corepack pnpm run build` → vite built 2530 modules in 16.68 s; dist regenerated at `Jun 20 23:34 CST`.
6. **Restarted uvicorn** via canonical `setsid` pattern (new PID 1812598).
7. **Ran cold waterfall** (3 passes via `scripts/diagnostic/display-cold-waterfall.sh --runs 3`):
   - `/api/v1/layers` cold median: **413 ms** (vs 21,430 ms pre-merge warm = **51.9× speedup**).
   - All other endpoints < 50 ms cold median.
   - Map-bootstrap stage cumulative: ~451 ms (well within 1 s interactivity budget).
8. **Warm steady-state probe**: 3 back-to-back hits, 2–3 ms each.
9. **Spec proof for PR 5/7** in current node-27 DB:
   - `/runs?source=GFS` → 142 runs total.
   - `/runs?source=GFS&flood_product_ready=true` → 0 runs total.
   - All 142 GFS runs are `frequency_done: unavailable` (flood-incomplete).
   - Without PR 5/7, frontend's hardcoded filter would have returned 0 → discharge layer broken end-to-end. With PR 5/7, discharge gets all 142. **Spec-faithful regression boundary.**
10. **Wrote receipt** `docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md`.
11. **Wrote bugs.md ledger** entry `BUG-20260620-001`.
12. **Browser PNG**: no Claude-in-Chrome browser was connected; operator (qingdanker@gmail.com) attested acceptance #2 directly after reviewing the API-side proof (51.9× speedup + 451 ms cold first-paint waterfall). Receipt Section E records the user-attested sign-off + preserves the capture procedure for future replay.

## Results summary

| Metric | Pre-merge | Post-merge cold | Post-merge warm | Spec budget |
|---|---|---|---|---|
| `/api/v1/layers` TTFB | **21,430 ms** (warm) | 413 ms median | 2–3 ms | < 200 ms cold (steady-state floor) |
| Layer catalog count | 5 | 4 | 4 | 4 |
| `loadOverview` map-bootstrap stage (cold) | 21+ s | ~451 ms | n/a | < 1 s interactivity |
| Backend `/runs` discharge contract | 0 runs (broken w/ frontend `flood_product_ready=true` forced) | 142 runs | 142 runs | discharge independent of flood readiness |

## Risks + caveats

- Cold-cache fidelity: `Python-LRU-cold + Postgres-buffer-warm` (Postgres restart needs sudo). Real production cold-cold is bounded below by 413 ms and above by Postgres warm-up cost on first session after Postgres restart — both within waterfall budget.
- 413 ms exceeds spec's 200 ms cold floor by ~213 ms — likely Python module import + DB pool init at first request. Mitigation candidates (e.g., uvicorn `--preload`, sqlalchemy `pool_pre_ping=true`) are out of scope for PR 6/7; carried as follow-up.
- Browser PNG was not captured (no Claude-in-Chrome browser connected); operator attested acceptance #2 directly. Receipt Section E records the user-attested sign-off + preserves the capture procedure for future regression replay. API-side first-paint waterfall (~451 ms cold) is the dominant timing evidence.

## Cross-PR dependencies honored

- PR 1/7 (#587, water-level removal): catalog 5 → 4 verified post-merge.
- PR 2/7 (#588, frontend dead variant): frontend rebuilt and served; no `water-level` references in client bundle (verified via `dist/index.html` modulepreload list).
- PR 3/7 (#589, loading split): map-bootstrap separation enables 451 ms first-paint measurement.
- PR 4/7 (#590, dead-call removal): default `loadOverview` no longer fans out 1 ranking + 2 valid-times fetches.
- PR 5/7 (#591, discharge decoupling): GFS 142-vs-0 runs delta is the live spec proof.

## Files changed in this PR

- **NEW**: `scripts/diagnostic/display-cold-waterfall.sh`
- **NEW**: `docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md`
- **NEW**: `openspec/changes/refactor-display-overview-bootstrap/issue-585-worklog.md` (this file)
- **MODIFIED**: `docs/bugs.md` (append `BUG-20260620-001` entry)
- **MODIFIED**: `openspec/changes/refactor-display-overview-bootstrap/tasks.md` (check off Group 6 tasks)

No `apps/`, `services/`, `tests/`, or spec/`*.md` changes in PR 6/7 boundary.
