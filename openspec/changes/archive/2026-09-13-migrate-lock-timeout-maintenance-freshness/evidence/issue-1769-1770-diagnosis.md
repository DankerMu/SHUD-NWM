# #1769 / #1770 诊断收口 receipt（node-27，只读）

- 探测时间：2026-09-13 07:11–07:17 UTC；原始输出：`2026-09-13-node27-autovacuum-output-probe.txt`
- 探测方式：`docker exec -i nhms-db psql -U nhms -d nhms -X`，仅 SELECT；`docker inspect` / `docker logs`
- 未执行：VACUUM / ANALYZE / 任何写 / 重启 / 配置变更

## 1. 机制结论：已定位（候选 → 已定位）

两单记录的"autovacuum/autoanalyze 自 2026-08-20 全库静默"是**同一根因的两个投影**：

1. 容器被 `docker restart`（或等价 stop）→ Docker 发默认 **SIGTERM** → PostgreSQL 进入 **smart shutdown**，
   对 autovacuum worker 与后台 worker 发出 `FATAL: terminating autovacuum process due to administrator command`
   / `terminating background worker ...`（这就是 #1769 日志普查里"批量 SIGTERM"的来源；另一已确认来源是
   集成测试一次性库拆除 `DROP DATABASE ... WITH FORCE`，见 #1770 2026-08-23 诊断评论）。
2. smart shutdown 要等所有客户端自行断开；应用持长连接，永远等不完。
3. Docker 默认 10 s 超时后补 **SIGKILL** → 下次启动 `database system was not properly shut down; automatic recovery`。
4. PG15 在崩溃恢复时**丢弃累计统计**（`n_dead_tup`、`n_mod_since_analyze`、`last_auto*` 等），而
   `pg_stat_database.stats_reset` 仍为 NULL（丢弃 ≠ 显式 reset）。
5. autovacuum 的触发判据完全建立在这些计数器上 → 计数器归零后表在它眼里"无需处理"。
   表面是 autovacuum 静默，实质是输入被反复归零。

2026-08-23 03:02 的一次 `docker restart` 在日志里完整复现了 1–4（#1770 评论 §3）。

**排除项（均为 08-23 实测，未推翻）**：`autovacuum=off` / `track_counts=off`；launcher 反复重启；
worker 槽位耗尽；fork / 共享内存失败；表级 `autovacuum_enabled=false`（`met_station` reloptions NULL）；
快照滞留（复制槽、prepared xact、长事务均为零）；wraparound 压力；cost 限流（单页表连续数天不触发）。

**修复（2026-08-23 03:17，operator 批准，#1770 评论）**：容器重建为 `StopSignal=SIGINT`（fast shutdown）、
`StopTimeout=300`、`ShmSize=1G`。

## 2. 修复持续有效的证据（2026-09-13）

| 项 | 实测 |
|---|---|
| 容器停机契约 | `docker inspect`: `SIGINT 300 1073741824`，容器 2026-09-12 05:38 重建时保留 |
| 最近一次停启 | `database system was shut down at 2026-09-11 16:45:18 UTC` → 干净停机，无崩溃恢复（当前容器日志） |
| 计数器跨停机存活 | `core.river_segment.last_analyze = 2026-08-23 03:57Z` 仍在；`met_station.autovacuum_count = 79`（08-23 03:02 崩溃清零后重新累计） — 若 09-11 停机发生过崩溃丢弃，这些字段会是 NULL/0 |
| 用户表 autovacuum 产出 | `max(last_autovacuum) = 2026-09-13 06:37Z`，`max(last_autoanalyze) = 06:51Z` |
| 24h 内产出 | 80 张用户表中 9 张 autovacuum、12 张 autoanalyze |
| 越 vacuum 阈值（剔除 `autovacuum_enabled=false`） | **0** |
| 越 analyze 阈值（reloptions 覆盖优先） | **0** |
| 000052 四张 core 表 | `river_segment` / `crosswalk` autoanalyze_count 7、autovacuum_count 3；`river_network_version` 5；`basin_version` 4 |
| launcher / 快照滞留 | launcher 自 09-12 05:38 存活；复制槽 0、prepared xact 0、仅探测会话持 xmin |

