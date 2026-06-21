## 1. Invariant guard in `_layer_source_refs`

- [ ] 1.1 在 `services/tiles/mvt.py` `_layer_source_refs` 函数入口加 `assert layer_id != "discharge", "discharge layer must use national source_refs={} via layer_metadata; _layer_source_refs is unreachable for discharge per PR #602 spec invariant (mvt-tile-contract: Discharge canonical URL is national across all callers)"`。
- [ ] 1.2 不动函数体其他逻辑（`layer_id != "river-network"` 三元运算符对 flood/warning 仍正确）。

## 2. Regression test

- [ ] 2.1 `grep -l "_layer_source_refs" tests/` 决定测试 home。若已有，邻近加；若无，放 `tests/test_flood_alerts_api.py` 或 `tests/test_tile_publisher.py`（看哪个 import `services.tiles.mvt`）。
- [ ] 2.2 新增 `test_layer_source_refs_rejects_discharge`：直接 import `_layer_source_refs`，用 `pytest.raises(AssertionError)` 包住一次 `layer_id="discharge"` 调用，断言 message 含 "discharge"。**覆盖** spec scenario *Discharge layer never reaches `_layer_source_refs`*。

## 3. Docs reconciliation

- [ ] 3.1 `docs/spec/04_api_design.md:94-104` 把 hydro tile 行块更新：
  - 加 `GET /api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf`（canonical discharge）作为主条目
  - 把现有 `GET /api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` 行加注 `(direct-deeplink only; not surfaced via /api/v1/layers discharge entry — see openspec/specs/mvt-tile-contract/spec.md)`
- [ ] 3.2 保持原文 voice / 编号 / 上下文结构不变；只动 hydro tile 列出方式。

## 4. Verify locally

- [ ] 4.1 `uv run pytest -q tests/test_flood_alerts_api.py tests/test_tile_publisher.py -k "layer_source_refs or layers_catalog or layer_metadata"` PASS
- [ ] 4.2 `uv run ruff check services/tiles/mvt.py tests/` clean
- [ ] 4.3 `openspec validate cleanup-docs-and-dead-source-refs --strict --no-interactive` PASS

## 5. PR / merge hygiene

- [ ] 5.1 PR body `Closes #604`，附 Chinese 工作总结
- [ ] 5.2 review-loop log append 一行
- [ ] 5.3 OpenSpec archive（**严格顺序**）：合并后先确保 `openspec archive fix-discharge-tile-always-national --yes` 已跑（PR #602 的 change，应先归档以让 canonical mvt-tile-contract 包含 *Discharge canonical URL is national across all callers* scenario），再 `openspec archive cleanup-docs-and-dead-source-refs --yes`。详见 proposal.md *Archive ordering* 段。
