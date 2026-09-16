# Runbook：canonical 降水镜像缺失 → 回填 → 验证

面向下一个运维人：把「全国展示的降水图层没了」从现象一路走到修复，不需要先读 epic #2003 的十几个子 issue。
适用于 display-v2 的降水叠加（`/api/v1/precip/**`）在 node-27 上不出图的所有情形。

- 权威状态：runbook（口径冲突时按 [`docs/governance/DOC_STATUS.md`](governance/DOC_STATUS.md) 裁决）。
- 相关 receipt：`receipts/2026-09-06-precip-copyback-backfill.md`（#2016 首次回填）、
  `receipts/2026-09-16-display-v2.md`（#2017 综合部署，镜像根首次配置）。
- 涉及节点：node-22（`/scratch/frd_muziyao/NWM`，产出侧）与 node-27（`/home/nwm/NWM`，展示侧）。
  两侧看到的是同一份 NFS：22 的 `/ghdc/data/nwm/` == 27 的 `/home/ghdc/nwm/`。

## 0. 这条链路长什么样

```
node-22 convert 终态 ──(canonical_precip_mirror hook)──► NFS object-store/canonical/<S>/<K>/prcp_rate_or_amount/
                                                                     │
node-27 display API ── NHMS_PRECIP_MIRROR_ROOT ──────────────────────┘
                     └─► /api/v1/precip/{source}/{cycle}/index  与  /{valid_time}.png
```

- `<S>` 是 **normalize 后**的存储 source：`gfs` 小写、`IFS` 大写。`<K>` 是 `%Y%m%d%H` 周期 token。
- 切片文件 `canonical/<S>/<K>/prcp_rate_or_amount/<S>_<K>_prcp_rate_or_amount_f<lead:03d>.nc`，
  网格 `canonical/<S>/grid/<grid_id>/grid.json`（`gfs_0p25` / `ifs_0p25`）。
- display API 读 `NHMS_PRECIP_MIRROR_ROOT`，**绝不**读 `NHMS_OBJECT_STORE_COPYBACK_ROOT`——后者在
  `apps/api/runtime_mode.py` 的 display 禁用清单里，存在即拒绝启动。两个变量在两个节点上指向同一棵树。
- 一个周期只要 `prcp_rate_or_amount/` 目录存在就算「已镜像」；目录在而 lead 文件缺，按 `PrecipWindowIncomplete`
  fail-closed，**不会**回退到更老的周期。

## 1. 判断：到底缺的是什么（node-27，只读）

按顺序问三个问题，答案决定下一步去哪。

```bash
# Q1 镜像根配了吗？
grep -c '^NHMS_PRECIP_MIRROR_ROOT=' /home/nwm/NWM/infra/env/display.env

# Q2 服务怎么答的？（用 API 自己列出的最新周期，别手编周期）
CYCLE=$(curl -s "http://127.0.0.1:8080/api/v1/layers/discharge/cycles?source=gfs" \
  | tr '{' '\n' | sed -n 's/.*"cycle_time":"\([^"]*\)".*/\1/p' | head -1)
curl -s "http://127.0.0.1:8080/api/v1/precip/gfs/$CYCLE/index" | head -c 300

# Q3 盘上有吗？
ls /home/ghdc/nwm/object-store/canonical/gfs | grep -E '^[0-9]{10}$' | tail -3
ls /home/ghdc/nwm/object-store/canonical/IFS | grep -E '^[0-9]{10}$' | tail -3
```

