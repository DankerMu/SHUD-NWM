# Tasks: node27-frontier-stall-alert

## 1. 实现

- [x] 1.1 `scripts/node27_frontier_stall_alert.py`：观测查询（`WHERE cycle_time IS NOT NULL AND status IN ('succeeded','parsed','published')`、per-source 三 marker：frontier/cycles/latest_created）、快照比对状态机（D1）、fail-safe 全表（D2）、sendmail 通道（D3）、原子状态/receipt 写、注入式 now()/sendmail_path/observation provider（可测性）、`--once`/`--dry-run`
- [x] 1.2 DSN 打码：复用/同构 retention `_redact_error_text` 纪律，覆盖邮件正文、日志、receipt、state 四个出口
- [x] 1.3 `scripts/node27_frontier_stall_alert_once.sh`（薄壳 wrapper；单实例互斥在 Python 内 fcntl 锁，见 D4/B13）
- [x] 1.4 `infra/systemd/nhms-node27-frontier-alert.{service,timer}` + `infra/env/node27-frontier-alert.example`（D4 变量全集，DATABASE_URL 注明只读角色）
- [x] 1.5 `scripts/node27_resource_governance.py` `DEFAULT_SERVICES` 增补 unit（ADR 0002 约定）
- [x] 1.6 runbook `docs/runbooks/current-production-ops.md` 新节：三类邮件判读、阈值调参（stall 4h 依据）、误报处置、恢复闭环语义

## 2. 测试（tests/test_node27_frontier_stall_alert.py，全部注入 fake clock + fake sendmail + fake observation）

- [x] 2.1 B1：快照 ≥4h 无变化 → 恰一封 `frontier-stalled`（invocation 计数=1，信头/收件人断言）
- [x] 2.2 B2：frontier 推进 / cycles 增加 / latest_created 抬高 / 新 source 四形各自算进度 → 零邮件、`last_change_at` 重置；NULL source_id 行归 `__null_source__` 组不丢行、其推进同样算进度
- [x] 2.2b B2-负例（方向性钉）：cycles **减少**（retention 删旧）、source 整体消失、集合内 status 跃迁（succeeded→parsed 同行）→ 均**不算**进度，stall 时钟不重置；`failed/cancelled/superseded/pending` 行不入快照；**减少后复原负例**：marker 降后回到原值不判进度（基线高水位钉，防 failed→parsed 出入集合伪进度）
- [x] 2.3 B3：stall 持续 <6h 不重发；≥6h 恰一封重发
- [x] 2.4 B4：恢复 → 恰一封 `frontier-recovered`，`alert_active` 清除；再次 stall 重新计满 4h 才触发
- [x] 2.5 B5：状态跨进程重启持久（写后新实例读回）；原子写（写入中断模拟不留半文件——tmp 残留可容忍，state 本体不可损）
- [x] 2.6 B6：状态**损坏**/schema_version 不符 → `monitoring-degraded` 邮件 + 基线重建（`baseline_reset_at`）+ 无异常逃逸；状态**缺失**（bootstrap）→ 零邮件、静默建基线（`baseline_established_at`），两分支分别钉
- [x] 2.7 B7：查询失败 tick 不重置 stall 时钟（失败期间跨过 4h 阈值仍触发 stalled）；连续 2 tick 失败 → `observability-unavailable` 邮件（6h 去重）
- [x] 2.8 B8：env 缺 `DATABASE_URL`/`NHMS_ALERT_EMAIL_TO` → 结构化 config error 退出非零、零 sendmail 调用
- [x] 2.9 B9：全故障注入路径出口文本（邮件/日志/receipt/state）0 命中 DSN 密码子串（打码钉）
- [x] 2.10 B10：sendmail 非零退出 → `last_alert_at` 不记、receipt 记发送失败、下 tick 重试发出
- [x] 2.11 B11：`tests/test_node27_resource_governance.py` 增补 frontier-alert unit 注册钉（先例同构：#849/#853/#855 三段）
- [x] 2.12 B12：`--dry-run` 完整判定 + 零副作用（零 sendmail 调用、state/receipt/JSONL 字节不变）
- [x] 2.13 B13：单实例互斥——第二实例对已持锁 state 目录结构化 no-op 退出 0，零观测零邮件

## 1R. Review round-1 修复（2×P1 + 3×P2 + 6×P3，verifier 全裁决通过）

