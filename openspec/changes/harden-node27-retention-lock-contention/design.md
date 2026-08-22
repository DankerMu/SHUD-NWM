# Design — harden-node27-retention-lock-contention

行号锚定 `master` @ `a2f59136`。**每一处行号在动手前按符号名 grep 复核。**

## 风险三角（Phase 0.5 triage）

| 轴 | 判定 | 依据 |
|---|---|---|
| Blast radius | **高** | 改的是生产 `drop_chunks` 路径——唯一有权删数据的代码。 |
| Reversibility | 中 | 代码可回滚；已删 chunk 不可回滚（但本 change 不放宽任何删除条件）。 |
| Observability | 低→本 change 提升 | 失败此前只落 journal，无 `OnFailure=`，靠人肉撞见。 |

**Fixture level: `expanded`**（issue 建议 S→M；取证后确认 M）。理由：production-automation
＋ live DB 删除路径行为变更 ＋ systemd/env 契约 ＋ runbook ＋ 实机 receipt AC，
与 #1660 同量级。

选中的 risk pack：`production-automation`、`live-db-behavior-change`、
`contract-byte-identity`（`WIRE_CODES` / `refusal_reason` 前缀 grep 契约）。
未选：`frontend`、`schema-migration`（不动 DDL）、`auth`。

## D1 — 为什么加 `lock_timeout` 而不是调排期

取证的天然对照实验（#1664 评论第 3 节）：

| 日期 | 压缩 (UTC) | retention (UTC) | 重叠 | 结果 |
|---|---|---|---|---|
| 08-18 | 未运行 | 05:15:03–05:20:28 | 无 | refused（40P01 vs ingest） |
| 08-19 | 04:25:00–05:28:53 | 05:15:00–05:18:02 | **完全重叠** | **enforced ✅** |
| 08-21 | 04:25:00–04:27:08 | 05:15:00–05:20:00 | 无 | refused（57014） |

假设方向完全相反。真正的对手方是 `~/.config/systemd/user/nhms-node27-autopipe.timer`
（`OnUnitActiveSec=10min`，常驻），排期上无处可避。

## D2 — `lock_timeout` 覆盖面的诚实边界（**承重句，不得在实现或文档中被上调**）

`lock_timeout` 给单次锁获取加上界，使锁等待以 `55P03` 自证，而**不是**与「删得慢」
混叠的 `57014`。它买到的是**有界 + 归因确定性**，不是「不再失败」。三条边界：

1. **不消除 `40P01`**：deadlock detector 一旦检出环就 abort 一方，与 `lock_timeout` 无关。
   08-18 那种 shape 仍会以 deadlock 现身。
2. **是 per-acquisition，不是累计**：一趟 drop 可能依次获取多把锁，每把都等 < 240 s
   却仍把 300 s 的 `statement_timeout` 耗尽，最终仍以 `57014` 收场。
   因此措辞是「大幅提高归因确定性」，**不是「保证」**。
3. **不增加回收量**：`lock_timeout` 不会让任何一块本来删不掉的 chunk 变成删得掉。
   它只改变失败的**形状**（更早、有署名），不改变失败的**频率**。

推论（写进 runbook）：**exit 1 不会归零。** 终态是「失败有界、失败可见、次日幂等自愈」
（drop 重入无副作用）。任何声称「加了 `lock_timeout` 就不再失败」的表述都是错的。

**未验证项（诚实登记）**：08-21 的等待对象是 TimescaleDB catalog 的一行 tuple lock
（`while locking tuple (0,18) in relation "dimension_slice"`）。PostgreSQL 的
`lock_timeout` 是否覆盖这类 tuple lock 等待，本 change **没有**离线证据，
只能由下一次真实争用事件回答。runbook 必须与 D2 并列写明这一点。

## D3 — 默认值 240_000 ms 的依据（**对 issue 推荐值的记录在案偏离**）

issue「解决思路」建议 2 s–5 s。本 change 取 `_DEFAULT_LOCK_TIMEOUT_MS = 240_000`。
三条依据，其中 **(1) 是「无法解释的耗时」，不是实测锁等待**：

**(1) 一趟成功的 08-19 花了 ≈182 s 墙钟，而它 0.638 GB 的删除工作量解释不了这个数。**
三份 enforced receipt 逐字对照（`/home/nwm/node27-timeseries-retention-logs/`，均为 2 块）：

