# Design

## D1 — Default: classes take effect as written

After the rule is gone, a control renders with the size / weight / family / line-height its own classes
name. That is treated as the author's intent and is **not** compensated by deleting the class to keep
today's pixels. The owner reviews the resulting list before merge.

Exceptions, where the as-written result would break an existing specified constraint — fix the classes,
do not weaken the constraint:

1. **Mobile 16px floor** (`openspec/specs/mobile-viewport-shell/spec.md` :119, :124;
   `mobile-curve-sheet`, `mobile-map-overlay-layout`): every visible `select`, select trigger and `input`
   in mobile form stays ≥ 16px. Controls that reached 16px only by inheriting from `body` now need an
   explicit `mobile:text-base` (no `!`).
2. **Issue-time trigger in mobile form** (#2870 coupling note on the issue; revised after review round
   1). `font-mono` now applies to the trigger. On desktop that is the as-written result and stays. In
   mobile form a 16px monospace `MM-DD HH:MM UTC…` does not fit the value element that #2870 tuned for the
   proportional face; widening the trigger to make it fit costs the river cycle bar 16px and, by the
   round-1 reviewer's arithmetic, pushes the caption "GFS + IFS 同步切换" out of view on ~360px-wide
   phones. Ruling: **mobile pixels unchanged** — the trigger carries `mobile:font-sans`, its mobile
   `max-w` stays at the pre-change value, and `e2e/m11-issue-time-trigger.mobile.mocked.spec.ts` keeps
   its assertions. No Linux monospace-width risk remains.
3. **Layout assertions that are not font baselines** (bar heights, no-overflow, tap targets, overlap
   and same-row specs, e.g. `m11-sheet-controls-desktop.mocked.spec.ts` :104-109): stay as they are. If an
   as-written font makes one fail, the tie-break is fixed: rewrite that control's font classes to the
   values that are effective today (pixels unchanged, the class becomes true) and report it as a row.
   Do not edit the assertion.
4. **Classes written knowing they were dead.** `pages/m11/M11BottomControlBar.tsx` :246-253 and
   `pages/m11/M11Controls.tsx` :353-358 document that the size class sits on the `<label>` on purpose and
   the `<select>`'s own `text-xs` is dead. Applying it as written would take the desktop issue-time select
   from 16px to 12px and drop the mobile selects below the floor. Ruling: pixels unchanged at these two
   sites — delete the dead `text-xs` from the `<select>`, keep what the label provides, add an explicit
   `mobile:text-base` on the `<select>` only if the floor needs it, and rewrite both comments to describe
   the new truth. `DESKTOP_CYCLE_FONT_PX` / `DESKTOP_SPEED_FONT_PX` therefore do not move.

Pure font baselines pinned to the inherited value are re-pinned to the as-written value, one PR-table
row each. Known ones: `controlFontPx` in `e2e/m11-sheet-controls-desktop.mocked.spec.ts` :44 is used for
both the trigger (`text-[11px]`, stays 11) and the variable toggles (`text-xs`, becomes 12) — split it
into two constants and fix the case title that says "字号 11px"; `MASTER_CONTROL.fontSizePx` in
`e2e/ops-fallback-desktop.mocked.spec.ts` :35/:58/:60 (16 → 14).

`e2e/ops-fallback-desktop.mocked.spec.ts` :64-74 is not a re-pin: it injects `main{font-size:12px}` and
asserts five form controls keep inheriting 12px — the very behaviour being removed. Invert it in place
into the D4 assertion (same injection; the five controls and the Role trigger are 14px).

Stale comments to rewrite: `m11-control-bar-desktop.mocked.spec.ts` :31-35,
`ops-fallback-desktop.mocked.spec.ts` :24-25, :34-39, `ops.mobile.mocked.spec.ts` :184.
`openspec/specs/mobile-viewport-shell/spec.md` :182 ("existing monitoring regression assertions pass
without modification") refers to `monitoring.mocked.spec.ts`, which pins no font and stays untouched.

## D2 — `!` removal

`mobile:text-base!` on `SelectTrigger` (`components/ui/select.tsx`), `M11PopupChrome.tsx`'s issue-time
trigger and `MonitoringPage.tsx`'s input lose the `!` when the plain utility yields ≥ 16px in mobile
form. A caller that passes a smaller mobile size through `className` must still not drop below the floor
in the shipped call sites; no new guard is added for hypothetical callers.

## D3 — The before/after table (owner sign-off input)

A one-off measurement script, **not committed** (it lives under the git-ignored `.workplans/2861/`, with
its JSON and Markdown output), drives the mocked app with Playwright and records computed `font-size`,
`font-weight`, first `font-family` and `line-height` of every rendered `button` / `input` / `select` /
`textarea` at the spec-oracle viewports 1280×900, 390×664 and 750×342 (the mobile-landscape form is the
only one that renders `M11Controls`' speed select). Reuse the control enumeration in
`e2e/support/touchAudit.mocked.ts`. States to drive: `/` with the three panels expanded, the river and
station curve windows open, the log dialog, a region error boundary, `/ops` including the rollback
select, the monitoring page and the model-assets page.

"Before" and "after" are measured in one checkout: right after deleting the rule and **before any class
edit**, measure once with `page.addStyleTag({ content: 'button,input,select,textarea{font:inherit}' })`
(the same unlayered rule, so the cascade equals master's) and once without. The later class edits (D1's
exceptions, D2) are measured again and listed as separate rows.

Output for the PR body: the rows that differ (page, element, location, property, before → after), grouped
by page, plus the count of unchanged rows. Screenshots are not compared in the mocked lane
(`src/__tests__/mobileSpecOracles.test.ts` forbids it there); before / after screenshots come from the
node-27 receipt.

## D4 — Utility-works assertion and the unlayered-rule checks

- Browser level: the inverted case in `e2e/ops-fallback-desktop.mocked.spec.ts` (see D1). Mutation:
  restoring the unlayered rule in `src/index.css` turns it red. The mobile half already exists:
  `e2e/ops.mobile.mocked.spec.ts` :185-190 becomes a utility-works check once the `!` is gone.
- Source level: a vitest next to `src/__tests__/indexCssViewportVariants.test.ts` scanning
  `src/index.css` top-level (brace depth 0) rules: none sets `font` on `button`, `input`, `select` or
  `textarea`.
- Built artefact (the issue's acceptance item): the same scan over the built CSS, added to the existing
  `check:bundle` step (`scripts/check-bundle-size.sh` already requires `dist/`), not to vitest — a vitest
  reading `dist/` would read a stale local build.

## Must preserve

- Mobile 16px floor specs and all `*.mobile.mocked.spec.ts` assertions.
- Non-font layout assertions (D1.3).
- Controls rendered as non-form elements (`SelectPrimitive.Item`, dropdown items) are unaffected.

## Evidence floor

- `pnpm build` then `pnpm test` (CI order), `pnpm typecheck`, `pnpm run check:types`, `check:bundle`;
  full mocked Playwright lane with counts; the D4 mutation (browser, source and artefact checks each
  shown red against the restored rule).
- The D3 table and its unchanged-row count; list of every re-pinned baseline (old → new) and every class
  edit made under D1's exceptions.
- `openspec validate layer-font-inherit-reset --strict --no-interactive`.
- node-27 browser receipt (PR build via the verify checkout's preview port against the live API,
  production untouched): both mobile presets and the desktop oracle pass; before / after screenshots of
  `/` (controls bar, both curve windows) and `/ops` at desktop and mobile. Orchestrator runs it.
