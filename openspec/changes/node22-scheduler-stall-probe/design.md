# Design

Change surface：
- 新增 `scripts/node22_scheduler_stall_health.py`（探针本体，只读）；
- 新增 `infra/systemd/nhms-node22-scheduler-stall-health.service` / `.timer`；
- 新增 `tests/test_node22_scheduler_stall_health.py`；
- 改 `scripts/select_ci_tests.py`（为新脚本/unit/套件加 `PATH_TEST_RULES` 路由行，
  并加 `services/orchestrator/scheduler_evidence.py` → 新套件 这条边）；
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
  `MAX_EVIDENCE_BYTES`（`:27`）、`SCHEDULER_PASS_EVIDENCE_SUFFIXES`（`:37`）、
  **`SCHEDULER_PASS_EVIDENCE_PREFIX`（`:36`）** 逐一一致。测试在仓库里跑，不受 staging 约束。
  **只断样本名同判不够**：若上游谓词将来从「前缀 + 后缀」收紧成「前缀 + 10 位 cycle +
  12 位 hex + 后缀」，样本仍会全部同判，而探针的三行拷贝静默偏松 —— 所以**必须**把
  prefix / suffixes / 上界三个常量本身也钉住。
- **平价测试必须能在打破平价的那个 diff 上触发**，否则它只是 merge 后的事后探测器：
  `scripts/select_ci_tests.py:1132-1134` 写明 importer 闭包只在**改动路径本身是测试文件**时生效，
  生产模块路径只走 `PATH_TEST_RULES`。所以必须显式加路由行，照先例
  （`select_ci_tests.py:4068` / `:4345` 的「#2146 widened this row by one」、`:4365-4379`）。
  不加的连带后果：只动探针/unit 的 PR 一条测试都选不出，降级成 `--collect-only` 零断言冒烟。
- `runtime-evidence-and-operations/spec.md:393` 的作用域明写是
  「Readiness root discovery and scheduler pass-evidence retention」两个具名消费者
  （scenario 在 `:406`）。**探针不在其辖内**：它不产生 readiness item、不进删除选择，
  所以自带一份拷贝不违反它。这里做的是**自愿对齐**，由平价测试保证，
  不得写成「满足 :393」或「成为第三个消费者」——那会让下一个读者以为存在一条它必须遵守的约束。
- `scripts/node22_scheduler_evidence_retention.py:225` 确实直接 import 了该谓词，
  但它是**另一类工具**：可变更的 retention 作业，本来就只从部署树跑，不具备 staging 属性。
  以它为先例会踩碎 D4。
- 范围澄清：本单发出的 unit 走的是**部署树**里的绝对路径
  （`/scratch/frd_muziyao/NWM/.venv/bin/python` + 部署树里的脚本，同先例 unit）。
  D4 的 staging 收益作用于运维**临时/应急的手工运行**，不作用于这对 unit ——
  不要误以为 unit 是 staged 的。

## 存活信号来自 systemd，不是产物年龄

**实测（node-22，蹲完一整趟 pass 的起止，含收尾跃迁）**：

```
在飞  timer=active/running   service=Type=oneshot ActiveState=activating SubState=start   Result=success
收尾  timer=active/waiting   service=                ActiveState=inactive  SubState=dead   Result=success
空闲  timer=active/waiting   service=                ActiveState=inactive  SubState=dead   Result=success
      timer: UnitFileState=enabled  NextElapseUSecRealtime=（恒空）
             NextElapseUSecMonotonic=<有值>  LastTriggerUSec=<有值>
      service: UnitFileState=static
```

三条由此钉死，任何一条写错都会把「正在干活」判成「已经死了」：

1. **service 是 `Type=oneshot`，在飞时 `ActiveState=activating` / `SubState=start`，
   永远不会出现 `active`。** 所以「service 在跑」的判据是
   `ActiveState in {"active", "activating"}`（等价写法 `SubState != "dead"`），
   **不是** `ActiveState == "active"`。凡是以「service 不活跃」为合取项的档
   （verdict 3、5、7）都必须用这个判据，且必须读 `SubState`。
2. **timer 在 pass 在飞期间始终 `ActiveState=active`**（`SubState` 在 `running`/`waiting` 间摆动），
   **从不 inactive**。`systemctl list-timers` 的 NEXT 显示 `-` 是因为 active 期间不计 realtime
   next elapse，与 `ActiveState` 无关。因此 verdict 3 的合取式虽然保守无害，其**理由**不是
   「timer 在飞时会 inactive」—— 不得在 spec 或注释里那样写。
