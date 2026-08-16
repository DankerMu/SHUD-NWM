# Tasks: runtime-root-safety-symlink-loop (#1401)

## Fixture triage

- Issue 无 upstream `Suggested fixture level`（来源为 PR #1399 round-1
  spec-compliance 评审路由，非 stage-change-pipeline）；orchestrator 定
  **compact**（单 helper 判据改造 + 新测试面、零重判、S 规模），记录在
  案。
- Minimal mergeable slice = 全部（S 不可再切）。

## Tasks

- [x] 0. 运行时探针（先于实现，结果记入 PR 偏离记录）：
  - (a) **双臂**复证 issue 证据 2/3：真实环 root 直调
    `_local_runtime_root_safety`——本机 3.14（收编 `(环路, "ok")`）+
    `uv run --python 3.11`（RuntimeError 逃逸；scratch venv 用
    `UV_PROJECT_ENVIRONMENT` 隔离，勿动工作树 .venv）。
  - (b) `os.path.realpath(<loop>, strict=True)` → OSError errno==ELOOP 两
    臂同型；非 strict 不抛不死循环（3.14 已证 errno 62）。
  - (c) ENOENT root：strict 抛 ENOENT；非 strict realpath 与旧
    `str(Path.resolve(strict=False))` byte-compat（3.14 已证相等，3.11 复
    证）；phantom `"<missing>/../<loop>"`：**旧行为双臂对照**（3.13+ 收编
    `(带环值, "ok")`；≤3.12 `Path.resolve(strict=False)` 抛
    RuntimeError——P1-1 的退化前提，必须实测）+ 新 loop-filtered 复查两
    臂拒收；`"<missing>/../<real>"` 复查干净 admit（D2 4b）。
  - (d) `os.path.expanduser("~<未知用户>/x")` 原样返回不抛（3.14 已证）；
    `Path("~<未知用户>/x").expanduser()` 抛 RuntimeError（rider 前提，两
    臂）。
  - (e) grep 复证 `_local_runtime_root_safety` 唯一调用点 `:1399`；
    journal 腿 import `_resolve_runtime_root_candidate`。
  - 任一探针与 design 断言不符 → 停下报告重裁。
- [x] 1. `_local_runtime_root_safety` 改造（design D1）：strict realpath
  + `except (ValueError, OSError)` errno 分流 + ENOENT 非 strict 回退 +
  loop-filtered 复查臂 + `os.path.expanduser` rider；`ENOENT` 用
  `retry.py:8` **既有** import（勿重复 import，F811）；签名与三个既有
  reason 不变。
- [x] 2. 下游接线核对（design D2 尾注）：`unsafe_rejected` →
  `RETRY_RUNTIME_ROOTS_UNSAFE` + evidence 附着预期零代码改动，测试钉住
  ——若需改动即停下报告。
- [x] 3. 新增测试（design D4/D5 seams 1-9）：`tests/test_retry.py`——单
  元矩阵（D2 行 1-8 + 4b）+ phantom 拒收钉（行 9）+ 三 field 参数化 +
  DB 腿 e2e（error_code + evidence 非空 + reason）+ manifest/比较基准排
  除 + rider 用例，EACCES 带 `geteuid()==0` skip 与 finally 权限复原；
  `tests/test_file_orchestration_journal.py`——db-free 日志腿同形一条
  （UNRESOLVED 供体 `:3802`/`:3985` 换 UNSAFE + 环 root）。62 个既有
  retry 用例全绿（`:1229` UNSAFE 锁 + `:929`/`:974`/`:1270` 重叠守卫
  锁）。
- [x] 4. 红证（design D6 R1/R2/R3/R4；R1 双臂 receipt：本机 3.14 +
  `uv run --python 3.11` scratch venv）：输出留存 + mutation 还原 +
  `git stash list` 空核验。
- [x] 5. 回归：`uv run pytest -q tests/test_retry.py
  tests/test_file_orchestration_journal.py` 全绿；
  `uv run pytest -q tests/test_production_scheduler.py` 全量全绿（issue
  AC-5 字面命令；本 change 不触该文件，跑一次证零跨文件回归）；
  `uv run --python 3.11 pytest -q tests/test_retry.py` 生产臂全绿
  （receipt 留存）；`uv run ruff check .`；`openspec validate
  runtime-root-safety-symlink-loop --strict --no-interactive`。
- [x] 6. AC 对照自审：issue #1401 五条 AC 逐条映射；具名偏离四条写入 PR
  body——rider expanduser、ValueError 防御、phantom loop-filtered 拒收
  （与 #1402/preflight 家族先例分歧 + ≤3.12 退化规避论证）、非 ENOENT
  errno 全版本扩面；`retry.py:1524` expanduser 抛点路由 #1424 评论补面
  （PR 合并前完成并在 body 记录）。

## Required evidence (maps every selected pack)

- oracle-integrity：task 0 探针 + task 4 三组红证 + mutation 还原自证。
- spec-compliance：spec delta 五场景 ↔ seams 映射（design Evidence
  mapping）+ task 6 AC 对照。
- terminal-state-semantics：D2 出口表逐行测试 + DB 腿错误码/evidence 终
  态断言。

## Non-goals

见 design "Non-goals"。
