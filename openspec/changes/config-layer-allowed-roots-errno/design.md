# Design: config-layer-allowed-roots-errno

## D1 — 判据(与 #1344/#1345 逐字同范式,禁止偏离)

唯一允许的 strict 解析形态:`os.path.realpath(path, strict=True)`。`Path.resolve(strict=True)`(≤3.12 抛无 errno RuntimeError)与非 strict `Path.resolve()`(≤3.12 对 `<missing>/../<loop>` 词法折叠撞环同样抛)**均禁用**;ENOENT 回退**必须**用非 strict `os.path.realpath`(3.11-3.14 永不抛);errno 读取用 `getattr(error, "errno", None)`。模块 `from errno import ...` 行需补 `ENOENT`(现有 EACCES/ELOOP/ENOTDIR/EPERM 在 :6)。

## D2 — 核心处方(选项 B:配置层零裁决;round-1 C-X1 裁决后改为**统一非 strict realpath 回退**)

```python
def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    expanded = Path(value).expanduser()
    try:
        return Path(os.path.realpath(expanded, strict=True))
    except OSError:
        # 选项 B:分类是 preflight 的职责,配置层永不裁决、永不抛。
        # 非 strict realpath 3.11-3.14 永不抛,且与旧非 strict resolve
        # 的规范化产物对齐(POSIX 正确:先解 symlink 再折 `..`)——
        # ENOENT(缺失根)与非 ENOENT(环/ENOTDIR/EACCES)两类回退产物
        # 在此收敛为同一形态,无需 errno 分流(见下"为何无 errno split")。
        return Path(os.path.realpath(expanded))
```

要点:

