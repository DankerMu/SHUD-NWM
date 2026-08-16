# Design: station-provenance-actual-member

## Context（现状锚点，master @ 9ff9e563 实测行号）

- `workers/forcing_producer/file_store.py`：`_handoff_station_rows(self, *, record, package_manifest, rows, basin_id, basin_version_id, model_id)`（:804-813）；`source` 兜底 `properties.setdefault("source", "qhh.tsd.forc")`（:843），无 basin 分支；相邻键 `shud_forcing_index`/`forcing_filename` 来自 per-station `manifest_station`（:836-842，同为 setdefault）；行装配 `"properties_json": _json_safe(properties)`（:865）。调用方 `_write_forcing_domain_handoff`（:707-714），`package_manifest` 由 `_read_json_reference(package_manifest_uri)` 取得（:679-680）——**完整 manifest dict 已在作用域内**，但当前只消费 `station_order`（`_manifest_station_order` :1178-1190）。
- **manifest `files` 条目形状**（`producer.py:1991-2002`）：station-index 成员条目携带 `{"role": SHUD_FORCING_ROLE, "relative_path": "shud/stations.tsd.forc" | "shud/qhh.tsd.forc", "uri", "checksum", ...}`，role 选择在 :1993-1997。契约常量（`packages/common/shud_forcing_contract.py`）：`CANONICAL_SHUD_FORCING_INDEX_MEMBER`（:39）、`LEGACY_...`（:44）、`SHUD_FORCING_INDEX_MEMBERS`、`CANONICAL_/LEGACY_..._BASENAME`、`SHUD_FORCING_INDEX_BASENAMES`、`SHUD_FORCING_ROLE`（:69）。`file_store.py` 当前**未 import** 该契约模块（仅 `producer.py` 引用）。
- **ingest 侧 upsert 是 no-op 更新**（`packages/common/forcing_domain_handoff_apply.py:678-714`）：`ON CONFLICT (station_id) DO UPDATE SET station_id = met.met_station.station_id`——**存量行的 `properties_json` 永不被 station_inventory ingest 刷新**。推论：(a) 存量行的旧值不会自然洗出；(b) 部署后 DB 可观测的新值只出现在**新插入**的站点行上。
- **冲突谓词语义消费相邻键**：apply 侧 direct-grid 升级分支要求 `EXCLUDED.properties_json ? 'forcing_filename'`、`? 'shud_forcing_index'` 且与存量行逐值 `IS NOT DISTINCT FROM`（:703-710）；producer 镜像 upsert（`workers/forcing_producer/store.py:403-419`）同样按 `derived_cache`/`binding_checksum` 等键过滤。**`source` 键本身零消费，但同一 dict 内的邻居键是活契约——实现不得扰动它们的取值与 setdefault 次序。**
- **`source` 值的全仓消费面**（explorer 全量扫描）：写点 2 处（本 change 目标 :843 + QHH bootstrap 真资产 lane `qhh_production_bootstrap.py:1441/:1452`，后者带 `source_file`/`source_sha256` 实证 provenance，:1442-1443）；读者零个按值分支（`forcing_domain_handoff.py:683-684` 只查 Mapping 形状；API/前端纯透传）；测试出现该字面量 2 处（`tests/test_object_store_forcing.py:53` 为**输入 fixture 非断言**——现有全套测试对 `source` 行为零 oracle，见风险表；`tests/test_qhh_production_bootstrap.py:1557` 兄弟 lane 断言）。
- **兄弟 lane 新观察（越界，报告不修）**：`_seed_station_rows` 被 `project_name` 参数化的入口调用（:642-646→:654/:1053），同 dict 内 `forcing_source_identity` 已是 `f"{project_name}.tsd.forc:..."`（:1446）而 `source` 仍硬编码（:1441）——同型缺陷的第二写点，挂账独立 issue，不进本 change。
- **改动非空转的实证**：file-plane 站点属性由 `file_store.py:1051-1055` 构造，只含 `shud_forcing_index`/`forcing_filename`/`manifest_authority`，从不带 `source`——`setdefault` 必然生效，不会被上游已有值架空。
- **同 station_id 域的第二个 properties_json 写点（fixture 评审补录）**：direct-grid registration/producer mirror（`workers/model_registry/direct_grid_variant_registration.py:554-576`、`workers/forcing_producer/store.py:403-419`）在 `direct_grid_cache` 身份谓词下会整体 `SET properties_json = EXCLUDED`，其 payload 键集（`store.py:358-380`）**不含 `source`**。两平面写同一 station_id 的铁证即 apply 升级分支谓词（`forcing_domain_handoff_apply.py:700-711`）。
- **运行平面（fixture 评审补录，决定部署顺位）**：`_handoff_station_rows` 属 `FileForcingRepository`，仅 db-free 模式选中（`producer.py:479-483` + `file_store.py:1003-1007`，开关 `NHMS_FORCING_DB_FREE`/`NHMS_SCHEDULER_DB_FREE_REQUIRED`）；db-free 是 compute 面配置（`infra/compose.compute.yml:21`、`infra/env/compute.example:50`、`compute.scheduler-dbfree.env.example:22`）——**生产上该代码跑在 node-22**（纯计算面），产物经 NFS 供 27 ingest。
- **station API 真实读面**：`GET /api/v1/met/stations`（`apps/api/routes/data_sources.py:91-92` → `forecast_store.list_met_stations`，返回列含 `ms.properties_json`，`forecast_store.py:968-976`；schema `openapi_patching.py:1132`）。basin-only 分支带 `active_flag = true` 过滤（`forecast_store.py:931`），而 handoff 新插入行一律 `active_flag=False`（`file_store.py:864`、apply 模板字面量）；带 `model_id` 的 interp-weight join 分支（:913-927）不过滤 active_flag。

