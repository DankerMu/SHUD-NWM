# Tasks: unpin-node27-runtime-to-active-tree (#2162，目标 1)

## Risk packs

核心包（`issue-risk-contract.md`），逐项裁定：

- **Schema / columns / units / field names — selected**：本单的实质动作是改 8 个 systemd unit 的有效配置。
  判据不是「脚本存在」，而是 `systemctl --user show -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStartPre -p ExecStart
  -p ExecStartPost -p ExecStop -p Environment -p EnvironmentFiles` 的改前/改后集合机械 diff（含 `ExecStartPre`：compression 的预算 preflight
  腿带仓库绝对路径，漏了它会留下「主腿解钉、preflight 腿仍用冻结树解释器」的半态）。期望集合由改前集合派生：
  `s#/home/nwm/NWM-reslice-original-5a86841c#/home/nwm/NWM#g`、只剔除 8 个 pin 文件**自身** `Environment=` 行注入的三类 token
  （`PYTHONPATH=`、`NODE27_*_REPO(_ROOT)=`、`NODE27_*_ENV_FILE=`——逐 pin `cat` 已存入备份目录，glob 锚定在 `=` 前，
  `NODE27_AUTOPIPE_BOOTSTRAP_LOG` / `NODE27_FRONTIER_ALERT_ENV_INJECTED` 不匹配）、`DropInPaths` 置空。
  **允许的残差只有一类**：pin 用 `ExecStart=` / `EnvironmentFile=` 清空过的 directive 在 base unit 文件里本来就有第二条
  （如 compression 的 cold-residency 腿），改后重现——判定规则是「改后多出的每一行必须是 `ExecStart=` / `ExecStartPre=` / `EnvironmentFiles=`，
  且其路径逐字出现在该 unit 的 `FragmentPath` 文件里」；改后**少**任何一行、或多出其他任何行即整体回滚。
  另断言 4 个曾被 rebind 改过键的 env（resource-governance / timeseries-compression / timeseries-retention / cold-residency）
  活动树侧 `NODE27_*_REPO*=/home/nwm/NWM`。
- **Auth / permissions / secrets — selected**：token 在 node-27 上 `openssl rand -hex 32` 生成，写入前断言两文件都无该键，
  写后断言两文件模式仍 0600、两行 sha256 相等；值不回显（脚本全程不 `echo` 它，日志连摘要都不打）。
  DSN 不出现在 receipt（`DATABASE_URL` 只用于 `psql` 读账本）。
- **Concurrency / shared state / ordering — selected**：窗口顺序写死
  `停 nhms-node27-{autopipe,download}.timer → 等两个 .service settled → 备份 → 删 pin → daemon-reload → unit diff 门禁
  → 写 token → 装回收 env/unit → daemon-reload → 重启 display → 读面门禁（point of no return）→ prewarm 锁计数 → plan-only
  → enable timer → operator tick → 起 writer timer → 等首个自然 tick`。理由：display 必须在 `000057` 之后重启（已于 #2145 施加），
  锁计数必须在没有 autopipe 自己的 prewarm 干扰下量；`nhms-node27-frontier-alert.timer` 每 30 分钟触发，
  会是解钉后第一个从活动树跑的 unit。脚本 `trap EXIT` 无条件尝试重新拉起两个 writer timer。
  **排空上限 30 min，到界分支 = 中止**：任何变更之前退 2，timer 由 trap 拉回，窗口记为「未开」——不在冻结树 prewarm
  仍可能在跑的情况下量锁计数（近 5 个 tick 实测各 ~10 s、10 min 一发，30 min 是 3 个 tick 的余量，不是 #2145 那次 4.9 h 在途 tick 的量级）。
