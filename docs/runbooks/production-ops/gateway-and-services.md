**分册：流域投递、Slurm Gateway、API / 展示与监控**

本页是当前生产值守手册的 §3.1.4、§3.2、§3.3、§3.4 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

#### 3.1.4 流域投递规范（发给建模者；平台侧照此验收）

新流域或更新版本交到 `/volume/nwm/Basins/` 之前，建模者必须满足下面七条。

> **先分清两个 Basins 根，别投错地方**（2026-08-22 实测）：
>
> | 路径 | 载体 | 谁读它 |
> |---|---|---|
> | node-22 `/volume/nwm/Basins` | 本地 175 T xfs (`/dev/sda`) | **投递落点与权威**：`NHMS_BASINS_ROOT`，scheduler / baseline publish / provision 全走它 |
> | node-22 `/ghdc/data/nwm/Basins` ≡ node-27 `/home/ghdc/nwm/Basins` | NFS `ghdc:/home/ghdc`（同 inode，确为一份） | node-27 ingest 的 `BASINS_ROOT`；#1699 的 staged 树也在这儿 |
>
> 两棵树**内容本就不同**且是有意的：权威根放原始投递名（`CJ-DTH-XJ`），NFS 树放
> only-root staging 后的名字（`DTH_XJ`）。所以「两端不一致」不等于漂移——比对前先确认
> 比的是同一棵树。建模者只投 `/volume/nwm/Basins`，NFS 侧由平台按需 stage。
不满足的交付会在上线四跳的第 1 跳（baseline publish）或第 2 跳（provision）失败，
或者更坏——静默上线成一个错的永久身份。

**1. 目录名就是永久 `basin_id`，投递后改不了。**
顶层目录名经 `_slug_id()`（`[^0-9a-zA-Z]+` → `_`，转小写）变成 `basin_id`：
`Huai-MAIN` → `basins_huai_main`，`SHJ-2SHJ` → `basins_shj_2shj`。
所以取短名、**不带分区/单位/人名前缀**。`basin_id` 一旦进注册表就嵌进
`basin_version_id` 和 dg `model_id` 的 hash 输入，改名等于换身份，
必须走一整套「新 id 注册 + 状态克隆 + 旧 id 退役」（见 #1698 / #1701），不是 rename。

**2. 二级容器目录名会成为 `basin_id` 前缀。**
`HYS/BST/input/BST/` → `basins_hys_bst`。要么别用二级容器，要么容器名本身也当作
永久标识来取。**深过两级的布局不会被发现**——`a/b/c/input/c/gis/` 在
`basins_discovery._find_model_dirs` 和两份 geo builder 里都被跳过，不报错、直接不存在。

**3. 结构固定为 `<顶层名>/input/<模型名>/`。**
`gis/`（含 `domain.shp` / `river.shp` 及其 `.shx`/`.dbf`/`.prj`）在
`input/<模型名>/gis/` 下。注意 `<模型名>`（`shud_input_name`）**可以**与顶层目录名不同，
这是 only-root staging 的正常结果（`SHJ-2SHJ/input/2SHJ/`）；但它决定
packaged IC 的规范路径 `<package>/<模型名>.cfg.ic`，写错就是 IC 探测失败。

**4. `cfg.ic` 首行必须是 3 列。**
少一列的 header 会让 SHUD 读 IC 失败。平台侧上线时会在 **staging 副本**上补
`\t0.000000`（#1699 补过 4 个流域），**不改源**——但这属于救火，交付时就应该是对的。

**5. `umask 002`。**
`/volume/nwm/Basins` 的默认 ACL 给 `nwmuser` 组写权限，但投递者的显式权限位会**覆盖**默认 ACL。
2026-08 出现过整棵树 20304 个文件对组只读、平台无法 staging 的情况。
交付后自查：`find <你的目录> -not -writable | wc -l` 应为 0（以 `nwmuser` 组成员身份）。

2026-08-25 复测：全树 **220** 个（不是历史峰值 20304），全部属 `st_zhanghx`、模式
`-rw-r-----` / `drwxr-s---`，集中在 7 个黄河流域（`longmen_zhi_sanmenxia` 32、
`lanzhou_zhi_hekouzhen` 32、`hekouzhen_zhi_longmen` 32、`sanmenxia_zhi_huayuankou` 31、
`neiliuqu` 31、`longyangxia_zhi_lanzhou` 31、`longyangxia_yishang` 31）。
**它复发在最新一批投递上**，即本条约束尚未被投递方执行；与 `forcing/` 零交集
（`find ! -writable -path "*/forcing/*"` = 0），不阻塞 forcing 清理。

本条即 #1702 长期方案的 (b) 支：**由投递者保证 `umask 002`**。另一支 (a)——把
`Basins/` 的 owner 改成平台账号 `nwm`——尚未采纳，属 owner 决策；在它落地之前，
(b) 是唯一在册的约束，一次性的 root `setfacl` 修复只是补救、不是机制。

**6. 率定更新原地覆盖，但必须通知平台。**
只改参数、不改 mesh/river/IC 的率定更新可以原地覆盖同一目录。
但**平台需要在覆盖前后各做一次动作**：包重新发布 + 状态延续克隆（`state_compatibility` 门，见 §5.7）。
不通知就覆盖 = 新包与旧状态之间没有克隆行，`REQUIRE_FORECAST_WARM_START=true` 下
下一个 cycle 直接 `state_clone_cold_start_approval_required` 停摆。