| 现象 | 结论 | 去 |
|---|---|---|
| Q1 = 0，且 Q2 是 404 `PRECIP_CYCLE_NOT_MIRRORED` + `details.reason = "mirror_root_unconfigured"` | 服务没配镜像根，盘上有没有数据都不影响判断 | §2 |
| Q2 是 404 `PRECIP_CYCLE_NOT_MIRRORED` 且 reason 不是 `mirror_root_unconfigured`，Q3 里该周期不在 | 该周期确实没镜像 | §3 |
| Q2 是 200 但某个时次 PNG 404 `PRECIP_WINDOW_INCOMPLETE` | 周期在、lead 文件缺片 | §3（对该周期重跑回填） |
| Q2 是 200 且 PNG 200，但前端不出图 | 不是镜像问题，查前端图层开关与 `precip=0`，见 `apps/frontend/e2e/m11-visual-evidence.md` | — |
| Q3 两源最新周期都停在几天前，且 `cycles` 端点同样停在那 | 上游没产出新周期，不是镜像断链 | §5 |

## 2. 配置镜像根（node-27，一次性）

```bash
ENVF=/home/nwm/NWM/infra/env/display.env
BK=/home/nwm/nhms-display-env-backup-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BK" && ( cd / && cp --parents "${ENVF#/}" "$BK/" )
printf '\nNHMS_PRECIP_MIRROR_ROOT=/home/ghdc/nwm/object-store\n' >> "$ENVF"
grep -c '^NHMS_PRECIP_MIRROR_ROOT=' "$ENVF"    # 必须是 1
systemctl --user restart nhms-display-api.service
curl -s http://127.0.0.1:8080/health
```

取值口径：**object-store 根**，不是 `canonical/` 子目录（`services/precip/mirror.py` 自己拼 `canonical/<S>/<K>/`）。
与 `infra/env/node27-raw-retention.env` 的 `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT` 必须同值。
回滚：把备份文件拷回去再重启，降水两条路由回到 404，其它读面不受影响。

**不要**用 `scripts/ops/start-display-api.sh` 重启——它会连带杀掉同机的 `yd` 实例（#2282）。

## 3. 回填（node-22，需要一个静默窗口）

回填脚本 `scripts/canonical_precip_copyback_backfill.py` 只用 `shutil`/`pathlib`，不引依赖。
node-22 的共享 `.venv` 在维护窗口前是 3.12.7，**禁止** `uv sync` 与任何会重建环境的 `uv run` 变体：
用钉住的解释器 `/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.canonical_precip_copyback_backfill`。

调度器 timer 的周期（5 min）短于单 pass（约 7.5 min），passes 背靠背，**自然空窗不存在**，必须停 timer：

```bash
cd /scratch/frd_muziyao/NWM
systemctl --user stop nhms-compute-scheduler.timer
systemctl --user is-active nhms-compute-scheduler.service      # 等到不是 active/activating

git status --porcelain && git pull --ff-only                   # 只在窗口内前滚，pass 运行中前滚会得到混版本进程

# 路径取自单元实际加载的 env 文件，不是交互 shell 里的同名变量（未导出会让脚本 fail-closed 退 2）
ENVF=infra/env/compute.scheduler-dbfree.env
SRC=$(sed -n 's/^OBJECT_STORE_ROOT=//p' "$ENVF" | tail -1)
DST=$(sed -n 's/^NHMS_OBJECT_STORE_COPYBACK_ROOT=//p' "$ENVF" | tail -1)
echo "source=$SRC copyback=$DST"                               # 回显解析结果，别靠记忆

PY=/scratch/frd_muziyao/NWM/.venv/bin/python
$PY -m scripts.canonical_precip_copyback_backfill --source-root "$SRC" --copyback-root "$DST" --dry-run
$PY -m scripts.canonical_precip_copyback_backfill --source-root "$SRC" --copyback-root "$DST"
$PY -m scripts.canonical_precip_copyback_backfill --source-root "$SRC" --copyback-root "$DST"   # 第二遍

systemctl --user start nhms-compute-scheduler.timer
```

判据：

- 第一遍汇总 `totals.copied > 0`、`totals.failed == 0`；
- **第二遍** `totals.copied == 0 && totals.failed == 0`，且逐条 `status ∈ {ok, no_precip_products}`
  （缺降水产物的周期记 `no_precip_products` 且三计数全 0，「全 skipped」按构造不成立）；
