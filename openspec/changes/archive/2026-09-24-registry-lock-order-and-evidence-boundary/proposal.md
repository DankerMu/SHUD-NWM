## Why

三个 model-registry 数据 / 锁序正确性问题（master `fd392615a`；#2617 已把 `packages/common/model_registry.py` 拆开，#2490 已拆 `qhh_production_bootstrap.py` / `basins_registry_import.py`，本批可合规提交）：

- **#2491**：`bootstrap-qhh-production` 取 `basin_version → river_network_version`（`qhh_bootstrap_registry.py::_lock_qhh_basin_scope` 的 `FOR UPDATE` 在先），通用 import 取 `river_network_version → basin_version`（`basins_registry_import.py::_refresh_parent_version_materialization` 先 `UPDATE core.river_network_version` 再 `UPDATE core.basin_version`）→ 同一已存在 basin 并发时 ABBA 死锁（`40P01`）。
- **#1729**：合成夹具 `basin__evidence_cmfd_p02_synth`（`basin_group='evidence-only'`）常驻 node-27 生产 `core.basin`，由公网 `GET /api/v1/basins` 与 `/basins/{id}/versions` 无条件返回（读侧无过滤；`core.basin` 无 `active_flag`）。
- **#1480**：node-27 `met.met_station` 1709 行 seed 行 provenance 自相矛盾（`project_name=heihe` 但 `source` / `elevation_metadata.source` = `qhh.tsd.forc`）；#1415 只修前向，不自愈。

## What Changes

- **#2491**：在 `import_basin_into_registry_core` 写任何父行之前，对 `core.basin_version` 取 `FOR NO KEY UPDATE`（唯一收口点，bootstrap 与通用 import 都委托给它；bootstrap 已持 `FOR UPDATE`，同事务再取为 no-op）。锁序不变量扩为 `basin_version → river_network_version → river_segment`，文档 + 静态扫描钉住 + 真实 DB 双会话交错测试。确认 `model_registry_catalog.py::_lock_basin_version_scope` 生命周期事务不锁 rnv（只记录结论）。
- **#1729**：读侧——`list_basins` 在 SQL 层排除 `basin_group = 'evidence-only'`（默认与 `has_display_product=true` 两路）；`list_basin_versions` 对 evidence-only basin 返回 404（`MissingResourceError`）。防线测试。数据侧（用户裁定：**删行**）——一次性脚本按 FK 顺序删除该 basin 及其依赖行，先导出备份、附回滚脚本；**执行前暂停等用户确认**。执行后更新 `openspec/changes/direct-grid-display-cutover/evidence/restore/README.md` 的保留声明。合并后部署 node-27 display API（用户已授权）并取公网前后 receipt。
- **#1480**（用户裁定：**回填**）——幂等定向 `UPDATE` 脚本（谓词按同行 `project_name` 推导，只 SET `source` 与 `elevation_metadata.source`）+ 回滚脚本 + 真实 DB 测试；**执行前暂停等用户确认**；执行后前后分组计数 receipt、二次执行 0 行；fixture `tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json` 同步（该 JSON 2074 行、未豁免，处置届时由用户裁定）。

## Impact

- Affected specs: `basins-registry-import`（ADDED：父表锁序）、`multibasin-product-discovery`（ADDED：evidence-only 排除）。
- Affected code: `workers/model_registry/basins_registry_import.py`、`packages/common/model_registry_catalog.py`、`apps/api/routes/models.py`（如需）、新增 `scripts/ops/` 一次性脚本、新测试文件。`qhh_production_bootstrap.py` / `station_set_flip.py` 预期不改（只读确认）。
- Out of scope：`_lock_qhh_basin_scope` 改 `FOR NO KEY UPDATE`（issue 列为可选加固，不做，锁序对齐已消除环）；写侧 CHECK 约束（需 migration，归 batch M 范围外；读侧过滤 + 测试即满足 #1729「自动防线之一」）；#1359 lane 既有数据；#2612。

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: concurrency/lock order, public API response, production data write)
Blast radius: 锁序错误 → 新死锁或挂起；过滤错误 → 真实流域从公网消失或分页错位；删除/回填错误 → 生产数据损坏
Selected risk packs: Concurrency / shared state / ordering; Public API; Error handling / rollback / partial outputs; Schema / field names (properties_json 键)
Evidence floor: 见 tasks.md
```
