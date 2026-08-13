# Tasks: node27-frontier-stall-alert

## 1. 实现

- [ ] 1.1 `scripts/node27_frontier_stall_alert.py`：观测查询（`WHERE cycle_time IS NOT NULL AND status IN ('succeeded','parsed','published')`、per-source 三 marker：frontier/cycles/latest_created）、快照比对状态机（D1）、fail-safe 全表（D2）、sendmail 通道（D3）、原子状态/receipt 写、注入式 now()/sendmail_path/observation provider（可测性）、`--once`/`--dry-run`
- [ ] 1.2 DSN 打码：复用/同构 retention `_redact_error_text` 纪律，覆盖邮件正文、日志、receipt、state 四个出口
- [ ] 1.3 `scripts/node27_frontier_stall_alert_once.sh`（薄壳 wrapper；单实例互斥在 Python 内 fcntl 锁，见 D4/B13）
- [ ] 1.4 `infra/systemd/nhms-node27-frontier-alert.{service,timer}` + `infra/env/node27-frontier-alert.example`（D4 变量全集，DATABASE_URL 注明只读角色）
- [ ] 1.5 `scripts/node27_resource_governance.py` `DEFAULT_SERVICES` 增补 unit（ADR 0002 约定）
- [ ] 1.6 runbook `docs/runbooks/current-production-ops.md` 新节：三类邮件判读、阈值调参（stall 4h 依据）、误报处置、恢复闭环语义

## 2. 测试（tests/test_node27_frontier_stall_alert.py，全部注入 fake clock + fake sendmail + fake observation）

- [ ] 2.1 B1：快照 ≥4h 无变化 → 恰一封 `frontier-stalled`（invocation 计数=1，信头/收件人断言）
- [ ] 2.2 B2：frontier 推进 / cycles 增加 / latest_created 抬高 / 新 source 四形各自算进度 → 零邮件、`last_change_at` 重置；NULL source_id 行归 `__null_source__` 组不丢行、其推进同样算进度
- [ ] 2.2b B2-负例（方向性钉）：cycles **减少**（retention 删旧）、source 整体消失、集合内 status 跃迁（succeeded→parsed 同行）→ 均**不算**进度，stall 时钟不重置；`failed/cancelled/superseded/pending` 行不入快照；**减少后复原负例**：marker 降后回到原值不判进度（基线高水位钉，防 failed→parsed 出入集合伪进度）
- [ ] 2.3 B3：stall 持续 <6h 不重发；≥6h 恰一封重发
- [ ] 2.4 B4：恢复 → 恰一封 `frontier-recovered`，`alert_active` 清除；再次 stall 重新计满 4h 才触发
- [ ] 2.5 B5：状态跨进程重启持久（写后新实例读回）；原子写（写入中断模拟不留半文件——tmp 残留可容忍，state 本体不可损）
- [ ] 2.6 B6：状态**损坏**/schema_version 不符 → `monitoring-degraded` 邮件 + 基线重建（`baseline_reset_at`）+ 无异常逃逸；状态**缺失**（bootstrap）→ 零邮件、静默建基线（`baseline_established_at`），两分支分别钉
- [ ] 2.7 B7：查询失败 tick 不重置 stall 时钟（失败期间跨过 4h 阈值仍触发 stalled）；连续 2 tick 失败 → `observability-unavailable` 邮件（6h 去重）
- [ ] 2.8 B8：env 缺 `DATABASE_URL`/`NHMS_ALERT_EMAIL_TO` → 结构化 config error 退出非零、零 sendmail 调用
- [ ] 2.9 B9：全故障注入路径出口文本（邮件/日志/receipt/state）0 命中 DSN 密码子串（打码钉）
- [ ] 2.10 B10：sendmail 非零退出 → `last_alert_at` 不记、receipt 记发送失败、下 tick 重试发出
- [ ] 2.11 B11：`tests/test_node27_resource_governance.py` 增补 frontier-alert unit 注册钉（先例同构：#849/#853/#855 三段）
- [ ] 2.12 B12：`--dry-run` 完整判定 + 零副作用（零 sendmail 调用、state/receipt/JSONL 字节不变）
- [ ] 2.13 B13：单实例互斥——第二实例对已持锁 state 目录结构化 no-op 退出 0，零观测零邮件

## 3. Evidence Floor

- [ ] 3.1 `uv run pytest -q tests/test_node27_frontier_stall_alert.py tests/test_node27_resource_governance.py` 全绿（治理注册是唯一触碰的既有代码点，其钉子测试随迁）
- [ ] 3.2 `uv run ruff check .`
- [ ] 3.3 `openspec validate node27-frontier-stall-alert --strict --no-interactive`
- [ ] 3.4 node-27 live receipt（四步，全记录进 `.workplans/issue-1368/`）：
  - timer/service 装载（`systemctl --user list-timers` 含 frontier-alert）
  - 真实 DB 查询成功一 tick（先跑 `SELECT status, count(*) FROM hydro.hydro_run GROUP BY 1` 记录真实 status 分布佐证 D1 集合选择；receipt 含真实 per-source 快照；首 tick 为 bootstrap——预期**零邮件**、记 `baseline_established_at`）
  - **真实邮件投递**：sendmail exit 0 + mail log 远端 250 + `mumzy1995@163.com` 收件箱人工确认（三重，不得只验 exit 0；163 拒收则按 issue 预案停下重新拍板）
  - 拨钟触发：state 的 `last_change_at` 人为回拨 5h → 下一 tick 真实触发一封 stalled 邮件
