# Design: config-layer-allowed-roots-errno

## D1 — 判据(与 #1344/#1345 逐字同范式,禁止偏离)

唯一允许的 strict 解析形态:`os.path.realpath(path, strict=True)`。`Path.resolve(strict=True)`(≤3.12 抛无 errno RuntimeError)与非 strict `Path.resolve()`(≤3.12 对 `<missing>/../<loop>` 词法折叠撞环同样抛)**均禁用**;ENOENT 回退**必须**用非 strict `os.path.realpath`(3.11-3.14 永不抛);errno 读取用 `getattr(error, "errno", None)`。模块 `from errno import ...` 行需补 `ENOENT`(现有 EACCES/ELOOP/ENOTDIR/EPERM 在 :6)。

## D2 — 核心处方(选项 B:配置层零裁决)

```python
def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    expanded = Path(value).expanduser()
    try:
        return Path(os.path.realpath(expanded, strict=True))
    except OSError as error:
        if getattr(error, "errno", None) == ENOENT:
            # 合法缺失路径:非 strict realpath 与旧非 strict resolve 的
            # 规范化产物对齐,全版本不抛。
            return Path(os.path.realpath(expanded))
        # 选项 B:分类是 preflight 的职责。词法绝对值原样下放,
        # _preflight_allowed_roots 在所有解释器上剔根并产出
        # SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH。
        if not expanded.is_absolute():
            return Path.cwd() / expanded
        return expanded
```

要点:

- 返回签名 `Path|None` 不变 → facade 动态 forwarder(scheduler_candidate_runtime.py:549)零改动。
- 非 ENOENT 词法下放采用与 `_preflight_allowed_roots` db-free 回退相同的"expanduser + cwd 绝对化"形状(仓内既有词法回退惯例)。
- 消费方封闭性(实现任务须 grep 复核):`_optional_config_path` 唯一生产调用链是 `scheduler_config.py:945`(`_optional_config_path_for_mode` 非 db-free 臂)← `:412`(`allowed_storage_roots` 循环)。其他 root 字段走 `_resolve_optional_config_path_for_mode`(:935),不经过本函数——爆炸半径 = 非 db-free 的 allowed_storage_roots,恰是缺陷面。

## D3 — 与 #1346 preflight 层的接续(seam 级不变量)

配置层下放的四类值到 `_preflight_allowed_roots` 后:
1. 成功规范化值 → 纳入;
2. ENOENT 规范化值(且规范化后仍缺失)→ strict realpath 再判 ENOENT → 纳入;
3. **ENOENT-掩盖-环路**(`<missing>/../<loop>`:配置层非 strict realpath 把它折叠成环本身)→ preflight strict realpath 判 **ELOOP** → 剔根 + blocker(与今日 3.13+ 行为同,非回归;py3.11/3.14 实测证实);
4. 词法下放值(非 ENOENT)→ strict realpath 非 ENOENT → 剔根 + blocker(`blockers[0]` 领先其 OUT_OF_ROOT 级联)。

**seam 级不变量**(B1 锚,直接调 `_slurm_preflight(config)`):构造前已存在的环根,全版本得到 `status="blocked"` + 根因码领先 + `checks["allowed_roots"]==[]`,构造永不抛——#1346 端到端锚被迫用"构造后造环"规避的生产时序由此翻转。**该不变量只在 preflight seam 成立,pass 级(run_once)在 ≤3.12 上不成立**,见 D4。

## D4 — 幽灵根窗口披露(有界,非回归)

非 ENOENT 值在 `config.allowed_storage_roots` 存活至 preflight。逐消费方:

读 `config.allowed_storage_roots` 的全部 4 处(字段级 grep 复核,task 1.2):

| 消费方 | ≤3.12 旧 | ≤3.12 新 | 3.13+ 旧=新 |
|---|---|---|---|
| `scheduler_preflight.py:529` `_preflight_allowed_roots` | 不可达(构造先崩) | 剔根 + blocker | 剔根 + blocker |
| `scheduler_runtime_roots.py:450` `_scheduler_allowed_roots`(#1348 缺陷位点) | 不可达(构造先崩) | **无条件抛 RuntimeError**:`run_once`(scheduler_runtime.py:606-609,非 db-free 无 try/except)先于 `_slurm_preflight`(:1159)调 `_scheduler_lock_evidence_root_preflight`,其 not-required 早退 payload 自身在 scheduler_runtime_roots.py:168 调本函数(py3.11 实测证实)——**不是**"lane 启用时才崩" | 放行(fail-open,#1348 既有,本变更零改变) |
| `scheduler_runtime_roots.py:431` `_scheduler_allowed_roots_policy_check` | —(只取 `configured` 布尔) | 同,无害 | 同,无害 |
| `scheduler_config.py:1060` `_db_free_allowed_roots` | —(db-free 专用,本变更车道不可达) | 同,无害 | 同,无害 |

**诚实披露**:选项 B 落地后,≤3.12 pass 级崩溃点从"config 构造"移到 run_once 的 runtime-roots preflight,运维在 pass 级拿到的仍是裸栈;`_slurm_preflight` seam 的结构化裁决全版本可证(B1),pass 级由 #1348 收口。B8 tripwire 钉(版本门 `sys.version_info < (3,13)`)钉住该残余:#1348 落地时该钉必红,强制其翻转为修复后断言。不得以此为由在本变更内扩面修 :448-462。

## D5 — 行为变更矩阵(以 config 层输入为界)

| 场景(非 db-free) | 旧 ≤3.12 | 旧 3.13+ | 新(全版本一致) |
|---|---|---|---|
| 构造前环根 | 构造抛无 errno RuntimeError | 静默放行→preflight blocker | 词法下放→preflight blocker |
| ENOTDIR/EACCES 根 | 非 strict resolve 吞错放行(祖先已规范化的产物) | 同左 | 词法下放(祖先已规范化时与旧产物逐字相同;symlink 祖先下为词法形状,见 B2 判别素材)→preflight blocker |
| 缺失根(ENOENT) | 非 strict resolve 规范化纳入 | 同左 | 非 strict realpath 规范化纳入(对齐) |
| `<missing>/../<loop>` 根 | 非 strict resolve 可抛 RuntimeError(构造崩) | 放行(折叠为环) | ENOENT 车道:非 strict realpath 折叠为环本身,构造永不抛;端到端 preflight 判 **ELOOP** 剔根 + blocker(D3 类 3,与今日 3.13+ 同) |
| db-free 臂(全场景) | 词法容忍(:929-932 except 兜底) | 同左(3.13+ resolve 不抛,产物为 resolve 值) | **不动**(Non-Goal,钉子锁定) |
