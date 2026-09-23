# Design — Slurm submission isolation (#1908), single-submit array refusal (#1909), gateway-error rendering (#2308)

## Fixture level

`expanded`. #1908 is p1 and changes the durable layout of inputs a live compute job reads after submission; #2308 changes two public response bodies and must not change the durable events beside them.

Unlike batch E1 these three do not share a mechanism. They are sequenced as three implementer passes with a commit between each, largest and riskiest first: #1908 → #1909 → #2308. #2308 touches `apps/api/routes/pipeline.py`, which batch E1 also touched; E1 is merged, so there is no overlap left.

## D1 — #1908: submission-unique inputs, not a lock

### The constraint that decides the shape

The array worker resolves its manifest from the index entry, but only through a safety gate: `workers/shud_runtime/cli.py:60-69` accepts `entry["manifest_path"]` **only** when it is exactly `<workspace>/runs/<entry["run_id"]>/input/manifest.json`, else it falls back to that same path. So a manifest cannot be moved to an arbitrary per-submission directory without changing the worker — which #1908 puts out of scope.

But the gate binds the path to **the entry's own `run_id`**, not to the caller's configured `run_id`. Making the two task run ids submission-unique therefore makes their manifest paths submission-unique *while satisfying the existing gate unchanged*.

### The design

One submission token per live submit: `datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")`, materialised by **exclusive create** of the index with a bounded retry on collision — the same loop `RealSlurmGateway.write_manifest_index` already uses (`services/slurm_gateway/real_backend.py:695-706`). The token must satisfy `SAFE_IDENTIFIER_RE` (`^[A-Za-z0-9][A-Za-z0-9_-]*$`) because the array log dir is derived from the index stem (`array_log_dir`, `real_backend.py:2217-2231`); the timestamp format above does.

| Artefact | Before | After (live submit only) |
|---|---|---|
| index | `runs/<run_id>/input/manifest_index.json` | `runs/<run_id>/input/manifest_index_<token>.json` |
| task run ids | `<run_id>_success`, `<run_id>_controlled_fail` | `<run_id>_<token>_success`, `<run_id>_<token>_controlled_fail` |
| task manifests | `runs/<run_id>_success/input/manifest.json` | `runs/<run_id>_<token>_success/input/manifest.json` |
| array log dir | derived from index stem | derived from index stem → per submission, for free |
| lane evidence (`lane_dir`) | unchanged | unchanged |

Two concurrent `--force` submissions are then disjoint **by construction**. No flock, no lease, no `squeue` probe, and nothing to mock — which is why this shape is preferred over the issue's alternative.

**Write order is load-bearing.** Today `_write_manifest_index` (`:685-688`) writes the two task manifests *first* and the index last, and `EvidenceWriter._write_bytes` (`:172-207`) overwrites via `atomic_write_bytes_no_follow`, skipping even the exists guard under `--force` (`:188`). If the token were only arbitrated by the index's exclusive create at the end, two submissions colliding on the same microsecond would have already overwritten each other's task manifests. So:

1. the token is **claimed first**, by the exclusive create of the index (its content is a pure function of config + token, so the order is free to change);
2. every live-lane shared write then goes through an exclusive-create primitive on `EvidenceWriter`, not the overwriting one. `--force` and `_created_paths` keep their meaning for the `lane_dir` evidence bundle only.

Without both, 1.4's "cleanup removes only what this submission wrote" is a window rather than a property.

Cleanup after a failed `sbatch` stops recomputing its target list from a `run_id` formula (`_shared_runtime_input_paths`, `:1049-1054`) and unlinks only what this submission actually wrote. That is the acceptance item "cleanup must not unlink runtime paths an active submission still references": with disjoint paths the formula would already be safe, but deriving the list from the written paths is what keeps it safe if the layout changes again.

`--force` keeps its documented meaning for the **evidence bundle** in `lane_dir` (the exists guard at `:188-191` is untouched). It no longer has any meaning for live shared inputs, because those can no longer collide. `docs/validation/production-closure.md:76-90` says so.

