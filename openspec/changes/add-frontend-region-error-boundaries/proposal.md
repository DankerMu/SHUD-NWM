## Why

- #2347, split from ruling C of #2129 (owner, 2026-09-14): the frontend has no error boundary in production code.
  - Any render-time throw unmounts the whole React tree, and the user sees a blank page with no error state.
  - Possible causes include backend shape drift, a derivation bug, or a lazy page chunk that fails to load.
  - Known live case: a `null` element in `/api/v1/layers/discharge/cycles` makes `deriveM11ControlBarModel` throw a `TypeError` (`M11BottomControlBar.tsx` `.map(entry => …entry.cycle_time)`).
- Boundaries are orthogonal to shape validation (#2129 B, next). They do not reduce malformed data; they shrink the blast radius of whatever still throws.

## What Changes

- One reusable `RegionErrorBoundary` (class component):
  - fallback text 「此区域加载失败」 with a 「重试」 button;
  - resets when its `resetKeys` change;
  - logs region name and error from `componentDidCatch`.
- Route-level boundary inside `AppShell`, wrapping `Suspense` → `AppRoutes`, via an exported router-agnostic `AppFrame` that `App` renders inside `BrowserRouter`:
  - fallback 「页面加载失败」 with 「重试」 and 「刷新页面」;
  - resets on location change;
  - the site header stays mounted.
- Overview page (`/`) region boundaries inside `M11FullscreenMap`:
  - map surface;
  - floating controls (layer switcher, basemap switcher, ops link);
  - forecast panels: the river forecast panel and the station popup only, wrapped in `OverviewMode`; the floating notice chain stays outside;
  - bottom control bar;
  - legend.
- The control bar model is derived inside its region: `M11FullscreenMap` receives the control-bar **input** and a small child derives the model, so a derivation throw stays in the control-bar region.
- `/ops` and `/monitoring` (`MonitoringPage`) region boundaries: `SummaryBar`, `StageList`, `JobsTable`, `TrendPanel`.

## Non-goals

- Shape validation or malformed-data degradation (#2129 B).
- Backend `response_model` (#2348).
- Localizing other page-body derivations (precipitation overlay resolution, layer states); the route boundary covers them.
- Errors thrown outside render (event handlers, effects, MapLibre callbacks, promise rejections). React boundaries do not catch these, and their existing handling is unchanged.
- Region split of `ModelAssetsPage` (route level only).
- Visual redesign; the fallback reuses existing glass-panel / card styling.

## Impact

- Frontend only:
  - `apps/frontend/src/components/layout/` (new boundary);
  - `App.tsx`, `pages/OverviewPage.tsx`, `pages/MonitoringPage.tsx`;
  - new tests.
- Specs: one ADDED requirement in the new capability `frontend-render-failure-containment`.
- node-27: frontend-only deploy plus a browser receipt with client-side response injection (owner authorization 2026-09-14, same method as #2336).
