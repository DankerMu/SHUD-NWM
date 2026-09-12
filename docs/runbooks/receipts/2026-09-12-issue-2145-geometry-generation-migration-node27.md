# node-27 live receipt — issue #2145：施加 `geometry_generation` 及其同批待施加迁移

- 日期：2026-09-12（writer 停机窗口 11:07:49Z – 11:32:18Z，UTC，历时 24 分 29 秒）
- 节点：node-27（`210.77.77.27`），活主库 `postgresql://***@127.0.0.1:55432/nhms`（容器 `nhms-db`）
- 活动树：`/home/nwm/NWM` = `a8db554d`（分支 `hotfix/node27-rollback-pre-2073`），`git status --porcelain` 全程为空
- 执行者：Claude Opus 5（`subagent-workflow`），OpenSpec change `apply-node27-pending-schema-migrations`

## 1. 结论

三个待施加迁移全部施加成功，账本 `56 → 59`；`core.river_network_version.geometry_generation`
为 `integer / NOT NULL / DEFAULT 0`，44 行全取默认值；两张热路径 hypertable 的 chunk 间隔
由 `7 days` 变为 `3 days`，既有 chunk 与压缩状态一字未动。红→绿在**同一个进程**（PID 2036297）上闭合。
生产 `:8080` 与 `5a86841c` 的 reslice pin 全程未动。

## 2. 不动点（本窗口刻意不碰）

| 对象 | 状态 | 归属 |
|---|---|---|
| `nhms-display-api.service.d/60-reslice-pin-original-5a86841c.conf` | 未动 | #2162 |
| 生产 `:8080` display 进程 | 未重启（PID 1933082，窗口后 `/api/v1/layers` = 200） | #2162 |
| `/home/nwm/NWM` 的 `git pull` / 分支恢复 | 未执行 | #2162 |
| `yd-*` timer（独立实例 `:55434`） | 全程保持运行 | — |
| `scripts/backfill_hydro_run_parsed_at.py` 回填 | 未执行 | #1789 |

**autopipe 也被同样的 pin 钉住**：`systemctl --user show nhms-node27-autopipe.service -p DropInPaths`
只列出一条 `nhms-node27-autopipe.service.d/60-reslice-pin-original-5a86841c.conf`，其 `ExecStart=` 先清空再指向
`/home/nwm/NWM-reslice-original-5a86841c/scripts/node27_autopipe_cron.sh`，
`PYTHONPATH` / `NODE27_AUTOPIPE_REPO` / `NODE27_AUTOPIPE_ENV_FILE` 同样指向该固定树；窗口期在跑的 tick 进程命令行实测为
`/home/nwm/NWM-reslice-original-5a86841c/.venv/bin/python .../scripts/node27_autopipeline.py`。
该树的 `workers/model_registry/basins_registry_import.py` 中 `geometry_generation` 出现 **0** 次。
即读写两侧由**同一套 pin** 同时围栏，#2162 撤 pin 时两侧一起变活——本单先把列建好，正是为那一刻。

> **更正（2026-09-12，本节初稿之后实测）**：本节初稿写作「同一条 drop-in 也钉住了 autopipe」，
> 那是把 display 与 autopipe 各自的 drop-in 误当成了同一个文件。实测
> `find /home/nwm/.config/systemd/user -name '60-reslice-pin*'` 返回 **8 个同名但互相独立的文件**
> （均为 2026-09-11 07:11），分别位于以下 8 个 unit 的 `.d/` 目录：
> `nhms-display-api`、`nhms-node27-autopipe`、`nhms-node27-download`、`nhms-node27-raw-retention`、
> `nhms-node27-timeseries-retention`、`nhms-node27-timeseries-compression`、
> `nhms-node27-resource-governance`、`nhms-node27-frontier-alert`。
> 每个文件把该 unit 的 `WorkingDirectory` / `ExecStart` 改指向同一份冻结副本，机制相同、文件相互独立。
> 这个差别有操作意义：**只删 display 那一个文件，另外 7 个 unit 仍跑冻结副本**，
> 解钉是 8 个文件的动作，不是 1 个。本单的结论（读写两侧被同样的 pin 同时围栏）不受影响。

