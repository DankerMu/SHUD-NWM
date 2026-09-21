**分册：业务流程与产物位置**

本页是当前生产值守手册的 §4、§5 开篇、§5.1-§5.6 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 4. 业务流程

当前物理流程按数据面理解：

```text
node-27 download timer
  -> downloads GFS/IFS raw cycles to shared NFS object-store
node-22 DB-free scheduler timer / Slurm
  -> consumes node-27 raw manifests from shared NFS
  -> submits per-basin GFS/IFS convert/forcing/forecast/state-save-QC work
     concurrently through Slurm Gateway/sbatch
  -> Slurm runs compute jobs on allocated compute nodes
  -> produces forcing and SHUD run artifacts
  -> writes shared NFS object-store/published roots
node-27 cron autopipe
  -> scans /home/ghdc/nwm/object-store/runs
  -> seeds basin registry when needed
  -> applies object-store forcing-domain handoff, registers/parses runs
  -> writes node-27 PostgreSQL :55432
  -> refreshes display coverage and publish status
node-27 display
  -> reads PostgreSQL :55432 and NFS object-store/published
  -> serves /, /ops, /api/v1/* through https://test.nwm.ac.cn
```

`scripts/node27_autopipeline.py` is idempotent. Already-seeded basins and
already-ingested runs are skipped, so cron re-runs are expected and cheap.
One run failure should appear in the JSON summary without aborting unrelated
run discovery.

## 5. 产物位置

### 5.1 数据库

当前 active NHMS DB 在 node-27 本机 `127.0.0.1:55432/nhms`。

数据库角色（#1774 起，写侧不再是 superuser）：

| 角色 | rolsuper | 用途 | 谁用它 |
|---|---|---|---|
| `nhms` | **t** | 库/扩展 owner + 迁移；两个 migration-class 例外 lane | `packages/common/migrate.py`；`node27-timeseries-compression-replay.env`（`pg_dump`/`psql --file`/`pg_restore`）；已退役的 archive-rebuild drill（需 `CREATEDB`） |
| `nhms_ingest_rw` | f | `core`/`hydro`/`met`/`ops`/`map`/`flood` 全部 relation 的 **owner** + DML + default privileges；ownership 是 `compress_chunk`/`drop_chunks`/chunk `ANALYZE` 与 #1643/#1468 统计守卫两条腿的硬要求 | `node27-ingest.env`、`node27-timeseries-compression.env`、`node27-timeseries-retention.env` |
| `nhms_download_rw` | f | 仅 `met.*` 的 DML + default privileges（下载链路实测**不连库**，角色存在是为了让模板承诺成立） | `node27-download.env` |
| `nhms_display_ro` | f | 只读；无 INSERT/UPDATE/DELETE | display API (`infra/env/display.env`)、frontier-alert、raw-retention、resource-governance |

选择性 cold runtime、其 env/template、以及 `nhms_cold` 的 source `CREATE` grant/positive audit 已从仓库代码退役；它们不是当前运行指导。此 source retirement 不是已执行的 live `REVOKE`、`DROP` 或生产配置交接声明。
有效部署与权限处置仍须按 R5.2 另获授权；不能根据本表自动撤权、删除 tablespace 或删除数据。

两个写角色的 `rolsuper`/`rolcreaterole`/`rolcreatedb`/`rolreplication`/`rolbypassrls`
全为 `f`，因此 `COPY … FROM PROGRAM`（= 容器内命令执行）对它们是关闭的。角色、授权与
ownership 由幂等脚本 `scripts/node27_provision_write_roles.sh`
（SQL: `db/roles/node27_write_roles.sql`）提供，**每次迁移后必须重跑**，否则新表仍归
`nhms` 所有、其 ANALYZE 会被静默跳过；完整口径与切换/回滚流程见
`docs/runbooks/tier-node27-timeseries-storage.md` §9。

当前宿主 PGDATA 已迁至 `/data/GHDC/nhms-primary/pgdata`，容器路径不变。
容器 `nhms-db` 由裸 `docker run` 创建（无 compose、无 systemd unit）。
以下区分当前 bind 与历史 `ghdc` 残留；若该历史 catalog/relations 仍在，必须保留其 bind，
不能据此重建退役车道或启用 `nhms_cold`：

| 宿主机路径 | 容器路径 | 设备 | 内容 |
|---|---|---|---|
| `/data/GHDC/nhms-primary/pgdata` | `/home/postgres/pgdata/data` | 配置目标实测 `device_identity`（不按路径前缀猜测） | 当前主 `pg_default` 表空间 |
| `/data/GHDC/nwm-archive/nhms-tablespace` | `/home/postgres/pgdata/tablespaces/ghdc` | 历史记录 `/dev/md0`；如仍有残留须现场确认 | 历史 `ghdc` overflow 残留，不是新冷层 |
| `/home/nwm/nhms-evidence` | `/var/lib/postgresql/evidence` | 宿主 `/home` 实测设备（独立于 PGDATA 容量证据） | evidence 输出 |

2026-08-06 的 `ghdc` overflow 与归档根共卷是**历史例外**，成因与当时条件见
`docs/adr/0002-node27-timeseries-hot-cold-tiering.md` "Amendment (2026-08-06)"。
归档车道已随 #1370 永久退役；历史表空间/归档统计偏差见 #1290，不把当时的
约 502 GB 当作当前体量，也不把 retained old PGDATA 当作第二个 current cluster。

容量核查仍须三个挂载点都看：`df -h / /home /data/GHDC`，并用 `psql` 实测。
Issue #2273 修正后的源码对配置 PGDATA 直接观察 available bytes（`statvfs.f_bavail`）
与设备身份，复用既有 `pgdata_root` 的 `du`；当前 receipt 的
`working_set_free_bytes` 和 `working_set_filesystem`（`path`、`device_identity`、
`status`、`blockers`）才是目标容量证据，独立 `/home` telemetry 不是比较输入。
目标 unavailable/ambiguous 必须产生 `WORKING_SET_FILESYSTEM_UNAVAILABLE` critical，
即使工作集为空也不能跳过；既有 `du` 不可用独立产生 `PGDATA_USAGE_UNAVAILABLE`。
目标峰值超过可用字节减 safety margin 时使用
`PROJECTED_PEAK_EXCEEDS_WORKING_SET_FREE`，恰好相等可容纳。不更改 lag 或阈值。
这是源码/模板契约修正，**不表示旧 pinned runtime 已部署修正或服务健康**；
I8 前须取得批准的 #2273 目标容量 receipt，不能用旧 `/home` PASS 代替。

重建 `nhms-db` 容器的流程见
`docs/runbooks/tier-node27-timeseries-storage.md` §4.3.3；**不要**拿
`infra/docker-compose.dev.yml` 当模板，那是本地 dev 栈。