- **Error handling / rollback / partial outputs — selected**：**回滚触发条件**（整体：8 个 pin + 两个 env 从备份恢复、
  卸回收 unit/env、`daemon-reload`、`systemctl --user restart nhms-display-api.service`、`/health`）：
  1.5 unit diff 残差 / 4 个 env 键缺失；1.6 token 键已存在或两行不等；1.7 回收 env 前置不满足；
  1.8 `/health` 30 s 内不通、`cwd` / `cmdline` 不是活动树、进程 env 缺 `NHMS_MVT_FILE_CACHE_DIR` 或 token 键、`basin_id` 为空；
  1.9 任一读面 500 或 cycles 端点非 200。**1.9 通过即 point of no return**：display 已在 a8db554d 上证明四个读面绿，
  1.10–1.16 的任何不达标（锁计数后 > 前、prewarm 非 0、plan-only 判据、健康判据、首个 tick 未观测、公网复测）
  一律**记录不回滚**，receipt 如实写「未达标」+ 后续 owner；回滚命令与备份目录路径写进 receipt，供 operator 事后决定。
  绝不留下「7 个解钉、display 仍钉」的半态。
- **Config / project setup — selected**：回收 env 由模板 `install -m 0600` 复制，断言非 symlink、`stat -c %a`=600、
  `NHMS_MVT_FILE_CACHE_DIR` 与重启后 display 进程 `/proc/<MainPID>/environ` 中的值相等；
  wrapper 的 `ENV_FILE_SYMLINK_FORBIDDEN` / `ENV_FILE_MODE_UNSAFE` 两条 rc=2 路径靠这两条断言前置排除。
- **Resource limits / large input — selected**：`df -h / /home /data/GHDC` 改前/改后实测；锁/瓦片计数改前/改后；
  `.pbf` 数会因 `000057` 后 digest basis 变化的 key 轮换而**上升**，记为预期而非回归（#2145 偏离 5 在此结清）。
  冷 prewarm 成本未知（a8db554d 侧无实测；冻结树上 z=3 全国查询曾跑 66 min）：`scripts/node27_mvt_prewarm.py`
  默认 `--deadline-seconds 540`、单请求 30 s，超出即 `deadline_skipped` 置 rc=1。**判定口径**：`failures[]` 里出现 5xx = 回归；
  404 `PRECIP_CYCLE_NOT_MIRRORED`/`mirror_root_unconfigured` = 预期（Non-Goals）；`error` 为超时类或 `deadline_skipped > 0`
  = 冷成本已知限制，报 #2017（task 7.2 的 oracle），不判本单失败——锁计数「后 ≤ 前」与它们无关，是独立判据
  （活动树含 #2032 的 fill-then-unlink，`services/tiles/mvt.py:395-405`）。**瓦片失败数的判定口径**因此是：
  `failed_count − Σ png_failed` 为 0，**或**其全部为超时/连接类（`failures[].status == 0` 且 `error` 为异常类名，非 HTTP 状态）；
  该分类只在 `failed_count ≤ 20` 时可从截断的 `failures[]` 精确判定，否则记「不可判」并报 #2017；任何 5xx 即回归。
- **Release / packaging / dependency compatibility — selected**：display 与 prewarm 都跑活动树自带 `.venv`
  （**不用 `uv run`**——pin 是 3.11，`uv run` 在 pin 不符时会删掉并重建在线服务占用的 `.venv`）；
  判据：`/home/nwm/NWM/.venv/bin/python -V` 为 `3.11.x`、`import apps.api.main, services.tiles.mvt` 退 0（已实测）、
  `git diff a8db554d HEAD -- pyproject.toml uv.lock .python-version` 为空。
- **File IO / path safety / overwrite — selected**：删除 8 个 drop-in 之前 `cp --parents` 到新建 0700 目录
  `/home/nwm/nhms-unpin-backup-2162-<ts>/`，`cmp` 8 个备份与原件一致后才 `rm`；空的 `.d/` 目录随之 `rmdir`
  （systemd 对空目录无感）；写 env 用 `>>` 追加一行，不重写文件。
- **Public API / CLI / script entry — not selected**：不新增/不改任何 CLI 或路由；所有探针只读。
  `start-display-api.sh` 的 `yd` 误杀 bug 报告不修（#2282，见偏离 1）。
- **Legacy compatibility — not selected**：`a8db554d` 与冻结副本的 env/unit 契约一致（5 个 env 逐字节相同、4 个只差 REPO 键，
  8 个 base unit 已指向活动树），解钉不改变任何 unit 对外契约；对外 API 面的变化（#2073 目录、#2190 422）是被回滚掉的、
  已 merge 到 master 的行为回到线上，不是新契约。