`services/tiles/mvt.py` 中 `rnv.geometry_generation` 的出现次数（`git show <sha>:...`）：
`d113edca` 3 · `fc21a19e` 3 · `a8db554d` 3 · **`5a86841c` 0**。生产此刻不 500 的唯一原因就在最后一列。

## 3. 窗口时间线

| 时刻（UTC） | 事件 |
|---|---|
| 11:07:49 | `nhms-node27-{autopipe,download}.timer` stop（`active -> inactive`）—— 新 tick 围栏建立 |
| 11:07:49–11:20:24 | stage-b 的两次启动在排空循环中被杀（见下），timer **全程保持 `inactive`**，围栏未曾解除 |
| 11:20:24 | 第三次启动；两个 timer 读数均为 `inactive -> inactive`，确认围栏仍在 |
| 11:20:24–11:28:25 | 8 分钟排空等待到界；围栏**前**已启动的 autopipe tick 仍 `activating`（见偏离 7） |
| 11:28:25 | 窗口内基线 + 锁持有者快照 + 属主漂移计数（`ownership_drift_relations=0`） |
| 11:28:25–11:28:32 | 超级用户写会话**之前**的 audit-only：`## audit: OK -- no owner drift` |
| 11:28:32–11:31:57 | `packages.common.migrate`：前 4 次被锁挡（`lock_timeout` 快失败），取消一条只读慢查询后第 5 次 rc=0 |
| 11:31:57–11:32:18 | 目录核验 + 完整 `node27_provision_write_roles.sh`（rc=0，审计干净）+ 写侧权限探针 |
| 11:32:18 | timer 重启，两者 `active`；窗口关闭 |

**writer 停机窗口的真实长度是 11:07:49Z → 11:32:18Z，24 分 29 秒**，不是最后一次启动到关闭的 11 分 54 秒。
stage-b 共启动三次：第一次（11:07:49）与第二次（11:19:49）都在**排空循环内、任何数据库变更之前**被杀，
原因分别是排空判据写成了 `case "$s$d" in *active*)`——而 `inactive` 里含 `active`，该 glob 恒真、循环永不退出；
以及把 30 分钟的排空上限缩到 8 分钟（真正的阻塞是那条只读慢查询，不是在途 tick）。
两次击杀都没有执行到 B2a 之后，因此本窗口对库的写操作只发生过一次。围栏自 11:07:49 起未曾解除，
在途 tick 被完整观测了 24 分钟仍未结束。

## 4. 改前基线

- 账本 56 行，最高 `000055`；`db/migrations/` 与 master 逐字节相同。
- 差集 = 3 个文件（`000056` / `000057` / `000058`）；另有 7 个账本行磁盘无文件，
  系 #2048 记录的 `b97c16e2` 追溯删除漂移，`packages/common/migrate.py:149` 的循环只遍历磁盘文件，**永不访问**它们。
- `core.river_network_version` 无 `geometry_generation` 列，44 行。
- `hydro.hydro_run.parsed_at` **已存在**（`timestamp with time zone | YES | NULL`）而文件不在账本——
  曾被某条不写账本的路径施加过。
- 窗口内 hypertable 读数：`hydro.river_timeseries interval=7 days`、`met.forcing_station_timeseries interval=7 days`；
  chunk 6 + 6，压缩 6/12。

`df -h / /home /data/GHDC`：

```text
改前 2026-09-12T10:50:49Z        改后 2026-09-12T11:32:26Z
/      98G   71G   23G  76%      /      98G   71G   23G  76%
/home  1.7T  455G  1.1T  29%     /home  1.7T  455G  1.1T  29%
/GHDC   15T  1.7T   13T  13%     /GHDC   15T  1.8T   12T  13%
```

