# Design: ci-selector-mvt-importer-closure

## 风险三元组

- **Fixture level**：compact——纯本地 Python selector + 其 meta 测试，无 DB / 无远端
  面；风险在"选错/漏选"与"PR lane 时长"。
- **Must-preserve**：(a) 既有两条闭包（`display_coverage`、`real_backend`）断言仍绿；
  (b) 原 5 个套件仍被选中；(c) 无 core-smoke 兜底泄漏（`fallback_only_tests & selected`
  为空）；(d) 门控 importer 不被拉进 PR lane；(e) 其它规则的选择集不变（selector 全套
  meta 测试绿）。
- **Seams under test**：`select_tests()` 纯函数；`GUARDED_MODULE_CLOSURES` 驱动的推导守卫；
  等值 pin。
- **Risk packs**：selected = test-oracle-integrity（pin 更新不得弱化）、ci-lane-cost
  （墙钟记录）；not selected = db/migration、frontend、slurm。

## D1: 规则补边的来源是守卫推导，不是手挑

required = `_non_gated_top_level_importer_tests("services.tiles.mvt")`
∪ `_one_hop_importer_tests("services.tiles.mvt")`，在 master `f664a21e` 上推导为 7 个
缺失套件（proposal 列表）。把 mvt 纳入 `GUARDED_MODULE_CLOSURES` 后，这个集合由树推导、
不冻结：新 importer 出现 → 守卫红 → 指向规则。等值 pin 仍保留（它钉的是"今天的集合 +
无兜底泄漏"，与守卫互补：守卫防漏、pin 防多）。

## D2: one-hop 三个 cutover 套件一并纳入

#1455 口径：file-level import 了被守卫模块的模块（`hydro_display.py`）把行为带进自己的
importer 套件。issue 只算到 2 个，`test_direct_grid_display_cutover_history.py` 在立案基线 `c2439f62`
上就已存在（`73806841`，2026-07-11，顶层 import `hydro_display`、无 file-level marker）
——手工枚举漏计，正是树推导闭包相对人肉清单的价值所在。代价 +49 tests / <1 s。

## D3: anti-vacuity 次序

先改 `GUARDED_MODULE_CLOSURES`（加 mvt）→ 跑守卫 → 必须红且 missing 列表 = 7 个 →
再补规则 → 绿。红证进 PR body。

## 残余风险

- `GUARDED_MODULE_CLOSURES` 仍是白名单（3 条）；全树化另立项。
- mvt-only lane 墙钟 +~30 s（实测 7 套件 521 passed / 29.2 s），可接受。