## Goals / Non-goals

- Goals：非 QHH 流域新写站点 `source` 不再含 `qhh` 字样；写入值对应所属 package 内实存 station-index 成员；解析不到时缺席而非捏造；QHH bootstrap lane 与相邻语义键零扰动。
- Non-goals：见 proposal 兼容性节（bootstrap lane、成员命名契约、插值/binding、历史 package 重写、存量行回填）。

## D1 — provenance 语义：写入时记录实际解析成员的 basename

**决策**（issue 三选一取推荐项）：新增模块内 helper `_station_index_member_basename(package_manifest) -> str | None`：

1. 取 `package_manifest.get("files")`，非 list 或缺失 → `None`。
2. 过滤条目：先 `isinstance(entry, Mapping)`（与本模块 `_manifest_station_order` :1183-1189 的既有条目守卫一致——非 Mapping 元素跳过，绝不让标签兜底升级为可中断生产写入的 `AttributeError`），再 `entry.get("role") == SHUD_FORCING_ROLE` **且** `entry.get("relative_path") in SHUD_FORCING_INDEX_MEMBERS`（双条件——role 单独不够窄，membership 单独防 role 漂移；两常量集合均来自 `shud_forcing_contract`，禁止手抄字面量。历史 legacy package 的 manifest 条目同样带 `role=shud_forcing` + `relative_path=shud/qhh.tsd.forc`——#1176 前的 role 三元即按该路径判定，legacy 分支可解析）。
3. 命中 0 条 → `None`；命中多条（理论上 canonical+legacy 并存的病态 package）→ 取 canonical 优先（`CANONICAL_SHUD_FORCING_INDEX_MEMBER` 在则用它，否则取第一条），并不视为错误——与既有 spec 对 non-direct-grid 成员解析"named-one-else-canonical"的语义同构（`openspec/specs/fixed-station-forcing-production/spec.md:140`），不与 direct-grid 双成员 fail-closed（:131-135）冲突（后者在 producer 准入层，先于本函数）。
4. 返回 `posixpath.basename(relative_path)`（canonical → `stations.tsd.forc`；legacy 重放 → `qhh.tsd.forc`，此时名副其实）。条目缺 `relative_path` 视同不命中（step 2 membership 条件天然覆盖）。

`:843` 改为：

```python
source_member = self._station_index_member_basename(package_manifest)  # 或模块级函数
if source_member is not None:
    properties.setdefault("source", source_member)
```