| 日期 | dropped | freed_bytes 合计 | wrapper `elapsed_sec` |
|---|---|---|---|
| 08-17 | 2 | **11.130 GB** | 23 s |
| 08-19 | 2 | **0.638 GB** | **182 s** |
| 08-20 | 2 | **11.070 GB** | 1 s |

11 GB 能在 1–23 s 删完，0.638 GB 却花了 182 s。
（08-19 正是压缩全程重叠的那天，且 PG 在同窗口有一个 `write=450.259 s` 的长 checkpoint。）

**但这个 182 s 归因不到 drop 会话**：它是 wrapper 的 `elapsed_sec`，括住整个 Python
调用——watermark（5 s）+ 枚举（60 s）+ **每块**测量（60 s），三者都没有锁上界，
光这些在 2 块的一趟里就能凑出 ~190 s。因此 receipt **既不能**证明「某一次加锁等了
182 s」，**也不能**反过来证伪 issue 的 2–5 s 建议：若等待发生在测量阶段，
5 s 的 *drop* 锁预算根本碰不到它。
**取 240 s 的决定因此落在下面 (2)(3) 两条上，它们与这个分解无关。**
D3a 的每块计时正是为了把这条推断换成实测。

**(2) 成本不对称。** 取小 = 在**唯一有删除权**的 lane 上把成功变失败；
取大 = 只损失 off-peak 的墙钟延迟——单元 `TimeoutStartSec=0`，wrapper 无 `timeout(1)`，
下一趟在 24 h 后，没有任何东西在等它。风险不对称，决定取大。

**(3) 240 与 300 之间留 60 s 归因间隙**，使 `55P03`（撞锁上界）与 `57014`
（撞语句上界）在 receipt 里不含糊。

字面常量 `240_000`，**不写成 `0.8 * _DROP_TIMEOUT_MS` 之类的派生式**——
两个上界是独立的运维旋钮，派生会把它们焊死。

**这个数字是可调优的，前提是有数据**——因此 D3a 的每块计时是本 change 的一部分，
而不是「顺手」。

## D3a — 每块 drop 计时（stderr，无 schema 变更）

drop 循环 `:950-971` 目前**不产出任何 per-chunk 时间**：全模块的 `print` 只有
`:655` 的测量失败告警与 `main()` 的诊断。因此 D3 的取值今天无法用实测收敛，
运维也无法判断一次 sub-timeout 的慢趟是「等锁」还是「删得慢」。

本 change 在 drop 循环里给每块加一行 stderr 诊断（chunk 名 + 毫秒级 elapsed，
成功与失败路径都出），**不动 receipt schema**（`schemas/timeseries_retention_receipt.schema.json`
的 `dropped_chunks[]` 形状不变）。纯计时，无凭据面，不经过 `_redact_error_text`。

副产品：`docs/runbooks/tier-node27-timeseries-storage.md:3019-3021` 已经指示运维去看
「per-chunk drop timings printed to stderr」——**这个 instrumentation 此前并不存在**。
补上之后文档由假变真，该 drift 无需另开 issue。

## D4 — 分类前缀的位置与格式（byte-identity 约束）

`scripts/node27_timeseries_retention.py:954-968` 的 #1213 注释是硬约束：
wire code + `<hypertable_schema>.<chunk_name>` 前缀 **byte-unchanged**，operator grep 依赖它。
因此分类段只能插在 chunk 名之后、驱动文本之前：

```
RETENTION_DROP_FAILED:met._hyper_1_70_chunk: lock-contention(40P01): deadlock detected\nDETAIL: ...
RETENTION_DROP_FAILED:hydro._hyper_3_32_chunk: lock-contention(55P03): canceling statement due to lock timeout
```

未分类（非锁类）失败**逐字保持今天的形状**，不插任何段：

```
RETENTION_DROP_FAILED:<schema>.<chunk>: <redacted driver text>
```

判据是 **default-deny**：只有 `getattr(error, "pgcode", None)` 精确等于 `"55P03"` 或
`"40P01"` 才分类。**禁止**消息文本匹配（`in` / `startswith` / 正则）——
`_redact_error_text` 的输出不是稳定契约。`pgcode` 缺失（非 psycopg2 异常、
测试替身）一律走未分类分支。