平台侧执行这一步时**顺序是硬的，且要留证**：先写克隆行、再发布 manifest。
反过来会在 `REQUIRE_FORECAST_WARM_START=true` 下 stall 一个 cycle。判定顺序一律看两份
receipt 的 `generated_at`，**不要看 `manifest-last.json.bak-*` 的 mtime**——备份是保留源
时间戳的拷贝，mtime 反映的是上一次发布，不是本次。#1698 的实测形状可照抄：

```text
receipts/<basin>-<cutover>-apply.json   generated_at 2026-08-22T06:42:07Z
                                        cloned_pair_count 2 / dry_run false /
                                        invocation_outcome complete
receipts/manifest-publish-<N>.json      generated_at 2026-08-22T07:02:41Z
                                        introduced_model_ids == 预期新身份集合
```

即克隆先于发布约 20 分钟。**不要把 provider refresh 的时序当兜底**——它只是碰巧
掩盖过顺序错误，不是保护机制。

**7. 改 mesh / river / IC 视为新版本，不是更新。**
这三者进 8 面指纹；变了就不是同一个模型，状态不可延续，必须冷启动或走新 id。
交付时明确写清属于第 6 条还是第 7 条。

**平台侧责任（不要建模者自己做）**：

- 旧目录**不要自删**。退役由平台 `mv` 到 `/volume/nwm/Basins-retired/issue-<N>-<slug>/`
  （同 xfs，rename 不拷贝），保留 90 天后由 owner 决定删除。自删会让还在引用该路径的
  注册行失去溯源根。
- `forcing/` 子目录（IDW 代站 CSV）**不要再带**。direct-grid 已不读：62 行注册表的
  `source_policy.forcing_source` 全部是 `node27_raw_handoff`，运行时 forcing 走
  object store（`manifest["forcing"]["forcing_uri"]`），从不读 Basins 树里的 CSV。
  2026-08-25 清理已执行（#1702 第 3 项）：**15 个目录全清**，共 **10040 个条目 / 62 G**
  移到 `/volume/nwm/Basins-retired/forcing-cleanup-20260825/`，全树 **66 G → 544 M**。
  清理**按 §5.5.1 的纪律**——清空目录、保留目录、不改名（含 `tailanhe/focing`
  这个拼写错误）。

  > `heihe/forcing`（12 G / 1711 文件）一度被错划进第 2 项「整目录退役」而缓做。
  > **划错了**：`heihe` 是活的生产流域（注册表 2 行 `active`、`basins_heihe_shud`
  > 的 `active_flag = t`、350 条 published run、在展示的流域集里），它 12 G 里的
  > 模型本体只有 `input/heihe/` 9.3 M，其余全是上一版率定留下的旧代站 CSV。
  > **流域是活的、forcing 是旧的**——属第 3 项，不属第 2 项。判一个目录该不该
  > 整体退役，看注册表和 `core.model_instance`，不要看它的体积。

### 3.2 Slurm Gateway

Slurm Gateway 当前仍在 node-22。它负责把调度/诊断请求转成 Slurm 行为；
node-27 display 不调用 Slurm Gateway。

四个 Slurm 变更操作（submit / array submit / cancel / enabled reset）要求
`Authorization: Bearer SLURM_GATEWAY_SERVICE_TOKEN`（路由级 scheduler 服务凭据，
constant-time 校验，固定 scheduler actor + operator role；reset 仍需 sys_admin，
scheduler token 到 reset 是 403）。token 只经 POST/DELETE 变更请求转发，
health/read 路由保持匿名且不携带。**没有该 token 时 scheduler preflight
fail-closed**（不会仅凭匿名 health 就报 submit-ready）。

确认 node-22 Gateway 与诊断 API：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
pgrep -af '[s]ervices.slurm_gateway|uvicorn apps[.]api[.]main'
ss -ltnp 2>/dev/null | grep -E ':(8000|8090)\b' || true
curl -fsS --max-time 2 http://127.0.0.1:8090/api/v1/slurm/health
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"
```

> 注：`/health`（bare）是 node-22 历史诊断 API 的路由（见本节末尾 2026-06-22
> 现场验证，当时在 `:8001` 返回 `{"status":"ok",...}`）；standalone Slurm
> gateway 的 health 实际是 `/api/v1/slurm/health`。8090 端口上只有
> `/api/v1/slurm/health`，不要混用两个路径。

#### 3.2.1 凭据与听端口边界（#1684）

- Gateway 只监听 loopback。**实测当前 live 端口是 `127.0.0.1:8090`**（2026-08
  现场观察，而非模板默认 8081）：`SLURM_GATEWAY_URL=http://127.0.0.1:8090`，
  `ss -ltnp` 显示仅 `127.0.0.1:8090`（有 IPv6 loopback 时另见 `::1`）。
- 共享凭据只存在于 untracked、owner-mode-0600 的 env 源
  （gateway unit 的 `EnvironmentFile=` 与 scheduler unit 的 drop-in
  EnvironmentFile 各引同一份）。**变量名入库，值永不入库/日志/OpenAPI/证据**；
  gateway 侧示例路径 `/opt/SHUD-NWM/infra/env/slurm-gateway.secret` 为通用模板，
  live node-22 实际路径以 drop-in 步骤里的 `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`
  为准。
- **进程级 loopback bind guard（可部署的等价网络漂移控制）**：
  `python -m services.slurm_gateway` 在 uvicorn 前 fail-closed，拒绝任何
  非 loopback bind（`0.0.0.0` / `::` / hostname / 非 loopback IP）；
  node-22 用户无非交互 sudo，这即是本 issue 要求的用户级等价第二控制。
