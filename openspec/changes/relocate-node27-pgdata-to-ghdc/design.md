## Context

Issue #2240 is a tooling PR, not a production deployment. The earlier reslice was abandoned before source deletion. The deployed application remains pinned at `5a86841c`; this change must not replace it. Existing cold-tier work excludes whole PGDATA and is not authority for this relocation.

## Goals / Non-Goals

Provide a complete, manually advanced offline physical-copy and exact-bind-switch path, with durable interruption state and safe pre-write restoration. Preserve the complete cluster and all existing business behavior. Do not automate production approval, schema/app/image upgrades, chunk rewrites, external tablespace migration, cold-lane activation, or old-copy deletion.

## Decisions

1. **Offline physical copy, not logical restore.** Cleanly stop PostgreSQL after draining writers. Copy all PGDATA, including WAL/indexes/catalogs, preserving numeric ownership/modes/hardlinks; flush and stream-verify the complete tree before activation. The observed exact image has `pg_controldata` but no rsync. Use available native image tools rather than adding a dependency or changing the image. Online precopy is not required; longer downtime is the explicit simplicity trade-off.
2. **One serialized operation, explicit actions.** `scripts/node27_pgdata_migrate.py --action plan|prepare|copy|activate|rollback|release` defaults to plan. Mutations require `--enforce`, private operation authority and the canonical lifecycle exclusion. Journal intent before side effects. Keep source/container/path identity across processes and re-observe actual state at every transition; neither a success receipt nor a dead PID is sufficient authority.
3. **Prepare freezes the real source.** Record exact Docker snapshot, restart policy, service/timer states and unrelated dropins. Install persistent operation-owned fences; stop timers and drain existing work, then collect data/read baselines, stop display and cleanly stop PostgreSQL. Preserve the original container and disable its automatic restart before a replacement can exist. Reboot/interruption must not silently enable either old writers or two primaries. Unknown writers or changed configuration refuse the next transition.
4. **Copy has a narrow admission boundary.** Fresh root-owned descriptor-bound mdadm and both-member SMART PASS, filesystem capacity plus explicit reserve, a new owned target, and observed numeric runtime ownership are mandatory. Reject external `pg_tblspc`/WAL/config dependencies not covered by the complete source, symlink/overlap/unrelated paths, running source, unclean control state and topology drift. Root helpers have only the required source read-only and operation-owned destination binds, no broad host root or privileged Docker socket. Prove the bound paths match the frozen identities before writing. Preserve partial output on failure; no automatic destructive retry.
5. **Activation changes one semantic setting.** Reuse `ContainerSnapshot` and factor the existing pure Docker argv serializer if needed; the cold installer retains its identity/add-cold-bind wrapper unchanged. Replace the source PGDATA bind, not the whole mount/config set. A tag-to-resolved-digest image reference normalization is acceptable only with identical resolved image bytes. Keep environment material private and out of command/error receipts. Keep the exact original container stopped, and all business writers fenced, while the copied database and read-only display are checked.
6. **Write release is irreversible with respect to the old snapshot.** Pre-release rollback restores the original container/config and original service/timer state; neither directory is deleted. Before controlled ingest or any other business write, release durably marks the original snapshot stale, then restores writer scheduling. After that marker, old-copy switchback is refused even if process interruption prevents confirming whether a write occurred. Post-release recovery uses a fresh consistent backup/copy of the current database under a new controlled window, not the original snapshot.
7. **Placement observation follows configuration.** Keep the current deployment default `/home/nwm/nhms-pgdata`; the rollout changes `NODE27_GOVERNANCE_PGDATA_ROOT` to the new path. Correct existing cold-governance sample attribution, which currently assigns PGDATA to `/home` unconditionally. Retained old copies are not current PGDATA. Do not enable the independent cold-residency installer/runner under this topology.
8. **Rehearsal is not rollout evidence.** The same code runs against strictly isolated node-27 fixtures: unique names, owned paths, ephemeral nonproduction ports, exact image and measured UID/GID. Disposable mode cannot address production paths/names/ports or relax a production health gate. Its receipt proves mechanics only. Actual HDD workload acceptance remains a separate human-gated deployment condition.

## Invariant Matrix

Governing invariant: no copied cluster can replace the original unless its complete clean-stopped source and exact deployment identity are proved; the original remains safely restorable until the durable first-business-write boundary, after which stale rollback is forbidden.

Source of truth: observed Docker ID/resolved image/configuration, stopped-source control identity plus complete-tree proof, operation-owned path descriptors, actual unit fences/state, and durable write-release marker.

| Surface | Preserve / reject |
|---|---|
| Producers | CLI/engine creates private authority before mutation; no forged receipt or secret disclosure. |
| Validators | Root health, capacity, ownership, clean control state, complete cluster and unchanged identities are required. |
| Storage | Stream-copy/verify all bytes and required metadata; original and failed target are retained. |
| Public entry | Default plan has no mutation; explicit enforce and one admitted operation are required. |
| Consumers | Same SQL/roles/ports/image/app; governance counts current PGDATA on its actual storage root. |
| Failure/recovery | Interrupted copy/rebind cannot become success; restore only the owned original before write release. |
| Evidence | Disposable results cannot satisfy live readiness; receipts bind actual source/target and do not contain credentials. |

Regression rows: healthy isolated cluster -> identical warm/compressed rows, roles and metadata after switch; failed/partial copy or path/config/health drift -> refusal without source deletion; interruption after stop/rename -> restartable pre-write restoration; released writes -> stale rollback refused; old `/home` geometry and existing cold installer -> unchanged behavior; relocated PGDATA -> counted only on target filesystem.

Boundary checklist: shared serializer and filesystem/evidence primitives; CLI and private state loader; source/copy/readback surfaces; container stop/rename/create/start; persistent unit fences; rollback/release marker; unchanged ingest/display/lifecycle callers; governance accounting and disposable/live evidence separation.

## Risks / Trade-offs

- HDD random I/O may regress hot queries/ingest -> production SQL/API/browser and controlled-ingest gates; do not relax SLOs silently.
- Offline copy extends display downtime -> explicit maintenance approval; use physical copy to avoid logical index rebuild cost, not a promised duration.
- RAID history and shared backup failure domain -> fresh root hardware evidence; independently accepted recovery evidence before separately authorized old-copy disposal.
- Post-release failure -> preserve new database and recover current data; the convenient pre-release rollback is deliberately unavailable.
- Runtime pin and external checkout drift -> retain original version/pin dropins and inspect actual service configuration, not the checkout used to run the new tooling.

## Migration Plan

Merge is human-gated. A later approved window runs fresh plan/health/capacity and baseline capture, prepare, copy, activation/read validation, then explicit write release and controlled ingest/natural tick. Retain both data directories. Production gates use representative SQL1+20 (P95<=300ms, buffers<=5000), local API1+20 (P95<=500ms), browser click1+20 (P95<2s), identical business content and successful ingest. Failures preserve state and follow the phase-specific procedure; no old directory is automatically removed.
