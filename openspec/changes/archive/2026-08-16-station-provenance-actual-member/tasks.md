# Tasks: station-provenance-actual-member

## 1. 实现（implementer）

- [x] 1.1 `workers/forcing_producer/file_store.py`：新增 `_station_index_member_basename(package_manifest)`（D1 规则全文为准：files 列表 → 条目 Mapping 守卫 → role+membership 双过滤 → 多命中 canonical 优先 → basename；常量一律 import 自 `packages/common/shud_forcing_contract`，禁止手抄字面量）；`:843` 的 `setdefault("source", "qhh.tsd.forc")` 改为解析成功才 `setdefault("source", <basename>)`、解析失败不落 `source`。相邻键 `shud_forcing_index`/`forcing_filename` 的 setdefault（:836-842）位置与语义零扰动。
- [x] 1.2 测试（评审 P1-1 修正后的真 oracle）：
  - **端到端 handoff oracle（主红证）**：`tests/test_forcing_producer.py:804-826` 的 package manifest 补 producer 形状 `files` 条目（`{"role": "shud_forcing", "relative_path": "shud/stations.tsd.forc", ...}`，字面量 import 契约常量），在 :905 附近断言解析出的 `met.met_station` 行 `properties_json["source"] == "stations.tsd.forc"`。红证双向：断言先于实现加入 → 红；实现落地 → 绿（两次 pytest 输出入偏离记录/PR）。
  - **六态单测**（直调模式，同 `tests/test_direct_grid_variant_registration.py:1958` 的 `object.__new__(FileForcingRepository)`）：canonical → `stations.tsd.forc`；legacy-only → `qhh.tsd.forc`；无 index 成员 / `files` 缺失或非 list → 缺席；`files` 含非 Mapping 元素 → 缺席且 handoff 正常完成（不抛）；已有 `source` → 不覆盖；双成员 → canonical 优先。fixture 用 `role`+`relative_path` 形状。
  - **禁改面**：`tests/test_object_store_forcing.py:53` 输入 fixture **不改写**（语义=已带 source 的历史站点；该文件留在 2.1 命令清单仅作 AC 指定回归）；`tests/fixtures/forcing_domain_handoff/**` 黄金 payload checksum 钉死，**禁止顺手同步**。
  - `tests/test_qhh_production_bootstrap.py`：兄弟 lane 断言（`:1557` 等）不改动且保持绿。
- [x] 1.3 偏离记录：实现与本 fixture 任何出入逐条报告（无偏离须显式声明）。

## 2. 验证（orchestrator）

- [x] 2.1 本地：`uv run pytest -q tests/test_object_store_forcing.py tests/test_qhh_production_bootstrap.py tests/test_forcing_producer.py tests/test_direct_grid_variant_registration.py <新增/改动测试文件>` 全绿。
- [x] 2.2 本地：`uv run ruff check .` 通过。
- [x] 2.3 本地：`openspec validate station-provenance-actual-member --strict --no-interactive` 通过。
- [x] 2.4 live receipt（D3 两级口径；生产写点在 node-22）：
  - 前置：node-22 ff-only pull（记录 `git rev-parse HEAD`）+ db-free 运行模式只读核验（发现生产不在 22 跑 file-plane 则停下按偏离上报）。
  - file-plane（必产出）：node-22 部署后**新产出**的非 QHH 流域 handoff `station_inventory.json` 样本行——canonical package 下 `source == "stations.tsd.forc"`；legacy 重放样本则 `== "qhh.tsd.forc"` 并注明成员实测。记录 package/产物 mtime（晚于部署）、basin、样本 station_id。node-27 复算仅辅助。
  - DB/API-plane（条件证据）：窗口内有 handoff ingest 新插入行（排除 `dg-*::cell:*` mirror 行）则 `GET /api/v1/met/stations?model_id=<model>&search=<station_id>`（必须带 `model_id`，新行 `active_flag=false`）实机查询，记录命令、basin、station_id、`station_role`、原始响应片段；无则如实记录"存量行按 D2 保留 legacy 标签"+ 存量样本对照。
- [x] 2.5 部署顺位：merge 后 node-22 与 node-27 均 ff-only pull（本 change 无迁移/DDL；node-22 是生产写点，单独 pull 27 不构成部署——见 design D3 部署前提）。

## 3. 交付（orchestrator）

- [x] 3.1 PR（含偏离记录节、语义决策与不回填裁定引用、证据包、中文工作总结）→ cross-review → merge gate。
- [x] 3.2 兄弟 lane 同型缺陷（`qhh_production_bootstrap._seed_station_rows` 的 `project_name` 参数化 vs `source` 硬编码）挂账独立 issue（issue-scribe）。

## Evidence Floor（对应 issue #1359 验收标准）

| Issue AC | 证据 | 任务 |
|---|---|---|
| 记录选定 provenance 语义与存量行处置 | design D1（实测成员 basename）+ D2（不回填裁定，含 no-op upsert + 第二写点适用域限定） | fixture 本身 |
| 非 QHH 新写站点 `source` 无 `qhh` 字样 | 端到端 handoff oracle（双向红证）+ 六态单测 + node-22 部署后 file-plane receipt | 1.2 / 2.4 |
| `source` 对应 package 实存成员（或缺席） | D1 解析规则单测 + receipt 用 node-22 真实产物 | 1.2 / 2.4 |
| bootstrap lane `qhh.tsd.forc` 不变 | `tests/test_qhh_production_bootstrap.py` 保持绿 | 1.2 / 2.1 |
| 指定 pytest + ruff 通过 | 本地输出 | 2.1 / 2.2 |
| live receipt（命令/basin/站点/响应片段） | D3 两级 receipt（file-plane 主证据 + 条件 API 面） | 2.4 |
