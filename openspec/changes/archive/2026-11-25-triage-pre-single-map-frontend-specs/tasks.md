# Tasks

Evidence floor: `openspec validate --strict`; every added requirement names
its implementing file; every removed requirement names its owner or says
it was dropped; every added scenario has a test that can go red; the
inventory no longer describes deleted modules as live.

## 1. Spec deltas (orchestrator)

- [x] 1.1 `forecast-frontend`: all seven requirements removed; the
  capability is retired.
- [x] 1.2 `national-overview-page`: four requirements removed, "Overview
  reports missing data in floating notices" added.
- [x] 1.3 `multi-source-comparison-ui`: two requirements removed, "GFS 与
  IFS 预报曲线同图渲染" added.
- [x] 1.4 `analysis-forecast-curve-splicing`: the two frontend requirements
  removed.
- [x] 1.5 `map-feature-popups`: two requirements removed; the river window
  requirement rewritten with the per-source degraded display; the station
  waiting state added.

## 2. Governance doc (orchestrator)

- [x] 2.1 `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`: the #410 entry
  records the #432 deletion (`1710375b1`) and loses the "not yet removed",
  "must keep" and `AppRoutes.test.tsx:12,214` claims; the sentence and the
  inventory row that class `apps/frontend/src/components/flood` as live
  are marked historical. Other flood mentions in the file are not touched.

## 3. Test evidence (implementer; tests only, no production line)

- [x] 3.1 For each of the twelve added scenarios, name the existing test
  (file and test name) that goes red if the behaviour changes. Where none
  does, add one in the test file that owns the neighbouring cases. Known
  gaps: the struck-through legend marker of the river window, the tooltip
  line format, and possibly the axis label format and axis name.
- [x] 3.2 A red run for every test added in 3.1 (the production line
  changed locally, the test red, the file restored byte-identical),
  recorded in `.workplans/3243/red-runs.log`.
- [x] 3.3 Report any scenario the code contradicts; the spec is corrected,
  not the code.

## 4. Archive-time edits (orchestrator)

- [x] 4.1 Strip the stray `---` separator lines from
  `openspec/specs/forecast-frontend/spec.md` so that archive can retire
  the capability (it refuses while the file holds content outside its
  requirements), and from `openspec/specs/multi-source-comparison-ui/spec.md`.
- [x] 4.2 After the archive, write the Purposes of `national-overview-page`
  and `analysis-forecast-curve-splicing` and correct the Purpose of
  `multi-source-comparison-ui`.

## 5. Evidence

- [x] 5.1 `cd apps/frontend && pnpm typecheck && pnpm lint && pnpm test`.
- [x] 5.2 `openspec validate triage-pre-single-map-frontend-specs --strict
  --no-interactive` before the archive; after it, the five capabilities
  pass `openspec validate --specs` (four specs; `forecast-frontend` is
  gone), `git grep -n forecast-frontend -- openspec/specs openspec/project-profile.md openspec/glossary.md docs scripts tests` shows no
  dangling reference, and the entropy hard gate and its tests are at the
  baseline.
- [x] 5.3 In the inventory, no line outside history describes
  `components/flood`, `stores/floodAlert`, `lib/meteorology` or
  `components/map/MapView` as live, kept or pending removal.