### Rejected alternatives

- **S1 — version only the index, keep stable task manifests.** Smaller, but does not close the defect: B's `--force` still overwrites the manifests A's queued tasks are about to read. Would require a lock *and* an in-flight probe on top.
- **S2 — move the whole bundle under `runs/<run_id>/input/submissions/<token>/`.** Structurally ideal, but the worker's safety gate rejects any manifest path that is not `runs/<run_id>/input/manifest.json`, so it silently falls back to the stable path — i.e. it would look fixed and not be. Requires a worker change, out of scope.
- **S3 — keep stable paths, add flock + `squeue` in-flight check.** The issue's own alternative. Leaves `--force` and failed-sbatch cleanup bound to mutable shared files, so the protection is a window, not a property; and the test for it is a mocked mutex rather than an observable fact.

### Scope of the behaviour change

Only the lane where `_uses_shared_workspace_inputs(config)` is true (`submit and not fake_slurm`). The dry-run and fake-Slurm lanes keep byte-identical evidence, which is what the existing suite pins. Submission-scoped run ids appear in the live lane's evidence. The sites that re-derive `f"{config.run_id}_success"` / `_controlled_fail` are `:1632`, `_blocked_partial_success` at `:1772`, `:1841` (`_qc_blocking_evidence`) and `:1854`, plus the cleanup formula at `:1049-1054`; `_retry_cancel_evidence` (`:1788-1816`) derives no run id and is **not** among them. All of them must read the ids from the written tasks, or the evidence will name runs that do not exist.

**Residual, disclosed not fixed**: successful submissions leave their index and input directories behind, one set per submission. That matches the gateway's existing timestamped-index behaviour; retention for this lane is not in scope.

## D2 — #1909: one source for the production array job types

`ARRAY_CAPABLE_JOB_TYPES` is a hand-maintained literal that has already drifted from `PRODUCTION_ARRAY_TEMPLATE_NAMES` once. Derive it instead: reverse-look `PRODUCTION_ARRAY_TEMPLATE_NAMES` through **`DEFAULT_JOB_TYPE_TEMPLATES` ∪ `settings.job_type_templates`**, so a deployment override (`services/slurm_gateway/config.py:57,65-68`, per-instance and environment-overridable) can only ever **add** refusals, never remove one. `ARRAY_CAPABLE_JOB_TYPES` has exactly one consumer today (`real_backend.py:263`), so it can safely become an instance-level derivation. `submit_job` keeps refusing before any sbatch call, with `details["endpoint"] == "/api/v1/slurm/job-arrays"`, and `submit_job_array` is untouched.

Check before implementing: whether `ARRAY_CAPABLE_JOB_TYPES` has consumers other than `submit_job` (`:263`) and whether `tests/test_slurm_array_contract.py`'s `_PRODUCTION_ARRAY_JOB_TYPES` already enumerates the four — if it does, the derived set should agree with it and the agreement should be the pin.

Non-goal: the mock backend's laxer accept, the sbatch templates, orchestrator stage routing.

## D3 — #2308: two shapes, not one

`gap["error"]` is currently one dict fed to **both** the HTTP body (`failed_jobs` / `blocked_jobs`) and `store.insert_event`'s `details["error"]` (`apps/api/routes/pipeline.py:646`, `:665`). The fix must therefore produce two:

