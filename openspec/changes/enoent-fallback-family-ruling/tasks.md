# Tasks —— ENOENT 非严格兜底的家族级裁定（#1627）

## A —— 裁定文档（编排者）

- [x] A-1 `docs/adr/0009-path-canonicalization-dereference-doctrine.md`：裁定、**三条**从句、
      19 成员快照表（逐行归到具名从句）、wrapper 消费者清单（design D2 / F5）、三个立场的去向、
      D6 两个子族的定性、D5 三条 report-only、以及「三条硬约束我自己违反了三条」这一事实。
- [x] A-2 tracker 指针双边闭合。**终态一律指向 ADR 0009，不指向任何 issue**（本 PR 关闭 #1627）。
      - [x] A-2a `openspec/specs/slurm-array-runner-integration/spec.md:106-112`
      - [x] A-2b `openspec/specs/runtime-evidence-and-operations/spec.md:206-217`
      - [x] A-2c `openspec/specs/runtime-evidence-and-operations/spec.md:233` 所属 requirement
            ——`archive/2026-09-02-journal-root-realpath-and-job-id-scope-census/design.md:152-155`
            把 `_db_free_path_check` 的义务派给了 #1627 并指向该 requirement（F5）。
            **裁定：该 requirement 正文不含 #1627，无需改写**；缺的是可追溯性，
            已由 ADR「权威集合是守卫的嗅探边界」一节的消费者表闭合。
      - [x] A-2d 以下 5 处 **tracker 语义**的活引用（实现者改，见 B-6）：
            `services/orchestrator/journal_root_authority.py:79`、
            `tests/test_scheduler_journal_root_authority.py:7`、`:134`、`:219`、
            `tests/test_production_scheduler.py:18383`
      - [x] A-2e 以下**不改**（属出处/沿革，非 tracker）：`services/orchestrator/scheduler_core.py:24`、
            `docs/adr/0003-review-lens-rotation-keep.md:185`、`openspec/changes/archive/**`
      - [ ] A-2f 合并前重跑全仓 `grep -rn '#1627'`，结果随 PR body 交付
- [x] A-3 `openspec/specs/job-retry-mechanism/spec.md` 的立场 C 措辞：按验收项「保留或改写，
      二选一并说明」——**保留**，ADR 写明它已被实核为立场 B 的实例（依从句 1，`:1401`）。
- [x] A-4 `openspec validate enoent-fallback-family-ruling --strict --no-interactive` 通过。

## B —— 守卫与注释（implementer）

- [x] B-1 AST 扫描器：枚举「调用 `os.path.realpath` 的 (模块, 函数) 对」，形状无关，
      覆盖 `services/`、`workers/`、`packages/`、`apps/` 四棵树。
- [x] B-2 守卫测试：断言集合内每个成员**至少有一处 `strict=True`**；违规者集合为空。
      **具名白名单恰好三项**（每项带理由注释 + 所依从句）：
      `journal_scope_census::_require_output_outside_root`（从句 2）、
      `shud_preflight::check_shud_executable`（从句 1）、
      `scheduler_config/db_free.py::_db_free_path_identity`（从句 1）。
      断言违规者集合，**不**断言成员清单（design D3）。
- [x] B-3 红证据（两向实跑，不接受结构性论证）：
      - [x] B-3a 新增一个只写非严格 realpath 的桩 → 守卫失败；删桩 → 恢复绿。
      - [x] B-3b 从白名单删掉 `_db_free_path_identity` → 守卫失败；恢复 → 绿。
            （证明三项白名单**每一项都承重**，不是凑数。）
- [x] B-4 `services/orchestrator/scheduler_config/path_modes.py:117-119` 注释补 ADR 0009 指针。
- [x] B-5 `services/orchestrator/journal_scope_census.py` 在 `:557` 的 docstring 留一条锚：
      该站点依从句 2，且该从句**不覆盖 check-then-act 竞态**（design D5.2）。
- [x] B-6 A-2d 的 5 处 tracker 引用改指 ADR 0009。**只改措辞，不动任何断言或行为。**

> **定性已在 fixture 内完成，不再派给实现任务**：`_safe_preserve_final_component`
> 的消费者链与跨解释器残留见 design D6，实现者无需复核，也不得据此改行为。

## Evidence Floor

- [x] EF-1 `uv run ruff check .` 干净。
- [x] EF-2 `uv run pytest -q <守卫测试文件>` 通过。
- [x] EF-3 B-3a / B-3b 的红证据两向实测输出（不是论证）。
- [ ] EF-4 `openspec validate enoent-fallback-family-ruling --strict --no-interactive` valid。
- [x] EF-5 权威集合基数实测值随 PR body 交付；若与 design D2 的 19 不符，
      **以实测为准并改 design**，不得反向修饰。
- [ ] EF-6 ADR 与 design 内每一条 `#N`、归档路径、file:line 引用均已逐条打开现证，
      核对清单随 PR body 交付（#1626 教训 2；本 change 初稿已在此复发过一次）。
- [ ] EF-7 A-2f 的全仓 `#1627` grep 结果为空或仅剩 A-2e 的沿革引用。
