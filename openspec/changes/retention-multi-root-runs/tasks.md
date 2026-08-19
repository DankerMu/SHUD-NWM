## 1. 实现

- [ ] 1.1 `services/orchestrator/retention.py`：`plan_retention` 增可选入参
      `runs_only_roots: Sequence[Path | str] = ()` 与 `extra_roots_cutoff: datetime | None`、
      `extra_roots_retention_days: int | None`；对每个额外根**只调用** `_collect_run_targets`。
- [ ] 1.2 根解析与去重：
      (a) **先丢弃 `None` / 空串 / 纯空白**，再 `Path()` —— `Path("").expanduser().resolve()` 等于 CWD，
          会把 `<cwd>/runs` 拉进删除面（`NHMS_OBJECT_STORE_COPYBACK_ROOT` 未设即 `None`，
          `scheduler_config.py:186-188`；其 preflight 只在 db-free/repair-authority 模式下运行，`:700-706` 早退）。
      (b) 其余全部 `expanduser().resolve()`；与主根或彼此重复的一律丢弃（主根优先保留）。
      (c) 不存在或非目录的额外根静默跳过。
      (d) **裁定：额外根的收集独立于主根可用性** —— `plan_retention` 在 `object_store_root is None` 或
          非目录时会早退（`retention.py:356-360`），额外根收集必须放在早退之前，
          否则「主根未配 → 额外根静默失效」（CLI 主根来自 `os.getenv("OBJECT_STORE_ROOT")`，未设即 `None`，该分支可达）。
- [ ] 1.3 `RetentionTarget` / `_target_payload` / skipped / failed 条目携带 `root`（resolve 后绝对路径字符串）。
- [ ] 1.4 `to_dict()`：`schema_version` → `nhms.production_scheduler.retention.v2`；新增顶层
      `extra_roots: {enabled, retention_days, cutoff, roots: [...]}`。
      闸门关闭时该块的取值一并钉死：`enabled=false`、`roots=[]`、`retention_days` 与 `cutoff` 取**配置值**
      （即使未扫描也如实反映配置，便于判读「开了会删什么」），不得为 `null` 或省略。
- [ ] 1.4b **`extra_roots` 必须进 evidence 压缩白名单**：把它加入
      `services/orchestrator/scheduler_evidence_payload.py:662-676` `_compact_retention()` 的保留键元组，
      与 `frontier`（`:669-673`）同理由——定长标量块，压缩后必须存活。不改则 receipt 一压缩就丢掉窗口归属，
      D3 升 v2 的收益归零。
- [ ] 1.5 `RetentionConfig.from_env()` 增 `extra_roots_enabled`（`NHMS_RETENTION_EXTRA_ROOTS_ENABLED`，默认 `false`）
      与 `extra_roots_retention_days`（`NHMS_RETENTION_EXTRA_ROOTS_DAYS`，默认 `30`）。
      新字段加默认值是必要的（`cli.py:138-142` 的构造点否则编译不过），但**正因如此**每个构造点都必须
      显式经 `from_env()` 取值（见 1.9(a)），否则默认值会静默顶替 env——测试 2.11b 专门钉这一点。
- [ ] 1.6 `run_retention` 计算第二个 cutoff 并转发；闸门关闭时 `runs_only_roots` 传空元组。
- [ ] 1.7 调用点转发：`services/orchestrator/scheduler_runtime.py::_run_retention` 传
      `workspace_root` 与 copyback 根；`services/orchestrator/scheduler_core.py` 同步签名。
- [ ] 1.8 copyback 根来源：**消费既有的 `SchedulerConfig.object_store_copyback_root`**
      （`services/orchestrator/scheduler_config.py:186`，已读 `NHMS_OBJECT_STORE_COPYBACK_ROOT`
      并在 `:761`/`:783`/`:809`/`:820` 走 preflight 与 db-free 拓扑校验），在调用点
      `scheduler_runtime.py::_run_retention` 与 `workspace_root` 同样方式取用。
      **不得**在 retention 内新开 `os.getenv`——那会绕过既有 preflight。
