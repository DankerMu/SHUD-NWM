# pin-frontend-ci-filter

## Why

`.github/workflows/ci.yml` 的 `frontend` paths-filter 是 Frontend Build 与分片 Frontend E2E (mocked) 两条车道的唯一开关。它的三条条目（`.github/workflows/ci.yml`、`apps/frontend/**`、`openapi/**`）没有任何测试钉住：条目被删或被挪到别的 filter 块，没有测试会红，之后只改 workflow 的 PR 会在两条前端车道都被跳过的状态下变绿（#2863 的可实施部分）。`backend` / `database` 块的同类不变量都有块级正反钉。

## What Changes

- `tests/test_select_ci_tests.py`：用已有的 `_frontend_filter_block()` 加块级钉（三条字面量在 `frontend:` 块内），配一条构造文本的反向用例（条目被删、被挪到别的块时钉必须红）。

不改 `ci.yml`、`scripts/select_ci_tests.py`、前端。#2863 里“是否启用 noUnusedLocals / knip / eslint / actionlint”需 owner 决策，不在本 change。design.md 省略（none）。

## Triage

```text
Issue type: test
Fixture level: none
Upstream suggested level: none (agree)
Blast radius: 仅一个 pytest 文件
Selected risk packs: none
Evidence floor: 新用例通过；反向用例证明钉能红；ruff
```

## Impact

- 受影响文件：`tests/test_select_ci_tests.py`。
- 受影响规格：`ci-merge-gates`。
