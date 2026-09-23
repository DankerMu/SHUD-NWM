# Design

Change surface：
- 新增 `scripts/node22_scheduler_stall_health.py`（探针本体，只读）；
- 新增 `infra/systemd/nhms-node22-scheduler-stall-health.service` / `.timer`；
- 新增 `tests/test_node22_scheduler_stall_health.py`；
- 改 `docs/runbooks/production-ops/stuck-detection.md`（新增 §6.2：11 档处置 + 安装 + 巡检 + retune）。

**不改**：`services/**`（含 `scheduler_no_progress.py`、`monitoring.py`）、
`scripts/node22_refresh_timer_health.py`、任何既有 unit。

## Governing invariant

**一次「调度器已经不再推进」的事实，必须在无人翻产物的前提下自己变成一个 failed unit；
该 failed 状态必须随条件存在而持续存在 —— 探针不得自我确认（auto-acknowledge）它自己的告警；
且「正在干活」永远不得被判成「已经死了」。**

后半句是初版 fixture 翻车处，见 proposal 的实测表。

## D4 自包含约束（**继承先例，不得违反**）

`scripts/node22_refresh_timer_health.py:1-30` 的模块 docstring 钉死两条结构属性，
其一是：**stdlib only，不 import `packages.common` 或任何仓库包**，理由是探针要能从
`/scratch/frd_muziyao/NWM` **之外** staged 运行 —— 在那棵树里 checkout 特性分支会把未评审代码
推进实时 tick。它连 `packages/common/safe_fs.py` 的 no-follow 读都刻意自带一份。

本探针继承该约束。由此：

- **不** import `services.orchestrator.scheduler_evidence`。文件名谓词与
  `MAX_EVIDENCE_BYTES` 在探针内**自带**（谓词只是「前缀 `scheduler_` + 后缀属于
  `('.pre_execution.json', '.json')`」三行）。
- 重复由**平价测试**钉死，不靠自觉：测试 import
  `services.orchestrator.scheduler_evidence` 并断言探针的谓词与常量与
  `is_scheduler_pass_evidence_filename`（`scheduler_evidence.py:40`）、
  `MAX_EVIDENCE_BYTES`（`:27`）、`SCHEDULER_PASS_EVIDENCE_SUFFIXES`（`:37`）逐一一致。
  测试在仓库里跑，不受 staging 约束。
- 这满足 live spec `runtime-evidence-and-operations/spec.md:393` 的
  「readiness 与 retention 对同一批名字分类一致」（scenario 在 `:406`）—— 探针成为第三个消费者，
  一致性由平价测试而非 import 保证。
- `scripts/node22_scheduler_evidence_retention.py:225` 确实直接 import 了该谓词，
  但它是**另一类工具**：可变更的 retention 作业，本来就只从部署树跑，不具备 staging 属性。
  以它为先例会踩碎 D4。

## 存活信号来自 systemd，不是产物年龄

实测（node-22，`systemctl --user show`）：

```
nhms-compute-scheduler.timer:   UnitFileState=enabled  ActiveState=active
                                NextElapseUSecRealtime=（空）
                                NextElapseUSecMonotonic=<有值>   LastTriggerUSec=<有值>
nhms-compute-scheduler.service: UnitFileState=static   ActiveState=inactive  SubState=dead
                                Result=success  InactiveEnterTimestamp=<有值>
```

两点必须照顾，否则会照抄 refresh 探针照出错：

1. timer 是 `OnUnitActiveSec=5min`（相对触发），所以 **`NextElapseUSecRealtime` 恒为空**，
   只有 monotonic 有值；refresh 探针那套「next-elapse 超过 dwell 即 `timer_not_scheduled`」
   **不可移植**，本单不设该档。
2. 一趟 pass 在飞时 `systemctl list-timers` 的 NEXT 显示 `-`（timer 等 service 变 inactive 才重新武装），
   这是**健康**态。所以 `timer_stopped` 的判据必须是 **timer 不活跃且 service 也不活跃**，
   单看 timer 会把「正在干活」判成「停了」。

verdict 5 `scheduler_not_triggering` 用 `LastTriggerUSec` 年龄，且同样要求 service 当前不活跃
（一趟 193 分钟的 pass 期间 `LastTriggerUSec` 自然会老）。