3. timer 是 `OnUnitActiveSec=5min`（相对触发），**`NextElapseUSecRealtime` 恒空**；
   refresh 探针那套「next-elapse 超过 dwell 即 `timer_not_scheduled`」**不可移植**，本单不设该档。

verdict 5 `scheduler_not_triggering` 用 `LastTriggerUSec` 年龄，且要求 service 当前**不在跑**
（一趟 193 分钟的 pass 期间 `LastTriggerUSec` 自然会老）。

产物年龄（verdict 7）保留为**独立兜底**：timer 照常触发但 service 每次秒退不写产物时，
systemd 侧全绿而产物断流。它**同样**要求 service 当前不在跑 —— 否则「一趟超过
`MAX_PASS_AGE_MINUTES` 的健康长 pass」会被判成 stale。今天 193.9 < 360 只是数值上的侥幸，
不是结构保证；把闸门写进去才是。两个信号仍相互独立（systemd 侧全绿时它照样能响），
这正是先例 spec 里「independent signals」的用法。

## 阈值与量纲（默认值即 node-22 生产值，全部由实测反推）

| env key | 默认 | 范围校验 | 依据（实测） |
|---|---|---|---|
| `NHMS_SCHEDULER_STALL_MAX_TRIGGER_AGE_MINUTES` | 360 | ≥240 | 相邻 `started_at` max 193.9 分钟，留 ~1.9× 余量 |
| `NHMS_SCHEDULER_STALL_MAX_PASS_AGE_MINUTES` | 360 | ≥240 | 同上 |
| `NHMS_SCHEDULER_STALL_LIMIT_LOOKBACK_MINUTES` | 120 | **≥30** | `resource_limit_blocked` 只在「最新」期间成立（实测 4.5–27 分钟）；下限须覆盖探针周期 15 分钟 **+ `RandomizedDelaySec=60` + 余量**，取 15 会放行一个能整段漏掉它的合法配置 |
| `NHMS_SCHEDULER_STALL_LOCK_PASSES` | 5 | ≥2 | 历史三次人工清锁；单趟 `lock_contended` 是常态噪声（实测 276 趟里 1 趟） |
| `NHMS_SCHEDULER_STALL_NO_SUBMISSION_PASSES` | 20 | ≥2 | ≈2–3 小时持续「有阻塞候选且零提交」 |
| `NHMS_SCHEDULER_STALL_CIRCUIT_PASSES` | 20 | ≥1 | 刻意高于调度器自身 observe 阈值 3（`compute.scheduler-dbfree.env.example:151`） |
| `NHMS_SCHEDULER_STALL_SUPPRESSED_REASONS` | `ambiguous_fallback_match:comment_accounting_unproven` | — | 见下 |
| `NHMS_SCHEDULER_STALL_SCAN_LIMIT` | 64 | ≥ max(no_submission, lock, 1) + 12 | 见下「排序」 |
| `NHMS_SCHEDULER_STALL_MAX_ENTRIES_SCANNED` | 4096 | ≥ scan_limit | 证据根实有 309 条且会长；**触顶即 `probe_failed`**（见下「排序」） |
| `NHMS_SCHEDULER_STALL_EVIDENCE_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence` | 必须是目录 | 同 `infra/env/compute.scheduler-dbfree.env.example:69` |
| `NHMS_SCHEDULER_STALL_RECEIPT_ROOT` | `/scratch/frd_muziyao/nhms-prod/workspace/scheduler-stall-health/receipts` | — | 照 refresh 探针的 `workspace/refresh-timer-health/receipts` 形状；**刻意不落在证据根下**，否则探针自己的产物会进 readiness/retention 的扫描面 |
| `NHMS_SCHEDULER_STALL_TIMER_UNIT` | `nhms-compute-scheduler.timer` | — | |
| `NHMS_SCHEDULER_STALL_SERVICE_UNIT` | `nhms-compute-scheduler.service` | — | |
| `NHMS_SCHEDULER_STALL_SYSTEMCTL` | `/usr/bin/systemctl` | — | 同先例 |

