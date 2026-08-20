## Context

`services/orchestrator/retention.py` 按「单一 object-store 根」建模：`plan_retention(object_store_root=...)`
解析出唯一 `root`，再对它跑 `_collect_cycle_targets`（`raw`/`canonical`/`forcing`）与 `_collect_run_targets`（`runs/`）。
SHUD 运行时与 copyback 却按多根建模，且生产实配三根互不相同。

实机基线（node-22，2026-08-19，只读取证）：

| 根 | 路径 | `runs/` 条目 | 最老 cycle | 最新 cycle |
|---|---|---|---|---|
| `OBJECT_STORE_ROOT`（已扫） | `/scratch/frd_muziyao/nhms-prod/object-store` | 986 | 2026-08-11 | 2026-08-19 |
| `WORKSPACE_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace` | 5274 | 2026-05-30 | 2026-08-18 |
| `NHMS_OBJECT_STORE_COPYBACK_ROOT` | `/ghdc/data/nwm/object-store`（NFS） | 3375 | 2026-06-30 | 2026-08-18 |

两个额外根的 run 名 **100% 可解析**（5274/5274、3375/3375），
含 `fcst_*` 与 `cycle_*` 两种形状，均在 `run_identity.py:27-36` 的 canonical 正则内。

## Goals / Non-Goals

Goals:

- 额外根的 `runs/` 纳入回收面，沿用同一套判定顺序（窗口 → 前沿豁免 → 删除）与保护语义。
- 额外根用独立窗口，默认 30 天。
- receipt 能逐条判读「这条属于哪个根、按哪个窗口裁定」。
- 闸门默认关：合并本身零行为变化。

Non-Goals（每条都有具体理由，不是省事）:

- **不回收额外根上的 cycle 前缀（`raw`/`canonical`/`forcing`）。** copyback 根的 `forcing/` 是 node-27
  display 的在线服务面：`docs/runbooks/object-store-forcing-series-read.md:113` 明确「可查询窗口等于
  node-27 上 `/home/ghdc/nwm/object-store/forcing/{source}/` 下仍保留的 cycle 目录集合」，
  `infra/env/display.example:30` 指向同一目录。按 14/30 天回收它 = 把 display 可查询窗口从 63 天砍到 30 天，
  是静默功能回归。copyback 根的 `raw/` 实测已稳定在 14 天（另有 node-27 raw-retention 车道在管），同样不接管。
  ⇒ **额外根一律 runs-only**，且此约束必须有测试钉死。
- 不改主根（`OBJECT_STORE_ROOT`）的任何现有行为，包括 cycle 前缀回收与 14 天窗口。
- 不改 `PROTECTED_PREFIXES={tiles,states}`、`STATIC_SEGMENTS={grid}` 的既有保护语义。
- 不改前沿豁免的推导（`scheduler_runtime._retention_active_lower_bound`）。
- 不做 `forcing/` 的跨节点保留期策略（属 node-27 display 数据保留期决策，另行立项）。

## Decisions

### D1：额外根 runs-only，不复用 `_collect_cycle_targets`

新增入参承载「只扫 runs 的根」，实现上只调用 `_collect_run_targets`。
Alternative（对额外根也跑 cycle 前缀）被否：见 Non-Goals 第一条，会砍掉 display 服务面。

### D2：独立窗口，两个 cutoff

`run_retention` 计算两个 cutoff：主根 `now - NHMS_RETENTION_DAYS`，额外根 `now - NHMS_RETENTION_EXTRA_ROOTS_DAYS`。
默认 30 天的理由有二，**第二条才是硬约束**：

1. 取证：额外根的 run 工作区是失败现场与事后取证的唯一载体，比主根产出更值得多留一倍时间；
   且 30 天把 copyback 根首次回收量从 2423 降到 1328，降低一次性删除风险。
2. **跨节点 ingest 滞后（binding constraint）**：copyback 根的 `runs/` 不是死字节，而是
   **node-22 生产者 → node-27 消费者的交接面**。node-27 的 autopipeline 从
   `<OBJECT_STORE_ROOT>/runs/` 发现 run（`scripts/node27_autopipeline.py:709-714` `_discover_runs`，
   模块 docstring `:1-35` 明写该 root 即 `/home/ghdc/nwm/object-store`，与 22 的 copyback 根同一 NFS 导出），
   并读 `runs/<run_id>/input/manifest.json` 与整棵 run 树完成 register → forcing handoff → parse
   （`:745`、`:964`）。`nhms-node27-autopipe.timer` 实测约每 30 分钟触发一次（2026-08-19 23:24 刚跑过）。
   已 ingest 的 run 由 DB 跟踪并跳过（`:867-886` `_already_ingested_runs`），因此**稳态安全**：
   ingest 滞后是分钟级，30 天窗口留了三个数量级的余量。
   **残余风险（记录在案，不由本变更消除）**：若 27 的 ingest 中断超过窗口期，或需要重跑早于窗口的 run 的 parse，
   届时 22 已把 run 树删掉，磁盘上找不到输入。这是一条跨节点耦合，缓解手段是先 dry-run 审清单 + 闸门默认关。

