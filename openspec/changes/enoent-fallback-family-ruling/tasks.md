# Tasks —— ENOENT 非严格兜底的家族级裁定（#1627）

## A —— 裁定文档（编排者）

- [x] A-1 `docs/adr/0009-path-canonicalization-dereference-doctrine.md`：裁定、**三条**从句、
      19 成员快照表（逐行归到具名从句）、wrapper 消费者清单（design D2 / F5）、三个立场的去向、
      D6 两个子族的定性、D5 三条 report-only、以及「三条硬约束我自己违反了三条」这一事实。
- [x] A-2 tracker 指针双边闭合。**终态一律指向 ADR 0009，不指向任何 issue**（本 PR 关闭 #1627）。
      - [x] A-2a `openspec/specs/slurm-array-runner-integration/spec.md:106-112`
      - [x] A-2b `openspec/specs/runtime-evidence-and-operations/spec.md:206-217`
      - [x] A-2c `openspec/specs/runtime-evidence-and-operations/spec.md` 的 requirement
            「DB-free scheduler config path adjudication survives symlink loops」
            （初稿在此写行号 `:233`，被本 commit 自己对同一文件的编辑移到 `:235`，交叉审查实测发现；已改符号锚）
            ——`archive/2026-09-02-journal-root-realpath-and-job-id-scope-census/design.md:152-155`
            把 `_db_free_path_check` 的义务派给了 #1627 并指向该 requirement（F5）。
            **裁定：该 requirement 正文不含 #1627，无需改写**；缺的是可追溯性，
            已由 ADR「权威集合是守卫的嗅探边界」一节的消费者表闭合。
      - [x] A-2d 以下 5 处 **tracker 语义**的活引用（实现者改，见 B-6）：
            `services/orchestrator/journal_root_authority.py:79`、
            `tests/test_scheduler_journal_root_authority.py:7`、`:134`、`:219`、
            `tests/test_production_scheduler.py:18383`
      - [x] A-2e 以下**不改**（属出处/沿革，非 tracker）。C-1 接线后该类引用从 3 条增至 40+ 条，
            均为 selector 路由的出处注释，不是 tracker：`services/orchestrator/scheduler_core.py:24`、
            `docs/adr/0003-review-lens-rotation-keep.md:185`、`openspec/changes/archive/**`
      - [x] A-2f 合并前重跑全仓 `grep -rn '#1627'`，结果随 PR body 交付。
            **实测：零条 live tracker 指针**；剩余全部是沿革注释（`scripts/select_ci_tests.py` 与
            `tests/test_select_ci_tests.py` 的 C-1 路由出处、`scheduler_core.py`、
            `docs/adr/0003`、ADR 0009 自述其单号），与 `#1656`/`#2185` 在同一文件里的写法同源。
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

## C —— 交叉审查第 1 轮长出来的任务

- [x] C-1 守卫接入 PR-time CI 选择（`scripts/select_ci_tests.py` supplemental 路由 +
      `tests/test_select_ci_tests.py` 从守卫自己的 `_SCAN_ROOTS` AST 推导所需根的元测试）。
      第 1 轮实测：8 条路径全部不选中该守卫，即「新站点被拦」这个场景在合并门上恒绿。
- [x] C-2 **P0**：15 个 admit 站点里 13 个没有记录所依从句，而 delta spec 的 SHALL 要求
      「recorded at the site」——交付该条款的提交自己违反它。处置：19 个权威成员各写
      `ADR 0009 clause N` / `ADR 0009 loop-filtered` 处置标记 + 守卫加第二条断言
      （无标记者集合为空，函数区间内检索，裸 `ADR 0009` 不算过）。
- [x] C-3 live spec 的跨解释器 `lstat` 实测断言补仓内凭据（本 PR 已两次栽在转抄未实测结论上）。

## Evidence Floor

- [x] EF-1 `uv run ruff check .` 干净。
- [x] EF-2 `uv run pytest -q <守卫测试文件>` 通过。
- [x] EF-3 B-3a / B-3b 的红证据两向实测输出（不是论证）。
- [x] EF-4 `openspec validate enoent-fallback-family-ruling --strict --no-interactive` valid。
- [x] EF-5 权威集合基数实测值随 PR body 交付；若与 design D2 的 19 不符，
      **以实测为准并改 design**，不得反向修饰。
- [x] EF-6 ADR 与 design 内每一条 `#N`、归档路径、file:line 引用均已逐条打开现证，
      核对清单随 PR body 交付（#1626 教训 2）。**本 change 在此复发了六次、跨三轮**：
      初稿一次、交叉审查第 1 轮两次、实现任务插入 19 条处置标记时又顶走一批。
      第三轮不再追着补行号——指向源码符号的锚已全部改为 `模块::函数` 符号锚。
      C-3 收窄 live spec 措辞的依据同属此项。
- [x] EF-7 A-2f 的全仓 `#1627` grep 结果为空或仅剩 A-2e 的沿革引用。
- [x] EF-8 C-1 的 selector before/after 实测表（接线前后各路径选中数 + 守卫是否被选中）。
- [x] EF-9 C-2 的红证据三向实跑：删标记 → 红；标记退化为裸 `ADR 0009` → 红；
      标记挪到函数体外 → 该成员仍报违规。三者还原后均绿。