产物年龄（verdict 7）保留为**独立兜底**：timer 照常触发但 service 每次秒退不写产物时，
systemd 侧全绿而产物断流。两个信号相互独立，这正是先例 spec 里
「independent signals」的用法。

## 阈值与量纲（默认值即 node-22 生产值，全部由实测反推）

| env key | 默认 | 范围校验 | 依据（实测） |
|---|---|---|---|
| `NHMS_SCHEDULER_STALL_MAX_TRIGGER_AGE_MINUTES` | 360 | ≥240 | 相邻 `started_at` max 193.9 分钟，留 ~1.9× 余量 |
| `NHMS_SCHEDULER_STALL_MAX_PASS_AGE_MINUTES` | 360 | ≥240 | 同上 |
| `NHMS_SCHEDULER_STALL_LIMIT_LOOKBACK_MINUTES` | 120 | ≥ 探针周期(15) | `resource_limit_blocked` 只在「最新」期间成立，实测 4.5–27 分钟 |
| `NHMS_SCHEDULER_STALL_LOCK_PASSES` | 5 | ≥2 | 历史三次人工清锁；单趟 `lock_contended` 是常态噪声（实测 276 趟里 1 趟） |
| `NHMS_SCHEDULER_STALL_NO_SUBMISSION_PASSES` | 20 | ≥2 | ≈2–3 小时持续「有阻塞候选且零提交」 |
| `NHMS_SCHEDULER_STALL_CIRCUIT_PASSES` | 20 | ≥1 | 刻意高于调度器自身 observe 阈值 3（`compute.scheduler-dbfree.env.example:151`） |
| `NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS` | `ambiguous_fallback_match:comment_accounting_unproven` | — | 见下 |
| `NHMS_SCHEDULER_STALL_SCAN_LIMIT` | 64 | ≥ max(no_submission, lock, 1) + 12 | 见下「排序」 |
| `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` | 4096 | ≥ scan_limit | 证据根实有 309 条且会长；retention 另有自己的上限 |
| `NHMS_SCHEDULER_STALL_EVIDENCE_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence` | 必须是目录 | 同 `infra/env/compute.scheduler-dbfree.env.example:69` |
| `NHMS_SCHEDULER_STALL_RECEIPT_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace/scheduler-stall-health/receipts` | — | 照 refresh 探针的 `workspace/refresh-timer-health/receipts` 形状；**刻意不落在证据根下**，否则探针自己的产物会进 readiness/retention 的扫描面 |
| `NHMS_SCHEDULER_STALL_TIMER_UNIT` | `nhms-compute-scheduler.timer` | — | |
| `NHMS_SCHEDULER_STALL_SERVICE_UNIT` | `nhms-compute-scheduler.service` | — | |
| `NHMS_SCHEDULER_STALL_SYSTEMCTL` | `/usr/bin/systemctl` | — | 同先例 |

探针周期固定在 unit 里：`OnCalendar=*:03/15`（每 15 分钟，偏移 3 分避开整点），
`RandomizedDelaySec=60`。`--now` 只走 CLI、**不进 env**（时间基准不可被环境静默改写，照先例）。
一切范围外配置在**收集任何证据之前**拒绝，退出码 2。

## 三类 pass 口径（verdict 10 的核心，初版这里是空白）

对参与判级的每一趟终态产物，先分类再计 streak：

- **progress**：`counts.submitted_count > 0` → **打断** streak。
- **blocked**：`submitted_count == 0` **且** `blocked_candidate_count > 0` → **延长** streak。
- **idle**：`submitted_count == 0` **且** `blocked_candidate_count == 0`，且 status 不属于中性集 →
  **打断** streak。理由：阻塞候选消失了就说明该阻塞已解除，不是停摆。
- **neutral**：status ∈ {`lock_contended`, `preflight_blocked`, `resource_limit_blocked`,
  `restart_reconciled`} **或** 两个 counts 键任一缺失 → **既不延长也不打断**，跳过并计数。
  依据：`compute.scheduler-dbfree.env.example:148-150` 对调度器自身 circuit 的同款口径；
  以及实测「counts 缺键的 4 份全部是 `resource_limit_blocked` 降级产物」——
  这批恰恰是探针最需要读的，既不能当 0 也不能整体 `probe_failed`。

中性趟计数写进 receipt。窗口内**全部**为中性时不报 verdict 10（无可判之事），
但 `resource_limit_blocked` 已由优先级更高的 verdict 8 覆盖，不会静默。

