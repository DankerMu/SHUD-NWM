## Why

PR [#602](https://github.com/DankerMu/SHUD-NWM/pull/602) reviewer-1 S1 + reviewer-2 S2 carried two cleanup items (issue [#604](https://github.com/DankerMu/SHUD-NWM/issues/604)):

1. `docs/spec/04_api_design.md:94-104` (dated 2026-05-06, 8 weeks stale) lists `/api/v1/tiles/hydro/{run_id}/...` as a public hydro tile endpoint but doesn't surface the new canonical `/api/v1/tiles/hydro-national/...` route, and doesn't mark `/hydro/{run_id}/...` as direct-deeplink-only. Spec source-of-truth (`openspec/specs/mvt-tile-contract/spec.md`) is already correct; this is doc reconciliation.
2. After PR #602, `_layer_source_refs` ([services/tiles/mvt.py:964](services/tiles/mvt.py:964)) is unreachable for `layer_id == "discharge"` because [services/tiles/mvt.py:863-873](services/tiles/mvt.py:863) short-circuits to `source_refs={}` whenever `national_discharge=True` (now unconditionally true for discharge). The current function still has a `layer_id != "river-network"` branch that *would* return `run_id` for discharge if called — silently inviting a regression where a future refactor wires discharge back through this path and reintroduces run_id leakage into the ETag input.

## What Changes

- **docs**: `docs/spec/04_api_design.md` — add hydro-national endpoint to the public tile list; mark `/api/v1/tiles/hydro/{run_id}/...` as "direct-deeplink only, not surfaced via `/api/v1/layers` discharge entry".
- **invariant guard**: `services/tiles/mvt.py:_layer_source_refs` — add entry-line `assert layer_id != "discharge"` with a one-line message naming the canonical short-circuit ([services/tiles/mvt.py:863-873](services/tiles/mvt.py:863)). This makes the invariant testable and fails loudly if a future change wires discharge back through this path.
- **regression test**: add unit test asserting `_layer_source_refs` raises `AssertionError` when called with `layer_id == "discharge"` (locks the invariant).
- No behavior change for any caller currently in the code base. `mvt-tile-contract` spec already documents the canonical disposition; this change only enforces it at the function-boundary level.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `mvt-tile-contract`: 新增 *Discharge layer never reaches `_layer_source_refs`* scenario under the existing *MVT tile API contract* requirement —— 把 PR #602 引入的 "discharge always national → source_refs={}" 不变量在函数边界层加 assertion 闭环。

## Impact

- **代码**：`services/tiles/mvt.py` 一行 assert + ≤2 行 message；`docs/spec/04_api_design.md` ~3-5 行更新；`tests/test_tile_publisher.py` 或邻近测试旁 ≤10 行新增测试。
- **API 契约**：无（纯 invariant guard + 文档同步）。
- **OpenAPI**：无变化。
- **CI**：路径 scope 命中 `services/**` → `unit-test-targeted` 跑（fast path）；`docs/**` → `markdown-lint` 跑。
- **Receipts**：本变更不需 node-27 live receipt（pure refactor，无 deploy-affecting 行为变更）。

## Archive ordering

This change MUST be archived AFTER `fix-discharge-tile-always-national` (PR #602 source change). The MODIFIED `mvt-tile-contract` Requirement body in this change assumes the post-#602 canonical baseline (i.e. it includes the *Discharge canonical URL is national across all callers* scenario verbatim from PR #602). If this change is archived first, canonical will absorb PR #602's scenarios under the wrong attribution AND subsequent archive of `fix-discharge-tile-always-national` may detect text drift.

Correct sequence:
1. `openspec archive fix-discharge-tile-always-national --yes` (apply PR #602's spec delta to canonical)
2. `openspec archive cleanup-docs-and-dead-source-refs --yes` (apply this change's new *Discharge layer never reaches `_layer_source_refs`* scenario on top)
