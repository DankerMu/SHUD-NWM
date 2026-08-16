# Tasks: artifact-guard-path-expanduser (#1424)

## Fixture triage

- Issue 无 upstream `Suggested fixture level`（issue-scribe 产出）；
  orchestrator 定 **compact**（S 规模单 seam、#1348 家族第 3 面、先例
  #1402/#1401 均 compact），偏离记录在案。
- Minimal mergeable slice = 全部（2 行核心 + 测试面不可再拆）。

## Tasks

- [x] 0. 运行时探针（design Task 0 (a)-(d)；(c) 终态不符 → 停下重裁）。
- [x] 1. `_local_artifact_path` 两处改 `Path(os.path.expanduser(...))`
  （design D1；except 臂/reason 码/准入判据零改动）。
- [x] 2. 测试（design D3 seams 1-6）：双触发面用例（chdir 锚定 cwd 于
  roots 外）、byte-compat oracle（输入域排除 home rstrip 后为空的环境）、receiver 判别
  式钉测（`os.path.expanduser` receiver 合法、`<Path>.expanduser()`
  红——非 attr 名全禁）、root/path 对称断言（终态 `(True, None)`）。
- [x] 3. 红证两组（design D4 R1/R2）+ 还原 + `git stash list` 空核验。
- [x] 4. 回归：`uv run pytest -q tests/test_production_scheduler.py`；
  `uv run ruff check .`；`openspec validate artifact-guard-path-expanduser
  --strict --no-interactive`。
- [x] 5. AC 对照自审（issue 六条 AC 逐条映射入 PR body）；偏离记录；
  兄弟副本 `scheduler_preflight.py:534/:587` 不修声明留 PR body。

## Required evidence (maps every selected pack)

- oracle-integrity：task 0 探针 + task 3 红证 + byte-compat oracle。
- terminal-state-semantics：D2 终态表逐行用例（双触发面 + file:// 臂
  no-op + 对称行）。

## Non-goals

见 proposal "Out of scope"。
