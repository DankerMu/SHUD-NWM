# w3 gateway runtime-path drop-in snapshot (read-only, 2026-11-26)

Read-only `cat` and `sha256sum` of one deployed user-scope drop-in on `w3`, the compute role's control host, taken on
2026-11-26T13:36Z for issue #3359 (item 4). Purpose: byte-exact source for
`infra/systemd/node22/nhms-slurm-gateway.service.d/20-runtime-path.conf`.

The file was deployed before the 2026-10-16 snapshot was taken, and that snapshot did not record it. The drift probe has
reported it as `untracked` on a healthy host ever since (first noted on 2026-11-12, see
`2026-11-12-issue-3127-node22-unit-failure-alert-deploy.md`). It holds no secret.

| relative path under `~/.config/systemd/user/` | sha256 | mtime |
|---|---|---|
| `nhms-slurm-gateway.service.d/20-runtime-path.conf` | `ae59e26c2440c3a9e886c1f2914d6f4907c67f881feb5ca49d7d1555b1d66a97` | 2026-08-30 10:57 UTC |

Size 78 bytes, mode `0600`. The content is byte-identical to the scheduler's drop-in of the same name, which the
2026-10-16 snapshot records with the same sha256:

```ini
[Service]
Environment=NHMS_PYTHON_VENV_BIN=/scratch/frd_muziyao/NWM/.venv/bin
```

The other file in that directory is `10-node22-live.conf`; the drift probe of 2026-11-24 reported it as `same`.