- [ ] 1.9 `services/orchestrator/cli.py::_run_cleanup`（`:121-152`）：额外根的取用与窗口语义必须**显式钉死**，
      两点各自裁定并写进 PR 描述：
      (a) **额外根来源**：CLI 没有 `SchedulerConfig`，其主根来自 `os.getenv("OBJECT_STORE_ROOT")`。
          裁定为同样从 env 读 `WORKSPACE_ROOT` 与 `NHMS_OBJECT_STORE_COPYBACK_ROOT`，
          并受同一个 `NHMS_RETENTION_EXTRA_ROOTS_ENABLED` 闸门约束（闸门关则 CLI 也不扫额外根）。
          **闸门与窗口都必须经 `RetentionConfig.from_env()` 取得**：CLI 现在构造的是**全新**
          `RetentionConfig(enabled=..., dry_run=..., retention_days=...)`（`cli.py:138-142`），只从
          `base = RetentionConfig.from_env()`（`:137`）取 `retention_days`。`RetentionConfig` 现有字段无默认值
          （`retention.py:63-68`），新增字段一旦带默认，这个构造点就会**静默吃掉 env 值**。
          裁定：改用 `replace(base, enabled=True, dry_run=dry_run, retention_days=...)`，
          不得新建一个会回落 dataclass 默认的实例。
          **只接受 env 显式给出的非空值，未设即不扫该根**；不得套用 `SchedulerConfig.workspace_root` 的
          相对默认 `.nhms-workspace`（`scheduler_config.py:83`）——既有 spec 已点名「相对默认在错误工作目录下
          静默错解」这个坑（`openspec/specs/production-scheduler-orchestration/spec.md:103`），
          在删除面上照抄比在证据面上危险一个量级。
      (b) **`--retention-days N` 的作用域**：裁定为**只覆盖主窗口**，额外根窗口仍取
          `NHMS_RETENTION_EXTRA_ROOTS_DAYS`；`--help` 与 docstring 必须写明这一点。
          （反例：若让它同时覆盖两者，`cleanup --retention-days 1 --execute` 会把额外根按 1 天大规模删除。）
      fail-closed 前沿语义不得改动。
- [ ] 1.9b 额外根删除安全（design.md D6）：额外根的 `runs/` 若为 symlink → 跳过该根并记入 `skipped`
      （reason 可定位，不静默）；额外根下的删除改用
      `packages/common/safe_fs.rmtree_no_follow(path, containment_root=<resolved root>)`
      （先例 `services/orchestrator/run_tree_copyback.py:14,354,357`）。主根删除路径保持不变。
      **`_delete_entry`（`retention.py:450-458`）的 except 子句必须改为 `(OSError, SafeFilesystemError)`**：
      `SafeFilesystemError` 是 `RuntimeError` 子类（`packages/common/safe_fs.py:10`），不是 `OSError`；
      逃逸后 pass 侧塌成 `{"status":"error"}`（`scheduler_runtime.py:2003`），
      CLI 侧因 `cli.py:146-154` 无 try/except 而直接抛栈退出。
- [ ] 1.10 `infra/env/compute.example` 与 `infra/env/compute.scheduler-dbfree.env.example` 增两个旋钮
      （默认值与代码默认一致，附一行说明「开启前先 dry-run 审清单」）。

## 2. 测试（`tests/test_retention.py` 为主）

每行给出输入与期望输出，对应 design.md 的 Regression rows。

- [ ] 2.1 额外根回收：workspace 根 `runs/fcst_gfs_<老 cycle>_m`（含 `input/`/`output/`/`logs/`/
      `state_checkpoint_recovery/`）→ 进入 `planned` 且被删除，条目 `root` == workspace 根。
- [ ] 2.2 **runs-only 钉死**：额外根下同时放 `raw/gfs/<老 cycle>`、`canonical/gfs/<老 cycle>`、
      `forcing/gfs/<老 cycle>` → `planned` 中**零条**来自这三个前缀（断言按 `root` + `key` 前缀过滤）。
- [ ] 2.3 双窗口独立：同一 cycle 同时存在于主根与额外根，cycle 早于主 cutoff、晚于额外 cutoff
      → 主根条目在 `planned`，额外根条目在 `skipped` 且 `reason == "within_retention_window"`。
- [ ] 2.4 闸门关闭：`extra_roots_enabled=False` 且额外根有大量老 run
      → `planned`/`deleted`/`freed_bytes` 与不传额外根时**逐 key 一致**，额外根未被扫描。
