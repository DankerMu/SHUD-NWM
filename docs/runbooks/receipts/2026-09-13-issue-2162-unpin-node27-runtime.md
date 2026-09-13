# node-27 live receipt — issue #2162（目标 1）：8 个 user unit 解钉到活动树 `a8db554d` + MVT 缓存回收 lane 部署

- 日期：2026-09-12 UTC（node-27 本地 2026-09-13 CST 00:47–00:53）
- 节点：node-27（`210.77.77.27`），活动树 `/home/nwm/NWM` = `a8db554d`（分支 `hotfix/node27-rollback-pre-2073`，无 upstream），
  `git status --porcelain` 窗口内为空；`git diff a8db554d HEAD -- pyproject.toml uv.lock .python-version` 0 行；`.venv` = Python 3.11.15
- 冻结副本：`/home/nwm/NWM-reslice-original-5a86841c`（`5a86841c`，未删除、未改动；porcelain 仅 `?? .venv`）
- 执行者：Claude Fable 5.1（`subagent-workflow`），OpenSpec change `unpin-node27-runtime-to-active-tree`
- 窗口脚本与证据：脚本原文 `stage-a.sh`（md5 `506beab224d590dd81d5c32db882136b`）、两次运行的日志、8 unit 捕获、prewarm / 回收 summary
  随本 receipt 提交在同名目录 `2026-09-13-issue-2162-unpin-node27-runtime/`（`attempt-1-rollback/`、`attempt-2/`）；
  node-27 上的原件已从 `/home/nwm/tmp/2162/` 挪到备份目录 `/home/nwm/nhms-unpin-backup-2162-20260912T165022Z/evidence/`
  （`out-20260912T164705Z/` 第一次回滚、`out-20260912T165022Z/` 第二次成功、`stage-a.log` / `stage-a-2.log`）

## 1. 决策留痕（issue 验收第 1 项）

**本窗口不恢复 master。** 理由：#2280——`db/migrations/000059`（#1987 narrow store）未施加到活库，master 头在活库上读路由 500；
恢复 master 必须与 #1987 task 5.2 的迁移窗口同窗完成，不能提前起新版代码。
**复查时间点** = #1987 task 5.2 的施加窗口；届时 GO 记录按 `docs/runbooks/tier-node27-timeseries-storage.md` §4.10.1 写明各项 owner，
并**逐项**核对 8 个 unit（本 receipt §3 的 `systemctl --user show` 捕获即为基线），不得自动解除任何容量 HOLD。
本 receipt 只交付目标 1：解钉到活动树 `a8db554d`（#2145 已证明它适配当前库）+ #2032 5.3 部署。issue #2162 保持 open。

## 2. 结论

- 8 个 `60-reslice-pin-original-5a86841c.conf` 全部移除（`~/.config/systemd/user` 下 drop-in 余 **0**），8 个 unit 的有效配置
  与「改前捕获做路径替换、去掉 drop-in 注入的 token」的机械推导**逐字相同**（`units-diff.txt` 0 字节，**0 条允许残差**）。
- 生产 display（`:8080`）在活动树重启：`MainPID=2186965`，`/proc` 实测 `cwd=/home/nwm/NWM`、
  `cmdline=/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2`，
  `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`，进程环境含 `NHMS_DISPLAY_CACHE_WARM_TOKEN`（值不记录）。
  重启 16:50:24Z → `/health` 200 于 16:50:27Z。`yd-NWM` `:8081` 实例 pid `3163766` 前后不变、`/health` 200。
- 读面门禁 0 个 500；公网 #2190 两个极端 instant 由改前 **500** 变为 **422 `VALIDATION_ERROR`**。
- 手工 prewarm：`.locks/**` 前 8520 → 后 8520（**不增**）；`.pbf` 8519 → 8690（+171，key 轮换，预期）；瓦片失败 **0**，10 个失败全部是
  降水 PNG `404 PRECIP_CYCLE_NOT_MIRRORED reason=mirror_root_unconfigured`（`NHMS_PRECIP_MIRROR_ROOT` 未配，#2017）。
- #2032 5.3：回收 env（0600）+ service + timer 已装；plan-only 首跑 `planned=0`；operator 触发的首个生产 tick
  `execution_mode=production_execute`、`failed=[]`、`precip_root_untouched=/home/nwm/.cache/nhms/mvt/precip`，健康判据 rc=0；
  timer `NEXT=Sun 2026-09-13 12:05:00 CST`（04:05 UTC）。
- writer 停机 16:50:23Z → 16:52:08Z（**1 分 45 秒**）。首个自然 autopipe tick 从 `/home/nwm/NWM/scripts/node27_autopipe_cron.sh`
  跑完 `Result=success`（00:52:08–00:52:18 CST，10 s），`seed_failed stage=import` 0 条、`CACHE_WARM_TOKEN unset` 0 条。
