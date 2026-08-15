# Tasks: capture argv 闭世界文法（issue #1266）

## 1. 实现（scripts/node27_timeseries_compression_live_evidence.py）

- [x] 1.1 新增注册 flag literal 元组（13 个生产 flag，见 design D2；不含 seam）
      与两道文法关卡：第一道 `--` 位置无关拒绝（打印 token+argv 下标）**前置到
      等值关卡 `:1183` 之前**；第二道成对文法（注册 flag 精确全拼 + 值 token
      不以 `-` 开头 + 悬空 flag 拒绝）置于既有四族 per-token 扫描之后
- [x] 1.2 `_argv_option_values` 遇裸 `--` token（精确等值）停扫；先 grep 复核
      调用方仅限 capture argv 关卡族，否则逐个论证并记偏离
- [x] 1.3 改写 `:142` 与 `:1288-1291` 两处作用域边界注释——exit-2 早退族不再
      出作用域，指向新文法关卡；不留过期"不裁定可解析性"声明

## 2. 测试（tests/test_node27_timeseries_compression_live_evidence.py）

- [x] 2.1 四种 exit-2 形态独立拒绝用例：`-xh`、`--evidence-dirx`、尾随
      `-- /tmp/whatever`、正确 `--evidence-dir` 对置于 `--` 之后（后两者均落
      第一道 `--` 措辞、以 argv 下标区分），并否定式断言四条措辞互不串台
- [x] 2.2 值位规则用例：`--schema-dump-host -xh` 被值位措辞拒；P1 回归锁
      `--schema-dump-host -- --psql /tmp/stub` 被第一道 `--` 措辞拒（钉住
      "值位 `--` 不解除工具值 pin"，design D4-2）
- [x] 2.3 文法允许集结构钉：literal 元组 ∪ {"-h","--help"} ∪ seam flags ==
      capture.py 真实 `_parser()` 注册面（17 个 option strings，design D4-3）
- [x] 2.4 `_argv_option_values` 的 `--` 停扫单测（前计入/后不计入）
- [x] 2.5 正向锁 `test_default_plan_author_capture_argvs_pass_the_whole_capture_gate_stack`
      保持全绿（十二 kind 零误伤，含 `schema_dump_list`）
- [x] 2.6 既有四族拒绝措辞测试逐字不变（不允许因文法关卡插入而改既有断言）

## 3. 本地验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_node27_timeseries_compression_live_evidence.py` 全绿
- [x] 3.2 `uv run ruff check .` 通过
- [x] 3.3 `openspec validate capture-argv-closed-world-grammar --strict --no-interactive` 通过

## 4. 交付记录

- [x] 4.1 PR body 记录路线裁定（A）与被否路线 tradeoff（issue AC-1；引 design D1）
- [x] 4.2 node-27 零实机变更声明（verifier 纯逻辑，无部署面）