- [x] 1R.1 **P1** state 读/解析异常收容：`load_state` 读阶段捕 `UnicodeDecodeError`、解析阶段补 `RecursionError` → 全部归 corrupt 分支（A-C4）
- [x] 1R.2 **P1** `psycopg2.connect` 加有界 `connect_timeout`（模块常量，同仓先例 `node27_timeseries_compression.py:464`）+ `.example` DSN 注释同步（B-C1）
- [x] 1R.3 **P2** 基线只由真实观测建立：rebuild/bootstrap 遇观测失败 → `baseline_pending`，不记 established/reset 戳、不落空基线；下一成功观测静默填充、不判进度、不动 `last_change_at`、不清 `alert_active`（A-C1）
- [x] 1R.4 **P2** degraded 发送失败 → `degraded_pending` 落盘、下 tick 重试（受 `last_degraded_alert_at` 去重钟约束）；持久损坏每 tick 发为记录在案的 over-report（A-C2/A-C3）
- [x] 1R.5 **P2** env 单读者：wrapper 仅在 `DATABASE_URL` 未注入时 source；`.example` 的 `NHMS_ALERT_EMAIL_FROM` 示例行改双语法安全（引号），加两解析器分歧警示注释（B-C3）
- [x] 1R.6 **P3** stalled 标签改 `last_alert_at is None` 判 initial/resend（C-C2）
- [x] 1R.7 **P3** `NHMS_ALERT_EMAIL_TO`/`_FROM` 拒 CR/LF（config 期 fail-closed）（C-C3）
- [x] 1R.8 **P3** `acquire_lock` 的 mkdir/os.open 移入 try → `FrontierAlertConfigError`（B-C4）
- [x] 1R.9 **P3** service unit `TimeoutStartSec` 改有界值（oneshot 执行期限；监控器挂死必须转 unit failed）（B-C2）

## 2R. Review round-1 回归锚

- [x] 2R.1 B14：非 UTF-8 state 文件 + 深嵌套 JSON state → corrupt 分支（degraded 邮件、基线重建/pending、零异常逃逸）
- [x] 2R.2 B15：connect_timeout 钉（fake psycopg2 断言 connect kwargs 或常量钉）
- [x] 2R.3 B16：corrupt/bootstrap 与观测失败同 tick → 空基线不落盘、无 established/reset 戳；下一成功观测（数据与损坏前一致）不判进度、stall 时钟不重置——A-C1 探针几何直接入测
- [x] 2R.4 B17：degraded 发送失败 → 下 tick 重试成功恰一封；6h 内不重复
- [x] 2R.5 B18：首封 stalled 发送失败 → 重试封标签 `initial`（受 B10 断言扩展）
- [x] 2R.6 B19：`NHMS_ALERT_EMAIL_TO` 含 `\r\n` → config error 退出非零零邮件
- [x] 2R.7 B20：锁目录只读 → 结构化 config error（非裸 traceback）
- [x] 2R.8 B21：`.example` 全部非注释行 + 全部注释示例行去注释后均通过 bash 语法（`bash -n` 级）且值与 systemd 释义一致（引号规约钉）

## 1S. Review round-2 修复（3×P2 + 4×P3，verifier 裁决；state-corruption-escape 类重复 → 不变式审计升级为整类收容）

- [x] 1S.1 **P2** wrapper symlink/0600 校验上移出哨兵门（无条件执行，仅 source 留门内）；哨兵改 lane-scoped `NODE27_FRONTIER_ALERT_ENV_INJECTED=1`（service 注入）；新增 wrapper 测试（兄弟先例形态，必含已注入路径用例）
- [x] 1S.2 **P2** degraded 去重钟 per-kind：`last_degraded_alert_by_kind: dict`（旧标量读入迁移，schema_version 不变）；`degraded_due` 判定移进 per-kind 循环
- [x] 1S.3 **P2** `load_state` 读+解析两阶段收敛为整类收容（`except Exception`，`RecursionError` 分支保留注释理由；reason 带 `type(error).__name__`）——不变式：失败类收容不得用枚举集
- [x] 1S.4 **P3** `baseline_pending_kind`（bootstrap|reset，缺省 bootstrap 兼容）：corrupt 起源填充记 `baseline_reset_at`，degraded retry 邮件正文戳位如实
- [x] 1S.5 **P3** runbook §10.2 锁/只读卷措辞如实分叉（锁不可开 → rc=2 零邮件仅 unit failed；state_path 指目录 → 每 tick 一封）；§10.4 时间线判读改指 JSONL（receipt 已被覆写）
- [x] 1S.6 **P3** `NHMS_FRONTIER_STALL_HOURS`/`_RESEND_HOURS` config 期拒 nan/inf/超界（timedelta 构造入 config try，数值不可用=rc=2 config error；注意 `math.isfinite` 不够，1e30 也炸 timedelta）
- [x] 1S.7 **P3** 默认 From 主机名 sanitize（剔除禁字符回落 `node-27`，绝不 reject——reject 把装饰性异常升级成零邮件 config error，反方向）

