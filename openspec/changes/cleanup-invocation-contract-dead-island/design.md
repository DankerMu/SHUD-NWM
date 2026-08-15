# Design: INVOCATION_ARGV 死岛删除（issue #1240）

fixture level: **compact**（S 规模、两文件、强测试脚手架；风险轴是"删除时误伤
活契约"而非新逻辑）。issue Readiness 为 needs-decision——裁决即 D1，裁完
implementation-ready。

前置复核（2026-08-15，@5625728c，#1233 教训的例行项）：岛屿仍在（漂移到
`:377-445`）、仍死（非定义命中仅测试 fixture 两处 + 变异重盖两处）；#1250-#1266
五个后续 PR 均未新增消费者。

Must-preserve：
- `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`（`:3260`）
  在且过、负向语义不变。
- `_validate_exact_command_argv` / `_concrete_argv` 活 argv 契约 diff 零触碰。
- schema `database_audit_proof` 两处 `{"const": false}`（issue AC-5）。
- #1351 的 `EXPECTED_TIMEOUT_SECONDS` 冻结钉及其测试——它属消费契约面，与
  `_TIMEOUT_PREFIX`（bundle 侧死岛）无关，删岛不得波及。
- 436 测试基线：计数只允许因 fixture 精简而变化，PR body 必须说明每一条增减。

## D1 — 方案裁决:A(删岛),B 否

**A**：KISS/YAGNI 默认。死岛的两个"内容"都已有活的权威版本：启动 argv 契约的
单一来源是 supervisor ledger lane（`command["argv"]` + `_validate_exact_command_argv`
+ `_concrete_argv`），launcher wall 的活配置属 #1156/#1352 的 runner env。删除
即消除漂移源与伪 oracle 面。

**B 否，三条独立理由**：
1. 与已记录裁定冲突：#1261 在 capture argv 锚定注释里明文裁定 interpreter/
   launcher 身份是 residual TRUST ROOT，闭合路线是 producer-side attestation
   （"NOT a verifier gate"）。B 恰是把它做成 verifier gate 的路线，等于推翻
   #1250-#1266 六个 PR 沿用的边界划分，而 issue 本身没有提出需要这条信任边界的
   新证据。
2. 成本不对称：schema + bundle_author + supervisor + capture 全链 + node-27
   实机重跑取证（M+），对一个"生产 bundle 里根本不存在该文件形态"的槽位。
3. 接线前还得先修平手抄本漂移（相对路径/缺参/占位符），做的第一件事就是
   重写死岛——证明死岛现值为负。

**不允许"删一半留一半"**（issue AC-1）：三符号 + fixture 字段一次清干净。

## D2 — 删除与精简的精确边界

script 侧删 `:377-445`（`INVOCATION_ARGV` 整 Mapping、`_TIMEOUT_PREFIX` 含其
frozen-wall 注释 :404-409、`_invocation_execution_identity` **整函数**——返回
dict 闭合在 :445）。删后
`grep -rn "INVOCATION_ARGV\|_invocation_execution_identity\|_TIMEOUT_PREFIX" --include="*.py" .`
必须 0 命中（issue AC-2）。

test 侧 `_invocation()`（`:247-264`）精简后保留字段 = 让 invocation JSON 文件
仍是合法可写的 ref 目标所需的最小集合。verifier 对内容零 parse，故最小形状由
**测试自身可读性**决定，不由 verifier 决定；**应保留** exit_code /
timeout_seconds（legacy 哨兵 :3268/:3270 对这两个键做"腐蚀既有值"式变异，
删掉会让变异退化成"新建键"、叙事变弱）及 kind/started_at/finished_at 叙事
字段；但 **argv、launcher_argv、resolved_* 五字段、artifact_bindings 必须
清零**（issue AC-3 的 grep 清单，词边界形式见 D3）。`receipt_sha256` **只**在
`_invocation()` fixture 与两处变异重盖行（`:2796`、`:3282`）内删除——该名在
prearm manifest 是活生产字段（`scripts/node27_timeseries_compression_prearm.py`
:486/:499 + 对应测试断言），**不进 grep 清单、不得全仓清零**。`bindings`
形参与 5 处调用点实参（:792/:828/:841/:856/:866）一并删除。

legacy 测试（`:3260`）继续塞"看起来像旧契约"的字段（exit_code=1、
timeout_seconds=901 等**行内字面量**）证明内容不是真相——行内字面量不落
AC-3 的 grep 清单即可；若其现文本含清单字段名，改用等价的其它假字段，负向
断言（qualifies_task_4_5 is True）逐字不变。

## D3 — 删除安全性论据（review 重点核对面）

- 三符号全仓唯一、无兄弟副本（issue 与本 fixture 双次 grep）。
- `_TIMEOUT_PREFIX` 注释里的 "frozen archival-evidence contract" 说的是死岛
  自己的 `launcher_argv` 期望值——随岛亡；#1351 的 timeout 冻结钉在
  live_evidence 消费契约测试（`EXPECTED_TIMEOUT_SECONDS`），符号不同、面不同，
  实现前 grep 确认后者零触碰。
- schema `timeseries_compression_live_evidence.schema.json` 对 invocation 槽只
  约束 ref 形状（`artifact_ref` = `{path,sha256,bytes}` + additionalProperties
  false），删除不需 schema 变更；AC-5 的两处 const false 用 diff 自证不动。
- **AC-3 grep 必须词边界**（`grep -rnE "\b(...)\b"`）：裸子串 `resolved_script`
  会命中无关活代码 `tests/test_select_ci_tests.py` 的 `_resolved_script_modules`
  （3 处，CI 选择器 helper，不得为凑 grep 改名）。词边界形式今日实测只命中
  死岛 :424/:436，删后归零。issue 原 AC 文本的裸子串形式按此据实调整并在
  PR body 记录。

## D4 — 测试义务

1. AC-2/AC-3 两条 grep 以测试或 PR body 记录的命令输出兑现（0 命中）。
2. legacy 测试保留且过；`uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py`
   全绿，计数变化在 PR body 逐条说明。
3. `uv run ruff check .`（删除后清 F401 等）。
4. 正向锁 `test_default_plan_author_capture_argvs_pass_the_whole_capture_gate_stack`
   等 #1250-#1266 全部既有钉不动（纯删除不该碰它们，diff 自证）。
