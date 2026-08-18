## 0. Probe（先探终态，写死进测试断言）

- [ ] 0.1 探针实测四站点在家族原语下 `~<不存在用户>/...` 的实际终态 reason/blocker
      （沿 #1424 task 0 先例：预期 `db_free_allowed_root_relative` /
      `db_free_selector_path_relative` / preflight 既有 blocker 臂，以实测为准入测试）

## 1. Implementation

- [ ] 1.1 `services/orchestrator/scheduler_preflight.py:534`（`_preflight_allowed_roots`）、
      `:587`（`_storage_root_check`）：`Path(os.path.expanduser(...))` 家族原语替换
- [ ] 1.2 `services/orchestrator/retry.py:1629`（`_db_free_selector_allowed_roots`）、
      `:1667`（`_db_free_selector_path_rejection`）：同 1.1；**不碰** `:1670-1673`
      resolve 行及其 except（#1400 范围）
- [ ] 1.3 `packages/common/object_store.py:48`：root 展开纳入既有错误转换边界，
      不可展开抛 `ObjectStoreError`（消息含原始 root 值）；确认不可展开 root 不触发
      任何目录创建
- [ ] 1.4 ride-along：删除 `scheduler_state_failure.py` 死包装 `_artifact_uri_is_missing`
      （删前复核零调用方；发现活调用方则保留并记偏离）

## 2. Tests

- [ ] 2.1 四站点 × 两触发面（`~<不存在用户>/...`；monkeypatch 家目录探测的 HOME-less
      `~/...`）：不抛，产出 task 0 实测的结构化结果
- [ ] 2.2 receiver 判别式钉测 lane 扩到四函数（钉 receiver 非 attr 名），保留非空洞断言
      （lane 内 `os.path.expanduser` 调用数 > 0）
- [ ] 2.3 `_slurm_preflight` 级门测：`allowed_storage_roots`/storage root 配成不可展开
      tilde 时整函数返回结构化 blocker 集合而非抛出
- [ ] 2.4 `LocalObjectStore` 行为门测：两触发面不抛裸 RuntimeError 而抛 `ObjectStoreError`；
      不创建字面 `~...` 目录；`_artifact_uri_missing_status` 在
      `OBJECT_STORE_ROOT=~<不存在用户>/store` + object 腿 uri 下返回
      `(True, "artifact_probe_error")`；`_forcing_sidecar_provenance` 同 root 返回
      `sidecar_unreadable`
- [ ] 2.5 零回归锁：绝对路径 / 可展开 `~` 在五处判定逐字不变（含 ENOENT、db-free
      词法回退臂）
- [ ] 2.6 object-store 既有套件回归：`tests/test_object_store_roots.py`、
      `tests/test_object_store_forcing.py` 全绿

## 3. Verification

- [ ] 3.1 红证：2.1/2.3/2.4 新用例在改动前红（本抛型全版本一致，当前 3.14 即可红，
      无需隔离 3.11 环境；记录改动前异常形态）
- [ ] 3.2 `uv run pytest -q tests/test_production_scheduler.py` 全量 +
      `uv run pytest -q tests/test_object_store_roots.py tests/test_object_store_forcing.py`
- [ ] 3.3 `uv run pytest -q tests/test_production_scheduler.py -k "artifact or sidecar or expanduser or tilde"`（#1441 Verification 原文选择器）
- [ ] 3.4 `uv run ruff check services tests packages`
- [ ] 3.5 openspec validate expanduser-throw-face-residue --strict --no-interactive
- [ ] 3.6 merge 后 node-27 oracle receipt：定向选择器 + object-store 套件，记入 #1436/#1441