`/` 是新加进这条核查的第三个挂载点（#1765）：一次跨两天的 pytest 把
`/tmp/pytest-of-nwm` 堆到把根卷塞满，而 `nhms-node27-resource-governance.service`
每天都量到了 `ROOT_FREE_BELOW_CRITICAL`、却仍然 exit 0 且没有 `OnFailure=`——
信号一直存在，结构上到不了人。现在该审计遇到任何 `critical` 建议会 exit 1 并向
stderr 打 `RESOURCE_GOVERNANCE_CRITICAL:<code>`，unit 带 `OnFailure=` +
`StandardError=journal`，锁也从 `/tmp` 挪到了 `$LOG_ROOT`；但**这些只在
`install ~/NWM/infra/systemd/nhms-node27-resource-governance.service
~/.config/systemd/user/` + `systemctl --user daemon-reload` 之后才生效**
（node-27 的 unit 是 user-scope，`git pull` 只换 `ExecStart` 背后的脚本）。

在 node-27 上跑 pytest 前先 `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`，
否则临时目录仍然落在 `/`。`mkdir -p` 不能省：`TMPDIR` 指向不存在的目录时
Python 会**静默回落**到 `/tmp`，跑完看着一切正常、根卷却又少了一块。核查看
`ls -d /home/nwm/tmp/pytest-of-nwm`，不要只看 `df`。仓库侧的另一半是
`pyproject.toml` 的 `tmp_path_retention_policy = "failed"`（只有失败的测试留下
`tmp_path`）；**不要**在共享配置里加 `--basetemp`，那会连本地 Mac 和 CI 一起清。

Secret-safe DB checks:

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a

psql "$DATABASE_URL" -P pager=off -Atc "
select current_database(), current_user, inet_server_addr(), inet_server_port();"

psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;"
```

Common tables:

| Schema / table | 用途 |
| --- | --- |
| `hydro.hydro_run` | 每个 source/model/basin 的水文 run 状态 |
| `hydro.river_timeseries` | q_down 等河段时序 |
| `hydro.run_display_coverage` | latest display fast path coverage |
| `met.forecast_cycle` | source cycle 状态 |
| `met.forcing_version` | forcing 包索引 |
| `ops.pipeline_job` | 阶段 job 状态 |
| `core.basin_version` / `core.river_segment` | 流域、河段、几何和输出段 |
| `map.tile_layer` | 发布图层登记 |

### 5.2 Workspace 和运行日志

node-27 ingest wrapper/log:

```text
/home/nwm/NWM/scripts/node27_autopipe_cron.sh
/home/nwm/NWM/scripts/node27_autopipeline.py
/home/nwm/autopipe-logs/autopipe.log
/home/nwm/autopipe-work/
```

node-22 compute workspace/log roots remain compute-side operational paths:

```text
/scratch/frd_muziyao/NWM
/scratch/frd_muziyao/nhms-prod/workspace/
/scratch/frd_muziyao/nhms-prod/object-store/
/scratch/frd_muziyao/nhms-prod/runtime/
```

Use node-22 paths for Slurm/job runtime troubleshooting. Use node-27 paths for
DB/display/ingest troubleshooting.

### 5.3 Object-store mirror

Complete forcing packages and run outputs live under shared object-store:

```text
node-22 view: /ghdc/data/nwm/object-store
node-27 view: /home/ghdc/nwm/object-store

forcing/<source>/<YYYYMMDDHH>/<basin_version_id>/<model_id>/
runs/<run_id>/
```

Check current visibility from both hosts:

```bash
# node-22
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'stat -c "%n %A %U:%G" /ghdc/data/nwm/object-store &&
   find /ghdc/data/nwm/object-store/runs -maxdepth 1 -type d \
     -printf "%TY-%Tm-%Td %TH:%TM %p\n" | sort | tail -20'

# node-27
ssh -p 32099 nwm@210.77.77.27 \
  'stat -c "%n %A %U:%G" /home/ghdc/nwm/object-store &&
   find /home/ghdc/nwm/object-store/runs -maxdepth 1 -type d \
     -printf "%TY-%Tm-%Td %TH:%TM %p\n" | sort | tail -20'
