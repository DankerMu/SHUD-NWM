## 1. Implementation

- [ ] 1.1 新模块 `services/orchestrator/scheduler_no_progress.py`：纯函数核
      （载入/校验状态、三适配器抽取观察、严格连续 merge（同对累加/换 reason
      重置/缺席清除）、open 判定与 50 条截断、block 组装）+ 状态文件原子写
      （tmp+rename）；文件缺失/损坏 → 空态 + block 标 `state_reset: true`，
      **不抛**
- [ ] 1.2 `ProductionSchedulerConfig` 加 `no_progress_circuit_passes`（env
      `NHMS_SCHEDULER_NO_PROGRESS_CIRCUIT_PASSES`，默认 3，≤0 禁用，照
      scheduler_config.py:221 邻位 `_env_int` 惯例与范围校验入队）
- [ ] 1.3 `run_once` 集成（scheduler_runtime.py，payload 组装后、
      `write_evidence`（scheduler_evidence.py:367）前单点）：enabled 时
      observe→注入顶层 `no_progress_circuit` block→存在 open 条目则记聚合
      WARNING（token `SCHEDULER_NO_PROGRESS_CIRCUIT_OPEN`）；disabled 时零
      接触（不读不写不注入不日志）
- [ ] 1.4 bounded 压缩层（scheduler_evidence_payload.py）：顶层
      `no_progress_circuit` block 在压缩 payload 中保留（block 自身有界，
      无需内部裁剪；既有键行为逐字不变）
- [ ] 1.5 `scripts/node22_scheduler_evidence_retention.py`：核查删除谓词对
      `no-progress-tracker.json` 的命中情况；若命中则白名单豁免（tracker 非
      evidence，不受 168h 裁剪）；不命中则以测试钉住现状
- [ ] 1.6 `docs/runbooks/current-production-ops.md` 新小节：block 形状、
      WARNING token grep 姿势、三适配器对应的下游 runbook 交叉指引
      （#1152 predecessor-pending / #1173 identity ladder / #1116
      comment_accounting_unproven 处置小节）

## 2. Tests（tests/test_production_scheduler.py 为主；纯函数层可同文件就近组织）

- [ ] 2.1 适配器契约：对**真实形状**的 payload 片段（candidate summary /
      state evidence operator_action_required / reserved_unbound outcome）各
      产生正确 (subject, reason) 观察；成功/推进候选（status 属推进类或
      reason 空）零观察
- [ ] 2.2 红证（A1 主线）：同 (candidate, reason) 连续 3 个独立 scheduler
      实例逐 pass 驱动（共享 evidence_root，oneshot 忠实——不共享内存对象）：
      第 3 pass 的 evidence 出现 `no_progress_circuit.open` 条目
      （consecutive_passes=3、first/last pass id 正确）+ 聚合 WARNING；
      **改动前该测试必须红**（payload 无此键）
- [ ] 2.3 连续语义：第 2 pass 换 reason → 重置（第 4 pass 才开闸）；subject
      缺席一 pass → 条目清除；健康 pass（全成功）→ block 注入但 open 空、
      无 WARNING
- [ ] 2.4 A2 / A3 各一条开闸场景（A3 用 comment_accounting_unproven 形状的
      outcome——#1116 wedge 的直接可见性验收）
- [ ] 2.5 disabled（0 与负值）：payload 无新键、evidence_root 无 tracker 文件、
      无日志——与今日逐字相同；默认值=3 经 env 与直接构造两路生效
- [ ] 2.6 持久化：损坏 JSON / 半写文件 → 空态 + `state_reset: true` 且 pass
      不失败；原子写后文件可被下一实例读回（round-trip）
- [ ] 2.7 有界性：51+ 个开闸主体 → open 列表截断至 50 + truncated 计数；
      bounded 压缩路径保留 block 且既有键裁剪行为逐字不变
- [ ] 2.8 retention：谓词层测试钉住 tracker 文件不被删除（依 1.5 的实际
      结论写：白名单豁免生效 或 glob 天然不命中）

## 3. Verification

- [ ] 3.1 红证记录：2.2 在改动前红（evidence 无 `no_progress_circuit` 键）
- [ ] 3.2 uv run pytest -q tests/test_production_scheduler.py（全量）
- [ ] 3.3 uv run ruff check services tests scripts
- [ ] 3.4 openspec validate no-progress-circuit-evidence --strict --no-interactive
- [ ] 3.5 merge 后：node-27 oracle receipt（3.2 套件 + 定向选择器）记入
      #1118；node-22 实机观察一个真实 pass 的 evidence block 形状（enabled
      默认下 open 空或如实反映当前 wedge——#1116 的 reserved 行若仍在扣即为
      首个真实开闸样本）一并记入
