# Tasks: config-dbbacked-resolve-loop

## 1. 实现

- [x] 1.1 `services/orchestrator/scheduler_config.py:928-930`（design D2）：
      db-backed 臂改 `os.path.realpath(path, strict=True)` + `except OSError`
      回退非 strict `os.path.realpath(path)`；注释对齐范式源
      `scheduler_runtime_roots.py:576-590` 的裁定要点（分类归 storage
      preflight、非 strict 全版本不抛且复刻旧产物、errno 分流裁弃）。
      db-free 臂 `:931-934` 逐字不动；调用方签名/返回类型不变。

## 2. 测试（tests/test_production_scheduler.py，靠近既有 symlink 环族
`:14318-14790`（复用 `_symlink_loop_dir`@:14318）；先红后绿）

- [x] 2.1 helper 判定表（design D4 六格）：真实目录 / ENOENT / 自指环 /
      环后缀 `loopdir/a/tail`（helper 直测） /
      `<gone>/../<loopdir>/<loop-leaf>`（helper 直测，P2-7 差分形） /
      db-free 臂对照——全部版本无关断言。
- [x] 2.2 构造 seam 红-绿主锚：环路 `OBJECT_STORE_ROOT`（db-backed）⇒
      `ProductionSchedulerConfig()` 不抛 + 字段值为非 strict realpath 规范
      形；参数化补 `LOG_ROOT`（**不用** `WORKSPACE_ROOT`——containment
      base，修复后仍经 `scheduler_runtime_roots.py:558` 崩，residual issue
      属地，P1-1）。红证：master 上经隔离环境
      `UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311 uv run --python 3.11
      --all-extras --dev …` 复现 RuntimeError（裸 `uv run --python 3.11`
      会销毁项目 .venv，禁用，P2-6；本地 .venv 3.14.2 不复现崩溃臂，红证
      记录注明解释器）。
- [x] 2.3 e2e 锚（D3 裁定证明）：环路 `OBJECT_STORE_ROOT` 构造成功后
      `_slurm_preflight(config)` 产出
      `SLURM_PREFLIGHT_OBJECT_STORE_ROOT_UNSAFE_PATH` blocker。
- [x] 2.4 ENOENT 零回归：既有「配置期不做存在性校验」相关测试不改断言
      通过；判定表 ENOENT 格产物与旧非 strict `Path.resolve()` 同值。
- [x] 2.5 版本矩阵复测（issue 证据 3 脚本）：3.11（隔离环境形态）与本地
      3.14 各一次，输出附 PR body。

## 3. 验证（Evidence Floor，per issue Verification）

- [x] 3.1 `uv run pytest -q tests/test_production_scheduler.py` 通过。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate config-dbbacked-resolve-loop --strict
      --no-interactive` 通过。
- [x] 3.4 issue Verification 步骤 4（≤3.12 臂缺陷消解）：隔离环境 3.11 下
      环路 `OBJECT_STORE_ROOT` 构造成功打印字段值，不再抛 RuntimeError
      （命令形态见 design D4，勿用裸 `uv run --python 3.11`）。
- [ ] 3.5 node-27 oracle（3.11.15，≤3.12 臂真实运行面）：新增测试于
      node-27 `uv run pytest -q tests/test_production_scheduler.py -k
      <新测试选择器>` 全绿（merge 后亦可，按 #1419 3.5 先例记录 receipt）。
- [ ] 3.6 PR body 裁定记录（issue 验收第 2 条 + fixture review P1-1/P2-4，
      Note-9）：记录（a）「二选一」选了 storage preflight 承接及其证据；
      （b）issue 验收第 1 条点名 `WORKSPACE_ROOT` 超出单点判据可达范围
      （containment-base 残余归 residual issue）；（c）
      `published_artifact_root` 修复后 ≤3.12 从响亮崩溃变静默收编、无
      storage preflight 承接——按已知残余明记。