- **HTTP 实现协议固定 h11（checked-in 模块入口已钉死）**：
  `services/slurm_gateway/__main__.py` 以 `uvicorn.run(..., http="h11")` 启动，
  规避 uvicorn 默认 `http="auto"` 落到 node-22 维护期活动 Python 3.12.7 环境里
  已实测损坏的可选原生 httptools（`AttributeError: module 'httptools.parser'
  has no attribute '__all__'`，gateway 在 bind 前 exit 1）。
  `UVICORN_HTTP=h11` 不影响程序化 `uvicorn.run`，只有显式关键字参数是确定性控制；
  这是**兼容性钉，不是维护窗口依赖修复**，不授权 `uv sync`。
- root 管理的 host packet-filter/ACL deny 规则属于**可选**的更强防线：仅在
  实际存在并留证时记录（本 run 的 PASS 不依赖它）；remote negative probe +
  bind-guard 拒绝即 live receipt。

#### 3.2.2 协调 rollout / rollback（先 runtime ConditionPathExists 围栏，再备份，再配，再启用）

先用可逆的 runtime `ConditionPathExists` drop-in 围住调度器（2026-08-28 现场观察到
`systemctl --user stop nhms-compute-scheduler.timer` 被另一个并发同用户维护会话
显式启动 timer 两次撤销；而运行中的 scheduler pass 必须自然跑完，不能 kill；
**`mask --runtime` 在这个拓扑上无效且被禁止**——对 `~/.config/systemd/user` 下的
persistent user unit，实测 `mask --runtime` 只造出
`/run/user/1103/systemd/user/<unit> -> /dev/null`，`is-enabled` 仍为 `static`、
`LoadState=loaded`，`start` 照常成功/active（2026-08-28 throwaway probe 已证明并清理）。
所以围栏用**已实测生效的 runtime condition drop-in**：对 timer 与 service 各装一个
`[Unit] ConditionPathExists=<释放哨兵>`，哨兵不存在时 condition 为 false，任何
`start` 都 condition-skip（probe 实测 `start` 返回 0、unit skipped、`ConditionResult=no`、
unit 保持 inactive）。drop-in 位于
`${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user`，可逆（删文件即恢复）、
重启即消失；维护期间**不得重启**，任何重启后必须先重建围栏再继续），再备份现有
配置、安装代码与凭据，AUTHENTICATED 验证 gateway，最后才删除围栏 drop-in 并恢复
timer。代码回滚由标准 ff-only 部署循环负责
（`git pull --ff-only` + 远端同步纪律），本块只负责配置/凭据的机械可回滚。
下面第 3 步 `restart nhms-slurm-gateway.service` 用的是 checked-in 模块入口
（`python -m services.slurm_gateway`），该入口在程序化 `uvicorn.run` 上钉死
`http="h11"`——node-22 维护期活动 Python 3.12.7 环境的可选原生 httptools
2026-08-29 现场复现为损坏（`module 'httptools.parser' has no attribute '__all__'`，
无法 bind），`UVICORN_HTTP=h11` 对程序化调用无效且不改这里任何行为；这是
兼容性钉，不是维护窗口依赖修复，**不授权 `uv sync`**。

