## Why

#2621：公网默认 `GET /api/v1/basins`（不带 `has_display_product`）返回 64 个流域，`has_display_product=true` 返回 48 个，node-22 `manifest-last.json` 也是 48 个，后两者完全相等。多出来的 16 个流域没有任何 active `model_instance`，这一点双向成立（node-27 只读实测，2026-09-29）。

问题在于口径不一致：#1729 第 3 条验收写的是「默认参数 ⊆ manifest」，而 `operating-scope.md:125-126` 写的是「裸 `/basins` 是原始目录，含退役流域属预期」，两者矛盾。同时，这 16 项里有 7 个同日改名残影、5 个退出调度却没有 runbook 记录。

owner 裁决（2026-09-29）：
1. **A**：保留原始目录语义，API 不改；用可执行判据统一口径。
2. 7 个改名残影**本批删除**。
3. 5 个退出调度的流域按退役格式**补登**。

#2644：`model_id` 有两种身份，文档没有区分。一种是入库时基础包的 id `<basin>_shud`，也是 river_segment_id 前缀的来源；另一种是 direct-grid variant 的 `dg_<32hex>`。

## What Changes

- **判据与巡检**：新增只读脚本 `scripts/basin_catalog_manifest_audit.py`，读取 DB 与 manifest，断言两件事：
  - `has_display_product=true` 集合 == manifest basin 集合；
  - 默认目录 − manifest 的每一项都没有 active `model_instance`。

  集合直接复用 `list_basins` 的生产实现计算，不另写 SQL，避免口径漂移。违反时 exit 1，并输出 JSON receipt。配套单测，以及 node-27 真实 DB 集成测试。
- **删除脚本**：沿用 #1729 已有的 oneshot 模式（`scripts/ops/node27_oneshot_sql.py`），新增 `scripts/ops/node27_2621_delete_rename_leftovers.sql` 和对应的 `_rollback.sql`。
  - 执行顺序：钉死 FK 清单和 ID 集合 → `FOR UPDATE` 锁定 → 用数组写法证明三张 hypertable 零引用 → fsync 备份 → 按顺序删除 → 孤儿检查。
  - 删 `river_segment` 和 `met_station` 这两步使用 `session_replication_role = replica`，范围只覆盖这两条语句；权限不足时直接失败，不退回慢路径。
  - 默认 dry-run 回滚；只有 `--apply` 才提交。
  - 配套 disposable-DB 往返测试。
- **生产执行**（node-27）：先跑 audit 和删除 dry-run，receipt 落盘；**停下来请 owner 确认**；确认后 `--apply`，再跑 audit，并留下公网三方差分的前后 receipt。
- **文档**：
  - `operating-scope.md` §7：统一默认 `/basins` 口径并引用判据；把「退役行不删」与「改名残影可按门禁删除」区分开；补登 2026-08-25 `neiliuqu` 退役、2026-09-22 onboarding-19（SHJ 三个子流域并入 `basins_shj`，`xinanjiang_upstream` 退出），并记录本次残影删除。
  - `service-bringup.md:650-690` 的 baseline 清单标为历史快照。
  - #1729（已关闭）下补一条评论说明口径统一。
- **#2644**：`openspec/glossary.md` 新增 `model_id` 消歧词条；两条 reach/output row 词条中的 `<model_id>` 链接到这个词条。runbook 里「部署组 id」的用词已由 #2643 archive 修正，本批不再改。

## Deviations from issue wording

- #2621 的「删行迁移」没有写成 `db/migrations/` 下的 schema 迁移，而是做成 oneshot 数据脚本，理由是删除的是数据、不是结构，这与 #1729 的先例一致。脚本只处理 owner 点名、且有后继 id 的 7 个改名残影，不是通用的删除工具。
- `basins_xinanjiang_upstream` 在前端静态 geojson 中的残留只记为遗留项，不在本批过滤（owner 裁决 3 只要求补文档）。
- #1729 已关闭，第 3 条验收项的措辞以评论形式追加，不重开。

## Impact

- 新增代码：`scripts/basin_catalog_manifest_audit.py`、`scripts/ops/node27_2621_delete_rename_leftovers{,_rollback}.sql` 及其测试；selector 路由。
- API 行为不变。
- 生产数据：删除 7 个流域的注册层行，涉及 basin 7、basin_version 7、model_instance 21（全部 inactive）、mesh_version 7、river_network_version 7、river_segment 85196、river_segment_crosswalk 85471、met_station 2290；run、forcing 和时序都是 0 行。
