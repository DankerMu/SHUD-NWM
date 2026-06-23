---
name: dual-end-issue-workflow
description: >
  Project-level workflow for implementing or finishing a GitHub issue in the NHMS/NWM repo under the
  local-edit + remote-test (双端) model across TWO role nodes: code is edited and committed locally
  (macOS), then pulled and verified on one of two remotes — node-22 (compute_control,
  frd_muziyao@…22:/scratch/frd_muziyao/NWM) owns the real PostgreSQL/TimescaleDB, Slurm, Docker, SHUD
  and is the backend/real-DB pytest oracle; node-27 (display_readonly, nwm@…27:/home/nwm/NWM) owns the
  read-only DB replica + published artifacts and is the display API / frontend-production /
  read-only-boundary live-receipt oracle. Codex orchestrates; fixes and reviews are delegated to
  dispatched subagents (the Agent tool), not codeagent-wrapper. Use this skill whenever the user wants
  to implement, fix, verify, review, or merge an issue/PR in this repo, or says things like "处理 issue",
  "完成 #XX", "跑工作流", "双端", "本地改远端测", "继续动作流", "派发 subagent 修/审", "远端验证",
  "node-27 开发", "27 上线", "display 验证", "前端生产化", "live receipt", or points at a PR and says
  "merge it / 合并". DECISIVE TRIGGER vs sibling issue-workflows: use this skill ONLY when verification
  must run on node-22's real PostgreSQL/Slurm/SHUD environment or node-27's display_readonly environment
  over SSH; for single-machine issue work with no remote node use codex-codeagent-workflow (or
  cc-cx-workflow). Do NOT use for pure spec authoring (use stage-change-pipeline) or docs-only PRs.
---

# Dual-End Issue Workflow (本地编辑 + 远端测试)

This is the **NHMS/NWM instantiation** of `codex-codeagent-workflow`. The deep concepts — OpenSpec
fixtures, risk-adaptive cross-review, the finding contract, the phase semantics — are owned by
`codex-codeagent-workflow` and `risk-adaptive-cross-review`. This skill does **not** restate them. It
binds them to two project realities the canonical workflow does not cover:

1. **Three ends, two oracles.** Code is edited on macOS; verification is a network round-trip across
   **two role nodes**. **node-22** (`compute_control`, `frd_muziyao@…22:/scratch/frd_muziyao/NWM`) is
   the only environment that can run DB/Slurm/SHUD tests — the backend / real-DB-pytest oracle.
   **node-27** (`display_readonly`, `nwm@…27:/home/nwm/NWM`) is a read-only DB replica + published
   artifacts — the oracle for display API / frontend production / read-only-boundary **live receipts**
   (it has no Slurm/SHUD/Docker-socket/control-plane write and must never be asked to run them). Route
   each verification to the node that owns it (see *Verification-oracle routing* below); never declare a
   display/frontend change verified off a node-22 pytest, nor a backend change verified off a node-27
   receipt.
2. **Subagent execution.** In this project, implementation fixes and reviews are delegated to
   **dispatched subagents (the Agent tool)** — not `codeagent-wrapper`. Codex stays the
   orchestrator and verifier.

If you only need the canonical issue workflow on a single machine, use `codex-codeagent-workflow`
directly. Use this skill when the work touches node-22's or node-27's real environment.

## Roles

| Role | Owns |
|------|------|
| **Codex** (local orchestrator) | State assessment, fix/review planning, candidate dedup + Phase 4.5 verdicts, evidence synthesis, git/PR, CI tracking, merge gate, **worklog maintenance** |
| **fix subagent** (dispatched) | A cohesive, invariant-level code/test fix. Leaf task: edits local files, runs `ruff`, **does not commit/push** |
| **review subagent** (dispatched, parallel) | One reviewer-pack lens (read-only). Emits candidate findings only |
| **node-22 server** (`compute_control`) | The real backend test environment (PostgreSQL/TimescaleDB, Slurm, Docker, SHUD). Authoritative for "do the backend tests pass" |
| **node-27 server** (`display_readonly`) | The display/frontend live-receipt environment (read-only DB replica + published artifacts; no Slurm/SHUD/control-plane write). Authoritative for "is the display API / frontend / read-only boundary live-verified". The **orchestrator** runs the node-27 deploy + receipt over SSH (`nwm@…27:/home/nwm/NWM`) |
| **GitHub CI** | The **final merge gate** — not an iteration signal. Never block the work waiting on CI mid-flow |

