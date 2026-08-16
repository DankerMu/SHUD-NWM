# Tasks: artifact-guard-symlink-loop-roots (#1402)

## Fixture triage

- Issue 无 upstream `Suggested fixture level`（来源为 PR #1399 review 路
  由，非 stage-change-pipeline）；orchestrator 定 **compact**（单文件单
  lane 判据改造 + 全新测试面、零重判），记录在案。
- Minimal mergeable slice = 全部（S 规模不可再切）。

## Tasks

- [x] 0. 运行时探针（先于实现，结果记入 PR 偏离记录）：
  - (a) **双臂**复证 issue 证据 2：真实环 root 下
    `_local_artifact_allowed_roots` 现状——本机 venv（3.13+ 收编臂）+
    `uv run --python 3.11`（≤3.12 RuntimeError 逃逸臂 = 生产臂；fixture
    review P2-3：3.11 可装，onnxruntime==1.19.2 有 cp311 wheel）。
  - (b) `os.path.realpath(<loop>, strict=True)` → `OSError` 且
    `errno==ELOOP`（两臂同型）；`os.path.realpath(<loop>)`（非 strict）
    → 不抛不死循环（D1 回退臂前提）。
  - (c) ENOENT root：strict 抛 ENOENT、非 strict 词法规范化——现行
    admitted 语义可保；`"<不存在>/../<loop>"` 形态经 ENOENT 臂后仍带环
    （D0 `_path_is_relative_to` 残留逃逸的前提）。
  - (d) `_path_is_relative_to` 本模块消费者 grep 复证仅 `:1093` 一处。
  - 任一探针与 design 断言不符 → 停下报告重裁。
- [x] 1. `_realpath_or_none` helper（design D1）+ lane 三处 resolve 替换
  （`:1091`/`:1120`/`:1133`）；`_path_is_relative_to` 改纯词法；lane 内
  不再出现任何形式 `Path.resolve()`（AC-1）。
- [x] 2. tri-state 出口分流（design D2 表 #1-#6 逐行 + 优先级规则：root
  故障 > path 故障）：`_local_artifact_allowed_roots` 返回
  `(roots, any_root_unresolvable)`；`_artifact_uri_missing_status` local
  腿新 reason `local_artifact_root_unresolvable`；未配置空 roots 既有分
  支不变（D2 #5b）。
- [x] 3. 修复通道拒收接线核对（**forcing 腿**，design D4 #4）：新 reason
  非空即被 `scheduler_candidates.py:1617` 拒收（预期零代码改动，测试钉
  住——若需改动即停下报告）；注意 copyback 腿走不到该拒收点，不得用
  copyback 腿充当本证。
- [x] 4. runbook：`docs/runbooks/current-production-ops.md` unsafe_reason
  路由表补 `local_artifact_root_unresolvable` 条目（路由语义：查 root 本
  身——symlink 环/权限/NFS ESTALE/挂载，非查产物摆放；显式写明非 ELOOP
  errno 也落此 reason，design D2 尾注；既有三 reason 条目不动）。
- [x] 5. 新增测试（design D4/D5 seams 1-10）：单元矩阵 + 出口表逐行
  （含 5a/5b 两行分开断言）+ copyback e2e（pass 存续 + 邻座候选判别断
  言）+ forcing 腿修复通道拒收 + path/root 两类可区分（roots 全可解析限
  定）+ seam 10 源码断言与调用者级钉。真实 symlink 环用 pytest
  tmp_path 构造。既有三条回归锁（`:12330`/`:12520`/`:12563`）保持绿。
- [x] 6. 红证（design D6 R1/R2/R3，R1 **双臂** receipt 留存：本机 3.13+
  臂 + `uv run --python 3.11` 生产臂）：输出留存 + `git stash list` 空核
  验。
- [x] 7. 回归：`uv run pytest -q tests/test_production_scheduler.py -k
  "artifact or symlink"` 全绿；`uv run pytest -q
  tests/test_production_scheduler.py` 全量全绿；**`uv run --python 3.11
  pytest -q tests/test_production_scheduler.py -k "artifact or symlink"`
  生产臂全绿（receipt 留存）**；`uv run ruff check .`；`openspec
  validate artifact-guard-symlink-loop-roots --strict --no-interactive`。
- [x] 8. AC 对照自审：issue #1402 七条 AC 逐条映射（AC-7 以 diff 证明
  `:1059` `path.exists()` 存在性判据未改——`:1057` 调用行因签名改造被
  动属预期，写入偏离记录）；`_path_is_relative_to` 一并治的具名边界偏离
  （design D0 P2-5 证据）写入 PR body；偏离/路由写入 PR body。

## Required evidence (maps every selected pack)

- oracle-integrity：task 0 探针 + task 6 三组红证 + e2e 判别断言。
- spec-compliance：spec delta 四场景 ↔ seams 映射 + runbook 条目 +
  task 8 AC 对照。
- terminal-state-semantics：D2 出口表逐行测试 + seam 8 pass 存续。

## Non-goals

见 design "Non-goals"。