```

#### Copyback batch mutex 与目录可穿越性（#2035）

写侧（node-22）与读侧（node-27）是不同 uid、同一份 NFS，因此这个 mirror 有两条硬规则：

1. **互斥**。所有会在 `NHMS_OBJECT_STORE_COPYBACK_ROOT` 下 promote 目录树的写者
   （publisher 的 q_down / run-products / canonical-precip 三条 lane、orchestrator 的
   run-tree copyback、以及两个 backfill CLI）都先取
   `$NHMS_OBJECT_STORE_COPYBACK_ROOT/.nhms-copyback-batch.lock` 上的排他 `flock`，
   一直持有到本 batch 的 commit 或 rollback 返回。删侧同样持这把锁：node-22 retention
   在 copyback root 上逐棵删树（#2238），node-27 raw retention 逐棵删
   `canonical/<S>/<cycle>`（#2252，用 POSIX 记录锁，见下文「node-27 canonical 删除持锁」）。路径固定、**没有环境变量覆盖**：
   放 `/tmp` 会被 systemd `PrivateTmp=true` / Slurm `job_container/tmpfs` 的私有
   `/tmp` 拆成两个 inode，互斥静默失效。
   - 锁文件 `0o600`、代码**从不 unlink**——这两条由代码直接强制
     （`packages/common/copyback_guard.py` 的 `_LOCK_MODE`，以及整个模块里没有
     任何 unlink 调用）。**「属主是写者本人」不是独立强制的不变量，而是推论**：
     代码断言的是「锁文件属主 == copyback root 属主」和「当前 euid == 锁文件属主」，
     现网所有写者恰好是同一个 uid，外来 uid 又在文件创建之前就被拒，两者才重合。
   - 持有者被 kill 时**本机**内核会释放 flock，所以「锁文件存在」≠「锁被持有」。
     但生产 copyback root 不在本机盘上：node-22 的 `/ghdc/data/nwm/object-store`
     是 `ghdc:/home/ghdc` 的 **NFSv4.2** 挂载（2026-09-10 实测）。**进程**死照样
     立刻释放；**整台主机**死不会——锁由服务端一直持有到该 client 的租约过期。
   - 等待上限由 `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` 控制，
     **默认 900 秒**（一次 acquisition ≈ 2.2 GB / 62 MB/s ≈ 36 s；取锁次数与
     900 s 的定法只写在 `packages/common/copyback_guard.py` 的
     `DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS` 注释里，那套算术偏保守，故意不回调）。
     竞争是等待不是拒绝；
     超时抛各 lane 自己的错误类型，canonical mirror 记一条 `failed` 的
     `canonical_precip_mirror` receipt 后 cycle 继续，**绝不降级成无锁 promote**。
     空值取默认，非数字/非正数是硬性配置拒绝。
   - 所有写者必须同 uid（node-22 上是 `frd_muziyao`）；别的账号会 fail closed。
     属主断言同时比对**当前 euid** 与 **copyback root 的属主**，并且外来 uid 在
     `O_CREAT` 之前就被拒——两个方向都堵死，锁文件不会被别的账号「毒化」。
     跨主机的互斥靠**锁原语**成立（2026-09-14 实测，ADR 0008）：node-22 NFS client 的
     `flock` 与 node-27（NFS server 本地文件系统）的 POSIX 记录锁双向互斥；node-27 本地
     `flock` 与 node-22 `flock` **不**互斥。因此 node-27 上任何取这把锁的进程都必须用
     `primitive="posix"`，node-22 写者保持默认 `flock`。
   - **`NHMS_OBJECT_STORE_COPYBACK_ROOT` 必须由那个唯一的写者 uid 属主持有。**
     条件是 **uid 相等**，不是「写者能写这个 root」：一个属主是别的账号、靠组位开放写
     的 root（例如容器 uid 在补充组里）会被**拒绝，而不是共享**——锁文件的属主锚定在
     root 的属主上。核查：`stat -c '%u %a %n' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"`
     与写者的 `id -u` 对比；不相等则每次 copyback 都 fail closed。报错有两种形态，
     都不是 timeout：
     - **锁文件还不存在**、写者又不是 root 的属主 → 在创建之前就拒绝，报错里同时
       给出两个 uid 和锁文件路径；
     - **锁文件已存在且属主是别人** → `cannot acquire copyback batch lock <path>:
       [Errno 13] Permission denied`，只有路径、没有 uid。这个 `Permission denied`
       本身就是「属主不对」的信号；属主由下面的 `ls -ln <lock>` /
       `stat -c '%u' <root>` 查出来。
   - **锁文件卡住时怎么处置**（三种形态，只有一种该动）：
     - **有活持有者**：`lsof <lock>` 或 `fuser -v <lock>` 打得出 pid → **不要动它**，
       等它结束或去查那个 writer 为什么卡住。
     - **属主是对的、本机查不到持有者、写者却仍然超时**：只会在 NFS root 上出现，
       说明持锁的那台主机整个死了，锁要等服务端租约过期才释放。**同样不要动它**：
       unlink 会让下一个 writer 去锁一个新 inode，互斥当场失效。等租约过期，或者
       先去确认那台写者主机的状态——这不是「孤儿」，下面那条不适用。
     - **属主不对的孤儿**：`ls -ln <lock>` 的属主 ≠
       `stat -c '%u' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"`，且没有任何进程持有 →
       必须修，否则所有 writer 会永远 fail closed。
       `sudo chown "$(stat -c '%u:%g' "$NHMS_OBJECT_STORE_COPYBACK_ROOT")" <lock>`
       是无条件可用的一条；也可以直接 `rm -f <lock>` 让下一个 writer 重建——
       **删除权限来自 copyback root 目录的写+执行位，不是锁文件的属主身份**
       （POSIX unlink 语义），前提是这条路径上没有 sticky bit。2026-09-10 node-22
       实测：`/ghdc` 755 root:root、`/ghdc/data` 777 root:root、`/ghdc/data/nwm`
       与 `/ghdc/data/nwm/object-store` 都是 775 `frd_muziyao`(1103):`huser`(1078)，
       整条链上**没有 sticky bit**，所以 gid 1078 的任何成员都能删，不必去找锁文件
       属主的凭据。若哪天 `ls -ld` 在这条链上看到 `t`，就退回 `sudo chown`。

       ```bash
       ssh -p 32099 frd_muziyao@210.77.77.22 \
         'L=/ghdc/data/nwm/object-store/.nhms-copyback-batch.lock;
          ls -ln "$L"; stat -c "%u %n" /ghdc/data/nwm/object-store; fuser -v "$L" || echo "no holder"'
       ```

   - **`canonical_precip_mirror` 记了 `failed` receipt 怎么补**：多数情况下会自动补，
     手工 backfill 只留给此后再无 pass 的 cycle。
     吞异常的是 `_copyback_canonical_precip`（`services/tile_publisher/publisher.py`）
     自己的 `except Exception`：它不抛，直接返回一份 `status: "failed"` 的 summary，
     `convert` 终态 hook 在该入口的 cycle 状态写入**之后**（#2070）把这份 summary 写成
     receipt，cycle 照常往下走。
     hook（`_mirror_canonical_precip`）自己的 `except Exception` 根本
     见不到 copyback 失败——它兜的是 publisher 那个 `try:` **之外**抛出的东西：
     `format_cycle_time`、`TilePublisher(...)` 构造。publisher `finally` 里的
     `release_copyback_batch_lock`（`packages/common/copyback_guard.py`）不会抛——
     unlock 与 close 的 `OSError` 都在函数内记 warning 后吞掉。
     重试靠 `_run_cycle_chain` 出口的 chain-exit recovery（#2076），前提是
     copyback root 已配、本地 `canonical/<S>/<cycle>/prcp_rate_or_amount/` 仍在
     （任一路径分量为 symlink 视为不在，retention 剪掉后不再补也不发 receipt）：
     - **同一 pass**：hook 记了 `failed`，该 pass 退出时（正常返回或抛异常）再镜像一次；
     - **后续 pass**：之后经过该 cycle 的每个下游 restart pass（`restart_stage` 在
       `convert` 之后，如 `forecast`）退出时都会再镜像一次，树已一致则记 `skipped`、不重写。
     重试记的仍是 `canonical_precip_mirror` receipt，`message` 为
     "Canonical precipitation mirror recovered at chain exit."（判读见
     `two-node-deployment-overview.md` §7.4）。它只搭**这个 cycle** 自己的 pass，
     不会被下个 cycle 顺带补上。hook 与同 pass 重试都失败、且此后该 cycle 再无任何
     pass（链已完成、retry 用尽）时，才需要手工跑 backfill：

     ```bash
     ssh -p 32099 frd_muziyao@210.77.77.22 \
       'cd /scratch/frd_muziyao/NWM &&
        . infra/env/compute.scheduler-dbfree.env &&   # 同时带 OBJECT_STORE_ROOT 与 NHMS_OBJECT_STORE_COPYBACK_ROOT（见下方实测 receipt）
        /scratch/frd_muziyao/NWM/.venv/bin/python \
          -m scripts.canonical_precip_copyback_backfill \
          --source-root "$OBJECT_STORE_ROOT" \
          --copyback-root "$NHMS_OBJECT_STORE_COPYBACK_ROOT" --dry-run'
     ```

     `cd` 到仓库根是必须的（否则 `-m` 会 `ModuleNotFoundError` 退 1）。先看 dry-run 计划
     （dry-run 不取锁、不写任何东西），确认无误后去掉 `--dry-run` 实跑。退出码：`0` 全成功、
     `1` 跑完但有 `failed`、`2` 参数或 root 不可用——两个 root 必须都非空（空值会被
     `resolve_roots` 当参数错误拒绝，退 2），所以只能 source 同时带这两个变量的
     `compute.scheduler-dbfree.env`（`compute.scheduler-provider-refresh.env` 只有
     `OBJECT_STORE_ROOT`）。

     该文件不在版本管理里，所以**不能拿 `.example` 当证据**。2026-09-10 在 node-22
     实读 `/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env`
     （`-rw-------` `frd_muziyao:huser`）确认两个变量都在：

     ```
     13:OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
     15:NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
     ```

     同日全量扫描：带 `NHMS_OBJECT_STORE_COPYBACK_ROOT` 的活文件只有
     `compute.host.env`、`compute.replay.env`、`compute.scheduler-dbfree.env` 三个。
   - **run-tree copyback 留下了 backup（`OBJECT_STORE_COPYBACK_BACKUP_RETAINED`，#2237）**：
     `services/orchestrator/run_tree_copyback.py` 的 `_replace_tree` / `_replace_file` 先把旧
     target rename 成同目录下隐藏的 `.<name>.copyback-<hex>.backup`，promote 失败且没能
     rename 回去时（`_raise_if_backup_retained`）保留它并抛这个 code。
     - **识别**：两种载体、字段层级不同。`object_store_copyback` 的 `failed` pipeline event
       （node-22 journal）里是 `details.error_code` 为上述 code、字段在 `details.details.*`；
       stage 抛出的 `OrchestratorError` 里是 `.code`、字段在 `.details.*`（浅一层）。字段为
       `target`、`backup_path`、`error`（promote 失败）、`restore_error`（回滚失败；未尝试回滚
       时为 `null`）。target 可能是 `runs/<run_id>`、run 引用的对象树，或一个 extra object 文件。
     - **含义**：`backup_path` 里是 target 的**旧内容**，而且很可能是唯一一份。target 要么
       不存在（回滚 rename 失败），要么已被别的东西占住（没有尝试回滚）。
     - **没有任何东西会回收它**，两条 lane 分开看：`runs/` 下的 backup 是点开头的名字，node-22
       retention 记成 `unparseable_run_cycle` 跳过；`forcing/**` 引用树与 extra object 旁的
       backup 不在任何 retention 目标里——node-22 对 copyback root 只按 `runs_only_roots` 扫
       `runs/`（`raw|canonical|forcing/<source>/<cycle>` 只在 primary root 上扫，`services/orchestrator/retention.py`），
       node-27 `scripts/node27_raw_retention.py` 明确不碰 `forcing`、`runs`。下一次 copyback 直接 promote、不看它。
     - **处置**（node-22、账号 `frd_muziyao`，即 copyback root 属主；node-22 是 NFS client，
       `flock` 与所有写者互斥。**从 node-22 做**，node-27 本地 `flock` 与之不互斥）：全程持
       batch mutex，期间不在这个 copyback root 下做别的。锁文件可能已被上文「属主不对的孤儿」
       处置删掉；锁文件不存在时 `flock(1)` 会以 `0666 & umask` 新建它，之后每个 copyback 写者都
       fail closed（`copyback batch lock must have mode 0600`）。所以两条命令都先核对锁文件存在、
       不是符号链接（`! -L`；`-f` 与 `stat` 都跟随符号链接）、是普通文件（`-f`）且为 `600` + uid 1103，
       不符就拒绝执行、绝不新建；再在锁内复核 target 状态，状态不符就什么都不做。不用 `stat -c %F`
       判类型：锁文件是 0 字节，GNU stat 对它报 `regular empty file` 而非 `regular file`（2026-09-14
       node-22 实测 `stat -c '%F %a %u' "$L"` -> `regular empty file 600 1103`）：

       ```bash
       ssh -p 32099 frd_muziyao@210.77.77.22
       L=/ghdc/data/nwm/object-store/.nhms-copyback-batch.lock
       T='<target>'; B='<backup_path>'   # 取自事件 details.details.* 或 OrchestratorError .details.*
       # target 不存在：把 backup 放回原名（mv -T 防止 target 期间出现时被搬进去）
       [ ! -L "$L" ] && [ -f "$L" ] && [ "$(stat -c '%a %u' "$L")" = "600 1103" ] \
         && flock -w 900 "$L" sh -c 'test ! -e "$1" && mv -T "$2" "$1"' _ "$T" "$B" \
         || echo "lock file missing/wrong identity, lock timeout, or target present; nothing done"
       # target 已存在且已确认是更新的成功 copyback（见下）：删 backup
       [ ! -L "$L" ] && [ -f "$L" ] && [ "$(stat -c '%a %u' "$L")" = "600 1103" ] \
         && flock -w 900 "$L" sh -c 'test -e "$1" && rm -rf -- "$2"' _ "$T" "$B" \
         || echo "lock file missing/wrong identity, lock timeout, or target absent; nothing done"
       ```

       删之前先确认 target 是一次**更新的、成功的** copyback 写出的（同一 run 之后有
       `status_to` 为 `copied` 的 `object_store_copyback` 事件）且内容完整；确认不了就保留
       backup、别动。放回或删除之后，如该 run 仍需要 copyback，再按该 cycle 的正常重跑路径
       重做（这条 lane 没有单独的 CLI）。
     - **target 不存在时绝不删 backup**——那等于删掉这棵树唯一的一份。
   - `services/orchestrator/retention.py` 只下钻 `root/<prefix>` 与 `root/runs`，
     不枚举 root 级文件，所以这把锁对保留策略不可见。
