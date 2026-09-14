# Forcing backfill per-package lock hold — node-22 measurement, 2026-09-14 (#2236, EF-9)

Where: node-22 `xnode`, reading the NFS copyback root
`/ghdc/data/nwm/object-store/forcing` exactly as the backfill does. Read-only.
Interpreter: `/scratch/frd_muziyao/NWM/.venv/bin/python` (no `uv sync`, no bare
`uv run`). The probe script lived in `/tmp` for the run and was deleted.

What was timed: the dominant cost of the new hold — a full read plus SHA-256 of
every file in a package tree. `_inspect_existing_target` does this for the
destination tree; `_validate_source_package` does it again for the source tree of
a copyable package, so a copyable package's hold is bounded by roughly twice the
figure below plus the copy itself (unchanged by this change: the copy was already
inside the lock).

| Measure | Value |
|---|---|
| Package trees under `forcing/` | 9332 (127 GiB) |
| Sample | 40 trees, seeded random |
| p50 read+hash | 0.052 s |
| max read+hash (sample) | 0.205 s (71.6 MB tree) |
| throughput | 218.1 MB/s |
| largest package on disk | 94 MiB → ≈0.43 s at the measured rate |

Bound against the 900 s guard deadline: the worst single already-present hold is
≈0.43 s and the worst copyable hold's validation share ≈0.9 s, three orders of
magnitude under the deadline a waiting writer carries. A full `--apply` over all
9332 packages takes 9332 acquisitions (previously only the copyable ones), with
≈9332 × 0.052 s ≈ 8 min of summed hold at the median, released between packages.
The non-fair poll caveat in design.md D2 stands: the summed figure is what a
writer competes against, not a single wait.