- **Documentation / migration — not selected**：无迁移；receipt 为新增文档，两处欠账（§7、tasks.md:38）是 issue 明写的验收项。

Domain packs（`openspec/project-profile.md`）：

- **Published NHMS artifacts / display identity — selected**：重启后首批全国瓦片请求必然冷 miss（key 轮换），
  `river-network-national/5/25/12` 改前/改后 ETag 对照 + `X-Tile-Cache` 记录；prewarm 后再打一遍应为 `hit`
  （若 prewarm 被 deadline 截断、该瓦片未被打到，则记 `miss` 并注明原因，不判失败）。
- **PostGIS / TimescaleDB、Hydro-met time series、Geospatial / CRS、SHUD numerical runtime、Slurm production lifecycle、
  External providers / snapshot、Run manifest / QC provenance — not selected**：本单不产出/不改任何数据、DDL、调度或 manifest。

## 解钉后 8 个 unit 的首跑观测面（P1-4）

| unit | 触发 | 首跑观测 |
|---|---|---|
| `nhms-display-api` | 1.8 手工重启 | 1.8/1.9 全部断言 |
| `nhms-node27-autopipe` | timer 10 min | 1.14（≤ 30 min 有界等待） |
| `nhms-node27-download` | timer `OnUnitActiveSec=30min` | 1.14 同段记录 `ExecStart` 路径与 `Result`；30 min 上界内**可能**未触发，届时记「未在窗口内观测」 |
| `nhms-node27-frontier-alert` | timer `*:00/30` | 1.14 记录解钉后首次 `Result`（同样可能落在上界外，同上处理） |
| `nhms-node27-resource-governance` | daily（已装 timer 实测 NEXT 04:10Z） | **1.14b 窗口内 operator `start` 一次**（只读审计，非破坏；预期仍 `failed` + `DATABASE_SIZE_ABOVE_CRITICAL`，证明它从活动树跑） |
| `nhms-node27-raw-retention` | daily（已装实测 NEXT 03:35Z） | **窗口内不触发**（删原始档，破坏性）→ 1.17 次日补记 |
| `nhms-node27-timeseries-compression` | daily（已装实测 NEXT 04:25Z，`--enforce`） | 同上 → 1.17 |
| `nhms-node27-timeseries-retention` | daily（已装实测 NEXT 05:15Z；仓库 timer 文件写 06:36Z，已装 ≠ 仓库，以 1.1 上账的 `TimersCalendar` 为准；`drop_chunks`） | 同上 → 1.17 |

三条破坏性 daily lane 不提前手工触发（那是本单范围外的生产数据动作）；它们的 base unit、env、脚本在活动树上的存在性与
REPO 键已在 1.1/1.5 断言，首跑 `Result` 由 1.17 在次日 **07:00Z 后**（晚于三条已装 cadence 与仓库 06:36Z 两者）以 issue 评论补记
（#2162 因目标 2 保持 open，有落点）；1.17 以 `ExecMainStartTimestamp > 窗口 T_START` 判定「解钉后首跑」，不是拿最近一次 `Result` 当首跑。

## Deployment tasks

- [x] 1.1 窗口内只读基线：UTC 时间、活动树 HEAD/branch/porcelain、`git diff a8db554d HEAD -- pyproject.toml uv.lock .python-version`
      为空、**重新枚举** `~/.config/systemd/user/*.d/*.conf`（断言恰 8 条且全为 `60-reslice-pin-original-5a86841c.conf`，否则退 2）、
      8 个 unit 的有效属性集合（改前集，含 `ExecStartPre/Post/Stop`）、8 个 pin 的 md5、三条 daily timer + download timer 的已装
      `TimersCalendar` / `TimersMonotonic`、display `MainPID` ∈ `:8080` uvicorn 进程集合且集合内其余 pid 的 ppid 均为 `MainPID`
      （排除 detached 孤儿；多 worker 下 worker 继承监听 fd，不能要求严格相等；不满足退 2）、其 `cwd/cmdline` 与 `NHMS_MVT_FILE_CACHE_DIR`、`yd` `:8081` pid、锁/瓦片计数与大小、`df` 三卷、
      账本 `version >= '000056'` 三行、CAPACITY HOLD 目录 `ls -la`（只读）、`:8080` 与公网 `/api/v1/layers` 状态与字节、
      `river-network-national/5/25/12` 的 ETag、#2190 两个越界瞬时在公网的改前状态（预期 500）、
      `/api/v1/layers?offset=999999999` 改前状态（预期 500）、两个「已装 ≠ 仓库」base unit 的 diff（只记录）。
