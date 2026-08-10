# Design: allowed-roots-errno-blocker

## D1 — 判据(与 #1344 三位点逐字同范式,禁止偏离)

唯一允许的 strict 解析形态:`os.path.realpath(path, strict=True)`。

- **禁用 `Path.resolve(strict=True)`**:≤3.12 对环抛无 errno 的 `RuntimeError`,errno 分流失效。
- **禁用非 strict `Path.resolve()`(包括任何回退车道)**:≤3.12 对 `<missing>/../<loop>` 形状在词法折叠撞环时同样抛 RuntimeError(#1344 PR round-1 P1 实锤)。ENOENT 回退**必须**用非 strict `os.path.realpath`——3.11-3.14 全版本永不抛。
- errno 读取用 `getattr(error, "errno", None)`,与 `_storage_root_check`(scheduler_preflight.py:562-585)一致。

## D2 — 逐根车道(核心处方)

```python
def _preflight_allowed_roots(config) -> tuple[tuple[Path, ...], list[dict[str, Any]]]:
    roots = list(config.allowed_storage_roots) or [Path(config.workspace_root)]
    db_free = bool(getattr(config, "db_free_required", False))
    resolved: list[Path] = []
    blockers: list[dict[str, Any]] = []
    for root in roots:
        expanded = root.expanduser()
        try:
            candidate = Path(os.path.realpath(expanded, strict=True))
        except OSError as error:
            if getattr(error, "errno", None) == ENOENT:
                # 合法缺失根(db-free NFS 未挂载 / 尚未创建):保持纳入语义,
                # 两臂均不产 blocker。非 strict realpath 3.11-3.14 永不抛。
                candidate = Path(os.path.realpath(expanded))
            elif db_free:
                # PR #831 词法回退容忍,逐字保留(不 realpath、不 blocker)。
                candidate = expanded
                if not candidate.is_absolute():
                    candidate = Path.cwd() / candidate
            else:
                blockers.append({
                    "code": "SLURM_PREFLIGHT_ALLOWED_STORAGE_ROOTS_UNSAFE_PATH",
                    "field": "allowed_storage_roots",
                    "path": str(expanded),
                    "message": "Slurm allowed storage root must be canonically resolvable.",
                })
                continue  # 剔除:不进 resolved
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved), blockers
```

要点:

- blocker dict 形状与 `_storage_root_check` 的 `{code, field, path, message}` 完全同构;code 沿用既有动态词汇 `SLURM_PREFLIGHT_{FIELD}_UNSAFE_PATH`(FIELD=ALLOWED_STORAGE_ROOTS)。
- `path` 用 `str(expanded)`(展开后路径),与 `_storage_root_check` 同类 blocker(:568/:577 用展开后 `str(path)`)口径一致——`~/foo` 形态不得以原始波浪线形式进证据面。
- `path` 不掩码:blocker 只在非 db-free 臂产出,而 `checks["allowed_roots"]` 恰在非 db-free 时展示真实路径——掩码规则不变(Non-Goal)。
- 去重 `if candidate not in resolved` 原样保留;空配置回退 `or [workspace_root]` 原样保留。

## D3 — 调用点与 facade

- **gateway(scheduler_gateway.py:50,唯一真实调用点)**:
  `allowed_roots, allowed_root_blockers = _scheduler._preflight_allowed_roots(config)`,随即 `blockers.extend(allowed_root_blockers)`——**必须在 storage_roots 循环之前**。顺序有语义负载:`scheduler_candidate_execution_evidence.py:341-364` 取 `blockers[0]` 作 `error_code`/`error_message`;剔根后四个 storage root 会级联 `OUT_OF_ROOT`,根因 blocker 必须**领先于它自己引发的 OUT_OF_ROOT 级联**(非全局 index-0 保证:`DATABASE_URL_*` blocker 在 :38 合法先行,那本身也是根因;A1 端到端在安全 database_url 前提下钉住 `blockers[0]["code"]`)。`checks["allowed_roots"]` 构造零改动——被剔除的根天然不在 `allowed_roots` 里,证据面自动收敛(验收:被剔除的根不得出现在 `checks["allowed_roots"]`)。
- **facade(scheduler_candidate_runtime.py:239 + :832)**:纯符号再导出,函数对象返回形状变化随符号透传,无需改动。实现任务必须 `grep -rn "_preflight_allowed_roots"` 全仓核对:除 preflight 定义、facade 再导出、gateway 调用外**不得**存在其他消费方(tests/ 已确认无直接调用者)。

## D4 — 空有效根集合(fail-closed,不补救)

非 db-free 下全部配置根均不可解析时 `resolved == ()`:后续每个 storage root 的 `_path_is_under_any` 判 False → 全数 `OUT_OF_ROOT` blocker,叠加本位点的 `ALLOWED_STORAGE_ROOTS_UNSAFE_PATH` 解释成因。这是 fail-closed 的正确形状——**不得**在剔空后回填 `workspace_root`(那等于用一个操作员未批准的根顶替,重新打开 fail-open)。

## D5 — 行为变更矩阵(披露面)

| 场景 | 旧 3.13+/3.14 | 旧 ≤3.12 | 新(全版本一致,以 config 层可达输入域为限) |
|---|---|---|---|
| 环根, db_free=False | 静默纳入(fail-open) | **RuntimeError** 逃逸崩溃(pathlib ELOOP→无 errno RuntimeError) | 剔除 + UNSAFE_PATH blocker + status=blocked(≤3.12 上仅对构造后出现的环可达——构造前已存在的环在配置层 `_optional_config_path` 先崩,#1347 跟踪) |
| 非 ENOENT 其他 errno(EACCES/ENOTDIR/ENAMETOOLONG…), db_free=False | 静默纳入 | **静默纳入**(非 strict resolve 不抛) | 剔除 + UNSAFE_PATH blocker(**所有版本收紧,含生产 3.11/3.12**) |
| 环根, db_free=True | 静默纳入(resolve 产物) | 词法回退纳入 | 词法回退纳入,无 blocker |
| 缺失根(ENOENT), 两臂 | 非 strict resolve 纳入 | 非 strict resolve 纳入 | 非 strict realpath 纳入,无 blocker(逐字对齐) |
| `<missing>/../<loop>` 根 | 纳入 | resolve 可能 RuntimeError | ENOENT 车道 → 非 strict realpath 纳入,永不裸抛 |

注:
- 新代码在 ≤3.12 亦不再泄漏 RuntimeError——strict `os.path.realpath` 只抛带 errno 的 OSError。
- db_free=True 环根臂在 3.13+ 上旧新产物路径可能逐字相同(自环 realpath 返回原路径)——判别器的可观测差异在**臂间**(A1 剔除+blocker vs A2 纳入+无 blocker),不在 db-free 臂自身的路径值上。
- `<missing>/../<loop>` 形状纳入不构成可利用 fail-open:errno 出现顺序决定其走 ENOENT 车道,但落在该容器根之下的每个 storage root 自身仍会被 `_storage_root_check` strict 解析并判 `UNSAFE_PATH`(storage root 自身也是 `<missing>/../<loop>` 形状时则为 `NOT_VISIBLE`,见 tests/test_production_scheduler.py:29862)——两者均阻断,单根纳入不放行任何实际路径。