- [ ] 2.5 同根去重：三根传同一路径 → 每个目标在 `planned` 中恰好一次，`freed_bytes` 不翻倍。
- [ ] 2.6 缺失根：额外根不存在 / 存在但无 `runs/` → 无条目、不抛异常。
- [ ] 2.6b **空根不得解析成 CWD**：`NHMS_OBJECT_STORE_COPYBACK_ROOT=""`（及未设）+ CWD 下存在
      `runs/<老 canonical run id>` → 该目录**不进** `planned`、磁盘上仍存在、`extra_roots.roots` 不含 CWD。
      同用例加一分支：`OBJECT_STORE_ROOT` 未设（主根 `None`）但额外根有老 run → 按 1.2(d) 的裁定断言
      （额外根仍被扫描并产生目标）。
- [ ] 2.7 非 canonical 名字：额外根 `runs/not-a-run-id` → `skipped` 且 `reason == "unparseable_run_cycle"`，
      永不进入 `planned`。
- [ ] 2.8 前沿豁免在额外根生效：cycle 早于额外 cutoff 但 >= `active_lower_bound`
      → `skipped` 且 `reason == pipeline_frontier_exempt`。
- [ ] 2.9 保护语义：额外根下 `runs/` 内的 `published_artifact_root` 指向目标 → `protected_path` 跳过。
- [ ] 2.9b **symlink 逃逸**：额外根的 `runs/` 是指向根外目录的 symlink（根外目录含老 run）
      → 该根零目标，`planned` 中不出现任何根外路径，根外目录在磁盘上仍存在；该根记入 `skipped`。
- [ ] 2.9c **containment**：额外根下某 `runs/<run_id>` 内含指向根外的 symlink 子树
      → 删除后根外目标仍存在（`rmtree_no_follow` 不跟随）。
- [ ] 2.9d **frontier 块保真**：v2 receipt 仍含 `frontier`（`active_lower_bound`/`source`/`protected_count`），
      形状与 v1 一致 —— `services/orchestrator/retention_frontier.py:133-134` 是既有消费者。
- [ ] 2.10 receipt v2 形状：`schema_version == "nhms.production_scheduler.retention.v2"`；
      `extra_roots` 块四个键齐全；`planned` 每条含 `root`。
- [ ] 2.10b **压缩后 `extra_roots` 存活**：输入 = 额外根产生数千条 `planned`、触发
      `MAX_EVIDENCE_BYTES`（`services/orchestrator/scheduler_evidence.py:23`）压缩的 pass evidence；
      期望 = 压缩后的 `retention` 块仍含 `extra_roots` 四个键与 `frontier` 三个键，
      `planned` 明细可以只剩 `planned_count`。
- [ ] 2.11 更新 `tests/test_production_scheduler.py:18601/18647/18704` 三处 `schema_version` 断言至 v2。
- [ ] 2.11b **CLI 窗口作用域（期望值必须偏离默认，否则测不出接线 bug）**：
      env 设 `NHMS_RETENTION_EXTRA_ROOTS_DAYS=7`（**非默认值**），额外根放一个约 10 天龄的 run；
      调用 `_run_cleanup(retention_days=1, dry_run=True)` 且闸门开
      → 主根按 1 天判定；额外根按 **7 天**判定，该 10 天龄 run 进入 `planned`
      （若实现回落到 30 天默认，它会落在 `within_retention_window`，用例即红——这正是本用例的目的）；
      `extra_roots.retention_days == 7`。闸门关时 CLI 不扫任何额外根。
      **不得**把期望窗口写成默认值 30，那样的用例对「env 未被读取」这个缺陷恒绿。
- [ ] 2.12 删除失败不中断（**异常类型必须钉死**）：输入 = 额外根下某 `runs/<run_id>` 删除时
      `rmtree_no_follow` 抛 `SafeFilesystemError`，`kind="unsafe"` 与 `kind="io"` 各一例；
      期望 = 该条进入 `failed` 并带可判读 error，**其余 planned 条目继续删除**，
      `freed_bytes` 只计成功项，函数正常返回（保 `retention.py:20` 模块契约与
      `scheduler_runtime.py:2003` "cleanup must never abort scheduling"）。
      仅用 `OSError` 写此用例**不算通过**——`SafeFilesystemError` 是 `RuntimeError` 子类，不会被 `except OSError` 捕获。

## 3. 风险包与证据映射