探针周期固定在 unit 里：`OnCalendar=*:03/15`（每 15 分钟，偏移 3 分避开整点），
`RandomizedDelaySec=60`。`--now` 只走 CLI、**不进 env**（时间基准不可被环境静默改写，照先例）。
一切范围外配置在**收集任何证据之前**拒绝，退出码 2。

## 四类 pass 口径，中性由**写方派生的可观测量**判定（不是 status 词表）

调度器自己的口径是**四类早退写入点**，不是四个 status 字符串
（`scheduler_no_progress.py:15-18`，与 `compute.scheduler-dbfree.env.example:148-150` 同源）：
「Early-exit, pre-lock, lock-contended and resource-limit-aborted passes carry empty candidate lists」。
把它映射成 status 允许表会两头都错 —— 实测证伪：

- `scheduler_2026092223_7e6955b406ba.json` 是 #2570 四趟停摆的**第一趟**，
  `status=restart_reconciled`、`submitted_count=0`、`blocked_candidate_count=47`，
  是**货真价实的 blocked pass**。把 `restart_reconciled` 塞进中性集会把事故证据跳过。
- `preflight_blocked` 由 `scheduler_runtime.py:1182/:1204` 在 `if candidates and ...` 分支里设置，
  而 `:1204` 到 circuit 钩子 `:1461` 之间**无 return** —— 这类 pass 是 fully-observed，
  调度器会计数。一次持续的 Slurm preflight 封锁若被整片判中性，verdict 10 永不触发。
- 「early-exit」根本没有对应 status 字符串，会漏进 `idle` 去**打断** streak。

**改用写方派生的判据**：`progress_guard` 在 `scheduler_runtime.py:834` 才被构造，
其后 `:882` 起逐 phase checkpoint，最终整块写进证据。**凡在 guard 武装之前 return 的 pass，
产物里就没有 `progress_guard` 键** —— 这恰好是上面那四类早退的可观测投影。
实测 276 份终态产物完全吻合：

| guard 键 | 出现的 status | n |
|---|---|---|
| **缺席** | `resource_limit_blocked` 4、`lock_contended` 1 | **5** |
| 在场 | `planned` 255、`submitted` 15、`restart_reconciled` 2、`preflight_blocked` 1、`submitted_partial` 1 | 271 |

即 `restart_reconciled`(blocked=47) 与 `preflight_blocked` 都会按 counts 正确判为 blocked，
而 resource-limit-aborted / lock-contended 自动落中性 —— 无需任何 status 词表。

分类规则（对参与 streak 判级的每一趟终态产物）：

- **neutral**（跳过，既不延长也不打断）：产物**无 `progress_guard` 键**，
  **或** `counts.submitted_count` / `counts.blocked_candidate_count` 任一缺失。
  后者是独立兜底：实测缺键的 4 份恰是 `resource_limit_blocked` 降级产物 ——
  探针最需要读的那批，既不能当 0，也不能因它整体 `probe_failed`。
- **progress**：`submitted_count > 0` → **打断** streak。
- **blocked**：`submitted_count == 0` **且** `blocked_candidate_count > 0` → **延长** streak。
- **idle**：`submitted_count == 0` **且** `blocked_candidate_count == 0` → **打断** streak。
  理由：阻塞候选消失即该阻塞已解除。（早退 pass 不会落到这里 —— 它们先被 guard 规则收走。）

中性趟数写进 receipt。窗口内**全部**为中性时不报 verdict 10（无可判之事），
而 `resource_limit_blocked` 已由优先级更高的 verdict 8 覆盖，不会静默。

## 排序：只信 `started_at`，不信文件名，更不信 mtime

pass 名是 `scheduler_<YYYYMMDDHH>_<hex12>.json`（`scheduler_runtime.py:571`），**时间只到小时**，
同小时内由 hex 决定字典序。实测按文件名升序取最后 8 个，`started_at` 序列是
`02:35, 02:14, 02:46, 03:15, 03:21, 03:09, 03:27, 03:03` —— 乱的。步骤：

1. 枚举目录，上界 `max_entries_scanned`。**触顶即 `probe_failed`，不得静默截断**：
   `os.scandir` 的返回序是文件系统序、任意的，一旦触顶，后面「取字典序最大的 `scan_limit` 个」
   就是在**任意子集**上做的，下面的 `+12` 余量论证（依赖「字典序最大 = 最高小时桶」）随之失效
   且完全无声。照 readiness discovery 的 bounded cap 口径，触顶是可报告事实而非默认行为；
