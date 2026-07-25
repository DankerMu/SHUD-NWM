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
      D7#7（`:231-240`）：删除"stopped at commit time by the
      `expected_preimage` CAS"对 manual publisher 的断言，改为事实措辞——
      manual publisher 并发为 operator-gated（runbook 显式禁令 + CLI 启动
      WARNING）；`expected_preimage` CAS 参数仅由内部 refresh runner 使用，
      CLI `main()` 不 populate。保留 refresh-vs-refresh 的 `refresh_lock`
      叙述不动。
- [ ] 1.2 `docs/runbooks/current-production-ops.md` 手动 publisher CLI 段
      （`:613-616` 一带）：新增显式条目——
      `nhms-scheduler-file-provider-refresh.timer` 活跃期**严禁**运行
      manual publisher CLI（CLI 路径无 CAS 防护，会静默覆写 refresh 产
      物）；附确认命令 `systemctl --user status
      nhms-scheduler-file-provider-refresh.timer --no-pager`（停用则先
      `systemctl --user stop`，跑完恢复）。中文、贴合该段既有风格。
- [ ] 1.3 `scripts/publish_scheduler_file_registry.py` `main()` 入口（在
      任何工作之前）：无条件向 stderr 打印一行 WARNING（风格镜像
      `--allow-uncovered-cutover` banner），内容含 timer 单元名与"确认
      timer 非活跃 / 见 runbook"指引。不改变任何退出码与后续输出。

## 2. Tests (requirement-driven)

- [ ] 2.1 warning 钉（成功路径）：既有成功 e2e 之上断言 stderr 含 WARNING
      行（含 `nhms-scheduler-file-provider-refresh.timer` 关键子串）。
      红证：pre-change 无该行。
- [ ] 2.2 warning + JSON 共存钉（失败路径）：任一确定性失败参数下，stderr
      同时 (a) 含 WARNING 行，(b) `strip().splitlines()[-1]` 仍解析出既有
      JSON 载荷且字段不变。红证：pre-change (a) 失败。
- [ ] 2.3 既有全量：`uv run pytest -q
      tests/test_publish_scheduler_file_registry.py` 全绿，零删除零弱化
      （若个别既有断言对 stderr 做全文 JSON 解析而非 last-line——先核实，
      如有则以等价 last-line 解析修正并在偏离记录说明，不得删断言）。

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
