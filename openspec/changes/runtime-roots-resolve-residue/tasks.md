## 1. Implementation

- [ ] 1.1 `:597` `_config_path_preserve_final_component`：父段 realpath(strict=True)+except OSError→非 strict
- [ ] 1.2 `:604` `_config_path_relative_to_preserve_final`：同 1.1（相对基几何保持）
- [ ] 1.3 `:558` `_confined_path`：parent 同范式取规范形，交 `_require_under_workspace` 判
      （两臂收敛结构化 ValueError；containment 语义与字段名文案不变）
- [ ] 1.4 核对 `scheduler_config.py:883-899`/`:954-969` 三个 *_for_mode wrapper 的 db-backed 臂
      在新范式下无需再兜（或按需最小调整），db-free 臂零改动

## 2. Tests（版本无关写法；判别力在 ≤3.12 臂，CI 3.11 / node-27 3.11.15 为回归护栏）

- [ ] 2.1 helper 直测：三站点 × 末段环 / 父段环（tmp_path symlink 自指环），断言不抛
      RuntimeError、产物与非 strict resolve 语义一致
- [ ] 2.2 config 构造级：末段环 WORKSPACE_ROOT 与 NHMS_SCHEDULER_LOCK_ROOT →
      同一结构化 ValueError（含字段名），构造不崩
- [ ] 2.3 config 构造级：父段环 OBJECT_STORE_ROOT（非 containment-base 代表）→ 构造 OK，
      规范形与 3.13+ 一致（值断言）
- [ ] 2.4 ENOENT 合法路径回归：不存在但无环的深路径构造 OK、产物不变
- [ ] 2.5 非环输入 containment 拒绝语义回归（must be under workspace_root 文案/类型不变）

## 3. Verification

- [ ] 3.1 红证：2.1/2.2 新用例在改动前于 **3.11 隔离环境**红（RuntimeError），
      形态：`UV_PROJECT_ENVIRONMENT=/tmp/venv311-1520 uv run --python 3.11 ...`（绝不裸 --python 覆盖项目 .venv）；
      本地 3.14 上同用例改动前后的行为差异如实记录（3.13+ 无崩溃臂，判别力声明写进 PR body）
- [ ] 3.2 issue Verification 步骤 4 两段脚本在 3.11 隔离环境修复后不再抛 RuntimeError（矩阵重测记录）
- [ ] 3.3 uv run pytest -q tests/test_production_scheduler.py
- [ ] 3.4 uv run ruff check services tests
- [ ] 3.5 openspec validate runtime-roots-resolve-residue --strict --no-interactive
