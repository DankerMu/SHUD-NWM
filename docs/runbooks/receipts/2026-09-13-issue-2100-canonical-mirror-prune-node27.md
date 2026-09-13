# Receipt: #2100 canonical 降水镜像跨账号删除权限 — 存量 sweep + node-27 实机 retention tick

- Issue: #2100 · change `fix-canonical-mirror-prune-permissions` · 前置 #2104 (PR #2307, merge `5b144dd1`) 已合入
- 路线：共享组 `nwmuser`(gid 1107) + setgid `2775`（design D1–D7）；`canonical/` 本身不动（新 source 失败关闭）
- 记录人：orchestrator，全部由实机输出转录；绝对数字以本 receipt 时刻为准，不回写到指令文件

## 1. node-22 owner-side sweep（存量目录）

账号/主机：`frd_muziyao@node-22`（uid 1103，groups 含 1107）。目标：`/ghdc/data/nwm/object-store/canonical/{gfs,IFS}`（node-27 侧同一 NFS：`/home/ghdc/nwm/object-store/canonical`）。

Before（2026-09-13T15:00:15Z）：

```
755 frd_muziyao huser canonical
755 frd_muziyao huser canonical/gfs
755 frd_muziyao huser canonical/IFS
755 frd_muziyao huser canonical/gfs/2026082312
755 frd_muziyao huser canonical/gfs/2026082312/prcp_rate_or_amount
gfs dirs=85 not2775=85 notgid1107=2382 files=2297
IFS dirs=85 not2775=85 notgid1107=2259 files=2174
```

命令（pre-order：`find` 默认父先子后，任一时刻不会出现 `prcp_rate_or_amount/` 可写而 `<cycle>/` 不可写）：

```bash
C=/ghdc/data/nwm/object-store/canonical
for s in gfs IFS; do chgrp -R 1107 "$C/$s" && find "$C/$s" -type d -exec chmod 2775 {} + ; done
```

After（2026-09-13T15:00:55Z）：

```
gfs dirs=85 not2775=0 notgid1107=0 files_not644=0
IFS dirs=85 not2775=0 notgid1107=0 files_not644=0
755  frd_muziyao huser   canonical            # 未动
2775 frd_muziyao nwmuser canonical/gfs
2775 frd_muziyao nwmuser canonical/IFS
2775 frd_muziyao nwmuser canonical/gfs/2026082312
2775 frd_muziyao nwmuser canonical/gfs/2026082312/prcp_rate_or_amount
2775 frd_muziyao nwmuser canonical/IFS/grid
644  frd_muziyao nwmuser canonical/gfs/2026082312/prcp_rate_or_amount/gfs_2026082312_prcp_rate_or_amount_f003.nc
```

`getent group 1107`（node-22）：`nwm,zhaochen,st_liwz,st_liyunhan,st_zhanghx,leleshu,frd_muziyao,wangjj`。

## 2. node-27 实机 tick（已安装 unit，活动树 `/home/nwm/NWM` 仍在 `a8db554d`，未 pull）

运行账号：`nwm` uid 1005，groups `1005(nwm),27(sudo),999(docker),1107(nwmuser)`。

Before（2026-09-13T15:01:37Z）：

```
du -sb canonical/gfs = 2779917192   canonical/IFS = 2624648108   cycles: gfs 41, IFS 41
df -B1 /home used=499681173504 avail=1189986144256
unit: ActiveState=failed SubState=failed Result=exit-code ExecMainStatus=1   (prev summary raw-retention-20260913T033532Z.json: failed=22)
```

触发：`systemctl --user start nhms-node27-raw-retention.service`（15:01:38Z，elapsed_sec=0）。

Summary `raw-retention-20260913T150138Z.json`：

```
schema_version=nhms.node27_raw_retention.production.v4 execution_mode=production_execute status=completed
reference_time=2026-09-12T12:00:00Z cutoff=2026-08-29T12:00:00Z
counts: deleted=26 failed=0 planned=26 skipped=123   freed_bytes=1683231101
deleted_by_lane: canonical=24 raw=2   canonical reasons: [canonical_cycle_aged_out]
failed: []   unsafe_skips: []
```

删除的 canonical key：gfs/IFS 各 12 个，`2026082312 … 2026082900`——包含上一 tick 报 `PermissionError` 的全部 22 个（`…082312 … …082812` × 2），另加 cutoff 前移（reference 09-12T00Z → 09-12T12Z）新老化的 `2026082900` × 2。raw 车道同 tick 删 `raw/IFS/2026082900`、`raw/gfs/2026082900`。

Unit：

```
ActiveState=inactive SubState=dead Result=success ExecMainStatus=0
ExecMainStartTimestamp=Sun 2026-09-13 23:01:38 CST  ExecMainExitTimestamp=Sun 2026-09-13 23:01:38 CST
systemctl --user list-units --state=failed | grep -c raw-retention  -> 0
```

After：

```
du -sb canonical/gfs = 1966287279   canonical/IFS = 1856463103   cycles: gfs 29, IFS 29
df -B1 /home used=497995923456 avail=1191671394304
```

文档化判据（程序从 `origin/master` @ `5b144dd1` 的 example 提取，含 #2104 加宽后的子句 4）：`jq -e` 对新 summary **rc=0**；把子句 3 换成 #2100 的 `([.failed[]] | length == 0)` 后同样 **rc=0**。

## 3. 覆盖与未覆盖

- 覆盖：design D2（存量目录）与 retention 路径（4.5、4.6）。
- 未覆盖：D3/D4（生产者对新镜像周期的持久化）——只能在合入后 node-22 `git pull --ff-only` 并镜像出第一个新周期时观察；届时 `stat -c '%a %U %G'` `<cycle>/` 与 `prcp_rate_or_amount/` 应为 `2775 … nwmuser`，作为 post-merge 回执补记于此（§4）。
- Sweep 与 node-22 部署之间镜像出的周期会落成 `<cycle>/` 0755 gid 1107 / `prcp_rate_or_amount/` 0755 gid 1078（干净拒绝，零字节），由 runbook §5.3 的 re-sweep 处理。

## 4. Post-merge persistence（待补）

（合入并 node-22 部署后由 follow-up 填写：首个新镜像周期两级 `stat`、re-sweep 结果。）
