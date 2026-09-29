## Context

- `packages/common/model_registry_catalog.py::list_basins`：默认分支只排除 `evidence-only`；`has_display_product=true` 时要求至少一条 ready forecast run。
- manifest：`/home/ghdc/nwm/object-store/scheduler/registry/manifest-last.json`（NFS，node-27 可读），结构为 `{"models":[{"basin_id":…},…]}`。
- 生产 FK 图（node-27 实测）：
  - `core.basin → basin_version → {mesh_version, river_network_version, model_instance, hydro_run, met_station}`
  - `river_network_version → {river_segment, model_instance}`
  - `river_segment → {river_segment_crosswalk, hydro.river_timeseries (hypertable, river_segment_key)}`
  - `met_station → {interp_weight, met.forcing_station_timeseries (hypertable, station_key), met.forcing_station_timeseries_legacy (hypertable, station_id)}`
  - `model_instance → {hydro_run, state_snapshot, forcing_version, interp_weight}`
- 非 FK 的文本引用：对所有非 hypertable 表中 `basin_id` / `basin_version_id` / `model_id` / `river_network_version_id` / `mesh_version_id` 的 text 列逐列计数，命中的只有上面这些注册表本身。

## Decisions

### D1 audit 复用生产集合定义

- `scripts/basin_catalog_manifest_audit.py` 通过 `PsycopgModelRegistryStore(...).list_basins(limit, offset, has_display_product=…)` 分页取全默认集合和 display 集合。
- 活跃判据用一条只读 SQL：`EXISTS core.model_instance mi JOIN core.basin_version bv … WHERE mi.active_flag`。
- manifest 只取 `models[].basin_id` 的集合。以下情况都 exit 2，不 exit 0：manifest 缺失、不可解析或为空（配置错误）；连库或查询失败（运行错误）。运行错误时 stderr 只打一行，不含 DSN，也不输出 receipt。
- receipt JSON 包含：`default_count`、`display_count`、`manifest_count`、`display_minus_manifest`、`manifest_minus_display`、`catalog_extras[]`（每项带 `active_models`）、`violations[]`、`verdict`。
- 连接通过 `--database-url` 或环境变量 `DATABASE_URL` 传入，推荐使用 `nhms_display_ro`（已确认对所需各表和 `hydro.run_status` 都有权限）。
- 只读的实现方式：`_PsycopgTransaction` 每次调用都新开连接，所以 audit 在 DSN 上追加 `options=-c default_transaction_read_only=on`（与已有 options 合并）。每一页是一个独立快照，receipt 会写明这一点。
- 新流域上线期间，display 集合会暂时不等于 manifest，这时 audit 报 exit 1 属于预期，receipt 里会给出两个差集。
- 默认目录的语义已经由 `openspec/specs/multibasin-product-discovery/spec.md` 的「缺省参数保持向后兼容」定义，本 capability 只做交叉引用。

### D2 删除沿用 #1729 的 oneshot 模式（fixture review 第 1 轮）

**执行器**：`scripts/ops/node27_oneshot_sql.py`，不另写工具。

**新增文件**：
- `scripts/ops/node27_2621_delete_rename_leftovers.sql`：删除脚本。
- `scripts/ops/node27_2621_delete_rename_leftovers_rollback.sql`：恢复脚本。

两个脚本的头注释都写清操作步骤：每次运行使用新的 `--copy-dir`；dry-run 用 runner 默认模式（结束时回滚）；只有 owner 确认后才能加 `--apply`；恢复时以 apply 目录作为 `--copy-dir`。