| 风险包 | 选中 | 证据 / 理由 |
|---|---|---|
| File IO / path safety / overwrite | **selected** | 2.1/2.2/2.5/2.6/2.6b/2.7/2.9/2.9b/2.9c/2.12：删除面扩大，根来自 config，需钉死 runs-only、去重、保护前缀、失败隔离、**symlink 逃逸与 containment**（design.md D6：`_iter_dirs` 只过滤子项，`runs_root.is_dir()` 会跟随 symlink，`_delete_entry` 今天是裸 `shutil.rmtree`）。 |
| Schema / columns / units / field names | **selected** | 2.10/2.10b/2.11：receipt v1→v2 + `root` 字段 + `extra_roots` 块，**含压缩路径存活**（1.4b）。 |
| Config / project setup | **selected** | 1.5/1.10/2.4：两个新 env 旋钮，默认值即「零行为变化」。 |
| Error handling / rollback / partial outputs | **selected** | 2.12（`SafeFilesystemError` 两种 kind，逐条隔离、其余继续删）+ 闸门回滚（关 env 即回到变更前）。 |
| Resource limits / large input / discovery | **selected** | 额外根 3375 目录走 NFS `_dir_size`；窗口/前沿跳过在 `_dir_size` 之前裁定（2.3/2.8 间接覆盖）；pass 耗时增量在 22 dry-run 时量。 |
| Documentation / migration notes | **selected** | 1.10 的 env 说明 + PR 描述记录 v1→v2 迁移与两步上线。 |
| Public API / CLI / script entry | **selected** | `plan_retention`/`run_retention` 新增入参虽全可选且默认保持行为，但 `cleanup` CLI 的 `--retention-days` 语义需重新界定（只覆盖主窗口，见 1.9(b)），属 flag 含义变化。证据：2.11b。 |
| Auth / permissions / secrets | not selected | 不涉及凭据或权限判定。 |
| Concurrency / shared state / ordering | **selected** | 本变更制造了一条**跨节点 生产者/消费者** 关系：node-22 retention 删 `<copyback_root>/runs/`，而 node-27 的 ingest autopipeline（`scripts/node27_autopipeline.py:709-714`，`nhms-node27-autopipe.timer` 约 30 分钟一次）正从同一 NFS 目录读同一批 run。安全性依据：已 ingest 的 run 由 DB 跟踪跳过（`:867-886`），ingest 滞后为分钟级，30 天窗口余量三个数量级。证据：design.md D2 第 2 条的残余风险记录 + 上线走 dry-run 审清单（Evidence Floor 末条）。 |
| Legacy compatibility / examples | not selected | 仓内 `schema_version` 无外部消费者（只有产出侧 + 3 处测试断言），无归档 receipt 回读路径。 |
| Release / packaging / dependency compatibility | not selected | 无新依赖、无打包面改动。 |
| 域包：Hydro-met 时间序列 / forcing 窗口 | **selected** | 正是本变更的 Non-Goal 支点：额外根 `forcing/` 是 display 服务面，2.2 钉死不碰。 |
| 域包：Geospatial / CRS / basin geometry | not selected | 不解析几何或投影。 |
| 域包：SHUD 数值运行时 | not selected | 不触碰 SHUD 运行或数值行为。 |
| 域包：PostGIS / TimescaleDB | not selected | 纯文件系统面，不连 DB。 |
| 域包：Slurm 生产生命周期 | not selected | 不触碰调度提交/轮询。 |
| 域包：外部气象 provider | not selected | 不触碰下载或快照。 |
| 域包：run manifest / QC provenance | not selected | 不改 manifest 或 QC 内容。 |
| 域包：已发布产物 / display identity | **selected** | `published_artifact_root` 保护须对所有根生效（2.9）；display 服务面零影响（2.2）。 |

## 4. Evidence Floor

- [ ] `uv run pytest -q tests/test_retention.py`
- [ ] `uv run pytest -q tests/test_production_scheduler.py -k retention`
- [ ] `uv run ruff check .`
- [ ] `openspec validate retention-multi-root-runs --strict --no-interactive`
- [ ] PR 描述记录：receipt v1→v2 迁移说明、闸门默认关、两步上线（22 dry-run 审清单 → enforce）
- [ ] 非合并门（运维后续，另行执行）：node-22 以
      `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true` + `NHMS_RETENTION_DRY_RUN=true` 跑一趟 pass，
      导出 `planned` 清单与 pass 耗时增量供人工审阅