- **缺席优于捏造**：解析不到成员时不落 `source`（provenance 未知就如实缺席）。`station_inventory` 的 required 字段只到 `properties_json` 本身（`forcing_domain_handoff.py` PARSER_REQUIRED_PAYLOAD_ROW_FIELDS），`source` 子键缺席不违反任何 ingest 契约（探针只查 Mapping 形状）。
- **`setdefault` 语义保留**：站点已带 `source`（QHH bootstrap 真资产站点、任何上游已有值）时不覆盖。
- **零参数穿线**：`package_manifest` 已是 `_handoff_station_rows` 入参；新增一个 import（`shud_forcing_contract`）+ 一个纯函数，别的都不动。相邻键 `shud_forcing_index`/`forcing_filename` 的 setdefault（:836-842）保持原位原序。

**否决的备选**：
- 直写常量 `CANONICAL_SHUD_FORCING_INDEX_BASENAME`：立刻消灭 `qhh` 误标，但 legacy 重放 package 的 handoff 会声称 canonical 而实为 legacy——把"悬空"换成"小概率错标"，仍是不看输入的常量；与实测解析的实现成本差一个 10 行纯函数，不值得省。
- 改写为角色名（`shud_forcing_index`）：变更字段含义；该名字已是 properties_json 里另一个**被 ingest 谓词消费的键**（Context），复用同名字符串做值会制造混淆；且丢失 basename 语义对排障（去 package 里找文件）净损。
- 覆盖而非 setdefault：会踩掉 bootstrap 真资产 lane 的正确值，违反 issue Out-of-scope。

## D2 — 存量行：不回填（裁定）

**决策**：不回填 `met.met_station` 存量行的 `properties_json.source`。

- **事实基础**：(a) 零消费方按值分支（Context），错误值无行为危害，纯标签失真；(b) station_inventory ingest upsert 是 no-op（Context），**存量行不会被 handoff ingest 自愈或污染回退**。
- **两态判读的适用域限定（评审补录）**：station_inventory ingest 空转 ≠ 全局不刷新——direct-grid mirror 写点（Context 第二写点）在 `direct_grid_cache` 身份谓词下会整体覆盖 `properties_json` 且 payload 不带 `source`，即 `dg-*::cell:*` mirror 行存在"`source` 被覆盖抹除"的第三态。故两态判读规则（部署前旧行=legacy 常量标签、部署后新插入行=实测成员或缺席）**只对由 handoff ingest 插入的非 direct-grid-mirror 行成立**；direct-grid mirror 行的 `properties_json` 作者是 registration/producer mirror，本 change 不覆盖也不承诺其 `source` 状态。
- **回填被否决的理由**：为零消费方的字段跑一次 node-27 生产 DB UPDATE（含 receipt/回滚义务），收益是美观、成本是实机变更面——YAGNI。若未来出现按 `source` 消费的真实需求，#1339 的 `node27_river_identity_backfill.py` 模式（dry-run 默认、receipt、有界批次）可直接复用且 `met.met_station` 量级小得多（无压缩规避机械）。该路由记录于此，不建预防性工具。

## D3 — live receipt 口径（两级；生产写点在 node-22）

**部署前提**（评审 P1-2 修正）：改动代码跑在 **node-22**（db-free compute 面，Context 运行平面项）。receipt 前必须完成 node-22 `/scratch/frd_muziyao/NWM` ff-only pull，主证据必须晚于该部署时刻——单独 pull node-27 不构成部署。

1. **file-plane（主证据，必产出）**：node-22 部署后**新产出**的非 QHH 流域 handoff 文件（`runs/<run_id>/input/forcing_domain_handoff.json` 引用的 `payloads/station_inventory.json`，NFS 上 node-22/27 同一份），断言样本行：canonical package 下 `properties_json.source == "stations.tsd.forc"`；若样本恰为 legacy 重放 package 则 `== "qhh.tsd.forc"` 并在 receipt 注明 package 成员实测（此时名副其实，不算失败）。receipt 记录 node-22 `git rev-parse HEAD`、package/产物 mtime（证明晚于部署）、basin、样本 station_id。node-27 侧对最新 package 的只读复算仅作**辅助**交叉验证，不得替代主证据。
2. **DB/API-plane（条件证据，如实记录）**：若部署后窗口内出现**由 handoff ingest 新插入**的站点行（排除 `dg-*::cell:*` direct-grid mirror 行——其 properties 作者是 mirror 写点，见 D2），以 `GET /api/v1/met/stations?model_id=<model>&search=<station_id>`（**必须带 `model_id`**：新行 `active_flag=false`，basin-only 分支会过滤掉）实机查询，记录命令、basin、station_id、`station_role`、原始响应片段；若窗口内无新插入，如实记录"存量行按 D2 保留 legacy 标签"并附一条存量行样本对照，不伪造 DB 面证据。