2. **可穿越性**。copyback 自己创建的每一级目录——**包括 copyback root 本身**——
   都会被显式 `chmod 0o755`；`safe_fs` 传给 `mkdir` 的 `0o755` 会被进程 umask 收窄
   （`umask 027` 下落成 `0o750`），而 `safe_fs` 按 #1513 的决定绝不事后 `chmod`。
   **本调用没有创建的层级一律不动**。手工 bring-up shell 里跑 copyback 时不再需要
   先 `umask 022`，但这条规则只覆盖 copyback 创建的层级，不修既有目录的历史模式。
   **唯一例外**：canonical 降水镜像 lane 自己拥有的三级目录（`<cycle>/`、
   `<cycle>/prcp_rate_or_amount/`、`grid/<grid_id>/`）是 `2775` 而不是 `0755`，
   且 `<cycle>/` 无论是否由本次调用创建都会被断言成该模式——见下一小节（#2100）。

快速核查（node-27 读侧，用 display 账号真正读到字节才算数）：

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'stat -c "%a %n" /home/ghdc/nwm/object-store /home/ghdc/nwm/object-store/canonical &&
   f=$(find /home/ghdc/nwm/object-store/canonical -type f | head -1); cat "$f" | wc -c'
```

#### canonical 降水镜像的跨账号删除权限（#2100）

写侧是 node-22 的 `frd_muziyao`，删侧是 node-27 retention unit 里的 `nwm`——同一份 NFS、
两个 uid。两端都存在、且两个账号都在里面的组只有 `nwmuser`（gid **1107**），所以镜像目录的
口径是 **`2775` + gid 1107**：

- 组写位给的是**删除权**。`rmtree` 先 unlink 文件（要 `prcp_rate_or_amount/` 的 `w+x`），
  再 `rmdir` 它（要 `<cycle>/` 的 `w+x`），最后 `rmdir <cycle>/`（要 `canonical/<S>/` 的
  `w+x`）。unlink 看的是**目录**的位，不是文件的位，所以文件一律留 `0644`。
- setgid 位让新建的下一级继承父目录的组（Linux 语义；macOS 无条件继承），这样 node-22 新
  镜像出来的 cycle 自动落在 1107，而不是写者的 egid 1078。
- 每一级的 `2775` 都是**显式写上去的**：`os.chmod` 会清 setgid，所以代码里没有任何地方依赖
  "这个位能活过一次 chmod"。产出侧两个常量同值——`services/tile_publisher/publisher.py`
  的 `CANONICAL_MIRROR_DIRECTORY_MODE` 与 `scripts/canonical_precip_copyback_backfill.py`
  的 `DIR_MODE`，都是 `0o2775`，且都只作用于自己**拥有**的目录（publisher 对 `<cycle>/`
  无论是否由本次调用创建都断言，见下）。两个产出侧的分工不同，按真实口径写：
  - publisher 用 copyback 通用 helper 建穿越层（`canonical/`、`canonical/<S>/`、
    `canonical/<S>/grid/`），落 `0755`；`2775` 落在它自己拥有的层——`<cycle>/`、本次
    copyback 的临时树根、以及复制进来的树内各级（`prcp_rate_or_amount/`、
    `grid/<grid_id>/`）。`canonical/<S>/` 与 `grid/` 这两个穿越层的 `2775` 由下面那段
    **存量扫描**收敛——那就是它们的稳态。
  - backfill 脚本建 `canonical/` 时落 `0755`（`MIRROR_ROOT_MODE`），`canonical/` 以下它
    创建的每一级（`<S>/`、`grid/`、`<cycle>/`、`prcp_rate_or_amount/`、`grid/<grid_id>/`）
    落 `2775`。
- **`canonical/` 自己不扫**，保持 `755` gid 1078（该 gid 在 node-27 上叫 `nfsdata`、空组，
  在 node-22 上叫 `huser`）——它是唯一一级 producer 与扫描**都不放权**的目录（backfill 新建
  它时也是 `0755`）。于是将来新增的 storage source 是 **fail closed**：第一个
  unlink 就被拒、零字节、`failed[]` 里一条 `PermissionError`，而不是删一半的形状。

**存量树扫描（node-22，账号 `frd_muziyao`；每个 source 一次，幂等）：**

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'cd /ghdc/data/nwm/object-store &&
   for S in gfs IFS; do
     chgrp -R 1107 "canonical/$S" &&
     find "canonical/$S" -type d -exec chmod 2775 {} + ;
   done'
```

