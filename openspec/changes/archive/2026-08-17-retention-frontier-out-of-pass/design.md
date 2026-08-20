# Design: retention-frontier-out-of-pass

坐标系：master（#1364 merge 后）。关键既有事实：
`run_retention(*, active_lower_bound=None, active_lower_bound_source=None)`
（retention.py:409-437）默认 None ⇒ 纯墙钟；`RetentionResult.to_dict()` 含
`frontier` 块（:104-127）；pass receipt 写至
`<evidence_dir>/<pass_id>.json`（scheduler_evidence.py:367-398），
`"retention"` 在压缩白名单（:46），frontier 块压缩后仍可读（#1307 场景钉过）；
evidence root env `NHMS_SCHEDULER_EVIDENCE_ROOT`（scheduler_config.py:106）。

## D1 面 A 前沿来源：最新 pass receipt 的 `retention.frontier`

新模块 `services/orchestrator/retention_frontier.py`：

```python
@dataclass(frozen=True)
class FrontierReadResult:
    status: str            # "ok" | "unavailable"
    active_lower_bound: datetime | None
    source: str | None     # ok 时 "receipt:<原 source>" 或 receipt 记录的 null 镜像
    reason: str | None     # unavailable 时机器可读 reason
    receipt_path: str | None
    receipt_started_at: datetime | None

def read_latest_pass_frontier(evidence_dir: Path, *, now: datetime,
                              max_age: timedelta) -> FrontierReadResult: ...
```

- **最新一份的选取**：`evidence_dir` 下 `*.json`、**排除
  `*.pre_execution.json`**（`reserve_pre_execution_evidence`
  scheduler_evidence.py:302-320 往同目录写 `<pass_id>.pre_execution.json`，
  与终件同 `started_at`、无 `retention` 块——不排除会在健康稳态下并列、
  在 pass 预留后崩溃时严格最新，两者都把 CLI 打进永久 dry-run，
  fixture review P1-2），按 receipt 内 `started_at` 取最大（文件名 pass_id
  不保证字典序可比；mtime 可被拷贝扰动）；**`started_at` 并列时取文件名
  字典序最大者**。选取阶段跳过不可解析/缺 `started_at` 的文件；单文件
  读取上限沿用 `MAX_EVIDENCE_BYTES` 口径（防超大文件拖垮 CLI）。
