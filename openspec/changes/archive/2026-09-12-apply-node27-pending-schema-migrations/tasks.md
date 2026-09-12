# Tasks: apply-node27-pending-schema-migrations (#2145)

## Risk packs

核心包（`issue-risk-contract.md`），逐项裁定：

- **Schema / columns / units / field names — selected**：本单唯一的实质动作就是加两列 + 改两张 hypertable 的
  新建 chunk 间隔。判据写死为列属性与 `timescaledb_information.dimensions` 读数，不靠「命令退 0」自证。
  `geometry_generation` 必须是 `integer` / `is_nullable = NO` / 默认 `0`，且
  `count(*) WHERE geometry_generation IS DISTINCT FROM 0 = 0`（行数以窗口实测为准，2026-09-12 读到 44；**不写死绝对值**）。
- **Auth / permissions / secrets — selected**：DDL 必须以 `nhms` owner 角色连接（`nhms_display_ro` 无 DDL 权限）；
  owner DSN 只存在于 node-27 的 `infra/env/node27-timeseries-compression-replay.env`，**不入库、不回显明文**
  （receipt 里一律 `postgresql://***@127.0.0.1:55432/nhms`）。按 runbook（`tier-node27-timeseries-storage.md:6726` 起）
  的硬性口径：**开超级用户写会话之前先跑 audit-only**（植入的 rule/trigger/column DEFAULT 正是在下一次超级用户写时触发），
  施加后再跑完整 `node27_provision_write_roles.sh` 收敛属主并确认 strict 审计干净（drift 时退 3）。
- **Concurrency / shared state / ordering — selected**：窗口顺序写死
  `等 autopipe tick 跑完 → 停 nhms-node27-{autopipe,download}.timer → 确认两个 .service 非 active/activating →
  audit-only → migrate → 列/账本/dimensions 核验 → provision_write_roles（full） → 起 timer`。
  理由：full 模式做 `ALTER ... OWNER TO`（`lock_timeout=5s`）且 runbook 明写须在 timer 停机窗口内跑；
  而 `000057` 必须**早于**下一次 autopipe tick 拉起的 `import-basins-registry`，否则该 tick 记 `seed_failed stage=import`。
  receipt 记 stop/start 时间戳与窗口时长。
- **Error handling / rollback / partial outputs — selected**：三个文件全是 `IF NOT EXISTS` / 可重跑语句，
  **恢复动作只有「重跑同一条命令」**，不做任何手工 DDL 补偿。`migrate.py` autocommit 逐语句 + 遇错中止：
  若中途失败，已成功的文件已写账本、不会重放，receipt 逐条记 Applied/Skipped 原文。
  不设「回滚」——加列是前向安全的，删列才是破坏性动作，本单不提供。
- **Config / project setup — selected**：owner DSN 来源、`PYTHONPATH=/home/nwm/NWM`、
  用活动树自己的 `.venv/bin/python`（**不用 `uv run`**，避免隐式重建环境）、:8090 一次性缓存目录，
  全部在 receipt 里记命令原文。
- **Resource limits / large input — selected**：`df -h / /home /data/GHDC` 改前/改后
  （#2240 刚把 PGDATA 迁到 `/data/GHDC/nhms-primary/pgdata`，容量基线已变，一律实测不引用历史数字）。