`find` 是先根后叶（pre-order），父目录一定先于子目录被放权，所以任何一个瞬间都不会出现
"`prcp_rate_or_amount/` 可写而 `<cycle>/` 不可写"的半删形状；扫到一半中断也只是让剩下的
cycle 继续干净地被拒，重跑即可。**用数字 gid**：1078/1107 在两台机上映射到不同的组名。

核查（同样在 node-22，`cd` 到 object-store 根）：

```bash
find canonical/gfs canonical/IFS -type d ! -perm -2775 | wc -l   # 期望 0
find canonical/gfs canonical/IFS ! -group 1107 | wc -l           # 期望 0（含文件）
stat -c '%a %U %g' canonical                                     # 期望 755 frd_muziyao 1078
```

**新增 storage source 的前置条件**：把 `<S>` 写进 `NODE27_RAW_RETENTION_SOURCES` 之前，
先扫 `canonical/<S>`。顺序反了不会丢数据，但那条 lane 每个 tick 都会报一条
`PermissionError`（零字节），直到补扫。

**部署后必须补扫一遍**（不是可选项）：producer 只在"这棵树确实需要复制"时才纠正
`<cycle>/` 的模式；目标字节完全相同的 cycle 在 plan 阶段就被整体 `skipped`，**不会被治愈**。
扫描完成到 node-22 拉起新 producer 之间镜像出来的 cycle 正是这一类（`<cycle>/` `0755` gid
1107、`prcp_rate_or_amount/` `0755` gid 1078，删除时干净被拒）。把上面那段扫描原样再跑一次
即可，幂等。

**回滚**（两条，和上面对称）：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'cd /ghdc/data/nwm/object-store &&
   chgrp -R 1078 canonical/gfs canonical/IFS &&
   find canonical/gfs canonical/IFS -type d -exec chmod 00755 {} +'
