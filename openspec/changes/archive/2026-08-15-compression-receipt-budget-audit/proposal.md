# Proposal: compression-receipt-budget-audit

## Why

Issue #1351：压缩 receipt 是这条车道唯一持久化审计物，但三个 operator 可改的预算值
（`compress_timeout_ms` / `wrapper_wall_seconds` / `systemd_wall_seconds`，#1156/PR #1350
从模块常量提升为 env）不落 receipt。后果：追赶态（抬墙）与默认态 receipt 字节形状不可
区分，事故复盘失去"当次 tick 实际给了多少预算"的证据链，追赶窗口是否回滚无机器可查
凭据。`head_sha` 作为配置指纹的历史语义被静默削弱且无替代。这是审计完整性缺口，
不是运行时故障——现有 fail-closed 不变量全部有效。

## What Changes

1. **receipt 新增 `budget` 对象**（`{compress_timeout_ms, wrapper_wall_seconds,
   systemd_wall_seconds}`，all-or-nothing）：四个构造点统一落值——
   `build_receipt`(:661)、`build_refused_lock_receipt`(:852)、`build_failed_receipt`(:888)
   带 config 三点必落；`_replace_early_stale_with_failure`(:982) tombstone 是唯一合法
   budget 缺省形态（design D2）。
2. **schema_version bump 到 "2.1"**（design D1 裁决及理由）：enum 加 "2.1"，failed 条件式
   :104 的 `const "2.0"` 放宽为 `enum ["2.0","2.1"]`；1.0/2.0 禁止 `budget`，2.1 非
   tombstone 必须有 `budget`，tombstone（`failure.stage=="config"`）禁止 `budget`。
3. **消费侧只容忍不推导**：live_evidence 的 schema 由文件加载自动跟随；
   `EXPECTED_TIMEOUT_SECONDS=900`(:72) 冻结契约不动并加测试钉。
4. **(d) 交叉校验纳入本批**（design D4）：`config_from_args` 新增不变量——
   `compress_timeout_ms > 默认值` 且 `per_tick_bound > 1` 时 fail closed，错误文案指向
   runbook §4.5。现状两值零交叉检查，§4.5 散文规则全靠人守。
5. **runbook §4.5**：四值表(:1578-1586)与 Cleanup order(:1670-1694) 增补"从 receipt 的
   `budget` 确认当次生效预算 / 确认追赶窗口已回滚"。
6. **schema example** 升到 2.1 带 `budget`（反映 runner 实际产出形状）。

## Impact

- 受影响 specs：`hypertable-compression`（ADDED ×2：budget 审计要求、抬墙×bound 交叉约束）
- 受影响代码：`scripts/node27_timeseries_compression.py`（4 构造点 + `SCHEMA_VERSION` 常量
  + 2 处硬编码 "2.0" + `config_from_args` 新不变量）、
  `schemas/timeseries_compression_receipt.schema.json`、
  `schemas/examples/timeseries_compression_receipt.example.json`、
  `scripts/node27_timeseries_compression_live_evidence.py`（仅测试钉，断言语义零改动）、
  两个测试文件、runbook §4.5。
- 兄弟副本：无（schema 无其它车道引用，explorer 已确认）。
- node-27：**零实机改动**；证据为 scratch dry-run receipt（分支代码，D3 三步法）。
