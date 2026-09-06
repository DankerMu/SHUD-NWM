# node-22 / node-27 receipt — issue #2016 I14 canonical 降水镜像一次性回填 + keep 水位不等式核查

- 日期：2026-09-06（全部时间戳为 UTC；node-22/27 本机时区 `+08:00`，`ls -la` 里的 `Sep 6 08:36` 即 `00:36Z`）
- 分支：`feat/issue-2016-precip-copyback-backfill-receipt` ・ epic #2003 (m27) ・ OpenSpec change `display-v2-national-timeline-precip-overlay` 第 4 组（4.5 / 4.6 / 4.11）
- 范围：**零代码改动**。node-22 用钉住解释器执行 `scripts/canonical_precip_copyback_backfill.py` 把 `canonical/{gfs,IFS}/<cycle>/prcp_rate_or_amount/` 与两源 `grid/<grid_id>/grid.json` 镜像到 NFS；node-27 侧目录/容量/可读性 receipt；keep 水位不等式逐源求值。
- 节点：node-22（`frd_muziyao@210.77.77.22`，`/scratch/frd_muziyao/NWM`，计算面）；node-27（`nwm@210.77.77.27`，`/home/nwm/NWM`，display 面）。`/ghdc/data/nwm/`（22）与 `/home/ghdc/nwm/`（27）是同一份 NFS。
- 全程**未执行** `uv sync`、裸 `uv run`、`uv run --active`；每条实机命令原文见 §7。

## 0. 操作前状态

**node-22（00:1x–00:30Z 勘察）**

- 活动 checkout `HEAD = 3acea778`（`fix(scheduler): compact completed evidence before limit fallback (#1932)`，2026-09-02 pull），落后 `origin/master 71fbfe4d` 301 个 commit。
  - `git status --porcelain` 只有 untracked `.nhms-work/`（数据目录，不动）；`git diff --name-only --diff-filter=A 3acea778 origin/master` 与本地 untracked 无同名冲突。
- `.venv/bin/python -V` = `Python 3.12.7`（共享 venv，被 slurm gateway / compute-api 占用）。`git diff --stat 3acea778 origin/master -- pyproject.toml uv.lock .python-version` 只有 `pyproject.toml` 的 pytest 配置 19 行（`tmp_path_retention_policy` + `node27_docker` marker），**无依赖变化**，前滚不需要环境重建。
- 调度器形态：用户级 systemd `nhms-compute-scheduler.timer`（`OnUnitActiveSec=5min`）触发 oneshot `nhms-compute-scheduler.service`。
  - `ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli plan-production --submit --continuous --max-passes 1`，`EnvironmentFile=infra/env/compute.scheduler-dbfree.env` + `nhms-prod/secrets/slurm-gateway.env`。
  - 单 pass ≈ 7.5 min > 5 min，故 passes 背靠背、**没有自然空窗**——回填窗口只能靠停 timer 得到。
- `~/.config/systemd/user/*.service|*.timer` 与其 ExecStart 脚本（`scripts/scheduler_file_provider_refresh_once.sh`）grep `uv run|uv sync|--active` 均无命中：前滚不会让任何单元在下次触发时重建 venv。
- 生效 env（`infra/env/compute.scheduler-dbfree.env`，不是 stale 的 `compute.env`）：`OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store`、`NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`、`NHMS_SCHEDULER_JOURNAL_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`。
  - `NHMS_SCHEDULER_LOOKBACK_HOURS=96`、`NHMS_RETENTION_DAYS=14`、`NHMS_SCHEDULER_ALLOWED_ROOTS` 含 `/ghdc/data/nwm/object-store`。
- 源侧 canonical：`gfs`/`IFS` 各 26 个周期 `2026082312 … 2026090500` + `grid/gfs_0p25`、`grid/ifs_0p25`；每周期 `prcp_rate_or_amount/` 文件数 gfs 56（f003–f168）、IFS 53；两源 `prcp_rate_or_amount` 合计 `du -sb` = 3 426 840 144 B（≈ 3.4 GB，每源每周期 ≈ 66 MB——计划文档里的「23 MB/周期」是 stale 估算，见 §8）。
- NFS：`/ghdc/data/nwm/object-store/` 下**没有** `canonical/`；`df -h /ghdc/data` = `1.7T 1.1T 478G 70%`。`touch`/`rm` 探针证明 `frd_muziyao` 可写该根；umask `0022`。
- 最近 passes 全部 `status=planned`、`candidates=[]`（`cycle_window.cycle_lag_hours=16`，`2026-09-05T12Z` 周期最早 `2026-09-06T04:00Z` 才进入窗口；NFS `raw/{gfs,IFS}/2026090512/` 已于 `20:41Z` 前齐、`manifest.json` `00:16Z` 落盘）。
  - `.err` 里持续的 `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`（18 个 IFS 候选 `blocked:forcing_version_row_absent`，自 `2026-09-01` 起）是既有状况，与本单无关（§8）。