- 容量 HOLD 目录 `ls -la` 改前后逐字相同；三卷 `df` 改前后逐字相同。

## 3. 8 个 unit 逐项核对（改前 → 改后）

来源：`units-before.txt` / `units-after.txt` / `units-expected.txt` / `units-final.txt`（`systemctl --user show` 的
`FragmentPath DropInPaths WorkingDirectory ExecStartPre ExecStart ExecStartPost ExecStop Environment EnvironmentFiles`，敏感值遮蔽）。
`units-final.txt`（窗口末）与 `units-after.txt` `cmp` 相同。

| unit | 改前 `DropInPaths` | 改前 `ExecStart` / `WorkingDirectory` 指向 | 改后 | 首跑观测 |
|---|---|---|---|---|
| `nhms-display-api` | pin | `…-reslice-original-5a86841c/.venv/bin/python -m uvicorn` / 冻结副本 | `/home/nwm/NWM/.venv/bin/python -m uvicorn …` / `/home/nwm/NWM` | §2（MainPID 2186965） |
| `nhms-node27-autopipe` | pin（`ExecStart=` 清空重指 + `PYTHONPATH`/`NODE27_AUTOPIPE_REPO`/`NODE27_AUTOPIPE_ENV_FILE`） | 冻结副本 `scripts/node27_autopipe_cron.sh` | `/home/nwm/NWM/scripts/node27_autopipe_cron.sh`，`Environment=` 仅余 `NODE27_AUTOPIPE_BOOTSTRAP_LOG` | 00:52:08 CST `success` |
| `nhms-node27-download` | pin | 冻结副本 | `/home/nwm/NWM/scripts/node27_download_once.sh` | 00:52:08–00:52:29 CST `success`（timer 起动即触发；窗口脚本用 `> T_START` 判为「未观测」，窗口后 `systemctl show` 补证） |
| `nhms-node27-raw-retention` | pin | 冻结副本 | `/home/nwm/NWM/...`（已装 unit ≠ 仓库版本，见 §8） | 1.17：03:35Z |
| `nhms-node27-timeseries-retention` | pin | 冻结副本 | `/home/nwm/NWM/...`；env `NODE27_TIMESERIES_RETENTION_REPO=/home/nwm/NWM` | 1.17：05:15Z（已装 timer；仓库文件 06:36Z） |
| `nhms-node27-timeseries-compression` | pin | 冻结副本 | `/home/nwm/NWM/...`（已装 unit ≠ 仓库版本，见 §8）；env `NODE27_TIMESERIES_COMPRESSION_REPO_ROOT=/home/nwm/NWM` | 1.17：04:25Z |
| `nhms-node27-resource-governance` | pin | 冻结副本 | `/home/nwm/NWM/scripts/node27_resource_governance_once.sh`；env `NODE27_RESOURCE_GOVERNANCE_REPO_ROOT=/home/nwm/NWM` | operator start 00:52:28 CST：`RESOURCE_GOVERNANCE_CRITICAL:DATABASE_SIZE_ABOVE_CRITICAL` → exit 1（解钉前同样 failed；#2273 相关；`OnFailure=` 触发一次属预期） |
| `nhms-node27-frontier-alert` | pin | 冻结副本 | `/home/nwm/NWM/scripts/node27_frontier_stall_alert_once.sh` | 01:00:32 CST `success`（窗口后 17:00:44Z `systemctl show` + journal 补证；窗口内最近一次 00:30:32 CST 是解钉前） |

`node27-cold-residency.env` 的 `NODE27_COLD_RESIDENCY_REPO_ROOT=/home/nwm/NWM` 一并断言。

## 4. 备份与回滚

- 备份目录（0700）：`/home/nwm/nhms-unpin-backup-2162-20260912T165022Z/`（`cp --parents` 8 个 pin + `display.env` + `node27-ingest.env`，
  `cmp` 逐一相同；pin 的 md5 见 `pins-md5.txt`）。第一次尝试的备份 `/home/nwm/nhms-unpin-backup-2162-20260912T164705Z/` 内容相同。
- 回滚命令（如需回到冻结副本）：

