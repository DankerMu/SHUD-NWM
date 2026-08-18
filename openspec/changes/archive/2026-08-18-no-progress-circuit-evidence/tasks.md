## 1. Implementation

- [x] 1.1 新模块 `services/orchestrator/scheduler_no_progress.py`：纯函数核
      （状态载入/校验、A1/A3 适配器抽取（含源在场判据与 subject 去重取首行）、
      严格连续 merge（同对累加/换 reason 重置/源在场缺席清除/源不在场保留）、
      open 判定与 50 条截断、block 组装（`state_reset` 三态：缺省/"missing"/
      "corrupt"、open 条目带 operator_action_required 标注））+ 状态文件覆盖
      写原语：目录 fd 相对建 `.tmp`（O_NOFOLLOW，0o644）→ 写 + fsync →
      dir_fd 相对 `os.replace`；读侧 O_NOFOLLOW；缺失/损坏 → 空态 + 对应
      `state_reset` 值，不抛；enabled 完整 pass **恒写**状态文件（零条目也写）
- [x] 1.2 `ProductionSchedulerConfig` 加 `no_progress_circuit_passes`（env
      `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`，默认 3，≤0 禁用，照
      scheduler_config.py:221 邻位 `_env_int` 惯例与范围校验入队）
- [x] 1.3 集成：**仅** `scheduler_runtime.py:1417` 完整 pass 写盘点前
      observe→注入顶层 `no_progress_circuit`→有 open 则聚合 WARNING（token
      `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`）。**不得**挂
      `scheduler_core.py:914` 共享 `_write_evidence` 方法；早退（:711/:765/
      :805/:839/:897/:985）、异常（:1430-1458）、prelock（:596/:641/:681）、
      `lock_contended`（:711）一律不观察、不读写状态文件；disabled 零接触
- [x] 1.4 压缩层（scheduler_evidence_payload.py:920-993 字面量白名单重建）：
      加 `no_progress_circuit` 键 + 照 :983-992 模式的「源 payload 缺席即
      弹出」守卫；既有键行为逐字不变
- [x] 1.5 retention 兼容：以谓词层测试钉住 `no-progress-tracker.json` 归
      `unrecognised` 跳过删除（scripts/node22_scheduler_evidence_retention.py
      :212-215/:268-277 现状），脚本零改动
- [x] 1.6 `docs/runbooks/current-production-ops.md` 新小节：block 形状、
      WARNING grep、`consecutive_passes`=完整观察 pass 数（墙钟可能大于同数
      tick，早退/中止不计不清）、reason 三类对应下游 runbook 交叉指引
      （#1152 predecessor-pending / #1173 identity ladder / #1116
      comment_accounting_unproven）

## 2. Tests（tests/test_production_scheduler.py 为主）

- [x] 2.1 适配器契约（真实形状 payload 片段）：A1 blocked 行（含
      `state_evidence.operator_action_required=true` 标注随行）产生正确观察；
      `skipped_candidates`（status 仍 selected，如 terminal_hydro_success）
      零观察；selected 正常行零观察；A3 outcome 行（含 reason_class None 档）
      产生正确观察；同 subject 同 pass 多行取首行
- [x] 2.2 红证（A1 主线）：同 (candidate, reason) 连续 3 个独立 scheduler
      实例逐 pass 驱动（共享 evidence_root tmp_path，oneshot 忠实，构造先例
      tests/test_production_scheduler.py:31013-31024/:419-457）：第 3 pass
      evidence 出现 `no_progress_circuit.open` 条目（consecutive_passes=3、
      first/last_pass_id 与 `evidence["pass_id"]` 对账）+ 聚合 WARNING；
      **改动前该测试必须红**（payload 无此键）
- [x] 2.3 连续/防误清语义：换 reason → 重置（第 4 pass 才开闸）；源在场且
      subject 缺席 → 清除；健康 pass → block 注入 open 空、无 WARNING；
      **A3 源不在场**（`reserved_unbound_error` 形状 / dry-run skipped）→
      A3 条目原样保留、计数不断；**早退/中止 pass**（lock_contended 或
      SchedulerResourceLimitError 分支）→ 不读写 tracker、其 evidence 形状
      逐字如旧、既有计数不受影响
- [x] 2.4 A3 开闸场景：comment_accounting_unproven 形状 outcome 连续 3 完整
      pass → open（#1116 wedge 可见性验收）
- [x] 2.5 disabled（0 与负值）：payload 无新键（**含超预算走压缩路径的
      pass**）、evidence_root 无 tracker 文件、零日志——逐字等旧；默认值 3
      经 env 与直接构造两路生效
- [x] 2.6 持久化：损坏 JSON/半写文件 → `state_reset:"corrupt"`；文件缺失 →
      `"missing"`；两者 pass 不失败；原子写 round-trip（下一实例读回）；
      enabled 健康 pass 后文件存在（恒写）→ 下一 pass 无 state_reset
- [x] 2.7 有界性：51+ 开闸主体 → open 截断 50 + truncated 计数；bounded
      压缩保留 block、禁用态压缩不出 null 键、既有键裁剪逐字不变
- [x] 2.8 retention 谓词：tracker 文件名归 unrecognised 不删（现状钉住）

## 3. Verification

- [x] 3.1 红证记录：2.2 在改动前红（evidence 无 `no_progress_circuit` 键）
- [x] 3.2 uv run pytest -q tests/test_production_scheduler.py（全量）
- [x] 3.3 uv run ruff check services tests scripts
- [x] 3.4 openspec validate no-progress-circuit-evidence --strict --no-interactive
- [x] 3.5 merge 后：node-27 oracle receipt 已记入 #1118（定向 circuit 28
      passed；全量 72 红全量分类为 #1513 umask 环境类
      `provider_lock_parent_unsafe`，与本 diff 无关，已追评 #1513 扩影响面）；
      node-22 实机 pass 观察**如实递延**——调度器当前未运营（无 timer/容器/
      cron，evidence root 空），义务转记 #1118 receipt 评论（下次运营首个完整
      pass 采 block 形状 + 产物字节数）