与前沿的关系：`active_lower_bound` **不由 `retention_days` 推导** —— 它取
candidate/blocked cycle、非终态 skipped cycle、discovery window floor 三者的**最小值**
（`scheduler_runtime.py:1912-1961`）。稳态下它约在 15 天前，比 30 天 cutoff 更晚，此时前沿闸对额外根不触发；
但**追赶/replay 期它可以远早于 `now-30d`，届时前沿闸会真实触发**（tasks 2.8 正是构造这一情形）。
判定顺序在所有根上不变：窗口 → 前沿 → 删除。

### D3：receipt schema v1 → v2

`to_dict()` 现有单个 `retention_days` / `cutoff` 标量；一份 receipt 现在混载两个窗口的判定，
沿用 v1 会让「某条 planned 按哪个窗口裁定」不可判读。故：

- `schema_version` → `nhms.production_scheduler.retention.v2`
- 新增顶层 `extra_roots: {enabled, retention_days, cutoff, roots: [<abs path>...]}`
- `planned` / `deleted` / `skipped` / `failed` 条目新增 `root` 字段（额外根下 `runs/<run_id>` 的 `key`
  会与主根重名，无 `root` 则 receipt 不可判读）

仓内 `schema_version` 消费者只有 `retention.py:124` 产出侧与 `tests/test_production_scheduler.py:18601/18647/18704`
三处断言，无外部消费者，升版本代价可控。

**`extra_roots` 必须进 evidence 压缩白名单。** `services/orchestrator/scheduler_evidence_payload.py:659-687`
的 `_compact_retention()` 是白名单式压缩：只保留 `status`/`enabled`/`dry_run`/`forced_dry_run_*`/
`retention_days`/`freed_bytes`/`frontier` + `counts` + 四个 `*_count`，`planned`/`deleted`/`skipped`/`failed`
明细整条丢弃。新增的顶层 `extra_roots` 若不加进去就会在压缩时消失——而压缩恰恰在本变更最需要判读的场景触发
（见 Risks 的 evidence 体积一条），压缩后只剩 `retention_days: 14` 与跨三根合计的 `freed_bytes`，
读者无法判断哪条按 30 天窗裁定，D3 升 v2 的全部收益被原地抵消。
仓内先例明确：`:669-673` 的注释正是 #1307 为把 `frontier` 塞进白名单而写的（同为定长标量块）。

### D4：闸门 `NHMS_RETENTION_EXTRA_ROOTS_ENABLED`，默认 false

首次 enforce 会一次删掉 workspace 3088 + copyback 1328 个 run 目录（NFS 侧约 29G），
爆炸半径要求上线与合并解耦：本变更合并 = 零行为变化；开闸 + dry-run 审清单 + enforce 是独立运维步骤。
回滚 = 关闸，一个 env。

### D5：根去重按 `resolve()` 后比较

三根若被配成同一路径（历史同根部署），必须只扫一次：避免 `freed_bytes` 重复计数与同一目录被计划两次。
去重在 `plan_retention` 内完成，主根优先保留（主根带 cycle 前缀，语义更全）。

**只按相等去重、不做 overlap 拒绝**（对比 `services/tile_publisher/publisher.py:750` 与
`services/orchestrator/run_tree_copyback.py:55-60` 的 overlapping-root 拒绝）：额外根只扫 `<root>/runs`，
嵌套根产生的目标集天然不相交（`A/runs` 与 `A/b/runs` 无交集），相等是唯一会产生重复的情形。

### D6：额外根的删除走 `rmtree_no_follow` + containment

`_delete_entry` 今天是裸 `shutil.rmtree`（`retention.py:450-458`），配合会跟随 symlink 的
`runs_root.is_dir()`（`:288`），构成一条「`runs/` 被换成 symlink → 删到根外」的路径。
本变更把删除面对准 node-27 也在写的共享 NFS 根，该风险不再可接受。裁定：

