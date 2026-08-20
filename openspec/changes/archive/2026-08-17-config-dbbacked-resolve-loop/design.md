# Design: config-dbbacked-resolve-loop

## D1 — 裁决：范式逐字对齐兄弟函数，errno 分流裁弃

issue 推荐措辞是「严格 realpath + errno 分流」，但范式源
`_optional_config_path`（`scheduler_runtime_roots.py:573-590`，PR #1349 定稿）
的注释已显式裁弃 errno 分流并给出理由，本 change 采纳该定稿而非 issue 的
概括措辞（issue 自己也说「与兄弟函数逐字对齐」）：

- `os.path.realpath(path, strict=True)` 成功 ⇒ 返回严格 canonical 形。
- `except OSError` ⇒ 回退非 strict `os.path.realpath(path)`：
  - 在 3.11-3.14 **永不抛**（与 `Path.resolve()` ≤3.12 对环抛 RuntimeError
    的病灶正交）；
  - 产物**逐字复刻**旧非 strict `Path.resolve()`（POSIX 序：symlink 先、
    `..` 后）——ENOENT（合法的「尚不存在路径」）行为零回归；
  - errno 分流买不到东西：两条 would-be lane（ENOENT 回退 / ELOOP 拒绝）
    收敛到同一产物，且词法 pass-through 会重开 `<file>/../<dir>` 形状的
    3.13+ vs ≤3.12 分叉（范式源注释原文论证）。

## D2 — 改动形状（唯一代码 hunk）

`services/orchestrator/scheduler_config.py:928-934`：

```python
def _resolve_config_path_for_mode(path: Path, *, db_free_required: bool) -> Path:
    if not db_free_required:
        try:
            return Path(os.path.realpath(path, strict=True))
        except OSError:
            # <注释逐字对齐 scheduler_runtime_roots.py:576-590 的裁定要点：
            #  分类归 storage preflight，非 strict realpath 全版本不抛且
            #  复刻旧产物，errno 分流两 lane 收敛同一产物故裁弃>
            return Path(os.path.realpath(path))
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path
```

- db-free 臂 `:931-934` **逐字不动**（#1400 属地）。
- 返回类型保持 `Path`，唯一调用方 `_resolve_optional_config_path_for_mode`
  （`:937-940`）签名不变，不新增 blocker 出口类型（issue In-scope 约束）。
- `os` 已在文件内 import（`:83-98` env 默认即用 `os.getenv`）；实现时确认
  顶部 import 形态，不引新依赖。

## D3 — 验收「二选一」裁定：canonical 产物下放 + storage preflight 承接

issue 验收第 2 条要求 3.13+ 环路 root「或 errno 分流拒绝，或落到既有 storage
preflight 结构化 blocker，二选一并在 PR body 记录裁定」。裁定：**后者**。

- 承接面已实测在场：`_slurm_preflight`（`scheduler_gateway.py:41-63`）对
  `workspace_root`/`object_store_root`/`log_root`/`runtime_root` 四字段逐个调
  `_storage_root_check`（`scheduler_preflight.py:565-648`）——strict realpath +
  errno 分流，**非 ENOENT（含 ELOOP）→ `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH`
  结构化 blocker**，拒绝提交先于建 Slurm job。
- 该结构与 allowed-roots 先例（2026-08-10-config-layer-allowed-roots-errno
  的 B1 seam 级不变量：「构造前已存在的环根，全版本 `status="blocked"` +
  根因码领先 + 构造永不抛」）同构；本 change 把同一不变量扩到**非
  containment-base 的**通用 root 字段（见下取景收窄）。
- **不变量取景收窄（fixture review P1-1 实测）**：构造不崩不变量只对
  「末段环 + 非 containment-base 字段」成立——`OBJECT_STORE_ROOT` /
  `LOG_ROOT` / `NHMS_SCHEDULER_RUNTIME_ROOT` / `NHMS_PUBLISHED_ARTIFACT_ROOT`
  / `NHMS_SCHEDULER_TEMP_ROOT`。`WORKSPACE_ROOT`（lock_path/evidence_dir 的
  containment base）与 `NHMS_SCHEDULER_LOCK_ROOT`
  在本修复后仍会经 `_confined_path` 的裸 `path.parent.resolve()`
  （`scheduler_runtime_roots.py:558`）崩（≤3.12 RuntimeError / 3.14
  ValueError@:701/:711，两臂异常类型还不同）；`NHMS_SCHEDULER_EVIDENCE_ROOT`
  两臂收敛于同一结构化 ValueError@:701（故意的 containment 拒绝，非 resolve
  崩溃——round-2 Note-1，正确终态，不属残余缺陷面）——:558 站点不在 issue
  In-scope，
  连「仅登记」名单都没进，已路由 issue-scribe 另立追踪（编号见 proposal
  Non-Goals 与 PR body）。issue 验收第 1 条点名 `WORKSPACE_ROOT` 的措辞
  超出单点判据可达范围，须在 PR body 明记该裁定。