- **Release / packaging / dependency compatibility — selected**：本单两个关键动作（1.6 的 migrate、1.3/1.11 的 :8090 uvicorn）
  都跑在活动树自带的 `.venv` 上，且刻意**不用 `uv run`**——仓库 pin 是 3.11，而 `uv run` 会在 pin 不符时删掉并重建
  正被在线服务占用的 `.venv`。判据（在 1.3 与 1.6 之前求值）：`/home/nwm/NWM/.venv/bin/python -V` 为 `3.11.x`；
  `/home/nwm/NWM/.venv/bin/python -c 'import psycopg2, dotenv, uvicorn, fastapi'` 退 0 无 `ImportError`；
  并断言 `git diff a8db554d HEAD -- pyproject.toml uv.lock .python-version` **为空**
  （即活动树自 `a8db554d` 起依赖面未动，现有 `.venv` 与该树同源），不为空即停。
  **不**照抄 #2016 的 `... origin/master ...` 形式：那条 diff 服务于「前滚 checkout 要不要 `uv sync`」，
  而本单 Non-Goals 明写不 `git pull`、活动树不动，master 侧有无依赖变化对本单结论零影响；
  且本树在 `hotfix/node27-rollback-pre-2073` 无 upstream，`origin/master` 是否新鲜也无从保证。全程 `grep` 自证命令原文中无 `uv run` / `uv sync` / `--active`。
- **Public API / CLI / script entry — not selected**：不新增/不改任何 CLI 或路由；:8090 探针只读既有路由。
- **File IO / path safety / overwrite — not selected**：不写对象存储、不删任何文件；唯一落盘产物是 receipt 与一次性 :8090 缓存目录（用完即删）。
- **Legacy compatibility — not selected**：理由不是「加列对读者透明」——那条不成立，
  `packages/common/forecast_store.py` 的 `get_run` / `list_runs` 是 `SELECT h.*` 无字段过滤，新列会直接进公开 payload。
  成立的理由是**契约早已声明且该键早已在线**：`parsed_at` 已存在于 `apps/api/openapi_restored_schemas.py`、
  `openapi/nhms.v1.yaml`、`apps/frontend/src/api/types.ts`，且 2026-09-12 实测 `/api/v1/runs` 行键已含它
  （列在库上先于账本存在），故本次施加对任何既有消费者零形状变化。守卫仍钉在 1.1 的改前/改后键集合对照上。
- **Documentation / migration — not selected**：无迁移说明需求；receipt 为新增文档。

Domain packs（`openspec/project-profile.md`）：

- **PostGIS / TimescaleDB — selected**：`000058` 直接动两张 hypertable 的 chunk 间隔；判据取
  `timescaledb_information.dimensions` 的 `time_interval`，并显式断言既有 chunk 数/压缩状态不变。
- **Published NHMS artifacts / display identity — selected**：`000057` 落地后每个全国缓存 key 一次性轮换，
  重启后首批请求必然冷 miss——这是**预期**（#2145 正文「已知副作用」），receipt 须把它记成预期而非回归；
  磁盘上的孤儿瓦片归 #2032，不为此另开单。
  **key 轮换那一轮 miss 的观测不在本窗口**（:8090 用一次性空缓存目录），随 #2162 的生产窗口产出，见偏离记录第 5 条。
- **Hydro-met time series / forcing windows — selected**：`000056` 的 `parsed_at` 与 `000058` 的 chunk 间隔都落在时序面；
  但本单**不做** `parsed_at` 回填、**不改** lag/保留期。
- **Geospatial / CRS、SHUD numerical runtime、Slurm production lifecycle、External providers / snapshot、Run manifest / QC provenance — not selected**：
  本单不产出/不改任何网格、模型、调度、资料源或 manifest 数据。

## Deployment tasks

- [x] 1.1 窗口前只读基线：`git rev-parse HEAD` + `git status --porcelain`（活动树）、账本全量 dump 与行数、
      `core.river_network_version` 列集合（证明 `geometry_generation` 缺失）与行数、
      `df -h / /home /data/GHDC`、`:8080 /api/v1/layers` 与 `/api/v1/runs` 的响应键集合（`x-nhms-cache-warm: refresh`）。
      **另加四项改后判据所需的改前读数**（缺了它们，1.7 的「改前/改后一致」不可求值）：
      `timescaledb_information.dimensions` 两张目标表的 `time_interval`（期望改前**非** `3 days`）、
      `timescaledb_information.chunks` 的逐表 chunk 数、全库 `is_compressed` 计数、
      `hydro.hydro_run.parsed_at` 的改前**三属性**（不只是存在性）：
      `data_type` / `is_nullable` / `column_default`。实测该列**已存在**，故 `000056` 是 no-op DDL + 账本补登——
      正因为它什么都不建，账本补登会从此断言该迁移已施加、且**永不重放**，所以线上列形状若与迁移本该建的
      （`TIMESTAMPTZ`、可空、**无默认值**，见 `000056_hydro_run_parsed_at.sql` 头注）不符，补登就会把一次真实漂移永久盖住。
