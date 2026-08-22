# Tasks — harden-node27-retention-lock-contention

行号锚定 `master` @ `a2f59136`。**每一处行号在动手前按符号名 grep 复核。**

## 1. 配置：`lock_timeout` 旋钮（`scripts/node27_timeseries_retention.py`）

- [x] 1.1 在 `_QUERY_TIMEOUT_MS` / `_DROP_TIMEOUT_MS`（`:113-114`）旁新增
      `_DEFAULT_LOCK_TIMEOUT_MS = 240_000`。注释逐字带上 design D3 的三条依据
      （观测成功等待下界 ≈182 s；成本不对称；240/300 之间的 60 s 归因间隙），
      并写明**这是对 issue #1664 建议的 2–5 s 的记录在案偏离**。
      **不得**写成 `_DROP_TIMEOUT_MS` 的派生式。
- [x] 1.2 `RetentionConfig` 冻结 dataclass 末尾新增
      `lock_timeout_ms: int = _DEFAULT_LOCK_TIMEOUT_MS`（放末尾是因为前面已有
      带默认值的 `archive_gate`；仓内**不存在**位置参数构造点，生产唯一构造
      `:348-356` 全关键字，测试走 `_build_config(**kwargs)`
      `tests/test_node27_timeseries_retention.py:176-186`）。
- [x] 1.3 `config_from_args`（`:306`）解析
      `NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS`，并**必须**把结果显式传进
      `RetentionConfig(...)`（`:348-356`）。**不能**复用 `_optional_positive_int`
      （无上界）——新增带上界的校验：`0 < value < _DROP_TIMEOUT_MS`，
      越界 / 非整数抛 `RetentionConfigError`（→ `RETENTION_CONFIG_INVALID`，DB 零接触）；
      **空串按仓内惯例（`:252-255`）视同缺失 → 默认值**。错误文本点名两端边界。
- [x] 1.4 `_default_drop_chunk`（`:670`）在 `SET statement_timeout`（`:685`）**之后、
      `drop_chunks` 之前**执行 `SET lock_timeout`，取值来自 `config.lock_timeout_ms`，
      **不得**硬编码。**不动**枚举/测量路径的 `_QUERY_TIMEOUT_MS`。

## 2. 锁类失败的自证前缀（同文件 `:950-971`）

- [x] 2.1 新增 `LOCK_CONTENTION_PGCODES: frozenset[str] = frozenset({"55P03", "40P01"})`。
      注释写明 design D4：分类段插在 `<hypertable_schema>.<chunk_name>:` **之后**，
      wire code + chunk 名前缀 byte-unchanged（#1213 的 operator-grep 契约）。
- [x] 2.2 在 `:954` 的 `except Exception as error:` 里用 `getattr(error, "pgcode", None)`
      做 **default-deny** 判据：仅当值精确属于该集合才插入 `lock-contention(<pgcode>): `。
      **禁止**任何消息文本匹配（`in` / `startswith` / 正则）。
      `pgcode` 缺失或不在集合内 → 逐字保持
      `f"{CODE}:{schema}.{chunk}: {redacted}"`。
- [x] 2.3 `_redact_error_text` 不动；分类段是纯 ASCII 常量，不经过驱动文本。

## 3. 每块 drop 计时（design D3a，stderr，无 schema 变更）

- [x] 3.1 drop 循环（`:950-971`）对每块记录 `time.monotonic()` 起止，
      成功与失败**两条路径都**向 stderr 输出一行诊断，含 chunk 的
      `qualified_name` 与毫秒级 elapsed。沿用 `:655` 既有诊断的 JSON 行形状。
- [x] 3.2 **不动** `schemas/timeseries_retention_receipt.schema.json`；
      `dropped_chunks[]` 的键集保持不变。
- [x] 3.3 计时诊断不经过 `_redact_error_text`（纯数字 + chunk 名，无凭据面），
      但**失败路径**那条诊断只输出计时，错误文本仍只走既有的 `refusal_reason` 通道。

## 4. 单测（`tests/test_node27_timeseries_retention.py`）

按 design D8 矩阵。T1–T7 注入式，不需要真 DB。

