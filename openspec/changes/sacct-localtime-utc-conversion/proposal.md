# sacct 裸时间戳按主机本地时区换算 UTC（#1117）

## Why

node-22 journal 中 cohort job 的 `finished_at` 比真实 UTC 快 8 小时：
本地 CST 墙钟被直接加 "Z" 后缀写入。根因是两段式误标：

1. `services/slurm_gateway/real_backend.py:1555-1565`
   `_parse_slurm_datetime` 解析 `sacct` 的 `Start`/`End` 字段——sacct
   按**调用环境的本地时区**打印裸 ISO 时间（无 offset 无 Z），
   `fromisoformat` 返回 naive datetime，原样进
   `SlurmJobRecord.started_at/finished_at`（`:1417-1418`，唯一两个
   调用点）。
2. 下游 `_ensure_utc` 族（`scheduler_state_common.py:137-140` 等）对
   naive 值走 `.replace(tzinfo=UTC)`——**贴标签不换算**，
   `_format_utc` 再补 "Z"。CST 墙钟 + "Z" = 快 8 小时的假 UTC。

CI/开发机在 UTC 时该缺陷不可见（relabel 恰为 no-op），只在
非 UTC 主机（node-22 CST）上兑现——现有测试零覆盖。

## What Changes

单 choke point 修复：`_parse_slurm_datetime` 解析后统一
`.astimezone(UTC)` —— naive 值按**进程本地时区**解释后换算
（gateway 与其 sacct 子进程共享同一 TZ 环境，语义精确对齐），
aware 值（防御 "Z" 后缀输入）正常换算。返回值恒为 tz-aware UTC；
下游 `_ensure_utc` 走 aware 分支，天然正确。

回归测试（`tests/test_real_slurm_gateway.py`）：

1. TZ 判别单测：`TZ=Asia/Shanghai` + `time.tzset()` 下
   `_parse_slurm_datetime("2026-07-12T08:00:00")` ==
   `datetime(2026, 7, 12, 0, 0, tzinfo=UTC)`（try/finally 恢复 TZ +
   tzset，进程态零残留）。该用例在修复前的代码上必红
   （naive 08:00 ≠ aware 00:00Z）。
2. aware 直通：同一非 UTC TZ 下 `"2026-07-12T08:00:00Z"` 仍解析为
   08:00 UTC（换算不受本地时区影响）。
3. record 级：sacct stdout 喂入后 `finished_at`/`started_at` 为
   tz-aware UTC（`tzinfo is UTC` 判定，杜绝 naive 值再进 record）。

Out of scope（报告不修）：**修复前已写入 journal 的
`finished_at`/`started_at` 不做回填/校正**——修复后 node-22 journal
将同时含 +8h 历史行与正确新行，跨 pass supersede 判定对历史行仍错
（follow-up issue 跟踪，见 PR body）；同文件反向缺陷
`real_backend.py:442`（aware `_now()` 被 `strftime` 抹 offset 后作
`--starttime` 传 sacct，按本地时区解释——同根因反方向，另行
issue，本 change 不得顺手修以免破 E4 表面）；下游 `_ensure_utc` 族
对**内部生成** naive 值的 relabel 语义（本修复后 gateway 值恒
aware，不再命中该分支；族内 6+ 份近重复实现的去重是既有熵债）；
`reconcile.py` cohort 投影不传 `finished_at` 的数据完整性缺口
（另行 issue）；sacct 输出格式的环境级强制（`SLURM_TIME_FORMAT`，
Slurm 配置面）。

## Impact

- Affected code: `services/slurm_gateway/real_backend.py`
  （`_parse_slurm_datetime` 一处），`tests/test_real_slurm_gateway.py`
  （新增回归用例）。
- Affected specs: `real-slurm-gateway-contract`（1 ADDED requirement：
  sacct 裸时间戳按主机本地时区换算 UTC 后才进 job record）。
- Frozen surfaces（零 diff）：`services/slurm_gateway/mock_backend.py`、
  `services/orchestrator/**`（含全部 `_ensure_utc` 族）、
  `tests/test_file_orchestration_journal.py`、
  `tests/test_gateway_reconcile.py` 的既有用例。