- [x] 1.2 决策留痕（issue 验收第 1 项）：**不恢复 master**，理由 = #2280（`000059` 未施加，master 在活库 500）；
      复查时点 = #1987 task 5.2 的窗口；解钉目标改为 `a8db554d`。写进 receipt §1 与 issue 评论。
- [x] 1.3 停 `nhms-node27-{autopipe,download}.timer`，等两个 `.service` settled（`inactive|failed`，上限 **30 min**；
      到界 → 退 2、trap 拉回 timer、窗口记「未开」），记时间戳；记 autopipe 日志字节偏移与 `ExecMainStartTimestampMonotonic`。
- [x] 1.4 备份：8 个 pin + `display.env` + `node27-ingest.env` → `cp --parents` 到 0700 目录；`cmp` 对账。
- [x] 1.5 删 8 个 pin，`rmdir` 空 `.d/`，`daemon-reload`；机械 diff 门禁（风险包第 1 条，含允许残差规则）；4 个 env 的 REPO 键断言；
      失败 → 回滚退 3。
- [x] 1.6 token：断言两文件无键 → 生成 → 追加 → 断言 0600、两行 sha256 相等；失败 → 回滚退 4。
- [x] 1.7 `install -m 0600` 回收 env、`install -m 0644` service/timer 到 `~/.config/systemd/user/`，`daemon-reload`；
      断言 env 非 symlink、600、`NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt`、`NODE27_MVT_CACHE_RETENTION_{ENABLED,PLAN_ONLY}`
      两行仍为注释（wrapper 在进程环境之后 `set -a` source env 文件，未注释的 `PLAN_ONLY=false` 会静默压过 1.11）；失败 → 回滚退 6。
- [x] 1.8 手工复现 `start-display-api.sh` systemd 分支：`systemctl --user restart nhms-display-api.service` → `/health`（≤30 s）
      → `MainPID` 的 `cwd=/home/nwm/NWM`、`cmdline` 以 `/home/nwm/NWM/.venv/bin/python -m uvicorn apps.api.main:app` 开头
      （`exe` 解析到 uv 托管的 cpython，不是判据）、environ 含 `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt` 与
      `NHMS_DISPLAY_CACHE_WARM_TOKEN=` 键 → `/api/v1/models?limit=1` 的 `items[0].basin_id` 非空 → `yd` `:8081` pid 不变且 `/health` 200。
      任一失败 → 回滚退 5。
- [x] 1.9 读面门禁（`:8080`）：`/api/v1/layers` 200、`/api/v1/runs` 200、`/api/v1/layers/discharge/cycles?source=gfs`（空则 ifs）HTTP 200
      并取 `data.cycles[0].{cycle_time,valid_time_start}`、`river-network-national/5/25/12` 200（记 ETag / `X-Tile-Cache`）、
      `hydro-national/q_down/{vt}/4/12/6` 与 `hydro-national/{src}/{cycle}/q_down/{vt}/4/12/6` 均非 500（200 或 424；无 cycle 时后者记 N/A）。
      任一失败 → 整体回滚退 5。**通过即 point of no return。**