**node-27（00:14–00:21Z 勘察，只读）**

- `/home/nwm/NWM` `HEAD = 71fbfe4d`（origin/master，含 #2009 I5）；生产 `nhms-display-api.service` :8080 进程 `Sep 6 04:01 CST` 起、跑的是该 HEAD，`GET /api/v1/layers/discharge/cycles?source=gfs|ifs` 返回 200（§4 的读数来源）。
- `ls /home/ghdc/nwm/object-store/canonical` → `No such file or directory`。
- `df -h / /home`（改前，00:21:20Z）：

```
/dev/mapper/ubuntu--vg-ubuntu--lv   98G   72G   22G  77% /
/dev/mapper/ubuntu--vg-home        1.7T  1.1T  478G  70% /home
```

## 1. 静默窗口（timer 停/启）与代码前滚

顺序：`timer stop → is-active 非 active/activating → git status --porcelain → git pull --ff-only → dry-run → run1 → run2 → git rev-parse HEAD → timer start`。

| 时刻（UTC） | 动作 / 读数 |
|---|---|
| 00:24:02Z | 最后一个旧代码 pass `scheduler_2026090600_8481c23ac327` `pass:started` |
| 00:30:09Z | `systemctl --user stop nhms-compute-scheduler.timer` → `timer=inactive service=activating`（在跑的 pass 不中断，等它自然结束） |
| 00:31:33Z | 该 pass `finished_at`，`status=planned`；00:31:48Z `is-active` = `inactive`，`pgrep` 无 `services.orchestrator.cli` 进程 |
| 00:35:54Z | runner 起跑：`timer ActiveState=inactive`、`service=inactive`、`orchestrator procs: 0`；`git status --porcelain` = `?? .nhms-work/` |
| 00:35:55Z | `HEAD before: 3acea778f26be4debe54be40c304078eb8a8eb9c`；`git fetch origin master` |
| 00:36:01Z | `git pull --ff-only origin master` → `HEAD after: 71fbfe4d6445edc12c4c1ea43266d6b91314d3ba`（Fast-forward，`git-pull.log` 无冲突）；`python after pull: Python 3.12.7`；`git status after: ?? .nhms-work/` |
| 00:36:01–00:36:43Z | 回填 dry-run / run1 / run2（§2） |
| 00:37:08Z | `systemctl --user cat nhms-compute-scheduler.service` 回读 `ExecStart` 仍为 `.venv/bin/python -m services.orchestrator.cli …`（无 `uv`）；`.err` 行数 588；`git rev-parse HEAD` = `71fbfe4d…`；`systemctl --user start nhms-compute-scheduler.timer` → `timer=active service=activating`（`OnActiveSec=30s` 立即触发首个新代码 pass） |

- 窗口时长 00:30:09Z → 00:37:08Z = **7 min**（其中无 pass 的净空窗 00:31:33Z → 00:37:08Z ≈ 5.5 min）≪ `NHMS_SCHEDULER_LOOKBACK_HOURS=96`，窗口内没有周期到期（下一个新周期最早 04:00Z 进窗口），无周期丢失。
- `git pull` 落在无 pass 的窗口内：oneshot pass 是惰性 import，未在运行中的进程上换代码，不存在混版本进程。

## 2. 回填（node-22，钉住解释器）

runner：`/scratch/frd_muziyao/nhms-2016-receipts/node22-backfill-2016.sh`（原文归档 `.workplans/issue-2016/node22/`），`{ setsid nohup ./node22-backfill-2016.sh > runner.out 2>&1 & }` 分离执行，三次调用命令原文（`--copyback-root` 由 runner 从 `compute.scheduler-dbfree.env` grep 取值并回显）：

