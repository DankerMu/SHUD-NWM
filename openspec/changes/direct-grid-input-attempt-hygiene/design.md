# Design: direct-grid-input-attempt-hygiene

坐标系：master `a9262fad`（`workers/shud_runtime/runtime.py` 4316 行）。

## D1 方向裁定：declared-member-anchored 卫生，否决 quarantine-and-recreate

采纳 issue 推荐方案。备选（`input` 整体走 `output` 的 quarantine-and-recreate,
`_prepare_attempt_output_dir` :488-555 样板）语义最干净，但每 attempt 重拷全量
model package + forcing 包（NFS 实打实 I/O），失去「复用 staging 加速重试」
现有性质，规模 S→M+，且需先量化 re-stage 成本——否决。定向卫生只删
`SHUD_FORCING_INDEX_MEMBERS` 中本次 manifest 未声明的成员——常态（声明恰一
成员）至多 1 个文件，零声明形删除两个（见 D6）；永不越出该两元素集合。
保留其余 staging 复用。

## D2 插入点：`_stage_direct_grid_directory_artifact` 写入循环之前

- direct-grid forcing **只有目录形来源**：`_stage_artifact` :1864-1896 对
  direct-grid 的 tar/regular-file 形来源抛
  `DIRECT_GRID_FORCING_TAR_UNSUPPORTED`，目录形一律进
  `_stage_directory_artifact` :1904-1913 → direct-grid 变体 :1920。因此在该
  函数入口做卫生**覆盖全部 direct-grid staging 面**，且非 direct-grid 车道
  by construction 不经过此函数、零触碰（兼容性主张的结构性证明）。
- 执行时序：`prepare_workspace` :557-582 中 `forcing_context` 先于 staging
  构造（:573），其 `checksum_entries` 已经过 `_verify_forcing_package_manifest`
  （checksum 校验）+ `_direct_grid_runtime_checksum_entries` :2041-2123 收敛
  ——后者对 manifest 声明多 index 成员已 fail-closed（:2049-2054），故进入
  staging 时 declared index 成员**至多一个**。卫生锚定的「声明集合」=
  `{e["relative_path"] for e in forcing_context.checksum_entries} ∩
  SHUD_FORCING_INDEX_MEMBERS`。
- 注意 model package 先于 forcing 包 staging（:572 vs :574）：model 包若携带
  station-index 同名文件也会被卫生视角覆盖——卫生在 forcing staging 前运行，
  删除的是「本次 forcing manifest 未声明」的 index 成员，与其来源无关；这
  正是「input 工作区 attempt 起点卫生」的语义（残留不问出处）。

## D3 删除机制与失败编码

```python
declared_index_members = {
    str(entry["relative_path"])
    for entry in forcing_context.checksum_entries
    if str(entry["relative_path"]) in SHUD_FORCING_INDEX_MEMBERS
}
for member in SHUD_FORCING_INDEX_MEMBERS:
    if member in declared_index_members:
        continue
    member_path = destination / Path(*PurePosixPath(member).parts)
    unlink_no_follow(member_path, containment_root=destination, missing_ok=True)
```

- `unlink_no_follow` + `containment_root=destination`（= model_input_dir，与
  staging 写入 `_write_staged_bytes(..., root=destination)` 同一约束根）；
  symlink 形残留：`unlink_no_follow` **拒绝**（safe_fs.py:394 "Refusing to
  unlink symlink"），与目录形（:396）同样落
  `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` fail-loud。**非文件形的
  修前/修后对照（round-1 复审 C1 改判，如实记账）**：symlink 形修前**已经**
  永久致命——`_regular_file_exists` 经 `stat_no_follow` 对 symlink 直接抛
  `SafeFilesystemError`、重编为 `WORKSPACE_PATH_UNSAFE`（runtime.py:2595-2596
  / safe_fs.py:257-258），从不返回 False——修后只是换成本 change 的 typed
  码，无新增自锁死；目录形修前**良性**（`_regular_file_exists` 返回 False、
  不计入 `staged_members`、staging 照常），修后**有意**转为不可重试
  fail-loud——这是对被篡改几何的新收紧，人群仅限带外写入（三条生产写入
  路径都不产 symlink/目录形成员），接受该权衡：fail-closed 是正确姿态，
  「忽略非文件形」恰是被禁止的两分支（fail-loud，不静默跳过、不降级为
  警告——**无两分支**，#1164 `_clear_packaged_initial_states` :1593-1618
  样板）。
- 失败编码：捕获 `SafeFilesystemError` / `OSError` → 重抛专用 typed 码
  `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`，消息含成员相对路径与原始
  错误；attempt 经 `execute()` 的统一 except 面（:469-486）落 failure log +
  receipt + `mark_failed`。