```bash
# 0) FENCE the scheduler with REVERSIBLE RUNTIME `ConditionPathExists` drop-ins
#    before anything else. A plain `stop nhms-compute-scheduler.timer` is NOT a
#    stable fence: on 2026-08-28 a concurrent same-user maintenance session
#    explicitly started the timer twice, undoing a plain stop. `mask --runtime`
#    is FORBIDDEN here: a live throwaway probe proved that for a PERSISTENT user
#    unit under ~/.config/systemd/user, `mask --runtime` creates only
#    /run/user/<uid>/systemd/user/<unit> -> /dev/null while `is-enabled` stays
#    `static`, `LoadState=loaded`, and `start` succeeds/active — the persistent
#    unit takes precedence over the runtime mask, so a masked timer still fires.
#    The PROVEN primitive is a runtime condition drop-in: both units get
#    `[Unit]\nConditionPathExists=<release sentinel>`; absent sentinel => every
#    start attempt is skipped with ConditionResult=no and the unit stays
#    inactive. Runtime drop-ins live under
#    ${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user, are REVERSIBLE (delete
#    the file), and vanish on reboot — NO reboot during maintenance; re-establish
#    the fence after any reboot before proceeding. NEVER `stop`/`kill` the
#    scheduler SERVICE while a pass is active: a pass must finish naturally.
#    Fail-fast shell semantics FIRST, before any state change: errexit +
#    pipefail make every load-bearing check (grep/test/systemctl/Python) abort
#    the block instead of silently continuing to fence release or later gates.
set -euo pipefail
RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
FENCE_RELEASE="$RUNTIME_DIR/nhms-issue-1684-scheduler-release"
FENCE_NAME=90-nhms-issue-1684-maintenance-fence.conf
rm -f "$FENCE_RELEASE"            # absent sentinel => condition false => fence on
install -d -m 0700 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d"
install -d -m 0700 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d"
install -m 0600 /dev/null "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"
cat > "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME" <<EOF
[Unit]
ConditionPathExists=$FENCE_RELEASE
EOF
install -m 0600 /dev/null "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
cat > "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME" <<EOF
[Unit]
ConditionPathExists=$FENCE_RELEASE
EOF
chmod 0600 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME" \
           "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
systemctl --user daemon-reload
#    Stop the timer: any later start attempt is now condition-skipped (no
#    rearm race). A concurrent same-user session CANNOT rearm the timer before
#    this step completes, and even a later direct start is a skipped no-op.
systemctl --user stop nhms-compute-scheduler.timer
#    Fail closed if a pass is STILL RUNNING: print a nonsecret instruction and
#    exit BEFORE backup/overwrite. Re-run this step once the service is
#    inactive (the pass finished naturally). No sleep loop — one mechanical
#    check, no waiting/retry. NEVER stop/kill the service.
if systemctl --user is-active --quiet nhms-compute-scheduler.service; then
  echo "rollout: a scheduler pass is still active; let it finish naturally" >&2
  echo "rollout: re-run this rollout step once nhms-compute-scheduler.service is inactive" >&2
  exit 1
fi
#    Mechanically verify the fence is LOADED and LIVE before any
#    backup/overwrite: both units' DropInPaths must include their runtime
#    drop-in, and a probe `systemctl --user start` on EACH unit must be a
#    condition-skipped no-op (unit stays inactive, ConditionResult=no). This
#    safe skipped start proves the fence; no sleep, one-shot checks only.
systemctl --user show nhms-compute-scheduler.timer -p DropInPaths | grep -F "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"
systemctl --user show nhms-compute-scheduler.service -p DropInPaths | grep -F "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
systemctl --user start nhms-compute-scheduler.timer
test "$(systemctl --user is-active nhms-compute-scheduler.timer)" = inactive
test "$(systemctl --user show nhms-compute-scheduler.timer -p ConditionResult --value)" = no
systemctl --user start nhms-compute-scheduler.service
test "$(systemctl --user is-active nhms-compute-scheduler.service)" = inactive
test "$(systemctl --user show nhms-compute-scheduler.service -p ConditionResult --value)" = no
echo "rollout: scheduler fenced (runtime ConditionPathExists drop-ins, skipped starts stay inactive)"

# 1) BACKUP FIRST (before any overwrite): owner-only backup ROOT directory and
#    a SEPARATE pointer file (the pointer must never be the backup directory —
#    a directory cannot be written by `>`). Snapshot the secret and both
#    drop-ins; each gets a .state file saying present/absent so rollback can
#    restore the EXACT prior file, or remove the rollout-created file when it
#    was previously absent.
BACKUP_ROOT="$HOME/.config/systemd/user/gateway-rollout-backups"
BACKUP_POINTER="$HOME/.config/systemd/user/gateway-rollout-backup"
install -d -m 0700 "$BACKUP_ROOT"
BACKUP_DIR="$(mktemp -d -p "$BACKUP_ROOT" snapshot.XXXXXX)"
chmod 0700 "$BACKUP_DIR"
install -m 0600 /dev/null "$BACKUP_POINTER.tmp"
printf '%s\n' "$BACKUP_DIR" > "$BACKUP_POINTER.tmp"
mv "$BACKUP_POINTER.tmp" "$BACKUP_POINTER"
chmod 0600 "$BACKUP_POINTER"
for path in \
  /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env \
  "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf" \
  "$HOME/.config/systemd/user/nhms-slurm-gateway.service.d/10-node22-live.conf"; do
  if [ -e "$path" ]; then
    printf 'present %s\n' "$path" > "$BACKUP_DIR/$(basename "$path").state"
    cp -a --preserve=mode,ownership,timestamps "$path" "$BACKUP_DIR/$(basename "$path").previous"
  else
    printf 'absent %s\n' "$path" > "$BACKUP_DIR/$(basename "$path").state"
  fi
done

# 2) generate the shared owner-only credential (0600 env source). The file is
#    created at mode 0600 BEFORE any token bytes are written (install), then
#    the exact active interpreter's stdout goes DIRECTLY into it; the `>`
#    truncates the already-0600 file without changing the mode (never
#    argv/stdout/log/evidence). Overwrite, not append (the backup in step 1
#    already preserves the prior value if one existed):
install -d -m 0700 /scratch/frd_muziyao/nhms-prod/secrets
install -m 0600 /dev/null /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
/scratch/frd_muziyao/NWM/.venv/bin/python -c 'import secrets; print("SLURM_GATEWAY_SERVICE_TOKEN=" + secrets.token_urlsafe(32))' \
  > /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
chmod 0600 /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
#    do NOT print or inspect the token value; only the path may be named.

#    scheduler unit drop-in (user systemd). The live scheduler base unit loads
#    /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env; the
#    drop-in RESETS the EnvironmentFile list (dropping any stale
#    inherited/generic entries) and explicitly re-adds that live base env FIRST,
#    then the shared secret — so the drop-in override is deterministic and the
#    real base config survives:
mkdir -p "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d"
install -m 0600 /dev/null "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf"
cat > "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf" <<'EOF'
[Service]
EnvironmentFile=
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env
EnvironmentFile=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
EOF
chmod 0600 "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf"

#    gateway unit (tracked template is a generic deployable system unit for
#    /opt/SHUD-NWM + 8081; the LIVE node-22 user-systemd override below RESETS
#    the inherited EnvironmentFile list, explicitly re-adds the live base env
#    /scratch/frd_muziyao/NWM/infra/env/compute.host.env (workspace, object
#    store, partition, runtime) — preserving the REAL base unit config instead
#    of dropping it along with the generic list — then the SAME untracked 0600
#    secret file as the scheduler drop-in, and overrides the live loopback 8090
#    URL):
GATEWAY_DROPIN_DIR="$HOME/.config/systemd/user/nhms-slurm-gateway.service.d"
mkdir -p "$GATEWAY_DROPIN_DIR"
install -m 0600 /dev/null "$GATEWAY_DROPIN_DIR/10-node22-live.conf"
cat > "$GATEWAY_DROPIN_DIR/10-node22-live.conf" <<'EOF'
[Service]
EnvironmentFile=
EnvironmentFile=/scratch/frd_muziyao/NWM/infra/env/compute.host.env
EnvironmentFile=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
Environment=SLURM_GATEWAY_URL=http://127.0.0.1:8090
EOF
chmod 0600 "$GATEWAY_DROPIN_DIR/10-node22-live.conf"

# 3) daemon-reload, restart gateway, verify the effective EnvironmentFiles.
#    `systemctl show -p EnvironmentFiles` prints only the FILE PATHS (never the
#    token value); for BOTH units assert BOTH resolved paths — the live base
#    env from the base unit AND the shared scratch secret — with path-only
#    `grep -F`:
systemctl --user daemon-reload
systemctl --user restart nhms-slurm-gateway.service
systemctl --user show nhms-slurm-gateway.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/NWM/infra/env/compute.host.env'
systemctl --user show nhms-slurm-gateway.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env'
systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env'
systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env'
stat -c '%a %U %n' /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env
ss -ltnp 2>/dev/null | grep ':8090'
curl -fsS --max-time 2 http://127.0.0.1:8090/api/v1/slurm/health

# 4) local auth boundary WITHOUT creating a job. `expect_status` is a generic
#    secret-safe helper: capture the HTTP status and require the EXACT expected
#    code, else report on stderr and return nonzero (fail-fast). The valid-token
#    probe is `token_probe` (value never on argv/stdout): source the owner-checked
#    0600 file in a private shell, use a 0600 temp curl header file, and require
#    exactly 422 (auth passed -> existing validation error, no submission).
expect_status() { # <expected-http> <label> <curl-args...>
  local expected=$1 label=$2 status
  shift 2
  status="$(curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true)"
  if [ "$status" != "$expected" ]; then
    echo "auth boundary: $label expected HTTP $expected, got ${status:-no-response}" >&2
    return 1
  fi
  echo "auth boundary: $label -> HTTP $expected (no submission performed)"
}
token_probe() {
  local secret_file=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env status
  test -r "$secret_file" || { echo "token probe: secret unreadable" >&2; return 1; }
  test "$(stat -c %a "$secret_file")" = 600 || { echo "token probe: secret mode != 0600" >&2; return 1; }
  local hdr; hdr="$(mktemp)"; chmod 600 "$hdr"
  trap 'rm -f "$hdr"' RETURN
  (
    set -a
    . "$secret_file" || { echo "token probe: secret source failed" >&2; return 1; }
    set +a
    printf 'Authorization: Bearer %s\n' "$SLURM_GATEWAY_SERVICE_TOKEN" > "$hdr"
  ) || return 1
  status="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
    -H 'Content-Type: application/json' -H @"$hdr" -d '{}' 2>/dev/null || true)"
  if [ "$status" != "422" ]; then
    echo "token probe: expected 422 authenticated validation, got ${status:-no-response}" >&2
    return 1
  fi
  echo "token probe: authenticated (422 validation, no submission performed)"
}
#    no token -> 401 before body validation (fail-fast gate)
expect_status 401 "no token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
  -H 'Content-Type: application/json' -d '{}' || exit 1
#    wrong token -> 401 (fail-fast gate)
expect_status 401 "wrong token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
  -H 'Authorization: Bearer wrong-token-value-000000' -H 'Content-Type: application/json' -d '{}' || exit 1
#    valid token -> auth passes into the existing validation error (422), no sbatch
token_probe || exit 1
#    disabled reset -> 404 for every credential
expect_status 404 "disabled reset" -X POST http://127.0.0.1:8090/api/v1/slurm/internal/reset || exit 1

# 5) live-safety receipt: loopback bind + remote refusal + bind-guard rejection
#    - ss -ltnp shows only 127.0.0.1:8090
#    - from another host: probe node-22:8090 -> connection refused/timed out
#    - a misbound start is rejected by the process itself before uvicorn:
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway --url http://0.0.0.0:8090
#      -> nonzero exit, stderr: non-loopback bind host is not allowed

# 6) AUTHENTICATED pre-validation BEFORE timer resume: anonymous health being
#    green is NOT enough — prove the gateway actually accepts the configured
#    service credential (token_probe above must pass 422), then run the
#    scheduler's own gateway preflight (read-only, NO submission) with the same
#    secret and the live settings, and only then resume the timer.
systemctl --user is-active nhms-slurm-gateway.service
token_probe || exit 1
# effective EnvironmentFiles path-only check (base env + shared secret, no values)
systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env'
systemctl --user show nhms-compute-scheduler.service -p EnvironmentFiles | grep -F '/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env'
/scratch/frd_muziyao/NWM/.venv/bin/python - <<'PY'
import os
import sys

SECRET = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
if (os.stat(SECRET).st_mode & 0o777) != 0o600:
    print("scheduler preflight: secret mode is not 0600", file=sys.stderr)
    sys.exit(1)
found = False
with open(SECRET, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if line.startswith("SLURM_GATEWAY_SERVICE_TOKEN="):
            os.environ["SLURM_GATEWAY_SERVICE_TOKEN"] = line.split("=", 1)[1]
            found = True
            break
if not found:
    print("scheduler preflight: secret must define SLURM_GATEWAY_SERVICE_TOKEN", file=sys.stderr)
    sys.exit(1)
# Required non-secret config only: the live gateway backend and loopback URL.
os.environ["SLURM_GATEWAY_BACKEND"] = "slurm"
os.environ["SLURM_GATEWAY_URL"] = "http://127.0.0.1:8090"
from services.orchestrator.scheduler import _default_gateway_probe

class _Config:
    slurm_gateway_url = "http://127.0.0.1:8090"

result = dict(_default_gateway_probe(_Config()))
ready = (
    bool(result.get("healthy"))
    and bool(result.get("submit_capable"))
    and bool(result.get("accounting_available"))
)
if not ready:
    print(
        "scheduler preflight: gateway is not submit-ready: %s"
        % (result.get("reason") or "unknown",),
        file=sys.stderr,
    )
    sys.exit(1)
print("scheduler preflight: healthy submit_capable accounting_available (read-only, no submission)")
PY

# 7) RELEASE the fence ONLY after the 401/401/404/422 boundaries and the
#    read-only preflight above ALL passed. The release IS the removal of the
#    runtime condition drop-ins — NEVER create the release sentinel as a
#    bypass: an existing sentinel would make `ConditionPathExists` true and
#    silently start a pass, skipping every gate. Remove BOTH drop-in files
#    (and the now-empty runtime dirs if possible), daemon-reload, verify the
#    fence is GONE (DropInPaths no longer list the drop-in), then start the
#    timer and require active. Ordering is exact — removal/reload/verify
#    BEFORE start, never start through the fence.
rm -f "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"
rm -f "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
rmdir "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d" 2>/dev/null || true
rmdir "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user show nhms-compute-scheduler.timer -p DropInPaths | grep -Fv "$FENCE_NAME"
systemctl --user show nhms-compute-scheduler.service -p DropInPaths | grep -Fv "$FENCE_NAME"
systemctl --user start nhms-compute-scheduler.timer
systemctl --user is-active nhms-compute-scheduler.timer
```

