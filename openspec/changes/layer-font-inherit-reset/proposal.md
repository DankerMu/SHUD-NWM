# Remove the unlayered `font: inherit` reset so font utilities work on form controls

Issue: #2861 (ruling 2026-10-11: option A). Fixture level: **expanded** (shared global style entry,
every page). Risk packs: frontend-display (site-wide visual), e2e baselines, mobile 16px floor.
Review seats: `correctness`; `test-evidence+spec-compliance`; `integration`.

**Merge gate: owner visual sign-off.** This PR is not merged by the workflow. It stops once CI is green,
review is closed and the node-27 browser receipt plus the before/after table are posted.

## Why

`apps/frontend/src/index.css:136-141` declares `button, input, select, textarea { font: inherit }`
outside any `@layer`. Unlayered declarations beat every layered one, so on those four elements every
non-`!` `text-*`, `font-*` and `leading-*` utility is silently dead; Tailwind's preflight already carries
the same reset inside `@layer base`. The only working spelling today is `!`, which is how
`mobile:text-base!` crept into `SelectTrigger`.

## What Changes

- Delete the unlayered rule. Nothing replaces it (preflight's `@layer base` rule is the reset).
- Every font utility on a `button` / `input` / `select` / `textarea` now takes effect **as written**.
  That is the intended outcome; the resulting visual changes are enumerated, not hidden (see design.md).
- Remove the `!` from the `mobile:text-base!` workarounds where the plain utility now satisfies the
  mobile 16px floor.
- Re-pin the desktop e2e font baselines that move, each listed in the PR with old and new value.
- One browser-level assertion that a non-`!` font-size utility on a `button` yields the class's size,
  not the ancestor's.

## Out of scope

- Redesigning the type scale or tokens; other unlayered globals (`a { color: inherit … }`,
  `body { margin: 0 }`, the `:focus-visible` rule) — recorded only.
- Any backend change.