## 排序：只信 `started_at`，不信文件名，更不信 mtime

pass 名是 `scheduler_<YYYYMMDDHH>_<hex12>.json`（`scheduler_runtime.py:571`），**时间只到小时**，
同小时内由 hex 决定字典序。实测按文件名升序取最后 8 个，`started_at` 序列是
`02:35, 02:14, 02:46, 03:15, 03:21, 03:09, 03:27, 03:03` —— 乱的。步骤：

1. 枚举目录，上界 `max_entries_scanned`；
2. **先**过谓词、**再**剔除 `.pre_execution.json`，**然后**才取字典序最大的 `scan_limit` 个名字。
   次序不可调换：`.pre_execution.json` 字典序排在同 pass_id 的终态产物**之后**
   （`.json` 与 `.pre_execution.json` 比到 `j` < `p`），且实测 21 份与终态产物长期并存；
   若先截断后剔除，边界处会优先保留 pre_execution 而丢掉对应终态产物，`+12` 的余量论证即失效。
3. 逐个 bounded、no-follow、≤ `MAX_EVIDENCE_BYTES` 读取并解析；
4. 按**内容里的 `started_at`** 降序排，取前 `max(no_submission_passes, lock_passes)` 趟参与 streak 判级；
   verdict 8 用时间窗口（`limit_lookback_minutes`）而非趟数。

余量论证：小时桶与 `started_at` 的小时同源，故桶间序即时序，乱序只在桶内；
取字典序最大的 `scan_limit` 个（已剔 pre_execution），落在边界桶**之上**的至少
`scan_limit - bucket_max` 个，全部早于边界桶，故 top-N 不被污染。实测单桶最多 8 趟，
常量取 12 留余量；范围校验 `scan_limit >= max(no_submission_passes, lock_passes) + 12`，
违反即退出码 2。

## 慢性条目：无状态 reason 白名单（已定，不留「或」）

备选是「与自己上一份 receipt 做 diff，只对新增 subject 告警」。**不采用**：

1. 违反 governing invariant：diff 方案第一 tick 报、第二 tick 因不再「新增」而自愈，
   等于探针替运维签收了告警。
2. 一旦有自持状态，就继承 `frontier-stall-alerting/spec.md:34` 整条义务
   （state 损坏 → 必须 over-report、缺失 → bootstrap 且记录、baseline 只能来自真实观测）。
   bootstrap-then-freeze 本质就是「带额外步骤的白名单」。

采用 `NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS`：逗号分隔的 **reason 精确串**
（不是前缀、不是 `subject_id` —— 后者带 cycle，每个新 cycle 都要改配置，必然腐烂）。
默认值 checked-in 为 `ambiguous_fallback_match:comment_accounting_unproven`。

该 reason 为何值得永久静音，**必须在 runbook §6.2 给出处并链到 issue**，不得裸断言：
成因是 Slurm accounting 不返回 `job_comment`，exact-comment 对账因此永远 unproven
（本 session 在 node-22 实测过：reconcile `match_count 2` 无法收敛）。
tasks 4.5 负责把它写成可追溯的一段，而不是留在代码注释里。

可审计性由 receipt 承担：被抑制的每个条目照样写进 `suppressed[]`
（subject_kind / subject_id / reason / consecutive_passes / matched_rule）。
**抑制只作用于 verdict 11，绝不影响 verdict 1–10。**

残余风险（明写、不消除）：若某个真实新 stall 复用了被抑制的 reason，探针不报。
接受，因为该 reason 已判定为结构不可收敛；换 reason 类的 stall 仍会报。

## 优先级次序的理由（首个匹配者胜）

`probe_failed` > `timer_not_enabled` > `timer_stopped` > `scheduler_service_failed` >
`scheduler_not_triggering` > `evidence_unavailable` > `evidence_stale` >
`pass_limit_blocked` > `lock_contended_persistent` > `submission_stalled` >
`no_progress_circuit_open` > `ok`

- 读不了就不能判级 —— 完整性最先。
- systemd 侧整体压在证据侧之上：lane 死了的话，产物侧的一切陈述都是几小时前的，报它会指错地方。
- `timer_not_enabled` 最狠（重启后仍死）> `timer_stopped`（现在死）> service 上次崩 > 触发断流。
- `pass_limit_blocked` 压在后三档之上：那趟产物本身已降级（#2570 B 组的失败原因投影就是为这档存在的），
  诊断窗口最窄。
