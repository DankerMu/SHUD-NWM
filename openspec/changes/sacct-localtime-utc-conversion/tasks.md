# Tasks — sacct-localtime-utc-conversion (#1117)

Anchors verified at master 2fe829cd (this session, by direct read):
`services/slurm_gateway/real_backend.py:1555-1565`
`_parse_slurm_datetime`（`fromisoformat(value.replace("Z",
"+00:00"))` 直接返回，naive 不换算）；调用点仅两处：`:1417`
started_at、`:1418` finished_at（terminal 态才取 End）；文件已
`from datetime import UTC, datetime, timedelta`（`:13`）；
`tests/test_real_slurm_gateway.py` 对解析出的 datetime **值**零断言
（grep `datetime(2026, 5, 8` 无命中；fake sacct stdout 全部为裸
ISO 字符串如 `2026-05-08T12:00:00`）；下游误标点
`services/orchestrator/scheduler_state_common.py:137-143`
`_ensure_utc`/`_format_utc`（aware 分支 `astimezone(UTC)` 正确，
修复后 gateway 值恒走该分支）。

Risk triage: fixture level **compact**（S-size；生产改动一处函数，
新增回归用例，零 schema/接口变化）。Risk pack selected:
**oracle-discrimination**（判别用例必须在修复前代码上红：naive
relabel 与 local→UTC 换算在 UTC 主机上不可区分，测试必须自带非
UTC 时区才有判别力）。Not selected: concurrency-lifecycle（无线程
语义变化——tzset 为进程级但用例内 try/finally 恢复且 pytest 串行）、
record-forensic、performance/UI/migration（n/a）。

ORACLE ROUTING（本 run 常设纪律：不使用 node-22）：issue
Verification 仅要求本地 pytest（journal + gateway_reconcile 两文件）
+ 本 fixture 追加 gateway 直接测试文件；缺陷判别不依赖 node-22
实机——TZ 判别用例通过 `TZ=Asia/Shanghai` + `time.tzset()` 在任意
POSIX 主机复现 CST 语义（本机 EDT、CI UTC 均可跑；tzset 为
POSIX-only，与本项目 macOS/Linux 双端约定一致）。修复前红证明由
orchestrator 以 backup-copy + `cmp` restore 方式在本地产出。

Must-preserve behavior:

- `_parse_slurm_datetime` 的哨兵语义不变：`""`/`"Unknown"`/
  `"None"`/`"N/A"` → None；无法解析**或无法换算**（`.astimezone`
  对边界年份可抛 `ValueError`）→ 同一 `SlurmParseError`——换算写在
  现有 `try` 内，`except ValueError` 同时覆盖解析与换算失败，不新增
  未捕获异常来源。
- aware 输入（"Z" 后缀）语义不变：仍为同一时刻（此前
  `replace("Z","+00:00")` 已产 aware，本修复只是统一
  `astimezone(UTC)`，同刻等值）。
- UTC 主机上全部既有测试计数不变（naive→astimezone 在 TZ=UTC 下
  与旧行为同值）；本机（EDT）计数同样不变——测得基线见 E2，
  既有用例对解析值零断言。
- Frozen：`services/slurm_gateway/mock_backend.py`、
  `services/orchestrator/**`、两个 journal/reconcile 测试文件的
  既有用例、`.github/workflows/ci.yml`。

Seams under test (upstream-declared, consumed not renegotiated):
sacct 按调用环境本地时区打印裸时间戳（Slurm 行为契约，issue 现象
+8h 即其兑现）；gateway 进程与 sacct 子进程共享 TZ 环境（subprocess
继承），故 `.astimezone(UTC)` 的"naive=进程本地时区"解释与 sacct
的打印时区精确对齐；`SlurmJobRecord` 的 datetime 字段无 tz
validator（`services/slurm_gateway/models.py:120`），aware 值可
直接入 record。

