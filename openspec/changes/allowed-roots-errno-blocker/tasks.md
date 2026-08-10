# Tasks: allowed-roots-errno-blocker

Fixture level: **compact**(单函数判据替换 + 单调用点解包;与 issue `Suggested fixture level` 一致,无分歧)。
风险轴:cross-version 语义(3.11/3.12 vs 3.13/3.14)、fail-open→fail-closed 契约升级、preflight 证据面形状。
Seams under test:`_preflight_allowed_roots` 返回值/blocker、`_slurm_preflight` 装配面(status/blockers/checks.allowed_roots)。
Must-preserve:ENOENT 纳入语义、db-free 词法回退容忍(PR #831)、去重、空配置回退 workspace_root、`checks["allowed_roots"]` 掩码规则、`_storage_root_check` 及其 114 条既有测试行为。

## 1. 实现

- [x] 1.1 `scheduler_preflight._preflight_allowed_roots` 按 design D2 重写:strict realpath + errno 分流,返回 `(tuple[Path, ...], list[dict])`;`Path.resolve` 任何形态不得出现(D1)。
- [x] 1.2 `scheduler_gateway._slurm_preflight`:解包 + `blockers.extend(allowed_root_blockers)`(storage_roots 循环之前);其余零改动。
- [x] 1.3 `grep -rn "_preflight_allowed_roots"` 全仓核对消费方封闭性:仅 定义/facade 再导出(scheduler_candidate_runtime.py:239,:832)/gateway 调用/测试;facade 无需改动。

## 2. 测试锚点(tests/test_production_scheduler.py,纯增量,零删除)

- [x] 2.1 **A1(RED 主锚)** 环根 + db_free=False:`_preflight_allowed_roots` 剔除环根且产出 code=`SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH`、field=`allowed_storage_roots` 的 blocker;端到端 `_slurm_preflight` status=`blocked`、**`blockers[0]["code"]` 即该根因码**(排序有语义:evidence 消费面取 `blockers[0]` 作 error_code,见 design D3)、环根不出现在 `checks["allowed_roots"]`。前提:config 须给安全远端 `database_url`(沿用 tests/test_production_scheduler.py:15091 的 `postgresql://nhms:secret@db.prod.example/nhms` 约定),否则 `DATABASE_URL_*` blocker 会占据 index 0,顺序断言失败且读数误导。RED 证:3.14 现行代码静默纳入且无 blocker;3.11 现行代码 **RuntimeError** 逃逸(pathlib ELOOP→无 errno RuntimeError;红证用 `pytest.raises(RuntimeError)` 读数,异常而非结构化 blocker)。
- [x] 2.1b **A1b(errno 二分钉)** ENOTDIR 变体 + db_free=False:根配置为 `tmp_path/"file.txt"/"root"`(`file.txt` 为普通文件,四版本稳定 errno=20,hermetic 无需 symlink/chmod):同样剔除 + 同 code blocker——钉住判据是"非 ENOENT"而非"仅 ELOOP"。
- [x] 2.2 **A2(判别器钉)** 同一环根 + db_free=True:纳入(词法回退)、零 blocker——与 A1 构成臂间可观测差异(D5 注)。3.11 现行即绿(PR #831 语义钉)。
- [x] 2.3 **A3(ENOENT 钉)** 不存在的根 × 两臂:纳入、零 blocker、无异常(旧非 strict resolve 语义逐字对齐)。
- [x] 2.4 **A4(崩溃车道钉)** `<missing>/../<loop>` 形状根 + db_free=False:走 ENOENT 车道纳入、零 blocker、**永不裸抛**——py3.11 腿为回退车道误用 `Path.resolve()` 的崩溃红证(#1344 P1 教训)。
- [x] 2.5 **A5(不变量钉)** 重复根去重、空 `allowed_storage_roots` 回退 `[workspace_root]`:返回形状升级后语义不变。
- [x] 2.6 **A6(零回归)** `uv run pytest -q tests/test_production_scheduler.py -k "preflight or allowed_root"` 全绿;既有 114 条 preflight 测试零改动零删除。

## 3. 突变击杀证(实现完成后逐条留证)

- [x] M1 判据回退为旧 `except (OSError, RuntimeError)` + resolve → A1 的 3.14 腿必死。
- [x] M2 ENOENT 回退换 `path.resolve()` → A4 的 py3.11 腿必死(RuntimeError)。
- [x] M3 剔根但不产 blocker → A1 的 blocker 断言必死。
- [x] M4 产 blocker 但不剔根 → A1 的 `checks["allowed_roots"]` 缺席断言必死。
- [x] M5 判据收窄为 `errno == ELOOP` → A1b 的 ENOTDIR 腿必死。

## 4. 规格

- [x] 4.1 `specs/slurm-array-runner-integration/spec.md` delta:MODIFIED `Array-capable model stages`,新增 scenario `unresolvable allowed storage root`;`openspec validate allowed-roots-errno-blocker --strict --no-interactive` 通过。

## Evidence Floor

- RED→GREEN 矩阵:A1 双解释器红证(3.14 fail-open / 3.11 RuntimeError 逃逸)→ 修后双腿绿;A1b/A2-A5 钉证;M1-M5 击杀证。
- 双腿:
  - 3.14 腿(项目 `.venv`,3.14.2):`uv run pytest -q tests/test_production_scheduler.py -k "preflight or allowed_root"`。
  - py3.11 腿(**严禁裸 `uv run --python 3.11`——会重建项目 `.venv`**):`UV_PROJECT_ENVIRONMENT=/private/tmp/claude-501/-Users-danker-Desktop-Hydro-SHUD-NWM--claude-worktrees-pr-1286-subagent-workflow-7fb9ee/03b2c0ce-847d-47b7-8b0f-8af56993ac52/scratchpad/py311 uv run --python 3.11 pytest -q tests/test_production_scheduler.py -k "preflight or allowed_root"`(该 venv 已存在,CPython 3.11.14;若依赖缺失按需 `uv pip install` 到该环境;venv 不存在时先 `uv venv --python 3.11 <同路径>` 重建——路径含会话 scratchpad,归档后复现时可任选等价位置,腿的本质是"任一 CPython 3.11 环境跑同一选择器全绿")。
- `uv run ruff check .` 通过;openspec validate 通过。
- CI:PR targeted Unit Tests(py3.11)绿。
- 无远端 receipt 需求(纯本地判据,无 DB/display 接触面)。

## Non-Goals(复述 proposal)

不改 `_storage_root_check`/`_path_is_under_any`/掩码规则;不做空根集合补救(D4 fail-closed);不动其他 3.13+ 同族位点(已由 #1332/#1344 处理或独立建单)。