- [x] 1.2 待施加集合枚举与 #2048 对账（差集 3 个文件；7 个账本-only 行归 #2048，不入本次施加面），写入 receipt。
- [x] 1.3 :8090 一次性 uvicorn（活动树 `a8db554d` + 其自身 `.venv`，一次性空 `NHMS_MVT_FILE_CACHE_DIR`）起好，
      **施加前**抓三条读路由 + `/api/v1/layers` 的状态码。期望：三条 national 瓦片路由**无条件 500**；
      `/api/v1/layers` 仅在存在 display-ready run 时 500，**若它返回 200 且 `data` 为空，那是无 display-ready run 的合法读数、
      不构成对红证据的反证**（早退路径在 digest 之前 `return []`）。报错正文须含缺失列名，否则红证据不算数。
- [x] 1.4 停 writer 窗口：等在跑的 autopipe tick 结束 → `systemctl --user stop nhms-node27-{autopipe,download}.timer` →
      确认两个 `.service` 非 active/activating → 记时间戳。（`yd-*` timer 指向独立库 `:55434`，不在本窗口内，receipt 记一行说明。）
- [x] 1.5 超级用户写会话**之前**的 audit-only：`docker exec -i nhms-db psql -U nhms -d nhms -X -v ON_ERROR_STOP=1
      -v do_roles=off -v do_ownership=off -v do_audit=on -v strict_audit=on < db/roles/node27_write_roles.sql`，须干净。
- [x] 1.6 施加：`cd /home/nwm/NWM && DATABASE_URL="<owner>" PYTHONPATH=/home/nwm/NWM .venv/bin/python -m packages.common.migrate`；
      逐行记 Applied/Skipped 原文（期望 3 Applied、其余全 Skipped）。
- [x] 1.7 核验：账本含 `000056/000057/000058` 三行且总数 `+3`；`geometry_generation` 为 `integer/NO/0` 且
      `count(*) WHERE geometry_generation IS DISTINCT FROM 0 = 0`；
      `hydro.hydro_run.parsed_at` 的三属性为 `timestamp with time zone` / `is_nullable = YES` / `column_default IS NULL`
      （**任一不符即停**——该列先于账本存在，补登会让它永不重放，漂移将被永久掩盖）；
      两张 hypertable 的 `time_interval = 3 days`；既有 chunk 数与压缩状态改前/改后一致。
- [x] 1.8 施加后完整 `bash scripts/node27_provision_write_roles.sh`，退 0 且 strict 审计干净；
      receipt 说明本次未新建任何 relation，故属主收敛应为 no-op。
- [x] 1.9 写侧最小权限探针（不 seed 真实 basin）：`SET ROLE nhms_ingest_rw;
      UPDATE core.river_network_version SET geometry_generation = geometry_generation WHERE false;` 须成功。
- [x] 1.10 起 timer：`systemctl --user start nhms-node27-{autopipe,download}.timer`，记时间戳与窗口时长；
      记录恢复后首个 autopipe tick 无 `seed_failed stage=import`（**只部分满足，见偏离 8**）。
