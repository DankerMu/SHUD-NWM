# Design: capture argv 闭世界文法（issue #1266）

风险三角：取证信任面（trust surface）× 单模块 S 规模 × 强测试脚手架在位。
fixture level: **compact**（issue 无 pipeline 字段，Phase 0.5 自行 triage；与
#1250/#1259/#1261/#1263 同族同规模）。

Must-preserve：
- `test_default_plan_author_capture_argvs_pass_the_whole_capture_gate_stack`
  （十二 kind 正向锁，含带 `--schema-dump-*` 的 `schema_dump_list`）零误伤。
- 既有四族拒绝措辞（seam / help / anchored 缩写 / pinned 缩写）及其测试逐字不变。
- 模块 non-derived-oracle 姿态（`:120-126` 记录的 "Restated LITERALS on
  purpose, NOT imported"）。
- supervisor 侧行为完全不变（hermetic 执行兼容性，#1250 executor 裁定）。

Seams under test：`_validate_*` 中 capture argv 关卡族的插入点；
`_argv_option_values` 的绑定语义；capture.py `_parser()` 的注册 flag 面
（只读取证，不修改）。

## D1 — 路线裁定：A（闭世界文法），B/C 否

**A 成立的前提**：合法 capture argv 形态封闭且上界确定。唯一合法作者
`plan_author.build_run_plan`（`plan_author:317-344`）产出
`[capture_python, capture_script, "--kind", kind, *10 对公共选项,
*(0|1×2 对 schema-dump)]`——只有精确全拼长选项、只有 pair 形态（无 `=` 拼写、
无 `--` 分隔符、无位置参数），且全部值 token 为 kind 名 / 库名 / 40 位 hex /
绝对路径，无一以 `-` 开头。文法收紧到"注册 flag + 非 `-` 开头值"成对序列不会
误伤任何 plan_author 产出（正向锁在 AC 中钉死）。

**B 否**：`capture._parser().parse_known_args()` 交叉判可解析性意味着验证器
导入生产者模块。本模块的独立 oracle 姿态是刻意且成文的（`:120-126`：literal
故意 restate 而非 import，plan_author 变更由 drift-guard 测试变红而非静默跟随）。
B 使 capture.py 的一次 parser 变更静默改变验证器期望——恰好是该姿态要防的事。
另：M 规模 > A 的 S 规模，收益相同。

**C 否**：零代码，但"PASS"语义要在规格/runbook 降级为"只覆盖 token 集合形状，
不覆盖可解析性"。#1250 以来四步锚定序列的全部意义就是把取证 PASS 收敛为结构
事实；在最后一族上以文档让步，性价比劣于 S 规模的 A。

## D2 — 文法定义与关卡位置

**第一道：`--` 分隔符位置无关拒绝，前置到等值关卡（`:1183`）之前**。
`argv[2:]` 中出现任何裸 token `--`（精确等值，flag 位或值位均算）即拒，独立
措辞，打印 token 及其 argv 下标（下标使尾随形态与挪对形态在措辞上可区分）。
位置无关是硬要求：若只在 flag 位拒，`--` 落在值位（如
`--schema-dump-host -- --psql /tmp/stub`）会被文法当值吞掉，同时 D3 的停扫
让 `--psql` 值 pin 只看见 `--` 前的绑定——#1261 的工具值 pin 被打穿，构成
相对 master 的净退化（master 的位置无关扫描能看见第二个 `--psql` 绑定并拒）。
前置安全性论据：裸 `--` 触发不到既有任何一族拒绝（seam 分支要求
`len(base)>=3`，help 分支要求 `-h` 前缀或 `len>=3` 的 `--help` 前缀，两缩写
分支同样要求 `len(base)>=3`），plan_author 也从不产出 `--`，故前置不吞既有
措辞、不误伤生产 plan。

**第二道：成对文法，在既有四族 per-token 扫描（`:1259-1309`）之后**。
`argv[2:]`（此时已保证无 `--`）从左到右消费：
- token 是注册集内 flag 的精确全拼 → `--flag=value` 形态自足成对；
  `--flag` 形态消费下一 token 为值；
- **值 token 不得以 `-` 开头，`=` 内联值同样适用**（`--schema-dump-host=-xh`
  对真实 parser 虽是 exit 0 合法，但 plan_author 从不产出 `=` 拼写内联 `-`
  开头值——闭世界取严，规则在两种拼写上定义一致，实现与测试不留分叉）
  → 违者拒绝，值位措辞（打印 flag 与该值 token）。
  这关掉 exit-2 族最后的存活成员：`--schema-dump-host -xh` 对真实 parser 是
  `expected one argument`（exit 2），而 schema-dump 两选项的值 deliberately
  不 pin（`:137-143`），无此规则则文法视其为合法对。安全性：plan_author 全部
  值为 kind 名 / 库名 / 40 位 hex / 绝对路径（`repo`/`root`/`schema_dump_host`
  在 `plan_author.py:109-111` 强制 absolute），无一以 `-` 开头；