结论：autovacuum 目前**无需修复**，也不在 epic #1979 I8 迁移（000059）的关键路径上。

## 3. 本次顺带发现（不在本 PR 修）

- `met.met_station` 堆 **325 MB / 42,029 行**（08-23 VACUUM FULL 后为 20 MB）；autovacuum 正常
  （79 次，`n_dead_tup` 6,309 未越阈），`n_tup_upd = 836,009`、`n_tup_hot_upd = 78,169`（HOT ≈ 9%）。
  计数器为自 08-23 03:02 崩溃清零以来的累计。这是写放大 / HOT 比例问题（改 `active_flag` 的 UPDATE 因部分索引
  `met_station_active_basin_station_idx WHERE active_flag = true` 不可能走 HOT），不是 autovacuum 停摆；回收需 operator
  窗口。已转 follow-up **#2300**（写源定位 + 回收方案，needs-triage）。

## 4. 产出级检查（本 PR 交付）与 #1765 的关系

- 检查落在 `scripts/node27_resource_governance.py`（governance 通道），五个代码：
  `TABLE_STATISTICS_STALE` / `TABLE_VACUUM_DEBT_STALE`（warning）、`AUTOVACUUM_OUTPUT_STALLED`（critical）、
  `MAINTENANCE_OUTPUT_UNAVAILABLE`（warning）、`TABLE_ZERO_STATISTICS`（info）。
- **#1765 已关闭且已交付**：critical 建议 → stderr `RESOURCE_GOVERNANCE_CRITICAL:<code>` → `main()` 返回 1 →
  unit `OnFailure=nhms-node27-unit-failure-alert@%n.service`（node-27 实机 unit 已含此行）。因此
  `AUTOVACUUM_OUTPUT_STALLED` 能送达人；#1769 验收"未修前不会升级为非零退出"的限制不再成立。
- 历史现场（`core.river_segment` 36.4×、`last_autoanalyze IS NULL`；`met_station` 65.9× 且全库产出 >24h 静默）
  由单测判为 warning / critical；当前现场为绿（见 §2）——**偏离**：#1769 验收要求"对当前现场给出非绿判定"，
  现场已恢复，无法给出；异常路径由单测覆盖，实机 receipt 证明检查在生产数据上采集成功并如实判绿。
- 生产 unit 部署：node-27 生产 checkout（`/home/nwm/NWM`，当前 `a8db554d`）只在 I8 维护窗口 `git pull`；
  本 PR 的实机证据来自**隔离 checkout 对生产库的只读审计**，生产 unit 随 I8 窗口拉到新代码后生效。

## 5. `core.basin` / `core.mesh_version` 归属

纳入检查（`TABLE_ZERO_STATISTICS`，info），**不补 per-table reloptions**：各 18 活行、19 次修改，低于默认
analyze 阈值 50；18 行表的 planner 误估无实际代价；churn 越过 50 后 autoanalyze 会使其离开该类，若不离开则由
stale 规则捕获。补 reloptions 需要新迁移，会破坏 I8 窗口"待施加集合恰为 000059"的门禁。

## 6. 已知盲区（记账）

崩溃丢弃计数器后，所有基于计数器的检查（含本检查与零统计类，因 `n_live_tup` 同样归零）都会读绿，且
`stats_reset` 不会提示。预防手段是容器停机契约（SIGINT/300，见 `docs/runbooks/node-27-database-container-operations.md`）；
planner 统计在 `pg_statistic` 中随 WAL 持久化，不受影响；double-NULL 的权威表由 autopipe stats guard 修复腿兜底。
