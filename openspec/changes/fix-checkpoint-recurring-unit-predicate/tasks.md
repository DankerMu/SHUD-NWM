# Tasks: fix-checkpoint-recurring-unit-predicate（#1255）

## 0. 前置实测（fixture review F1 责成；已完成）

- [x] 0.1 node-27 read-only 采已 tick unit 七字段（2026-08-14）：`InvocationID=0d8bd46e8f634e0296d8cbf49a938231` **非空保留**、`ExecMainStartTimestamp=Thu 2026-08-13 12:25:00 CST`、四字段 canonical inactive——推翻 InvocationID 入判定集的原案，D1 收敛为四字段

## 1. 实现

- [x] 1.1 supervisor `capture_checkpoint` recurring-unit 断言改 D1 四字段语义化谓词（InvocationID 与两时间戳降证据字段），错误文本改偏离字段清单；`SubState=failed` 专用文案（点名 `systemctl --user reset-failed`，D2）
- [x] 1.2 live-evidence 同形断言同步（字段集合与判定逐字对称）
- [x] 1.2b live-evidence **新增** `recurring` 键集钉：`_require_exact_keys(recurring, {七字段})` + 时间戳/InvocationID 存在性与类型校验（风格对齐 `live_evidence.py:939/:944`）——现状整体等值即唯一键集钉，删等值必须补此钉（F2）
- [x] 1.3 `unit_show`/show 文档字段采集集合不变（七字段照采照记）
- [x] 1.4 更新两处在线陈旧引用（**只改叙述文字，不改 `SYSTEMD_UNSET_TIMESTAMP` 常量值**）：`docs/runbooks/tier-node27-timeseries-storage.md:2151-2164`（"whole-dict shape … tracked as its own issue" 段收口为已修复叙述）与 `packages/common/node27_container_contract.py:86-94` 注释（"one member of a whole-dict equality" + 漂移行号）（F3）。实现期发现并同型收口第三处：`scripts/node27_external_contract_snapshot.py:98-107` 同一已证伪叙述 + 漂移锚（comment-only，PR 偏离记录 2）

## 2. 测试（tests/test_node27_timeseries_compression_supervisor.py + tests/test_node27_timeseries_compression_live_evidence.py）

- [x] 2.1 B1：已 tick 形态（0.1 实测值全套，含非空 InvocationID）→ 两端放行；红证=修复前代码对同输入必炸
- [x] 2.2 B2：**单字段偏离参数化**（范式 `live_evidence.py` 测试 `:4400-4411` replay 块）——`ActiveState=activating` / `SubState=failed` / `MainPID≠0` / `FragmentPath` 漂移逐一单独偏离 → 两端 fail-closed、错误文本含该字段；failed 分支断言专用文案；变异证明：删任一单字段判定 → 对应参数红（F5）
- [x] 2.3 B3：never-started（`"n/a"` + `InvocationID=""`）形态既有用例保留通过（重新生成语义，非"旧 bundle 兼容"——F7）
- [x] 2.4 B4：show 文档缺时间戳/InvocationID 字段或类型错 → 键集钉拒绝（证据字段不可删钉）
- [x] 2.5 B6：双平面一致性锁（issue-1069 缺陷类同型，先例 `live_evidence.py` 测试 `:4095-4130`）——真实 supervisor `unit_show` 产出的已 tick show 文档喂 verifier 必须接受；翻转任一被判定字段两端同拒

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_node27_timeseries_compression_supervisor.py tests/test_node27_timeseries_compression_live_evidence.py`
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate fix-checkpoint-recurring-unit-predicate --strict --no-interactive`
- [x] 3.4 node-27 read-only receipt：timer 已 tick 当天采七字段，新谓词判定通过 + 旧整体等值判定失败（修复前后对照；0.1 已采的这组值即素材，merge 前用最终代码重放一次），记录进 `.workplans/issue-1255/` 与 PR 评论