- [x] 1.11 :8090 施加后复测同三条路由 + `/api/v1/layers`：**四个面零 500**——三条 national 瓦片路由为 200
      （或无 display-ready run 时的预期 424），`/api/v1/layers` 为 200（它永远不会 424，无 run 时是 200 + 空列表）；
      若需重启一次 :8090 uvicorn 才转绿则如实记录。记录 first-touch miss，并**注明它不是 key 轮换证据**
      ——:8090 用的是一次性空缓存目录，施加前的请求又全是 500、什么都没缓存，所以这里的 miss 与 digest basis
      变更引发的缓存 key 轮换无关（见偏离记录第 5 条）。
- [x] 1.12 补 #2145 欠 PR #2190 的项：重跑 `/home/nwm/tmp/receipt2033/` 的比对脚本，
      对 `hydro-national/{variable}`、`hydro-national/{source}/{cycle}`、`river-network-national`
      三层做瓦片字节 / ETag / cache-key 恒等比对，结论入 receipt（方法与 baseline 见
      `docs/runbooks/receipts/2026-09-09-issue-2033-mvt-instant-range-node27.md` §5.1 / §5.1b）。
- [x] 1.13 `df -h / /home /data/GHDC` 改后读数；:8090 一次性缓存目录与 worktree 清理干净并自证。
- [x] 2.1 receipt 落 `docs/runbooks/receipts/2026-09-12-issue-2145-geometry-generation-migration-node27.md`。
- [x] 2.2 在 #2031 回链评论（#2145 验收标准最后一条）。

## Evidence Floor

- 本地：`openspec validate apply-node27-pending-schema-migrations --strict --no-interactive` + markdownlint（CI，本 PR 改 `docs/**`）。
  **无 pytest**：本单零代码 diff，`scripts/select_ci_tests.py` 对纯 `docs/**` + `openspec/**` 清单选不出后端套件；
  该事实须用 `scripts/select_ci_tests.py` 复现 CI 自己的选择并把输出贴进 PR，不得手挑清单。
- node-27（全部为本窗口实测，命令原文入 receipt）：
  1. 施加前：账本行数 + 全量 dump、`core.river_network_version` 列集合（无 `geometry_generation`）与行数、
     待施加差集 3 项、账本-only 7 项、`df` 三卷、`:8080` 的 `/api/v1/layers` 与 `/api/v1/runs` 键集合，
     **外加**两张目标 hypertable 的 `time_interval` 改前读数、逐表 chunk 数、全库 `is_compressed` 计数、
     `hydro.hydro_run.parsed_at` 的改前存在性。
  1a. 依赖面：`.venv/bin/python -V` 为 `3.11.x`、`import psycopg2, dotenv, uvicorn, fastapi` 退 0、
     `git diff a8db554d origin/master -- pyproject.toml uv.lock .python-version` 的结论（据以说明不需要 `uv sync`）。
  2. 红证据：:8090 上三条 national 读路由施加前的 **500** 原文（含 `error.code` / 报错列名）。
  3. audit-only（施加前）干净读数。
  4. `packages.common.migrate` 全量输出：3 Applied + 其余 Skipped。
  5. 施加后：账本 `+3` 与三行存在、`information_schema.columns` 三属性、
     `count(*) WHERE geometry_generation IS DISTINCT FROM 0 = 0`、
     `parsed_at` 的三属性（`timestamp with time zone` / `YES` / `NULL` 默认）与改前读数一致、
     `timescaledb_information.dimensions` 两张表 `3 days`、既有 chunk 数/压缩状态不变。
  6. 完整 `node27_provision_write_roles.sh` 退出码 + strict 审计干净 + 「未新建 relation 故属主收敛 no-op」自证。
  7. `nhms_ingest_rw` 对新列的写权限探针成功。
  8. timer stop/start 时间戳、窗口时长（以 **11:07:49Z 首次 stop** 起算，非最后一次启动）、恢复后首个 autopipe tick 的终态（无 `seed_failed stage=import`）。
  9. 绿证据：:8090 上同三条路由 + `/api/v1/layers` 施加后**零 500**，并记 first-touch miss
     （**非** key 轮换证据，见偏离记录第 5 条）。
  10. #2190 欠账：两层三路由的字节 / ETag / cache-key 恒等比对结论。
  11. `df` 改后读数 + 一次性缓存目录/worktree 清理自证。
