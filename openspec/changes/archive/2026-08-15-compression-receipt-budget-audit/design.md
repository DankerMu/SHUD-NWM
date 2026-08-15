# Design: compression-receipt-budget-audit

## 风险分级（fixture level: compact）

Issue #1351 无 `Suggested fixture level` 字段（早于 0.16.0 契约）；triage 定 compact：
持久化审计物 schema 变更 + fail-closed 新不变量，错了影响历史证据可验证性与 live tick
准入，但无数据破坏面。风险轴：
1. **schema 兼容性**：历史 1.0/2.0 receipt 必须在新 schema 下继续 valid（live_evidence
   会用当前 schema 文件校验 bundle 里的历史 receipt）。
2. **oracle 完整性**：`EXPECTED_TIMEOUT_SECONDS=900` 是冻结归档契约，改成从 receipt
   推导等于取消断言（issue 明令禁止）。
3. **fail-closed 方向**：(d) 只允许新增拒绝，不得放宽任何既有拒绝。
4. **tombstone 旁路**：`_replace_early_stale_with_failure` 不走 `publish_receipt`，
   直接 `atomic_write_bytes_no_follow`(:1014)——schema 变更必须覆盖这条旁路的产出。

## 裁决

### D1：schema_version bump 到 "2.1"（bump，理由如下）

additive optional 通常不 bump，但本案 bump 恰好承担审计语义：**"budget 缺失"的歧义
只有版本号能消除**——`2.1` + 无 `budget` ⇒ 只能是 config tombstone（schema 强制）；
`2.0` + 无 `budget` ⇒ 旧代码产物。不 bump 则"新代码 tombstone"与"旧代码任意 receipt"
永远不可区分，审计缺口只补一半。且本 schema `additionalProperties:false` + enum 闭集，
加字段本来就是显式 schema 变更，bump 无额外破坏面。
落点：schema :13 enum `["1.0","2.0"]` → `["1.0","2.0","2.1"]`；failed 条件式 :104
`{"const":"2.0"}` → `{"enum":["2.0","2.1"]}`（历史 2.0 failed receipt 保持 valid）；
**:93 head_sha 条件式同步放宽** `{"const":"2.0"}` → `{"enum":["2.0","2.1"]}`——漏改此处
则 2.1 非 failed receipt 可合法缺 `head_sha`，即 bump 反向削弱 provenance 钉（fixture
复审 P1-1）；runner 四个发射点全部升 "2.1"（`SCHEMA_VERSION` 常量喂 :832/:866，硬编码
"2.0" 在 :899/:992 两处——**全部四点必须同版本**，禁止混发）。

### D2：tombstone 是唯一合法 budget 缺省形态（不改签名）

`_replace_early_stale_with_failure` 仅在 `config_from_args` 抛错时触发（唯一调用点
:1083-1088，`stage="config"`），即**此路径上从未存在过合法 config**——补发预算值就是
其 docstring 明言拒绝的 "config lies"。判别子 `failure.stage=="config"` 为 tombstone
独占（`build_failed_receipt` 其余调用点 stage ∈ {display_watermark, freeze_head,
acquire_lock, publish_refused_lock, runner, publish_receipt}，已 grep 验证）。
schema 编码（三段，全部显式；tombstone 豁免用**双判别子合取**——`failure.stage`
是自由字符串、runner 不自校验产出，单靠约定会让未来 `build_failed_receipt(stage="config")`
落入 schema-invalid 陷阱，fixture 复审 P2-2）：
- `schema_version ∈ {1.0, 2.0}` → `{"not": {"required": ["budget"]}}`（沿用 :84-86 模式）。
- `schema_version == 2.1` 且非 tombstone 豁免形状 → `required: ["budget"]`。
- tombstone 豁免形状 = `outcome=="failed"` **且** `failure.stage=="config"` **且**
  `per_tick_bound` 缺失（结构性 config-absence 标记：`build_failed_receipt` 恒带
  `per_tick_bound`，tombstone 恒不带）→ 禁止 `budget`。带 `per_tick_bound` 的
  `stage=="config"` 假想形状不落豁免、正常要求 `budget`，不会 schema-invalid。
- 配套测试钉：runner 源码中 `stage="config"` 仅存在于 tombstone 调用点（grep 断言）。
schema 编辑手段仅限 `if/then/else` + `required` + `not`（消费侧 :1785 用 Draft7Validator
而 schema 声明 2020-12——禁用 `dependentRequired` 等 2020-12 独占关键字，防校验器分裂，
P3-2），并加一条显式走 `Draft7Validator` 的反例测试。
all-or-nothing 由 `budget` 自身定义承担：`additionalProperties:false` + required 三字段
全列 + 三字段类型/下界与 config 解析约束一致（`compress_timeout_ms ≥ 1` integer 等）。