- retry 分类：**不改 `retry.py`**。该码不进 `TRANSIENT_ERROR_CODES`——不加
  进可重试集合是 issue 的硬边界（删不掉的文件重试也删不掉）；当前实现下
  走未知码默认 non-transient（`unknown_error_code_defaulted_non_transient`
  常量 retry.py:52，分类逻辑 `auto_retry_skipped_details` :133-152）。
  测试只锚定「不在 TRANSIENT_ERROR_CODES」；是否登记进
  `NON_TRANSIENT_ERROR_CODES`（`warn_unknown_error_code` :155-167 视未分类
  为待登记项）不由本测试固定，留给后续登记动作。
- 删除范围合同：**只**动 `SHUD_FORCING_INDEX_MEMBERS` 补集；`shud/` 下其他
  文件（上一 attempt 的 station CSV、无关文件）一律不碰——它们要么被本次
  staging 同名覆写，要么由 checksum 校验/读取路径裁决；无差别清空是明确
  non-goal。

## D4 文件系统歧义门保留 + 消息假说段改写

`_stage_standard_shud_forcing` :1061-1073 的 direct-grid 文件系统门原位保留，
语义升级为**纵深防御**：卫生后仍并存双成员，意味着白名单 staging 之外的
带外写入/篡改（或卫生与门之间的窗口内写入），继续 fail-closed 正确。消息
中「most likely cause is a prior attempt that staged a different
station-index identity …（prior-attempt residue）」假说段与「Remediation:
manually remove …」人工处置指引改写为带外写入假说 + 指引报告而非手删
（残留成因已被卫生消除，旧假说会把操作员引向错误的排障方向）。
`Direct-grid staging copies only manifest-allowlisted members` 句保留。

## D5 规格修订：残留 vs 污染两分

`ambiguous index membership fails closed` 场景现文（spec.md:131-135）把
「manifest 声明多成员」与「staged filesystem 并存」合并为同一 fail-closed；
修订为：

- **manifest 声明多成员** = 真实 package 污染 → 继续 fail-closed（消费侧
  `_direct_grid_runtime_checksum_entries` :2049-2054 manifest 门，未动）。
- **staging 前工作区残留**（文件系统有、manifest 未声明）→ 自愈删除，
  不再进入歧义门；删除失败 → `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`
  fail-loud 终止。
- **卫生后文件系统仍并存** → 歧义门 fail-closed（纵深防御）。

requirement 主段落同步补一句 staging 前卫生义务；非 direct-grid 场景
（spec.md:137-141）与缺席/错码场景（:143-151）原文不动。

## D6 兼容与回归面

- 非 direct-grid：结构性零触碰（D2），既有 residual-member 解析场景测试
  保持绿，另加「残留成员在非 direct-grid staging 后仍存活」的显式反例锚
  （证卫生未泄漏到该车道）。
- legacy-only 历史包（manifest 只声明 `shud/qhh.tsd.forc`）：声明集合 =
  {legacy}，卫生删除对象 = {canonical}\{legacy} 中不存在的文件 →
  `missing_ok=True` no-op，行为不变。
- AC1 主锚（红-绿）：attempt N staging legacy 成员后失败；attempt N+1
  manifest 只声明 canonical → 修前 `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`，
  修后 staging 成功且 `shud/` 只剩声明成员。
- 零声明成员（manifest 不含任何 accepted index 成员，
  `_direct_grid_runtime_checksum_entries` 返回 `[]`，:2043-2047）：声明
  集合为空，卫生删除两个成员。修前该形会被 `_stage_standard_shud_forcing`
  当作单成员**消费掉上一 attempt 残留**而「成功」（陈旧数据）；修后残留
  没了、`staged_members` 为空，正确落
  `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING`——这是 spec
  `missing index membership fails closed for direct-grid` 场景的**收敛**，
  不是回归。

## Invariant Matrix（pin 的行为）

| # | 面 | 不变式 | 锚 |
|---|---|---|---|
| I1 | direct-grid staging | 写入前 `shud/` 中未声明 accepted index 成员被删除；删除集合 ⊆ `SHUD_FORCING_INDEX_MEMBERS` | tasks 2.1/2.6 |
| I2 | 失败编码 | 残留删除失败 → `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`，无静默继续；该码不在 TRANSIENT/NON_TRANSIENT 集合 | tasks 2.3 |
| I3 | manifest 门 | manifest 声明多 index 成员 → `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` 不被卫生掩盖 | tasks 2.2 |
| I4 | 纵深防御 | 卫生后文件系统仍并存 → 歧义门照抛（消息为带外写入假说） | tasks 2.5 |
| I5 | 车道隔离 | 非 direct-grid staging 不删除任何残留成员，manifest 锚定解析原样 | tasks 2.4 |
| I6 | legacy 消费 | legacy-only 包 staging 行为逐位不变 | tasks 2.4 |