- **为何本位点无 errno split**(与 #1344/#1345 三位点的表观差异,round-1 C-X1 裁决):初稿的"非 ENOENT 词法原样下放"在 `<file>/../<dir>` 类形状上引入**新的** 3.13+ vs ≤3.12 分歧(pre-3.13 strict realpath 不校验被后续 `..` 抹掉的组件);而 normpath 词法折叠会在"symlink 祖先 + `..`"形状上开出 master 与 PR 都没有的 fail-open(normpath 抹掉 symlink 重定向)。统一非 strict realpath 回退经 10 形状 × 双腿实测**全版本一致且 master 平价**,同时保留 #1347 崩溃修复(环根构造存活→preflight blocker)。errno 分流在两车道产物收敛后成为死重,按 KISS 删除;`Path.resolve` 两形态仍禁用(D1 不变)。
- 返回签名 `Path|None` 不变 → facade 动态 forwarder(scheduler_candidate_runtime.py:549)零改动。
- 推论:`<file>/../<realdir>` 类"strict 失败但 `..` 折叠后可解析"的形状,回退产物为可解析的 `<realdir>` → preflight **纳入**(与 master 全版本行为逐字一致,非 fail-open——非 strict realpath 先解 symlink 再折叠,symlink 祖先重定向保留,`<loop>/../x`、`<noperm>/sub/../x` 等真不可解析形状仍落 blocker 车道)。
- 消费方封闭性(实现任务须 grep 复核):`_optional_config_path` 唯一生产调用链是 `scheduler_config.py:945`(`_optional_config_path_for_mode` 非 db-free 臂)← `:412`(`allowed_storage_roots` 循环)。其他 root 字段走 `_resolve_optional_config_path_for_mode`(:935),不经过本函数——爆炸半径 = 非 db-free 的 allowed_storage_roots,恰是缺陷面。

## D3 — 与 #1346 preflight 层的接续(seam 级不变量)

配置层下放的五类值到 `_preflight_allowed_roots` 后(回退产物 = 非 strict realpath):
1. 成功规范化值 → 纳入;
2. 回退产物仍缺失(纯 ENOENT)→ preflight strict realpath 再判 ENOENT → 纳入;
3. **ENOENT-掩盖-环路**(`<missing>/../<loop>`:非 strict realpath 折叠成环本身)→ preflight 判 **ELOOP** → 剔根 + blocker(与今日 3.13+ 行为同,非回归;py3.11/3.14 实测证实);
4. 回退产物仍不可解析(环 / 无 `..` 的 ENOTDIR / 深层 EACCES)→ preflight 非 ENOENT → 剔根 + blocker(`blockers[0]` 领先其 OUT_OF_ROOT 级联);
5. **strict 失败但折叠后可解析**(`<file>/../<realdir>` 类,仅 3.13+ 的 strict 才对其报错)→ 回退产物为可解析的 `<realdir>` → preflight **纳入**——全版本一致且与 master 逐字平价(B9 锚钉住)。

**seam 级不变量**(B1 锚,直接调 `_slurm_preflight(config)`):构造前已存在的环根,全版本得到 `status="blocked"` + 根因码领先 + `checks["allowed_roots"]==[]`,构造永不抛——#1346 端到端锚被迫用"构造后造环"规避的生产时序由此翻转。**该不变量只在 preflight seam 成立,pass 级(run_once)在 ≤3.12 上不成立**,见 D4。

## D4 — 幽灵根窗口披露(有界,非回归)

非 ENOENT 值在 `config.allowed_storage_roots` 存活至 preflight。逐消费方:

读 `config.allowed_storage_roots` 的全部 4 处(字段级 grep 复核,task 1.2):

| 消费方 | ≤3.12 旧 | ≤3.12 新 | 3.13+ 旧=新 |
|---|---|---|---|
| `scheduler_preflight.py:529` `_preflight_allowed_roots` | ELOOP:不可达(构造先崩);ENOTDIR/EACCES:**可达**,剔根 + blocker(#1346 A1b-e2e 锚 py3.11 现绿) | 剔根 + blocker(全非 ENOENT errno) | 剔根 + blocker |
| `scheduler_runtime_roots.py:450` `_scheduler_allowed_roots`(#1348 缺陷位点) | ELOOP:不可达(构造先崩);ENOTDIR/EACCES:**可达且 fail-open**(非 strict `resolve` 在 ≤3.12 只对环抛) | **按 errno 二分,与 lane 是否启用无关**:ELOOP 无条件抛 RuntimeError(`run_once` scheduler_runtime.py:606-609 非 db-free 无 try/except,先于 `_slurm_preflight` :1159 调 `_scheduler_lock_evidence_root_preflight`,其 not-required 早退 payload 在 scheduler_runtime_roots.py:168 调本函数;py3.11 实测);**其余非 ENOENT errno:静默 fail-open**,幽灵根进入 `allowed_roots` 证据,与本变更前逐字同值(#1348 既有,零改变) | 放行(fail-open,全 errno,#1348 既有,零改变) |
| `scheduler_runtime_roots.py:431` `_scheduler_allowed_roots_policy_check` | —(只取 `configured` 布尔) | 同,无害 | 同,无害 |
| `scheduler_config.py:1060` `_db_free_allowed_roots` | —(db-free 专用,本变更车道不可达) | 同,无害 | 同,无害 |

**诚实披露**:选项 B 落地后,≤3.12 pass 级形态按 errno 二分——**ELOOP 形状**崩溃点从"config 构造"移到 run_once 的 runtime-roots preflight(裸栈);**其余非 ENOENT errno(ENOTDIR/EACCES)静默 fail-open**,与本变更前逐字同值。`_slurm_preflight` seam 的结构化裁决全版本可证(B1),pass 级两种形态均由 #1348 收口。B8 tripwire(版本门)只钉 ELOOP 形态:#1348 落地时必红,强制翻转;fail-open 形态归 #1348 自己的锚。不得以此为由在本变更内扩面修 :448-462。

## D5 — 行为变更矩阵(以 config 层输入为界)

| 场景(非 db-free) | 旧 ≤3.12 | 旧 3.13+ | 新(全版本一致,C-X1 后以统一 realpath 回退交付) |
|---|---|---|---|
| 构造前环根 | 构造抛无 errno RuntimeError | 静默放行→preflight blocker | realpath 回退(环原样)→preflight blocker |
| ENOTDIR/EACCES 根(无 `..`,如 `<file>/sub`) | 非 strict resolve 吞错放行(产物仍不可解析) | 同左 | realpath 回退(symlink 祖先保留、产物仍不可解析)→preflight blocker |
| `<file>/../<realdir>` 类(strict 仅 3.13+ 报错) | 非 strict resolve 折叠→**纳入** | 同左 | realpath 回退折叠→**纳入**(master 全版本平价,B9 锚) |
| 缺失根(ENOENT) | 非 strict resolve 规范化纳入 | 同左 | realpath 回退规范化纳入(对齐) |
| `<missing>/../<loop>` 根 | 非 strict resolve 可抛 RuntimeError(构造崩) | 放行(折叠为环) | realpath 回退折叠为环,构造永不抛;端到端 preflight 判 **ELOOP** 剔根 + blocker(D3 类 3) |
| db-free 臂(全场景) | 词法容忍(:929-932 except 兜底) | 同左(3.13+ resolve 不抛,产物为 resolve 值) | **不动**(Non-Goal,钉子锁定) |
