# node-27 `nhms-db` 容器运维

适用对象：node-27 上承载生产 `nhms` 库的 `nhms-db` 容器。
本文只写**会咬人的那几条**，通用 PostgreSQL 知识不重复。

状态：2026-08-23 建立并实测。所有数字与日志片段均为当日实测，不是设计意图。

## 1. 绝对不要用 `docker restart`（历史上每一次重启都是一次崩溃）

`docker restart` / `docker stop` 默认发 **SIGTERM**。PostgreSQL 收到 SIGTERM 走
**smart shutdown**——它会等所有客户端**自己**断开。本机的 display API 与 ingest 持有长连接，
所以它永远等不完；Docker 的停止超时到点补 **SIGKILL**，于是：

```
03:02:27  received smart shutdown request
03:02:45  starting PostgreSQL 15.2 ...
03:02:46  database system was not properly shut down; automatic recovery in progress
03:02:53  redo done ... elapsed: 7.03 s
```

崩溃恢复的代价不是那 7 秒，是 **PostgreSQL 15 在崩溃后丢弃全部累计统计**：
`n_dead_tup`、`n_mod_since_analyze`、`idx_scan`、`last_autovacuum` 全部归零。

后果比听起来严重得多。**autovacuum 的触发判据完全建立在这些计数器上**——计数器清零之后，
一张有 40 万死元组的表在 autovacuum 眼里就是"零死元组、无需处理"，于是它确实**不需要**跑。
表面现象是"autovacuum 静默"，实质是**它的输入被反复归零**。issue #1468 立单时看到的
"三张权威表 `last_analyze`/`last_autoanalyze` 全为 NULL、`n_live_tup=0`"就是这个机制，
issue #1769 与 #1770 也同源。

> **`stats_reset IS NULL` 不代表统计从未被清零。** 崩溃丢弃**不会**设置该字段。
> 要判断计数器覆盖的时间窗，看 `pg_postmaster_start_time()` 与上次关机是否干净，
> 不要看 `stats_reset`。ADR 0004 里有一条因为读错这个字段而产生的更正。

### 正确的停机方式

容器已于 2026-08-23 起配置 `--stop-signal=SIGINT --stop-timeout=300`，
所以现在 `docker stop` / `docker restart` 也是 fast shutdown。**若容器被重建而漏掉这两个参数**，
必须手工用：

```bash
docker kill --signal=INT nhms-db
```

干净关机的日志长这样（实测 8 秒、退出码 0）：

```
received fast shutdown request
shutting down
checkpoint starting: shutdown immediate
```

启动侧的判据是 `database system was shut down at ...`（干净）而不是
`database system was not properly shut down`（崩溃）。验证计数器是否保住，
取一个停机前后都能读的数即可，例如 `pg_stat_database.xact_commit`。

## 2. `/dev/shm` 默认 64 MB，大表维护会报一个假装是"磁盘满"的错

裸 `docker run` 的 `--shm-size` 默认 64 MB，而本机 `maintenance_work_mem` 约 2 GB。
并行索引 vacuum 需要在共享内存里开数组，于是：

```
ERROR: could not resize shared memory segment "/PostgreSQL.3313941706"
       to 829009152 bytes: No space left on device
```

**这不是磁盘满。** 容器已于 2026-08-23 起用 `--shm-size=1g`。
若在旧配置上遇到，临时绕开：

```bash
docker exec -i -e PGOPTIONS='-c max_parallel_maintenance_workers=0' nhms-db \
  psql -U nhms -d nhms -c 'VACUUM (VERBOSE) <table>;'
```

## 3. 端口只绑回环

容器发布为 `-p 127.0.0.1:55432:5432`。**不要**改回 `-p 55432:5432`——那等于
`0.0.0.0`，数据库直接暴露公网。2026-08-23 之前就是这个状态，日志里有持续的凭据爆破
（`postgres` / `user` / `wog` / `pgg_superadmins` 等字典用户名）。

仓库内**全部** DB 消费者都用 `127.0.0.1:55432`（`infra/env/*.env`），
没有任何一处用公网 IP，所以回环绑定不需要改任何配置。需要从外部连库时走 SSH 隧道。