**删除范围写死在脚本里**，全部是 SQL 常量。脚本只读取 `nhms.manifest_basins` 和 `nhms.probe_timeout_s` 这两个 setting，任何其他 `nhms.*` setting 出现即 RAISE；不存在覆盖计数或 ID 集合的路径。与 node-27 盘点结果（`.workplans/reg-inventory`，见 receipt）逐项等值：
- 7 个 basin_id，每个都带 `successor_basin_id`（`basins_se_{mdzh,mj,mnzh,qtj}`、`basins_sw_{dulongjiang,lancangjiang,nujiang}`），由 NOTICE 输出；
- 各张表的计数：basin 7 / basin_version 7 / mesh 7 / rnv 7 / model_instance 21 / river_segment 85196 / crosswalk 85471 / met_station 2290。

**Fail-closed 检查**：以下任一项不满足即 RAISE，已做的删除全部回滚。

1. **目标形态**：每个 basin 存在，`basin_group` 不是 `evidence-only`，不在 node-22 manifest 中。manifest 的 basin 集合通过 runner 的 `--set nhms.manifest_basins=<逗号串>` 传入，由操作者从 `manifest-last.json` 生成；脚本里要断言这个值非空。每个后继 basin 必须存在，且至少有一个 active model_instance。
2. **FK 清单**：live FK 集合必须与盘点到的非 chunk FK 集合完全相等（父表属于删除所触及或级联到的表，不含 `_timescaledb_internal`）；所删各表上不得有用户 trigger。
3. **ID 集合**：
   - model 集合取 `basin_version_id ∈ 目标 OR river_network_version_id ∈ 目标 OR mesh_version_id ∈ 目标` 的并集，结果必须恰好等于目标下的 21 个 inactive 模型，且没有 active 模型；
   - 以下各项全部为 0：hydro_run（按 basin_version 和 model 两个口径）、forcing_version、interp_weight（按 model_id 和 station_id 两个口径）、state_snapshot（含 `cloned_from_model_id`）、run_display_coverage、`ops.pipeline_job.model_id`。
4. **锁定**：按父到子的顺序对待删行加 `FOR UPDATE`（basin → basin_version → river_network_version → mesh_version → model_instance → met_station → river_segment），每次加锁都断言行数。加锁之后，并发写入这些父行的 FK 子行会被阻塞，所以删除后只需要对普通表做孤儿检查，不必重扫 hypertable。
5. **hypertable 零引用**（Gate A2）：
   - 前提：已锁定；
   - 写法：只允许 `= ANY(<数组>)`，不得用 join 或 IN 子查询。EXPLAIN 已确认 join 写法会全量解压；
   - 检查内容：`hydro.river_timeseries.river_segment_key`、`met.forcing_station_timeseries.station_key`、`met.forcing_station_timeseries_legacy.station_id`，三者引用目标 key 的行数都必须为 0；
   - 每个探测单独计时，结果由 NOTICE 输出；
   - 超时预算：探测在 plpgsql 中单进程执行，要扫约 110 GB 压缩数据（18 个压缩 chunk，每个 5.8–9 GB），#1729 记录过这类扫描超过 10 分钟。因此 dry-run 时每个探测用 `SET LOCAL statement_timeout = '3600s'`，其余语句保持 `300s`，`lock_timeout` 为 `10s`。apply 的探测超时取 dry-run 实测最大值 ×2，上限 3600s。这个值由操作者通过 `--set nhms.probe_timeout_s=<秒>` 传入，脚本只接受 60–3600 之间的整数，缺省时用 3600。
   - dry-run 若超时，就停下并报告 owner。是否改用下方的不变量证明，由 owner 决定；执行者不得自行放宽或加大超时重跑。
   - 注意：探测期间会长时间持有事务（最长 1 h），期间会推迟全局 vacuum 视界。在 receipt 中记录这段时长。

   这一步给出的是 FK 级的精确证明。写入路径还有一条不变量作为补充：`river_timeseries` 的每一行都经 `run_key` 挂在某个 hydro_run 上，而第 3 步已经证明目标 rnv 只被目标模型引用、这些模型没有任何 hydro_run。这条不变量写进 NOTICE，不替代探测。