- 额外根的 `runs/` 若为 symlink，直接跳过该根并记入 `skipped`（不是静默忽略）。
- 额外根下的删除一律用 `packages/common/safe_fs.rmtree_no_follow(path, containment_root=<resolved root>)`。
- **异常类型必须一并处理**：`SafeFilesystemError` 是 `RuntimeError` 子类（`packages/common/safe_fs.py:10`），
  **不是 `OSError`**，而 `_delete_entry`（`retention.py:450-458`）今天只 `except OSError`。不改就会逃逸：
  pass 侧被 `scheduler_runtime.py:2003` 的 `except Exception` 兜住 → receipt 塌成 `{"status":"error"}`、
  本趟明细全丢、后续条目一条都不删；**CLI 侧 `services/orchestrator/cli.py:146-154` 调 `run_retention`
  外面没有任何 try/except** → `cleanup` 直接抛栈退出，删到一半且无 payload。两者都违反
  `retention.py:20` 的模块契约与本变更 spec delta 的「cleanup never aborts scheduling 对额外根同样适用」。
  裁定：`_delete_entry` 捕 `(OSError, SafeFilesystemError)`。
- 主根删除路径**保持不变**（不在本变更范围内改既有行为），差异记录在案。

## Risks / Trade-offs

- **NFS 遍历开销**：copyback 根 3375 个 run 走 `_dir_size` 的 rglob-stat，每趟 pass 都做。
  缓解：前沿豁免与窗口内跳过均在 `_dir_size` 之前裁定（`_frontier_exempt_entry` 刻意不带 size）；
  需在 22 dry-run 时量 pass 耗时增量。
- **evidence 体积**：一次几千条 `planned` 可能触发 `scheduler_evidence_payload` 的有界压缩，
  使 receipt 里看不全删了什么。缓解：闸门首次开启走 dry-run，清单单独导出审阅。
- **删除面扩大本身**：由 D4 的默认关闸 + 两步上线承担。

## Invariant Matrix

Governing invariant: retention 只删除「属于某个已配置根的 `runs/` 下、名字匹配 canonical run-id、
cycle 早于该根适用窗口 cutoff、且早于前沿下界」的目录；任何其它路径——尤其额外根上的 cycle 前缀
（`raw`/`canonical`/`forcing`）与受保护前缀——在任何配置下都不得进入删除计划。

Source-of-truth identity/contract: `(root, key)` 二元组 —— `root` 为 resolve 后的绝对根路径，
`key` 为相对该根的 posix 路径；receipt 条目以该二元组唯一标识。

Surfaces:

- Producers: `services/orchestrator/retention.py::plan_retention` / `_collect_run_targets` / `_target_payload`
- Validators/preflight: `retention.py::_is_protected`、`_extract_run_cycle`（`run_identity.parse_run_cycle`）、`_normalize_bound`
- Storage/cache/query: 无（retention 无持久化状态；receipt 即输出）
- Public routes/entrypoints: `services/orchestrator/cli.py::_run_cleanup`（out-of-pass 手动清理）、
  `scheduler_runtime.py::_run_retention`（pass 收尾）、`scheduler_core.py::_run_retention`（`:243`，纯委派）
- Frontend/downstream consumers（**两个，均在 node-27，均读同一 NFS 导出**）:
  (a) display API 直读 `<copyback_root>/forcing/**`（`docs/runbooks/object-store-forcing-series-read.md:113`）
  —— 必须保持零影响（本变更 runs-only，不碰）；
  (b) **ingest autopipeline 直读 `<copyback_root>/runs/**`**（`scripts/node27_autopipeline.py:709-714`、`:745`、`:964`，
  由 `nhms-node27-autopipe.timer` 约每 30 分钟驱动）—— 本变更**确实会删它读的目录**，
  安全性依赖「已 ingest 的 run 由 DB 跟踪跳过」（`:867-886`）+ 30 天窗口，见 D2 第 2 条
- Failure paths/rollback/stale state: `run_retention` 的逐条删除失败记录（不得中断 pass，
  `scheduler_runtime.py:2003` "cleanup must never abort scheduling"）；闸门关闭即回滚
- Evidence/audit/readiness: retention receipt（`to_dict`，v2）、`RetentionResult.frontier()`

Regression rows:

