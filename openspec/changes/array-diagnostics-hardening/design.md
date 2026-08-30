## Context

`submit_job_array` renders one script for a cohort and currently injects the first task's `run_id`; all four array templates consequently write `%A_%a` files under that member. The exact immutable `manifest_index_path` already identifies the submission and maps every `task_id` to `model_id`/`run_id`, but `_collect_array_task_logs` returns only content and task id. Independently, `array_task_status` indexes an empty split result and violates the orchestrator's classified accounting boundary.

## Goals / Non-Goals

**Goals:** make physical log placement cohort-neutral; bind API identity only to the exact immutable index for that submission; preserve old logs and restart lookup; keep reads/discovery bounded and fail-safe; make empty state a failed task on both accounting legs.

**Non-Goals:** changing task artifact paths, terminal reuse (#1736), Slurm comments, non-array templates, or the existing state vocabulary beyond the empty-input guard.

## Decisions

1. Derive one submission-specific log directory from the trusted workspace, cycle, and immutable manifest-index filename (for example `workspace/<cycle>/array_logs/<index-stem>`) through one canonical Slurm-gateway-owned path helper. Render all four templates' `--output`, `--error`, and `mkdir -p` against this directory, and require every submitter—including `production_closure`'s direct-render/raw-sbatch acceptance lane—to safely create that exact path before `sbatch` and pass it unchanged to every post-submit reader/evidence producer. No member `run_id` appears. This avoids wrapper redirection's two-log split, prevents renderer/submitter/reader drift, and binds restart discovery to one exact index without guessing the newest timestamp.
2. Join task identity from that exact index in one bounded read. A live record uses its stored `manifest_index_path`; restart discovery derives the index from the neutral directory. Missing, unsafe, ambiguous, malformed, oversized, or mismatched metadata must not invent identity or erase readable log content; the response marks identity incomplete. Legacy `workspace/<run_id>/logs` and root-log lookup remain read-only fallbacks.
3. `array_task_status` maps empty/whitespace state to `failed`. `UNKNOWN` is not used because this function's public return domain is `succeeded|cancelled|failed`; empty input belongs to the existing fail-closed default and drives normal task error classification.

## Risks / Trade-offs

- [New directory absent when Slurm opens output] → gateway and every direct/raw submitter use the canonical path helper, create that exact path before `sbatch`, and retain the template `mkdir` as an idempotent guard.
- [Renderer and evidence reader drift to different paths] → carry the derived neutral path explicitly through production-closure submit, blocker checks, bounded reads, and emitted evidence; owning tests assert the directory exists at raw sbatch time and all task paths use it.
- [Restart discovery scans unrelated workspace content] → traverse only the fixed neutral hierarchy with entry/task/byte bounds and reject ambiguity.
- [Identity index is damaged while logs remain valid] → preserve content, expose incomplete identity, and never pair logs with a guessed index.
- [Historical operators still request leader-run paths] → keep old location fallback and regression coverage; no migration deletes or moves old files.

## Migration Plan

Deploy gateway, production-closure validator, and templates together. New gateway and production-closure submissions write/read only the neutral layout; historical gateway layouts remain readable. Rollback restores old templates/read ordering without moving data. No database migration or object-store mutation is required.

## Open Questions

None. Issue #1539 selects the recommended `failed` behavior; issue #1742 selects neutral placement plus identity join.
