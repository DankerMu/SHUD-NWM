# Tasks — operator-gate-manual-publisher (#1104)

Fixture level: compact（S 规模：doc 事实化 + 1 行 warning + 测试钉）。
Risk triage：无 DB/远端面、无 receipt/schema 变更；主要风险 (a) WARNING 行
破坏 stderr JSON 消费——已核实测试全部用 `strip().splitlines()[-1]` 解析，
且 `--allow-uncovered-cutover` banner 已是同通道先例；(b) design.md 措辞改
完后 `node22-scheduler-registry-refresh` change 必须仍 strict 通过；(c) 假
绿——warning 钉若只断言子串存在，删掉 print 的 mutant 必须红（简单面，无
需矩阵）。Must-preserve：CLI 全部现有出口行为、stderr JSON 载荷逐字节不变
（除新增前置行）、`--allow-uncovered-cutover` banner 原样。Seams under
test：main() 启动 warning、失败路径 last-line JSON 仍可解析。Not-selected
packs：concurrency（本 change 恰是把并发防护显式归为 operator-gated 文档
边界）、migration。

## 1. Implementation

- [ ] 1.1 `openspec/changes/node22-scheduler-registry-refresh/design.md`
      D7#7（`:228-240`，要改的断言句在 `:234-240`）：删除"stopped at
      commit time by the `expected_preimage` CAS"对 manual publisher 的
      断言，改为事实措辞——manual publisher 并发为 operator-gated
      （runbook 显式禁令 + CLI 启动 WARNING）；`expected_preimage` CAS
      参数仅由内部 refresh runner 使用，CLI `main()` 不 populate。
      `:228-233` 的 refresh-vs-refresh `refresh_lock` 叙述保留不动。
- [ ] 1.1b（评审 P1-2）同一 change 的
      `specs/scheduler-registry-refresh/spec.md:5-8`：从
      expected-preimage writer 列表移除 "manual"（destination-lock 串行
      化部分保留——manual CLI 提交时确实取该锁）；顺带读 `:41-47` 场景
      "Timer manual and lifecycle writers contend"——其 "the contender
      replaces no canonical bytes" 对 manual CLI 仅在锁窗口重叠时成立，
      snapshot→commit 窗口不成立；措辞如需收窄一并改，若判定超范围则在
      proposal Out of Scope 明写并留 follow-up 记录，不得默认无视。
      改后重跑 3.1 strict validate。
- [ ] 1.2 `docs/runbooks/current-production-ops.md` 手动 publisher CLI 段
      （`:613-617` 一带）：新增显式条目——
      `nhms-scheduler-file-provider-refresh.timer` 活跃期**严禁**运行
      manual publisher CLI（CLI 路径无 CAS 防护，会静默覆写 refresh 产
      物）；附成对确认命令（评审 P2-3，对齐 `:696-697` 既有写法）
      `systemctl --user status nhms-scheduler-file-provider-refresh.timer
      nhms-scheduler-file-provider-refresh.service --no-pager`，判据为
      **timer inactive/disabled 且 service 非 activating/active**（oneshot
      service 可能在 timer 停后仍在执行）才可运行 CLI；跑完恢复 timer。
      中文、贴合该段既有风格。
- [ ] 1.2b（评审 P1-1）`docs/runbooks/current-production-ops.md:357-359`
      §3.1.2：删除"CLI 与 timer 等共用 expected-preimage 检查、并发者不
      会覆盖较新权威内容"的断言，改为——CLI 提交时仅短暂持有
      destination lock、不传 expected_preimage，对 refresh timer 的并发
      保护是 operator-gated（指向 §manual publisher CLI 条目），非代码强
      制；lock 与其余 writer 的 preimage 叙述保留不动。
- [ ] 1.3 `scripts/publish_scheduler_file_registry.py` `main()`：WARNING
      打印位置在 `_parse_args` 之后、任何 I/O 与 gate 计算之前（argparse
      usage error exit 2 无 WARNING，属界定内，评审 P2-5）；无条件向
      stderr 打印一行（风格镜像 `--allow-uncovered-cutover` banner），
      内容含 timer 单元名与"确认 timer/service 非活跃 / 见 runbook"指
      引；文案**不得包含**子串 `allow-uncovered-cutover`（评审 P2-4，保
      `:1373` bypass banner 断言区分度）。不改变任何退出码与后续输出。

## 2. Tests (requirement-driven)

- [ ] 2.1 warning 钉（成功路径）：既有成功 e2e 之上断言 stderr 含 WARNING
      行（含 `nhms-scheduler-file-provider-refresh.timer` 关键子串）。
      红证：pre-change 无该行。
- [ ] 2.2 warning + JSON 共存钉（失败路径）：任一确定性失败参数下，stderr
      同时 (a) 含 WARNING 行，(b) `strip().splitlines()[-1]` 仍解析出既有
      JSON 载荷且字段不变。红证：pre-change (a) 失败。
- [ ] 2.3 既有全量：`uv run pytest -q
      tests/test_publish_scheduler_file_registry.py` 全绿，零删除零弱化
      （评审核实：本 CLI 无整段 stderr JSON 解析断言，全部 last-line/子
      串模式）。`:1372` 的 `assert "WARNING" in err` 在新增启动 WARNING
      后对所有 run 恒真——收紧为对 bypass banner 特征串的断言（不删断
      言，评审 P2-4）。

## 3. Verification (issue 验收标准)

- [ ] 3.1 `openspec validate node22-scheduler-registry-refresh --strict
      --no-interactive` 通过（design.md 改后）。
- [ ] 3.2 `openspec validate operator-gate-manual-publisher --strict
      --no-interactive` 通过。
- [ ] 3.3 `uv run pytest -q tests/test_publish_scheduler_file_registry.py`
      全绿（附计数）。
- [ ] 3.4 `uv run ruff check .` 通过；`npx markdownlint-cli2
      docs/runbooks/current-production-ops.md` 干净。
- [ ] 3.5 scope 核查：diff 仅触及 Impact 列出的文件。
