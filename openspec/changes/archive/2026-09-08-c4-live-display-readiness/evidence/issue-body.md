Part of #1891
Prerequisite for #1895
Depends on #1970

Implementation Ready: yes

OpenSpec change: `c4-live-display-readiness`
Suggested fixture level: expanded
Repair intensity: high
Minimal mergeable slice: atomic — 一个可执行的 C4 producer→validator→private publisher→binder 接受链；readonly `/ops` 是真实浏览器运行的必要前提。
Width exception: multi-path - browser、RBAC、文件系统测试是同一 C4 接受链的故障注入，不是独立业务交付；拆开会留下没有可用生产入口或缺少接受校验的半套证据能力。

## Module / Scope

独立 C4 live-display browser evidence 能力，包含必要的 readonly `/ops` 可达性。本项来自 #1895 readiness review breadth/round-ceiling 的拆分纠正；原工作树没有 PR，历史轮次未完整记账，不能借子 issue 追溯宣称合规。

## Current / Desired Behavior

现有 monitoring 与 river-click lane 各有不同合同，不能代表 C4 对真实 `/`、GFS/IFS strict `/ops`、job/log identity 和无控制副作用的证明。新增独立显式配置入口与完整私有 receipt 接受链；没有 live 事实时拒绝 PASS。

## In Scope

- `test:e2e:live-c4-display` shell/config/setup/spec、C4 lane owner 与有界网络/DOM 观察。
- C4 schema/examples、semantic validator、私有 no-clobber publisher、五输入/秒级 bracket/POSIX facts binder；端到端 SHA/digest 仍由 #1895 G0/C3 接受链强制。
- `/ops` viewer 仅在 runtime 同时报告 `service_role=display_readonly` 和 `display_readonly=true` 时可达，其它权限不变。
- 共享 TS publisher 的 river-click 兼容、普通测试不依赖 Chromium、schema-only frontend CI 路由。

## Out of Scope

不改 Python readiness/C1–C3/census/installer/timer/performance；不部署、不访问 node-27、不执行 census/probe/install；不改河段点击 1+20/P95 合同；不关闭 #1895/#1891。

## Tasks

- [ ] 显式切出完整 C4 文件与 `tests/test_select_ci_tests.py` 的 C4 helper+assertion；保留其余 WIP 与保护路径。
- [ ] 基于最新 master 验证独立切片；不混入当前分支已有 readiness commits。
- [ ] 完成 `tasks.md` 的 Evidence Floor、frozen-SHA review/verdict ledger、最终 Gap Sweep、CI 和中文证据。
- [ ] 合并未部署能力，向 #1895 交付明确依赖；live 验收仍由其维护窗口完成。

## Acceptance Criteria

- [ ] 显式五输入启动独立真实 C4 lane；缺输入、role override、mock/HAR、不完整或过期证据不能变成 PASS。
- [ ] 合法双源运行完成 home/current-read 与 strict ops status/stages/jobs/logs，允许合法多 stage jobs；loading/quiet/body/evaluate 均有界，正常非必需 abort 不误拒，required failure/source error/权限错误/控制请求阻止 PASS。
- [ ] schema/semantic/binder 的闭集、identity、隐私、秒粒度 bracket、POSIX facts 一致；交付 frozen-SHA 与 #1895 G0/C3 的 reviewed-SHA/C4 sha256 绑定义务仍强制保留，不把 C4 CLI PASS 当外层门通过；输出私有0700/0600、single-link、no-follow/no-clobber、durable readback，故障不覆盖旧文件、不清理他人对象。
- [ ] readonly runtime 双字段 viewer 的 `/ops` 例外不扩散；其它 RBAC/control 与 river-click 既有接受链契约保持。
- [ ] frontend 单测/类型/构建、schema 正负矩阵、CLI/static contract 与 CI scope 通过，证据绑定实际切片 SHA；普通单测不要求 Chromium。

## PR Boundary

仅 C4 frontend/shared TS/schema/必要 readonly ops/CI/test/spec/docs。不得带入 Python readiness 或完整 cold rollout。顺序为本项 → 完整 #1895 readiness PR → node-27 维护窗口。

先合并代码能力不等于生产验收：完整 readiness 合并之前禁止 node-27 访问；生产部署与 C1–C4 live receipt 仍由 #1895 完成，共享 `compressed-chunk-cold-tablespace-tiering` change 保持 active。本项不关闭 #1895/#1891，不把 local/mock/API-only/历史 receipt 冒充 live PASS。

## Required Reading

- `openspec/changes/c4-live-display-readiness/{proposal.md,design.md,tasks.md,specs/**}`
- `openspec/changes/compressed-chunk-cold-tablespace-tiering/evidence/issue-1895-readiness-review-retro-20260907.md`
- `CLAUDE.md`、`openspec/project-profile.md`
- `docs/runbooks/node-27-bringup-checklist.md`（仅 C4 生产验收口径；本项不运行远端）

## Evidence Floor

完整矩阵以上述 change `tasks.md` §3 为准：配置/CLI、fake-page 和隔离 VM、RBAC/store、schema/semantic/binder、private publisher 及 river-click 兼容、CI 路径范围。每项有显式合法/非法输入和预期输出；历史 798 项 frontend green 不是本切片整合 master 后的证据。

## Verification:

本地串行去重执行：

- frontend 目录：空 browser cache 下 `corepack pnpm test`；`corepack pnpm typecheck`；`corepack pnpm exec tsc --noEmit -p tsconfig.node-playwright.json`；`corepack pnpm build`。
- C4 schema metaschema/examples：按 `.github/workflows/ci.yml` 的 `check-jsonschema` 命令；AJV negatives 与 binder/semantic tests 随 frontend suite。
- `openspec validate c4-live-display-readiness --strict --no-interactive`。
- `uv run ruff check tests/test_select_ci_tests.py` 与 `git diff --check`。
- Python CI-scope 针对测试由 CI 执行；后端/真实 DB oracle 仍遵循 node-27 节点纪律，不在本项访问远端。
- frozen-SHA comprehensive review、independent verdicts、Gap Sweep、origin branch-tip 一致及 required CI。

生产 `test:e2e:live-c4-display`、C1–C4 live receipt 是 #1895 完整 readiness merge 后的强制后续验收，不属于本前置项的已验证事实，也不被取消。

## 偏离记录

此项为已有 WIP 的事后拆分，fixture review 在拆分阶段补做，不能追溯称最初开发流程合规。此前禁区只读遍历、stash 违规、越界 entropy 修复（已还原）、reviewer 角色与轮次账缺陷详见父 retro。没有真实 live receipt，也未部署 readonly `/ops` 变更。