## 2S. Review round-2 回归锚

- [x] 2S.1 B22：极值时间戳 state（`9999-12-31T23:59:59-14:00`）→ corrupt 分支、一封 degraded、下 tick 自愈（state 已重写）
- [x] 2S.2 B23：state-corrupt 邮件发出后 <6h 内 observability budget 跨越 → observability-unavailable 照发（跨类不抑制）；同类 6h 内重复仍去重
- [x] 2S.3 B24：corrupt 起源 pending 填充 → `baseline_reset_at == fill_at ∧ established is None`；bootstrap 起源对偶（既有断言保留）
- [x] 2S.4 B25：wrapper——已注入哨兵 + 644 env 文件 → BLOCKED rc=2；已注入 + 0600 正常放行不 source；未注入 + 0600 → source 且跑通（subprocess 真 bash）
- [x] 2S.5 B26：`NHMS_FRONTIER_STALL_HOURS=nan/inf/1e30` → rc=2 结构化 config error 零邮件；默认 From 注入坏主机名 → sanitize 回落且 `_header_safe` 通过

## 1T. Review round-3 修复（三轮门 depth retro 的 invariant closure；1×P2 + 2×P3）

- [x] 1T.1 **P2** 邮件链整类收容：`_Outbox.send` 层 `except Exception` 覆盖 `build_message` + runner 调用 → SendResult 失败（UnicodeEncodeError 等落 degraded_pending/stalled 重试，over-report）；随附全文件 except 子句清点报告（收敛/有意保留窄集+理由）
- [x] 1T.2 **P3** 删 `_degraded_clock_from_json` legacy 标量迁移（return {}，按 bootstrap；标量不可归因）；B17-throttled 改钉真实几何（resend_hours 收窄）或删除标 defensive；新增反向回归：legacy 标量 state → 首封 observability-unavailable 照发
- [x] 1T.3 **P3** runbook §10.2 "唯一零邮件几何"改口：锁文件是一类，wrapper BLOCKED rc=2 家族 + config error 家族同为零邮件几何，triage 统一走 unit failed + `frontier-alert.log` 的 `BLOCKED rc=2 reason=` 行

## 2T. Review round-3 回归锚

- [x] 2T.1 B27：`NHMS_ALERT_EMAIL_TO` 含 surrogate 字节（`"ops\udcc4@example.com"`）→ 该 tick 不逃逸：send 失败落 receipt/degraded_pending、state 照写、下 tick 照跑（对照 pre-fix：FRONTIER_ALERT_UNCAUGHT_ERROR 冻结循环）
- [x] 2T.2 B28：legacy 标量 state（含已置钟）→ `last_degraded_alert_by_kind == {}`；budget 跨越 → 恰一封 observability-unavailable（probe B 几何断言 1 非 0）

## 1U. Live-receipt 期通道改判（本机 postfix 空路由 → 认证 SMTP shim）

- [x] 1U.1 实测判因：bootstrap tick 零邮件 ✓、拨钟 5h 触发 ✓、sendmail exit 0 ✓，但 mail log `dsn=5.0.0 status=bounced relay=none`——node-27 postfix `default_transport = error`（刻意空路由），非 163 拒收；按 issue 预案停下重新拍板，用户拍板认证 SMTP shim
- [x] 1U.2 `scripts/node27_frontier_smtp_sendmail.py`：stdlib-only sendmail 兼容 shim（`-t -i`、stdin 读信、`SMTP_SSL` 认证直投、凭据仅经 `NHMS_SMTP_USER/PASS` env、成功落 `SMTP-ACCEPTED` 证据行、失败整类收容 exit 70、口令零泄漏）；`.example` 增 SMTP 段；runbook §10 通道与盲区收口改口

## 2U. 通道改判回归锚

- [x] 2U.1 B29：shim 单测（fake SMTP 注入）——`-t` 收件人提取/Bcc 剥离、缺凭据 exit 64 不连网、认证失败 exit 69 且口令绝不出现在任何输出、整类收容 exit 70、subprocess 入口线

## 1V. PR#1371 round-5 裁决修复（#1373 子 PR；1×P1 + 1×P2 + 4×P3）