```bash
BK=/home/nwm/nhms-unpin-backup-2162-20260912T165022Z
cd "$BK/home/nwm" && find .config -name '60-reslice-pin-original-5a86841c.conf' | while read -r f; do install -D -m 0644 "$f" "/home/nwm/$f"; done
cp "$BK/home/nwm/NWM/infra/env/display.env" "$BK/home/nwm/NWM/infra/env/node27-ingest.env" /home/nwm/NWM/infra/env/
systemctl --user disable --now nhms-node27-mvt-cache-retention.timer
systemctl --user daemon-reload && systemctl --user restart nhms-display-api.service && curl -s http://127.0.0.1:8080/health
```

  该回滚保留已装的回收 service/timer/env 文件（timer 已 disable，不再触发），不删除。

- **第一次尝试（16:47:05Z）在 1.6 回滚**：`openssl rand -hex 32` 失败——`PATH` 里的 `/usr/local/bin/openssl` 链接到本机没有的
  `OPENSSL_3.2.0`/`3.0.9` 符号。回滚动作：8 个 pin 从备份恢复、`daemon-reload`、display 未曾重启故不重启、writer timer 由 EXIT trap 重启；
  回滚后 8 unit 捕获 `units-rollback.txt` 与 `units-before.txt` `cmp` 相同。第二次改用 `/usr/bin/openssl`（3.0.2）。

## 5. token（#2079）

- 改前两文件均无 `NHMS_DISPLAY_CACHE_WARM_TOKEN=`；`/usr/bin/openssl rand -hex 32` 生成，追加到 `infra/env/display.env` 与
  `infra/env/node27-ingest.env`，两文件 mode `600`，两行 sha256 相等。值不出现在任何日志/receipt。
- 公网 `/api/v1/runs` TTFB（秒，×3）：warm `0.0021 0.0033 0.0021`；255 个 junk 请求后 `0.0028 0.0055 0.0021`（#2078）；
  无 header `0.0024 0.0022 0.0022`、外部 refresh header `0.0022 0.0022 0.0022`、正确 token `0.078 0.071 0.079`（#2079：只有持 token 的刷新走慢路径）。

## 6. 读面与瓦片

| 探针（`:8080`，改后） | 结果 |
|---|---|
| `/api/v1/layers` | 200，6073 B（改前 5790 B；两版 body 未逐字比对） |
| `/api/v1/runs` | 200，58212 B |
| `/api/v1/layers/discharge/cycles?source=gfs` | 200，`cycle=2026-09-11T12:00:00Z`，`default_cycle` 同 |
| `river-network-national/5/25/12` | 200，142594 B；ETag `W/"m16-bd3332e7…"` **改前后相同**；`X-Tile-Cache-Key` `3399c760…` → `5a18028f…`（key 轮换），第一次 miss、第二次 hit |
| `hydro-national/q_down/2026-09-11T12:00:00Z/4/12/6` | 200，1374104 B，miss，7.1 s |
| `hydro-national/gfs/2026-09-11T12:00:00Z/q_down/2026-09-11T12:00:00Z/4/12/6` | 200，1374068 B，miss，47.2 s（冷生成） |

公网 `https://test.nwm.ac.cn`（#2190）：`9999-12-31T23:59:59-08:00` / `0001-01-01T00:00:00+08:00` 在 `hydro-national` 与 `hydro/{run}`
改前 **500**（4 条），改后 **422 `VALIDATION_ERROR`**（4 条）；`not-an-instant` 前后均 422。
`/api/v1/runs?offset=999999999` 改前 200（77 B，非窗口脚本标注的「预期 500」——该标注沿用了 #2078 receipt 的旧基线，实际改前已是 200）、改后 200 `data_len=0`。

## 7. prewarm 与缓存回收（#2032 5.3）

手工 prewarm（writer 停机中，`NHMS_DISPLAY_CACHE_WARM_TOKEN` 只从 `node27-ingest.env` 读入进程环境；`prewarm.json`）：

| 项 | 值 |
|---|---|
| `requests_total / failed_count / cache_hits / deadline_skipped` | 183 / 10 / 2 / 0 |
| 瓦片失败（`failed_count − Σ png_failed`） | **0** |
| `per_source` | gfs `cycle=2026-09-11T12:00:00Z discharge_requests=65 png_ok=0 png_failed=5`；ifs 同 |
| 失败分类（`failed_count ≤ 20`，精确） | 10 × `status=404 PRECIP_CYCLE_NOT_MIRRORED reason=mirror_root_unconfigured kind=png` |
| rc / 耗时 | 1 / 43 s（rc=1 完全来自降水 PNG；#2017） |
| `.locks/**` 前 → 后 | 8520 → 8520 |
| `.pbf` 前 → 后 | 8519 → 8690 |

首个自然 tick 内的 prewarm（`tick-prewarm.json`）：`requests_total=183 failed_count=10 cache_hits=173 tile_failed=0 png_failed=10`；tick 后 `.locks/**` 仍 8520。
（tick 日志里的 `MVT prewarm rc=0 (non-fatal)` 是 `node27_autopipe_cron.sh:245` 的日志缺陷：该行只在 prewarm 非零退出时打印，
但 `$(ts)` 先于 `$?` 展开，把 rc 冲成 0；见 §8。）