- **response** — `message` through `_public_error_message` (introduced by #2305) and `details` through `_public_evidence`, which renders list elements and nested scalars alike, so `command[0]` and `stderr.snippet` both lose their host paths.
- **persisted event** — unchanged: secrets-only raw text. Not for anti-laundering reasons — `[local-path]` is deliberately persisted evidence (`openspec/specs/pipeline-job-persistence/spec.md:719`), and #1592/#1630 govern object-URI placeholders only. The reasons are that operators diagnose from the raw text, and that retry's runtime-root recovery reads `("gateway_response", "manifest")` off persisted events (`services/orchestrator/retry.py:1421`, field set `:89-95` including `workspace_dir`/`object_store_root`). A rendered event copy would break that recovery.

Rendering once and sharing the dict is the mistake to avoid; the split is the design.

The same rendering applies to `queue_depth`'s two `ApiError` raises (`:864-878`). `_api_error` (`:899-905`) is **out of scope**: it only ever receives `RetryError`, whose messages are constants and whose details carry no gateway text.

`_unproven_slurm_cancellation_payload`'s `gateway_response` (`:946-958`, also fed to an event at `:687`) is **settled, not deferred**: it dumps a `SlurmJobRecord` whose `manifest` carries `workspace_dir`, `manifest_index_path` and `array_log_dir` for array submissions (`services/slurm_gateway/real_backend.py:390-400`), so the response copy carries host paths and must be rendered, while the event copy must stay raw because `retry.py:1421` recovers runtime roots from exactly that persisted subtree.

Do not try to settle this with a live probe: `RealSlurmGateway.cancel_job` only returns proven records (`real_backend.py:474-484`), so this branch is unreachable against the real backend and a probe would wrongly conclude the paths are structurally impossible.

OpenAPI: `SlurmGatewayUpstreamError`'s **keys** do not change, only values, so `openapi/nhms.v1.yaml` is not touched.

## Sibling surfaces (must not change)

| Surface | Why listed |
|---|---|
| `RealSlurmGateway.write_manifest_index` (`real_backend.py:644-706`) | The precedent #1908 copies. Untouched. |
| `submit_job_array` (`real_backend.py:337-401`) | #1909 changes only the single-submit refusal set. |
| mock backend (`services/slurm_gateway/mock_backend.py`) | Deliberately laxer; out of scope. |
| `chain_array_accounting`, orchestrator stage routing | Production arrays already go through `submit_array_stage`; unaffected. |
| persisted `slurm_cancellation_gap` / `cancel_failed` event `details.error` | Must stay raw. Pinned. |
| `_api_error` (`pipeline.py:899`) | RetryError only; must not be rendered differently. |
| dry-run / fake-Slurm validation lane | Byte-identical evidence; pinned by the existing suite. |
| `workers/shud_runtime/cli.py`'s manifest safety gate | Unchanged — the whole point of D1's run-id choice. |

## Seams under test

- Two live-submit runs against one workspace with the same configured `run_id`, with `sbatch` stubbed: assert the two submissions' index paths, task run ids, manifest paths and array log dirs are pairwise disjoint (#1908).
- The second run's `sbatch` failing: assert its cleanup unlinks only its own paths and leaves the first submission's index and manifests byte-identical (#1908).
- `RealSlurmGateway.submit_job` for each of the four production array job types, with a complete authorized manifest (so the refusal cannot be an accident of log-dir binding), asserting no sbatch call was made (#1909).
- `POST /runs/{run_id}/cancel` and `GET /queue/depth` through `TestClient` with a gateway raising stderr that contains an absolute workspace path and a configured `slurm_bin_path`, asserting the response bodies are rendered and the persisted events are not (#2308).

## Non-goals

- Changing `workers/shud_runtime/cli.py`'s manifest safety gate or the sbatch templates.
- Retention/GC of per-submission inputs and logs.
- The gateway service's own `_gateway_error_response` (`services/slurm_gateway/routes.py:213-218`).
- `redact_payload` / `redact_text` secrets semantics.
- Any live node-22 execution (pre-maintenance freeze).

## Review focus

1. #1908: are the two submissions disjoint in **every** artefact a live task reads — index, manifest, log dir — and does any evidence field still re-derive `<run_id>_success` from config instead of reading the written tasks?
2. #1908: does the dry-run/fake lane produce byte-identical evidence to master?
3. #1909: is the refusal set derived from one source, and would adding a fifth array template be refused without touching `submit_job`?
4. #2308: is the persisted event's `details.error` provably still raw, and is the response provably rendered — in the same test?
5. Does anything in this PR require a live node-22 run that was not performed, beyond what the PR body declares oracle-blocked?
