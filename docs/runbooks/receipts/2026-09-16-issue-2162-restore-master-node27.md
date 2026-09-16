# node-27 live receipt — issue #2162（目标 2）：活动树 `415cbd1e` 恢复到 master `f2476c8c` + 8 unit 逐项复核

- 日期：2026-09-16 UTC（node-27 本地 CST 15:13:01–15:14:07）
- 节点：node-27（`210.77.77.27`），活动树 `/home/nwm/NWM`
- 执行者：Claude Fable 5.1；GO record 见 issue #2162 评论（2026-09-16，动手前冻结 OLD/NEW SHA 与各项 owner）
- 窗口脚本与证据：`stage-b.sh`（md5 `a6c082f17e9b1ee3c97abf464958b007`）与 `window/`（脚本全文日志 `stage-b.log`、9 unit 捕获、
  unit-vs-master diff、prewarm / tick summary、timers、ledger、df、HOLD 目录、前端构建日志）随本 receipt 提交在同名目录
  `2026-09-16-issue-2162-restore-master-node27/`。node-27 上的原件在备份目录 `/home/nwm/nhms-restore-master-backup-2162-20260916T071301Z/evidence/`。
- 前置（目标 1 receipt `2026-09-13-issue-2162-unpin-node27-runtime.md` §1 写明的复查条件）：`000059` 已在活库（2026-09-14 21:10:42Z）；
  #1895 R5.2 交接已于 2026-09-16T04:08Z 落地（compression unit 绑到独立 reviewed checkout）；补解析 flock 无持有者；8 timer 已全部恢复；
  用户 2026-09-16 裁决「#1895 落地了，可以继续」。

## 1. 决策留痕（issue 验收第 1 项）

**恢复 master。** OLD = `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2`（分支 `issue1987-reviewed-new-415cbd1e`，upstream
`origin/issue1987-frozen-new-415cbd1e`，porcelain 0）；NEW = `f2476c8c29774d40b300d4a8a13657b5d6d05139`（`origin/master`，OLD 之后 196 提交、0 落后）。
脚本在 T0 `git fetch` 后断言 `origin/master == NEW`，master 若前移则不动、另行决策（实测相等）。
窗口 owner / runtime-pin owner / capacity-hold owner = 用户；执行 = 本 session。DBA：本窗口零 DB 变更（OLD/NEW 的 `db/migrations` 头都是 `000059`；
`db/roles/node27_write_roles.sql` 的 `nhms_cold` grant 变更属 #1895 R5.3 owner，未施加）。
不套用 `tier-node27-timeseries-storage.md` §4.10.2「先停 service」：无迁移、API 只读；checkout + 前端构建期间旧进程继续服务，末尾一次重启（见 §2 暴露区间）。

## 2. 结论

- `/home/nwm/NWM` 在 `master` @ `f2476c8c`，upstream `origin/master`，`git pull --ff-only` 为 `Already up to date.`，porcelain 改前/改后均 **0**（无需备份、无 stash）；
  `e0cfe40b` 是 HEAD 祖先（验收第 2 项）。`issue1987-reviewed-new-415cbd1e` 本地分支保留、`origin/issue1987-frozen-new-415cbd1e` 未动（均仍指 `415cbd1e`）。
- 三件套 `pyproject.toml uv.lock .python-version` 与 `apps/frontend/{package.json,pnpm-lock.yaml}` OLD→NEW **0 行差异**：`.venv`（Python 3.11.15）与
  `node_modules` 复用，未 `uv sync`、未 `pnpm install`。
- 生产 display（`:8080`）在 master 重启：MainPID 1086781 → **2846999**，`/proc` 实测 `cwd=/home/nwm/NWM`、
  `cmdline=/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2`，进程环境
  `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`、含 `NHMS_DISPLAY_CACHE_WARM_TOKEN`（值不记录）。重启 07:13:40Z → `/health` 200 于 07:13:42Z。
  `yd-NWM` `:8081` pid `3163766` 前后不变、`/health` 200。
