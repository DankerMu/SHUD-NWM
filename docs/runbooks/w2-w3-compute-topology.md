# w3 / w2 计算节点拓扑

最后更新：2026-11-24（主机事实于 2026-11-24T02:27Z 在 w3、w2 上只读采集）

适用范围：承载 NHMS **计算角色**的两台 LAN 主机 w3、w2。仓库里的 `node-22` / `node22`
（脚本名、unit 名、env key、receipt schema id、规格名）是这个计算角色的历史代号，自
2026-11-23 起指 w3 / w2，不改名。原主机 `210.77.77.22` 已退出 NWM 计算，只作 rollback
archive。容量、行数、耗时一律以实测为准，本文不记绝对数字。

## 1. 主机与角色

| 主机 | LAN IP | 角色 |
|---|---|---|
| w3 | `10.0.1.13` | Slurm 控制器（slurmctld + slurmdbd）+ 计算；scheduler 定时器、slurm-gateway、scheduler journal、本机 object-store；生产 checkout `/scratch/frd_muziyao/NWM` |
| w2 | `10.0.1.12` | 纯计算；承载 `workspace/runs`；无出网，apt 与 chrony 经 w3 中继 |
| node-27 | `10.0.1.11` | 活动 DB / ingest / display；向两台计算主机导出 `/home/ghdc`（挂为 `/ghdc/data`） |

- 两台主机只能从 node-27 经 LAN 到达（本机 ssh 走 ProxyJump）。本文的命令都以生产用户
  `frd_muziyao` 执行；生产用户 uid/gid、绝对路径（`/scratch/frd_muziyao`、
  `/users/frd_muziyao`、`/volume/nwm/Basins`、`/ghdc/data`）、Slurm 分区 `CPU` 与账户
  `friends` 都与迁移前一致，所以代码、env key、unit 名没有任何改动。
- 计算角色的全部边界照旧适用于 w3 和 w2：**不连任何活的 NHMS 库**、不是任何 NHMS 库的写入方（w3 上为 slurmdbd 跑的 MariaDB 只是 Slurm
  记账库，不算）、活动操作只用
  精确解释器、生产变更只在 scheduler pass 边界做。
- **生产用户的 systemd user manager 只允许在 w3 上运行。** `/users/frd_muziyao` 经 NFS 共享到 w2，
  里面的 user 单元和 `*.wants` 链接在 w2 上同样可见，单元里没有主机条件：一旦 w2 上为生产用户起了
  user manager（以生产用户 ssh 登录、`su -`、`loginctl enable-linger`），scheduler 定时器、
  slurm-gateway、journal-backup 会在 w2 上再起一套，与 w3 并发跑 pass。w2 上生产用户只通过 Slurm
  作业出现；要看文件用运维账号。2026-11-24 实测 w2 上生产用户没有 user manager、没有 linger、
  没有进程；目前没有机械防护，只有这条纪律（主机侧防护见 #3359）。
- 两台主机上除 NHMS 外还有 yd producer（每小时 cron，`/scratch/frd_muziyao/yd`），与 NHMS
  共用 Slurm 账户：**不要按用户取消 Slurm 作业**。

## 2. 存储布局与 loopback-NFS 规则

| 路径（两台主机上相同） | 后端 | 导出方 | 说明 |
|---|---|---|---|
| `/scratch/frd_muziyao` | w3 `/srv/nhms-scratch/frd_muziyao`（ext4 LV） | w3 → w3、w2 | checkout、`.venv`、`nhms-prod`（journal、object-store、state index） |
| `/scratch/frd_muziyao/nhms-prod/workspace/runs` | w2 `/srv/nhms-runs/runs`（ext4 LV） | w2 → w2、w3 | 每次运行的工作目录 |
| `/ghdc/data` | node-27 `/home/ghdc` | node-27 → w3、w2 | 与 node-27 `/home/ghdc/nwm` 同一份 NFS；copyback 目标 |
| `/users/frd_muziyao` | w3 本地 | w3 → w2（rw） | 家目录（用户 unit） |
| `/volume/nwm` | w3 本地 | w3 → w2（只读） | Basins 源数据 |