`/` 与 `/home`（库所在卷）零变化——三条迁移都是元数据级 DDL（两条 `ADD COLUMN IF NOT EXISTS`、
一条只影响新建 chunk 的 `set_chunk_time_interval`），本就不该产生数据增长。
`/data/GHDC` 的 1.7T → 1.8T 是同期 ingest 往 object-store 写的量，与本次施加无关。

## 5. 红证据（施加前，:8090 一次性实例 PID 2036297，起于 2026-09-12 18:50:47 CST）

四个面全部 500，正文与服务端 traceback 一致：

```text
psycopg2.errors.UndefinedColumn: column rnv.geometry_generation does not exist
LINE 6:                        rnv.geometry_generation,
                               ^
```

| 面 | 施加前 |
|---|---|
| `/api/v1/layers` | 500 |
| `/api/v1/tiles/river-network-national/5/25/12.pbf` | 500 |
| `/api/v1/tiles/hydro-national/q_down/{vt}/4/12/6.pbf` | 500 |
| `/api/v1/tiles/hydro-national/gfs/{cycle}/q_down/{vt}/4/12/6.pbf` | 500 |

`/api/v1/layers` 也 500，说明当时存在 display-ready run——它没有走 `return []` 早退路径，
这条红证据因此是无条件的，不是「恰好没有 run」。

## 6. 施加

```bash
cd /home/nwm/NWM
PGOPTIONS="-c lock_timeout=5s -c statement_timeout=120s" \
DATABASE_URL="postgresql://***@127.0.0.1:55432/nhms" PYTHONPATH=/home/nwm/NWM \
  .venv/bin/python -m packages.common.migrate
```

```text
Applied migration: 000056_hydro_run_parsed_at.sql
Applied migration: 000057_river_network_version_geometry_generation.sql
Applied migration: 000058_hot_timeseries_chunk_interval_3d.sql
Migrations complete: 3 applied, 49 skipped, 52 total.
```

前 4 次尝试全部被锁挡（每次 1 处 `lock timeout`），见偏离 6 与 §9。

## 7. 目录核验（判据取自库自己的目录，不取退出码）

```text
ledger_rows_after=59            (56 → 59，+3)
ledger_has_000056=1  ledger_has_000057=1  ledger_has_000058=1
ledger delta: +000056_hydro_run_parsed_at.sql
              +000057_river_network_version_geometry_generation.sql
              +000058_hot_timeseries_chunk_interval_3d.sql

core.river_network_version.geometry_generation | data_type=integer | is_nullable=NO | default=0
hydro.hydro_run.parsed_at | data_type=timestamp with time zone | is_nullable=YES | default=NULL
rnv_rows_total=44   rnv_rows_distinct_from_zero=0

flood.return_period_result          interval=7 days
hydro.river_timeseries              interval=3 days   <- 000058
met.best_available_selection        interval=7 days
met.forcing_station_timeseries      interval=3 days   <- 000058

hydro.river_timeseries chunks=6
met.forcing_station_timeseries chunks=6
compressed_over_total=6/12
chunk_inventory_unchanged=YES
```

`parsed_at` 的三属性与改前**逐字一致**。这条判据是本次施加里唯一不可省的安全阀：该列先于账本存在，
账本补登会让 `000056` 从此**永不重放**；若线上列形状与迁移本该建的不符，补登就把一次真实漂移永久盖住。
实测一致，故补登安全。

`chunk_inventory_unchanged` 的比较基准是**窗口内**（11:28:25）读数，不是 stage A 三小时前、timer 仍在跑时的读数——
否则期间 ingest 新建的 chunk 会被误读成 `000058` 的副作用。

## 8. 角色审计与写侧探针

- 施加前 audit-only（`do_roles=off do_ownership=off do_audit=on strict_audit=on`）：`## audit: OK -- no owner drift`。
  该步是**硬闸**：审计不干净就拒绝开超级用户写会话并恢复 timer。