```
cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root /scratch/frd_muziyao/nhms-prod/object-store --copyback-root /ghdc/data/nwm/object-store --dry-run
cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root /scratch/frd_muziyao/nhms-prod/object-store --copyback-root /ghdc/data/nwm/object-store
cd /scratch/frd_muziyao/NWM && /scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill --source-root /scratch/frd_muziyao/nhms-prod/object-store --copyback-root /ghdc/data/nwm/object-store
```

| 运行 | 时刻 | rc | 耗时 | `totals` | 条目 | stderr |
|---|---|---|---|---|---|---|
| dry-run | 00:36:01Z | 0 | 0 s | `copied 2836 / skipped 0 / failed 0`（计划值，未写） | 52 cycles + 2 grids，status 全 `ok` | 空 |
| run1 | 00:36:01→00:36:42Z | 0 | 40 s | `copied 2836 / skipped 0 / failed 0` | 52 cycles + 2 grids，status 全 `ok`，`errors` 全空 | 空 |
| run2（幂等） | 00:36:42→00:36:43Z | 0 | 1 s | **`copied 0 / skipped 2836 / failed 0`** | 52 cycles + 2 grids，status 全 `ok`（无 `no_precip_products`） | 空 |

- 2836 = 26 × 56（gfs）+ 26 × 53（IFS）+ 2 个 `grid.json`。run2 判据 `totals.copied == 0 && totals.failed == 0` 且逐条 `status ∈ {ok}` 成立。三份 JSON 汇总原文：`.workplans/issue-2016/node22/summary-{dry-run,run1,run2}.json`。
- 逐周期（run1 / run2）：

| source | cycle | run1 copied/skipped/failed | run2 copied/skipped/failed | status |
|---|---|---|---|---|
| IFS | 2026082312 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082400 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082412 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082500 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082512 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082600 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082612 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082700 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082712 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082800 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082812 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082900 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026082912 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026083000 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026083012 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026083100 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026083112 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090100 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090112 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090200 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090212 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090300 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090312 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090400 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090412 | 53/0/0 | 0/53/0 | ok/ok |
| IFS | 2026090500 | 53/0/0 | 0/53/0 | ok/ok |
| gfs | 2026082312 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082400 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082412 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082500 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082512 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082600 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082612 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082700 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082712 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082800 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082812 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082900 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026082912 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026083000 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026083012 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026083100 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026083112 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090100 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090112 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090200 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090212 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090300 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090312 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090400 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090412 | 56/0/0 | 0/56/0 | ok/ok |
| gfs | 2026090500 | 56/0/0 | 0/56/0 | ok/ok |
| IFS | grid/ifs_0p25 | 1/0/0 | 0/1/0 | ok/ok |
| gfs | grid/gfs_0p25 | 1/0/0 | 0/1/0 | ok/ok |

- 目的端校验（node-22 视角，00:36:43Z）：每个周期 `prcp_rate_or_amount/` 文件数源 = 目的（gfs 26 × 56、IFS 26 × 53，`dest-counts-*.txt` 零 MISMATCH）。
  - `du -sh` = `1.7G canonical/gfs` + `1.6G canonical/IFS`；模式 `canonical/`、`canonical/{gfs,IFS}/`、`<cycle>/prcp_rate_or_amount/` 均 `drwxr-xr-x`，`.nc` 与 `grid.json` 均 `-rw-r--r--`（脚本显式 chmod 0755/0644）。
- 容量：`df -h /ghdc/data` 改前 `478G` 可用 → 改后 `474G` 可用（`1.7T 1.1T … 70%` 不变）；增量 ≈ 3.4 GB 与源侧 `du -sb` 一致。

## 3. node-27 视角（`nwm` 账号，00:37:30Z）