**规则：每个节点——包括导出方自己——都只经 NFS 客户端访问 `/scratch/frd_muziyao` 和
`workspace/runs`。** w3 把自己的导出再 loopback 挂回 `/scratch/frd_muziyao`，w2 对 runs 同理；
后端目录 `/srv/nhms-scratch`、`/srv/nhms-runs` 是 root-only，不给作业直接用。

原因：本地文件系统上的 `flock` 与 NFS 客户端的 `flock` **互不排斥**。作业侧的 state-index
锁是阻塞式 `fcntl.flock`（`packages/common/state_manager.py`），如果 w3 上的进程走本地路径、
w2 上的进程走 NFS，两边会同时拿到同一把锁。实测见
[`receipts/2026-11-23-lock-interop-w2-w3.md`](receipts/2026-11-23-lock-interop-w2-w3.md)：
切换前的 5 行矩阵里 local ↔ loopback 两个方向都是 ACQUIRED；全部改走客户端之后的 13 行矩阵
全部 BLOCKED。node-27 ↔ w3 的矩阵见
[`receipts/2026-11-23-lock-interop-node27-w3.md`](receipts/2026-11-23-lock-interop-node27-w3.md)
（node-27 `flock` ↔ 客户端 `flock` 两行仍是 ACQUIRED，与 2026-09-14 的结论和 ADR 0008 一致）。

配置要点：

- 所有 NFS 挂载 `vers=4.2,hard,_netdev`；scratch 与 runs 两处挂载在两台主机上都带 `nofail`，
  其中导出方自己的 loopback 挂载另带
  `x-systemd.requires=nfs-server.service,x-systemd.after=nfs-server.service`。
- scratch 导出带 `no_root_squash`：runs 嵌套挂在 scratch 导出里面，root 要能穿过导出查到这个嵌套
  挂载点，所以这两台专用主机的 root 被信任；去掉它，runs 会挂不上。runs 导出没有这个选项。实测
  root 在 `/ghdc/data` 下读不了生产用户的私有文件，必须以生产用户身份读。
- 两台主机 nfsd `threads=64`（`/etc/nfs.conf.d/nhms.conf`）。
- **改 `/etc/fstab` 后先 `systemctl daemon-reload` 再挂载/卸载。** 迁移时 systemd 用旧 fstab
  生成的 `scratch.mount` 把 LV 又挂回了 `/scratch`，盖住了 loopback 挂载；处置是停 slurmd、
  卸载、`daemon-reload`、确认 `/scratch` 不再是挂载点，再挂 NFS。

## 3. Slurm

- 集群 `nwm`，`SlurmctldHost=w3`，记账走 slurmdbd（w3，MariaDB）；
  `AccountingStorageEnforce=associations,limits`，账户 `friends` 下有生产用户的 association。
- `FirstJobId=1000000`：迁移前的 journal 里已有到 167660 的 job id，新集群从不复用那个区间。
- `SelectType=select/cons_tres` + `CR_Core`：可消耗资源只有核，内存不计——作业申请 8G、实际
  约 0.2G，按 `--mem` 记账只会白白压低并发。w3 `ThreadsPerCore=1`，w2 `ThreadsPerCore=2`。
  分区 `CPU` 含两台主机，`MaxArraySize=2001`。
- slurmd 有 `RequiresMountsFor` drop-in（`/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf`）：
  w3 要求 `/scratch/frd_muziyao`、`/ghdc/data`；w2 另外要求 runs、`/users/frd_muziyao`、
  `/volume/nwm`。所列挂载不在，slurmd 不起，节点不接作业。
- **已知缺口（#3359）：w3 的 drop-in 不含 runs。** w2 不在时，w3 的 runs 挂载因 `nofail` 被跳过，slurmd
  照常起来；这时落在 w3 上的作业会把结果写进挂载点下面的本地空目录，等 runs 后来挂上就被盖住。
  w3 重启后、接作业前，先确认 `findmnt /scratch/frd_muziyao/nhms-prod/workspace/runs` 的来源是
  w2 的导出。
- **w3 ⇄ w2 互相依赖**：w3 上的作业要用 w2 导出的 runs，w2 要用 w3 导出的 scratch。任一台
  停机，另一台上的作业会挂在 `hard` 挂载上等待，而不是失败。