Non-goals: 修复前已写入 journal 的 +8h 历史行回填/校正
（follow-up issue）；`real_backend.py:442` `--starttime` 反向缺陷
（另行 issue，不得顺手修）；`_ensure_utc` 族去重/收紧（6+ 近重复
实现，既有熵债）；`reconcile.py` cohort 投影缺 `finished_at`
（另行 issue）；`SLURM_TIME_FORMAT` 环境级强制；mock backend
时间语义。

Minimal mergeable slice: 修复 + 三类回归用例一起（判别用例是
oracle，缺它则修复在 UTC CI 上不可证）。

## 1. 修复与回归

- [x] 1.1 `services/slurm_gateway/real_backend.py`
  `_parse_slurm_datetime`：`fromisoformat(...)` 结果统一
  `.astimezone(UTC)` 后返回（naive→按进程本地时区解释并换算；
  aware→换算）。**`.astimezone(UTC)` 必须写在现有 `try` 块内**，
  使换算期 `ValueError`（边界年份的 libc localtime 溢出）与解析
  失败同路归入 `SlurmParseError`。哨兵路径不动。
- [x] 1.2 `tests/test_real_slurm_gateway.py` 新增回归：
  (a) TZ 判别——**单一恢复机制**（不得用 `monkeypatch.setenv`，其
  teardown 只还原 `os.environ` 不重调 `tzset()`，libc 时区态会
  残留污染全 session）：手写 contextmanager/fixture——保存
  `os.environ.get("TZ")`，`os.environ["TZ"] = "Asia/Shanghai"` +
  `time.tzset()`，`finally` 中 restore-or-del + 再 `tzset()`；
  断言 `_parse_slurm_datetime("2026-07-12T08:00:00")
  == datetime(2026, 7, 12, 0, 0, tzinfo=UTC)`；
  (b) aware 直通——同一 TZ 下 `"2026-07-12T08:00:00Z"` 仍为
  08:00 UTC；
  (c) record 级——沿用文件现有 fake-sacct 构造喂裸 stdout，断言
  `finished_at`/`started_at`（及经 `started_at` 继承的
  `submitted_at`，`real_backend.py:1435`）`tzinfo` 非 None 且
  `utcoffset() == timedelta(0)`。用例需 `pytest.mark.skipif(not hasattr(time,
  "tzset"), ...)` 守 POSIX-only（(a)(b)；(c) 无需）。

## 2. Spec + validation

- [x] 2.1 Spec delta: ADDED requirement in
  `real-slurm-gateway-contract` —— sacct 裸时间戳 SHALL 按 gateway
  进程本地时区解释并换算 UTC 后才进 job record；3 scenarios
  （naive 换算；aware 直通；record 恒 aware-UTC）。
- [x] 2.2 `openspec validate sacct-localtime-utc-conversion --strict
  --no-interactive` green.

## Evidence Floor

- [ ] E1 修复前红证明（orchestrator backup-copy + `cmp` restore）：
  仅还原 `real_backend.py` 至 master 形态跑新增判别用例 (a) → red
  （naive 08:00 被 relabel 成 08:00Z ≠ 00:00Z）；record 级 (c) →
  red（tzinfo None）；恢复修复后同用例 green。`cmp` 确认还原
  字节一致。
- [ ] E2 计数对齐：`uv run pytest -q
  tests/test_real_slurm_gateway.py` 修复前 **223 passed** →
  修复后 **226 passed**（+3 新增回归）；`uv run pytest -q
  tests/test_file_orchestration_journal.py
  tests/test_gateway_reconcile.py`（issue Verification 指定）前后
  均 **686 passed**（零 diff 文件，基线本机 2fe829cd 实测）。
- [ ] E3 `uv run ruff check .` green; openspec strict green.
- [ ] E4 Surface check: `git diff master...HEAD --name-only` = 1
  生产文件 + 1 测试文件 + 本 openspec change；frozen 面零 diff。
- [ ] E5 CI `Unit Tests` green on PR head（UTC 主机上判别用例经
  TZ pin 仍有判别力——(a) 用例自带 Asia/Shanghai，不依赖 runner
  时区）。