2. **先**过谓词、**再**剔除 `.pre_execution.json`，**然后**才取字典序最大的 `scan_limit` 个名字。
   次序不可调换：`.pre_execution.json` 字典序排在同 pass_id 的终态产物**之后**
   （`.json` 与 `.pre_execution.json` 比到 `j` < `p`），且实测 21 份与终态产物长期并存；
   若先截断后剔除，边界处会优先保留 pre_execution 而丢掉对应终态产物，`+12` 的余量论证即失效。
3. 逐个 bounded、no-follow、≤ `MAX_EVIDENCE_BYTES` 读取并解析；
4. 按**内容里的 `started_at`** 降序排。**streak 类判级**（verdict 9/10）取前
   `max(no_submission_passes, lock_passes)` 趟；**verdict 8 的判定集合是全部成功解析出的产物
   （≤ `scan_limit`）中 `started_at` 落在 `limit_lookback_minutes` 内的那些**，
   而不是那 20 趟的子集 —— 否则 pass 密集时 120 分钟的回看会被趟数悄悄截短。
   两个集合的口径差异必须写进 receipt（各自的参与趟数）。

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
- **「正在干活不得判成死」**（用**实测几何**，不是想象的几何）：
  timer `ActiveState=active`/`SubState=running` + service `ActiveState=activating`/`SubState=start`
  → 不报 `timer_stopped`、不报 `scheduler_not_triggering`（即使 `LastTriggerUSec` 已超龄）、
  也不报 `evidence_stale`（即使最新产物已超龄）；
- 乱序用例：文件名字典序与 `started_at` 相反 → 判级取 `started_at` 最新那趟（唯一能证伪
  「按文件名排序」实现的用例）；mtime 用例：最旧产物 mtime 改成最新 → verdict 不变；
- 截断次序用例：边界处 `X.json` 与 `X.pre_execution.json` 并存 → 断言终态产物入窗
  （证伪「先截断后剔除」实现）；
- 四类口径各一个用例 + 中性趟既不延长也不打断的用例 + counts 缺键不被读成 0 的用例 +
  **`restart_reconciled` 且 `blocked_candidate_count > 0` 必须被判为 blocked**（回归用例，
  直接钉住 #2570 第一趟停摆产物的形状）；
- 抑制异质用例（1 条慢性 + 1 条新增 → 报）、抑制不越界用例；
- 有界/fail-closed：超 `MAX_EVIDENCE_BYTES`、软链、坏 JSON、缺 `started_at`、tracker schema 不符、
  systemctl 非零退出、**目录枚举触顶** → `probe_failed`，绝不 `ok`；
- 配置拒绝：范围外阈值、`scan_limit` 不足、证据根不是目录 → 退出码 2 且**未读任何产物**；
- 平价测试：谓词/常量与 `scheduler_evidence` 一致；
- unit 静态断言；receipt 可独立重推判级（每个信号的观测值与阈值并列）；
- must-remain-green：`tests/test_node22_refresh_timer_health.py`、
  `tests/test_scheduler_evidence_retention.py`、`tests/test_scheduler_evidence_decidability.py`；
- node-22 live receipt。

## Review focus

1. 「service 在跑」是否判成 `ActiveState in {active, activating}`（或 `SubState != dead`）——
   写成 `== "active"` 即缺陷（oneshot 在飞时是 `activating`）；verdict 3/5/7 是否都挂了这道闸门。
2. 中性是否由 `progress_guard` 缺席判定，而**不是**任何 status 允许表；
   counts 缺键是否走中性而非 0、也非整体 `probe_failed`。
3. 排序是否只依赖内容 `started_at`；`sorted(paths)` 直接进判级即缺陷；剔除是否在截断**之前**。
4. 抑制是否只作用于 verdict 11。
5. 是否违反 D4（任何 `from services...` / `from packages...` import 即缺陷）；
   平价测试是否钉住了 prefix/suffixes/上界**三个常量本身**，
   以及 `scripts/select_ci_tests.py` 是否真的会在改动上游谓词时选中它。
6. unit 是否零 EnvironmentFile、是否 unset 全套 PG 变量、是否**绝不**触碰 `nhms-compute-scheduler.*`。
7. `ok` 是否存在任何「证据缺失时回落」的可达路径。