2026-06-22 现场验证（历史）：

- `python -m services.slurm_gateway` 在 node-22 运行。
- node-22 diagnostic API `/health` 在 `:8001` 返回 `{"status":"ok",...}`。
- node-22 `/ghdc/data/nwm/object-store` 与 `/ghdc/data/nwm/published`
  可见，是 node-27 `/home/ghdc/nwm/...` 的同一份 NFS 数据面。

Rollback（先重assert 同一 runtime `ConditionPathExists` 围栏，再从 backup pointer
精确恢复；**本块绝不删除围栏 drop-in、绝不创建释放哨兵、绝不 daemon-reload 释放、也绝不
恢复 timer**——恢复后的配置/代码必须独立通过完整 rollout 认证门后由操作者手动删除两个
runtime drop-in 再手动启 timer；全程无匿名兼容旁路）：

```bash
# 0) RE-ASSERT THE FENCE. A concurrent same-user session may have removed a
#    drop-in or started a unit since rollout began; re-establish BOTH runtime
#    condition drop-ins (exactly like rollout step 0: same RUNTIME_DIR /
#    FENCE_RELEASE / FENCE_NAME, no release sentinel), daemon-reload, stop the
#    timer, and make sure both units are fenced BEFORE restoring any file.
#    `mask --runtime` is still FORBIDDEN (persistent user unit precedence —
#    proven ineffective on this topology). If a pass is still running, do NOT
#    stop/kill the service — fail closed and let it finish naturally; re-run
#    rollback once inactive. Fail-fast shell semantics FIRST, before any state
#    change: errexit + pipefail make every load-bearing check abort the block
#    instead of silently continuing to restore or start the timer.
set -euo pipefail
RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
FENCE_RELEASE="$RUNTIME_DIR/nhms-issue-1684-scheduler-release"
FENCE_NAME=90-nhms-issue-1684-maintenance-fence.conf
rm -f "$FENCE_RELEASE"
install -d -m 0700 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d"
install -d -m 0700 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d"
install -m 0600 /dev/null "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"
cat > "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME" <<EOF
[Unit]
ConditionPathExists=$FENCE_RELEASE
EOF
install -m 0600 /dev/null "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
cat > "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME" <<EOF
[Unit]
ConditionPathExists=$FENCE_RELEASE
EOF
chmod 0600 "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME" \
           "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
systemctl --user daemon-reload
systemctl --user stop nhms-compute-scheduler.timer
if systemctl --user is-active --quiet nhms-compute-scheduler.service; then
  echo "rollback: a scheduler pass is still active; let it finish naturally" >&2
  echo "rollback: re-run this rollback step once nhms-compute-scheduler.service is inactive" >&2
  exit 1
fi
systemctl --user show nhms-compute-scheduler.timer -p DropInPaths | grep -F "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME"
systemctl --user show nhms-compute-scheduler.service -p DropInPaths | grep -F "$RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME"
systemctl --user start nhms-compute-scheduler.timer
test "$(systemctl --user is-active nhms-compute-scheduler.timer)" = inactive
test "$(systemctl --user show nhms-compute-scheduler.timer -p ConditionResult --value)" = no
systemctl --user start nhms-compute-scheduler.service
test "$(systemctl --user is-active nhms-compute-scheduler.service)" = inactive
test "$(systemctl --user show nhms-compute-scheduler.service -p ConditionResult --value)" = no
echo "rollback: scheduler fenced (runtime ConditionPathExists drop-ins, skipped starts stay inactive)"

# 1) read the backup pointer and restore the EXACT prior state. State value is
#    matched by `case`: `present` requires the `.previous` file and restores
#    byte-for-byte with its prior mode; `absent` removes the rollout-created
#    file; anything else (corrupted/unknown state) FAILS closed rather than
#    treating it as absent.
BACKUP_POINTER="$HOME/.config/systemd/user/gateway-rollout-backup"
if [ ! -r "$BACKUP_POINTER" ]; then
  echo "rollback: no gateway-rollout-backup pointer; nothing to restore" >&2
  exit 1
fi
BACKUP_DIR="$(cat "$BACKUP_POINTER")"
restore_snapshot() { # <live-path> <snapshot-basename>
  local live_path=$1 base=$2 state="$BACKUP_DIR/$2.state" marker
  if [ ! -f "$state" ]; then
    echo "rollback: missing snapshot state for $base" >&2
    return 1
  fi
  marker="$(awk 'NR==1 {print $1}' "$state")"
  case "$marker" in
    present)
      if [ ! -f "$BACKUP_DIR/$base.previous" ]; then
        echo "rollback: $base state is present but .previous snapshot is missing" >&2
        return 1
      fi
      mkdir -p "$(dirname "$live_path")"
      cp -a --preserve=mode,ownership,timestamps "$BACKUP_DIR/$base.previous" "$live_path"
      ;;
    absent)
      rm -f "$live_path"
      ;;
    *)
      echo "rollback: $base snapshot state is corrupt/unknown: ${marker:-<empty>}" >&2
      return 1
      ;;
  esac
}
restore_snapshot /scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env slurm-gateway.env || exit 1
restore_snapshot "$HOME/.config/systemd/user/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf" 10-slurm-gateway-token.conf || exit 1
restore_snapshot "$HOME/.config/systemd/user/nhms-slurm-gateway.service.d/10-node22-live.conf" 10-node22-live.conf || exit 1

# 2) reload, restart gateway, verify restore (health reachable, mutations 401)
systemctl --user daemon-reload
systemctl --user restart nhms-slurm-gateway.service
systemctl --user show nhms-slurm-gateway.service -p EnvironmentFiles
curl -fsS --max-time 2 http://127.0.0.1:8090/api/v1/slurm/health

# 3) FAIL-CLOSED readiness gate: if a first-deploy snapshot had no token/drop-ins
#    while the NEW auth code remains active, anonymous health alone is NOT
#    enough. re-run the SAME gates as rollout: no-token/wrong-token/reset exact
#    statuses, authenticated 422 via token_probe, and the executable scheduler
#    preflight. Any failure leaves the timer STOPPED and REQUIRES code rollback
#    or fix-forward — never resume on anonymous health or a bare 401.
expect_status() { # <expected-http> <label> <curl-args...>
  local expected=$1 label=$2 status
  shift 2
  status="$(curl -s -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true)"
  if [ "$status" != "$expected" ]; then
    echo "auth boundary: $label expected HTTP $expected, got ${status:-no-response}" >&2
    return 1
  fi
  echo "auth boundary: $label -> HTTP $expected (no submission performed)"
}
token_probe() {
  local secret_file=/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env status
  test -r "$secret_file" || { echo "token probe: secret unreadable" >&2; return 1; }
  test "$(stat -c %a "$secret_file")" = 600 || { echo "token probe: secret mode != 0600" >&2; return 1; }
  local hdr; hdr="$(mktemp)"; chmod 600 "$hdr"
  trap 'rm -f "$hdr"' RETURN
  (
    set -a
    . "$secret_file" || { echo "token probe: secret source failed" >&2; return 1; }
    set +a
    printf 'Authorization: Bearer %s\n' "$SLURM_GATEWAY_SERVICE_TOKEN" > "$hdr"
  ) || return 1
  status="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
    -H 'Content-Type: application/json' -H @"$hdr" -d '{}' 2>/dev/null || true)"
  if [ "$status" != "422" ]; then
    echo "token probe: expected 422 authenticated validation, got ${status:-no-response}" >&2
    return 1
  fi
  echo "token probe: authenticated (422 validation, no submission performed)"
}
expect_status 401 "rollback no token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
  -H 'Content-Type: application/json' -d '{}' || exit 1
expect_status 401 "rollback wrong token" -X POST http://127.0.0.1:8090/api/v1/slurm/jobs \
  -H 'Authorization: Bearer wrong-token-value-000000' -H 'Content-Type: application/json' -d '{}' || exit 1
expect_status 404 "rollback disabled reset" -X POST http://127.0.0.1:8090/api/v1/slurm/internal/reset || exit 1
token_probe || exit 1
/scratch/frd_muziyao/NWM/.venv/bin/python - <<'PY'
import os
import sys

SECRET = "/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env"
if (os.stat(SECRET).st_mode & 0o777) != 0o600:
    print("scheduler preflight: secret mode is not 0600", file=sys.stderr)
    sys.exit(1)
found = False
with open(SECRET, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if line.startswith("SLURM_GATEWAY_SERVICE_TOKEN="):
            os.environ["SLURM_GATEWAY_SERVICE_TOKEN"] = line.split("=", 1)[1]
            found = True
            break
if not found:
    print("scheduler preflight: secret must define SLURM_GATEWAY_SERVICE_TOKEN", file=sys.stderr)
    sys.exit(1)
os.environ["SLURM_GATEWAY_BACKEND"] = "slurm"
os.environ["SLURM_GATEWAY_URL"] = "http://127.0.0.1:8090"
from services.orchestrator.scheduler import _default_gateway_probe

class _Config:
    slurm_gateway_url = "http://127.0.0.1:8090"

result = dict(_default_gateway_probe(_Config()))
ready = (
    bool(result.get("healthy"))
    and bool(result.get("submit_capable"))
    and bool(result.get("accounting_available"))
)
if not ready:
    print(
        "scheduler preflight: gateway is not submit-ready: %s"
        % (result.get("reason") or "unknown",),
        file=sys.stderr,
    )
    sys.exit(1)
print("scheduler preflight: healthy submit_capable accounting_available (read-only, no submission)")
PY

# 4) FAIL-CLOSED: this block NEVER removes the fence drop-ins, NEVER creates
#    the release sentinel, NEVER daemon-reloads a release, and NEVER starts the
#    timer (no executable rm of the fence / sentinel creation / timer start may
#    appear anywhere in the fenced rollback block). If every gate above passed,
#    the restored configuration is auth-ready; MANUAL recovery, exactly in this
#    order:
#      1. run the FULL authenticated gates (401/401/404/422 + read-only
#         preflight) against the restored config/code;
#      2. rm -f  $RUNTIME_DIR/systemd/user/nhms-compute-scheduler.timer.d/$FENCE_NAME \
#               $RUNTIME_DIR/systemd/user/nhms-compute-scheduler.service.d/$FENCE_NAME
#         (remove BOTH runtime drop-ins; do NOT create the release sentinel)
#      3. systemctl --user daemon-reload
#      4. verify DropInPaths no longer list the fence / ConditionResult no
#         longer blocks, then systemctl --user start nhms-compute-scheduler.timer
#    The runtime condition fence above stays in place until that manual
#    recovery — no executable fence removal/start in this block. If any gate
#    failed (e.g. first-deploy rollback with no shared token while new auth
#    code is active), the scheduler REMAINS FENCED: code rollback or
#    fix-forward is required before the scheduler may resume.
```

