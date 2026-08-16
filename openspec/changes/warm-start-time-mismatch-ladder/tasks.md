# Tasks: warm-start-time-mismatch-ladder (#1431)

## Fixture triage

- Issue readiness=needs-triage，唯一缺口（A/B/C 语义裁决）已由 owner 拍
  板走 C；fixture level=compact（偏离记录：无 upstream suggested level）。
- Minimal mergeable slice = 全部（转译 + 计数 escalate + 锁改写不可拆：
  只转译不 escalate 即纯 A，丢系统性信号）。

## Tasks

- [x] 0. 运行时探针（design Task 0 (a)-(e)；任一不符 → 停下重裁）。
- [x] 1. 腿层：`:1298-1300` 转译臂扩展（exact-warm 清 staged 后裸
  raise + token 前缀消息）+ 标志 while 体首行复位 + 双计数（URI-only
  计入）+ pending mark 缓存与出口 flush 语义 + `:1333` 耗尽分派（≥2 且
  全 mismatch → `WARM_START_TIME_MISMATCH_SYSTEMIC`，先清 staged、不
  flush mark）+ `:60-68` 模块注释改真（design D1 全 7 条）。
- [x] 2. 测试（design D3 seams 1-11）：阶梯续跑（含 mark flush 断言）/
  单 mismatch 冷启动 / systemic escalate（含 usable_flag 未落断言）/
  混合拒因双顺序 / exact-warm 原码+清 staged / 合法降级复用保绿 / 两处
  旧锁显式改写（`:704-731`/`:761-780`）/ 工作区卫生 / 形状畸形回归锁 /
  retry 分类 parity / warm 出口 flush。
- [x] 3. 红证两组（design D4 R1/R2）+ 还原 + `git stash list` 空核验。
- [x] 4. 回归：`uv run pytest -q tests/test_warm_start.py
  tests/test_warm_start_chaining.py`；`uv run pytest -q
  tests/test_shud_runtime.py`（同文件邻域）；`uv run ruff check .`；
  `openspec validate warm-start-time-mismatch-ladder --strict
  --no-interactive`。
- [x] 5. AC 对照自审（issue 七条 AC 映射入 PR body；AC-4 的实码口径修正
  与 escalate 阈值 N=2 偏离显式声明）。

## Required evidence (maps every selected pack)

- terminal-state-semantics：D2 七行逐格用例 + escalate 新终态 + 三类坏
  快照口径统一记录（design Risk triage）。
- oracle-integrity：Task 0 探针 + R1/R2 红证 + 旧锁改写前后语义对照 +
  合法路径保绿。

## Non-goals

见 proposal "Out of scope"。