## Core rules (non-negotiable)

- **Remote is the test oracle, not CI.** Iterate against the real remote result (node-22's real DB,
  or node-27's live receipt — see routing). CI is consulted only at the merge gate. Blocking the flow
  on a 16-minute CI run mid-iteration wastes the whole point of having a real remote environment.
- **Verification-oracle routing — send each check to the node that owns it.** Backend code, real-DB
  pytest, Slurm/SHUD behavior → **node-22**. display API / frontend production / read-only-boundary
  (display_readonly deploy receipt, read-only-DB denied-write, cross-plane identity live, `/hydro-met`
  + `/ops` browser e2e) → **node-27**. `ruff` / `openspec validate` / frontend `tsc`/`pnpm test`/
  `check:api-types` → local. A backend change that also alters a display contract (OpenAPI, a field the
  display consumes) is verified on **both**: node-22 pytest for the contract, node-27 receipt for the
  live display. Never cross-credit — a node-22 pytest does not verify a display change, and a node-27
  receipt does not verify backend DB logic.
- **GitHub is the transit.** Local commits → `git push` → remote `git pull`. Never rsync source between
  machines for the workflow; the repo syncs through `DankerMu/SHUD-NWM`. (Runtime/env dirs are never
  synced — see AGENTS.md.)
- **Subagents are leaves.** Every dispatched fix/review subagent gets the delegation guard (below). It
  must not dispatch its own subagents, call codeagent-wrapper, or invoke any workflow skill. Fix
  subagents edit and run `ruff`; they do **not** commit. The orchestrator commits in logical units.
- **Disjoint write sets when parallel.** Two fix subagents must never share a file. The producer/core
  module and the downstream test-fixtures are separate lanes; serialize when a contract is still
  settling (downstream fixtures depend on the core's final contract).
- **Cross-review is a parallel panel, then a verifier gate.** A real review round is N reviewer packs
  dispatched in parallel (not one consolidated reviewer), followed by Phase 4.5: dedup, then one
  independent verifier per surviving candidate (verifier ≠ originating reviewer) returning
  CONFIRMED/PLAUSIBLE/REFUTED. Pack count and finding contract come from
  `risk-adaptive-cross-review`; DB-backed state / schema / shared-root PRs warrant the 6-pack.
- **The review-fix round LOOPS until clean — this is the gate to merge.** One round is
  panel → verify → fix the in-scope CONFIRMED/merge-blocking-PLAUSIBLE. After a fix round you **re-run
  a review round** (a fix can introduce a new defect, and a real fix changes the code the last panel
  read). Keep looping until a round produces **a clean verdict**, then and only then proceed to the
  final independent check (Phase 7) and merge (Phase 8). **Clean** = zero in-scope CONFIRMED findings
  and zero merge-blocking PLAUSIBLE left open; every other surviving candidate is either REFUTED (with
  rationale) or converted to an out-of-scope artifact (issue).
- **A post-fix round is a COMPREHENSIVE review of the full updated diff — never narrowed to the fix
  area.** Use the same reviewer mix/count as round 1 on the current head. A prior round can have missed
  a defect outside the fix area, so narrowing the re-review defeats the loop. (The only exception is a
  Phase-8 `ci-only` repair — lint/timing/wiring with no runtime-semantic change — which does not
  retrigger the review panel; a `semantic` change always does.)
- **The loop is bounded — inherit codex-codeagent-workflow's escape gates, do not invent "loop forever".**
  Count comprehensive rounds. Same failure class surviving into a 3rd round → write a **Review Failure
  Retro** and change the plan (broaden fixture/scope, strengthen reviewer prompts, or split) instead of
  another identical fix. Reaching 5 comprehensive rounds → the **hard gate**: stop ordinary looping,
  write the Deep Retro + Gate-Level PR Strategy Review before any further fix/review/merge. A 2nd
  same-class finding in a shared surface → pattern escalation → an invariant-closure fix (audit all
  sibling surfaces), not a per-line patch. These come verbatim from `codex-codeagent-workflow` Phase
  4/5/6.2 — this skill does not restate the templates, it requires you to load and apply them.
- **The orchestrator owns the verdict.** When reviewers conflict, resolve it yourself with code
  evidence (a recall-biased reviewer raising a candidate is doing its job; the verification gate
  decides). Record the verdict and its evidence.
- **Worklog is the single source of truth.** Maintain `openspec/changes/<milestone>/issue-<N>-worklog.md`
  continuously: roles, dynamic phase state, candidate/verdict ledger, validation matrix, decisions.
  Update it as work happens, not at the end — it is how the user (and a resumed session) tracks progress.
- **Out-of-scope findings become artifacts, not scope creep.** A real but pre-existing / out-of-boundary
  finding gets an OpenSpec change + GitHub issue (see `gh-create-issue`, `stage-change-pipeline`), not a
  silent fix inside the current PR.
- **Respond in Chinese; PR work-summary in Chinese.** Per repo convention, post a structured Chinese
  work-summary comment before the merge gate.

## Delegation guard (paste into every dispatched subagent)

```text
你是父 workflow 的 leaf 任务。
- 不得调用 codeagent-wrapper / codeagent 技能 / 任何 workflow 技能。
- 不得派发子 agent / 并行 agent / 嵌套 reviewer / 任何其他 AI 代理。
- 只用普通 shell/build/test 工具，直接编辑文件完成本任务。
- 评审任务为只读：不得编辑/commit/push。
- 修复任务可编辑，但不得 commit/push（父 workflow 统一提交）。
- 若没有嵌套 AI 委派就无法完成，停止并报告 blocker。
```

## The dual-end verification loop

This is the inner loop that replaces "run the tests" in the canonical workflow:

```
本地编辑(subagent) → 本地 ruff → 父 workflow commit → git push
  → 远端 git pull → 远端真实 DB 跑验证命令 → 结果回流 → 下一轮
```

Remote mechanics that actually matter (learned the hard way — keep them):

- Connect: node-22 `ssh -p 32099 frd_muziyao@210.77.77.22` (repo `/scratch/frd_muziyao/NWM`);
  node-27 `ssh -p 32099 nwm@210.77.77.27` (repo `/home/nwm/NWM`, user is **nwm** not frd_muziyao —
  frd_muziyao has no account on 27). Both reach GitHub `DankerMu/SHUD-NWM` for pull.
- **Always use a login shell for `uv`**: `ssh ... 'bash -lc "cd <repo> && uv run pytest ..."'`.
  A non-login shell does not have `uv` on PATH and the run silently fails.
- **Pull fails loud on a dirty remote tree — never auto-`stash pop`.** The remote working tree is shared
  and may hold uncommitted work; a swallowed `git stash pop` conflict silently loses it. Gate the pull on
  `git status --porcelain` and `git pull --ff-only`; if dirty, STOP and resolve by hand. Full recipe in
  `references/dual-end-flow.md` §2.
- **node-27 sync gotcha (a long-behind tree): `--ff-only` can abort on untracked SAME-NAME files.**
  When 27 is far behind, files master newly tracks may exist as untracked locally → `error: untracked
  working tree files would be overwritten by merge`. Recipe: `diff` them against the incoming master
  version (the ones we hit — `.agents/skills/*`, `docs/bugs.md` — were 0-diff identical); back up to
  `~/NWM-presync-backup-<date>/`, `git stash push -- <uniquely-valuable files>` (E2E receipts!), clean
  ONLY the confirmed-identical blockers, then `git merge --ff-only origin/master`. **Never** touch
  gitignored data/evidence dirs (`artifacts/`, `.nhms-*`, `data/Basins/`). Full recipe in
  `references/dual-end-flow.md` §2b.
- **Long tests: write to a UNIQUELY-named remote file and wait via `run_in_background`, not a poll loop.**
  Don't pipe a long run through the SSH session (the pipe can drop). Name the log per issue/sha
  (`/tmp/verify-<issue>-<sha>.log`) so concurrent runs don't clobber each other. Launch the SSH command
  with the Bash tool's `run_in_background:true` — it re-invokes you on exit. **Do not** hand-roll
  `until ssh ...; do sleep 15; done`: the Codex Bash tool blocks foreground `sleep`, so that loop
  will not run as written (use Monitor if you need an explicit until-condition).
- The DB suite is slow (full suite ~16 min). Prefer a **targeted set** = Evidence-Floor files + the
  exact files the change touches, and let GitHub CI run the full suite as the gate. (CI cost
  discipline, per AGENTS.md: bundle docs/specs into the one terminal push; do not retrigger the full
  7-job CI with a trailing docs-only commit while waiting for green.)
- **Sourcing node-22 prod env needs `set -a`.** `infra/env/compute.host.env` (not synced) defines
  `DATABASE_URL`/`OBJECT_STORE_ROOT`/… **without `export`**, so a plain `source` leaves them out of
  child processes (`uv run python` → `KeyError: 'DATABASE_URL'`). Use
  `set -a; source infra/env/compute.host.env; set +a`. `psql` is **not on the node-22 PATH** — query
  the real DB via `uv run python` + `psycopg2`, and redact the password
  (`sed -E 's#://[^@]*@#://REDACTED@#'`) before echoing any DSN.
- **`s3://nhms` is a filesystem object store** rooted at `OBJECT_STORE_ROOT` (= `QHH_RUN_ROOT`), not
  real S3. A worker CLI (`nhms-canonical convert`, …) run by hand outside the launcher without
  `OBJECT_STORE_ROOT` resolves `s3://` as a local relative path (`raw`) and fails — go through the
  launcher/sbatch, or export the roots first.
- `openspec` is a node CLI and is **not on the remote PATH** — run `openspec validate` locally; the
  spec files are identical across both ends.
- A harmless `cfgrib/libeccodes GLIBCXX` warning appears on node-22; it is a warning, not a failure.
- **node-22 unreachable = the oracle is offline.** Do not declare "tests pass" from local `ruff` alone;
  report the outage, hold the phase, and let CI's full suite be the Phase G gate.

The detailed phase flow, the worklog template, and a full worked example are in
`references/dual-end-flow.md`. Load it when actively running the workflow.

## Issue archetypes — not every issue is a code-fix

The base loop assumes the deliverable is committed code verified by node-22 pytest (the m21/m22/m23
remediation shape; worked example #256). Some issues — notably m24 live operationalization — are a
different shape; adapt the loop instead of forcing it:

- **Deployment / live-operationalization issue** (deploy a service or run the daemon on the owning
  node and produce a **live receipt**). Two flavors by owning node:
  - **node-22 compute_control** — Slurm gateway #288, continuous daemon #292, multi-basin live pass
    #291. Receipt = the service ran on the real DB/Slurm/SHUD.
  - **node-27 display_readonly** — the m25 / node-27 bring-up shape. The Evidence Floor is
    `docs/runbooks/node-27-bringup-checklist.md` **C1–C4**: C1 deploy receipt (no Slurm CLI/socket,
    `/api/v1/slurm/*` 404, runtime config = display_readonly), C2 read-only-DB denied-write matrix
    (real RO account, INSERT/UPDATE/DDL all rejected, BLOCKED if no real DB — no mock PASS), C3
    cross-plane identity live (one `run_id/source/cycle_time/model_id/basin_id` strung 22-produce → DB
    → published logs → latest-product → 27 `/hydro-met`+`/ops`, reject historical fallback), C4 browser
    e2e (display controls hidden/disabled, no retry/cancel/Slurm POST). These run on **node-27** — a
    node-22 pytest does **not** close them.

    **Dev-phase: run the display API LOCALLY on node-27, do NOT `docker compose up`.** During iteration
    verify against a locally-started service on node-27 (`uv run python -m uvicorn apps.api.main:app`
    bound to `127.0.0.1`, with `NHMS_SERVICE_ROLE=display_readonly` from `display.env`) — fast, no image
    build/registry, no outward-facing container. `infra/compose.display.yml` is the **production deploy
    artifact**: C1's `validate_two_node_docker_runtime.py static` checks the compose/env config *without
    bringing it up*; an actual `docker compose up -d` is a **production deploy** step (see the deploy
    gate below), not part of dev iteration. The same `apps.api.main:app` entrypoint and role guards run
    both ways, so the local-run receipt is faithful to the containerized boundary.

  The deliverable is partly **node-side** (compose/env config that is *not synced*) plus a live receipt,
  not only committed code. The **orchestrator** (not a leaf subagent) starts the service / runs the
  receipt over SSH on the owning node; a fix subagent still owns the repo-side code (app entrypoint,
  compose/unit template, deterministic tests). "Verify" = the receipt exists and validates, **or** a
  typed `BLOCKED` naming the exact missing dependency (e.g. published artifacts not yet copy-backed to
  27's readable path; no real RO DB account) — both are legitimate terminal outcomes. Never fabricate
  PASS off local ruff. Record `execution_mode` (`live_proof` vs `deterministic`) in the receipt; when
  the Evidence Floor defines a machine-checkable receipt schema, add a `validate_receipt` assertion so
  the floor closes objectively, not on reviewer judgement.
  - **Deploy gate (outward-facing — human-confirmed).** A real `docker compose up -d` (or any command
    that starts a persistent outward-facing service / production container) is hard-to-reverse and
    state-changing: the orchestrator requires explicit human confirmation or standing pre-authorization
    before running it, same governance as merge. Dev-phase local-run (bound to `127.0.0.1`, torn down
    after the receipt) does **not** need that gate.
- **Gate / tracking issue** (no code; closes on a referenced receipt — e.g. a cross-milestone
  dependency gate #287). No fix subagent, no review panel; it closes when the referenced capability
  is proven closed or recorded `BLOCKED`. Label it (`dependency-gate`) so it is not mistaken for an
  implementation issue.
- **Evidence-baseline issue** (read-only; emit a baseline artifact — #286). No production code
  change; the deliverable is the baseline receipt other issues reference.

## Keep issue-state and spec-state in sync

When you close an issue, tick the matching OpenSpec `tasks.md` boxes in the same change. A closed
issue with unchecked `tasks.md` (seen on m23 #255, which a later milestone then mis-read as an open
prerequisite) makes the spec lie about what is done. The worklog, the GitHub issue, and `tasks.md`
must agree.

## Dynamic phase entry (don't replay finished phases)

Assess the issue's real state first, then enter at the first unfinished phase. A PR that already has an
implementation and a round-1 review enters at "apply fixes", not at "create the OpenSpec fixture".

```
assess state (PR? commits? prior review? CI red? tasks checked?)
  → [if needed] OpenSpec fixture + risk triage          (Phase 0/0.5)
  ┌─► fix: dispatch fix subagents, disjoint write sets    (Phase 6 / 1)
  │     → remote verify on node-22 real DB                (the dual-end loop)
  │     → cross-review: parallel reviewer-pack panel      (Phase 4)
  │     → verify gate: dedup + independent verifier       (Phase 4.5)
  │     → synthesis: in-scope CONFIRMED? out-of-scope?    (Phase 5/6)
  └──── NOT clean ──┘   (in-scope CONFIRMED / blocking PLAUSIBLE remain → loop)
        │
        clean (0 in-scope CONFIRMED, 0 blocking PLAUSIBLE; rest REFUTED or → issue)
        ▼
  → final independent check on the clean tree            (Phase 7)
  → evidence + Chinese work-summary + CI gate + merge     (Phase 8)
```

The review-fix loop is the merge gate: you cannot reach Phase 7/8 while an in-scope CONFIRMED or
merge-blocking PLAUSIBLE is open. Merge itself is human by default; auto-merge only with explicit
pre-authorization, and only after the review is clean **and** required CI is green.

## References

- `references/dual-end-flow.md` — detailed phase steps, remote command recipes, the issue worklog
  template, the parallel-review dispatch pattern, and a worked example (issue #256).
- `codex-codeagent-workflow` — canonical phase semantics, OpenSpec fixture rules, merge governance.
- `risk-adaptive-cross-review` — reviewer packs, the finding contract, failure-class synthesis.
- `stage-change-pipeline` / `gh-create-issue` — turning out-of-scope findings into specs + issues.
- Project `AGENTS.md` — server topology, sync boundaries, current-milestone verify commands.

## When NOT to use

- Pure OpenSpec/spec authoring with no implementation → `stage-change-pipeline`.
- Docs-only PRs, or a single-machine task that touches neither node-22's DB/Slurm/SHUD nor node-27's
  display_readonly live environment → `codex-codeagent-workflow` or a direct edit.