```
$ ls /home/ghdc/nwm/object-store/canonical/gfs
2026082312 2026082400 2026082412 2026082500 2026082512 2026082600 2026082612 2026082700 2026082712 2026082800 2026082812 2026082900 2026082912 2026083000 2026083012 2026083100 2026083112 2026090100 2026090112 2026090200 2026090212 2026090300 2026090312 2026090400 2026090412 2026090500 grid
$ ls /home/ghdc/nwm/object-store/canonical/IFS
2026082312 2026082400 2026082412 2026082500 2026082512 2026082600 2026082612 2026082700 2026082712 2026082800 2026082812 2026082900 2026082912 2026083000 2026083012 2026083100 2026083112 2026090100 2026090112 2026090200 2026090212 2026090300 2026090312 2026090400 2026090412 2026090500 grid
$ ls -la …/canonical/gfs/grid/*/grid.json …/canonical/IFS/grid/*/grid.json
-rw-r--r-- 1 frd_muziyao nfsdata 3392 Sep  6 08:36 /home/ghdc/nwm/object-store/canonical/gfs/grid/gfs_0p25/grid.json
-rw-r--r-- 1 frd_muziyao nfsdata 3392 Sep  6 08:36 /home/ghdc/nwm/object-store/canonical/IFS/grid/ifs_0p25/grid.json
$ stat -c '%a %U %n' …
755 frd_muziyao /home/ghdc/nwm/object-store/canonical
755 frd_muziyao /home/ghdc/nwm/object-store/canonical/IFS
755 frd_muziyao /home/ghdc/nwm/object-store/canonical/IFS/2026090500
755 frd_muziyao /home/ghdc/nwm/object-store/canonical/IFS/2026090500/prcp_rate_or_amount
644 frd_muziyao /home/ghdc/nwm/object-store/canonical/IFS/2026090500/prcp_rate_or_amount/IFS_2026090500_prcp_rate_or_amount_f000.nc
755 frd_muziyao /home/ghdc/nwm/object-store/canonical/gfs/grid/gfs_0p25
644 frd_muziyao /home/ghdc/nwm/object-store/canonical/gfs/grid/gfs_0p25/grid.json
644 frd_muziyao /home/ghdc/nwm/object-store/canonical/IFS/grid/ifs_0p25/grid.json
$ head -c 8 <IFS f000 .nc> | od -c   → 211 H D F \r \n 032 \n   （HDF5/NetCDF4 魔数，nwm 实读成功）
$ head -c 8 <ifs_0p25/grid.json> | od -c → { " a x i s _ o   ；gfs_0p25/grid.json 同
```

- 每周期文件数（node-27 视角）：`26 × gfs 56`、`26 × IFS 53`，与源侧一致；`IFS` 目录名大小写与源一致（无小写 `ifs`）。
- `df -h / /home` 改后（00:37:30Z）：`/` `98G 72G 22G 77%`（不变）；`/home` `1.7T 1.1T 474G 70%`（478G → 474G）。
- 覆盖集合（spec「keep 集合覆盖全部可列周期 + lead-0 借片」）：`cycles?source=` 全集（§4，`2026-08-27T00Z … 2026-09-05T00Z` 连续 19 个 00Z/12Z 周期）∪ {oldest − 12h, oldest − 24h} = 21 个 token，全部 ⊆ `ls canonical/gfs/` 与 `ls canonical/IFS/`（镜像最老 `2026082312`），missing = []。
- **外部变动（记录，不处置）**：00:34:24Z（`08:34:24 CST`）生产 `nhms-display-api.service` 被本单之外的操作重启，`/home/nwm/NWM` 的 HEAD 变为 `5a86841c`（#2073 合并前的 master），`cycles` 端点因此在 00:38:09Z 返回 404 `{"detail":"Not found"}`。§4 的 cycles 读数取自 00:21:20Z、`71fbfe4d` 在跑时的 :8080，读数有效；本单不动 node-27 部署（I15 范围）。

## 4. keep 水位不等式（node-27，2026-09-06T00:21:20Z 采样）

数据来源：
- `oldest_listed_cycle`：`GET http://127.0.0.1:8080/api/v1/layers/discharge/cycles?source=<s>`（生产 display API :8080，master `71fbfe4d`，含 #2009 I5），取 `data.cycles[-1].cycle_time`。
- `display_watermark` 与 `cutoff`：`scripts/node27_raw_retention.py` plan-only 汇总。
  - 调用形态：`NODE27_RAW_RETENTION_PLAN_ONLY=true`，`--summary-path /home/nwm/tmp/issue-2016/raw-retention-plan-only.json`，env 来自 `infra/env/node27-raw-retention.env`，`anchor.mode = display_watermark`，`execution_mode = plan_only`，`counts.deleted = 0`）。
  - `retention_days = 14`（`NODE27_RAW_RETENTION_DAYS`，未调整）。