### D3：消费侧零推导，两级契约分开对待（fixture 复审 P1-2 修正）

live_evidence 有**两级**校验：(1) `_load_receipt`(:1783-1786) 用文件加载的 schema 做
结构校验——schema 更新自动生效，无需改代码；(2) `verify_bundle` 的语义门
:3564-3567 **硬钉 `schema_version == "2.0"`**——它校验的是 #1069 冻结历史 bundle 里的
dry/enforce receipt，与 `EXPECTED_TIMEOUT_SECONDS`/`EXPECTED_LAG_SECONDS`/`EXPECTED_BOUND`
(:66-73) 同族，是冻结归档契约。**裁决：:3565-3566 的 "2.0" 钉保持不动**——历史 bundle
里的 receipt 永远是 2.0，放宽到 2.1 就是把冻结基线改成跟随现状的自证（issue 边界明令
禁止的方向）。`EXPECTED_TIMEOUT_SECONDS=900`(:72) 同样保持硬编码字面量。
新增测试钉两枚：900 字面量 + `verify_bundle` 的 "2.0" 语义钉（断言源码字面量，防好心
放宽）。带 budget 的 2.1 receipt 的消费侧证据**只走 `_load_receipt` 结构校验路径**，
不进 `verify_bundle`。

### D4：纳入 (d) 交叉校验

`config_from_args` 新增第三条不变量腿：`compress_timeout_ms > _DEFAULT_COMPRESS_TIMEOUT_MS(3_600_000)`
且 `per_tick_bound > 1` → `CompressionConfigError`，文案指向 runbook §4.5（抬墙必须
`PER_TICK_BOUND=1`）。等于默认或更低不触发（收紧安全）；只加拒绝不减拒绝。
理由同批：与既有两条 budget-chain 腿同函数同模式，S 体量；receipt `budget` 让"追赶态
bound=1 + 抬墙"从此有据可查，两者互证。
**残差显式记录（P2-4）**：本腿只守 §4.5 追赶窗口（抬 timeout 的显式操作），**不守**
默认 timeout 下 bound=4 遇 ≥2 river chunk 的撞墙险——config 时刻看不见 chunk 尺寸，
该险仍按 runbook §4(:400-415) 的 operator 检测权威处置。spec delta 措辞同幅收窄。
**阈值搭在可重调常量上（P2-3）**：`_DEFAULT_COMPRESS_TIMEOUT_MS` 曾被 #1352 重调过；
未来**调低**默认会让 live 组合（显式 3600000 + bound=4——若 live 显式设了 timeout）
fail closed 而常量基准的测试仍绿。对策：测试 (e) 增加一条**解析 env 模板字面量**
（timeout+bound 从 `infra/env/node27-timeseries-compression.example` 读出）断言该组合过
`config_from_args`——模板即 live 的部署源，模板组合过 = live 组合过。
**pre-merge 安全前提**：node-27 live env 实测未设三个预算 key（timeout 走代码默认），
live tick 不会被新腿拒绝——已实测（2026-08-14，key 名单：DATABASE_URL/REPO_ROOT/
LAG_SECONDS/PER_TICK_BOUND/RECEIPT_PATH/LOCK_PATH，无预算三键），列入 tasks 4.1 证据。

### D5：schema example 升 2.1 带 budget

example 是工程师复制模板，应反映 runner 实际产出形状（refused_lock 带 config，budget
必在）。CI 泛化 schema/example 配对检查自动覆盖。

## Must-preserve（seams under test）

- 历史 1.0/2.0 receipt 在新 schema 下 valid（含 `docs/runbooks/receipts/.../timeseries-compression/`
  下实际历史 receipt + 新增 2.0-无-budget 正例）；1.0/2.0 带 budget 为 violation。
- **既有 schema 测试语义保持、字面需适配（P2-1/P3-1，非零修改）**：:1178-1184 的
  1.0 降级测试在 example 升 2.1+budget 后必须同步 pop `budget`（语义——1.0 兼容性——
  不变）；:473 与 :1146 两处硬编码 `"2.0"` 断言升 `"2.1"`。除此三处外 schema/builder
  既有测试零修改。
