# Historical Forcing Copyback Backfill

> Status: historical, not current production runbook. This document is retained
> for audit evidence from the pre-#837 copyback period. Do not run DB-backed
> scan steps from node-22; current production DB reads must run on node-27
> against its local `:55432` writer/readable operational DSN. Node-22 is now
> compute/artifact producer only and may only participate in file copyback work
> from a node-27-produced manifest or a separately approved rollback drill.

最后更新：2026-06-16
适用范围：历史记录。pre-#837 时该流程在 node-22 计算控制面补拷历史已发布 q_down
run 引用的 forcing package 到 shared object-store copy root；当前不得从 node-22
执行 DB-backed scan。

## 结论

历史默认命令只做 dry-run，不写目标目录。只有显式加 `--apply` 才会把通过校验的
`forcing/<source>/<cycle>/<basin_version_id>/<model_id>/` 包复制到
`NHMS_OBJECT_STORE_COPYBACK_ROOT`。这些命令保留用于解释历史证据；当前如需
copyback，应先在 node-27 生成候选 manifest，再让 node-22 只执行文件层复制。

该工具不修改数据库、不推进 `hydro` 或 `met` 状态；审计证据来自 stdout JSON 报告。

## 环境变量

历史 pre-#837 命令曾在 node-22 checkout root 执行并加载计算控制面环境：

```bash
cd /scratch/frd_muziyao/NWM
set -a
source infra/env/compute.host.env
set +a
```

历史命令曾要求：

```bash
# Historical pre-#837 DB DSN removed after #837; run DB scans on node-27.
OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
```

如果生产对象 URI 依赖前缀，也保留：

```bash
OBJECT_STORE_PREFIX=s3://nhms
```

`OBJECT_STORE_ROOT` 是历史 forcing package 的 staging/source root；
`NHMS_OBJECT_STORE_COPYBACK_ROOT` 是 22 写、27 只读消费的 shared object-store copy root。
两者不能互相嵌套，也不能配置成同一个目录。发布时的 exact-root skip 语义不适用于本工具；
backfill 的目标是修复 shared mirror，same-root 属于配置错误。

## Copyback batch mutex（#2035）

所有会在 `NHMS_OBJECT_STORE_COPYBACK_ROOT` 下 **promote 目录树** 的写者——publisher 的
q_down / run-products / canonical-precip 三条 lane、orchestrator 的 run-tree copyback、
本工具（每个 package）、以及 `scripts/canonical_precip_copyback_backfill.py`（每棵树）——
在临界区开始前先取一把跨进程排他 `flock`：

```
$NHMS_OBJECT_STORE_COPYBACK_ROOT/.nhms-copyback-batch.lock
```

- **路径固定、无环境变量覆盖**。锁挂在 copyback root 下，而不是 `/tmp`：systemd
  `PrivateTmp=true` 与 Slurm `job_container/tmpfs` 会给进程各自的私有 `/tmp`，两个写者会
  flock 到两个 inode，互斥静默失效。
- 锁文件以 `0o600` 创建，代码**从不 unlink**——这两条由代码直接强制
  （`packages/common/copyback_guard.py` 的 `_LOCK_MODE`，模块里没有任何 unlink 调用）。
  「属主就是写者本人」则**不是独立强制的不变量，而是推论**：代码断言的是
  「锁文件属主 == copyback root 属主」和「当前 euid == 锁文件属主」，现网所有写者恰好
  是同一个 uid、外来 uid 又在文件创建前就被拒，两者才重合。
- 持有者被 kill 时**本机**内核释放 flock，所以「锁文件还在」不等于「锁还被持有」。
  但生产 copyback root 是 NFS（node-22 的 `/ghdc/data/nwm/object-store` 是
  `ghdc:/home/ghdc` 的 NFSv4.2 挂载，2026-09-10 实测）：**进程**死立刻释放，
  **整台主机**死不会——锁由服务端持有到租约过期。因此卡住的锁有三种形态，
  只有一种该动：
  - **有活持有者**（`lsof <lock>` / `fuser -v <lock>` 打得出 pid）→ **不要动它**：
    删掉会让下一个写者去锁一个新 inode，互斥当场失效。
  - **属主是对的、本机查不到持有者、写者却仍在超时** → 持锁主机整台死了，等服务端
    租约过期。**同样不要动它**，理由与上一条相同（unlink 会拆成两个 inode）。
    这不是「孤儿」，下一条不适用。
  - **属主不对的孤儿**（`ls -ln <lock>` 的属主 ≠ `stat -c '%u' <copyback-root>`，
    且无进程持有）→ 必须修，否则所有写者永远 fail closed：
    `sudo chown "$(stat -c '%u:%g' <copyback-root>)" <lock>` 无条件可用；也可以直接
    `rm -f <lock>` 让下一个写者重建——**删除权限来自 copyback root 目录的写+执行位，
    不是锁文件的属主身份**（POSIX unlink 语义），前提是这条路径上没有 sticky bit。
    2026-09-10 node-22 实测：`/ghdc` 755 root:root、`/ghdc/data` 777 root:root、
    `/ghdc/data/nwm` 与 `/ghdc/data/nwm/object-store` 都是 775
    `frd_muziyao`(1103):`huser`(1078)，整条链上没有 sticky bit，所以 gid 1078 的
    任何成员都能删。若 `ls -ld` 看到 `t`，退回 `sudo chown`。
- 互斥**覆盖整个 batch**：plan → copy → 每次 promote → commit 或 rollback 返回之后才释放。
  per-tree 粒度不够：batch rollback 中 `backup_dir is None` 的分支会删掉「此刻位于目标位置的
  东西」，那只有在期间没有别的写者提交过才等于恢复。