- `published_artifact_root` carve-out（fixture review P2-4 修正理由）：其
  下游消费面是 scheduler runtime-root preflight
  （`scheduler_runtime_roots.py:110-117` → `_scheduler_root_check`），且吃的
  是 `_published_artifact_root_preflight_path`（preserve-final 路径）而非本
  change 修的 resolved 字段——本 change 对其下游分类零影响；#1402 家族的
  `retry.py:1410` 直读 env，也不消费本字段。因此对该字段，修复后 ≤3.12 从
  「响亮崩溃」变为「静默收编 + 无 storage preflight 承接」——这是**已知
  残余**，PR body 按残余明记，不得当已覆盖。`temp_root`/`lock_root`/
  `evidence_root` 经 `_scheduler_runtime_root_preflight`（`:124-137`）消费，
  同属 runtime-root preflight 平面，非 `_slurm_preflight` 四字段 dict。

## D4 — 测试计划（tests/test_production_scheduler.py，靠近既有 symlink 环族：
local-artifact 族 `:14318-14790`（含可复用 helper `_symlink_loop_dir`@:14318）、
storage-preflight/allowed-roots 族 `:36264-36500`（`gone/../loop` 形先例
@:36304）；db-free 对照锁已在 `:31585`——行号为 fixture review 实测，issue
正文的 `:12570-12699` 已陈旧）

判定表（**helper 直测** `_resolve_config_path_for_mode`，
`db_free_required=False`；格 4/5 只做 helper 直测——构造 seam 上该形在
`:604` preserve-final 处更早崩，属 residual issue 属地，fixture review P2-3）：

| # | 输入形 | 期望 |
|---|---|---|
| 1 | 真实目录 | canonical 形（与旧 `Path.resolve()` 同值） |
| 2 | 尚不存在路径（ENOENT） | 非 strict 产物，不抛（既有语义） |
| 3 | 自指 symlink 环 `a->a` | **不抛**，返回非 strict realpath 产物（版本无关） |
| 4 | 环后缀形 `loopdir/a/tail`（helper 直测） | 不抛，产物版本无关同值 |
| 5 | `<gone>/../<loopdir>/<loop-leaf>`（helper 直测） | 不抛（fixture review P2-7：`<gone>/../<loopdir>` 本身三版本 both-不抛且同产物，锁不住差分；真正分叉形是带环末段的本形——master 旧 resolve 在 ≤3.12 抛） |
| 6 | db-free 臂同环入参（对照） | 既有优雅降级返回原 path，diff 级不变 |

构造 seam（env 驱动，monkeypatch env + `ProductionSchedulerConfig()`；字段
取景按 D3 收窄——**不含** `WORKSPACE_ROOT`/lock/evidence）：

- 环路 `OBJECT_STORE_ROOT`（db-backed）⇒ 构造不抛，字段值为非 strict 产物
  规范形——**红-绿主锚**：master 上 ≤3.12 抛 RuntimeError；3.13+ 全绿但
  字段值断言版本无关成立。
- 参数化补 `LOG_ROOT`（非 containment-base 的另一条 env 腿；fixture review
  P1-1 实测修复后 construct OK）。

e2e 锚（preflight 承接，D3 裁定的证明）：

- 环路 `OBJECT_STORE_ROOT` + db-backed 构造成功后，`_slurm_preflight(config)`
  产出 `SLURM_PREFLIGHT_OBJECT_STORE_ROOT_UNSAFE_PATH` blocker（fixture
  review 已模拟实证：非-ENOENT 分支，strict realpath errno=62 ELOOP）；全
  版本一致。

**3.11 臂命令形态（fixture review P2-6：裸 `uv run --python 3.11` 会就地
销毁重建项目 .venv 且无 pytest）**：一律用
`UV_PROJECT_ENVIRONMENT=<scratchpad>/venv311 uv run --python 3.11 [--all-extras --dev] …`
隔离环境；若误用裸形态，事后 `uv sync --all-extras --dev` 复位。红证记录
须注明解释器（本地 .venv 3.14.2 不复现崩溃臂）。

版本矩阵复测（issue 验收第 5 条）：修复后在 3.11（隔离环境形态）与本地
3.14 各跑一次证据 3 的矩阵脚本，结果附 PR body。node-27（3.11.15）为
≤3.12 臂真实 oracle：新增测试须在 node-27 跑通（tasks 3.5）。

## D5 — Invariant Matrix

- I1 构造不变量（取景收窄，P1-1）：db-backed 下**末段环 + 非
  containment-base 字段**（OBJECT_STORE / LOG / RUNTIME / PUBLISHED_ARTIFACT
  / TEMP）不再让 `ProductionSchedulerConfig()` 构造期抛（≤3.12 与 3.13+
  同一产物、同一后续判定）。WORKSPACE/LOCK/EVIDENCE 与父段环形不在本
  不变量内（residual issue 属地）。
- I2 ENOENT 零回归：尚不存在的合法路径产物与旧非 strict `Path.resolve()`
  逐字同值；既有测试不改断言即通过。
- I3 db-free 臂零改动：`:931-934` diff 级不动，其优雅降级对照测试不变。
- I4 下游承接：环根经 `_slurm_preflight` 得结构化 UNSAFE_PATH blocker，
  拒绝提交先于建 job（全版本一致）。
- I5 非环路径产物零漂移：真实目录/常规相对形的 canonical 产物与改前同值。