## 4. 定时器与单元

用户单元（`frd_muziyao`，w3 上已 `loginctl enable-linger`）与迁移前同名，清单与校验值见
`infra/systemd/node22/MANIFEST.json`，漂移探针 `scripts/node22_unit_drift_check.py`。单元文件在
共享家目录里，w2 上也看得见，但只允许 w3 运行它们（见第 1 节）。迁移后新增、只在 w3 上运行的
两项：

- `nhms-w3-journal-backup.service` / `.timer`（仓库副本在 `infra/systemd/`）：每小时 `:40`
  把 scheduler journal `rsync -a --delete --max-delete=5000` 到
  `/ghdc/data/nwm/operator-archives/w3-scheduler-journal/journal/`，成功后把 UTC 时间写进同目录
  的 `LAST_OK`。本机 journal 目录不存在时（例如 `/scratch` 没挂上）`ExecCondition` 让它跳过，
  `--delete` 不会清空备份。检查（以生产用户）：`cat /ghdc/data/nwm/operator-archives/w3-scheduler-journal/LAST_OK`。
  这个单元没有接 `nhms-node22-unit-failure-alert@` 的 `OnFailure`：失败不会告警，只能看 `LAST_OK`
  的时间是否在一小时内。
- 系统级 `nhms-mounts-ensure.service` / `.timer`（两台主机都有，仓库不跟踪，原文如下）：开机
  90 s 后起、每 2 min 执行 `mount -a -t nfs4`，并在 slurmd 未运行时启动它。两台主机互为 NFS
  服务端，冷启动时先起来的一台挂不上对方，靠它自愈。

  ```ini
  # /etc/systemd/system/nhms-mounts-ensure.service
  [Unit]
  Description=NHMS: retry NFS mounts and start slurmd once they are present
  [Service]
  Type=oneshot
  ExecStart=/bin/sh -c 'mount -a -t nfs4 2>/dev/null; systemctl is-active -q slurmd || systemctl start slurmd || true'

  # /etc/systemd/system/nhms-mounts-ensure.timer
  [Unit]
  Description=NHMS: periodic NFS mount self-heal
  [Timer]
  OnBootSec=90s
  OnUnitActiveSec=2min
  [Install]
  WantedBy=timers.target
  ```

## 5. Retention（已部署值）

scheduler 与 retention 单元读 `infra/env/compute.scheduler-dbfree.env`：

- `NHMS_RETENTION_DAYS=5`（迁移前 14）：本机 object-store 所在的 scratch LV 比迁移前小。
  w3 加盘后恢复 14。
- `NHMS_RETENTION_EXTRA_ROOTS_DAYS=14`：**不要跟着调小**——这个窗口同时管 NFS copyback 根
  （node-27 读取的那一份），缩短它等于缩短对外可见的历史。
- w2 的 runs 卷按当前产出速度能放约两周，余量不大；判断一律 `df -h` 实测。
- 两个窗口都处于启用状态（`NHMS_RETENTION_ENABLED=true`、`NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true`，
  `NHMS_RETENTION_DRY_RUN=false`）。

## 6. 部署（pass 边界、ff-only）

w3 访问 github.com 不稳定，部署走本地打 `git bundle`、经 ssh 送到 w3、`--ff-only` 合入：

1. 本地：`git bundle create d5.bundle <w3 当前 HEAD>..origin/master`，送到 w3 的
   `/scratch/frd_muziyao/`。
2. w3（生产用户）：确认 `git status --porcelain --untracked-files=no` 为空；`git fetch origin`
   失败时 `git bundle verify` 后从 bundle 取 `refs/remotes/origin/master`；确认目标提交是
   HEAD 的后代且在 `origin/master` 上。
3. 等一个 pass 在跑（`nhms-compute-scheduler.service` 为 `activating`），停
   `nhms-compute-scheduler.timer`，等该 pass 结束，`git merge --ff-only <目标>`，立刻重启定时器（这次部署改了 `uv.lock` 时先做 3a）。
   - 3a（仅当 `git diff --name-only <旧 HEAD> <目标> -- uv.lock` 非空）：合入后**先不要**重启定时器。
     等 `squeue` 里没有 `nhms_*` 作业（yd 的作业不带这个前缀，不用等），以生产用户在 checkout 里
     执行 `uv sync --frozen --all-extras --dev`（w3 的环境在迁移时就是这样建的），确认
     `.venv/bin/python -V` 仍是 3.11.x，再重启定时器。w3 上还没有演练过带 `uv.lock` 变更的部署。
