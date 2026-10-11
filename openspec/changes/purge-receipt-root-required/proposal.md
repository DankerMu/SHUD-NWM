# purge-receipt-root-required

## Why

`scripts/node27_purge_superseded_weights.py` 的 `--receipt-root` 默认是 `<OBJECT_STORE_ROOT>/scheduler/weight-purge`，而 node-27 上 `scheduler/` 归 node-22 用户所有、`nwm` 无写权限（#2773 第 3 项；2026-10-11 复核：`drwxr-xr-x frd_muziyao nfsdata`，该子目录不存在）。首次生产 apply 就是在任何写入前被这个默认值挡下的；runbook 已改为显式传 `--receipt-root /home/ghdc/nwm/archive/weight-purge`，代码默认值仍指向一个写不了的地方。

同单提到的 `scripts/node27_retire_basin.py` 已核实无此问题：它的默认 `<OBJECT_STORE_ROOT>/scheduler/succession` 在 node-27 上是 `drwxrwsr-x frd_muziyao nwmuser`，`nwm` 可写（且它要在同一处读 node-22 的回执），不改。

## What Changes

- `--receipt-root` 改为必填，删除默认值 `DEFAULT_RECEIPT_ROOT_KEY`；不传时 argparse 直接拒绝，任何 DB 连接或写入之前。
- `docs/runbooks/production-ops/operating-scope.md` 7.7 的参数说明同步（不再写“默认 …”）。

## Triage

```text
Issue type: ops tool hardening
Fixture level: none
Upstream suggested level: 未给
Blast radius: 一个 node-27 运维 CLI 的参数；无库表 / 无服务代码
Selected risk packs: 无
Evidence floor: 不传 --receipt-root 即 exit 2 且无任何写入的单测；现有用例通过；ruff
```

## Impact

- 受影响文件：`scripts/node27_purge_superseded_weights.py`、其单测、`operating-scope.md`。
- 受影响规格：`superseded-weight-purge`。