```

`00755` 的**五位数字是必须的**：`chmod(1)` 对目录用数字模式时默认保留 setgid，`chmod 0755`
会留下 `2755`（这是 `chmod(1)` 的行为，不是 `chmod(2)` 的——`os.chmod(dir, 0o755)` 直接清位）。
producer 侧回滚就是对该 commit 做 `git revert`。

**回滚的中间态同样安全**，理由和正向扫描对称但方向相反：`chgrp -R` 是**后序**（先子后父，
GNU coreutils 与 BSD 的实现都只在 `FTS_DP` 上动手），所以收权过程中任一瞬间只会出现
"子目录已被拒、父目录仍可写"，永远不会出现被禁的反形状（`prcp_rate_or_amount/` 可写而
`<cycle>/` 不可写）。`chgrp` 跑完整棵树已经全拒，后面那条 `find ... chmod`（先根后叶）
无论顺序都不再改变可删性。因此回滚中断后原样重跑即可，同样幂等。

**影响面**：gid 1107 `nwmuser` 的所有成员都获得了对这两棵镜像树的写/删权——node-27 上是
`nwm` 与 `frd_muziyao`，node-22 上是包括 `frd_muziyao` 在内的七个人类账号。授权范围仅限
`canonical/gfs`、`canonical/IFS` 两棵树的目录位；`canonical/` 本身、`runs/`、`forcing/`
以及所有文件位都不动。

#### node-27 canonical 删除持锁（#2252 / #2239 / #2262 / #2360）

node-27 上 `/home/ghdc/nwm/object-store` **就是** node-22 写者 promote `canonical/` 的那个
共享 copyback root，所以 `scripts/node27_raw_retention.py` 删每个
`canonical/<S>/<cycle>` 前都取 `.nhms-copyback-batch.lock`：POSIX 记录锁（`fcntl.lockf`），
逐棵取、只罩住那一次 `rmtree`，整个 pass 共用 300 s 等锁预算；规划遍历不持锁。
`raw/` 与 precip PNG 缓存**不取锁**——没有任何持锁写者往里 promote。disabled、plan-only、
preflight-blocked 的 tick 一次都不取、也不建锁文件。summary schema 升到
`nhms.node27_raw_retention.production.v5`。

`failed[]` 里锁失败的条目带 `error`、`error_type` 和 `lock_failure`，summary 顶层带
`copyback_lock_failures`（三个计数，恒有、没有失败时全 0）。node-22 scheduler pass receipt 的
`retention` 块同样带这两样，且 `copyback_lock_failures` 在 receipt 压缩后仍保留：

| `lock_failure` | 含义 | 处置 |
|---|---|---|
| `lock_timeout` | 别的进程持锁超过本 pass 剩余预算 | 树保留，下个 tick 重试；持续出现就去查 node-22 哪个写者卡住（上文「锁文件卡住时怎么处置」） |
| `lock_budget_exhausted` | 本 pass 预算已被前面的等待耗尽，这一条没有尝试取锁 | 同上，跟着 `lock_timeout` 一起看 |
| `lock_unsafe` | 锁文件不安全、打不开或配置被拒（属主/模式/硬链接/符号链接不对，或本账号无权打开） | 不是 busy；**不要删锁文件**（会把互斥拆成两个 inode），按下面的身份现状处理 |

**身份：canonical 车道拆到系统 unit，以 copyback root 属主运行（#2360，2026-09-18）**。
锁文件 `0600`、属主是 copyback root 属主 `frd_muziyao`(1103)；锁身份契约**不放宽**（组共享
`0660` 会被 node-22 在 #1831 前的现网代码拒掉，打断所有写者），锁文件的模式/属主本次也不动。
所以 retention 按车道拆成两个 unit，靠 `NODE27_RAW_RETENTION_LANES` 选车道：

| unit | 作用域 / 身份 | `NODE27_RAW_RETENTION_LANES` | env | summary 目录 | 失败告警 |
|---|---|---|---|---|---|
| `nhms-node27-raw-retention.{service,timer}` | nwm user unit，`nwm`(1005) | `raw,precip-cache` | `/home/nwm/NWM/infra/env/node27-raw-retention.env` | `/home/nwm/node27-raw-retention-logs/` | `OnFailure=nhms-node27-unit-failure-alert@%n.service`（user journal） |
| `nhms-node27-canonical-retention.{service,timer}` | 系统 unit，`User=frd_muziyao`(1103) | `canonical` | `/etc/nhms/node27-canonical-retention.env`（安装脚本生成） | `/var/log/nhms-node27-canonical-retention/`（0755，nwm 可读） | `OnFailure=nhms-node27-system-unit-failure-alert@%n.service`（以 nwm + `systemd-journal` 跑同一个 handler，`NHMS_UNIT_FAILURE_JOURNAL_SCOPE=system` 读 system journal） |

- 两个 timer 都是 `03:35:00 UTC`，同一条 cutoff 规则（同一 display watermark、同一
  `NODE27_RAW_RETENTION_DAYS`），一个 cycle 的 canonical 镜像和它的 PNG 缓存在同一天到龄，
  只是由两个进程分别删、不是原子的：一个 unit 失败，另一个车道照剪（与单进程内车道互相隔离的
  既有语义一致）。
- 未选中的车道在 `skipped[]` 里恰好一条 `{"key": <lane>, "reason": "lane_not_selected"}`，
  根目录不探测、不列举、不取锁；summary 带排好序的 `lanes`，看它就知道是哪个 unit 写的。
  `LANES` 取值为空或含未知名字 → preflight `lanes` blocker，rc=2，一个字节都不删。
- 为什么不把整个 unit 换成 `frd_muziyao`：precip PNG 缓存根 `/home/nwm/.cache/nhms/mvt` 是
  `nwm:nwm 775`，uid 1103 删不动——fail-closed 只会换一条车道。
- 拆分后 canonical 不再出现 `lock_unsafe`；再出现就是车道跑错了账号（nwm env 丢了 `LANES`
  行，或系统 unit 的 `User=` 被改了），按事故处理，**不要删锁文件**。
- `infra/env/node27-raw-retention.example` 的 `counts.failed == 0` 判据在拆分安装后恢复，两份
  summary 都适用；安装前 nwm unit 的到龄 canonical `lock_unsafe` 让它恒红（已知 #2360 形态）。
- 容量：canonical 镜像 14 天两个源实测 3.7 GiB，对 `/home` 余量无压力——判定时仍以
  `df -h` 实测为准。

**一次性安装（运维 sudo）**。前提（按顺序）：先在 nwm env 加 `NODE27_RAW_RETENTION_LANES=raw,precip-cache`（安装脚本强制
检查这一行，缺失或含 `canonical` 即拒绝），repo 已 `git pull --ff-only`；再以 nwm 装 repo 的 nwm unit 并 reload：
`install -m 0644 /home/nwm/NWM/infra/systemd/nhms-node27-raw-retention.service ~/.config/systemd/user/ && systemctl --user daemon-reload`，
`diff ~/.config/systemd/user/nhms-node27-raw-retention.service /home/nwm/NWM/infra/systemd/nhms-node27-raw-retention.service` 无输出、
`systemctl --user show -p OnFailure nhms-node27-raw-retention.service` 列出 `nhms-node27-unit-failure-alert@` 模板；然后：

```bash
sudo /home/nwm/NWM/scripts/node27_canonical_retention_install.sh
```

脚本先做 fail-closed 前置检查（root；`frd_muziyao` 是 uid 1103；object-store 根与
`.nhms-copyback-batch.lock` 属主 1103、锁文件已存在且 `600`——**从不创建**；三份 repo unit
存在；源 env 存在、`600`、非符号链接，且恰好一行 `NODE27_RAW_RETENTION_LANES` 只含
`raw`/`precip-cache`、恰好一行 `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store`
（按文本读、不 source；接受 `[export ]KEY=value`，值可带一对引号）；以 `frd_muziyao` 身份
import 探针通过），任一不过就
rc≠0 退出、什么都不写。然后：生成 `/etc/nhms/node27-canonical-retention.env`（从 nwm env 去掉
`LANES`/`LOG_ROOT`/`LOCK_PATH`/`LOG_FILE`/`SUMMARY_PATH`/`BOOTSTRAP_LOG` 行，追加
`LANES=canonical`、`LOG_ROOT=/var/log/nhms-node27-canonical-retention`、
`LOCK_PATH=/run/nhms-node27-canonical-retention/raw-retention.lock`；属主 `frd_muziyao`、`600`；
不打印任何值）→ `install -m 0644` 三份系统 unit 到 `/etc/systemd/system/` → `daemon-reload` →
`enable --now` timer → 同步 `start` 一次 service，打印 `User,Result,ExecMainStatus`、timer 状态、
最新 summary 的 `lanes`/`counts`/`copyback_lock_failures` 和锁文件 `%A %U`；service
`Result` 不是 `success` 就 rc≠0。

**改了 nwm raw-retention env 之后必须重跑同一条 sudo 命令**：系统 unit 的 env 是安装时的快照，
不会跟着变（比较两份 summary 的 `retention_days`、`sources`，不等就重跑安装脚本）。

**回滚**（回到"canonical 不删"的状态；锁文件不动）：

```bash
sudo systemctl disable --now nhms-node27-canonical-retention.timer
sudo rm -f /etc/systemd/system/nhms-node27-canonical-retention.service \
           /etc/systemd/system/nhms-node27-canonical-retention.timer \
           /etc/systemd/system/nhms-node27-system-unit-failure-alert@.service \
           /etc/nhms/node27-canonical-retention.env