4. 等新代码上的第一个 pass 结束，看 evidence、`scripts/node22_scheduler_stall_health.py` 和
   `systemctl --user --failed`。

**绝不**切换生产 checkout 的分支、手改工作树、`git stash pop`。`.venv` 经 NFS 被两台主机上的
Slurm 作业和定时器共用：日常禁止在这里同步或重建环境；只有部署改了 `uv.lock` 时才同步（步骤 3a），且须定时器已停、
没有在跑的 pass、Slurm 队列里没有 NWM 作业。

## 7. 冷启动

**两台主机在最终布局下还没有做过冷启动演练**（`uptime -s`：w3 2026-11-20T07:50Z、w2 2026-11-20T07:40Z，都早于 2026-11-23 的切换）。下面是按
单元依赖推出的预期顺序和检查清单，第一次真实重启时要逐项核对并回填本节。

预期：

1. 两台主机各自起 `nfs-server`、`munge`、`chrony`；w3 另起 MariaDB、`slurmdbd`、`slurmctld`。
2. 导出方自己的 loopback 挂载等 `nfs-server`；对方的导出此时可能还不可达，`nofail` 让开机继续。
3. `nhms-mounts-ensure.timer` 每 2 min 重试挂载并拉起 slurmd。
4. w3 上生产用户的 user manager 因 linger 自动起来，`nhms-slurm-gateway.service`
   （`Restart=on-failure`）和各定时器随之启动；scheduler 定时器 `OnBootSec=2min`。

检查：

- `findmnt -t nfs4`：w3 三处、w2 五处挂载齐全，`/scratch` 本身**不是**挂载点，且 runs 的来源在两台
  主机上都是 w2 的导出（w3 的 slurmd 不会因为缺 runs 而拒绝启动，见第 3 节）。
- `sinfo -N`：两个节点 `idle`；`systemctl is-active slurmctld slurmdbd slurmd munge nfs-server`。
- 生产用户 `systemctl --user --failed` 为空，`systemctl --user list-timers` 里每个 `nhms-*`
  定时器（scheduler、journal-backup、两个 health、file-provider-refresh 与各 retention /
  state-index 维护项）都有下一次触发时间。
- 可能要手动处理的：user manager 先于 `/scratch/frd_muziyao` 挂载起来时，读
  `EnvironmentFile` 的单元第一次会失败——gateway 会自动重试，定时器触发的单元等下一次触发；
  若 `--failed` 里留有残项，`systemctl --user reset-failed` 后手动 `start`。

## 8. Bring-up 时踩到的缺口

- **只在 w3 上** `loginctl enable-linger frd_muziyao`（否则没有登录会话时 user 单元不跑）；w2 上
  **禁止**给生产用户开 linger（见第 1 节）。
- slurmdbd 需要单独装库、建集群与账户 association；`AccountingStorageEnforce` 打开后，没有
  association 的用户提交即被拒。
- `infra/env/compute.host.env` 没有模板（`infra/env/` 下没有对应的 `.example`），迁移时是从
  原主机逐文件拷贝的；密钥文件只做主机到主机的拷贝，不落到别处。
- w2 无出网：apt 经 w3 上的 HTTP 代理，时间同步以 w3 为 chrony 上游。

## 9. 回滚到原主机

`210.77.77.22` 上的 journal 副本停在 2026-11-23T02:34Z。回滚不是"把定时器开回来"：先停 w3
的 scheduler 定时器，把 w3 的 journal（或 NFS 上的 journal-backup）拷回去，再启用原主机的
单元；原主机的 `.venv` 是 #1831 留下的 Python 3.12.7，那里不要运行任何 `uv` 命令。原主机本机
PostgreSQL `:55433` 是历史库，已 archived/stopped，仅作 rollback archive，do-not-connect，**不要连**。
回滚需要新的运维决定。