- `EXPECTED_TIMEOUT_SECONDS = 900` 字面量冻结 + `verify_bundle` `"2.0"` 语义钉
  :3564-3567 冻结（D3 双测试钉）。
- budget-chain 既有两腿不变量测试群 :126-384 语义零改动（新腿是追加）。
- env 模板 `infra/env/node27-timeseries-compression.example`：~~字节不动~~ **冻结解除
  （round-1 C3 amendment；理由经 round-2 N3 更正）**——钉保护的**不是空集**：第一个 pin
  测试钉住多条 comment substring（含纯注释行），第二个以唯一性钉赋值行；comment 编辑
  完全可能破钉。**钉的权威清单以测试源码为准**（`tests/test_node27_timeseries_compression.py`
  的两个 env-example pin 测试），本记录不复述行号——此段枚举曾两次抄错（round-2 N3、
  round-5），手抄 grep 可得事实是漂移源，按 round-ceiling 终局裁定移除（见
  `.workplans/pr-1388/review/terminal-decision.md`）。解除许可站在操作规则上而非"注释免钉"错误论述上：**赋值行与全部既有
  钉住 substring 字节不动**，pin 测试必须原样通过（round-1 修复实际以保留
  `PER_TICK_BOUND=1` 字面为约束完成，即活证）。解除动机不变：leg 3 落地后模板只列
  两腿 + "either leg"、把 bound=1 说成 hint，是本 PR 自己制造的矛盾（模板即部署源），
  允许 comment-only 刷新（补 leg 3、"either"→"any"、hint 改"抬 timeout 时强制"）。
- tombstone "no config lies" 性质：payload 仍不含任何 config 派生值。
- `_emit_stderr_diagnostic` payload keys 不变（本案不走备选的 journald 方案）。

## Non-Goals

- 压缩运行时行为、既有 env 解析规则、不变量语义（#1156/PR #1350 定稿）。
- capability spec :705-730 超时默认值 stale（已立案 #1386，本变更不碰该 requirement）。
  **含 :713 "the receipt schema identical to the previous hardcoded configuration" 句**——
  本变更让它因第二个新原因（defaults unset 时 receipt 带 budget、版本 2.1）失真（P2-5）；
  显式移交：merge 后在 #1386 追评注明该句需一并改写，本 delta 不 MODIFIED 该 requirement
  以免与 #1386 的 840000/900/940 修正相互踩踏。
- live-evidence bundle 自身 schema 扩展。
- `per_tick_bound` 目标值（#1237 已定稿）。
- schema enum 死值 `outcome:"refused_config"`（runner 零发射点，grep 已证）——范围外
  报告，不在本变更清理。

## 证据映射

| AC | 证据 |
|---|---|
| 四构造点 budget 全有/全无且等于生效值 | 单测：默认 env / 非默认 env（如 1800000/1900/1940 + bound=1）→ receipt.budget 逐字段断言；tombstone 无 budget |
| schema 接受带 budget、拒半截、1.0/2.0 拒 budget、2.1 非 tombstone 缺 budget 拒 | jsonschema 正反用例矩阵 |
| bump 裁决入 fixture | 本文件 D1 |
| live_evidence 容忍带 budget receipt + 双冻结钉 | `_load_receipt` 结构校验路径最小正例（**不进 verify_bundle**）+ D3 双测试钉（900 字面量、:3565 "2.0" 钉） |
| runbook §4.5 receipt 确认路径 | 四值表 + Cleanup order 增补，markdownlint |
| (d) fail closed | 红绿证：抬墙+bound=4 拒（文案含 §4.5），抬墙+bound=1 过，默认+bound=4 过；**加一条从 env 模板解析字面量的组合过证（P2-3）**；反例中显式走 Draft7Validator 一条（P3-2） |
| 红证的承载条 | **正例红证：新 runner 产 2.1+budget receipt 在旧 schema 下 FAIL**（P3-4——半截/1.0+budget 反例在旧 schema 因 additionalProperties 也拒，属错因红，不承载） |
| node-27 | live env 非敏感 grep 证 (d) 安全（已实测，见 D4）+ 分支代码 scratch dry-run receipt 含 budget（D3 三步法，`--receipt-path` **与 `--lock-path`（或等效 env 覆写）双 scratch 覆写**防撞生产锁/timer tick，生产 receipt mtime 不动）+ **scratch receipt 用 jsonschema 对分支 schema 校验通过**（unit 测试 stub 了 chunk 列表，这是唯一真实数据的 runner-output ⊨ schema 证据，P3-3） |
