# w3 scheduler and gateway units snapshot (read-only, 2026-11-28)

**DRAFT (#3427): the two rows below hold the sha256 of the tracked files. The host step of #3427 has not run yet; the
values read on the host replace this paragraph and the `mtime` column in the same pull request.**

Read-only `sha256sum` of two deployed user-scope units on `w3`, the compute control host, for issue #3427 (slice 9.2 of
the narration sweep #3345). Purpose: byte-exact source for `infra/systemd/node22/nhms-compute-scheduler.service` and
`infra/systemd/node22/nhms-slurm-gateway.service` after their `Description=` line was changed and the two files were
redeployed.

The 2026-10-16 snapshot (`2026-10-16-node22-live-units-snapshot.md`) records the same two paths with the bytes deployed
until 2026-11-28. Receipts are history and keep their rows: where two receipts record a path, the later receipt holds the
bytes the repository tracks. Only the `Description=` line differs between the two versions of each file; neither file
holds a secret.

| relative path under `~/.config/systemd/user/` | sha256 | mtime |
|---|---|---|
| `nhms-compute-scheduler.service` | `5fbd228b11eb44160dcd66115035a25243047d6fc68fbad1c08ff7941a6445a5` | not read yet |
| `nhms-slurm-gateway.service` | `0aaa07e91044ce07f573c097588c5cf515ae643d5a7de85bdcd54b3bee5df862` | not read yet |

The other files of `infra/systemd/node22/` were not redeployed and stay recorded by the 2026-10-16 snapshot and by
`2026-11-26-w3-gateway-runtime-path-dropin-snapshot.md`. The redeploy itself, with the fifteen top-level units that
changed in the same step, is recorded in `2026-11-28-issue-3427-unit-descriptions-redeployed.md`.