- 读面 + 前端门禁 **0 个 500**（§6）；公网 `https://test.nwm.ac.cn` 的 `/`、5 个 bundle、`/ops`、`/api/v1/layers` 全 200，三个极端 instant 均 422 `VALIDATION_ERROR`。
- 前端 bundle：`vite build` 在树外（`/home/nwm/tmp/2162/dist-new-…`）从 master 构建 16.3 s、2542 modules，原子 `mv` 换入 `apps/frontend/dist`。
  构建产物（15 个 asset + `index.html`）与被换出的 09-14 dist **逐字节相同**——即现网自 09-14 起服务的已是 master 前端
  （master 上 `apps/frontend/src` 最后一次提交 `cf47ac22a` 为 2026-09-14T02:35Z，早于该 dist 的 07:19Z 构建时间；它不是当时 checkout 的 `a8db554d` 的产物，
  `a8db554d..master` 前端 src 差 48 文件）。本次重建把这一点从推断变成实测。
- 手工 prewarm：`.locks/**` 前 8520 → 后 8520（**不增**）；`.pbf` 11423 → 11423（全部 173 请求 cache hit，0.72 s）；瓦片失败 **0**，10 个失败全部是
  降水 PNG `404 PRECIP_CYCLE_NOT_MIRRORED reason=mirror_root_unconfigured`（`NHMS_PRECIP_MIRROR_ROOT` 未配，#2017；脚本 rc=1 仅因 failed_count>0，与目标 1 同因）。
- writer 停机 07:13:06Z → 07:13:45Z（**39 秒**）。首个自然 autopipe tick 从 master 跑完 `Result=success`（15:13:45–15:13:56 CST，11 s）：
  `seed_failed stage=import` 0 条、`CACHE_WARM_TOKEN unset` 0 条、tick 内 prewarm `rc=0`（183 请求 / 173 hit / 10 PNG 404）。download 首跑 `success`（15:13:45 CST）。
- **9 个 unit（8 + `mvt-cache-retention`）窗口末捕获与窗口前逐字相同**（`units-diff.txt` 0 字节）；drop-in 0；compression 仍绑 `/home/nwm/NWM-maintenance-reviewed-95481481`（#1895 围栏）；
  `~/.config/systemd/user/nhms-*` 与 `infra/env/*.env` 与备份 `cmp` 全部相同（本窗口**没有改任何 unit / env 文件**）。
- 容量 HOLD 目录 `ls -la` 改前后逐字相同；三卷 `df` 改前后逐字相同（`/` 81%、`/home` 31%、`/data/GHDC` 15%）。
- 混版本暴露区间：checkout 07:13:17Z → display 重启 07:13:40Z（23 s，旧进程服务已加载模块 + 新 dist 从 07:13:39Z 起）；公网无 5xx 观测。

## 3. 8 个 unit 逐项复核（基线 = 目标 1 receipt §3 的 `attempt-2/units-final.txt`；本窗口 = `window/units-before.txt` / `units-final.txt`）

| unit | 目标 1 基线 → 本窗口前 | 本窗口前 → 本窗口末 | 已装 unit vs master `infra/systemd`（`window/unitdiff-*`） | 首跑观测 |
|---|---|---|---|---|
| `nhms-display-api` | 相同 | 相同（MainPID 1086781 → 2846999，cwd `/home/nwm/NWM`） | 0 行 | §2 |
| `nhms-node27-autopipe` | 相同 | 相同 | 0 行（service/timer） | 15:13:45 CST `success`，11 s |
| `nhms-node27-download` | 相同 | 相同 | 0 行 | 15:13:45 CST `success` |
| `nhms-node27-raw-retention` | 相同 | 相同 | **6 行**（已装缺 `ExecStartPre mkdir` + Description，#2285；未改） | 下次 2026-09-17 03:35Z；见 §8 预告 |
| `nhms-node27-timeseries-retention` | 相同 | 相同 | service 0 行；**timer 4 行**（已装 05:15Z vs 仓库 06:36Z，仓库值自 `59a26eca6`；未改，归 #2285） | 下次 2026-09-17 05:15Z |
| `nhms-node27-timeseries-compression` | **不同**：#1895 R5.2 已把 `WorkingDirectory` / `ExecStartPre` / `ExecStart` / `Environment` 绑到 `/home/nwm/NWM-maintenance-reviewed-95481481`（wall 3941 s、私有 env） | 相同（围栏保留） | 12 行（= #1895 绑定，有意；master 的 unit **不得** install） | 本日 12:25:32–12:31:42 CST `success`（#1895 交接后） |
| `nhms-node27-resource-governance` | 相同 | 相同 | 0 行 | 本日 12:10 CST `success`；下次 2026-09-17 04:10Z |
| `nhms-node27-frontier-alert` | 相同 | 相同 | 0 行 | 窗口内 15:00:06 CST 是改前；改后首跑见 §7 |
| `nhms-node27-mvt-cache-retention`（目标 1 部署） | — | 相同 | 0 行 | 本日 04:05:32Z `production_execute` `failed=[]`，健康判据 rc=0 |

