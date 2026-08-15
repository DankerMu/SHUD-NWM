# Proposal: capture argv 闭世界文法 —— 关闭 exit-2 早退族 fail-open（issue #1266）

## Why

#1250 → #1259 → #1261 → #1263 逐层关掉了 capture argv 的 seam / 生产者身份 /
工具值 pin / help-and-exit-0 早退四族，但 exit-2 早退族从第一天起就写在声明的
作用域之外（`live_evidence.py:142`、`:1288-1291`）。在 f8b3e544 实测：`-xh`、
`--evidence-dirx`、尾随 `-- /tmp/whatever`、以及把完全正确的
`--evidence-dir <expected>` 整对挪到 `--` 之后——四种形态全部满足现有全部关卡
（位置锚、exactly-once、七值 pin + `--database` 动态 pin、关系式
`--evidence-dir`、四族 per-token 扫描），而被记录的生产者 `capture.py` 一进
`parse_args` 就 exit 2、零采集。ledger 侧只拒 `exit_code != 0` 的自称
（`:1562`），没有任何检查把"这条 argv 根本跑不到 exit 0"与 ledger 声称的
`exit_code=0` 对质。于是一份"十二份快照齐全、PASS"的 live-evidence bundle
可以由一条根本无法解析的命令行背书——取证信任面上的 fail-open。

最难看的形态是 `--` 之后的正确对：`_argv_option_values`（`:706-730`）是纯
位置无关 token 扫描、不认识 argparse 的 `--` 分隔符，关卡看见的绑定与真实
parser 看见的绑定完全脱钩（parser 视角 `--evidence-dir` 反而是缺失必填项）。

## What Changes

- **路线裁定：A（闭世界文法）**——issue 三路线中 B（导入生产者 parser 交叉
  对质）破坏本模块刻意维持的 non-derived-oracle 姿态，C（记录为永久边界）把
  锚定序列在最后一族上放弃。裁定理由与被否路线 tradeoff 见 design.md D1。
- `scripts/node27_timeseries_compression_live_evidence.py`：capture argv 关卡族
  新增两道文法关卡——第一道：裸 `--` 位置无关拒绝（flag 位或值位均算），前置到
  值等值关卡之前（防值位 `--` 配合停扫解除工具值 pin 的净退化）；第二道：
  `argv[2:]` 必须能被消费为"已注册生产 flag（精确全拼，`--flag value` 或
  `--flag=value` 两种形态）+ 非 `-` 开头值"的成对序列，未注册 token
  （`-xh`、`--evidence-dirx`）、`-` 开头值（`--schema-dump-host -xh`）、悬空
  flag 分别以可区分的措辞拒绝。flag 注册集以模块内 restated literal 形式落地
  （non-derived-oracle 姿态不变），由结构测试对 capture.py 真实 parser 钉漂移。
- `_argv_option_values` 停在第一个 `--`（与 argparse 绑定语义一致），消除
  "关卡看见绑定、parser 看不见"的定义分裂；第一道前置后本条为定义一致性 +
  纵深防御，不独立承重。
- `:142` 与 `:1288-1291` 的作用域边界注释同步改写（exit-2 族不再出作用域）。
- `tests/test_node27_timeseries_compression_live_evidence.py`：四种 exit-2 形态
  各一条独立拒绝用例 + 文法允许集结构钉 + 正向锁保持全绿。

## Impact

- Affected specs: `hypertable-compression`（1 条 ADDED requirement）
- Affected code: `scripts/node27_timeseries_compression_live_evidence.py`、
  `tests/test_node27_timeseries_compression_live_evidence.py`
- Not affected（non-goals）: `capture.py` parser 本身（不改生产者）；command 侧
  argv（已逐 token 精确等值）；supervisor 侧 capture 关卡（executor 合法跑
  hermetic stub plan，文法是 verifier 独有的取证裁定）；#1250/#1259/#1261/#1263
  已闭合四族的既有措辞与测试；#1240 `INVOCATION_ARGV` 死岛（另一条线）。
- node-27 实机：**零变更**——verifier 纯逻辑，无部署面、无 runbook 操作步骤变化。
