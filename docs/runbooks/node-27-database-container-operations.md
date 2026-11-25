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

PGDATA 是 **bind mount**，所以重建容器不动数据。2026-11-05 换盘后（原 14T `/dev/md0` 已退役，`/data/GHDC` 现只是根卷上的目录）cluster 由 fresh initdb 重建，当前三个 bind 是：

| 宿主机路径 | 容器路径 |
|---|---|
| `/home/nwm/nhms-primary/pgdata` | `/home/postgres/pgdata/data`（PGDATA） |
| `/home/nwm/nhms-tablespace-ghdc-stub` | `/home/postgres/pgdata/tablespaces/ghdc`（空 stub，catalog 只有 `pg_default`/`pg_global`） |
| `/home/nwm/nhms-evidence` | `/var/lib/postgresql/evidence` |

旧容器 `nhms-db-lost14t-20261105`（exited，restart=no）只是证据，不能拿来回滚；保留的 `/home/nwm/nhms-pgdata` 也不是当前 cluster。工作集容量须绑定配置目标的实际设备与可用字节（该设备现即 `/home` LV）；此修正不启用冷层。
重建前先按 §1 干净停机，并把旧容器 `docker rename` 留作回滚参照而不是直接 `rm`。

**镜像用 ID 而不是 tag。** `timescale/timescaledb-ha:pg15-latest` 是移动标签，
用 tag 重建可能静默换掉 PostgreSQL / TimescaleDB 小版本。当前 ID 见
`docker inspect nhms-db --format '{{.Image}}'`（2026-11-05 重建沿用同一 timescaledb-ha pg15 镜像）。

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
这组参数是 `nhms-db` 重建的唯一权威；`tier-node27-timeseries-storage.md` §4.3.3 的完整 `docker run`
命令须与之逐参数一致，两处冲突时以本节为准（#3060）。

重建后的验收：启动日志是 `database system was shut down at ...`；
`docker exec -i nhms-db df -h /dev/shm` 显示 1.0G；
`docker inspect` 的 `PortBindings` 的 `HostIp` 是 `127.0.0.1`；
display API `active` 且本地 `:8080` 与公网 `https://test.nwm.ac.cn` 均 200；
行数抽查（`met.met_station`、`core.river_segment`）与 `pg_database_size` 对得上。

## 5. Historical selective-cold installer and tablespace procedure

Archive status:
- status: superseded
- current_authority: docs/runbooks/tier-node27-timeseries-storage.md (current retirement authority); openspec/changes/archive/2026-09-16-compressed-chunk-cold-tablespace-tiering/evidence/retirement-effective-handoff.json
- superseded_by: openspec/changes/archive/2026-09-16-compressed-chunk-cold-tablespace-tiering/tasks.md
- status_since: 2026-09-16
- archive_scope: section
- retained_for: #1894 installation/probe safety history

The `nhms_cold` installer, disposable probe, receipt schemas, source role grant,
and positive audit have been retired from the repository. This document no longer
contains a runnable installation, `CREATE TABLESPACE`, rollback, or Docker-oracle
procedure for that withdrawn capability.