- `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`（可选，**默认 900 秒**）限制等待上限。
  竞争是**等待**而不是拒绝；超时抛出各 lane 自己的错误类型（run-tree lane 是
  `RunTreeCopybackError`，q_down lane 是带 `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` code 的
  `PublishError`，canonical mirror 记 `failed` receipt 后本 cycle 继续），
  **绝不降级为无锁 promote**。空值取默认；非数字或非正数是硬性配置拒绝。
- 所有 copyback 写者必须是同一个 uid（node-22 上是 `frd_muziyao`）。`0o600` + 属主断言让
  跑在别的账号下的写者 fail closed，而不是静默地不加锁运行；属主同时比对当前 euid 与
  copyback root 的属主，且外来 uid 在 `O_CREAT` 之前就被拒，所以锁文件不会被别的账号毒化。
  互斥只在**单机**内成立。
- **`NHMS_OBJECT_STORE_COPYBACK_ROOT` 必须由那个唯一的写者 uid 属主持有。** 条件是
  **uid 相等**，不是「写者能写这个 root」：属主是别的账号、只靠组位开放写的 root
  （例如容器 uid 在补充组里）会被**拒绝，而不是共享**——锁文件的属主锚定在 root 的属主上。
  核查 `stat -c '%u %a %n' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"` 与写者 `id -u` 是否相等；
  不相等则每个 package 都会记 `copyback_lock_unavailable`。报错有两种形态：锁文件还不存在时，
  在创建之前就拒绝，同时给出两个 uid 和锁文件路径；锁文件已存在且属主是别人时，报的是
  `cannot acquire copyback batch lock <path>: [Errno 13] Permission denied`——只有路径、没有 uid，
  这个 `Permission denied` 本身就是属主不对的信号，属主用 `ls -ln <lock>` 与
  `stat -c '%u' "$NHMS_OBJECT_STORE_COPYBACK_ROOT"` 查。
- 若某个 package 报 `category: copyback_lock_unavailable`，说明它没拿到锁（超时或锁文件被篡改），
  目标树本身完好无损，重跑即可。

## Dry-Run

先运行 dry-run 并保存 JSON：

```bash
uv run python -m services.tile_publisher.forcing_copyback_backfill \
  > /scratch/frd_muziyao/nhms-prod/workspace/forcing-copyback-backfill-dry-run.json
```

历史 dry-run 会扫描数据库表；当前这类 DB scan 必须改在 node-27 对 `:55432`
执行，node-22 不再提供 `DATABASE_URL`：

- `hydro.hydro_run.status IN ('parsed', 'frequency_done', 'published')`
- `hydro.river_timeseries.variable = 'q_down'`
- `met.forcing_version` 中 joined 的 `forcing_package_uri`、`checksum`、`lineage_json`

扫描只读，不写数据库；查询从符合状态的 `hydro.hydro_run` 出发，用 `EXISTS` 验证 q_down 覆盖，
避免为发现候选而 materialize 全量 q_down run 集合。历史包很多时 stdout JSON 仍可能很大，建议始终重定向保存。

报告重点看：

- `copyable_package_count`
- `already_present_checksum_consistent_count`
- `missing_source_count`
- `checksum_mismatch_count`
- `legacy_key_rejected_count`
- `failure_count`
- `failures[]`

`failures[]` 中的 `run_id`、`forcing_version_id`、`forcing_package_uri` 和 `reason`
用于人工处理。形如 `forcing/{forcing_version_id}/` 的 legacy key 不会自动猜测迁移目标。

## Apply

确认 dry-run 后再执行写入：

```bash
uv run python -m services.tile_publisher.forcing_copyback_backfill --apply \
  > /scratch/frd_muziyao/nhms-prod/workspace/forcing-copyback-backfill-apply.json
```

apply 只复制满足同一套 publish-time forcing 校验的包：normalized key、source/cycle/basin/model
identity、manifest SHA-256、lineage manifest checksum 和 source tree 都必须一致。

目标端如果已经存在且 `forcing_package.json` checksum 与 `met.forcing_version.checksum` 一致，
报告为 `already_present`，不会重复计为 copied。

## 重跑

dry-run 和 apply 都可以重跑。checksum 一致的目标包会稳定进入
`already_present_checksum_consistent_count`，缺源、checksum mismatch、legacy key 或 unsafe path
仍会保留为 failure/manual item。

重跑前不要手动修改 DB 状态。该工具没有 DB 写入路径，不能用来修正 `hydro.hydro_run.status` 或
`met.forcing_version` 元数据。

## 回滚边界

工具在单个 package copy 失败时会使用 publish-time copyback helper 的目标替换/回滚行为，避免留下部分包。

已成功报告为 copied 的包如果需要撤回，回滚是人工文件操作：根据 apply JSON 中 `packages[].object_key`
在 `NHMS_OBJECT_STORE_COPYBACK_ROOT` 下隔离、删除或恢复对应目录。优先先挪到同一文件系统的 quarantine
目录，确认下游不再读取后再删除，例如：

```bash
target=/ghdc/data/nwm/object-store/forcing/<source>/<cycle>/<basin_version_id>/<model_id>
quarantine=/ghdc/data/nwm/object-store/.manual-rollback-$(date -u +%Y%m%dT%H%M%SZ)-<model_id>
mv -- "$target" "$quarantine"
```

隔离前必须先确认该目录对应 apply 报告中的 copied package，且没有被后续生产重新发布。数据库行不会自动回滚，
因为本工具从不修改数据库。