- 其它任何 token（`-xh` 单破折号簇、`--evidence-dirx` 未注册长选项）→ 拒绝，
  未注册 token 措辞（打印 offending token）；
- 尾部悬空 flag（`--flag` 后无值 token）→ 拒绝（未成对），可并入未注册类
  措辞或独立措辞，实现自选，但必须打印 token。

第二道放在四族扫描之后的理由不变：seam/help/缩写 token 同时也是"未注册
token"，文法先跑会吞掉四族的既有专用措辞、打破措辞钉测试；文法只对幸存
token 面收口。

**注册集**：模块内新 restated literal 元组（13 个生产 flag：`--kind`
`--database` `--mutation-head-sha` `--repo` `--container` `--evidence-dir`
`--psql` `--systemctl` `--docker` `--journalctl` `--git`
`--schema-dump-host` `--schema-dump-container`）。**不含** `--self-test-*`
seam flag——seam 扫描已在文法之前把它们拒掉，注册集不给 seam 任何合法地位。

**四形态归宿**（AC 措辞可区分性的落点）：
| 形态 | 拒绝关卡 | 措辞类 |
|---|---|---|
| `-xh` | 文法第二道 | 未注册 token（打印 `-xh`） |
| `--evidence-dirx` | 文法第二道 | 未注册 token（打印 `--evidence-dirx`） |
| 尾随 `-- /tmp/whatever` | 文法第一道（前置） | `--` 分隔符类（含下标） |
| 正确 `--evidence-dir` 对在 `--` 之后 | 文法第一道（前置） | `--` 分隔符类（含下标，与上行下标不同） |

## D3 — `_argv_option_values` 停在 `--`（定义一致性，非唯一防线）

扫描循环遇到裸 token `--`（精确等值，非前缀）即 break。与 argparse 语义一致：
`--` 之后一切都是位置参数，不构成选项绑定。这使"绑定"在文法、等值关卡、真实
parser 三处只有一个定义。**注意**：D2 第一道前置后，任何含 `--` 的 argv 在
等值关卡运行前已被拒，本条对 verdict 不再独立承重——它是定义一致性 + 纵深
防御（防未来有人移动关卡顺序时静默重开缺口）。该函数现仅被 capture argv
关卡调用（`:1183` `:1188` `:1203` `:1225`）；实现前用 grep 复核无其它调用方，
若有则逐个论证语义兼容并记入偏离记录。

## D4 — 测试义务

1. 四形态各一条独立拒绝用例（同一 baseline argv 上做单点变异；`--` 尾随形态与
   `--evidence-dir`-after-`--` 形态失败机理不同，必须分开），并断言四条拒绝
   措辞互不串台（否定式断言，与 #1263 既有测试同纪律）。
2. 值位规则用例：`--schema-dump-host -xh`（未 pin 值选项 + `-` 开头值）被值位
   措辞拒绝；值位 `--` 用例：`--schema-dump-host -- --psql /tmp/stub` 被第一道
   `--` 措辞拒绝（P1 回归锁，钉住"值位 `--` 不解除工具值 pin"）。
3. 文法允许集结构钉：对 capture.py 真实 `_parser()` 取注册 option strings，
   断言 `生产 flag 注册集(文法 literal) ∪ {"-h", "--help"} ∪ seam flags ==
   parser 全量注册面`（argparse 自动注册 `-h` 与 `--help` 两个 string，实测
   全量 17 个）——capture.py 加/删 flag 时变红，姿态与既有
   `test_capture_cli_registers_no_business_flag_in_the_help_rejection_domain`
   同构（测试可导入生产者取证，验证器本体不导入）。
4. 正向锁保持全绿（AC 明列）。
5. `_argv_option_values` 的 `--` 停扫行为直接单测（`--` 前有绑定→计入；
   `--` 后有绑定→不计入）。

## 边界注释改写

`:142`（"it does not assert parser-viability of the whole argv"）与
`:1288-1291`（`-xh` 出作用域声明）改写为指向新文法关卡；不得留下"本模块不裁定
整条 argv 可解析性"的过期声明。ledger 侧 `exit_code` 检查（`:1562`）不动——
文法在 plan 侧把不可解析 argv 拒死后，"ledger 声称 exit 0 但 argv 必然 exit 2"
的自相矛盾已无法进入 PASS 路径（plan/ledger argv 等值绑定既有）。
