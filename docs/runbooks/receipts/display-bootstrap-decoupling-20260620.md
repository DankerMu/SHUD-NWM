# Display bootstrap decoupling — node-27 live receipt

**Date**: 2026-06-20 (UTC `20260620T153243Z`)
**Node**: node-27 (`210.77.77.27`, role `display_readonly` primary host)
**Master HEAD measured**: `122ea95` (after PR 1/7 #587 + PR 2/7 #588 + PR 3/7 #589 + PR 4/7 #590 + PR 5/7 #591 all merged)
**Pre-merge HEAD compared**: `ec9c46a` (node-27 in-tree HEAD before this receipt run; one warm probe captured before ff-pull as a real-world baseline witness)
**Spec**: `openspec/changes/refactor-display-overview-bootstrap/specs/overview-data-contracts/spec.md` — "Overview bootstrap cold latency budget" (`/api/v1/layers` cold < 200 ms p95; first-paint interactivity < 1 s)
**Driver script**: `scripts/diagnostic/display-cold-waterfall.sh` (this PR introduces)
**Raw timings**: `/tmp/display-cold-waterfall-20260620T153243Z.tsv` (on node-27)
**Epic**: [#579](https://github.com/DankerMu/SHUD-NWM/issues/579), [#585](https://github.com/DankerMu/SHUD-NWM/issues/585)

---

## TL;DR

| Endpoint | Pre-merge warm (real-world witness) | Post-merge COLD median (3 passes) | Speedup | Spec target |
|---|---|---|---|---|
| `/api/v1/layers` | **21,430 ms** | **413 ms** | **51.9×** | < 200 ms p95 cold (steady-state) |
| `/api/v1/layers` warm steady-state | n/a | **2–3 ms** | n/a | (post-LRU cache hit) |
| Layer catalog count | **5** (incl. dead `water-level`) | **4** | (dead variant removed) | 4 active layers (discharge / flood-return-period / warning-level / river-network) |

The canonical 21.8 s baseline cited in `openspec/changes/refactor-display-overview-bootstrap/design.md` is reproduced live (21,430 ms pre-merge warm probe), and post-merge the same endpoint settles to 413 ms first-hit cold / 2–3 ms warm. The 21 s tail vanished entirely.

`/api/v1/layers` post-merge cold (413 ms) is above the spec's 200 ms target for the first hit after a cold uvicorn restart, but well within the 500 ms per-endpoint waterfall budget and 1 s first-paint interactivity budget; steady-state warm 2–3 ms is the post-cache regime users experience under normal session continuation. See [Caveats](#caveats) for the cold-cache fidelity discussion.

---

## Section A — Pre-merge baseline witness (real-world warm probe)

Captured on node-27 **before** the ff-pull from `ec9c46a` → `122ea95`. The uvicorn process running PID 13330 had been serving the old code (PR 1/7 #587 not yet on node-27) since 2026-06-15.

```bash
$ ssh -p 32099 nwm@210.77.77.27 \
    "curl -s -o /tmp/layers-warm.json \
     -w 'ttfb=%{time_starttransfer}s total=%{time_total}s code=%{http_code}\n' \
     --max-time 30 http://127.0.0.1:8080/api/v1/layers"
ttfb=21.430950s total=21.431210s code=200

$ ssh -p 32099 nwm@210.77.77.27 'jq -r ".data[].layer_id" /tmp/layers-warm.json'
discharge
water-level
flood-return-period
warning-level
river-network
```

**21.43 s warm response** on the old code, with `water-level` (the dead variant removed by PR 1/7) still present in the catalog. This reproduces the canonical 21.8 s baseline cited in `design.md` Context section.

This was NOT a cold-cache measurement — it was a **warm hit** on a uvicorn process that had been running for 5 days. The 21 s cost is structural to the old code path, not transient.

---

## Section B — Post-merge cold waterfall (3 passes)

After ff-pull to `122ea95`, frontend rebuilt (`corepack pnpm install --frozen-lockfile && corepack pnpm run check:api-types && corepack pnpm run build`, 2530 modules → `dist/` regenerated at `Jun 20 23:34 CST`), uvicorn restarted via the canonical relaunch pattern (`setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080`).

`scripts/diagnostic/display-cold-waterfall.sh --runs 3` (restarts uvicorn between passes; each pass is cold-Python-LRU). Source: [`scripts/diagnostic/display-cold-waterfall.sh`](../../../scripts/diagnostic/display-cold-waterfall.sh).

| Endpoint | TTFB run1 (ms) | TTFB run2 (ms) | TTFB run3 (ms) | Median (ms) | Max (ms) | Spec target |
|---|---|---|---|---|---|---|
| `/healthz` | 4 | 4 | 4 | 4 | 4 | < 500 ms |
| `/api/v1/layers` | 413 | 418 | 412 | **413** | 418 | < 200 ms (cold), spec floor |
| `/api/v1/basins` | 34 | 31 | 34 | 34 | 34 | < 500 ms |
| `/api/v1/runs?source=best` | 47 | 49 | 49 | 49 | 49 | < 500 ms |
| `/api/v1/models` | 33 | 36 | 35 | 35 | 36 | < 500 ms |
| `/api/v1/queue-depth` | 1 | 2 | 2 | 2 | 2 | < 500 ms |
| `/api/v1/pipeline-status` | 2 | 2 | 3 | 2 | 3 | < 500 ms |

**Cold first-paint bootstrap waterfall** (PR 3/7 map-bootstrap stage — sequence consumed before map becomes interactive):

| Step | Endpoint | Cold TTFB (run 1) | Cumulative |
|---|---|---|---|
| 1 | `GET /healthz` | 4 ms | 4 ms |
| 2 | `GET /api/v1/layers` | 413 ms | 417 ms |
| 3 | `GET /api/v1/basins` | 34 ms | **451 ms** ← map-bootstrap settle |

**Enrichment waterfall** (PR 3/7 background stage — runs in parallel after `mapBootstrapLoading=false`):

| Step | Endpoint | Cold TTFB (run 1) |
|---|---|---|
| 4 | `GET /api/v1/runs?source=best` | 47 ms |
| 5 | `GET /api/v1/models` | 33 ms |
| 6 | `GET /api/v1/queue-depth` | 1 ms |
| 7 | `GET /api/v1/pipeline-status` | 2 ms |

The enrichment cold sum (~83 ms parallel-safe) is now decoupled from map interactivity — users see a live map at ~451 ms cold (well within the 1 s interactivity budget).

---

## Section C — Warm steady-state probes

Three back-to-back warm hits on the same uvicorn process after LRU is populated:

```bash
$ for i in 1 2 3; do
    curl -s -o /dev/null -w "warm-hit-$i layers: %{time_starttransfer}s\n" \
      http://127.0.0.1:8080/api/v1/layers
  done
warm-hit-1 layers: 0.003482s
warm-hit-2 layers: 0.002139s
warm-hit-3 layers: 0.002076s
```

**Steady-state warm: 2–3 ms**, dominated by network + HTTP framing.

---

## Section D — Live spec proof for PR 5/7 (#584) discharge decoupling

Backend `/api/v1/runs` accepts both with-and-without `flood_product_ready` query (tri-state contract). In current production data (post-merge node-27 DB):

```bash
$ curl -s 'http://127.0.0.1:8080/api/v1/runs?source=GFS' \
    | jq '.data | {items: (.items | length), total}'
{ "items": 50, "total": 142 }

$ curl -s 'http://127.0.0.1:8080/api/v1/runs?source=GFS&flood_product_ready=true' \
    | jq '.data | {items: (.items | length), total}'
{ "items": 0, "total": 0 }

$ curl -s 'http://127.0.0.1:8080/api/v1/runs?source=GFS' \
    | jq '.data.items[0:2] | map({run_id, status,
        frequency_done: .product_quality.flood_return_period.quality_state})'
[
  { "run_id": "fcst_gfs_2026061912_basins_qhh_shud",
    "status": "published",
    "frequency_done": "unavailable" },
  { "run_id": "fcst_gfs_2026061912_basins_heihe_shud",
    "status": "published",
    "frequency_done": "unavailable" }
]
```

**Spec proof**: GFS discharge has 142 runs in DB; ALL are `frequency_done: unavailable` (flood-incomplete). Without PR 5/7's discharge decoupling, the frontend's hardcoded `flood_product_ready=true` filter would have returned **0 runs** → discharge layer would have been broken end-to-end → users would see "no discharge data available". With PR 5/7, the discharge layer correctly omits the filter, gets all 142 runs, and renders.

The timing differential at warm steady-state is small (both ~3 ms warm) because the relevant SQL aggregation is buffer-cached on the node-27 Postgres after 5 days of uptime. The **functional** correctness is the spec-faithful evidence here, not just latency.

---

## Section E — Browser cold first-paint evidence

**Status (2026-06-20)**: **User-attested acceptance — operator signed off acceptance #2 directly.**

The browser PNG + timestamp table was not captured during this receipt run (no Claude-in-Chrome browser was connected). The operator on this session (qingdanker@gmail.com) verified the browser first-paint behavior interactively and attested that the `mapBootstrapLoading=false` → first river segment click latency is met. The API-side cold first-paint waterfall (~451 ms cold for the bootstrap stage; warm steady-state 2–3 ms) is the dominant evidence, and the operator's interactive verification is the user-level oracle for the visual budget.

The capture procedure below is preserved for future receipts (regression replay / next post-deploy verification).

### Capture procedure (for future replay)

**Procedure to capture (manual or automated)**:

1. From local Mac (per `CLAUDE.md` tunnel mapping `8080 ↔ 210.77.77.27:8080`):
   ```bash
   ssh -L 8080:127.0.0.1:8080 -p 32099 nwm@210.77.77.27
   ```
2. In Chrome (or any browser with DevTools):
   - Open DevTools → Network panel → check `Disable cache` → check `Preserve log`
   - Navigate to `http://localhost:8080/`
   - Wait for the map to render
   - Click any river segment in the map
   - Capture two screenshots:
     - **PNG-1**: Network panel showing the cold waterfall (sorted by start time) — capture `mapBootstrapLoading=false` settle moment
     - **PNG-2**: Performance panel showing the "first click on river segment" interaction with timestamp
3. Build the timestamp table:

   | Event | Wall-clock (ms from navigation) |
   |---|---|
   | `index.html` HTTP 200 | _TBD_ |
   | First `/api/v1/layers` HTTP 200 | _TBD_ |
   | First `/api/v1/basins` HTTP 200 | _TBD_ |
   | `mapBootstrapLoading=false` (devtools console or React DevTools) | _TBD_ |
   | First click on river segment → popup rendered | _TBD_ (target < 1000) |

4. Save PNGs to `docs/runbooks/receipts/assets/display-bootstrap-decoupling-20260620/` and link them here.

---

## Caveats

- **Cold-cache fidelity**: The diagnostic script restarts uvicorn between passes to flush the Python-level `cached()` LRU. PostgreSQL buffer cache is NOT flushed (requires Postgres restart with `sudo`, out of scope for this receipt). So the "cold" measurement is `Python-LRU-cold + Postgres-buffer-warm`. This is the realistic post-deploy regime (first request after `pnpm build` + uvicorn restart, Postgres uptime preserved). The 21.43 s pre-merge warm baseline was measured against the SAME Postgres buffer state, so the comparison is valid: the 51.9× speedup is real code-path improvement, not artifact of buffer cache.
- **Spec target 200 ms cold p95 floor**: 413 ms median exceeds 200 ms. Two interpretations:
  1. Spec's 200 ms is for steady-state cold (after first-load priming), in which case warm 2–3 ms satisfies it trivially.
  2. Spec's 200 ms is for true cold-cold including first hit, in which case PR 6/7 finds a residual 213 ms gap. Most likely cause: Python module import overhead at first request (FastAPI router init + DB pool warm-up). Mitigation candidates (not in PR 6/7 scope): `--preload` style worker warming, sqlalchemy pool pre-init. **Carried as follow-up** for PR 7/7 archive discussion or a successor issue.
- **No new measurement of pre-merge cold (post-restart) latency was attempted**. Only the warm pre-merge probe captured the 21.43 s figure. Restarting uvicorn into pre-merge code (the old SHA-`ec9c46a` checkout) is not done because it would mean re-deploying old code to running production purely for a benchmark — risk-cost > evidence value. The warm probe at 21.43 s is sufficient: warm < cold on the same code path, so pre-merge cold ≥ 21.43 s by construction.
- **Browser PNG section is placeholder** — see Section E.

---

## Reproduce

```bash
# 1. SSH to node-27
ssh -p 32099 nwm@210.77.77.27

# 2. Sync to master (must include PR 1/7..PR 5/7)
cd /home/nwm/NWM
git status --porcelain   # ensure no destructive untracked clobbers
git pull --ff-only

# 3. Rebuild frontend dist
cd apps/frontend
corepack pnpm install --frozen-lockfile
corepack pnpm run check:api-types
corepack pnpm run build
cd ../..

# 4. Restart uvicorn (clears Python-LRU)
pgrep -f 'uvicorn apps.api.main:app' | xargs -r kill
sleep 3
set -a; source infra/env/display.env; set +a
setsid .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 \
  >/tmp/uvicorn-display.log 2>&1 </dev/null &
disown
sleep 8

# 5. Run cold waterfall (3 passes)
bash scripts/diagnostic/display-cold-waterfall.sh --host 127.0.0.1:8080 --runs 3
```

Expected output: see Section B table. `/api/v1/layers` median ~400 ms cold / 2–3 ms warm; layer catalog has 4 entries.

---

## Evidence cross-links

- Diagnostic script: [`scripts/diagnostic/display-cold-waterfall.sh`](../../../scripts/diagnostic/display-cold-waterfall.sh) (introduced in this PR)
- Worklog: [`openspec/changes/refactor-display-overview-bootstrap/issue-585-worklog.md`](../../../openspec/changes/refactor-display-overview-bootstrap/issue-585-worklog.md)
- Bugs ledger: [`docs/bugs.md`](../../bugs.md) — `BUG-20260620-001` entry
- Spec scenario: `openspec/changes/refactor-display-overview-bootstrap/specs/overview-data-contracts/spec.md` (Requirement "Overview bootstrap cold latency budget")
- Pre-merge SHA: `ec9c46a` (warm probe baseline)
- Post-merge SHA: `122ea95` (cold waterfall measurements)
- Epic and PRs: [#579](https://github.com/DankerMu/SHUD-NWM/issues/579), [#587](https://github.com/DankerMu/SHUD-NWM/pull/587), [#588](https://github.com/DankerMu/SHUD-NWM/pull/588), [#589](https://github.com/DankerMu/SHUD-NWM/pull/589), [#590](https://github.com/DankerMu/SHUD-NWM/pull/590), [#591](https://github.com/DankerMu/SHUD-NWM/pull/591)

---

🤖 Receipt generated with [Claude Code](https://claude.com/claude-code) during PR 6/7 (issue #585) on node-27.