回收 lane：

- `infra/env/node27-mvt-cache-retention.env`：regular file、`600`、`NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`，
  `NODE27_MVT_CACHE_RETENTION_ENABLED` / `PLAN_ONLY` 保持注释（由 unit 的 `Environment=` 决定）。
- plan-only 首跑（`NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true bash scripts/node27_mvt_cache_retention_once.sh`）：
  `status=completed execution_mode=plan_only counts={planned:0,deleted:0,skipped:0,failed:0} retention_days=14 cutoff=2026-08-29T16:52:07Z
  precip_root_untouched=/home/nwm/.cache/nhms/mvt/precip`，`planned[]` 无 `precip/` 路径（`plan-only.json`）。
- `systemctl --user enable --now nhms-node27-mvt-cache-retention.timer` → `list-timers`：`NEXT Sun 2026-09-13 12:05:00 CST`，`LAST n/a`。
- operator 首个生产 tick（`systemctl --user start nhms-node27-mvt-cache-retention.service`，rc=0，`Result=success`）：
  `execution_mode=production_execute counts={0,0,0,0} failed=[] finished_at=2026-09-12T16:52:08Z`，健康判据
  `production_execute ∧ finished_at 近 26 h ∧ failed 为空` rc=0；`skipped` 直方图为空。timer `LastTriggerUSec` 为空 → 该 summary 来自 operator start。
- **同秒覆盖**：plan-only 与生产 tick 都落在 16:52:07Z，`node27_mvt_cache_retention_once.sh:78` 的 summary 文件名只有秒级分辨率，
  生产 summary **覆盖**了 plan-only 的 `mvt-cache-retention-20260912T165207Z.json`；plan-only 内容在覆盖前已复制到 `plan-only.json`，
  `mvt-cache-retention.log` 里两次运行的 JSON 各一行完整保留。窗口脚本据此记 `RECORD: production tick wrote no new summary` 并以 rc=6 结束——
  这是脚本判据的误报（判「新文件」而非「新内容」），生产 tick 本身成功。

## 8. 派生发现（本单不修）

- #2283 `scripts/node27_autopipe_cron.sh:245`：`|| echo "[$(ts)] autopipe: MVT prewarm rc=$? (non-fatal)"` 里 `$(ts)` 先于 `$?` 展开，日志永远打 `rc=0`。
- #2284 `scripts/node27_mvt_cache_retention_once.sh:78`：summary 路径秒级分辨率，同秒两次运行互相覆盖。
- `/usr/local/bin/openssl` 坏二进制盖住 `/usr/bin/openssl`（任何依赖 `PATH` 里 `openssl` 的 runbook 步骤在 node-27 都会失败）。
- #2285 已装 unit ≠ 仓库 `a8db554d` 版本（与本单无因果，下一次 `git pull` 不会自动修正）：
  - `nhms-node27-raw-retention.service`：仓库版多 `ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-raw-retention-logs`，Description 不同。
  - `nhms-node27-timeseries-compression.service`：仓库版多 `ExecStartPre=… node27_timeseries_budget_preflight.py --check` 与
    `ExecStart=… node27_cold_residency_once.sh --enforce`，`TimeoutStartSec` 7842 vs 已装 3940。
- `nhms-node27-resource-governance.service` 持续 `DATABASE_SIZE_ABOVE_CRITICAL`（#2273 相关）。
- `scripts/ops/start-display-api.sh` 的 `UVICORN_PATTERN` 会误杀同机 `yd` 实例（#2282，本窗口手工复现其 systemd 分支）。

## 9. #2145 偏离结清

- 偏离 5（`.pbf` 数因 key 轮换上升，不作回归）：**结清**——`river-network-national` 字节与 ETag 改前后相同、cache-key 轮换、`.pbf` 8519 → 8690。
- 偏离 8（解钉后首个 tick 证据）：**结清**——§2 首个自然 autopipe tick 四项；download（00:52:08 CST）与 frontier-alert（01:00:32 CST）首跑均已观测 `success`；三条 daily lane 按 1.17 次日补记。

## 10. 待补（task 1.17，次日 07:00Z 后）

`nhms-node27-raw-retention` / `nhms-node27-timeseries-compression` / `nhms-node27-timeseries-retention`
解钉后首跑：`ExecMainStartTimestamp > 2026-09-12T16:52:08Z`、`ExecStart` 路径、`Result`、日志尾部 → 评论到 #2162。