- `ok` 仅在**无任何条件匹配**时可达；任何证据缺失都不得回落到 `ok`。

## Sibling surfaces

- `scripts/node22_refresh_timer_health.py`（判级/退出码/有界读/receipt/D4 自包含的形状母本，不得改动）；
- `scripts/node22_scheduler_evidence_retention.py`（同一谓词的既有消费者；平价测试保证分类一致）；
- `infra/systemd/nhms-node22-refresh-timer-health.{service,timer}`（unit 形状母本：
  `Environment=PATH=`、`UnsetEnvironment` 全套 PG 变量、无 EnvironmentFile、`UMask=0077`、
  无 `PrivateTmp`、`StandardOutput/StandardError=journal`、`TimeoutStartSec`、
  以及 `Persistent=` 那段「不复活停掉的探针」的注释纪律）；
- live spec `runtime-evidence-and-operations/spec.md:393`（谓词一致性，scenario `:406`）；
- `openspec/specs/frontier-stall-alerting/spec.md`（node-27 侧同类能力，本单是其 node-22 对偶；
  差别是 node-27 有 mail lane、有持久 state，本单两者都没有且已说明理由）。

## Seams under test

探针的 `main(argv)` 公开边界 + 落盘 receipt；证据目录用 `tmp_path` 造真实文件树（真实 JSON 字节）；
systemd 用**可注入的 `systemctl` 路径**（指向测试造的假可执行文件）注入 `show` 输出，
不 monkeypatch `subprocess`。无需 live systemd / Slurm / DB。
unit 文件由静态内容断言覆盖。

## Required evidence

新模块的诚实口径：**master 上「红」= 模块不存在**，不是有效 red proof。oracle 是行为用例：

- 11 档 verdict 各一个判级用例，每档断言 verdict **与退出码**；
- 优先级用例：systemd 死 + 产物 `resource_limit_blocked` 同时成立 → 报 systemd 档；
  `pass_limit_blocked` + circuit 同时成立 → 报前者；
- **「正在干活不得判成死」**：timer `ActiveState=inactive` 但 service `ActiveState=active`
  （193 分钟长 pass 的真实几何）→ 不报 `timer_stopped`；
- 乱序用例：文件名字典序与 `started_at` 相反 → 判级取 `started_at` 最新那趟（唯一能证伪
  「按文件名排序」实现的用例）；mtime 用例：最旧产物 mtime 改成最新 → verdict 不变；
- 截断次序用例：边界处 `X.json` 与 `X.pre_execution.json` 并存 → 断言终态产物入窗
  （证伪「先截断后剔除」实现）；
- 三类口径各一个用例 + 中性趟不打断 streak 的用例 + counts 缺键不被读成 0 的用例；
- 抑制异质用例（1 条慢性 + 1 条新增 → 报）、抑制不越界用例；
- 有界/fail-closed：超 `MAX_EVIDENCE_BYTES`、软链、坏 JSON、缺 `started_at`、tracker schema 不符、
  systemctl 非零退出 → `probe_failed`，绝不 `ok`；
- 配置拒绝：范围外阈值、`scan_limit` 不足、证据根不是目录 → 退出码 2 且**未读任何产物**；
- 平价测试：谓词/常量与 `scheduler_evidence` 一致；
- unit 静态断言；receipt 可独立重推判级（每个信号的观测值与阈值并列）；
- must-remain-green：`tests/test_node22_refresh_timer_health.py`、
  `tests/test_scheduler_evidence_retention.py`、`tests/test_scheduler_evidence_decidability.py`；
- node-22 live receipt。

## Review focus

1. 存活判据是否真的是「timer 不活跃**且** service 不活跃」；单看 timer 即缺陷。
2. 三类 pass 口径是否穷尽，counts 缺键是否走中性而非 0、也非整体 `probe_failed`。
3. 排序是否只依赖内容 `started_at`；`sorted(paths)` 直接进判级即缺陷；剔除是否在截断**之前**。
4. 抑制是否只作用于 verdict 11。
5. 是否违反 D4（任何 `from services...` / `from packages...` import 即缺陷），
   平价测试是否真的钉住了重复。
6. unit 是否零 EnvironmentFile、是否 unset 全套 PG 变量、是否**绝不**触碰 `nhms-compute-scheduler.*`。
7. `ok` 是否存在任何「证据缺失时回落」的可达路径。