宿主机上另有一条 `DOCKER-USER` 链的 DROP 规则作为纵深防御。**注意它重启即失**，
且 `ufw` 对 docker 发布的端口无效（docker 自己插 DNAT 规则绕过 ufw），
所以别用 `ufw deny` 冒充这层防护。

## 4. 容器重建

PGDATA 是 **bind mount**（宿主 `/data/GHDC/nhms-primary/pgdata` → 容器 `/home/postgres/pgdata/data`），所以重建容器不动数据；保留的 `/home/nwm/nhms-pgdata` 不是当前 cluster。工作集容量须绑定配置目标的实际设备与可用字节，不能借用 `/home`；此修正不启用冷层。
重建前先按 §1 干净停机，并把旧容器 `docker rename` 留作回滚参照而不是直接 `rm`。

**镜像用 ID 而不是 tag。** `timescale/timescaledb-ha:pg15-latest` 是移动标签，
用 tag 重建可能静默换掉 PostgreSQL / TimescaleDB 小版本。当前 ID 见
`docker inspect nhms-db-pre1770 --format '{{.Image}}'`。

口令等容器级环境变量从旧容器导出，不要手抄：

```bash
docker inspect <old> --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^POSTGRES_(PASSWORD|DB|USER)=' > /tmp/nhms-db.env
chmod 600 /tmp/nhms-db.env
# ... docker run --env-file /tmp/nhms-db.env ...
shred -u /tmp/nhms-db.env
```

必须带上的参数（每一条都对应上面一个坑）：
`--user 1005:1005`、三个 bind mount、`-p 127.0.0.1:55432:5432`、`--shm-size=1g`、
`--stop-signal=SIGINT`、`--stop-timeout=300`、`--restart unless-stopped`、命令 `postgres`。

重建后的验收：启动日志是 `database system was shut down at ...`；
`docker exec -i nhms-db df -h /dev/shm` 显示 1.0G；
`docker inspect` 的 `PortBindings` 的 `HostIp` 是 `127.0.0.1`；
display API `active` 且本地 `:8080` 与公网 `https://test.nwm.ac.cn` 均 200；
行数抽查（`met.met_station`、`core.river_segment`）与 `pg_database_size` 对得上。

## 5. Historical selective-cold installer and tablespace procedure

Archive status:
- status: superseded
- current_authority: openspec/changes/compressed-chunk-cold-tablespace-tiering/tasks.md (R5.2 effective-deployment handoff)
- superseded_by: openspec/changes/compressed-chunk-cold-tablespace-tiering/tasks.md
- status_since: 2026-09-14
- archive_scope: section
- retained_for: #1894 installation/probe safety history

The `nhms_cold` installer, disposable probe, receipt schemas, source role grant,
and positive audit have been retired from the repository. This document no longer
contains a runnable installation, `CREATE TABLESPACE`, rollback, or Docker-oracle
procedure for that withdrawn capability.

Source retirement does not authorize a live `REVOKE`, `DROP`, tablespace/data
deletion, or change to deployed unit/dropin/env references. R5.2 must separately
observe and authorize any effective-deployment disposition while normal PGDATA,
container restart, governance, compression, and retention operations remain under
their existing owners. Historical probe records and completed receipts remain
evidence, not current commands.

## 6. 口令轮换

两个角色：`nhms`（应用写路径，**目前仍是 superuser**）与 `nhms_display_ro`（只读边界）。

先备份 `infra/env/*.env`，再 `ALTER ROLE`，再改 env，最后逐条 URL 实连验证。
`nhms_display_ro` 历史上在 14 个 env 文件里存在**五个互不相同的口令值**，
即其中大部分是陈的——轮换时顺手统一。

**验证必须从容器外做。** `pg_hba` 里 `local` 与 `127.0.0.1/32` 是 `trust`
（那是容器内部的回环），在容器里 `psql` 无论填什么口令都能进，
用它验证"旧口令已失效"会得到假警报。正确做法是从宿主机经 `127.0.0.1:55432` 连，
那条路径命中的是 `host all all all scram-sha-256`。