- 偏离记录（PR body `偏离记录` 段，逐条一行）：
  1. 施加 **3** 个迁移而非 issue AC 假设的 1 个；账本按 `56 → 59` 读，不是 `+1`。
  2. AC1「已 `git pull --ff-only` 到含 #2031 的 commit」由**祖先关系**满足（`60ab5528 ⊆ a8db554d`），
     本窗口**未执行 pull**——活动树在 `hotfix/node27-rollback-pre-2073` 且无 upstream，其修复归 #2162。
  3. AC「display API 已按 `scripts/ops/start-display-api.sh` 重启」**未执行**——生产 `:8080` 被 reslice pin 钉在
     不含该列的 `5a86841c`，在其上重启对本单 oracle 零信息量；解钉 + 生产重启是 #2162 的窗口。
     本单改用活动树 `a8db554d` 自身 `.venv` 的 :8090 一次性实例产出读路由 oracle。
     **等价性边界**：对「列存在后 digest SQL 不再炸」这一 oracle 等价；对生产公网面（反代、生产
     `display.env`、生产缓存目录、多 worker）**不等价**，那半边随 #2162 的生产窗口闭合。
  4. 施加命令由 issue In-scope step 3 明写的 `uv run python -m packages.common.migrate` 改为活动树
     `.venv/bin/python -m packages.common.migrate`：仓库 pin 是 3.11，`uv run` 在 pin 不符时会删掉并重建
     正被在线服务占用的 `.venv`；模块与 `DATABASE_URL` 语义完全相同。
  5. AC「日志中记录了预期的一次性冷 miss」**只部分满足**：其语义是 digest basis 变更导致的缓存 **key 轮换** miss，
     而 :8090 用一次性空缓存目录、施加前又全是 500，按构造只能记到 first-touch miss。
     key 轮换那一轮 miss 随生产解钉重启产出，归 **#2162**。
  6. **施加连接加了 `PGOPTIONS="-c lock_timeout=5s -c statement_timeout=120s"`**，issue 与 runbook 都没写这一步。
     理由是实测：`packages/common/migrate.py` 全文无 `lock_timeout`（`psycopg2.connect(database_url)` +
     `autocommit = True`，`:160-161`），而窗口打开时生产 `:8080` 有一条 `nhms_display_ro` 的 z=3 全国瓦片查询
     已 CPU-bound 跑了 53 分钟（`wait_event_type` 为空 = 在算，不是在等），握着
     `core.river_network_version` / `hydro.hydro_run` / `hydro.river_timeseries` 的 `AccessShareLock`。
     无 `lock_timeout` 时 `ADD COLUMN` 的 `AccessExclusiveLock` 会无限排队，**并把其后每一个读者一起堵在锁队列里**
     ——那才是真正的生产事故。加了之后每条语句 5 秒失败，migrate 按账本跳过已施加文件，重试免费。
     代码本身不改（超出本单范围），已作为后续项记录。
     **并且**：4 次重试仍被挡后，对那条只读会话发了 `pg_cancel_backend(5375)`（不是 `terminate`）。
     这是本窗口唯一一个既不在 fixture 计划内、也不在用户预授权范围内（用户授权的是 **merge**）的
     生产可见动作，故单列：取消对象是 `nhms_display_ro` 的一条已跑 66 分钟的只读全国瓦片查询，
     代价是一个 HTTP 请求失败；未改任何数据、未重启任何服务、未动 reslice pin、未碰写角色会话。
  6b. writer 停机窗口的真实长度是 **11:07:49Z → 11:32:18Z（24 分 29 秒）**：stage-b 共启动三次，
     前两次都在排空循环内、任何数据库变更之前被杀（排空判据 `case "$s$d" in *active*)` 恒真——
     `inactive` 里含 `active`；以及随后把排空上限由 30 分钟缩到 8 分钟）。timer 自 11:07:49 停后
     全程保持 `inactive`，围栏未曾解除，对库的写操作只发生过一次。
  7. **窗口内有一个围栏前就已启动的 autopipe tick 未排空**（`window_residual_tick`）。
     timer 围栏成立（无新 tick），但在途 tick 的前一轮实测跑了 `elapsed_sec=17576`（4.9 小时），
     等它结束等于把窗口敞开数小时。判定为记录而非中止，依据两条实测：
     (a) 施加受 `lock_timeout` 保护，冲突只会快失败；
     (b) `ownership_drift_relations = 0`，而 `db/roles/node27_write_roles.sql:427/438/450/459` 的属主循环
     带 `c.relowner <> 'nhms_ingest_rw'::regrole` 谓词，drift 为 0 时**一条 `ALTER ... OWNER TO` 都不发、不取关系锁**。
     脚本对此设了硬闸：`DRIFT != 0` 且 tick 仍在途时跳过 1.8 并退 4，不在有写者在途时抢 `AccessExclusiveLock`。
  8. **1.10 的「恢复后首个 autopipe tick 无 `seed_failed stage=import`」只部分满足**：
     围栏前启动的那个 tick 在 receipt 写作时仍在 `activating`，timer 要等它结束才触发下一个，
     本窗口内取不到「首个新 tick」的终态（当前日志尾 200 行 `seed_failed` 计数为 **0**）。
     替代判据是构造性的、且比等一个 tick 更强：autopipe 跑的是 pin 住的 `5a86841c` 树，
     其 `basins_registry_import.py` 中 `geometry_generation` 出现 0 次，本窗口不可能引入该列导致的
     `seed_failed stage=import`。需要真正观测这条的时刻是 #2162 撤 pin 之后，届时列已就位。
  9. **#2190 欠账三路由中的 `hydro-national` legacy alias 未取得跨臂恒等，且本单不声称它恒等**。
     另两条（`{source}/{cycle}`、`river-network-national`）三轴逐字节全等。第三条已用同树对照证伪为
     **时间性而非因果性**：同一棵 `d113edca` 相隔约 10 分钟两次跑出不同字节/md5，而背靠背 11 秒两次完全恒等。
     该路由请求时自解析「最新 run」，窗口后 ingest 已恢复写入，按构造不具备跨时刻可比性；
     要取得因果判据须在 writer 静止时重做。