- [x] 1.10 writer 仍停机：锁计数（前）→ `NHMS_DISPLAY_CACHE_WARM_TOKEN` 只从 `node27-ingest.env` 读入进程环境 →
      `/home/nwm/NWM/.venv/bin/python scripts/node27_mvt_prewarm.py --zooms 3,4,5 --workers 8`（生产默认 deadline 540 s）→
      锁计数（后）**≤ 前**（不满足 → 记录，不回滚）；记 `.pbf` 前/后（预期上升，key 轮换）；prewarm summary 判据用真实键：
      `requests_total` / `failed_count` / `cache_hits` / `deadline_skipped` / `per_source[*].{error,png_failed,png_out_of_contract}` /
      `failures[]`（**截断前 20 条**，只作分类样本），瓦片失败数按 `failed_count − Σ png_failed` **精确**求得，判定口径见风险包
      「Resource limits」（0，或全部为超时/连接类且 `failed_count ≤ 20` 可判；5xx = 回归）；
      `failures[]` 中每条降水项 `error_code == PRECIP_CYCLE_NOT_MIRRORED` 且 `error_reason == mirror_root_unconfigured`；
      `per_source[*].error` 全 null、`png_out_of_contract` 全 0；`deadline_skipped` 与超时类 `error` 按风险包「Resource limits」口径记。
      `river-network-national/5/25/12` 再打一次记 `X-Tile-Cache`。
- [x] 1.11 plan-only 首跑：`NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=true bash scripts/node27_mvt_cache_retention_once.sh`
      rc 0；summary `execution_mode == "plan_only"`、`counts`、`precip_root_untouched == "/home/nwm/.cache/nhms/mvt/precip"`（该字段是路径字符串，`openspec/specs/mvt-tile-cache-lifecycle/spec.md` 定义）、`planned/skipped/failed` 中含 `precip/` 的路径 0 条、
      `planned` 长度（预期 0）。
      rc≠0 或 `execution_mode` 不符 → 记录为回收车道未上线（不回滚 display），退 6。
- [x] 1.12 `systemctl --user enable --now nhms-node27-mvt-cache-retention.timer`；`list-timers` 可见，`NEXT` 为次日 04:05 UTC。
- [x] 1.13 operator 触发首个生产 tick：`systemctl --user start nhms-node27-mvt-cache-retention.service`；
      模板 `:38-42` 的 jq 健康判据退 0；`skipped[]` 按 `reason` 直方图记录。
- [x] 1.14 起 writer timer，记窗口时长；等首个自然 autopipe tick（**有界 ≤ 30 min**，依据 = 当前 10 min 一发、单 tick ~10 s 的实测；
      到界未观测 → receipt 记「首个 tick 未在窗口内观测」并交接，**不声称** #2145 偏离 8 结清）：
      `ExecStart` 为 `/home/nwm/NWM/...`、日志段 `seed_failed stage=import` 0 条、无 `CACHE_WARM_TOKEN unset`、
      tick 内 prewarm summary 的瓦片失败数（`failed_count − Σ png_failed`，口径同 1.10）。同段记 download（30 min 节拍）与
      `nhms-node27-frontier-alert`（`*:00/30`）的 `ExecMainStartTimestamp` 是否 `> T_START`：是则记其 `ExecStart` 路径与 `Result`，
      否则记「未在窗口内观测」，并入 1.17 次日补记。
- [x] 1.14b `systemctl --user start nhms-node27-resource-governance.service`（只读审计）；记其 `ExecStart` 路径、`Result`、
      journal 末行（预期与解钉前相同的 `RESOURCE_GOVERNANCE_CRITICAL:DATABASE_SIZE_ABOVE_CRITICAL`，证明从活动树跑且行为不变）。
- [x] 1.15 公网复测 `https://test.nwm.ac.cn`：#2190 `9999-12-31T23:59:59-08:00` / `0001-01-01T00:00:00+08:00` ×
      `hydro-national/q_down` 与 `hydro/{run_id}/q_down` → 422 `VALIDATION_ERROR`（控制组 `not-an-instant` 422）；
      #2078 `/api/v1/layers?offset=999999999` → 200 `data: []`，`:8080` 上预热后 `/api/v1/runs` TTFB、255 条 junk 后再测 ≤ 10 ms；
      #2079 `:8080` 上不带头 / 外部 `x-nhms-cache-warm: refresh` / 正确 token 三组 TTFB（正确 token 组明显更慢）。不达标 → 记录。
