# workflow-actionlint-lane

## Why

`.github/workflows/` 下 5 个 workflow 没有任何静态检查（#2863 的决策项 (2)）：表达式拼错、`needs` 指向不存在的 job、`run:` 里的 shell 错误都只能等真跑到那一步才发现。owner 裁定（2026-10-11，issue 评论）：启用 actionlint，作为 `ci.yml` 里按 `.github/workflows/**` 路径 scope 的 job，不进每个 PR 都跑的车道。存量实测为 0（actionlint v1.7.7 + shellcheck，2026-10-10）。

## What Changes

- `ci.yml` 的 `changes` job 增加 filter `workflows`（`.github/workflows/**`）与对应 output。
- 新 job `workflow-lint`（显示名 “Workflow Lint”）：`needs: changes`，`if: needs.changes.outputs.workflows == 'true'`，checkout 后用钉死版本的官方镜像 `docker://rhysd/actionlint:1.7.7`（自带 shellcheck）检查全部 workflow；`timeout-minutes: 5`。
- `tests/test_ci_shard_tests.py` 增加 YAML 级钉（沿用 #2856 在该文件里的写法）：filter 条目、`changes` 的 output、`workflow-lint` 的 `if` / `needs`、没有 job 依赖它；正反两向。
- `CLAUDE.md` / `AGENTS.md` 的“CI 门控要点”如需补一句，改源 `instructions/agents/` 并重新生成（仅当该节列举了全部按路径 scope 的车道；否则不动）。

design.md 省略（compact）。

## Triage

```text
Issue type: CI gate (new path-scoped lane)
Fixture level: compact
Upstream suggested level: 未给
Blast radius: 只改 workflow 的 PR 多一个秒级 job；其他 PR 不受影响
Selected risk packs: CI gating（新 job 不得成为其他车道的 needs；filter 丢失时元测试要红）
Evidence floor: 本地 actionlint 1.7.7 对 5 个 workflow（含改后的 ci.yml）0 发现；故意写坏一处时非零退出；元测试正反钉；PR 自身的 CI 上 Workflow Lint 实际执行并通过
```

## Impact

- 受影响文件：`.github/workflows/ci.yml`、`tests/test_ci_shard_tests.py`（及可能的指令源文件）。
- 已知局限：`ci.yml` 坏到无法加载时整个 workflow 不跑，lint 也不跑；master 没有 required checks，这条车道是红叉而不是硬阻断。
- 受影响规格：`ci-merge-gates`。