- [x] 4.1 T1/T2：**合成异常类**（真 psycopg2 异常的 `pgcode` 只读且客户端构造为 `None`，
      赋值抛 `AttributeError`）带 `pgcode = "55P03"` / `"40P01"` →
      `refusal_reason` 以 `RETENTION_DROP_FAILED:hydro.chk-a: lock-contention(55P03): ` 起头。
      同时断言 `psycopg2.errorcodes.LOCK_NOT_AVAILABLE == "55P03"` 与
      `DEADLOCK_DETECTED == "40P01"`，把常量钉在驱动自己的表上。
- [x] 4.2 T3：`pgcode="57014"` → **不含** `lock-contention`，与既有 `:968` 的形状逐字一致。
- [x] 4.3 T4：无 `pgcode` 属性的裸 `RuntimeError` → 不含 `lock-contention`。
- [x] 4.4 T5：`LOCK_TIMEOUT_MS` = `0` / `-1` / `300000` / `300001` / `"abc"`
      → 全部 `RetentionConfigError`，且断言**零 DB 调用**。
- [x] 4.5 T5b：`LOCK_TIMEOUT_MS = ""` → 视同缺失，取默认值。
- [x] 4.6 T6：未设 env → `config.lock_timeout_ms == 240_000`。
- [x] 4.7 **T6b（反空转）**：env 取**非默认**值 `"2000"` → `config.lock_timeout_ms == 2000`。
      没有这条，`config_from_args` 校验了 env 却忘记传进 dataclass 时 T5/T6 会双双保持绿色。
- [x] 4.8 T6c：断言 `0 < _DEFAULT_LOCK_TIMEOUT_MS < _DROP_TIMEOUT_MS`（spec 要求默认值
      本身满足同一严格边界）。
- [x] 4.9 **T7（反空转，承重）**：用既有 `_install_fake_drop_psycopg2` /
      `_DropProbe`（`tests/test_node27_timeseries_retention.py:1176-1233`，
      现有 `:1235` / `:1288` 已这样驱动真 `_default_drop_chunk`）直测，
      config 的 `lock_timeout_ms` 取**非默认**值 `1234`。断言 `probe.executed`
      里 `SET lock_timeout = 1234` 与 `SET statement_timeout` **都**出现，
      且**都在** `SELECT drop_chunks(` 之前。
      断言对象必须是**实际传给 `cursor.execute` 的 SQL 文本**，不是 config 字段
      ——否则 SQL 里硬编码 `240_000` 也能通过。
- [x] 4.10 T8（告警 wrapper 行为）：新建测试，用 fake sendmail 捕获 stdin。
      断言 (a) 正文含传入的失败单元名；(b) `NHMS_ALERT_EMAIL_TO` 未设时 `exit 0`；
      (c) sendmail 路径不存在时 `exit 0`。
      env-pin 形状照抄 `tests/test_node27_frontier_stall_alert.py:41-47`。
- [x] 4.11 把 `NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS` 加进 H13 catalogue 断言
      `test_env_example_lists_all_h13_keys`（`tests/test_node27_timeseries_retention.py:2063-2076`），
      使 example ↔ 代码的耦合被钉住。

## 5. 失败可见性（systemd + wrapper）

- [x] 5.1 新增 `infra/systemd/nhms-node27-unit-failure-alert@.service`（design D6）：
      `Type=oneshot`、`WorkingDirectory=/home/nwm/NWM`、
      `EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env`（复用既有告警地址）、
      `ExecStart=/home/nwm/NWM/scripts/node27_unit_failure_alert_once.sh %i`、
      `TimeoutStartSec=120`。
- [x] 5.2 新增 `scripts/node27_unit_failure_alert_once.sh`（`set -euo pipefail`，`chmod +x`）：
      `$1` 为失败单元名（**带 `.service` 后缀**，因 `%n` 展开为全名），
      `journalctl --user -u "$1" -n 30 --no-pager` 拼正文，经
      `${NHMS_FRONTIER_SENDMAIL:-/usr/sbin/sendmail} -t -i` 投递，
      收件人 `NHMS_ALERT_EMAIL_TO`、发件人 `NHMS_ALERT_EMAIL_FROM`。
      **软失败一律 `exit 0`**（地址未配置 / sendmail 不存在），
      不得再叠一个 failed 单元。**不做**去重/状态文件。
- [x] 5.3 `infra/systemd/nhms-node27-timeseries-retention.service` 的 `[Unit]` 段加
      `OnFailure=nhms-node27-unit-failure-alert@%n.service`。**只改这一个单元**。