`_redact_error_text` 本身不动：分类段是纯 ASCII 常量，不经过驱动文本，
不可能携带凭据。

## D5 — 为什么不加新 wire code

`WIRE_CODES` 的注释（`:125-142`）列出 4 个 byte-identical 副本，其中一个是
**尚未归档**的 `openspec/changes/tier-node27-timeseries-storage/design.md`（#855），
且有 forward-walk 测试读取它（`timeseries-db-retention` spec 的
「Byte-identity guard tests MUST survive openspec change archival」要求）。
新增 code 还会牵动 receipt schema 的 `oneOf`。AC 字面只要求
「`refusal_reason` 归到锁而非泛超时」——前缀满足，且 #1660
（`concurrent-replace: ` 前缀）是同仓刚落地的先例。

## D6 — `OnFailure=` 的形状

**Template unit**，本 PR 只挂 retention 一个消费者：

```ini
# infra/systemd/nhms-node27-unit-failure-alert@.service
[Unit]
Description=NHMS node-27 unit failure alert for %i
[Service]
Type=oneshot
ExecStart=/home/nwm/NWM/scripts/node27_unit_failure_alert_once.sh %i
```

retention 单元加一行 `OnFailure=nhms-node27-unit-failure-alert@%n.service`。

wrapper 是**哑脚本**（无 DB、无状态机、无 Python）：取 `journalctl --user -u "$1" -n 30`
拼进正文，经 `$NHMS_FRONTIER_SENDMAIL -t -i` 投递，收件人取 `NHMS_ALERT_EMAIL_TO`。
逐字复用 frontier lane 已被证明活着的通道（每 30 min 实跑）——那条通道是
**认证 SMTP shim**（`scripts/node27_frontier_smtp_sendmail.py`），不是本机 MTA：
node-27 的 postfix 被 null-route，收下（exit 0）之后异步退信
（2026-08-13 实测 `dsn=5.0.0 status=bounced`）。因此 `NHMS_FRONTIER_SENDMAIL`
**不给默认值**，未配置即软退出，而不是「记 SENT、其实没投出去」。
`NHMS_ALERT_EMAIL_FROM` 同理不给派生默认值：shim 会拒绝非认证账号的 From（exit 64），
派生默认只会把一次配置疏漏变成第二个 failed 单元。
`NHMS_ALERT_EMAIL_TO` / `NHMS_ALERT_EMAIL_FROM` 含 CR/LF 时**拒发**（不 strip），
与 frontier lane 的 `_header_safe` 同口径；消息头补齐 `Date` / `MIME-Version` /
`Content-Type: text/plain; charset="utf-8"`——shim 与 `send_message` 都不会替它注入，
而 journal 行可能非 ASCII。

**不做**去重/抑制/状态文件：这是每天最多一次的排程失败，重复告警不是问题；
状态机是 frontier lane 的复杂度，不该在这里复制。

**`%n` 展开注意**：`OnFailure=nhms-node27-unit-failure-alert@%n.service` 里 `%n` 是
**全名**（含 `.service`），故实例名是 `...@nhms-node27-timeseries-retention.service.service`，
wrapper 收到的 `$1` 带 `.service` 后缀。这是 `status-email@%n` 的标准形状，
但校核必须针对**实例名**，裸模板 `systemd-analyze verify` 抓不到实例化错误。

**部署不是自动的**：node-27 的单元是 **user-scope 手工安装**（`~/.config/systemd/user/`，
Phase 0 取证 §4 已实测）。`git pull --ff-only` 只更新 `ExecStart` 背后的 Python，
**既不会**装上新 template unit，**也不会**把 `OnFailure=` 写进活单元。
因此必须有一步显式安装 + `daemon-reload` + `systemctl --user show ... -p OnFailure` 实机核对。

告警脚本自身失败**不得**污染 retention 的退出码——systemd `OnFailure=` 天然如此
（是独立单元），无需额外处理，但 wrapper 必须 `exit 0` 于「配置缺失/不可用」这类
软失败（收件人未配、From 未配、任一值含 CR/LF、传输未配或不可执行），
以免在 journal 里再叠一个 failed 单元。

## D7 — 实机 receipt 的时序陷阱（**给 implementer 的硬约束**）