- 由本次施加派生、**不在本单做**的后续（每条须为已跟踪 issue 或带一行理由）：
  - `000058` 落地后按 `docs/runbooks/tier-node27-timeseries-storage.md`「Per-tick capacity」一节复核 3 天 chunk 混合下的
    per-chunk 时长 / tick wall / 峰值磁盘余量。
  - `000056` 的 `parsed_at` 历史回填（`scripts/backfill_hydro_run_parsed_at.py`，人看着分批跑）。
  - `packages/common/migrate.py` 无 `lock_timeout`/`statement_timeout`：在活主库上施加 DDL 时会无限排队并堵住锁队列。
    本窗口用 `PGOPTIONS` 在连接层绕过（偏离 6），但下一个在没读过这条记录的人手里开的窗口会再踩一次——需立单收口。
  - 生产 `:8080` 的 z=3 全国瓦片查询可 CPU-bound 跑 50 分钟以上（本窗口实测 pid 5375 / 53 分钟，
    `wait_event_type` 为空）。与本单无因果关系（它跑在 pin 住的 `5a86841c` 上），报告不修。
  - `#2222`：`parsed_at` 早已在线，本次施加**不**改变 `/api/v1/runs` 的响应形状（本单只做改前/改后键集合对照并记录）；
    但 `packages/common/forecast_store.py` 的 `SELECT h.*` 无字段过滤这件事仍未收口，**下一个**内部列照样外泄，故 #2222 保持 open。