顺带记一笔：`local`/`127.0.0.1` 的 `trust` 意味着**任何能 `docker exec` 的人无需口令即可进库**。
这是现有运维模型的一部分，不是本文要改的东西，但要知道它在那儿。

## 6. 已知未处置

- **应用以 superuser 连库**。superuser 可以 `COPY ... FROM PROGRAM`，即在容器内执行命令；
  这不是"能读写数据"而是命令执行。最小权限改造另行立单。
- **`ssl = off`**。口令走 SCRAM 不会明文暴露，但查询与结果集明文过网；收到回环后风险大降。
- autovacuum 在统计不再被反复清零之后是否恢复正常，需要等有机 churn 把计数器累积到阈值之上
  才能验证，见 #1770。（2026-09-13 只读探针已见输出恢复；持续检查见 §7。）

## 7. autovacuum / autoanalyze 输出新鲜度检查（#1769）

资源治理审计（`scripts/node27_resource_governance.py`，每日 unit）在 receipt 的
`postgres.maintenance_output` 里采集**输出**而不是配置：每表的有效触发阈值（表级 `reloptions`
覆盖，否则集群设置；`reltuples < 0` 按 0 计）、`n_dead_tup` / `n_mod_since_analyze`、
四个 last_* 时间的年龄（秒），以及用户表 `max(last_autovacuum)` / `max(last_autoanalyze)`。
`reloptions` 含 `autovacuum_enabled=false` 的表（压缩 chunk 的幽灵计数器）不参与。
行集只含超阈值或零统计的表，最多 50 行。

| code | 严重度 | 含义 |
|---|---|---|
| `TABLE_STATISTICS_STALE` | warning | `n_mod_since_analyze` > 10 × 有效 analyze 阈值，且 `last_autoanalyze`/`last_analyze` 较新者为空或超过 24h |
| `TABLE_VACUUM_DEBT_STALE` | warning | `n_dead_tup` > 10 × 有效 vacuum 阈值，且 `last_autovacuum`/`last_vacuum` 较新者为空或超过 24h |
| `AUTOVACUUM_OUTPUT_STALLED` | critical | 有 analyze-stale 表且全库 `max(last_autoanalyze)` 为空或超过 24h，**或**有 vacuum-stale 表且 `max(last_autovacuum)` 为空或超过 24h（08-20 形态）；审计 exit 1 → `OnFailure=` 告警，receipt `status` 仍为 `completed` |
| `MAINTENANCE_OUTPUT_UNAVAILABLE` | warning | `postgres` 可达但探针失败/缺失/畸形；看不见的输出**不算**健康 |
| `TABLE_ZERO_STATISTICS` | info | `relpages = 0`、`reltuples < 0`、`n_live_tup > 0` 且从未 analyze |

要点：

- 判据用修改量与年龄，**不用**"双 NULL"引导签名，所以手工 `ANALYZE` 一次之后再次失修的表仍会报。
- 让计数器活下来的是 §1 的停机契约（`StopSignal=SIGINT`、`StopTimeout=300`）；容器重建漏掉它，
  下一次 `docker stop` 就是崩溃恢复。
- **盲区**：崩溃丢弃计数器后，所有基于计数器的检查（包括本检查）都会读成绿色，而且
  `stats_reset` 仍为 NULL——`stats_reset IS NULL` 不是"从未清零"的证据。预防靠停机契约；
  规划器统计存于 `pg_statistic` 不受影响，双 NULL 的权威表由 autopipe 统计守卫的引导腿兜底。
- `core.basin` / `core.mesh_version`（18 行、19 次修改，低于默认 analyze 阈值 50）会以
  `TABLE_ZERO_STATISTICS` info 出现，这是预期处置：18 行表的估计误差无关紧要，
  churn 过 50 后 autoanalyze 会把它们移出该类，届时若没移出由 stale 规则捕获。
  不为它们加 `reloptions`（需要迁移，会破坏 I8 "待执行集合恰为 000059" 的门）。