- 施加后完整 `bash scripts/node27_provision_write_roles.sh`：`provision_rc=0`，
  `169 expression(s)/trigger(s) scanned, 21 distinct function(s) referenced, 0 untrusted for a superuser writer`，
  `nhms_ingest_rw` / `nhms_download_rw` 的 `has_temp=f`、`create_on_schemas=(none)`，
  收尾 `## audit: OK -- no owner drift`、`full provision complete; audit clean`。
- 属主收敛为 no-op 有**前置实测**而非事后推断：`ownership_drift_relations = 0`，而
  `db/roles/node27_write_roles.sql:427/438/450/459` 的四个属主循环都带 `c.relowner <> 'nhms_ingest_rw'::regrole`
  谓词，drift 为 0 时一条 `ALTER ... OWNER TO` 都不发、不取任何关系锁。本次未新建 relation，只加列，不改 `relowner`。
- 写侧最小权限探针：`SET ROLE nhms_ingest_rw; UPDATE core.river_network_version
  SET geometry_generation = geometry_generation WHERE false;` → `SET` / `UPDATE 0`，rc=0。

## 9. 施加期间的锁争用（本窗口最需要被下一个人读到的一段）

`packages/common/migrate.py` 全文**没有** `lock_timeout`——它只是
`psycopg2.connect(database_url)` + `autocommit = True`（`:160-161`）。窗口打开时，生产 `:8080` 上有一条
`nhms_display_ro` 的 z=3 全国瓦片查询已 **CPU-bound 跑了 53 分钟**（`wait_event_type` 为空 = 在算不是在等），
握着 `core.river_network_version` / `hydro.hydro_run` / `hydro.river_timeseries` 的 `AccessShareLock`：

```text
| 5375 | nhms_display_ro | nhms-display-api | river_network_version | AccessShareLock | active | 01:03:18 |
```

无 `lock_timeout` 时，`ADD COLUMN` 的 `AccessExclusiveLock` 会无限排队，**并把其后每一个读者一起堵在锁队列里**
——那才是真正的生产事故，而不是迁移慢。处置：连接层加
`PGOPTIONS="-c lock_timeout=5s -c statement_timeout=120s"`，每条语句 5 秒失败；`migrate` 按账本跳过已施加文件，重试免费。
4 次重试仍被挡后，对**只读**会话发 `pg_cancel_backend`（不是 `terminate`）：

```text
| pid  | application_name | query_age | cancel_sent |
| 5375 | nhms-display-api | 01:06:01  | t           |
```

代价是一个 HTTP 请求失败；无数据变更、未重启服务、未动 pin。第 5 次施加 rc=0。

## 10. 绿证据（施加后，**同一个** PID 2036297，未重启）

| 面 | 施加后 | 字节 | `x-tile-cache` |
|---|---|---|---|
| `/api/v1/layers` | **200** | 6073 | — |
| `river-network-national/5/25/12` | **200** | 142594 | miss → 第二遍 **hit** |
| `hydro-national/q_down/{vt}/4/12/6` | **200** | 1374092 | miss |
| `hydro-national/gfs/{cycle}/q_down/{vt}/4/12/6` | **200** | 1374312 | miss → 第二遍 **hit** |

四个面零 500。这里的 miss 是 **first-touch**，**不是** key 轮换证据：:8090 用的是一次性空缓存目录，
施加前的请求又全是 500、什么都没缓存。key 轮换那一轮 miss 归 #2162（偏离 5）。

## 11. #2190 欠账：两臂三路由的字节 / ETag / cache-key 恒等比对

两臂 `d113edca`（base）与 `fc21a19e`（head），均实测 `pyproject.toml` / `uv.lock` 与活动树一致，
故复用活动树 `.venv`；两臂 `services/tiles/mvt.py` 均含 `rnv.geometry_generation` ×3，digest basis 同源。
两臂 discovery 完全一致（`default_cycle=2026-09-10T12:00:00Z`、`valid_time_count=56`、
`chosen_instant=2026-09-14T00:00:00Z`、同一 `run_id`）。