| source | n | newest_listed | oldest_listed_cycle | oldest − 24h | display_watermark | retention_days | cutoff = watermark − retention_days | oldest − 24h ≥ cutoff |
|---|---|---|---|---|---|---|---|---|
| gfs | 19 | 2026-09-05T00:00:00Z | 2026-08-27T00:00:00Z | 2026-08-26T00:00:00Z | 2026-09-05T00:00:00Z | 14 | 2026-08-22T00:00:00Z | **成立**（余量 4 d） |
| ifs | 19 | 2026-09-05T00:00:00Z | 2026-08-27T00:00:00Z | 2026-08-26T00:00:00Z | 2026-09-05T00:00:00Z | 14 | 2026-08-22T00:00:00Z | **成立**（余量 4 d） |

- 两源不等式均成立，`retention_days` 保持 14，无偏离。
- 备注：cycles 端点自带 12 天 lookback（#2009 决策 15），最老可列周期上界为 `now − 12 d`。
  - 镜像源侧（node-22 canonical，`NHMS_RETENTION_DAYS=14`）当前最老周期 `2026082312`，也覆盖 `oldest_listed − 24h`。
- 已知限制：node-27 侧 `node27_raw_retention.py` 尚未把 `canonical/<S>/<K>` 纳入剪枝目标（4.4 / #2011 未落地），故 NFS 镜像在 #2011 合并前只增不减；该 plan-only 汇总的 `planned = 4` 是 raw 目录里早于 cutoff 的 4 个周期（`2026-08-21T00Z/12Z` × 2 源），与本单无关且 plan-only 未删除。

## 5. 首个新代码 pass（node-22，`HEAD 71fbfe4d`）

| pass_id | started | finished | status | candidates | retention（新代码） |
|---|---|---|---|---|---|
| `scheduler_2026090600_ef252ee9668d`（timer start 后首个） | 00:37:10Z | 00:45:16Z | `planned` | 0 | `completed`，planned 0 / deleted 0 / skipped 9649 |
| `scheduler_2026090600_6926eed315a6` | 00:45:25Z | 00:53:32Z | `planned` | 0 | 同上 |
| `scheduler_2026090600_296ae6d9cdc5` | 00:53:41Z | 01:01:48Z | `planned` | 0 | 同上 |

- `pass:finished` 正常；`.err` 行数 588 → 588（timer start 前后），新增 traceback 0。
- 三个新代码 pass 之后（01:06:40Z）NFS 镜像完好：`canonical/gfs` 26 周期 / 1456 `.nc`、`canonical/IFS` 26 周期 / 1378 `.nc`、2 个 `grid.json`——新代码的 in-pass retention 没有把 copyback root 上的 `canonical/` 当剪枝目标（与 `services/orchestrator/retention.py` 「additional roots 只剪 `runs/`」一致）。
- 既有 `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`（§8）在 `.err` 里未新增行——因为 `.err` 只在 circuit 状态变化时写；其 18 个被阻候选前滚后不变。

## 6. 新周期镜像（`convert` 终态 → NFS + journal `canonical_precip_mirror`）——**PR #2090 冻结时未观测，§6.1 已补录**

- 触发点：timer start（00:37:08Z）之后首个进入 `convert` 终态的周期，按 `cycle_window.cycle_lag_hours=16` 预计为 `2026090512`，最早 `2026-09-06T04:00Z` 进入候选窗口（NFS raw 已齐）；运维方口径整条链落地约需再等 ~10 h。
- 判据（tasks.md 4.11a / runbook §7.4）：
  - journal：`$NHMS_SCHEDULER_JOURNAL_ROOT/journal/<S>/2026090512*.jsonl` 里 `record_type == "pipeline_event"` 且 `payload.event_type == "canonical_precip_mirror"` 的记录，`payload.details.precip_mirror.status == "ok"`；
  - trees：`object_key` 以 `/prcp_rate_or_amount` 结尾的 `trees[]` 项 `action == "copy"` 且 `status == "copied"`；`grid/<grid_id>` 项因本次回填已镜像而合法地为 `skip`/`skipped`；
  - NFS：出现 `canonical/<S>/2026090512/prcp_rate_or_amount/` 且 node-27 以 `nwm` 可读（hook 走 publisher 拷贝助手，不是回填脚本的显式 chmod，须单独抽查）。
