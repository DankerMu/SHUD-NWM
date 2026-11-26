# w3 / w2 计算节点拓扑

最后更新：2026-11-26（主机事实于 2026-11-24T02:27Z 在 w3、w2 上只读采集；2026-11-26 的主机改动见
`receipts/2026-11-26-issue-3359-compute-host-hardening.md`）

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
  作业出现；要看文件用运维账号。
  - 机械防护（#3359，2026-11-26）：w2 上 `user@1103.service` 已 mask
    （`/etc/systemd/system/user@1103.service -> /dev/null`），以生产用户登录也起不了 user manager。
    Slurm 作业不经 PAM，不受影响（mask 后实测作业正常 `COMPLETED`）。
  - 核对：w2 上 `systemctl is-enabled user@1103.service` 输出 `masked`；回滚是 `systemctl unmask`。
  - 这条 mask 不在版本库里（它是一个指向 `/dev/null` 的链接），重装 w2 后要重做；纪律照旧。
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
  约 0.2G，按 `--mem` 记账只会白白压低并发。两台都是 `ThreadsPerCore=1`、40 核（w2 自 2026-11-26 起
  关 SMT，见下）。
  分区 `CPU` 含两台主机，`MaxArraySize=2001`。
- slurmd 有 `RequiresMountsFor` drop-in（`/etc/systemd/system/slurmd.service.d/10-nhms-mounts.conf`，
  仓库副本见第 4 节）：两台都要求 `/scratch/frd_muziyao`、runs、`/ghdc/data`，w2 另外要求
  `/users/frd_muziyao`、`/volume/nwm`。所列挂载不在，slurmd 不起，节点不接作业。
- **w2 不在时 w3 不往 runs 挂载点下面写**（#3359，2026-11-26）。两台的 runs 都挂在 w3 scratch 上的
  同一个目录上，w2 不在时这个挂载因 `nofail` 被跳过。两层防护：
  - w3 的 slurmd drop-in 要求 runs 挂载，挂不上 slurmd 就不起（`nhms-mounts-ensure` 每 2 min 重试）。
  - 挂载点下面的目录（后端路径 `/srv/nhms-scratch/frd_muziyao/nhms-prod/workspace/runs`）是
    `root:root`、`0000`。scheduler pass 是 user 单元，不受 slurmd drop-in 约束，它在提交前就往
    `workspace/runs/<run_id>/input/` 写；未挂载时这次写入现在直接报 `Permission denied`，不会被后来的
    挂载盖住。root 仍能在这个目录上挂载（已用探针目录在两台主机上演练）。
  - 还没证明的：w2 关机时 w3 的 slurmd 确实不起。这只有冷启动演练能验证（第 7 节）。
- **w2 关 SMT**（#3384，2026-11-26）。w2 原来开着超线程（w3 的在固件里关着），解释器类单线程代码因此
  慢 2.2–2.9 倍；关掉后两台基准一致。之前把 forcing 在 w2 上慢一倍读成"IO 局部性"是错的：时间落在
  user CPU 上，不是 IO 等待。
  - 运行时：`/sys/devices/system/cpu/smt/control` 为 `off`；开机时由 `/etc/tmpfiles.d/nwm-smt-off.conf`
    写入（仓库副本见第 4 节）。`slurm.conf` 里 w2 的节点定义是 `CPUs=40 ... ThreadsPerCore=1`，两台主机的
    `slurm.conf` 必须一致。
  - **w2 每次重启后看 `cat /sys/devices/system/cpu/smt/active`，应为 0。** drop-in 的写入已实测，
    重启后是否保持还没有验证过。为 1 时 w2 会回到慢的状态；这时 Slurm 怎么对待多出来的线程没有测过。
  - 回滚：删掉 drop-in、向 `smt/control` 写 `on`、两台主机把节点定义改回 `ThreadsPerCore=2`，在 pass
    边界重启 slurmctld 和两台 slurmd。
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
  这个单元接了 `nhms-node22-unit-failure-alert@` 的 `OnFailure`，并有 `TimeoutStartSec=900`（#3359）。
  各种不更新的情况怎么发现：

  | 情况 | 怎么发现 |
  |---|---|
  | `rsync` 非零退出 | 单元失败 → 告警邮件 |
  | 挂死在 `/ghdc/data` 上 | 15 分钟超时 → 单元失败 → 告警邮件 |
  | 挂死在 `/scratch/frd_muziyao` 上 | 单元超时失败，但**发不出邮件**：告警脚本、发信包装和解释器都在 `/scratch` 下 |
  | 被 `ExecCondition` 跳过（本机 journal 目录不在） | 不算失败，不告警。这时 `/scratch` 没挂上，scheduler 也跑不了，由 node-27 的新鲜度告警报缺 cycle |
  | 定时器没触发（user manager 没起、定时器被停） | 不告警 |

  后三种只能看 `LAST_OK` 的时间是否在一小时内。
