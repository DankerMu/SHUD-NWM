# Tasks: direct-grid-input-attempt-hygiene

## 1. 实现

- [x] 1.1 `_stage_direct_grid_directory_artifact` 入口（写入循环前）加
      declared-member-anchored 卫生（design D3 代码形）：声明集合取
      `forcing_context.checksum_entries` ∩ `SHUD_FORCING_INDEX_MEMBERS`，
      删除 `destination` 下补集成员；`unlink_no_follow` +
      `containment_root=destination` + `missing_ok=True`。
- [x] 1.2 删除失败重编码：捕获 `SafeFilesystemError`/`OSError` → 抛
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED`（消息含成员相对路径 +
      原始错误），无两分支降级（#1164 样板）；docstring 声明删除范围合同
      （只动两元素集合补集）与 retry 分类依据（未知码默认 non-transient，
      不改 retry.py）。
- [x] 1.3 歧义门消息假说段改写（design D4）：`runtime.py:1061-1073` 的
      prior-attempt-residue 假说与手删指引改为带外写入假说；
      manifest-allowlisted 句保留；错误码与抛出条件不变。
- [x] 1.4 确认 `retry.py` 零改动（只读）：新码不在
      `TRANSIENT_ERROR_CODES`（硬边界）；是否登记进
      `NON_TRANSIENT_ERROR_CODES` 不由本 change 固定（design D3）。

## 2. 测试（tests/test_shud_runtime.py；先红后绿）

- [x] 2.1 AC1 主锚（红-绿）：同一 run_id，attempt N 以 legacy-only 包
      staging 成功（`shud/qhh.tsd.forc` 落盘）后模拟失败；attempt N+1 的
      manifest 只声明 `shud/stations.tsd.forc` → staging 成功、
      `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` 不抛、`shud/` 下 index 成员
      只剩 canonical（修前红：歧义门抛错）。
- [x] 2.2 AC2 污染锚：manifest 自身声明两个 index 成员 → 仍抛
      `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`（manifest 门，不被卫生掩盖；
      若既有测试已覆盖则指认并保持绿）。
- [x] 2.3 AC3 删除失败锚：残留成员 unlink 失败——**目录形与 symlink 形**
      两种被拒形状（safe_fs.py:394/:396）各一夹具，外加 monkeypatch unlink
      抛 OSError 的 I/O 形——→ attempt 以
      `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` 终止、staging 不继续
      （目标目录无本次成员写入）；并锚定该码不在
      `TRANSIENT_ERROR_CODES`（**只**锚这一个集合，design D3）。
- [x] 2.4 AC4 车道隔离 + legacy 回归：非 direct-grid staging 后残留成员
      仍存活（卫生未泄漏）且 manifest 锚定解析场景既有测试保持绿；
      legacy-only 包（声明集合={legacy}）staging 行为不变、无删除副作用。
- [x] 2.5 纵深防御锚：卫生后文件系统仍并存双成员（构造带外写入形）→
      歧义门照抛，消息含带外写入假说、不再断言 prior-attempt residue。
      复用既有
      `test_runtime_direct_grid_both_index_files_staged_on_disk_fails_closed`
      （tests/test_shud_runtime.py:1458，直调 `_prepare_shud_project_forcing`
      绕过 staging，天然是带外形）并把其 docstring 前提从「model package
      夹带」改为带外写入（model 包夹带现在会被卫生删除）。
- [x] 2.6 删除范围合同锚：`shud/` 下上一 attempt 的 station CSV 与无关
      文件在卫生后存活（只删 index 成员补集）。
- [x] 2.7 零声明锚（design D6）：direct-grid manifest 不声明任何 index
      成员 + `shud/` 有上一 attempt 残留 → 残留被删、以
      `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING` 终止，不再消费残留
      「成功」。

## 3. 验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_shud_runtime.py` 通过。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate direct-grid-input-attempt-hygiene --strict
      --no-interactive` 通过。
- [x] 3.4 spec-compliance 人工证据：D5 两分场景与实现逐句对读；歧义门
      消息与 spec 措辞一致。
