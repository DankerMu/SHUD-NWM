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

PGDATA 是 **bind mount**（`/home/nwm/nhms-pgdata`），所以重建容器不动数据。
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

## 5. `nhms_cold` 安装契约（#1894；不得在本 issue live 执行）

`nhms_cold` 仅是终态已压缩 chunk 的 DB 专用 tablespace，固定映射为：

| Host | Container | Catalog |
|---|---|---|
| `/data/GHDC/nhms-cold-tablespace` | `/home/postgres/pgdata/tablespaces/nhms_cold` | `nhms_cold` |

安装器 `scripts/node27_cold_tablespace_install.py` 默认 dry-run。它只在显式
`--enforce`、所有 descriptor-bound RAID/SMART/备份/容量 gate 新鲜通过、且
拓扑完全不存在时才允许改变容器或 catalog。完整且可读的既有拓扑是无写
`already_ready`；任何部分、漂移或未知状态都是 NO-GO，绝不尝试猜测性修复。

容器操作必须从 bounded inert `docker inspect` JSON 取得，并以 argv list 重建。
重建前先将旧 `nhms-db` 停止并 rename，保留为回滚权威；带精确 Env 的恢复
bundle 必须原子写入 mode 0600 私有路径，公开 receipt 只能保留字段名、摘要和
非秘密配置。不得 shell-source inspect 或 env，也不得将 password、DSN、signed URL
写入 stderr 或 receipt。新容器 bind ready 后才可执行：

```sql
CREATE TABLESPACE "nhms_cold"
  LOCATION '/home/postgres/pgdata/tablespaces/nhms_cold';
```

随后 fresh readback 必须同时证明 catalog location、`pg_tblspc` target、当前 bind
source、host device identity 和容器可写性；还必须证明两个业务 hypertable 没有
attach `nhms_cold`，新 chunk 仍在 `pg_default`。不得 attach tablespace，不得在此
步骤移动 chunk。

失败 rollback 仅可删除 installer 自己创建且无 dependents 的 catalog/container state，
然后恢复旧容器。只要 catalog、`pg_tblspc`、运行或 stopped container bind、path identity
或 emptiness 有任一引用/不确定，就不得删 host directory，亦不得将空目录 bind 到仍有
有效数据的 container path。

### #1894 disposable Docker oracle（仅 node-27、绝不使用 live identity）

此 oracle 不是 live install 命令；它只创建带 `nhms-1894-tablespace-` 前缀的唯一
container/prior 名、由内核临时分配的非 `55432` loopback port，及一个唯一临时 work root。它在任何 Docker、
filesystem 或 DB 操作前拒绝 `nhms-db`、`nhms-db-before`、`/data/GHDC`、
`/home/nwm/NWM`、`/home/nwm/nhms-pgdata`、活动 checkout 和全部 #1892 前缀。真实测试唯一命令是：

```bash
NHMS_RUN_NODE27_DOCKER=1 uv run pytest -q -m 'integration and timescaledb_210 and node27_docker' tests/test_node27_cold_tablespace_integration.py
```

前提是先量测 exact-SHA image 的默认 `postgres` uid/gid（不得把 `999` 或 `1000` 当作第二权威）；该对只是 image identity evidence。
synthetic container `--user`、host PGDATA 与 cold path owner 必须等于已证明的 host observer effective uid/gid，且 exact image 必须能以该 numeric identity 运行；不得把 image default 当成 runtime owner。
root capability 可为免交互 `sudo -n`，或在 sudo 不可用时由 exact-SHA image 的 isolated
`--user 0:0` helper 证明；后者仅可使用唯一 ownership-prefixed helper name、`--network none`、一个
owned work-root 到 `/nhms-owned` 的 RW bind，且不得携带 port、Docker socket、checkout 或 live path。
宿主 Python 只在该 owned root render synthetic documents；image 内不得执行项目 Python，只可 root seal
PGDATA/cold 为 proven host runtime owner、证据目录及四个 evidence 文件为 `root:reader_gid` ownership 和既定 mode。
证据仍须是 root-owned approved mode `0640`、uid `0` 的 synthetic `mdadm`、两个 SMART PASS 与 backup
inventory envelope，production parser 的 `expected_uid=0`、descriptor identity、hostname、command、subject/member
或 mode 校验不降低。测试 finally 必须先证明 current/prior/root-helper 均不存在及 port 可 bind；cleanup 仅可
删除 `pgdata`、`cold`、`evidence`、`receipts`、`postgres.env` 这些已知 child，未知 child 一律 refusal，随后宿主机
只能删除已经为空的 work root。清理失败即失败。此文不声明 node-27 remote PASS。

每一次 `--enforce` mutation 和 rollback 前，安装器都必须以固定 argv
`/usr/bin/systemctl --user --no-pager show <unit> -p ActiveState -p SubState -p Result`
重新读取以下六个 unit：autopipe service/timer、timeseries-compression service/timer、
timeseries-retention service/timer。仅 `ActiveState=inactive`、service `SubState=dead`
或 timer `SubState=waiting`、且 `Result=success|n/a` 可通过；active、failed、unknown 或
缺字段一律 NO-GO。#1894 不执行 stop/start：#1895 的人工执行顺序是先 drain 并确认上述
六个 unit 全部静止，再运行 installer；若需要 rollback，先再次确认静止，删除仅限已证明
installer-owned 的 replacement/catalog/path，rename/start prior `nhms-db`，最后 fresh
inspect catalog、`pg_tblspc`、bind、device、writability 和 prior config equality，才恢复
writer/timer 的原有启用状态。恢复或终态 receipt 发布失败时保留 private authority，绝不对
已完整 target 猜测性 rollback。

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
  才能验证，见 #1770。
