# Tasks: raw-manifest-probe-unification (#1393)

## Fixture triage

- Issue 无 upstream `Suggested fixture level`（issue-scribe 产出）；
  orchestrator 定 **compact**（S/M 规模、单 lane 两调用点、#1365 家族续
  面），偏离记录在案。
- Minimal mergeable slice = 全部（两腿 + 弃权定性 + 容器不可拆：拆任一
  腿都留下 fail-open 面）。
- 对 issue 解决思路（blocked/manual 臂）的具名偏离见 design「Risk
  triage」，PR body 的 AC 对照必须显式引用。

## Tasks

- [x] 0. 运行时探针（design Task 0 (a)-(e)；任一不符 → 停下重裁）。
- [x] 1. 腿层：两处调用改走 `_artifact_uri_missing_status`，unsafe 非
  null 一律弃权 `return None`（design D1/D2）；`:1027-1028` 豁免注释改
  真；`_object_manifest_is_missing` 与决策层零改动。
- [x] 2. 测试（design D3 seams 1-9）：downstream 假存在面消失、瞬时可用
  性回归锁、repair 弃权具名限制、ObjectStoreError × run_once 存活、
  byte-compat（真锚点 `:20338`/`:20417` + 等价断言）、探针接线 spy、残
  余臂、**迁移约 10 个无 root monkeypatch 用例（per-test 真实 tmp_path
  root、断言零改动，改断言即停）**、permanent-refused 梯子回归锁。
- [x] 3. 红证两组（design D4 R1/R2）+ 还原 + `git stash list` 空核验。
- [x] 4. 回归：`uv run pytest -q tests/test_production_scheduler.py`；
  `uv run ruff check .`；`openspec validate raw-manifest-probe-unification
  --strict --no-interactive`。
- [x] 5. AC 对照自审（issue 五条 AC 映射入 PR body；AC-1/AC-2 的弃权偏
  离显式声明 + 理由）。

## Required evidence (maps every selected pack)

- oracle-integrity：task 0 探针 + task 3 红证 + byte-compat + seam 8 迁
  移前后双绿 oracle。
- terminal-state-semantics：D2 五行 × 两腿 × 终态列逐格用例 + 瞬时可用
  性锁 + run_once 存活。

## Non-goals

见 proposal "Out of scope"。