- 系统级 `nhms-mounts-ensure.service` / `.timer`（两台主机都有）：开机 90 s 后起、每 2 min 执行
  `mount -a -t nfs4`，并在 slurmd 未运行时启动它。两台主机互为 NFS 服务端，冷启动时先起来的一台
  挂不上对方，靠它自愈。

**系统级文件的仓库副本**（#3359）在 `infra/systemd/compute-system/<主机>/` 下，按主机上的绝对路径存放：
两台各有 `nhms-mounts-ensure.service` / `.timer` 和 slurmd 的 `10-nhms-mounts.conf`，w2 另有
`/etc/tmpfiles.d/nwm-smt-off.conf`。它们由 root 手工安装（`install -m 0644 <仓库副本> <绝对路径>`，
改了单元文件后 `systemctl daemon-reload`），仓库里没有安装脚本。

- 比对（只读，以 root 执行；checkout 只有 root 和生产用户读得到，生产用户不登录 w2）：

  ```bash
  sha256sum -c --strict /scratch/frd_muziyao/NWM/infra/systemd/compute-system/w3.sha256   # w3 上
  sha256sum -c --strict /scratch/frd_muziyao/NWM/infra/systemd/compute-system/w2.sha256   # w2 上
  ```

- 这个比对只证明清单里的文件没变，看不到旁边多出来的文件。
- 不在这棵树里、只记在本文和 receipt 里的主机状态：w2 上的 user manager mask（第 1 节）、
  `/etc/fstab`、`/etc/exports`、`slurm.conf`。

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
   `nhms-compute-scheduler.timer`，等该 pass 结束。然后按这次部署是否改了 `uv.lock`
   （`git diff --name-only HEAD <目标> -- uv.lock`）分两种做法：
   - 没改：`git merge --ff-only <目标>`，立刻重启定时器。
   - 改了：**先不要合入**。定时器保持停着，等 `squeue` 里没有 `nhms_*` 作业（yd 的作业不带这个
     前缀，不用等）——作业是在启动时才进入 checkout、调用 `.venv` 的，合入早了，还在排队的作业
     会拿新代码配旧环境。队列清空后再 `git merge --ff-only <目标>`，接着以生产用户在 checkout 里
     执行 `uv sync --frozen --all-extras --dev`（w3 的环境在迁移时就是这样建的），确认
     `.venv/bin/python -V` 仍是 3.11.x，最后重启定时器。w3 上还没有演练过带 `uv.lock` 变更的部署。
4. 等新代码上的第一个 pass 结束，看 evidence、`scripts/node22_scheduler_stall_health.py` 和
   `systemctl --user --failed`。

**绝不**切换生产 checkout 的分支、手改工作树、`git stash pop`。`.venv` 经 NFS 被两台主机上的
Slurm 作业和定时器共用：日常禁止在这里同步或重建环境；只有部署改了 `uv.lock` 时才同步（步骤 3 的第二种做法），且须定时器已停、
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
  主机上都是 w2 的导出。w2 还没起来时，w3 的 slurmd 应当不起、`workspace/runs` 挂载点下面没有新文件
  （第 3 节；这一条还没有在真实重启中验证过）。
- w2：`cat /sys/devices/system/cpu/smt/active` 为 0；`systemctl is-enabled user@1103.service` 为
  `masked`，`loginctl list-users` 里没有生产用户。
- 两台各跑一次第 4 节的 `sha256sum -c --strict`。
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
