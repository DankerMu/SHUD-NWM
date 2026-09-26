# node-27 live receipt: river-click trusted pointer dispatch (#1970, batch Q)

## Verdict

**PASS.** On the public site `https://test.nwm.ac.cn` `/`, a real trusted browser click on each of the three D4 pins reached both the GFS and the IFS forecast series and the chart, with P95 well under the 2000 ms gate. Receipts use schema 1.1 with `click_dispatch=trusted_pointer_event`.

## Deploy (tasks 5.1, 2026-09-26T07:09Z, `/home/nwm/tmp/q/deploy/deploy.log`)

1. `git status --porcelain` was clean; then `git pull --ff-only` moved the checkout from `579b2a6f` to `325ccc7aa` (the #2643 merge).
2. `corepack pnpm@10.11.0 install --frozen-lockfile`, then `vite build --outDir dist.new`, then `mv -T dist dist.old && mv -T dist.new dist`. `dist.old` is kept for rollback; there was no earlier `dist.old`.
3. Served `index.html` sha went from `2d1f3ab7d7febaa5` to `9c85c9508eab7b8f`, which equals the new `dist` and the §4.3 throwaway build. No restart was needed.

## Pin discovery (tasks 5.2, D4)

The runbook C4 read-only discovery block was run verbatim against the public API. Its read-only SQL ran with `BEGIN READ ONLY` through the display role `nhms_display_ro`. The pins are listed in the verdict table below; each one's segment detail returned 200.

- **First attempt: FAIL, correctly fail-closed.** The block merged in #2643 built the pin from the latest-product `model_id`. On live data that field is a direct-grid variant `model_id` (`dg_be70a045…`), not the model-package id that prefixes segment ids, so the pin returned 404 and the block exited 1 before any lane ran. Evidence: `/home/nwm/tmp/q/public/failed-discovery-583PXP`.
- **The fix, in this archive PR:** the pin is now the network's single `…_shud_riv_000001` segment in `core.river_segment`, with exactly one match required.
- **Second attempt: this receipt.** It ran the fixed block, and 48 product networks took part in the ranking.

The median network is `basins_huaiyss`. That is the rule's pick over all current product networks; the §4.3 throwaway gate used `basins_haihe_ziyahe`.

## Receipts (tasks 5.2)

Each pin ran:
1. the runbook's private run-directory prelude (`.nhms-issue1895-riverclick-XXXXXX`, mode 0700; the receipt is absent beforehand, then mode 0600);
2. the exact merged command (`corepack pnpm@10.11.0 --dir apps/frontend run test:e2e:live-river-click`, single worker, 0 retries);
3. the runbook binder block (file and mtime checks, `check-jsonschema`, `river-click-receipt-binder.mjs`).

| role | pin | segments | CMD_EXIT | status | click_dispatch | warmup + accepted | P95 ms | binder |
|---|---|---|---|---|---|---|---|---|
| largest | `basins_shj_shud_shud_riv_000001` | 29428 | 0 | PASS | trusted_pointer_event | 1 + 20 | 432.3 | `BINDER: PASS` |
| median | `basins_huaiyss_shud_shud_riv_000001` | 4469 | 0 | PASS | trusted_pointer_event | 1 + 20 | 431.4 | `BINDER: PASS` |
| smallest | `basins_tailanhe_shud_shud_riv_000001` | 63 | 0 | PASS | trusted_pointer_event | 1 + 20 | 421.9 | `BINDER: PASS` |

Receipts on node-27, all gitignored after this PR:

| role | path | sha256 prefix |
|---|---|---|
| largest | `/home/nwm/NWM/.nhms-issue1895-riverclick-X9uNNT/…-20260926T072109Z.json` | `3d8b892f002d16b8` |
| median | `…-B8vm33/…-20260926T072148Z.json` | `537d7df076b00d72` |
| smallest | `…-KcycoA/…-20260926T072219Z.json` | `c327e8a4cf0ca789` |

Copies are in `/home/nwm/tmp/q/public2/`.

Accepted-sample durations, in ms:
- largest: 357.8, 400, 405.6, 411.1, 392.3, 427.5, 296.6, 286.6, 292, 437.9, 384.9, 301.6, 298, 399.9, 305.3, 301.8, 409.6, 432.3, 417.7, 305.7
- median: 468.4, 307.2, 345.4, 297.2, 310, 291.9, 421.6, 431.4, 310.2, 297.6, 306.6, 308.2, 398, 391.9, 314.4, 311.2, 309.3, 297.3, 298.4, 254.8
- smallest: 325.1, 429.5, 305.6, 293.2, 249.9, 312, 304.5, 292.2, 310.2, 243.5, 247.3, 323.9, 313.4, 290.5, 299.8, 303.1, 323.4, 413.7, 421.9, 295.4

## Before and after (§1.3)

| network | hook-dispatch 1.0 P95 (§1.3) | trusted-click 1.1 P95 |
|---|---|---|
| shj | 472.8 ms | 432.3 ms |
| haihe_ziyahe | 388.7 ms | 430.7 ms (§4.3 throwaway stack) |
| tailanhe | 461.6 ms | 421.9 ms |

The two mechanisms are **not comparable**. In 1.1, t0 is the trusted pointer-down, which comes earlier than the old pre-dispatch t0. The Playwright mouse move also triggers the hover latest-product prefetch before the pointer-down. Both runs are far below 2000 ms.

## Pre-merge gate (tasks 4.3), for reference

On the throwaway stack running `c1af3a1c1`, all three pins returned PASS:

| pin | P95 ms |
|---|---|
| shj | 508.6 |
| haihe_ziyahe | 430.7 |
| tailanhe | 410.5 |

The first gate run, on `ef19c721e`, FAILed on the `{x, y}` whole-viewport query defect, which was fixed in fix pass 1 (§2F).

## Boundaries

- The DB was only read, through the read-only role.
- The production `:8080` API was not restarted.
- `/ops` and `/monitoring` were not exercised.
- The lane visits `/` only.