- [x] 1V.1 **P1** F1 CONFIRMED：`default_smtp_factory` 补 `context=ssl.create_default_context()`（实测缺省 `ssl._create_stdlib_context()` = `check_hostname=False`/`CERT_NONE`——授权码在未验证隧道递出且 250 可被路径上攻击者伪造）
- [x] 1V.2 **P2** F3 CONFIRMED：lane `default_sendmail_runner` 成功分支捕获 stderr 尾行入 SendResult/outbox 记录（现状成功分支丢弃 `completed.stderr`，`SMTP-ACCEPTED` 证据行生产路径不可观测，receipt 与空路由时代逐字节相同；verifier 裁定 runbook 手工管道措辞不构成 remedy）——lane 侧唯一允许触碰点
- [x] 1V.3 **P3** F2 PLAUSIBLE：shim 加 From↔`NHMS_SMTP_USER` 地址部分一致性校验（display-name 形式取 addr-spec 比对，不匹配 exit 64 config error）；`.example` 出厂态 `NHMS_ALERT_EMAIL_FROM` 解注释为激活行；runbook "必须等于"措辞改"地址部分相等"
- [x] 1V.4 **P3** F4 PLAUSIBLE：8-bit 正文（`RUNBOOK_REFERENCE` 中文使每封必含）按 `has_extn('8bitmime')` 协商 `BODY=8BITMIME` **且对每个 8-bit part 声明 `Content-Transfer-Encoding: 8bit`**（RFC 6152 信封+part 两半；round-1 R1 red-first 补钉，已编码 part 保留自身 CTE），服务器不支持时回落显式 CTE（quoted-printable/base64 重编码）——绝不裸推 8-bit
- [x] 1V.5 **P3** F6 CONFIRMED：oracle 收口——真实 lane `build_message` 输出穿 shim 的回归（兼作 F4 锚）+ `default_smtp_factory` 钉（verified context/SMTP_SSL/timeout，变异明文 SMTP 必红）
- [x] 1V.6 **P3** F5 CONFIRMED-DEFER 收编：`.example`/runbook 加"勿配多收件人"警示（部分拒收 → exit 69 → 已收方 30min 重复风暴；69-over-0 方向符合 over-report 契约不改语义）

## 2V. 裁决修复回归锚

- [x] 2V.1 B30：factory 钉（context.verify_mode==CERT_REQUIRED ∧ check_hostname ∧ SMTP_SSL ∧ timeout）；变异去 context/换明文 SMTP → 红
- [x] 2V.2 B31：lane 成功发送 → outbox/receipt 记录含 shim 证据行尾巴（`SMTP-ACCEPTED` 可从部署路径取证）；失败分支既有行为不回归
- [x] 2V.3 B32：真实 `build_message`（含中文 runbook 引用 + em dash）输出穿 shim → 收件人/信封正确、8BITMIME 或 CTE 路径断言、`=?unknown-8bit?` 不出现在出线信头
- [x] 2V.4 B33：From≠认证账号（含 display-name 形式匹配/不匹配两例）→ exit 64 不连网；匹配 → 放行

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_node27_frontier_stall_alert.py tests/test_node27_resource_governance.py` 全绿（治理注册是唯一触碰的既有代码点，其钉子测试随迁）
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate node27-frontier-stall-alert --strict --no-interactive`
- [x] 3.4 node-27 live receipt（四步；完成于 PR #1374 @ 47b4edd6，证据存 PR 评论 + `.workplans/`）——2026-08-14 实录：timer 装载 ✓；真实 status 分布 + bootstrap 零邮件 + `baseline_established_at`（2026-08-13，PR #1371 期）✓；拨钟 5h → 恰一封 stalled，**三重投递验证**：shim exit 0 + receipt/JSONL `evidence: "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"`（目的方同步 250）+ 收件箱人工确认**在收件箱非垃圾箱** ✓；operator 随即裁定生产阈值 48h（单日 cycle 稳态，runbook §10.3）。首轮本机 postfix 通道 bounce 实录保留为盲区证据（1U.1）：
  - timer/service 装载（`systemctl --user list-timers` 含 frontier-alert）
  - 真实 DB 查询成功一 tick（先跑 `SELECT status, count(*) FROM hydro.hydro_run GROUP BY 1` 记录真实 status 分布佐证 D1 集合选择；receipt 含真实 per-source 快照；首 tick 为 bootstrap——预期**零邮件**、记 `baseline_established_at`）
  - **真实邮件投递**（通道改判后口径）：shim exit 0 + `SMTP-ACCEPTED`（250 由 smtp.163.com 提交服务器同步返回）+ `mumzy1995@163.com` 收件箱人工确认（三重，不得只验 exit 0；163 拒收则按 issue 预案停下重新拍板）。首轮本机 postfix 通道的 exit-0-后异步-bounce 实录保留为盲区证据
  - 拨钟触发：state 的 `last_change_at` 人为回拨 5h → 下一 tick 真实触发一封 stalled 邮件