- 额外根 + 老于额外窗口的 `runs/<canonical_run_id>` -> 被选中删除，`root` 字段指向该额外根
- 额外根 + 其下存在 `raw/`/`canonical/`/`forcing/` 目录 -> **一个 cycle 目标都不产生**（runs-only 钉死）
- 额外根 + 名字非 canonical run-id -> `unparseable_run_cycle` 跳过，永不删除
- 额外根 + cycle 在额外窗口内 -> `within_retention_window` 跳过（即使早于主窗口 cutoff）
- 主根 + 同一 cycle 且早于主窗口 cutoff -> 照旧删除（两窗口互不干扰）
- 三根 resolve 后相同 -> 计划与变更前**逐 key 一致**，`freed_bytes` 不重复计数
- 额外根不存在 / 无 `runs/` -> 静默 no-op，不抛异常
- 闸门 `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=false` -> 输出与变更前逐字节一致（除 `schema_version` 与 `root` 字段）
- 额外根的 `runs/` 本身是指向根外的 symlink -> 该根零目标（或删除被拒），`planned` 中永不出现根外路径
- receipt v2 仍带 `frontier` 块 -> `retention_frontier.py:133-134` 的消费者读法不变
- 触发 evidence 压缩的一趟 pass -> 压缩后的 `retention` 块仍含 `extra_roots` 与 `frontier`（明细可只剩 `*_count`）
- 额外根删除抛 `SafeFilesystemError`（`unsafe` 与 `io` 两种 kind） -> 该条进 `failed`、其余 planned 继续删除、
  `freed_bytes` 只计成功项、函数正常返回
- 额外根 env 为空串/未设 + CWD 下存在老 run -> 该目录不进 `planned`、磁盘仍在、`extra_roots.roots` 不含 CWD
- 未改动的下游消费者：`cli.py::_run_cleanup` 的 fail-closed 前沿语义不变

## Boundary-Surface Checklist

- 共享 helper 根：`retention.py` 为唯一实现，无兄弟副本（`grep rmtree` 已核）
- 公共入口：pass 侧 `scheduler_runtime._run_retention`、out-of-pass 侧 `cli._run_cleanup`（两者都必须转发新入参或显式不转发）
- 读面：额外根的 `runs/` 目录枚举。**注意 `_iter_dirs`（`retention.py:318-323`）只过滤 symlink 子项，
  不保护 `runs/` 目录本身**：`runs_root = root / RUNS_PREFIX`（`retention.py:288`）后的 `is_dir()`（`:289`）会跟随 symlink，
  因此一个被换成 symlink 的 `runs/` 可以把枚举面指到根外。今天只有一个 operator 自有根时风险有限；
  本变更把它对准 node-27 也在写的 NFS 根，必须处置（见下条）。
- 读面（根来源卫生，三点均须裁定，见 tasks 1.2/1.9(a)）：
  (a) `None`/空串/纯空白的额外根**必须在 `Path()` 构造前丢弃** —— `Path("").expanduser().resolve()` 等于 **CWD**，
      CWD 存在且是目录，`<cwd>/runs` 会直接进入删除面。`NHMS_OBJECT_STORE_COPYBACK_ROOT` 未设即 `None`
      （`scheduler_config.py:186-188`），且其 preflight 整体门控在 db-free / repair-authority 模式
      （`:700-706` 早退 `not_required`），非 db-free 部署下不校验。
  (b) CLI 的额外根只接受 env 显式给出的**非空**值，未设即不扫该根；**不得**套用
      `SchedulerConfig.workspace_root` 的相对默认 `.nhms-workspace`（`scheduler_config.py:83`）——
      既有 spec 正文已点名过这个「相对默认在错误工作目录下静默错解」的坑
      （`openspec/specs/production-scheduler-orchestration/spec.md:103`），在删除面上照抄它比在证据面上危险一个量级。
  (c) 主根为 `None` 或非目录时 `plan_retention` 会**早退**（`retention.py:356-360`）；额外根的收集必须裁定放在早退之前，
      否则「主根未配 → 额外根静默失效」。CLI 主根来自 `os.getenv("OBJECT_STORE_ROOT")`（`cli.py:147`），未设即 `None`，该分支现实可达。
- 写/删除/覆盖面：`_delete_entry`（`retention.py:450-458`）当前是**裸 `shutil.rmtree`，无 containment 校验**。
  裁定（D6）：额外根一律走 `packages/common/safe_fs.rmtree_no_follow(path, containment_root=<resolved root>)`
  —— 仓内先例 `services/orchestrator/run_tree_copyback.py:14,354,357`；且 `runs/` 自身为 symlink 时拒扫该根。
- staging/publish/rollback 面：`published_artifact_root` 保护对所有根生效
- 生产者/消费者证据边界：receipt v2 的 `root` 字段与 `extra_roots` 块
- 陈旧状态/幂等边界：同一 run 在多个根下同名，删除彼此独立、不得互相判定
- 未改动的下游消费者：node-27 display 的 forcing 直读面（`object-store-forcing-series-read.md:113`）；
  node-27 raw-retention 车道（`nhms-node27-raw-retention.timer`）
- **受影响的跨节点消费者**：node-27 ingest autopipeline（`scripts/node27_autopipeline.py`，
  `nhms-node27-autopipe.timer`）—— 读 `<copyback_root>/runs/`，是本变更删除面的真实消费者，见 D2 第 2 条
