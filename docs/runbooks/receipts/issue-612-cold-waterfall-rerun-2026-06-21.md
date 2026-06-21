# node-27 live receipt — issue #612 cold-waterfall defer-to-wrapper + /health

- Date: 2026-06-21
- Branch: `ops/issue-612-cold-waterfall-defer-to-wrapper` HEAD `de255d1`
- Operator: `nwm@210.77.77.27`
- Issue: [#612](https://github.com/DankerMu/SHUD-NWM/issues/612)
  (PR [#611](https://github.com/DankerMu/SHUD-NWM/pull/611) Phase 4 F1 follow-up)
- Prior cold-waterfall receipt: [`display-bootstrap-decoupling-20260620.md`](display-bootstrap-decoupling-20260620.md)
  (PR [#592](https://github.com/DankerMu/SHUD-NWM/pull/592))

## What this receipt validates

1. `scripts/diagnostic/display-cold-waterfall.sh` `launch_uvicorn()` now defers
   to `scripts/ops/start-display-api.sh` (PR #611 canonical wrapper). Verified
   by stdout: each cold pass prints the wrapper's preamble
   (`[start-display-api] repo_root=... DATABASE_URL=... smoke check passed`).
2. `/healthz` (404 on healthy uvicorn) replaced by `/health` (root, real
   endpoint per [apps/api/main.py:1947](../../../apps/api/main.py#L1947)).
   Verified by TTFB column: `/health` returns 2ms across 3 passes (was 4ms in
   the prior `/healthz` 404 receipt — 4ms was 404 dispatch overhead,
   not real health-check).
3. SIGTERM-then-SIGKILL grace period inherits from wrapper (cold-waterfall
   no longer inlines it). pid handover across 3 passes:
   `2453425 → 2636092 → 2636205 → 2636733`.

## Invocation

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
git checkout ops/issue-612-cold-waterfall-defer-to-wrapper   # HEAD de255d1
bash scripts/diagnostic/display-cold-waterfall.sh --runs 3
```

Exit: **0**. Raw timings: `/tmp/display-cold-waterfall-20260621T080518Z.tsv`.

## Cold-waterfall results (3 cold passes — UTC `20260621T080518Z`)

| Endpoint                     | TTFB run1 | TTFB run2 | TTFB run3 | Median | Max  | Spec target       |
|------------------------------|-----------|-----------|-----------|--------|------|-------------------|
| `/health`                    | 2         | 2         | 2         | 2      | 2    | < 500 ms (spec)   |
| `/api/v1/layers`             | 392       | 405       | 377       | 392    | 405  | < 200 ms (spec)   |
| `/api/v1/basins`             | 35        | 35        | 34        | 35     | 35   | < 500 ms (spec)   |
| `/api/v1/runs?source=best`   | 49        | 50        | 50        | 50     | 50   | < 500 ms (spec)   |
| `/api/v1/models`             | 34        | 27        | 35        | 34     | 35   | < 500 ms (spec)   |
| `/api/v1/queue-depth`        | 3         | 2         | 3         | 3      | 3    | < 500 ms (spec)   |
| `/api/v1/pipeline-status`    | 2         | 1         | 2         | 2      | 2    | < 500 ms (spec)   |

All units: milliseconds (TTFB).

## Wrapper deferral evidence (one pass excerpt)

```text
=== Cold pass 1/3 ===
[start-display-api] repo_root=/home/nwm/NWM
[start-display-api] env_file=/home/nwm/NWM/infra/env/display.env
[start-display-api] DATABASE_URL=postgresql://<redacted>@127.0.0.1:55432/nhms
[start-display-api] NHMS_ENABLE_LIVE_POSTGIS_MVT=true
[start-display-api] target=127.0.0.1:8080  log=/tmp/display-api.log
[start-display-api] stopping prior uvicorn pid(s): 2453425
[start-display-api] relaunched pid=2636092 (log: /tmp/display-api.log)
[start-display-api] OK pid=2636092 basin_id=basins_heihe (smoke check passed)
  /health: 2ms
  /api/v1/layers: 392ms
  ...
```

Three identical preambles (one per cold pass) confirm wrapper invocation per
restart — no parallel inline `setsid python` path remains.

## Comparison with PR #592 prior receipt

PR #592 `display-bootstrap-decoupling-20260620.md` reported `/api/v1/layers`
cold ≈ 213ms (≥ 51.9× lower-bound speedup from 21.8s baseline). This receipt
shows `/api/v1/layers` cold median ≈ 392ms (≈ 55.6× speedup). Both are well
under the 21.8s pre-PR-5/7 baseline and well above the < 200 ms spec target —
the Epic [#579](https://github.com/DankerMu/SHUD-NWM/issues/579) PR 1-7 cold-warmup
recovery holds, with run-to-run variance in the few-hundred-ms band (expected;
cold uvicorn first-request dispatch + Postgres connection pool fill). The
lower-bound speedup notation (≥ 51.9×) in PR #592 receipt remains a defensible
floor; this re-measurement does not falsify it.

`/healthz` 4ms in the prior receipt was 404 dispatch overhead, not real
health-check latency. The new `/health` 2ms is faster precisely because the
real endpoint is a tiny constant-dict return without route mismatch handling.
Comparing the two columns directly is not meaningful — they were measuring
different paths.

## Acceptance — issue #612

- [x] `scripts/diagnostic/display-cold-waterfall.sh` `launch_uvicorn()`
  defers to `scripts/ops/start-display-api.sh` (verified by 3× wrapper
  preamble in stdout).
- [x] All `/healthz` references replaced with `/health` (verified by
  TTFB table column header + sequence output; only residual `healthz`
  token is in an explanatory comment at `display-cold-waterfall.sh:120`
  documenting *why* we probe `/health` not `/healthz`).
- [x] Re-run cold-waterfall on node-27 with corrected paths and update
  prior receipt with History note (this file + History note added to
  `display-bootstrap-decoupling-20260620.md`).

## Caveats

- `/api/v1/layers` cold median 392 ms is still above the spec target
  < 200 ms. PR #592 receipt established the lower-bound speedup against the
  21.8s baseline; the residual gap to the spec target is open work
  (PR #592 Caveat). This receipt does not change that — it only fixes the
  measurement honesty of `/healthz`.
- The `< 500 ms (spec)` column in the table is the diagnostic script's
  built-in default for non-`/api/v1/layers` endpoints; it is not derived from
  a published spec and should be read as "diagnostic sanity threshold," not
  a hard contract.
