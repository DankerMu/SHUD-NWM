# Proposal: unpin-node27-runtime-to-active-tree (#2162，目标 1)

## Why

#2162 在执行前被拆成两个目标（issue 评论 2026-09-12 + 用户裁定）：

1. **安全解钉到活动树 `a8db554d`**——交接已证明它适配当前库（#2145 receipt §10：四个面在真库上全 200），
   不依赖 #2273 / `000059`，可与 #2273 并行；并把 #2032 tasks 5.3 欠的缓存回收车道部署掉：
   env + unit + timer、plan-only 首跑、prewarm 前后锁计数、首个生产 tick 健康判据。**只解 pin 不算关单。**
2. **恢复到 origin/master**——必须与 #1987 的 `000059` 同一窗口完成，不得提前启动新版代码（#2280 实测 master 在活库上
   `h.timeseries_store` 缺列 500）。八个 unit 逐项核对，不得只处理 API / autopipe，不得自动解除任何容量 HOLD。

本 change 只做目标 1。目标 2 留在 #2162 里等 #1987 task 5.2 的窗口。

node-27 现场（2026-09-12 15:55Z–16:40Z 只读实测，全部为一手读数）：

- `/home/nwm/NWM` = `a8db554d`，`hotfix/node27-rollback-pre-2073`，无 upstream，porcelain 0，落后 `origin/master` 180 提交。
  `a8db554d` **含** #2151（`e0cfe40b`，#2032 本体）、#2168（#2079 token）、#2176（#2078 LRU）、#2190（#2033 越界 422）、
  #2073、#2149——即 #2162 正文与三条评论要部署的内容全部在活动树上，无需 pull。
- 8 个 `60-reslice-pin-original-5a86841c.conf`（2026-09-11 07:11，md5 各不相同）把 8 个 unit 的
  `WorkingDirectory` / `ExecStart` / `PYTHONPATH` / `NODE27_*_REPO*` / `NODE27_*_ENV_FILE` 全指向冻结副本
  `/home/nwm/NWM-reslice-original-5a86841c`（`5a86841c`，porcelain 仅 `?? .venv`）。
  它们是 `/data/GHDC/nwm-archive/reslice-20260909T161239Z-d35580c6/` 那次 reslice 的 `runtime-rebind`
  （`runtime-rebind-evidence.json` 逐一列出这 8 个文件），而该 reslice 已于 2026-09-10T23:22Z **在任何数据变更之前放弃**
  （`reslice-abandoned-before-mutation.json`：`reason: "user chose whole database physical migration PR"`，
  `source_removed: false`，`backups_retained: true`）。pin 是一次已放弃操作的遗留，没有任何在途流程依赖它。
- 除这 8 个 `60-*` 外**没有其他 drop-in**：`find ~/.config/systemd/user -path '*.d/*.conf'` 恰好 8 条。
  #2240 的 CAPACITY HOLD 已由其 owner 解除：`/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold/resume-approved`
  存在（2026-09-12 13:49 CST），`state.json` 授权文本为「remove leftover capacity-hold systemd fences after resume-approved」，
  四个 `91-nhms-pgdata-pr-2240-capacity-hold.conf` 已不存在。**本单不读写该目录下任何文件。**
- 落地面已干净：8 个 base unit 全部指向 `/home/nwm/NWM`，引用的 9 个脚本在活动树上都在；
  9 个 `infra/env/*.env` 冻结副本 vs 活动树：5 个逐字节相同，4 个只差 `NODE27_*_REPO(_ROOT)` 一键
  （活动树侧已是 `/home/nwm/NWM`，与 `runtime-rebind-evidence.json` 的 `changed_keys` 完全一致）；
  活动树 `.venv` 为 Python 3.11.15，`import apps.api.main, services.tiles.mvt` 退 0；
  `infra/systemd/nhms-display-api.service` 已装版本与 `a8db554d` 逐字节相同。
- `#2032` 的 5 个部署文件（wrapper、runner、env 模板、service、timer）在活动树上都在；自 `a8db554d` 起 master 没再改过它们。
- 生产缓存 `/home/nwm/.cache/nhms/mvt`：`.lock` **8492** / `.pbf` 8488 / 1.2 GB（15:55Z），仍在单调增长；
  `df`：`/` 76%、`/home` 30%（1.1T 可用）、`/data/GHDC` 14%。
