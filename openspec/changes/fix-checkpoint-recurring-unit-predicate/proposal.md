# Proposal: fix-checkpoint-recurring-unit-predicate（#1255）

## Why

supervisor 与 live-evidence 对 recurring unit `nhms-node27-timeseries-compression.service` 的 checkpoint 断言是**整体字典等值**，把 `ExecMainStartTimestamp="n/a"`/`...Monotonic=0`（"本 boot 从未启动过"）钉进了放行判定。该断言写于 timer 尚未 enable 的一次性窗口（#1069，`71125485`），彼时"从未启动"恰好等价于"无并发压缩"；step 6 之后 `OnCalendar=*-*-* 04:25:00 UTC` daily timer 常态运行，前提被自己上线的动作打破——timer tick 过的任何一天，下一次授权 mutation window 的**第一个 checkpoint 必然误 abort**（fail-closed 方向，不损数据，但烧掉整次 arm 准备），且错误信息 `"is not (canonically) inactive"` 把排障引向"有并发压缩在跑"的错误方向。hermetic 测试恒喂 `"n/a"`，CI 恒绿，只有实机证伪（#1089 fixture review 2026-08-02 实测 `ExecMainStartTimestamp=Sun 2026-08-02 12:25:00 CST`）。

## What Changes

1. **语义化谓词**（maintainer 裁定 2026-08-14，AskUserQuestion 记录；备选"窗口内停 timer"路线未采用）：两端 recurring-unit checkpoint 改为断言**四字段**——`FragmentPath` 等值 + `ActiveState="inactive"` + `SubState="dead"` + `MainPID=0`；`InvocationID` 与 `ExecMainStartTimestamp`/`...Monotonic` 一并降级为**证据记录字段**（2026-08-14 node-27 实测：unit 保持 loaded 时 systemd 在 inactive 后**保留**非空 InvocationID——`InvocationID=""` 判定与整体等值同为"本 boot 从未启动"谓词，fixture review F1 实锤）。三个证据字段仍必须出现在 checkpoint show 文档中（live-evidence 新增键集钉执行，见 design D1），不再参与放行判定。
2. **产/验双平面同步**：`scripts/node27_timeseries_compression_supervisor.py`（producer checkpoint）与 `scripts/node27_timeseries_compression_live_evidence.py`（verifier 同形断言）是同一份 fact 的两个平面，必须同一谓词、同步落地。
3. **错误信息可区分**：谓词失败信息点名偏离字段；不再把 boot 历史表述为并发活动。
4. hermetic 测试补"本 boot 已 tick、当前 inactive/dead"真实形态；never-started（`"n/a"`）形态既有用例保留（新谓词天然兼容）。

## Non-Goals

- #1089 快照探测面（外部契约漂移，独立面）；`SYSTEMD_UNSET_TIMESTAMP` 常量**值**本身（对 never-started 渲染仍正确；其**注释叙述**引用整体等值断言已陈旧，随 tasks 1.4 改文字）；replay unit 的 active-owner 断言（语义正确，`"n/a"` 显式拒绝必要，不动）；压缩容量目标（#1237）与 self-test seam 可见性（#1250）；runbook mutation-window **步骤**（备选路线未采用，步骤与 cleanup 证据要求不变——但 `tier-node27-timeseries-storage.md:2151-2164` 对旧断言的**在线叙述**随 tasks 1.4 收口，那是引用文字不是流程变更）。

## 待实测项（live receipt 期）

node-27 read-only receipt：在 timer 已 tick 过的当天探测 checkpoint 谓词输入（`systemctl --user show` 五字段 + 两时间戳），证明新谓词判定通过、旧谓词判定失败（正好构成修复前后的对照）。
