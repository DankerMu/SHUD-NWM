# Proposal: station-provenance-actual-member

## Why

`workers/forcing_producer/file_store.py:843` 在 `_handoff_station_rows()` 内对**任意流域**的站点无条件 `properties.setdefault("source", "qhh.tsd.forc")`。#1176 迁移后新 package 只产出 canonical 成员 `shud/stations.tsd.forc`，该字面量对所有流域都成为**悬空标签**（不指向所属 package 内任何实存成员），对非 QHH 流域还叠加**身份错标**。该值经 `station_inventory` payload 落 `met.met_station.properties_json`，是 DB 持久化 + 对外 station API 可见字段，错误值随 ingest 持续增殖。（issue #1359；源自 PR #1354 final-review N1 / r1-C3 已知残余。）

## What Changes

- `_handoff_station_rows()` 的 `source` 兜底从"猜一个文件名常量"改为**写入时记录实际解析到的 station-index 成员 basename**：从已在作用域内的 `package_manifest["files"]` 取 `role == SHUD_FORCING_ROLE` 且 `relative_path ∈ SHUD_FORCING_INDEX_MEMBERS` 的条目，写其 basename（canonical package → `stations.tsd.forc`，legacy 重放 package → `qhh.tsd.forc`——此时名副其实）；解析不到则**不落 `source`**（缺席优于捏造）。`setdefault` 语义保留：站点已带 `source`（如 QHH bootstrap 真资产站点）时不覆盖。
- 新增六态成员解析单测 + 端到端 handoff `source` 断言（真 oracle 与双向红证见 tasks 1.2）；`tests/test_object_store_forcing.py:53` 输入 fixture 与黄金 payload fixture（`tests/fixtures/forcing_domain_handoff/**`，checksum 钉死）均**禁改**。
- 存量行**不回填**（裁定见 design D2）。
- live receipt：**生产写点在 node-22**（db-free compute 面）——node-22 部署后新产出 handoff 的 file-plane 主证据 + node-27 侧条件 DB/API 证据（口径见 design D3 / tasks 2.4）。

## Capability 影响

- `fixed-station-forcing-production`：ADDED — 站点 handoff provenance `source` 记录实测成员的纪律。

## 兼容性与非目标

- **零下游语义耦合**（explorer 全仓扫描 + fixture 评审独立复核）：无任何生产读者按 `properties_json.source` 的值分支/解析——`forcing_domain_handoff.py:683-684` 只查 Mapping 形状；`object_store_forcing.py:55` 只读 `forcing_filename`；met_station 的 API 读面 `GET /api/v1/met/stations`（`apps/api/routes/data_sources.py:91-92` → `forecast_store.list_met_stations` :968-976）纯透传 `properties_json`，schema `openapi_patching.py:1132` `additionalProperties: true`；前端仅 `types.ts` 泛型声明，无 UI 消费。仅测试字面量钉 2 处。never break userspace 满足。
- **不动** `workers/model_registry/qhh_production_bootstrap.py:1441/:1452`（QHH 真资产 lane，带 `source_file`/`source_sha256` 实证 provenance，#1176 Non-Goal 延续）。explorer 新观察——该 lane `_seed_station_rows` 的 `project_name` 已参数化（`:642-646`）而 `source` 仍硬编码，属同型缺陷的**另一写点**——按"报告不修"挂账新 issue，不进本 change。
- 不改 package 成员命名契约（`shud_forcing_contract.py` 是 #1176 终态）、不改插值/binding/气象值/流域范围、不重写历史 object-store package。

## 风险与不确定性

- 风险低（provenance 标签语义，P3）：主要失效模式是成员解析规则写错（例如把非 index 成员的 basename 当 source、或 manifest 形状差异导致缺席面扩大）。以 manifest 形状实测锚点 + 六态单测 + 端到端 handoff 断言 + node-22 部署后 file-plane receipt 覆盖。
- 部署面要点：**生产写点在 node-22**（db-free compute 面，design Context 运行平面项）；存量行两态判读只对 handoff-ingest 插入的非 mirror 行成立（direct-grid mirror 行存在第三态，见 design D2 适用域限定）；receipt 两级口径见 tasks 2.4。