- [x] 1.16 `df` 改后；8 个 unit 终态 dump；CAPACITY HOLD 目录 `ls -la` 与改前逐字相同；备份目录路径与回滚命令入 receipt。
- [ ] 1.17 **PR 后、次日 07:00Z 后**：`systemctl --user show` raw-retention / timeseries-compression / timeseries-retention 三个 service
      （加 1.14 未观测到的 download / frontier-alert）的 `ExecMainStartTimestamp`（须 `> T_START`）/ `ExecStart` / `Result` + 各自日志末段，
      以评论补记到 #2162（issue 因目标 2 保持 open）。
- [x] 2.1 receipt `docs/runbooks/receipts/2026-09-13-issue-2162-unpin-node27-runtime.md`（含 #2145 偏离 5 / 8 的结清段或「未结清」说明）。
- [x] 2.2 `docs/runbooks/receipts/2026-09-08-issue-2032-mvt-cache-retention-node27.md` 追加 §7「5.3 部署」（指向 2.1），
      并把 §6 标题改为「历史：deferred 记录（已由 §7 执行）」指向 §7，避免自相矛盾。
- [x] 2.3 `openspec/changes/archive/2026-09-08-mvt-tile-cache-lifecycle-retention/tasks.md:38` 勾选、去掉 deferred 说明。
- [ ] 2.4 #2162 评论：决策留痕 + 目标 1 证据摘要 + 目标 2 保持 open 的说明 + 1.17 待补项；不 `Closes #2162`。

## Evidence Floor

- 本地：`openspec validate unpin-node27-runtime-to-active-tree --strict --no-interactive` + markdownlint（CI，`docs/**`）。
  **无 pytest**：零代码 diff；`scripts/select_ci_tests.py` 对纯 `docs/**` + `openspec/**` 清单选不出后端套件，输出贴进 PR。
- node-27（全部为本窗口实测，脚本原文与输出目录入 receipt）：
  1. 改前基线（1.1 全部读数，含 drop-in 重枚举 = 8、MainPID ∈ uvicorn 进程集合且其余 ppid 归于它、已装 timer cadence 上账）。
  2. 8 个 unit 改前/改后机械 diff 原文（为空，或只含允许残差并逐行标注来源 FragmentPath 行）；4 个 env 的 REPO 键。
  3. 备份目录清单与 `cmp` 对账。
  4. token：两文件 0600、两行 sha256 相等（值不出现）。
  5. 回收 env：`stat`、非 symlink、根相等。
  6. display 重启：`/health` 时间、`basin_id` 冒烟、`/proc/<MainPID>` 三项、`yd` pid 不变。
  7. 四个读面状态 + `river-network-national` 改前/改后 ETag 与 `X-Tile-Cache`。
  8. prewarm 前/后锁计数（后 ≤ 前）、`.pbf` 前/后（**上升为预期：key 轮换，非回归**）、summary 的
     `requests_total / failed_count / cache_hits / deadline_skipped / 瓦片失败数（精确差） / per_source 分解`。
  9. plan-only summary 三判据。
  10. `list-timers` 行；operator tick 的 jq 健康判据退 0 + `skipped` 直方图。
  11. writer timer stop/start 时间戳与窗口时长；首个自然 autopipe tick 的四项（或「未观测」记录）；download / frontier-alert 首跑；
      resource-governance operator 触发的 `Result` 与 journal 末行。
  12. 公网 #2190 / #2078 / #2079 判据表。
  13. `df` 改后、终态 dump、HOLD 目录不变、备份目录与回滚命令。
  14. （PR 后）1.17 三条 daily lane 首跑 `Result`，以 #2162 评论落地。