- `scripts/ops/start-display-api.sh` 的 `UVICORN_PATTERN='\.venv/bin/python -m uvicorn apps\.api\.main:app'`
  在本机同时匹配 `yd-NWM` 的 `:8081` 实例（pid 3163766，独立库 `:55434`）；它在 systemd 分支里先 `stop` 本 unit、
  再对 pgrep 剩余匹配 `kill -TERM/-KILL`——照字面跑会把 `yd` 生产实例杀掉。**本单不跑这个脚本**，手工复现其 systemd 分支。

## What Changes

**零代码改动。** 交付物是一次 node-27 实机运维动作 + live receipt + 两处文档欠账：

1. 在 writer-timer 停机窗口内：备份 → 删除 8 个 `60-reslice-pin-original-5a86841c.conf` → `daemon-reload` →
   对 8 个 unit 的有效 `ExecStartPre` / `ExecStart` / `ExecStartPost` / `ExecStop` / `WorkingDirectory` / `Environment` /
   `EnvironmentFiles` / `DropInPaths` 做**机械 diff**（改前集合做 `s#冻结副本#/home/nwm/NWM#g` 后，唯一允许的残差是 pin 注入的
   `PYTHONPATH` / `NODE27_*_REPO*` / `NODE27_*_ENV_FILE` 消失与 `DropInPaths` 变空），任何其他差异即回滚。
2. 写入 `NHMS_DISPLAY_CACHE_WARM_TOKEN`（#2079 / PR #2168 tasks 4.3）：node-27 上 `openssl rand -hex 32`，
   同值追加到 `infra/env/display.env` 与 `infra/env/node27-ingest.env`（均 0600），值不回显、不进 receipt。
3. 安装 `infra/env/node27-mvt-cache-retention.env`（0600，非 symlink，`NHMS_MVT_FILE_CACHE_DIR` 与 display
   **进程**实际值相等）+ `nhms-node27-mvt-cache-retention.{service,timer}`。
4. 手工复现 `start-display-api.sh` 的 systemd 分支重启 `:8080`（`daemon-reload` → `systemctl --user restart` →
   `/health` → `/api/v1/models?limit=1` 的 `basin_id` 冒烟），断言进程 cwd / exe / `NHMS_MVT_FILE_CACHE_DIR` / token 键。
5. 四个读面零 500 门禁（`/api/v1/layers`、`river-network-national`、两条 `hydro-national`），失败即**整体回滚**
   （8 个 pin + 两个 env 从备份恢复，`daemon-reload`，重启 display）。
6. writer 仍停机时：锁计数 → 手工 prewarm → 锁计数（后 ≤ 前）；plan-only 首跑 + jq 判据；`enable --now` timer；
   operator 触发一次生产 tick（`systemctl --user start` 该 service）+ 健康判据退 0。
7. 起 writer timer；等首个自然 autopipe tick（有界 ≤ 30 min，依据是当前 10 min 一发、单 tick ~10 s 的实测；到界未观测则
   如实记录并交接，不声称结清 #2145 偏离 8）：跑的是 `/home/nwm/NWM`、`seed_failed stage=import` 为 0、
   无 `CACHE_WARM_TOKEN unset`、prewarm 摘要里瓦片失败 0（`failed_count − Σ png_failed`；降水 PNG 的 `mirror_root_unconfigured`
   单列，见 Non-Goals；`deadline_skipped` / 超时是冷成本已知限制，报 #2017）。
   8 个 unit 的首跑观测面逐一列在 `tasks.md`：display / autopipe / download / frontier-alert 在窗口内观测，
   resource-governance 由 operator 触发一次只读审计，三条破坏性 daily lane（raw-retention / compression / ts-retention）
   **不提前触发**，次日按 timer 首跑后以 issue 评论补记。
8. 公网复测：#2190 两个越界瞬时 × 两条 legacy 路由 → 422 `VALIDATION_ERROR`；#2078 `offset=999999999` → 200 空列表、
   255 条 junk 冲刷后 `/api/v1/runs` 仍 ≤ 10 ms；#2079 外部 `refresh` 头不再冷。