### 3.3 API / 展示服务

node-27 display API 通过仓库 wrapper 管理：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
bash scripts/ops/start-display-api.sh
```

wrapper 会：

- source `infra/env/display.env`；
- 校验 `DATABASE_URL`、`NHMS_ENABLE_LIVE_POSTGIS_MVT`、`OBJECT_STORE_ROOT`；
- 创建并校验 `NHMS_MVT_FILE_CACHE_DIR`，未设置时默认 `$HOME/.cache/nhms/mvt`；
- 安装仓库内 `infra/systemd/nhms-display-api.service`，停掉旧的手工 uvicorn；
- 由 user systemd 在 `127.0.0.1:${NHMS_DISPLAY_API_PORT:-8080}` 启动
  `${NHMS_DISPLAY_WORKERS:-2}` 个 worker，失败自动恢复；
- 跑 `/health` 与 `/api/v1/models?limit=1` basin_id smoke check。

确认当前 live 状态：

```bash
cd /home/nwm/NWM
systemctl --user is-enabled nhms-display-api.service
systemctl --user is-active nhms-display-api.service
grep -E '^NHMS_DISPLAY_API_PORT=|^NHMS_SERVICE_ROLE=|^OBJECT_STORE_ROOT=' \
  infra/env/display.env

if grep -q '^DATABASE_URL=' infra/env/display.env; then
  printf 'DATABASE_URL=<set redacted>\n'
