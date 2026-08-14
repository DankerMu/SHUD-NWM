# Design: fix-checkpoint-recurring-unit-predicate（#1255）

## 风险三角与 fixture level

- 风险：**误拒绝**（陈旧谓词烧掉授权窗口，本 issue 存在理由）×**误放行**（谓词改弱后真并发压缩溜进 mutation window——绝不允许方向）×**排障误导**（错误文本指错方向）。
- fixture level：**compact**（S 规模谓词修改；双平面对称 + 语义决策已由 maintainer 拍板）。

## 现状基线（fixture 撰写时核实）

- supervisor 断言：`scripts/node27_timeseries_compression_supervisor.py:1382-1393`（整体字典等值，`SupervisorError("checkpoint recurring compression unit is not inactive")`）。
- live-evidence 同形断言：`scripts/node27_timeseries_compression_live_evidence.py:965-976`（`EvidenceError("... is not canonically inactive")`）。
- `SYSTEMD_UNSET_TIMESTAMP = "n/a"`：`packages/common/node27_container_contract.py`（不动）。
- checkpoint 挂载链：`capture_checkpoint`（supervisor def `:1286`）→ `live_checkpoint`（`:1918-1934`）→ `execute_producer_state_machine`——mutation window 内每个 checkpoint 都执行。
- replay unit active-owner 断言（supervisor `:1394-1407`、live_evidence `:977+`）语义正确，**不动**。
- hermetic 现状：`tests/test_node27_timeseries_compression_supervisor.py` / `tests/test_node27_timeseries_compression_live_evidence.py` 固定喂 `"n/a"`（never-started 形态），真实"已 tick"形态零覆盖。

## 决策

### D1 — 语义化谓词（maintainer 裁定 2026-08-14；备选停-timer 路线未采用）

放行判定（两端完全一致，**四字段**）：

| 字段 | 判定 | 语义 |
|---|---|---|
| `FragmentPath` | `== /home/nwm/.config/systemd/user/nhms-node27-timeseries-compression.service` | unit 身份未被顶替 |
| `ActiveState` | `== "inactive"` | 当前无活动 |
| `SubState` | `== "dead"` | 无残留子状态（`failed` 也拒，专用文案见 D2） |
| `MainPID` | `== 0` | 无在跑主进程 |
| `InvocationID` | **不判定**，仅证据 | **实测被保留**（见下），与时间戳同属 boot 历史 |
| `ExecMainStartTimestamp` / `...Monotonic` | **不判定**，仅证据 | boot 历史，与"当前是否并发"正交 |

- **2026-08-14 node-27 实测**（fixture review F1 责成的前置探测，read-only `systemctl --user show`，timer 已于 08-13 12:25 CST tick）：`ActiveState=inactive SubState=dead MainPID=0 FragmentPath=<canonical>`，但 `InvocationID=0d8bd46e8f634e0296d8cbf49a938231`（**非空保留**）、`ExecMainStartTimestamp=Thu 2026-08-13 12:25:00 CST`。systemd 在 unit 保持 loaded（enabled timer 持续引用）期间保留 invocation/timestamp runtime state——`InvocationID=""` 判定与整体等值同样是"本 boot 从未启动"谓词，必须一并降级为证据字段。B1 固件必须用这组实测值。
- **活动事实由三字段闭合**：unit 为 `Type=oneshot` 且无 `RemainAfterExit`（`infra/systemd/nhms-node27-timeseries-compression.service`），运行中必为 `activating/start` + 非零 `MainPID`，结束必回 `inactive/dead` + `MainPID=0`；`ExecStopPost`/`deactivating`/`reloading` 几何均被 `ActiveState != "inactive"` 拦下。**点时谓词覆盖不了的"窗口中途 timer 触发"竞态另有两道护栏且本 change 不动**：窗口 journal 拒绝任何 recurring 激活（supervisor `:1445`、live_evidence `:1013-1015`），以及 checkpoint 的 DB 写权限 backend / relation lock 断言（supervisor `:1350-1353`）。
- 三个证据字段**必须仍出现在 checkpoint show 文档**（AC 明文：不得为了让断言通过而删字段）：`unit_show` 的字段采集集合不变；live-evidence 侧**新增** `_require_exact_keys(recurring, {七字段})` + 时间戳/InvocationID 类型校验（风格对齐 `live_evidence.py:938/:943` 的 `_require_*` 惯例）——现状整体等值就是键集的唯一钉子，删等值后若不新增键集钉，AC5 失去执行点（fixture review F2）。
- **不引入**时间序断言（时钟域换算新增脆弱面，三字段已闭合活动事实，YAGNI）。
- 谓词收敛为共享 helper 或两端字面同构均可，但**字段集合与判定必须逐字对称**（产/验双平面；issue-1069 缺陷类"双平面独立硬编码、修一半烂一半"在仓内有专门 regression lock 先例 `tests/test_node27_timeseries_compression_live_evidence.py:4095-4130`，本 change 以 B6 同型钉住）。
- never-started（`"n/a"` + `InvocationID=""`）形态在新谓词下同样放行——表述为"该形态**重新生成**的 bundle 仍通过"（=B3）；不存在"旧 bundle 向后兼容"一说（bundle 的 `verifier_head_sha` 钉死 repo HEAD，改动前 bundle 本就无法被改动后 verifier 校验——fixture review F7）。