sudo systemctl daemon-reload
# 再从 /home/nwm/NWM/infra/env/node27-raw-retention.env 删掉 NODE27_RAW_RETENTION_LANES 行。
```

删掉 `LANES` 行后 nwm unit 回到三车道：每个到龄 canonical cycle 又会记 `lock_unsafe`、tick
rc=1、经 `OnFailure=` 告警——这是回滚后的已知形态，不是新故障。

**核查（两个 summary 目录都要看）**。unit 级 `OnFailure=` 只覆盖 rc≠0；canonical 的
`*_unsafe` 跳过（车道/源根不可达）和过期 summary（tick 没跑或在写 summary 前崩了）都是 rc=0，
**永远到不了 `OnFailure=`**，只有下面的检查能看到：

```bash
ssh -p 32099 nwm@210.77.77.27 '
  red=0
  for d in /home/nwm/node27-raw-retention-logs /var/log/nhms-node27-canonical-retention; do
    f=$(ls -t "$d"/raw-retention-*.json | head -1); echo "== $f"
    jq "{lanes, execution_mode, finished_at, counts, copyback_lock_failures,
         failed: [.failed[] | {key, lock_failure, error_type}],
         unsafe: [.skipped[] | select(.reason | endswith(\"_unsafe\"))]}" "$f"
    jq -e "(.execution_mode == \"production_execute\")
           and ((.finished_at | fromdateiso8601) > (now - 26*3600))
           and ([.failed[]] | length == 0)
           and ([.skipped[] | select(.reason | endswith(\"_unsafe\"))] | length == 0)" "$f" >/dev/null \
      || { echo "RED: $d"; red=1; }
  done
  systemctl status nhms-node27-canonical-retention.service --no-pager | head -5
  stat -c "%A %U" /home/ghdc/nwm/object-store/.nhms-copyback-batch.lock
  [ "$red" = 0 ]'
```

期望：两段都没有 `RED`、命令 rc=0（任一段 `RED` 则 rc=1）；nwm summary `lanes=["precip-cache","raw"]`、canonical summary
`lanes=["canonical"]`；`copyback_lock_failures` 全 0；锁文件仍是 `-rw------- frd_muziyao`。
判据本体与各退出码含义见 `infra/env/node27-raw-retention.example`（测试直接抽取那份 `jq`
程序执行）。

已知限制：跨主机互斥只有 receipt 证明（CI 与本机测试只覆盖同机 `posix` 对 `posix`）；node-27
自身上 `posix` 持有者不排斥本机 `flock` 取锁者，所以 node-27 以后新增的取锁方都必须用
`posix`；NFS server 重启后的 grace period 不约束 node-27 本地 `lockf`，低概率、未缓解。

### 5.4 Published artifacts

Display products, tiles, manifests, and logs live under `published/`:

```text
node-22 view: /ghdc/data/nwm/published
node-27 view: /home/ghdc/nwm/published

published/logs/<source>/<YYYYMMDDHH>/...
published/tiles/hydro/<source>_<YYYYMMDDHH>/...
published/manifests/...
```

Do not look under `published/` for complete SHUD `runs/<run_id>/output`.
Those belong under `object-store/runs/<run_id>/`.

Checks:

```bash
# node-22
ssh -p 32099 frd_muziyao@210.77.77.22 \
  'test -d /ghdc/data/nwm/published &&
   stat -c "%n %A %U:%G" /ghdc/data/nwm/published &&
   find /ghdc/data/nwm/published/logs /ghdc/data/nwm/published/tiles \
     -maxdepth 4 -type f -printf "%TY-%Tm-%Td %TH:%TM %p\n" 2>/dev/null |
   sort | tail -40'

# node-27
ssh -p 32099 nwm@210.77.77.27 \
  'test -d /home/ghdc/nwm/published &&
   stat -c "%n %A %U:%G" /home/ghdc/nwm/published &&
   find /home/ghdc/nwm/published/logs /home/ghdc/nwm/published/tiles \
     -maxdepth 4 -type f -printf "%TY-%Tm-%Td %TH:%TM %p\n" 2>/dev/null |
   sort | tail -40'

ssh -p 32099 nwm@210.77.77.27 \
  'find /home/ghdc/nwm/published -path "*/runs/*" -o -path "*/forcing/*"'
```

The second command should normally print nothing. If full `runs/` or `forcing/`
payloads appear under `published/`, the publication boundary is wrong.

### 5.5 Basins source data

node-27 autopipe seeds/refreshes basin registry from:

```text
/home/ghdc/nwm/Basins
```

Check:

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'stat -c "%n %A %U:%G" /home/ghdc/nwm/Basins &&
   find /home/ghdc/nwm/Basins -maxdepth 2 -type d | sort | head -40'
```

#### 5.5.1 清理已注册流域的 `forcing/`：清空目录，不要删目录（#1813 / #1702 第 3 项）

`forcing/` 下的 IDW 代站 CSV direct-grid 已不读，可以清理。但清理方式决定它是不是
**真 no-op**，因为 basins 包身份对 `forcing/` 的依赖不是一刀切的（裁定见
[ADR 0006](../../adr/0006-forcing-csv-out-of-basins-package-identity.md)）：

| 对已注册流域的操作 | 包身份 | 下次 baseline publish |
|---|---|---|
| 删除/修改 `forcing/*.csv`，**保留** `forcing/` 目录 | 不变 | 无 cutover |
| 整个 `forcing/` 目录 `mv` 走或删除 | **变** | 需要逐流域 declared cutover |
| 把 legacy `focing/` 改名成 `forcing/` | **变** | 需要 declared cutover |

原因：CSV 载荷证据（数量、字节、聚合校验和）自 `basins.package.v2` 起已不进
`content_sha256` / `package_checksum`，discovery 也不再把 `forcing_csv_count` 写进
inventory；但 `forcing_dir` / `forcing_dir_original_name` 仍在 inventory 里（打包要靠
它们定位源目录），它们随目录存在与否变化，进而改变
`source_inventory_checksum`——而 cutover 门把该字段算作 model identity
（`scripts/scheduler_refresh/identity.py` 的 `REGISTRY_MODEL_NESTED_IDENTITY_FIELDS`，
由 #1099 从 `scheduler_file_provider_refresh.py` 拆出）。目录是结构事实，载荷不是。

所以清理动作是：

```bash
# 已注册流域：搬走 CSV，留下空目录
ssh -p 32099 nwm@210.77.77.27 \
  'set -e
   d=/home/ghdc/nwm/Basins/<basin>/forcing
   dest=/home/ghdc/nwm/Basins-retired/forcing-csv-$(date +%Y%m%d)/<basin>
   mkdir -p "$dest"
   find "$d" -maxdepth 1 -type f -name "*.csv" -exec mv -t "$dest" {} +
   ls -A "$d" | wc -l   # 期望 0；目录本身必须还在'
```

留一个空目录的代价是零，换来的是清理当天和往后每次 publish 都不触发 cutover。

**新流域投递**则相反：新流域根本不带 `forcing/` 是对的——它没有历史身份要延续，
首次 publish 不存在 `package_changed`。投递规范禁止再带 `forcing/`，只约束新投递，
不要拿它去反推已注册流域可以直接删目录。

### 5.6 新增或恢复流域的运维入口

后续增加新的 `Basins/` 流域时，当前生产入口固定为：

| 目标 | 节点 | 入口 |
| --- | --- | --- |
| seed/register/ingest/display coverage | node-27 | `scripts/node27_autopipe_cron.sh` -> `scripts/node27_autopipeline.py` |
| 刷新可计算模型清单 | node-22 | `scripts/publish_scheduler_file_registry.py` |
| 重启展示 API | node-27 | `scripts/ops/start-display-api.sh` |

不要把新增流域做成 qhh/heihe/kashigeer 的一次性手工流程。标准流程：

1. 把流域源数据放到共享 Basins 根：

   ```text
   node-22 view: /ghdc/data/nwm/Basins/<basin>...
   node-27 view: /home/ghdc/nwm/Basins/<basin>...
   ```

   目录必须允许 node-27 的 `nwm` 用户读取和进入。跨用户从 node-22 复制
   Basins 源时，不要保留源端私有权限；复制后至少确认：

   ```bash
   ssh -p 32099 nwm@210.77.77.27 \
     'find /home/ghdc/nwm/Basins/<basin> -maxdepth 3 -type d | sort | head -40'
   ```

2. 在 node-27 走 autopipe wrapper，而不是直接绕过 wrapper 调 Python：

   ```bash
   ssh -p 32099 nwm@210.77.77.27
   cd /home/nwm/NWM
   bash scripts/node27_autopipe_cron.sh
   tail -n 240 /home/nwm/autopipe-logs/autopipe.log
   ```

   wrapper 会从 `infra/env/node27-ingest.env` 加载 writer DB、NFS object-store
   和 `BASINS_ROOT`，并阻断 display env、ambient libpq env、node-22 historical
   DB env。`scripts/node27_autopipeline.py` 是实现入口：发现 `Basins/` inventory
   与 `object-store/runs/`，seed 缺失 basin registry，应用 forcing-domain
   handoff，解析 run，并刷新 display coverage。它是幂等的，后续新增流域也走
   同一入口。

3. 在 node-22 生成 baseline staging，然后在 node-27 provision GFS/IFS 两个
   source-scoped direct-grid variant；禁止把 baseline/IDW registry 直接发布为
   canonical。完整命令和原子发布顺序见本手册第 3.3 节。完成后必须证明：

   - 每个流域恰有 GFS/IFS 两行；
   - `resource_profile.forcing_mapping_mode` 只有 `direct_grid`；
   - station binding 的 `grid_id`、`grid_cell_id` 和经纬度来自对应
     GFS/IFS 0.25° 原始格点；
   - canonical consumer 保持 `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`。

   `NHMS_SCHEDULER_MODEL_IDS` 和 `NHMS_SCHEDULER_BASIN_IDS` 正常保持为空；
   不要为了新增流域在生产长期写死单个 basin。direct-only canonical 原子发布后，
   `nhms-compute-scheduler.timer` 的后续 tick 才会按 00/12 UTC 业务 cycle 走
   Slurm 计算。

4. 展示 API 不负责 seed 新流域。只有代码、env、端口或 display runtime
   变更后，才用以下入口重启：

   ```bash
   ssh -p 32099 nwm@210.77.77.27
   cd /home/nwm/NWM
   bash scripts/ops/start-display-api.sh
   ```

5. 新增流域完成后的最低验收：

   ```bash
   # node-27: API 能枚举新 basin；有 published run 后 has_display_product=true 才会出现
   curl -fsS 'http://127.0.0.1:8080/api/v1/basins?limit=500'
   curl -fsS 'http://127.0.0.1:8080/api/v1/basins?has_display_product=true&limit=500'

   # node-22: scheduler registry 包含新增 model
   ssh -p 32099 frd_muziyao@210.77.77.22
   cd /scratch/frd_muziyao/NWM
   .venv/bin/python -c 'import json; from pathlib import Path; p=json.loads(Path("/scratch/frd_muziyao/nhms-prod/object-store/scheduler/registry/manifest-last.json").read_text()); print("\n".join(sorted(item["model_id"] for item in p.get("models", []) if "model_id" in item)))'
   ```

   `has_display_product=true` 只代表已有发布 run 的流域；新流域完成 registry
   但尚未跑出 SHUD run 时，应先出现在普通 `/api/v1/basins` 和 scheduler
   registry 中，等 22 产出 run、27 autopipe ingest 后再进入展示产品列表。
