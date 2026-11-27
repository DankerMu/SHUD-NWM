# Receipt: two host facts re-measured on w3, 2026-11-27

> Provenance: read-only commands run in an interactive shell on `w3` (the compute control host) for #3412,
> which rewrote two runbook pages whose rules rested on observations made on the retired node-22 host.
> Nothing was changed on the host.

Collected 2026-11-27T16:31:46Z on `w3`.

## 1. `pgrep -f` also matches the shell that runs the check

The rule that depends on it: the anchored `pgrep` form for `node22-run-cycle-once.sh` in
[`../production-ops/known-issues-pipeline.md`](../production-ops/known-issues-pipeline.md) §8.5
(first recorded for #3113 on 2026-11-10 on the retired node-22 host).

```text
$ pgrep --version
pgrep from procps-ng 4.0.4
$ bash -c "pgrep -f zz_probe_3412_marker >/dev/null; echo pgrep_self_shell_rc=\$?"
pgrep_self_shell_rc=0
```

No process of that name exists; the only command line that carries the marker is the `bash -c` shell
that runs the check. Exit status 0 means `pgrep` reported it: `pgrep` excludes only itself, so a bare
`pgrep -f <name>` run through `bash -c` or `ssh host '...'` always reports a hit.

## 2. Slurm does not store the job comment

The rule that depends on it: the `comment_accounting_unproven` outcome in
[`../production-ops/stuck-detection.md`](../production-ops/stuck-detection.md) §6.2.4 and
[`../failed-basin-retry.md`](../failed-basin-retry.md) (#1116).

```text
$ scontrol show config | grep -i -E "AccountingStoreFlags|^SLURM_VERSION"
AccountingStoreFlags    = (null)
SLURM_VERSION           = 23.11.4
```

`(null)`: the line is present and names no flag, so `job_comment` is not stored (the probe in
`services/orchestrator/reconcile_sacct_probe.py` reads a missing line as unknown and `(null)` as "does not store comments").
