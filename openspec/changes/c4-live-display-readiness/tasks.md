## 1. Split contract and fixture

- [x] 1.1 将 C4 完整能力重入 implementation-ready 子 issue #2123，明确 #1895 依赖、独立 PR 边界及父 retro；issue body 的 `Verification:` 字段列明 §3 验证命令并引用完整 Evidence Floor，原样保留 live 验收后置及“不关闭 #1895/#1891”声明；单轮轻量 alignment PASS（`evidence/issue-alignment.md`）。历史轮次/PR 缺失的结构化 sizing-retro 限制保留在父 retro，不伪造数字。
- [x] 1.2 完成只读 fixture review 与本 change strict validation；记录事后拆分与旧 review 非 SHA-bound 的偏离（`evidence/fixture-review.md`）。

Suggested fixture level: expanded
Minimal mergeable slice: atomic — C4 producer/validator/publisher/binder 与必要 readonly ops，不能发布半条接受链。
Width exception: multi-path - 同一接受链的 browser/RBAC/filesystem 故障注入不是独立业务能力。

## 2. Preserve and isolate implementation

- [x] 2.1 显式保存 C4 frontend/schema/CI 切片（C4 保存点 `0ed538ba`，readiness 保存点 `2f7e95b4`）；混合文件 `tests/test_select_ci_tests.py` 必须同时包含 `_frontend_filter_block` 辅助函数和 `test_c4_schema_only_change_runs_frontend_ajv_negative_suite` 断言，两段可能位于同一 diff hunk，均不可遗漏（分配见父 change 的 review retro）。`scripts/select_ci_tests.py` 增量全部留给 readiness；保留所有 readiness WIP 和保护路径，不 stash/reset/checkout/clean，不携带缓存。
- [x] 2.2 基于 master `e6b5e4ffd06c6e6b18824ec51082ff4965a1c42a` 创建独立 #2123 分支并仅移植 C4，自动合并无冲突，diff 不含 Python/readiness 实现；仅额外携带父 split retro 作溯源。原 readiness 分支及 design 全部保留。
- [x] 2.3 对照 Invariant Matrix 核对现有实现与测试，补齐切片使用文档和真正缺失的契约，不重写已完成能力。

## 3. Evidence Floor

- [x] 3.1 frozen slice 的 frontend `corepack pnpm test`（空 browser cache）+ `corepack pnpm typecheck` + `corepack pnpm exec tsc --noEmit -p tsconfig.node-playwright.json` + `corepack pnpm build`，串行去重执行，提供 exact SHA 与输出。
- [x] 3.2 schema metaschema/examples 按 CI check-jsonschema 命令通过；AJV negatives 对 PASS/BLOCKED/FAIL 分支、必填/额外字段/身份/隐私错误确实拒绝；semantic/binder 测试承担跨字段关系。
- [x] 3.3 C4 lane/owner 测试：双源成功、multi-stage jobs 成功；loading 等待、source error/权限/runtime/required response 失败、headers-only/body/evaluate/quiet deadline、正常 abort 与 required failure 分流、Slurm/非 GET 控制请求均以预期 terminal 状态结束。
- [x] 3.4 C4 publisher/binder + river-click sibling：symlink/parent swap/nonregular/nlink/权限/并发/已有目标/读回变化/oversize/depth/JSON/UTF-8/原生错误与 FD 覆盖，旧目标无覆盖；C4 输入/bracket/POSIX facts mismatch 拒绝。交付 frozen-SHA 见 3.1/4.1；#1895 G0/C3 的 reviewed-SHA/C4 sha256 mismatch 拒绝仍是强制后续验收，不能声称由 C4 CLI 完成。
- [x] 3.5 RBAC/store tests：readonly 双字段 viewer 可达、loading 10s 有界拒绝/晚到恢复；其它 runtime、monitoring/model-assets 权限不放宽；role/retry/cancel 控件不存在。
- [x] 3.6 C4 CLI/config/static contract：五输入缺失、role/mock override、普通 test discovery、typecheck include、schema-only CI 路由；Python selector CI hunk 在 PR #2126 run `34174843099` 执行 `tests/test_select_ci_tests.py` 的 593 项断言通过（`evidence/ci-selector.md`），不是 collect-only 冒烟。后端 oracle 仍按仓库节点纪律，不冒充本地生产验证。
- [x] 3.7 `openspec validate c4-live-display-readiness --strict --no-interactive`、改动 Python 文件 Ruff 与 `git diff --check`；与 CI 相同命令每个仅执行一次。新行为 red-proof 不使用 stash/reset/checkout；缺历史输出如实记录，由 implementer 给安全隔离 mutant proof。

## 4. Reviewed delivery

- [x] 4.1 完成含实际切片提交的 bounded comprehensive review、独立 verdict ledger 与最终 Gap Sweep（`evidence/review/`）；最终 push 后的 exact branch-tip、required CI 与 oracle-integrity 是外部 pre-merge 硬门，不能靠勾选本任务替代。
- [x] 4.2 中文 PR/工作说明与完整偏离记录已生成并在最终 push 后以文件方式发布；不关闭 #1895/#1891，不勾其 live tasks。

Post-merge obligation: 合并后以独立 accountability/archive 变更记录最终 merge SHA、关闭 #2123 并归档本 change。该动作不在等待 CI 时通过 docs-only 尾随 commit 注入本 PR，也不改变 #1895 的 live 先后顺序。

## Evidence boundary

历史 798 frontend tests 和相关 Python green 只作线索，不满足本切片整合 master 后的 frozen-SHA Evidence Floor。项目 display live oracle 无豁免：本 PR 只合并未部署能力，生产激活与 node-27 C1–C4 receipt 由完整 readiness merge 后的 #1895 完成。在此之前不得声称本切片生产验收完成。

验证证据：`evidence/phase2.md` 绑定实现提交 `97c7ec4e`。3.6 的 frontend 部分通过，Python CI-scope 断言等待 CI，整项不提前勾选；3.4 的勾选仅对应本切片 C4 契约，不代替 #1895 外层 SHA/digest 验收。