- **新鲜度（双边）**：`abs(now - receipt.started_at) > max_age` ⇒
  `unavailable/receipt_stale`——**未来侧同样判 stale**（review round-1 B：
  时钟前跳/手拷 receipt 的 started_at 在未来时会永久赢得选取且单边判据下
  永不过期，其 null bound 会伪装成健康镜像静默真删；判 stale 而非选取时
  跳过——跳过会静默回落到更老 receipt、掩盖时钟故障）。max_age 来自 env
  `NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS`（默认 **24**——生产 scheduler
  为分钟级循环，24h 无 receipt 即流水线停摆，删除本就该缓）；env 解析
  total：空/0/负/非数字/**溢出**（timedelta 上限外，review round-1 A）
  一律回落默认，`max_age_from_env` 永不 raise。
- **unavailable 唯一枚举**（fail-closed 判定表，测试逐格；本列表为权威，
  fixture review P2-1）：
  - `evidence_dir_unresolved`——evidence_dir 派生本身失败（D2）。
    **产出层脚注（fixture review N3）**：helper 签名以 `evidence_dir` 为
    入参，此 reason 由 cli.py 包裹层（派生失败时）产出，与 helper 的其余
    7 个 reason 同一命名空间、同走 `frontier_blocker`；
  - `evidence_dir_missing`——目录缺失/不可读；
  - `no_readable_receipt`——无任何可解析且含 `started_at` 的 receipt
    （选取规则下「全部 started_at 畸形」与「只剩 `*.pre_execution.json`
    件」都与空目录同形，恒落此 reason——排除规则已把 pre_execution 件
    挡在选取之外，不得为产出别的 reason 给它开特例，fixture review N1）；
  - `pass_retention_not_run`——选中 receipt 的 `retention.status` 为
    `disabled` 或 `error`（scheduler_runtime.py:1970,1985 两个无 frontier
    块的形，语义上单列以给运维正确指引，fixture review P1-5）；
  - `frontier_block_missing`——选中 receipt 无 `retention` 键或无
    `frontier` 块（且不落上一条）；
  - `frontier_bound_invalid`——bound 字符串畸形（不静默当 null）；
  - `receipt_stale`——过旧；
  - `frontier_read_error`——helper 内任何未预期异常的包裹形。
  `receipt_started_at_invalid` **不设**：现选取规则下不可达（选取阶段已
  跳过畸形 started_at 文件）。
  **判定优先级（fixture review N2）**：选中 receipt 后**先**判
  `retention.status ∈ {disabled, error}` ⇒ `pass_retention_not_run`，
  **再**判 `retention`/`frontier` 键缺失 ⇒ `frontier_block_missing`——
  disabled 形 receipt（scheduler_runtime.py:1970-1971）有 `retention` 键、
  无 `frontier` 块，两条同时命中时必须落前者，否则
  `pass_retention_not_run` 成死代码、运维恢复指引失效。
- **null 镜像（ok 形）**：新鲜 receipt 的 `frontier.active_lower_bound:
  null` 按 ok 返回 bound=None——pass 自己当趟就按纯墙钟跑了，CLI 镜像 pass
  语义而非比 pass 更严。**source 标签不进 frontier 块**：retention.py:114
  在 bound 为 null 时恒写 `"source": null`（#1307 有意裁定，本 change 不
  动 pass 侧），镜像标签 `"receipt:none"` 记在 cleanup payload 顶层
  `frontier_source` 键（fixture review P1-1）。bound 非 null 时解析为 UTC
  datetime，`active_lower_bound_source` 传 `"receipt:<原 source>"`（此时
  frontier 块与顶层 `frontier_source` 一致）。

## D2 面 A 接入：`_run_cleanup` fail-closed 语义

- `_run_cleanup`（cli.py:92-105）改为：读 helper →
  - `ok` ⇒ `run_retention(..., active_lower_bound=bound,
    active_lower_bound_source=source)`，payload 顶层加 `frontier_source`
    （P1-1 的 null 镜像标签载体；bound 在场时与 frontier 块一致）；
  - `unavailable` ⇒ **强制 dry-run**（`config = replace(config,
    dry_run=True)`，与 scheduler_runtime.py:1974 同法），照常出 plan，
    payload 顶层加 `frontier_blocker = {"reason": ..., "forced_dry_run":
    true, "receipt_path": ...}`——运维仍能看到「本会删什么」，但一个字节
    都不删。不提供绕过 flag（proposal Non-Goals：escape hatch 否决）。

> **Supersession（后续追加，原文不改）**：上面那条 `frontier_blocker`
> 三键形状已被 #1503 / `retention-lane-hygiene` 扩展——blocker 增
> `evidence_dir` 键（探测到的绝对路径；目录本身解析失败时为显式 null），
> ok 路径 payload 顶层同增一个同名键。本文件是归档证据（记录当时的裁定），
> 现行权威形状见 `openspec/specs/production-scheduler-orchestration/spec.md`。

- **cleanup 的「receipt」= stdout JSON**（cli.py:467/:779 的
  `json.dumps`），非落盘文件；`frontier_blocker`/`frontier_source` 加在该
  payload 顶层，本 change 不引入落盘 receipt（fixture review P2-3）。
- evidence_dir 派生：`ProductionSchedulerConfig().evidence_dir`
  （scheduler_config.py:569-577，缺 root 时回落
  `<workspace_root>/scheduler/evidence`）——**派生本身也在 try 内**，构造
  抛错（如 confinement 检查 :585-598）⇒ `unavailable/
  evidence_dir_unresolved` + 强制 dry-run，不吐 traceback（fixture review
  P2-2）。测试经 `NHMS_SCHEDULER_EVIDENCE_ROOT` /
  `NHMS_SCHEDULER_WORKSPACE_ROOT` 注入临时目录。
- `pass_retention_not_run` 形的**运维恢复路径**（P1-5）：让一趟 pass 重新
  产出 frontier 块——`NHMS_RETENTION_ENABLED=true` +
  `NHMS_RETENTION_DRY_RUN=true` 跑一趟（pass 仍零删除），随后 cleanup 即可
  取到新鲜 frontier；**不是**给 CLI 加旁路 flag。写进 helper/CLI 注释。
- click（cli.py:461-468）与 argparse（:700-703/:777-779）两入口共用
  `_run_cleanup`，行为一致；`--execute` 语义不变（但 unavailable 时
  `--execute` 也被强制 dry-run——这正是 fail-closed 的定义）。

## D3 面 A 判定表（Invariant Matrix 的 I1-I5 来源）

| receipt 状态 | cleanup 行为 | payload 可读证据 |
|---|---|---|
| 新鲜 + bound 在场 | 正常删，`cycle_time >= bound` 记 `pipeline_frontier_exempt` | frontier 块（bound/source/protected_count）+ 顶层 frontier_source |
| 新鲜 + bound null | 纯墙钟删（镜像 pass，不比 pass 严） | frontier 块 bound=null（source 按 retention.py:114 恒为 null）+ 顶层 frontier_source="receipt:none" |
| 新鲜 + retention.status=disabled/error | 强制 dry-run | frontier_blocker.reason="pass_retention_not_run"（恢复路径见 D2） |
| 过旧 | 强制 dry-run | frontier_blocker.reason="receipt_stale" |
| 缺失/不可读/无块/畸形 bound/派生失败 | 强制 dry-run | frontier_blocker.reason=对应枚举 |
| helper 自身异常 | 强制 dry-run（包裹为 unavailable，不逃逸崩 CLI） | frontier_blocker.reason="frontier_read_error" |

## D4 面 B 范围裁定：维持 display-watermark 锚点（具名记录）

**裁定：不接前沿判据。** 理由（仓内双载体：本 design + spec delta 场景；
issue 评论仅为 PR body 转述，不作为一致性对读对象——fixture review
P2-4/N4）：

1. **结构性不可达**：pass receipt 与 journal 都在 node-22 私有 `/scratch`
   （`NHMS_SCHEDULER_EVIDENCE_ROOT=/scratch/frd_muziyao/...`，
   infra/env/compute.example:61；journal 同 workspace 根）。node-27 只挂
   shared NFS（`/ghdc/data/nwm` = node-27 `/home/ghdc/nwm`），读不到。
2. **接判据的前置是新的跨节点发布面**（scheduler 往 shared store 写前沿
   sidecar），涉及 shared-store 写面治理与新鲜度/一致性契约，规模远超本
   issue 的 M；若未来需要按 stage-change-pipeline 另立。
3. **既有缓解已存在**：面 B 的 cutoff 锚点不是纯墙钟——`main()` 以
   `fetch_display_watermark`（`MAX(cycle_time) ... status IN ('succeeded',
   'parsed','published')`）作 reference_time 且 fail-closed（取不到 exit 2）。
   流水线整体停摆时水位不前推，删除随之停住。
4. **残余风险具名**：水位是上界不是下界——**追赶/backfill 中「老于
   `watermark - retention_days`」的 cycle 在面 B 无保护**。触发需要
   backfill 深度超过 retention_days（默认 14 天）且恰逢 daily timer；风险
   窗口窄但真实，接受并披露（anchor 块 + spec 场景 + runbook 不改——该
   script 的 runbook 面由 summary 自述）。

## D5 面 B 附带硬化：闸 + 披露

- **execute-only 裁定的显式修订（fixture review P1-3）**：commit `9c1625ee`
  （"Make node27 raw retention execute-only"）曾有意删除 `dry_run` 字段、
  `_env_flag`、`infra/env/node27-raw-retention.example` 中的
  `NODE27_RAW_RETENTION_DRY_RUN` 与两个 CLI flag；
  `migrate-downloads-to-node27-retire-node22-db` tasks.md:135-140 以
  Evidence Floor（"no longer expose NODE27_RAW_RETENTION_DRY_RUN"）+
  receipt `docs/runbooks/receipts/2026-06-27-node27-raw-retention-
  production-proof.md` 记为已交付。本 change **显式取代**（supersede）该
  裁定的 env 面：原裁定动机是消除 dry-run 默认导致的静默不删；本次只回补
  **env 闸**（**CLI `--dry-run`/`--execute` flag 保持移除**，既有钉测
  `test_node27_raw_retention_dry_run_cli_is_removed`
  tests/test_node27_raw_retention.py:109-111 **继续绿**），默认
  `false`/`true` 保持 execute-only 语义逐位不变，闸仅供灰度/回滚软着陆。
- `RawRetentionConfig` 增 `enabled: bool` / `dry_run: bool`；env
  `NODE27_RAW_RETENTION_ENABLED`（默认 true）/
  `NODE27_RAW_RETENTION_PLAN_ONLY`（默认 false）——**默认逐位保持现行为**
  （always-on 执行）。**dry-run 闸刻意不复用旧名
  `NODE27_RAW_RETENTION_DRY_RUN`**（fixture review N5）：旧变量默认
  true（不设即 dry-run）、新闸默认 false，同名反默认意味着 node-27 实配
  里任何遗留的 `NODE27_RAW_RETENTION_DRY_RUN=true` 行会把生产静默打回零
  删除——正是 9c1625ee 要消灭的失败形；换名后遗留行惰性无害，风险结构性
  消除，无需 rollout 实配核查。`enabled=false` ⇒ summary
  `status="disabled"` 零删除；`plan_only`（内部字段名 `dry_run`）为
  true ⇒ 照常收集 targets、零 `rmtree`，summary 记 `dry_run: true`。
  `infra/env/node27-raw-retention.example` 同步补注释（即 9c1625ee 删除
  处，写明本 change 取代旧 evidence floor 而非打破）。
- summary payload 增 `anchor` 披露块：

```json
"anchor": {
  "mode": "display_watermark",
  "reference_time": "...",
  "frontier_active_lower_bound": null,
  "decision": "issue-1407-keep-watermark-anchor",
  "residual_risk": "backfill cycles older than watermark - retention_days are unprotected"
}
```

  SCHEMA_VERSION 升 v3（payload 增键）；既有消费者核查由测试面承担（该
  summary 的读者是运维与测试，无机器管道）。
- 布尔 env 解析复用仓内既有惯例（如有共享 helper 则用之，无则本地小
  parser，"1/true/yes" 族），实现时指认。

## D6 测试面

- 新 `tests/test_retention_frontier.py`：D3 判定表逐格（含 helper 异常
  包裹形、畸形 bound、`started_at` 选取正确性——两份 receipt 乱序 mtime）。
- cleanup 双入口：click runner 与 argparse main 各至少 1 条（AC3）；追赶
  场景 `--execute` 出 `pipeline_frontier_exempt`（红-绿：接线前该场景被真
  删）；unavailable + `--execute` 强制 dry-run 且目录仍在（「取不到 ⇒
  不删」主锚，红-绿）。
- `tests/test_node27_raw_retention.py`：enabled/dry_run 闸三态 + anchor 块
  在场 + 默认行为逐位回归（既有用例全绿）。
- 回归：`tests/test_retention.py` 全绿（pass 内零改动）。

## Invariant Matrix（pin 的行为）

| # | 面 | 不变式 | 锚 |
|---|---|---|---|
| I1 | CLI | 新鲜 receipt bound 在场 ⇒ `>= bound` 的目录不删、记 `pipeline_frontier_exempt` | tasks 2.2 |
| I2 | CLI | receipt 缺失/过旧/畸形/读取异常 ⇒ 强制 dry-run，零删除，blocker 入 receipt | tasks 2.3 |
| I3 | CLI | 新鲜 receipt bound=null ⇒ 镜像 pass 纯墙钟（不比 pass 严） | tasks 2.4 |
| I4 | CLI | click 与 argparse 两入口同行为 | tasks 2.5 |
| I5 | pass 内 | scheduler 路径零改动（`run_retention` 签名与语义不变） | tasks 2.6 回归 |
| I6 | node-27 | 默认行为逐位不变；`enabled=false` 零删除；`dry_run=true` 零 rmtree | tasks 2.7 |
| I7 | node-27 | summary 含 anchor 披露块（decision + residual_risk） | tasks 2.7 |
