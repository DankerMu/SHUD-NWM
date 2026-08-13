# Proposal: node27-frontier-stall-alert（#1368）

## Why

2026-08-12 事故：scheduler pass 在 forcing 阶段被 NFS 锁挂死（unit 恒 `activating`、从未 failed），业务化静默停摆 ~11h 零告警。缺口不是"某个 unit 没超时"，而是**没有任何机制发现"业务化不再产出"**。unit 级超时/OnFailure 已在 issue 中论证为无效方案（unit 未 failed；全趟 2h16m-3h01m 无法定安全阈值），明确 out of scope。

## What Changes

在 node-27 新增独立告警 lane（照抄成熟的 `nhms-node27-*.timer` 模式）：

1. **新脚本 `scripts/node27_frontier_stall_alert.py`**：每 30 分钟查 `hydro.hydro_run`（最终落库产物——node-22 任何故障类都表现为前沿不推进，一个信号全覆盖），限定 post-ingest 生命周期行（`status IN ('succeeded','parsed','published')`，集合内跃迁不动快照），按 `source_id` 取三 marker：`max(cycle_time)`（前沿）、`count(DISTINCT cycle_time)`（回填计数）、`max(created_at)`（到达高水位）；与持久化基线比对，**进度=新 source 或任一 marker 严格增**（减少——行转出集合、人工删除——不算进度、不重置计时），**连续 ≥4h（可配）无进度**即告警。判据 progress-based，永不比对墙上时钟（追欠账时前沿本就落后数天，wall-clock 判据恒误报）。
2. **邮件通道**：本机 `sendmail`（两节点实测在位、:25 开放；无凭据参与）。收件地址 env `NHMS_ALERT_EMAIL_TO`（operator 拍板出处：本次工作流会话 2026-08-13 AskUserQuestion 答复，`mumzy1995@163.com`——issue 原文列为待确认项，此处即确认记录），未配置即 fail-closed 拒启。去重：首触发一封；持续未恢复每 6h（可配）重发；前沿恢复发一封 recovered 闭环。
3. **fail-safe 方向（宁可多报不可漏报）**：状态文件**损坏** → 立即发"监控降级"告警 + 重建基线（绝不静默重置计时）；状态**缺失**=bootstrap，静默建基线（与损坏分流，防全新安装噪音）；DB 查询连续失败 ≥2 tick → 发"观测不可用"告警，且查询失败**永不重置** stall 计时；sendmail 非零退出 → 不记 `last_alert_at`（下 tick 自动重试）。
4. **systemd + env + 治理注册**：`nhms-node27-frontier-alert.{service,timer}`（OnCalendar 每 30 分钟、oneshot、in-script fcntl 单实例锁）；`infra/env/node27-frontier-alert.example`；unit 注册进 `node27_resource_governance.py` `DEFAULT_SERVICES`（ADR 0002 约定）。
5. **runbook**：`docs/runbooks/current-production-ops.md` 新节——告警邮件判读、误报/恢复处置、阈值调参口径。

## Non-Goals

- `RuntimeMaxUSec` unit 超时兜底（issue 明确另开，阈值需 ≥5h 且要追欠账实测）。
- 钉钉/企业微信通道（可达但 operator 未采用）。
- per-source 独立告警：判据是**观测级**（任一 source 推进=业务活着）；单 source 上游断供属另一失败类，要做另立 issue。
- receipt 正式 schema/`schemas/` 注册：状态与 receipt 为本 lane 内部产物，无跨工具消费方。
- node-22 侧任何改动；恢复自动化（本 lane 只通知不处置）。
- **信号口径认领**：本 lane 观测的是 post-ingest 前沿（含 `succeeded`），非严格 display-published 前沿——parse/publish 段静默冻结而 ingest 仍活跃的几何不告警（该故障类使 autopipeline 非零退出 → unit failed，属 systemd 可见面，与本 issue 针对的"挂死不 failed"不同类）；要观测 display 前沿另立 issue。
- 本次事故根因（rpc.statd 自启）的修复——硬件/OS 运维流程。

## 待实测项（live receipt 期）

从 node-27 直发 `mumzy1995@163.com` 的真实可达性：投递验证=sendmail exit 0 + mail log 远端 250 + 收件箱人工确认三重，**不得只验 sendmail 返回 0**（issue 原文要求）。若 163 拒收/进垃圾箱，按 issue 预案回来重新拍板通道，deviation 记录。

**2026-08-13 实测判决**：非 163 拒收——node-27 本机 postfix 刻意空路由（`default_transport = error`），`/usr/sbin/sendmail` exit 0 后异步 bounce（`dsn=5.0.0`），"只验 exit 0"盲区被实锤。按预案重新拍板（用户裁定）：改认证 SMTP shim 直投 `smtp.163.com:465`（出网 465 实测可达；见 design D3、tasks §1U）。250 改由目的方提交服务器同步返回，三重验证口径随之更新（tasks 3.4）。
