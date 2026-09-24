## Risk packs

- Concurrency / shared state / ordering — **selected**：父表锁序 → 真实 DB 交错测试（2.3）+ 静态扫描（2.2）。
- Public API / CLI / script entry — **selected**：`GET /api/v1/basins`、`/basins/{id}/versions` 响应集合变化 → 3.2 测试 + 公网 receipt（6.3）。
- Error handling / rollback / partial outputs — **selected**：一次性脚本单事务 + 计数断言 + 备份/回滚 → 4.x/5.x disposable DB 测试。
- Schema / columns / units / field names — **selected**：`properties_json` 两键精确改写，其余键字节不变 → 5.2 测试。
- Auth / permissions / secrets — not selected：读侧过滤不改鉴权；脚本以 owner 角色人工执行（D3），不入 CI。
- File IO / path safety — not selected：脚本备份写 node-27 `/home/nwm/tmp`（人工步骤）。
- Release / operational — **selected**：生产 display 部署 + 活库写入 → 6.2/6.3，失败回滚到上一 SHA / 回滚脚本。
- Config / Resource / Legacy compatibility — not selected：无配置、依赖、既有消费者契约变更（前端用 `has_display_product=true` 且 evidence 行本就不在其结果中）。
- Documentation / migration notes — selected：`evidence/restore/README.md`、`_lock_river_network_version` docstring、receipt 文档。

## 1. Baselines

- [x] 1.1 node-27 只读：`core.basin` 行数、evidence 依赖计数、#1480 分组计数、公网 `/api/v1/basins` 与 versions 响应；live `pg_constraint` FK dependents、跨 schema 列名扫描、`pg_trigger`（落 `.workplans/`）

## 2. #2491

- [x] 2.1 `_lock_basin_version` + 在 `import_basin_into_registry_core` 最前调用；docstring 锁序不变量
- [x] 2.2 静态扫描测试（bv 锁先于其它写 helper）+ 突变红证
- [x] 2.3 真实 DB 双会话交错测试（修复前红 / 修复后绿、有界）
- [x] 2.4 `_lock_basin_version_scope` 同类确认结论写入 PR body

## 3. #1729 读侧

- [x] 3.1 `list_basins` / `list_basin_versions` 过滤；常量
- [x] 3.2 单测 + 真实 DB 用例；其它公开 basin 读路径的逐项处置说明

## 4. #1729 删行脚本

- [x] 4.1 backup / delete / rollback 脚本（计数断言、单事务）
- [x] 4.2 disposable DB 测试：删除成功、计数不符零删除、rollback 还原

## 5. #1480 回填脚本

- [x] 5.1 backfill / rollback 脚本（谓词推导、两键、幂等）
- [x] 5.2 disposable DB 测试：改写正确、其余键不变、二次 0 行、rollback 还原

## 6. 执行与收口

- [x] 6.1 node-27 全量 pytest（disposable DB，frozen SHA）
- [x] 6.2 合并后 node-27 display 部署 + 公网 curl 前后 receipt（`/api/v1/basins` 不含 evidence basin；versions 404；集合 ⊆ node-22 `manifest-last.json` basin 集合或差异有 issue 归属）
- [x] 6.3 **暂停等用户确认** → 执行 4.x / 5.x，receipt（前后计数、二次执行 0 行）；`evidence/restore/README.md` 更新；fixture 同步（处置按用户裁定）——随 post-merge archive PR 提交
- [x] 6.4 ruff、openspec strict、guard hook

## Evidence Floor

1. 交错测试：修复前 `40P01`（红，附输出），修复后两会话均成功、B 等待 A、无挂起。
2. 静态扫描：bv 锁调用位于 `import_basin_into_registry_core` 中所有其它写 helper 之前；移动它 → 红。
3. #2490 的 spy 计数测试（`tests/test_basins_registry_import.py`）保持绿。
4. `list_basins` 两路均排除 evidence-only、保留 NULL；分页正确；versions 对 evidence-only 返回 404；真实 DB 用例通过。
5. 删除脚本：disposable DB 上成功/失败/回滚三路径通过；node-27 执行 receipt（前后计数、备份位置）。
6. 回填脚本：disposable DB 上改写/其余键不变/幂等/回滚通过；node-27 receipt 1709→0 / 0→1709 / 386 不变，二次执行 0 行。
7. 公网 receipt：部署前后 `/api/v1/basins` 与 versions 对比。
8. node-27 全量 pytest 与 master 同环境失败集合一致；ruff、openspec strict 绿。