replay unit `nhms-node27-timeseries-compression-replay.service`：`LoadState=not-found`（#1895 absent-approved，T0 复验）。`91-*` 围栏 0。

## 4. 备份与回滚

- 备份目录（0700）：`/home/nwm/nhms-restore-master-backup-2162-20260916T071301Z/`：`git-identity-before.txt` / `git-identity-after.txt`、
  `cp --parents` 的 18 个 `~/.config/systemd/user/nhms-*` 与 14 个 `infra/env/*.env`（清单 `window/backup-list.txt`）、被换出的整份前端 dist
  `frontend-dist-415cbd1e/`（与新 dist 逐字节相同）、`evidence/`（脚本 + 窗口输出）。
- 回滚（只在读面门禁通过前自动执行，本窗口未触发；通过后 RECORD 不回滚）：

```bash
cd /home/nwm/NWM && git checkout issue1987-reviewed-new-415cbd1e && [ "$(git rev-parse HEAD)" = 415cbd1e9d0eee39ba0dfb623a586b02cbb340f2 ]
BK=/home/nwm/nhms-restore-master-backup-2162-20260916T071301Z
mv apps/frontend/dist "$BK/frontend-dist-new-rejected" && mv "$BK/frontend-dist-415cbd1e" apps/frontend/dist
systemctl --user daemon-reload && systemctl --user restart nhms-display-api.service && curl -s http://127.0.0.1:8080/health
```

## 5. 窗口时序

| 时刻（UTC） | 步骤 |
|---|---|
| 07:13:01 | T0：前置断言（HEAD=OLD、origin/master=NEW、三件套 0 差异、drop-in 0、replay not-found、flock 无持有者）、9 unit 捕获、读面基线 |
| 07:13:06 | 停 `autopipe` / `download` / `frontier-alert` timer，20 s 后三 service 均 inactive |
| 07:13:17 | 备份；`git checkout -B master origin/master`；`git pull --ff-only` = Already up to date |
| 07:13:20–07:13:39 | `vite build --outDir` 树外构建（vite 内计时 16.3 s，含启动 19 s） |
| 07:13:39 | 原子换 dist |
| 07:13:40 | `systemctl --user restart nhms-display-api.service`；07:13:42 `/health` 200 |
| 07:13:42 | 读面 + 前端门禁通过（不可回退点） |
| 07:13:44 | prewarm（8520 → 8520） |
| 07:13:45 | timer 回放；autopipe / download 立即触发并 success |
| 07:14:05 | 公网复测 |
| 07:14:07 | 终态捕获；`DONE rc=0` |

## 6. 读面门禁（`:8080` 本地；改后）

| 路由 | 结果 |
|---|---|
| `/api/v1/layers` · `/api/v1/runs` · `/api/v1/models?limit=1` | 200 / 200 / 200 |
| `/api/v1/layers/discharge/cycles?source=gfs` | 200（cycle `2026-09-14T00:00:00Z`） |
| `river-network-national/5/25/12.pbf` | 200，ETag `W/"m16-bd3332e7…"` 改前后**相同**，`x-tile-cache: hit` |
| `hydro-national/q_down/{vt}/4/12/6.pbf` · `hydro-national/gfs/{cycle}/q_down/{vt}/4/12/6.pbf` | 200 / 200（hit） |
| `hydro/{run}/q_down/{vt}/7/100/50.pbf` | 404（run 无该 valid time；非 5xx） |
| `/api/v1/layers?offset=999999999` | 200 `data_len=0` |
| `/` · 5 个 `assets/*` · `/ops` · `/geo/national-basin-river.geojson` | 全 200；`/` 引用的 bundle 集合 == 构建产物 |

公网（`https://test.nwm.ac.cn`）：同上全 200；三个极端 instant 的 hydro-national 与 hydro/{run} 均 422 `VALIDATION_ERROR`。

## 7. 窗口后补证（脚本窗口外，orchestrator 于 2026-09-16T07:45Z `systemctl --user show` 取证）