6. **备份**：对即将删除的行执行 `@copy-out`，谓词与后面的 DELETE 完全相同；列集合的守卫与 rollback 脚本逐字节一致。
7. **删除顺序**：`river_segment_crosswalk` → `river_segment` → `met_station` → `model_instance` → `mesh_version` → `river_network_version` → `basin_version` → `basin`。
   - `river_segment` 和 `met_station` 这两条 DELETE 在 `SET LOCAL session_replication_role = replica` 下执行。`SET LOCAL` 会一直持续到事务结束，所以每条 DELETE 之后立即执行 `SET LOCAL session_replication_role = origin`，并在下一条语句之前断言 `current_setting('session_replication_role') = 'origin'`。
   - DELETE 如果出错，整个事务回滚，SET LOCAL 也随之撤销。
   - 设置 replica 需要超级用户（node-27 的 `nhms` 为 `rolsuper=t`，PG 15.2，TimescaleDB 2.10.2）。
   - 如果权限不足，**直接 RAISE**，不退回逐行 RI 的慢路径：#1732 删 8394 行时就卡了 4 分 44 秒，本次要删 85196 行。
   - 每一步都断言删除行数，并输出耗时。
8. **Gate B**：对普通表做孤儿检查：crosswalk、interp_weight、hydro_run、model_instance、met_station、river_segment 对已删父行的引用，全部必须为 0。
9. **不删、只登记**：`ops.audit_log`（只做等值计数）、`ops.pipeline_job`、`hydro.state_snapshot.cloned_from_model_id`、`met.best_available_selection`。最后一张表与注册表之间没有 FK，只有 text 列 `forcing_version_id`，而目标下没有 forcing_version。

**执行期间**：dry-run 和 apply 两次运行都要先暂停 node-27 的压缩和保留作业，跑完再恢复（与 #1729 头注释的要求一致）。作业暂停前后的状态都记进 receipt。

**rollback 脚本**：按父到子的顺序执行 `@copy-in`，恢复时保留 identity 列 `river_segment_key` / `station_key` 的原值（`OVERRIDING SYSTEM VALUE` 或等效写法）。如果目标行仍然存在，就拒绝恢复。

### D3 文档口径

- 默认 `/basins` = `core.basin` 原始目录（不含 `evidence-only`）。验收口径改为：「display 集合 == manifest，且默认目录 − manifest 的每一项都满足『没有 active model_instance』」，由 D1 脚本机检。
- 退役流域不论有没有 run，一律保留 `core.basin` 行，保住复活路径和血缘。例如 `basins_neiliuqu` 虽然没有任何业务数据，也属于退役。D2 只处理 owner 点名、且已经有后继 id 的同日改名残影，receipt 里记录每一项的 `successor_basin_id`。

## Risks

- `basins_xinanjiang_upstream` 退役后仍留在 `apps/frontend/public/geo/national-basin-{domain,river}.geojson` 中（1 个面要素、216 个河段要素）。owner 裁决 3 只要求补文档，所以 §7 记录里把它列为遗留项，并注明后续需按 §7.1 过滤；本批不改前端资产。
- node-22 本地注册表副本 `/scratch/frd_muziyao/nhms-prod`：只做只读 `grep` 核查，确认补登的 5 个流域是否已经从中移除，结果写进记录；不写 node-22。
- 删除后，node-27 `node27-ingest.env` 的 `AUTOPIPE_EXCLUDE_BASINS` 里这 7 个 id 会变成死配置，照 #1732 的做法只在记录里注明。seed 源目录里已经没有 `DNZH-*`/`xinan*`，复活路径是关着的，receipt 里记一行。

- 删除操作可以恢复但代价高：有 copy 备份（fsync）和 rollback 脚本，并由往返测试钉住。dry-run receipt 交给 owner 过目后才会 `--apply`。
- `session_replication_role=replica` 会关闭所有 trigger，包括 RI。它只覆盖两条 DELETE 语句，每条之后立即切回 `origin` 并断言；前面由已锁定状态下的 Gate A2 精确零引用检查兜底，后面由 Gate B 兜底。