9. receipt 落 `docs/runbooks/receipts/2026-09-13-issue-2162-unpin-node27-runtime.md`；
   `2026-09-08-issue-2032-mvt-cache-retention-node27.md` 追加 §7「5.3 部署」；
   `openspec/changes/archive/2026-09-08-mvt-tile-cache-lifecycle-retention/tasks.md:38` 勾选并去掉 deferred 说明
   （issue 验收标准明写这两条）；#2145 receipt 欠本窗口的偏离 5（key 轮换 miss）与偏离 8（解钉后首个 tick）在本 receipt 里点名结清。

## Non-Goals

- **不恢复 master、不 `git pull`、不 `git checkout`**——目标 2，等 #2280 / #1987 task 5.2 的窗口；活动树保持 `a8db554d`。
- **不读写 `/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold/`**，不创建/删除任何 `91-*`，不碰 `yd-*` 实例、不碰 node-22。
- 不配 `NHMS_PRECIP_MIRROR_ROOT`：镜像根布局是 #2017 runbook 的决定；未配时 `apps/api/routes/precip.py::_mirror_root`
  返 404 `mirror_root_unconfigured`，而 `scripts/node27_mvt_prewarm.py:97-100` 刻意**不**把它列入白名单，
  所以解钉后每个 tick 的 prewarm 会因降水 PNG 返 rc=1（autopipe 记 `non-fatal`，不改 ingest 结果）。
  这是本单**明知并接受**的已知限制，由 #2017 收口；receipt 记每 tick 的 `failed` 分解证明瓦片侧为 0。
- 不改任何代码、不改 unit / env 语义（`start-display-api.sh` 误杀 `yd` 的 bug 报告不修，已立单）。
- 不手工删缓存文件；旧的 8492 个锁按 14 天口径自然老化（缓存根 09-04 建，最早 09-18）。
- 不动 `nhms-node27-raw-retention.service` / `nhms-node27-timeseries-compression.service` 两个「已装 ≠ 仓库」的 base unit
  （只记录 diff）；不处理 `nhms-node27-resource-governance.service` 的 `DATABASE_SIZE_ABOVE_CRITICAL` failed 状态（与本单无因果）。

## Risk triage

- Fixture level: **`none`**（零代码 diff；交付物是运维动作 + receipt + 两处文档欠账 + 本 fixture）。
  Upstream suggested level: **absent**——#2162 是手写 follow-up 单，无该字段（`预估规模 S`）。
  `unit` / `config` 触发词描述的是生产动作风险，由下列风险包承载（与 #2145 / #2016 同构先例）。
- Repair intensity: 不适用——零代码改动。
- Risk packs 与 evidence mapping 见 `tasks.md`。`design.md` 按 `none` 级豁免。

## Must preserve

- 冻结副本 `/home/nwm/NWM-reslice-original-5a86841c` 及其 `.venv` 一字节不动（它是回滚目标）。
- 8 个 pin 文件的字节在删除前进入 0700 备份目录（`cp --parents`），回滚 = 拷回 + `daemon-reload` + 重启 display。
- `display.env` / `node27-ingest.env` 除追加一行 token 外不变，模式保持 0600；token 值不出现在任何日志/receipt/PR。
- `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt` 在重启前后对 display **进程**恒等，1.2 GB 热缓存不丢。
- `yd-*` 的 `:8081` 进程 pid 与 `/health` 在窗口前后相同。
- 8 个 base unit 文件、活动树的 9 个 `*.env`（除上述 token 行）、CAPACITY HOLD 目录、`public.schema_migrations` 全程不动。

## Seams under test

- 8 个 unit 的 `systemctl --user show` 有效属性集合（改前/改后机械 diff）。
- `:8080` 进程的 `/proc/<MainPID>/{cwd,exe,environ}`。
- 四个读面的 HTTP 状态与 `X-Tile-Cache` / `ETag`（`river-network-national/5/25/12` 改前/改后 ETag 对照）。
- `find .../.locks -name '*.lock' | wc -l` prewarm 前/后。
- 回收 runner 的 summary JSON：`execution_mode`、`planned[]`、`skipped[]`、`failed[]`。
- 首个自然 autopipe tick 的日志段与 prewarm summary。
- 公网 `https://test.nwm.ac.cn` 的 #2190 / #2078 / #2079 判据。

## Evidence mapping

见 `tasks.md` Evidence Floor。oracle 全部在 node-27 实机；本地只闭 `openspec validate` 与 markdownlint。