- 状态（PR #2090 冻结时）：未观测。tasks.md 4.11 / 4.11a 当时保持未勾选；**#2068 前置条件 1 当时判为未满足**。事件落地后以 docs-only 补录 commit 追加到本节（同一文件），并在 #2068 留证。**已于 2026-09-06T09:38–09:40Z 完成补录，结论见 §6.1：两源事件均 `ok`，4.11 / 4.11a 均已勾选，#2068 前置条件 1 已满足。**

### 6.1 补录（2026-09-06T09:38–09:40Z 观测，docs-only）

- 观测时间：node-22 `2026-09-06T09:38:08Z`–`09:38:57Z`、node-27 `2026-09-06T09:39:14Z`（两端 `date -u +%FT%TZ` 实读）。本次**全程只读**，命令清单见 §7 补录段。
- journal 绝对根（从 node-22 生效 env `infra/env/compute.scheduler-dbfree.env` 解析）：`NHMS_SCHEDULER_JOURNAL_ROOT=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`，故事件文件在 `$NHMS_SCHEDULER_JOURNAL_ROOT/journal/<S>/`（注意 `journal/journal/` 两层；`object-store/journal/…` 不是该根）。
- 本周期两源各只有单段 `2026090512.jsonl`（未触发 `MAX_FILE_JOURNAL_CYCLE_SEGMENTS` 轮转），`<cycle>*.jsonl` glob 与单文件结果等价。
- node-22 状态：`git rev-parse --short HEAD` = `71fbfe4d`（与 §5 一致，未前滚）；`nhms-compute-scheduler.timer` `ActiveState=active`、`ActiveEnterTimestamp` = `2026-09-06T00:37:08Z`。

时序（全部 UTC，取自 journal 记录自身的 `created_at`，不依赖文件 mtime）：

| 源 | `convert` job | convert started | convert finished | `canonical_precip_mirror` `created_at` |
|---|---|---|---|---|
| gfs | `job_cycle_gfs_2026090512_convert_cohort_44f168c553e3_convert` | 04:14:36Z | 04:15:19Z | 04:15:54.045217Z（`payload.created_at` 04:15:53.987633Z） |
| IFS | `job_cycle_ifs_2026090512_convert_cohort_15774060b152_convert` | 04:14:55Z | 04:15:45Z | 04:16:09.253363Z（`payload.created_at` 04:16:09.146505Z） |

两源事件均落在 timer start（00:37:08Z）之后，且在 §6 预测的「最早 04:00Z 进入候选窗口」内；覆盖该时间窗的 scheduler pass 是 `scheduler_2026090604_428c99830451`（`started_at` 04:04:56.359032Z）。

journal 摘要（`record_type == "pipeline_event"`、`payload.event_type == "canonical_precip_mirror"`，每源命中数 **1**；信封字段 `schema_version=nhms.scheduler.file_orchestration_journal.v1`、`sequence=9`、`payload.status_to="ok"`；`payload.details` 除 `precip_mirror` 外为空）：

```json
{"cycle":"2026090512","file_count":57,"root":"[local-path]","status":"ok","storage_source":"gfs","trees":[
  {"action":"copy","byte_count":67794864,"file_count":56,"object_key":"canonical/gfs/2026090512/prcp_rate_or_amount","status":"copied"},
  {"action":"skip","byte_count":3392,"file_count":1,"object_key":"canonical/gfs/grid/gfs_0p25","status":"skipped"}]}
{"cycle":"2026090512","file_count":54,"root":"[local-path]","status":"ok","storage_source":"IFS","trees":[
  {"action":"copy","byte_count":64007033,"file_count":53,"object_key":"canonical/IFS/2026090512/prcp_rate_or_amount","status":"copied"},
  {"action":"skip","byte_count":3392,"file_count":1,"object_key":"canonical/IFS/grid/ifs_0p25","status":"skipped"}]}
```

判据逐条：`status == "ok"` ✅（两源）；`prcp_rate_or_amount` 树 `action == "copy"` 且 `status == "copied"` ✅（两源）；`grid/<grid_id>` 树 `skip`/`skipped` ✅——`grid.json` 已被 §2 回填，按 4.11a 明文合法，不要求 `copy`。`root` 字段本身在 journal 里就是字面量 `[local-path]`（写入侧脱敏），照录。

