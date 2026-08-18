## 1. Implementation

- [ ] 1.1 `services/orchestrator/reconcile.py`：新增
      `_accounting_store_flags_allow_comment(stdout) -> bool`（解析
      `AccountingStoreFlags` 行，逗号 token 含 `job_comment` 才 True；行缺失/
      `(null)`/空值 False）与 `default_comment_storage_probe(slurm_bin_path)`
      （`scontrol show config` 经 `_bounded_visibility_stdout`；异常 catch
      `ReconcileQueryUnavailable` 父类吞为 False）；失败路径 WARNING 一次、
      原因可区分（「探针无法执行」vs「flags 不含 job_comment」）
- [ ] 1.2 `default_comment_sacct_querier` 加 `comment_storage_probe` 可注入参数
      （默认 `default_comment_storage_probe(slurm_bin_path)`）；插入点钉死：
      contract-version 检查（:368-369）之后、visibility 门整块（:370-377）之前
      ——插在第 370 行之前、不得落入 `if not any(owner_scope) …` 块内部；
      nonlocal 一次性缓存；未证明抛
      `ReconcileQueryUnavailable("accounting does not store job comments",
      reason_class="comment_accounting_unproven")`；owner/global/legacy 三类
      查询均被门住；其余渲染/分页/预算逐字不变
- [ ] 1.3 `docs/runbooks/failed-basin-retry.md` 新增「comment_accounting_unproven
      卡住的 reserved 行」处置小节：按 reason token 从 pass evidence 定位卡住行
      → 复用 :187-189 的 `sacct -a --name nhms_forecast` + user/account/提交窗
      兜底核实在飞与否 → 如实写当下存在的处置出口（在飞：等终态按既有流程；
      确死：人工降级机制按仓内实有工具查明记载；无安全工具则明说并路由
      tooling follow-up）；写明代价：未处置期间该 cohort 的 cycle 维持
      PIPELINE_ALREADY_ACTIVE
- [ ] 1.4 既有 15 处实构 querier 用例注入 `comment_storage_probe=lambda: True`
      （tests/test_gateway_reconcile.py:4621,4648,4669,4898,5038,5084,9876,9894,
      10217,10280,10327,10396,10420,10462,10494），保证单测零真实 scontrol、
      :9876/:9894 抛点归属断言不变

## 2. Tests（tests/test_gateway_reconcile.py）

- [ ] 2.1 探针解析/日志向量：`AccountingStoreFlags    = (null)`（node-22 实测
      格式，含多空格）→ False + WARNING 含「不存 comment」语义；`= job_comment`
      → True；`= job_comment,job_extra` → True；`= job_extra` → False；行缺失 →
      False；scontrol 抛 `ReconcileQueryUnavailable` → False + WARNING 含
      「探针无法执行」语义（不外抛）
- [ ] 2.2 querier 门：probe False → 抛 `ReconcileQueryUnavailable` 且
      reason_class=="comment_accounting_unproven" 且 **sacct stdout 缝零调用**；
      probe True → sacct 正常分页（现状）；同一 querier 连续两次查询 probe 仅
      执行一次（缓存）
- [ ] 2.3 e2e 红证（**模块缝，不经新 kwarg**——改动前该测试必须可执行且红）：
      注入 `global_visibility_probe=lambda: True` 钉住 visibility 轴；monkeypatch
      `_bounded_visibility_stdout` 返回含 `AccountingStoreFlags    = (null)` 的
      scontrol config、`_bounded_sacct_stdout` 返回 coverage-complete 且真实在飞
      job 的 Comment 为空的分页输出；过 grace reserved 行经
      `reconcile_reserved_unbound_jobs`：**改动前**判 reservation_lost（红形状），
      **改动后**行保持 reserved、pass evidence 呈现 query_unavailable/
      comment_accounting_unproven（绿）
- [ ] 2.4 存 comment 集群零变化锁：probe True 下 owned_match 绑定与
      global_absence 过 grace 判 reservation_lost 与现状逐字一致
- [ ] 2.5 既有 comment_query 可调用级 fake 用例与 1.4 迁移后的 15 处实构用例
      全绿
- [ ] 2.6 抛点优先级断言：comment probe False + visibility False →
      reason_class=="comment_accounting_unproven"（comment 门先）；unsupported
      accepted_submit_contract_version → 仍抛原「contract version is
      unsupported」消息（contract-version 检查最先）

## 3. Verification

- [ ] 3.1 红证记录：2.3 在改动前红（形状：reservation_lost 被判出，非 TypeError）
- [ ] 3.2 uv run pytest -q tests/test_gateway_reconcile.py（全量）
- [ ] 3.3 uv run ruff check services tests
- [ ] 3.4 openspec validate comment-absence-capability-gate --strict --no-interactive
- [ ] 3.5 merge 后双 receipt 记入 #1116：(a) node-27 oracle receipt（3.2 套件 +
      定向选择器）；(b) node-22 真探针 receipt——在 node-22 上以真实
      `scontrol show config` 驱动 `default_comment_storage_probe` 确认返回 False
      （集群 `AccountingStoreFlags=(null)` 输入串已于 2026-08-18 前置实测，见
      proposal Why），如当时存在 reserved-unbound 行则一并采 pass evidence 中的
      `comment_accounting_unproven` token；follow-up issue（保守自动匹配兜底）
      经 issue-scribe 归档并在 #1116 交叉引用