dry-run 分支在 `:940-947` 直接 return，**根本不进 drop 阶段**，因此永远不执行
`SET lock_timeout`。AC「附一次 node-27 实机 receipt 证明正常趟不误触发」
只能由一次 **enforce** 趟满足。

`systemctl --user start nhms-node27-timeseries-retention.service` 会**真删生产 chunk**。
**implementer 与 orchestrator 一律不得手动触发**。receipt 从 merge + 部署后的
**下一个 13:15 CST 排程趟**读取，或由用户显式授权后手动触发。

**空转陷阱**：`selected` 为空时 enforce 分支直接落到 `_build("enforced", dropped_chunks=[])`，
`_default_drop_chunk` 一次都不调用、`SET lock_timeout` 一次都不执行，receipt 仍写
`enforced`。因此 E6 必须**同时**断言 `len(dropped_chunks) >= 1`，
延后语义是「第一个**实际删了 chunk** 的排程趟」，不是「下一趟」。

## D8 — 测试矩阵（注入式，不需要真 DB）

`drop_chunk` 是注入 seam：`run_retention(...)`（`scripts/node27_timeseries_retention.py:890`）
与 `main(..., drop_chunk=...)`。**没有 `run_tick` 这个符号**。
`_default_drop_chunk` 本身也可直测：`tests/test_node27_timeseries_retention.py:1186-1233`
的 `_install_fake_drop_psycopg2` 已经用 `monkeypatch.setitem(sys.modules, "psycopg2", ...)`
接住函数内的 `import psycopg2`，`_DropProbe.executed`（`:1176`）按序记录每个
`(sql, params)`；`:1235` 与 `:1288` 两个现有测试已经这样驱动真 `_default_drop_chunk`。
T7 不需要新机制。

| # | 注入 | 期望 |
|---|---|---|
| T1 | `pgcode="55P03"` 的**合成异常类** | `refusal_reason` 含 `: lock-contention(55P03): `，wire 前缀 byte-unchanged |
| T2 | `pgcode="40P01"` 的合成异常类 | 含 `: lock-contention(40P01): ` |
| T3 | `pgcode="57014"`（非锁类超时） | **不含** `lock-contention`，逐字保持旧形状 |
| T4 | 无 `pgcode` 属性的裸异常 | 不含 `lock-contention`（default-deny） |
| T5 | `LOCK_TIMEOUT_MS` = `0` / `-1` / `300000` / `300001` / `"abc"` | `RETENTION_CONFIG_INVALID`，DB 零接触 |
| T5b | `LOCK_TIMEOUT_MS = ""`（空串） | 按 `_optional_positive_int`（`:252-255`）惯例视同缺失 → 默认值 |
| T6 | 未设该 env | `config.lock_timeout_ms == 240_000` |
| T6b | `LOCK_TIMEOUT_MS = "2000"`（**非默认**） | `config.lock_timeout_ms == 2000` |
| T6c | 常量自身 | `0 < _DEFAULT_LOCK_TIMEOUT_MS < _DROP_TIMEOUT_MS` |
| T7 | `_DropProbe` + `lock_timeout_ms=1234`（**非默认**） | probe 收到的 SQL 文本里 `SET lock_timeout = 1234` 与 `SET statement_timeout` **都**出现，且**都在** `SELECT drop_chunks(` 之前 |
| T8 | 告警 wrapper + fake sendmail | 捕获的 stdin 正文含失败单元名；`NHMS_ALERT_EMAIL_TO` 未设 → `exit 0`；sendmail 路径不存在 → `exit 0` |

**合成异常类是必须的**（P3）：真 psycopg2 异常的 `pgcode` 是只读属性且
客户端构造时为 `None`（`psycopg2.OperationalError('boom').pgcode is None`，
赋值抛 `AttributeError: readonly attribute`）。同时把两个常量对着驱动自己的表钉住：
`psycopg2.errorcodes.LOCK_NOT_AVAILABLE == "55P03"`、`DEADLOCK_DETECTED == "40P01"`。

**T6b / T7 的非默认值是反空转的关键**：`lock_timeout_ms` 在冻结 dataclass 上带默认值，
若 `config_from_args` 校验了 env 却忘了把它传进 `RetentionConfig(...)`（`:348-356`），
只用默认值的 T6 与 T7 会双双保持绿色。

T7 是**反空转**保证：没有它，T1–T6 全绿也无法证明 `lock_timeout` 真进了 SQL。