NFS 目录（node-22 视角，`/ghdc/data/nwm/object-store/`；`ls` 显示的 `Sep 6 12:16` = `04:16Z`）：

| 源 | 目录 | mode/owner | `.nc` 数 | 与事件 `file_count` 一致 |
|---|---|---|---|---|
| gfs | `canonical/gfs/2026090512/prcp_rate_or_amount/` | `755 frd_muziyao:huser` | 56 | ✅ 56 |
| IFS | `canonical/IFS/2026090512/prcp_rate_or_amount/` | `755 frd_muziyao:huser` | 53 | ✅ 53 |

表中「与事件 `file_count` 一致」比的是 prcp 树自身的 `trees[].file_count`（56 / 53）；事件顶层 `file_count`（57 / 54）多出的 1 是同一事件里 `skip` 的 `grid.json`。

两源 `canonical/<S>/` 下条目数现为 28 = 27 个周期目录 + 1 个 `grid/`，相对 §5（01:06:40Z 的 26 个周期目录）净增 `2026090512` 一个新周期；总 `.nc` gfs 1456 → 1512（+56）、IFS 1378 → 1431（+53），增量与事件 `trees[].file_count` 逐源吻合。

node-27 可读性实测（`nwm` 账号，同一份 NFS 的 `/home/ghdc/nwm/object-store/` 前缀）——hook 走 publisher 拷贝助手、**没有**回填脚本的显式 `chmod 0644`，故必须实测而非推断：

| 检查 | gfs | IFS |
|---|---|---|
| 周期目录 / `prcp_rate_or_amount/` `stat -c '%a %U:%G'` | `755 frd_muziyao:nfsdata` | `755 frd_muziyao:nfsdata` |
| 目录内 `.nc` mode 去重（`stat -c %a`） | 全部 `644` | 全部 `644` |
| 首文件 `head -c 8` 经 `od -c` | `211 H D F \r \n 032 \n`（HDF5 魔数） | 同左 |
| 末文件（`…_f168.nc`）`head -c 8` | 同上，HDF5 魔数 | 同上，HDF5 魔数 |
| `[ -r ]` 以 `nwm` 判定 | `readable_by_nwm=yes` | `readable_by_nwm=yes` |

结论：hook 写出的树 owner 是 `frd_muziyao:nfsdata`、mode `0755`/`0644`，`nwm` 走 other 位可读，实读到 HDF5 魔数——与回填脚本显式 chmod 的结果等价，**AC5 可读性判据满足**。两源 `grid/<grid_id>/grid.json` 仍为 `644`（本次 `skip` 未改动）。node-27 容量 `df -h`：`/` 98G 用 72G（77%）、`/home` 1.7T 用 1.2T（424G 可用）；本次镜像由 node-22 写入 NFS，27 侧只读。

`runs/` copyback 照常（4.11 的第二半判据）：`2026090512` 的 run 目录源侧（`/scratch/frd_muziyao/nhms-prod/object-store/runs/`）与 NFS 侧各 **gfs 38 / IFS 38**，数量一致。
抽样 `fcst_gfs_2026090512_dg_0883c7e9c1006c6fd347df500315e9df` 具备 `input/`、`logs/`、`output/`（含 `output/state_checkpoints/`）完整结构，mtime `05:55:19Z`；`canonical/` 与 `runs/` 两条路径互不干扰。

scheduler 健康度：`.err` 路径由 unit 的 `StandardError=append:…/workspace/scheduler/logs/nhms-compute-scheduler.err` 回读确定，行数 **588 → 588**，与 §5 的 timer start 前后读数一致，本次窗口零新增行、零新增 traceback（文件内累计 23 处 `Traceback` 为历史存量，非本次新增）。
`.err` 只在 circuit 状态变化时写，§8 的 `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN threshold=3 open=18` 仍是尾行，状态未变。

**判定**：tasks.md 4.11 与 4.11a 判据全部满足（新周期 `canonical_precip_mirror` 事件 + NFS 目录 + node-27 `nwm` 可读 + `runs/` copyback 不变 + 新代码 pass `pass:finished` 且 `.err` 无新 traceback），本补录 commit 一并勾选两项；**#2068 前置条件 1（本回执 §6 补录完成）已满足**。

## 7. 命令审计（无 `uv sync` / 裸 `uv run` / `--active`）

