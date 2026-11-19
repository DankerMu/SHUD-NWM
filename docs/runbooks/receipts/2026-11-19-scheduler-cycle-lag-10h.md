# 2026-11-19 调度器 cycle lag 16 h → 10 h 收据（#3225）

生产 `NHMS_SCHEDULER_CYCLE_LAG_HOURS` 从 16 改为 10，`NHMS_SCHEDULER_LOOKBACK_HOURS` 保持 32。
本文记录定值所依据的实测、node-22 上的切换过程和切换后的首批 pass。模板
`infra/env/compute.scheduler-dbfree.env.example` 已同步为 10。

## 1. 定值依据

原始数据完成时间取每个 cycle、每个 source 目录下**最新一个 raw 文件的 mtime**（node-27
`object-store/raw/{gfs,IFS}/<cycle>/`，2026-11-19T21:17Z 读取）。`manifest.json` 的 mtime 不能用：
下载作业每 30 分钟重写一次最新 cycle 的 manifest，且 manifest 先于文件写出。

27 个 cycle（2026-11-06T12 .. 2026-11-19T12；2026-11-04 .. 11-06T00 是 node-27 换盘后的积压，
T+9.9 h .. T+57 h，不计入）相对名义 cycle 时刻：

| source | 首个文件 | 完成（27 个中的 24 个） | 慢 cycle（完成时刻） |
|---|---|---|---|
| GFS | T+8.01 .. 8.49 h | T+8.28 .. 8.79 h | 11-18T12 T+9.24 h；11-10T00 T+11.23 h；11-18T00 T+12.45 h |
| IFS | T+8.29 .. 8.81 h | T+8.84 .. 9.33 h | 11-18T12 T+9.81 h；11-10T00 T+11.93 h；11-18T00 T+12.98 h |

- 三个慢 cycle 两个 source 是同一批：下载作业慢，而不是上游发布晚（11-18T00 的 GFS 从 T+8.11 h 下到
  T+12.45 h，IFS 在 T+12.46 h 才开始）。下载侧的两个后续问题见 #3237、#3238。
- 未下完的 cycle 只会被**推迟**，不会被提交：node-22 判定 `available` 要求 raw manifest 存在且合法、
  每个条目文件都存在、是普通文件且非空（`source_cycle_raw_manifest.py::_verify_entry_files`），manifest 与
  文件都是原子写。这样的 cycle 在 backfill 腿里只留一条 `not_selected` 证据，pass 仍是 `planned`，不建
  candidate、不建 blocked candidate、不占执行槽
  （`tests/test_production_scheduler.py::test_backfill_defers_a_newest_cycle_whose_raw_data_is_not_ready`）。
- 所以 lag 只需覆盖常态。10 是高于常态完成时刻（≤ T+9.33 h）的最小整数小时，也覆盖 11-18T12；最慢的
  两个 cycle 会被推迟 2 h、3 h，数据齐后的第一趟 pass 选中，仍比 lag 16 早 3 h。

约束核对：lookback + lag = 42 h ≤ 48 h（node-27 压缩 lag）；forecast-data 与 journal retention 的不等式
只会更宽；state-index 清理窗口 `max(21, floor // 24 + 1)` 在 lag 16（下限 400 h）与 lag 10（394 h）下都是
21 天。

## 2. node-22 切换（2026-11-19T21:18:43Z）

| 项 | 值 |
|---|---|
| 文件 | `/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env` |
| 改动 | `NHMS_SCHEDULER_CYCLE_LAG_HOURS=16` → `10`（diff 恰为这一行，mode 600 不变） |
| 备份 | `~/NWM-presync-backup-2026-11-19/infra-env/compute.scheduler-dbfree.env.pre-3225-20261119T2118Z`（env 目录之外，见 `infra/env/README.md` 的 Backup copies） |
| 日志 | `/scratch/frd_muziyao/nhms-prod/tmp/deploy-3225-20261119T2118Z/run.log` |
| 前置检查 | 上一趟 pass `scheduler_2026111921_b6d861195c80` 为 `planned`，gfs / IFS 均 `discovered 4 / complete 4 / gap 0` |

过程：停 `nhms-compute-scheduler.timer` → 等 service 变 inactive（pass 边界）→ 前置检查 → 备份 → `sed`
改一行 → 校验 diff 恰为 2 行且新值唯一 → trap 恢复 timer。

**副作用（已自行消除）**：timer 停了 1 秒（21:18:42–43Z），15 分钟一次的
`nhms-node22-scheduler-stall-health` 探测恰好落在这一秒，报 `verdict=timer_stopped`、unit 失败并触发
OnFailure 告警。下一次探测（21:33:51Z）`verdict=ok`，失败 unit 数回到 0。是误报，调度未受影响。以后在
pass 边界改配置，应避开该探测的整点附近，或接受这一条可预期的告警。

**未改动（如实记录的漂移）**：node-22 的 `compute.host.env` 与 `compute.replay.env` 里 lag 仍是 16；
调度 timer 及其 retention / state-index 单元都不读这两个文件。

回滚：在 pass 边界把该行改回 16（或从上表备份恢复整个文件）。回滚不需要其它配套动作；已提交的 cycle
不受影响。

## 3. 切换后的 pass

证据目录：`/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/`。

| pass | 启动 (UTC) | status | `cycle_window.cycle_lag_hours` | 说明 |
|---|---|---|---|---|
| `scheduler_2026111921_9a172ef82516` | 21:18:44 | `planned` | 10 | 新 lag 下首趟；`root_preflight ready`；两源 `discovered 4 / complete 4 / gap 0` |
| `scheduler_2026111921_902ccb59bcdb` | 21:57:30 | `planned` | 10 | 22:00Z 之前的最后一趟空转 |
| `scheduler_2026111922_a13aa40e501c` | 22:03:29 | `submitted` | 10 | 22:00Z（T+10 h）后首趟：两源各 `gap 1 / available_gap 1 / selected 1`，选中 `gfs_2026111912` 与 `ifs_2026111912`；candidate 132、submitted 132、failed 0、blocked 0；23:23:29Z 结束 |

- cycle 2026-11-19T12 的 raw 数据在 T+8.29 h（GFS）/ T+8.87 h（IFS）完成，T+10.06 h 被选中提交。lag 16 下
  它要到 2026-11-20T04:00Z 才进窗。
- 这趟提交 pass 的 wall 4,808 s、CPU 804 s；此前四趟提交 pass（2026-11-18T04 .. 11-19T16）wall
  4,838 .. 5,161 s、CPU 899 .. 925 s。CPU 的下降来自同日部署的 #3223，与 lag 无关。
- 切换前 node-27 展示库的落地时刻基线（`hydro.hydro_run`，2026-11-15T00 .. 11-19T00 共 18 个 source-cycle，
  每个 66 条 run）：首条 run 行 T+16.36 .. 16.92 h，末条 T+17.79 .. 18.87 h。cycle 2026-11-19T12 的首条 run
  行在 T+10.49 h 入库；全部 132 条入库的时刻与两天观察（没有 cycle 卡在 unavailable）记录在 #3225。