- 偏离记录（PR body `偏离记录` 段）：
  1. **未按 issue 字面跑 `scripts/ops/start-display-api.sh`**：其 `UVICORN_PATTERN` 在本机同时匹配 `yd-NWM` 的 `:8081`
     实例，systemd 分支 stop 本 unit 后会 `kill -TERM/-KILL` 剩余匹配（即 `yd`）。改为手工复现该分支
     （已装 unit 与 `a8db554d` 逐字节相同，`install` 步骤为 no-op）。bug 已立单 #2282，不在本单修。
  2. **验收第 2 项（`/home/nwm/NWM` 在 master 且含 `e0cfe40b`）未满足**：活动树保持 `a8db554d`（已含 `e0cfe40b`，
     祖先关系成立）；恢复 master 是目标 2，按验收第 1 项的「写明保留理由 + 复查时间」路径留痕，issue 保持 open。
  3. **首个生产 tick 由 operator `systemctl --user start` 触发**，不是 timer 04:05 UTC 自发；unit、env、runner 与 timer 触发
     完全相同，只差触发源；timer 的 `NEXT` 行另记。
  4. **`NHMS_PRECIP_MIRROR_ROOT` 未配**（见 proposal Non-Goals）：解钉后每 tick 的 prewarm rc=1 来自降水 PNG
     `mirror_root_unconfigured`，瓦片侧失败 0；由 #2017 收口。实测：手工 prewarm rc=1、10 个失败全是该类；tick 日志打的
     `MVT prewarm rc=0 (non-fatal)` 是 `node27_autopipe_cron.sh:245` 的 `$(ts)` 冲掉 `$?` 的日志缺陷（该行只在非零退出时出现），派生发现 #2283。
  5. 编辑了 **archived** change 的 `tasks.md:38`（issue 验收标准明写）；不改其他归档内容。
  6. **三条破坏性 daily lane 的解钉后首跑不在 PR 前观测**（已装 timer 实测 03:35Z / 04:25Z / 05:15Z，窗口后 11–13 h），按 1.17 次日补记；
     不提前手工触发生产数据动作。实测补充：download 首跑在 timer 起动的同一秒（00:52:08 CST）发生，窗口脚本的 `> T_START` 判成「未观测」，
     窗口后 `systemctl show` 补证 `Result=success`、`ExecStart=/home/nwm/NWM/scripts/node27_download_once.sh`；frontier-alert 17:00:32Z 首跑 `success`、`ExecStart=/home/nwm/NWM/scripts/node27_frontier_stall_alert_once.sh`（窗口后补证）。
  8. **窗口脚本以 rc=6 结束但生产 tick 成功**：plan-only 与生产 tick 同秒起跑，`node27_mvt_cache_retention_once.sh:78` 的 summary 文件名
     秒级分辨率，生产 summary 覆盖了 plan-only 文件；脚本判「新文件」而非「新内容」故误报。plan-only 内容已在覆盖前复制保存，
     `mvt-cache-retention.log` 两次 JSON 完整；健康判据（`production_execute ∧ failed=[]`）rc=0。派生发现 #2284，本单不修。
  7. **窗口跑了两次**：第一次（16:47:05Z）在 1.6 因 `openssl rand -hex 32` 失败触发回滚——`PATH` 里的
     `/usr/local/bin/openssl` 链接到本机没有的 `OPENSSL_3.2.0` 符号，根本起不来；回滚按设计执行（8 个 pin 从备份恢复、
     `daemon-reload`、display **未重启过所以不重启**、writer timer 由 EXIT trap 重启），回滚后 8 unit 的 `systemctl show`
     捕获与改前 `cmp` 逐字相同。第二次（16:50:22Z）改用绝对路径 `/usr/bin/openssl`（3.0.2），token 仍按 fixture 口径生成；
     两次的 `OUT`/备份目录都入 receipt。
- 由本次派生、**不在本单做**的后续：
  - #2282 `start-display-api.sh` 的进程匹配误杀同机其他 `apps.api.main:app` 实例。
  - #2285 `nhms-node27-raw-retention.service` / `nhms-node27-timeseries-compression.service` 已装 unit ≠ 仓库版本（receipt 记 diff，
    与本单无因果；下一次 `git pull` 不会自动修正，需人手 `install` + `daemon-reload`）。
  - `nhms-node27-resource-governance.service` 因 `DATABASE_SIZE_ABOVE_CRITICAL` 持续 failed（#2273 相关）。
  - 冷 prewarm 成本（`deadline_skipped` / 超时）→ #2017 task 7.2。
  - 目标 2（恢复 master）：#2280 + #1987 task 5.2；届时 8 个 unit 仍需逐项核对、不得自动解除任何 HOLD。