AC 措辞"非 QHH 流域的新写入站点"以上述两级口径落地；receipt 不得含凭据。**实机前置核验**：receipt 执行时先确认 node-22 forcing producer 确以 db-free 模式运行（env/service 只读检查）；若发现生产已不在 22 跑 file-plane，立即停下按偏离上报（影响面需重新论证），不得继续照本宣科出证。

## 风险与验证映射

| 风险 | 缓解/验证 |
|---|---|
| 成员解析规则错（role/membership 过滤宽窄失当、多成员取舍错、非 Mapping 条目炸 handoff） | 六态单测（真实直调模式，模式同 `test_direct_grid_variant_registration.py:1958` 的 `object.__new__(FileForcingRepository)`）：canonical manifest → `stations.tsd.forc`；legacy-only → `qhh.tsd.forc`；无 index 成员/files 缺失或非 list → 缺席；`files` 含非 Mapping 元素 → 缺席且 handoff 正常完成；已有 `source` → 不覆盖；病态双成员 → canonical 优先。fixture 用 producer 形状（`role`+`relative_path`），并记录"无 `relative_path` 条目按缺席处理"为有意语义 |
| 扰动相邻语义键（`shud_forcing_index`/`forcing_filename` 次序或取值） | 既有 handoff 测试断言 + diff 审查（改动限 :843 一处 + helper + import） |
| **改后行为零 oracle**（评审 P1-1：`test_object_store_forcing.py:53` 是输入 fixture 非断言，现有全套测试不覆盖 `source` 行为；真实 handoff 测试的 manifest 均无 `files` 键） | 真 oracle 落在端到端 handoff 测试：`tests/test_forcing_producer.py:804-826` 的 package manifest 补 producer 形状 `files` 条目，在 :905 附近断言 `parsed["parsed"]["met.met_station"][0]["properties_json"]["source"] == "stations.tsd.forc"`；红证：实现回退旧字面量 → 该断言红（实现前红/实现后绿双向贴输出）。`test_object_store_forcing.py` 留在 AC 命令清单但其 :53 fixture **不改写**（语义=已带 source 的历史站点输入）。**黄金 payload fixture（`tests/fixtures/forcing_domain_handoff/**`）checksum 钉死，禁止"顺手同步"**——动了 apply/contract 测试反而红 |
| 兄弟 lane 回归 | `tests/test_qhh_production_bootstrap.py` 相关断言不动且必须保持绿（QHH 值不变） |
| 生产 manifest 形状与单测 fixture 漂移 | receipt file-plane 一级用 node-22 部署后真实产物（D3.1），node-27 复算仅辅助 |
| 部署面错配（只 pull 27 → 生产 handoff 继续写旧值而 receipt 假绿） | D3 部署前提：node-22 ff-only pull + 产物晚于部署的 mtime 证明 + node-22 HEAD 记录；实机前置核验 db-free 运行模式 |

## 验证入口（Phase 2 / Evidence Floor 消费）

- 本地：`uv run pytest -q tests/test_object_store_forcing.py tests/test_qhh_production_bootstrap.py tests/test_forcing_producer.py tests/test_direct_grid_variant_registration.py` + 新增/改动测试文件；`uv run ruff check .`；`openspec validate station-provenance-actual-member --strict --no-interactive`。
- node-22（生产写点部署面）：ff-only pull 为 receipt 前提；db-free 运行模式只读核验（D3）。
- node-27（数据/API oracle）：D3 两级 receipt 的采集与 DB/API 面查询。