- `nhms-node27-frontier-alert`：改后首跑 15:30:01 CST `Result=success`，`ExecStart=/home/nwm/NWM/scripts/node27_frontier_stall_alert_once.sh`。
- `nhms-node27-autopipe`：改后第二个及后续自然 tick 15:23:45 / 15:33:45 / 15:44:40 CST 均 `success`（最近一次 15:44:40–15:44:51 CST，11 s）。
- `nhms-node27-download`：15:44:40 CST `success`。
- display MainPID 2846999 `active`，`/health` 200；`git status --porcelain` 0，分支 `master`。
- `systemctl --user --failed` 中无 `nhms-display-api` / `nhms-node27-*`；列出的 failed 全是窗口前就存在的一次性外来 unit（`nhms-issue1987-*`、`nhms-issue2349-*`、`nhms-issue2374-*`、`nhms-issue2382-*`、`nhms-pgdata-*-prepare`、`nhms-reslice-*-d35580c6`），不属本窗口。
- §8 提到的外来 pytest 进程已结束（07:45Z 不存在）。

## 8. 派生发现（本窗口不修）

- **#2285**：已装 `raw-retention.service` 缺 `ExecStartPre mkdir`（6 行 diff）；已装 `timeseries-retention.timer` 05:15Z vs 仓库 06:36Z（4 行 diff，仓库值自 `59a26eca6`）。
  compression 那一半已被 #1895 R5.2 的独立绑定取代（master 的 compression unit 现在**不适用**于现网）。
- **raw-retention 明日 tick 预告**：master 的 `scripts/node27_raw_retention.py` 带 #2252 copyback 互斥；unit 以 uid 1005 跑、锁文件归 uid 1103，
  有老化 canonical cycle 时 tick 按设计 rc=1 `Result=failed`（`lock_unsafe`，无 `OnFailure=` 不寻呼）。2026-09-17 03:35Z 若变红是设计态，不是本窗口回归。
- `display.env` 缺 master `display.example` 新增的 `NHMS_DISPLAY_DB_POOL_SIZE` / `NHMS_DISPLAY_DB_MAX_OVERFLOW`（代码默认 4 / 2，`apps/api/routes/hydro_display.py:215`）与
  `NHMS_DISPLAY_WORKERS` / `NHMS_MVT_FILE_CACHE_DIR`（unit 内已给默认）。`NHMS_PRECIP_MIRROR_ROOT` 仍未配（#2017 7.3）。
- `db/roles/node27_write_roles.sql` OLD→NEW 变更（去 `nhms_cold` grant）未施加，归 #1895 R5.3。
- 2.2 检出时另一 session 正以 `uv run --no-sync pytest`（pid 2839592/2839596，cwd `/home/nwm/tmp/wt-b7a-r5`，07:07:48Z 起）借活动树 `.venv` 跑 scheduler 测试；
  本窗口未动该进程。若其导入路径经由活动树，07:13:17Z 后的结果需由该 session 自行判读。
- 09-14 15:19 CST 的 dist 构建来源在 reflog 无记录（09-14 无 checkout 事件），产物与 master 构建逐字节相同；来源只影响历史归因，不影响现网。

## 9. issue #2162 验收对照

| # | 项 | 证据 |
|---|---|---|
| 1 | 运维决策留痕 | issue 评论 GO record + §1 |
| 2 | `/home/nwm/NWM` 在 `master` 且含 `e0cfe40b`；porcelain 处置；无 stash | §2（porcelain 0，`merge-base --is-ancestor e0cfe40b` 通过） |
| 3 | `000057` 在 ledger | `window/ledger.txt`（2026-09-12 11:31:56Z） |
| 4 | 回收 env 存在、600、非 symlink、cache dir == 进程值 | 目标 1 部署；本日复核 `mode=600 symlink=no`，进程 `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt` |
| 5 | plan-only 首跑 | 目标 1 receipt §7 |
| 6 | timer NEXT 次日 04:05Z | `window/timers-after.txt`：`Thu 2026-09-17 12:05:00 CST` |
| 7 | 重启后 prewarm 锁计数后 ≤ 前 | 8520 → 8520 |
| 8 | 首个生产 tick 健康判据 rc=0 | 本日 `mvt-cache-retention-20260916T040532Z.json`：`production_execute`、`failed=[]`，jq 判据 rc=0 |
| 9 | #2032 receipt「5.3 部署」节 | 目标 1（PR #2286） |
| 10 | tasks.md 5.3 勾选 | 目标 1（PR #2286） |