R5.2 committed the 2026-09-16 effective-maintenance handoff
([`retirement-effective-handoff.json`](../../openspec/changes/archive/2026-09-16-compressed-chunk-cold-tablespace-tiering/evidence/retirement-effective-handoff.json)).
It does not authorize a live `REVOKE`, `DROP`, tablespace/data deletion, or
cleanup of SOURCE `/home/nwm/NWM-maintenance-reviewed-95481481`, its `.venv`,
or STATE/config
`/home/nwm/.local/state/issue1895-maintenance-retirement-95481481/`. Those
paths stay the active compression deployment until the compression-unit
rebind (#2285, design D7; Stage B, after merge) and rollback and recovery
inputs after it; either way do not clean them. Normal PGDATA, container restart,
governance, compression, and retention operations remain under their existing
owners. Historical probe records and completed receipts remain evidence, not
current commands. Archive and issue closeout are being completed in this PR.

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

### 7.1 `AUTOVACUUM_OUTPUT_STALLED` 的两种形态与处置（#3234）

这条 critical 至今出现过四次（#1769、#1770、#2696、#3234），背后是两个不同的机制。先分辨，再处置。

| 形态 | 判别 | 处置 |
|---|---|---|
| 崩溃恢复清零了计数器 | `pg_postmaster_start_time()` 晚于最后一次 autovacuum，且那次重启不是按 §1 的停机契约做的 | 等有机 churn 重新累积；必要时手工 `ANALYZE` 关键表。预防靠 §1 |
| launcher 被已删除库的残留统计项卡住 | postmaster 没有重启；launcher 进程在、`wait_event=AutoVacuumMain`；没有任何 worker；`max(last_autovacuum)` 停在某一时刻不再前移 | 杀掉 launcher（见下） |

**第二种形态的成因。** PostgreSQL 15 的 worker 启动时先向统计子系统登记（不存在就新建该库的统计项），
后连库。若该库恰好在 launcher 选中它之后被 `DROP DATABASE`，已删除库的统计项会被重新建出来，
worker 随后以 `database "…" does not exist` / `It seems to have just been dropped or renamed.` 退出。
launcher 的轮转名单对"仍有统计项"的库一律保留，于是这个已删除的库永久留在名单上；
它轮到队尾后，launcher 每 `autovacuum_naptime / 名单长度` 空转一次，不再向任何已有的库派 worker。
停转期间只有新建的库（不在名单上）还会被派到 worker，所以共享目录可能仍在被处理，容易误判为"在跑"。
触发源是在同一个 postmaster 上频繁建删库。2026-11 的六次竞态全部来自 node-27 真实 DB pytest 的
`nhms_it_<uuid>` 库（20 天 839 次 `DROP DATABASE`）；自 #3376 起这些库建在一次性实例上（§8），
本实例上不应再有任何建删库。

2026-11-25 的处置确认了这个机制：杀掉 launcher 后 75 秒内有 8 个 worker 进入 `nhms`，
而六个已删除库的残留统计项仍然在。

**识别（只读，但需 superuser）。** `pg_stat_have_stats` 对 `nhms_display_ro` 不可执行，
最后一条要用 superuser 连接跑；前三条只读角色即可。

```sql
SELECT pg_postmaster_start_time();
SELECT pid, backend_start, wait_event FROM pg_stat_activity WHERE backend_type = 'autovacuum launcher';
SELECT max(last_autovacuum), max(last_autoanalyze) FROM pg_stat_user_tables;
-- 已删除库的残留统计项；扫的是 OID 空间，约 200 万个 OID 耗时数秒。
-- 上界取全局 OID 计数器：每个测试库消耗约 2,000 个 OID，本库 pg_class 的最大 OID 可能落后很多。
SELECT g AS dropped_database_oid
FROM generate_series(1, (SELECT next_oid::bigint FROM pg_control_checkpoint()) + 100000) AS g
WHERE pg_stat_have_stats('database', g::oid, 0)
  AND NOT EXISTS (SELECT 1 FROM pg_database d WHERE d.oid = g::oid);
```

- 最后一条有任何一行，且前三条符合上表第二行，即为本形态。
- 佐证：`docker logs nhms-db 2>&1 | grep -B1 'just been dropped'` 能看到对应时刻的 FATAL，
  最早一条的时间就是停转时刻。
- 残留统计项本身无害，只有留在**当前这个 launcher 进程**的名单上才会卡住它；没有清理它们的手段，也不需要清理。

**处置（superuser，不是只读操作，需 operator 批准）。**

```sql
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE backend_type = 'autovacuum launcher';
```

- postmaster 立即重生 launcher，新 launcher 的名单只从 `pg_database` 重建。约一分钟内应看到 worker 出现、
  `max(last_autovacuum)` 前移；之后 autovacuum 按现有 cost 限速消化欠账。
- 最脏的表可以顺手 `ANALYZE`（#2696 的做法），让规划器统计先恢复。
- **不要**用 `docker restart` 来"顺便重启"（§1）。
- 处置后若再次停转，先查 `docker logs nhms-db 2>&1 | grep -c 'nhms_it_'` 在处置之后是否又有增长：
  有，说明又有 integration 会话指向了本实例（§8 的工具与 `tests/conftest.py` 的拦截被绕过）；
  没有，则是别的 `DROP DATABASE` 来源，按上面的识别步骤留证后立单。

## 8. integration 测试用一次性 PostgreSQL 实例（#3376）

node-27 上的 integration pytest（`NHMS_RUN_INTEGRATION=1`）逐测试建删 `nhms_it_<uuid>` 库。
这些库**不得**建在本文的生产实例上（§7.1 的停转就是这样来的），而是建在
`scripts/node27_integration_pg.py` 起的一次性实例上：

| 项 | 值 |
|---|---|
| 容器 | `nhms-it-db`，镜像取自 `docker inspect nhms-db --format '{{.Image}}'`（与生产同一二进制） |
| 端口 | `127.0.0.1:55442`（`--port` 可改；`55432` 被拒绝） |
| 数据 | `/home/nwm/nhms-it-pg/`（`--root` 可改），`stop` 时整个删除 |
| 资源 | `--memory 8g --cpus 8 --shm-size 1g`，容器日志上限 2 × 20 MB，无 restart 策略 |
| 设置 | 沿用生产中会影响测试行为的几项（`max_locks_per_transaction=512`、`max_connections=100`、UTC、`shared_preload_libraries=timescaledb`）；持久性关闭（`fsync=off` 等），实例里的一切都是一次性的 |

在 `ci-test-routing.md` 的 detached-worktree lane 的 shell 里（`PATH` / `PYTHONPATH` / `TMPDIR` 已按该节设好）：

```bash
python scripts/node27_integration_pg.py start
python scripts/node27_integration_pg.py run -- python -m pytest -q -m integration -rs
python scripts/node27_integration_pg.py stop
```

- `run --` 把 `NHMS_RUN_INTEGRATION=1` 和指向该实例的 `NHMS_INTEGRATION_DATABASE_URL` 只放进子进程的环境，
  自己不打印任何东西；口令每次 `start` 重新生成，只存在实例目录里 `0600` 的 env 文件中。
- `run` 存活期间 `stop` 会拒绝（旁边的 `nhms-it-pg.lock` 文件锁）；确认无人在用后才用 `stop --force`。
- 工具只会删除带自己标签的 `nhms-it-db` 容器和带自己哨兵文件的目录；对 `nhms-db` 只做一次取镜像 ID 的 `inspect`。
- 实例可以在多次运行之间留着（占用以上表为限）；主机重启后它不会自启，重新 `start` 即可。
- `tests/conftest.py` 另有一道拦截：会话会跑 integration 且 URL 的端口是 `55432`（或 URL 没写端口而
  `PGPORT=55432`）时，pytest 以 usage error 直接结束，不跑任何测试。它拦的是文档化的生产入口，
  不是隔离手段：别手工把 integration URL 指向生产实例的任何地址。
- 口令并非对主机上所有人保密（docker 组成员可 `docker inspect`；pytest 的长 traceback 可能打印 fixture 值）。
  贴 receipt 前先在其中搜一遍口令。