else
  printf 'DATABASE_URL=<missing>\n'
fi

pgrep -af 'uvicorn apps[.]api[.]main'
ss -ltnp 2>/dev/null | grep -E ':(55432|8080)\b'
curl -fsS --max-time 5 http://127.0.0.1:8080/health
curl -fksS --max-time 5 https://test.nwm.ac.cn/health
```

2026-06-22 现场修正过一次 display port drift：`display.env` 曾设置
`NHMS_DISPLAY_API_PORT=8000`，而 nginx 与仓库模板期望 `8080`。已备份原文件并
改回 `8080`，随后 `scripts/ops/start-display-api.sh` smoke check 和 public
`https://test.nwm.ac.cn/health` 均返回 `ok`。后续若公网 502，先同时检查本地
`127.0.0.1:8080/health`、nginx `proxy_pass` 和 `NHMS_DISPLAY_API_PORT`。

### 3.4 监控快照

node-27 ingest 侧优先看 autopipe 日志和 DB/run coverage：

```bash
ssh -p 32099 nwm@210.77.77.27
tail -n 200 /home/nwm/autopipe-logs/autopipe.log

cd /home/nwm/NWM
set -a
. infra/env/node27-ingest.env
set +a
psql "$DATABASE_URL" -P pager=off -F $'\t' -Atc "
select run_id, source_id, cycle_time, model_id, status,
       coalesce(error_code,''), updated_at
from hydro.hydro_run
order by updated_at desc nulls last
limit 30;"
```

If the host-provisioned `infra/env/node27-ingest.env` is absent, treat ingest
writer checks as blocked and fix the ingest env. Do not fall back to
`infra/env/display.env`; that file is display_readonly runtime config only.

node-22 compute 侧优先看 Slurm queue、Gateway、shared NFS 输出：

```bash
ssh -p 32099 frd_muziyao@210.77.77.22
squeue -u "$USER" -o "%.18i %.20j %.2t %.10M %.10l %.6D %R"
pgrep -af '[s]ervices.slurm_gateway'
systemctl --user list-timers 'nhms-compute-scheduler.timer' --no-pager
find /ghdc/data/nwm/object-store/runs -maxdepth 1 -type d \
  -printf '%TY-%Tm-%Td %TH:%TM %p\n' | sort | tail -20
```