- [x] 5.4 `infra/env/node27-timeseries-retention.example` 增
      `#NODE27_TIMESERIES_RETENTION_LOCK_TIMEOUT_MS=240000`，注释写明上界断言与 D3 依据。

## 6. 文档

- [x] 6.1 runbook §8.2（`docs/runbooks/tier-node27-timeseries-storage.md:2581-2594` 邻域）：
      新 env、`lock-contention(<pgcode>)` 前缀读法、**D2 的三条承重边界**
      （不消除 40P01 / per-acquisition 非累计 / 不增加回收量 → **exit 1 不会归零**），
      以及 D2 的未验证项（`lock_timeout` 是否覆盖 `dimension_slice` 的 tuple lock 等待，
      本 change 无离线证据）。
- [x] 6.2 §8.6 新增「被锁失败的判据与升级阈值」：单趟 `lock-contention(*)` refused =
      **可接受，次日幂等自愈，不需人工介入**；**连续 ≥3 天**或**同周 ≥4 趟** = 升级。
      抄录 #1664 取证结论：对手方是 `OnUnitActiveSec=10min` 的 autopipe 摄入；
      冲突面是 FK（`db/migrations/000005_met.sql:100,102`）＋ catalog（`dimension_slice`）；
      压缩越线假设被 08-19 天然对照推翻。
- [x] 6.3 §8.6 记 `OnFailure=` 告警入口（template unit 名、复用的邮件通道、部署步骤）。
- [x] 6.4 **点名修正两处会因分类前缀而失准的句子**：
      `docs/runbooks/tier-node27-timeseries-storage.md:2779-2781`（把后缀写成
      `:<hypertable_schema>.<chunk_name>: <error>`）与 `:3027-3029`
      （「chunk 名之后的 cause text 是 redacted 的」）——锁类失败中间多了分类段。
- [x] 6.5 §8.6 的 per-chunk 计时说明（`:3019-3021` 此前声称存在、实际不存在的
      instrumentation，本 change 补上；写明输出到 stderr → `retention.log`）。

## 7. 部署（node-27，user scope；**非自动**）

- [ ] 7.1 把 `nhms-node27-unit-failure-alert@.service` 与更新后的
      `nhms-node27-timeseries-retention.service` 安装进 `~/.config/systemd/user/`
      并 `systemctl --user daemon-reload`。
      （`git pull --ff-only` 只更新 `ExecStart` 背后的 Python，**不**装单元。）
- [ ] 7.2 实机漂移核对：`systemctl --user show
      nhms-node27-timeseries-retention.service -p OnFailure` 非空且指向实例名。

## Evidence Floor

- [x] E1 本地 `uv run pytest -q tests/test_node27_timeseries_retention.py` 全绿，
      且收集用例数 **> 0**（防 `no tests ran` 的零断言冒烟）。
- [x] E2 本地 `uv run ruff check .` 零告警。
- [x] E3 本地 `openspec validate harden-node27-retention-lock-contention --strict --no-interactive`。
- [ ] E4 node-27 `git pull --ff-only` 后
      `uv run pytest -q tests/test_node27_timeseries_retention.py`。
- [ ] E5 node-27 单元语法：`bash -n scripts/node27_unit_failure_alert_once.sh` +
      对**实例名**（而非裸模板）做 `systemd-analyze --user verify
      'nhms-node27-unit-failure-alert@smoke.service'` 或 `systemctl --user cat` 该实例。
- [ ] E5.5 node-27 通道 smoke（**不碰 DB**）：
      `systemctl --user start nhms-node27-unit-failure-alert@smoke.service`，
      确认单元 `Result=success` 且邮件通道未报错。
- [ ] E6 node-27 **enforce 实机 receipt**：merge + 7.1 部署后，读**第一个实际删了 chunk
      的排程趟**（13:15 CST）的 `retention-*.json`，断言
      `outcome == "enforced"` **且** `len(dropped_chunks) >= 1`
      （只断言 outcome 会被「无候选块 → 空转 enforced」空转满足，design D7）。
      **不得手动 `systemctl --user start` retention**——那会真删生产 chunk。
      合并时若尚未到点，记为**已路由的延后证据**并在 #1664 留追踪。
- [ ] E7 `#1664` 评论抄录终态口径：D3 对 issue 推荐值 2–5 s 的偏离及其 receipt 依据、
      D2 的三条边界、告警入口、E6 的延后状态。