| 路由 | base | head | 恒等 |
|---|---|---|---|
| `hydro-national/{source}/{cycle}` | 200 / 1418920 B | 200 / 1418920 B | **是**（md5 / ETag / cache-key 三轴全等） |
| `river-network-national` | 200 / 142594 B | 200 / 142594 B | **是**（三轴全等） |
| `hydro-national`（legacy alias） | 200 / 1418895 B | 200 / 1418908 B | 否 |

第三行**不是代码差异**，已用同树对照证伪：同一棵 `d113edca` 相隔约 10 分钟跑两次，
该路由从 `1418895 B / md5 45280ee2…` 变为 `1418908 B / md5 915f5ac1…`（与 head 同字节数），
而背靠背 11 秒内的两次（base2 vs base3）三轴完全恒等。

结论：legacy alias 在请求时自解析「最新 run」，窗口后 ingest timer 已恢复并在持续写入 `hydro.river_timeseries`，
该路由在写入进行中**按构造就不具备跨时刻可比性**；两条把身份钉死在 URL 里的路由跨臂逐字节恒等。
`hydro-national` legacy alias 的 base/head 比对需在 writer 静止时重做才具因果力，本 receipt 不声称它恒等。

## 12. 清理与收尾不变量

- :8090 一次性实例已 kill（`processes left: 0`），一次性缓存目录已删。
- 三个临时 worktree（`2145-wt-base` / `2145-wt-head` / `2145-wt-base2`）已全部 remove + prune，
  `git worktree list | grep -c 2145-wt` = 0。
- 活动树收尾：`hotfix/node27-rollback-pre-2073` `a8db554d`，`porcelain=0`（与窗口前一致）。
- 生产 `:8080`：PID 1933082 未变，`/api/v1/layers` = 200。
- timer 收尾：`nhms-node27-autopipe.timer` / `nhms-node27-download.timer` 均 `active`。
- **恢复后首个 autopipe tick 的终态：本 receipt 写作时尚不可观测**。围栏前启动的那个 tick 仍在
  `activating`（上一轮实测 `elapsed_sec=17576`），timer 要等它结束才会触发下一个，因此「首个新 tick
  无 `seed_failed stage=import`」这条断言在本窗口内取不到。当前日志尾 200 行中 `seed_failed` 出现 **0** 次。

  比等一个 tick 更强的是构造性论证：autopipe 执行的是被 pin 住的 `5a86841c` 树，
  该树的 `workers/model_registry/basins_registry_import.py` 中 `geometry_generation` 出现 0 次——
  本窗口**不可能**引入由该列导致的 `seed_failed stage=import`。真正需要观测这条的时刻是 #2162 撤 pin、
  autopipe 改跑含该列的树之后，届时列已就位。

## 13. 由本次施加派生、不在本单做的后续

1. `packages/common/migrate.py` 无 `lock_timeout` / `statement_timeout`：在活主库上施加 DDL 会无限排队并堵住锁队列。
   本窗口用 `PGOPTIONS` 在连接层绕过，但下一个没读过本 receipt 的人会再踩一次——需立单收口。
2. 生产 `:8080` 的 z=3 全国瓦片查询可 CPU-bound 跑 50 分钟以上（本窗口实测 pid 5375 / 66 分钟被取消时仍在算）。
   与本单无因果关系（它跑在 pin 住的 `5a86841c` 上），报告不修。
3. `000058` 落地后按 `docs/runbooks/tier-node27-timeseries-storage.md`「Per-tick capacity」复核 3 天 chunk 混合下的
   per-chunk 时长 / tick wall / 峰值磁盘余量。
4. `000056` 的 `parsed_at` 历史回填（`scripts/backfill_hydro_run_parsed_at.py`，人看着分批跑）。
5. `#2222`：`parsed_at` 早已在线，本次施加不改变 `/api/v1/runs` 的响应形状；但
   `packages/common/forecast_store.py` 的 `SELECT h.*` 无字段过滤仍未收口，#2222 保持 open。
