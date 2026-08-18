## Why

Issue #1384：`tests/test_node27_timeseries_retention.py` 的 `_DESIGN_PATH` 硬编码
PENDING change 路径。#855 归档后三条 H4/H6 字节一致性测试 FileNotFoundError 集体红，
且归档 PR（纯 openspec/**）不触发任何后端 CI —— 破坏在 merge 后 master 全量 run 才炸。

## What Changes

- `_DESIGN_PATH` 常量替换为 `_resolve_design_path()` helper：pending 优先，
  `archive/*-<slug>/design.md` glob 回退取最新；两处皆缺 `pytest.fail` 给明确指引。
- 三个读取点改走 helper；新增 3 条 tmp_path 双形态单测（pending 优先 / archive 回退 /
  双缺失大声失败）。

## Non-Goals

- H4/H6 断言语义本身；#855 归档时机；runbook 与 design 内容同步。

## Risk triage

- Fixture level: none（test-only 路径解析）。Repair intensity: low。
- Risk packs: test-evidence selected（本 issue 即守卫可用性）；其余 not selected。

## Must preserve

- 现 pending 形态下 149 条测试全绿不变。