node-22 实机执行过的全部写/状态变更命令（其余均为只读 `ls/df/du/stat/grep/git log/git diff/systemctl show|cat|is-active`）：

```
systemctl --user stop nhms-compute-scheduler.timer                       # 00:30:09Z
git fetch origin master && git pull --ff-only origin master              # runner 内，00:35:55–00:36:01Z，cwd /scratch/frd_muziyao/NWM
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill … --dry-run   # 见 §2
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill …             # run1
/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill …             # run2
systemctl --user start nhms-compute-scheduler.timer                      # 00:37:08Z
```

node-27 实机执行过的命令全部只读（`curl`、`ls`、`stat`、`head -c 8`、`df`、`python3` 集合比对）加一次 **plan-only** retention：

```
cd /home/nwm/NWM && set -a && . infra/env/node27-raw-retention.env && set +a && export TMPDIR=/home/nwm/tmp && \
  NODE27_RAW_RETENTION_PLAN_ONLY=true /home/nwm/NWM/.venv/bin/python scripts/node27_raw_retention.py \
  --summary-path /home/nwm/tmp/issue-2016/raw-retention-plan-only.json      # rc=0，execution_mode=plan_only，counts.deleted=0
```

（该次调用没有显式 `PYTHONPATH=/home/nwm/NWM`，靠 cwd 解析 `packages.common`，结果与 tasks.md Evidence Floor 规定形态等价；`NODE27_DISPLAY_WATERMARK_DATABASE_URL` 未回显。）

### 7.1 补录窗口命令审计（2026-09-06T09:38–09:40Z，两端全只读）

本次补录**没有任何写/状态变更命令**。node-22 只跑 `date -u`、`ls`、`stat`、`find`、`wc`、`grep`、`jq`、`cut`、`git rev-parse --short HEAD`、`git status --porcelain`（计数用）、`systemctl --user is-active` / `show` / `cat`；node-27 只跑 `date -u`、`id -un`、`ls`、`stat`、`head -c 8`、`od -c`、`[ -r ]`、`df -h`。
**未执行** `uv sync` / 裸 `uv run` / `uv run --active`（本次两端连 Python 都未调用），未 `git pull`、未改动任何 gitignored 目录、未连接 `:55433`、未回显 `NODE27_DISPLAY_WATERMARK_DATABASE_URL` 或 `DATABASE_URL`（env 读取用 `grep -E` 白名单键并显式滤除 `database`/`url`）。

判读用的 `jq` 选择器（runbook §7.4 口径，`<S>` ∈ {`gfs`,`IFS`}）：

```bash
J=/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal/journal
jq -c 'select(.payload.event_type=="canonical_precip_mirror") | .payload.details.precip_mirror' "$J/<S>/2026090512"*.jsonl
```

## 8. 顺带发现（不在本单修）

- `docs/plans/2026-09-03-display-v2-header-river-precip-timeline.md:25/145` 的「每源每周期约 23 MB / 26 周期 ≈ 1.2 GB」stale：实测 `prcp_rate_or_amount` 每源每周期 ≈ 66 MB，两源 26 周期 ≈ 3.4 GB。NFS `/home` 还有 474 GB，本单不构成容量风险，但 #2011 的剪枝与 I15 的容量核查应按实测值算。
- node-22 `.err` 持续 `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN threshold=3 open=18`：18 个 IFS 候选（`2026-08-29T00Z` 起）`blocked:forcing_version_row_absent`，自 `2026-09-01` 累计 58+ passes；与本单无关、前滚前后一致。
- node-27 生产 display API 在 00:34Z 被外部操作回退到 `5a86841c`（无 I5 `cycles` 端点）；#2010 / I15 的 live 验收前须先把 `/home/nwm/NWM` 恢复到含 #2073 的 master。
- NFS 上的 `canonical/<S>/<K>` 在 #2011（4.4）合并前不被任何 retention 剪枝（node-22 的 in-pass retention 对 copyback root 只剪 `runs/`）；每天净增 ≈ 2 × 2 × 66 MB。
- node-22 `infra/env/compute.env` 的 `OBJECT_STORE_ROOT=/ghdc/data/nwm/workspace/22-e2e/object-store` 仍 stale（计划文档已记），生效文件是 `compute.scheduler-dbfree.env`。