### D2 — 错误信息可区分（AC 第 3 条）

- 谓词失败时报**偏离字段清单**（field=observed vs expected）；**`SubState="failed"` 单独出文案**——failed 既非并发活动也非身份漂移，是本 unit 的常态化风险（runbook §4.5：per-chunk timeout 墙可致 tick 失败，failed/failed 持续到下一 tick 或 `reset-failed`），文案须点名 `systemctl --user reset-failed` 为处置手段，否则在 failed 几何下重犯 AC3 的误导错（fixture review F4）。其余偏离用"recurring unit shows current activity or unexpected identity"方向措辞。
- 两端错误前缀保留各自惯例（`SupervisorError` / `EvidenceError` + label）。

### D3 — 测试形态（B 锚）

- B1：**已 tick 形态**（D1 实测值：真时间戳 + `InvocationID=0d8bd46e...` 非空 + 四字段 canonical inactive）→ 两端均放行（本 issue 的核心红→绿；红证=修复前代码对同输入必炸）。
- B2：**单字段偏离参数化**（范式同 `tests/test_node27_timeseries_compression_live_evidence.py:4400-4411` replay 块）——对四个被判定字段逐一单独偏离（`ActiveState=activating`、`SubState=failed`、`MainPID≠0`、`FragmentPath` 漂移），两端 fail-closed 且错误文本含该偏离字段；`failed` 分支断言专用文案（含 reset-failed）。单字段构造保证"删任一字段判定"的变异必被对应参数杀死（fixture review F5——多字段同时偏离的固件杀不死单字段删除）。
- B3：never-started（`"n/a"` + `InvocationID=""`）形态 → 仍放行（既有用例保留）。
- B4：show 文档缺时间戳/InvocationID 字段或类型错 → live-evidence **新增键集钉**（`_require_exact_keys` + 类型校验）拒绝。
- B6：**双平面一致性锁**（issue-1069 缺陷类同型钉）：用真实 supervisor `unit_show` 采集逻辑产出的已 tick show 文档喂 verifier 必须接受；翻转任一被判定字段必须两端同时拒绝。
- 红证：B1 修复前红；B2 每个参数对应单字段判定删除的变异红。

## Invariant Matrix（本 change 触及）

- 不变式：mutation window 检查点对 recurring unit 的放行判定只依赖**当前活动事实**，不依赖 boot 历史；时间戳证据完整性不因判定收窄而丢失。
- 兄弟面自查：仓内同模式整体等值断言仅此两处（`grep -n 'ExecMainStartTimestamp.*SYSTEMD_UNSET' scripts/ packages/` 复核）；replay 侧显式拒 `"n/a"` 属不同语义（active owner 必须有真时间戳），不在本不变式面内。

## Evidence Mapping

AC1↔B1；AC2↔B2；AC3↔D2+B2（failed 专用文案钉）；AC4↔B1+B3；AC5↔B4；AC6↔Non-Goals（备选路线未采用，如实记录）；AC7↔tasks 3.1/3.2；AC8↔tasks 3.4。B6 为双平面一致性附加钉（issue-1069 缺陷类），不对应单条 AC。