- 窗口时长要远小于 `NHMS_SCHEDULER_LOOKBACK_HOURS`（96），否则可能真丢周期；receipt 记 stop/start 时刻与窗口时长；
- 重启后首个 pass 对窗口内结束作业做 reconcile 是**预期**事件，不是故障。

容量：每源每周期 `prcp_rate_or_amount/` 约 66 MB（gfs 56 个 `.nc`、IFS 53 个）。回填前后各记一次
`df -h /ghdc/data`（22）与 `df -h / /home`（27）。

## 4. 验证（node-27）

```bash
S=gfs; CYCLE=$(curl -s "http://127.0.0.1:8080/api/v1/layers/discharge/cycles?source=$S" \
  | tr '{' '\n' | sed -n 's/.*"cycle_time":"\([^"]*\)".*/\1/p' | head -1)
curl -s "http://127.0.0.1:8080/api/v1/precip/$S/$CYCLE/index" > /tmp/idx.json; head -c 200 /tmp/idx.json
VT=$(sed -n 's/.*"valid_times":\["\([^"]*\)".*/\1/p' /tmp/idx.json)
curl -s -o /dev/null -D - "http://127.0.0.1:8080/api/v1/precip/$S/$CYCLE/$VT.png" | head -8
```

- index 200，`valid_times` 非空（窗口完整的时次才会被列出，IFS 在 cycle+144h 之后天然止步，不是缺片）。
- PNG 首次 200 且 `X-Tile-Cache: miss`，再请求一次应为 `hit`；带 `If-None-Match` 应得 304。
- 跨账号可读性（22 写、27 读）：对一个 `.nc`、一个 `grid.json` 及其父目录 `stat -c '%a %U'`，期望 644 / 755，并 `head -c 8` 实读。
- keep 水位不等式逐源成立：`oldest_listed_cycle − 24h ≥ display_watermark − retention_days`，
  其中 `display_watermark = MAX(cycle_time)`（`hydro.hydro_run`，`run_type='forecast'`，
  `status IN ('succeeded','parsed','published')`），`retention_days` 取 `infra/env/node27-raw-retention.env`。
- 浏览器侧一键复核：`node scripts/node27_display_v2_browser_evidence.mjs --base-url https://test.nwm.ac.cn
  --out-dir <dir> --playwright-root /home/nwm/NWM/apps/frontend`（零 mock，跑完给 `pass: true/false`）。

## 5. 上游没有新周期时

`cycles` 端点与盘上镜像**同时**停在同一个周期，说明不是镜像断链，而是上游没产出。
这时不要回填（没有源可填），去查 node-22 的 `nhms-compute-scheduler` pass 日志与 `.err`：
`cycle_lag_hours`（默认 16）决定一个新周期最早什么时候进入候选窗口，未到窗口不产出是正常的。

## 6. 已知边界

- 降水 PNG 缓存 `NHMS_MVT_FILE_CACHE_DIR/precip/<S>/<K>/` 的剪枝由 `scripts/node27_raw_retention.py` 承担，
  需要 `infra/env/node27-raw-retention.env` 里配 `NHMS_MVT_FILE_CACHE_DIR`（模板
  `infra/env/node27-raw-retention.example:63` 有该行）。未配时该 lane 记
  `skipped: precip_cache_root_unconfigured`，缓存只增不减——2026-09-16 实测即为此状态（#2011 面）。
- `scripts/node27_autopipe_cron.sh` 不 source `display.env`，cron 起的 prewarm 因此看不到
  `NHMS_DISPLAY_CACHE_WARM_TOKEN`，目录发现可能读到最多 45 s 陈旧的 catalog。
- 镜像 hook 是 fail-open 的：源缺失只记 `precip_mirror: failed` 回执并继续，不会让 pipeline 失败。
