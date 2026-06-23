# Changelog

## 0.2.0 — 2026-06-07 (node-27 display_readonly as a second oracle)

- **Three ends, two oracles.** The skill no longer treats node-22 as the sole test oracle. node-27
  (`display_readonly`, `nwm@…27:/home/nwm/NWM`, read-only DB replica + published artifacts, no
  Slurm/SHUD/control-plane write) is now the oracle for display API / frontend production /
  read-only-boundary **live receipts**. Intro reality #1, the Roles table, and the description were
  updated; the description gained node-27 trigger phrases (`node-27 开发`, `27 上线`, `display 验证`,
  `前端生产化`, `live receipt`).
- **New core rule — Verification-oracle routing.** Each check goes to the node that owns it (backend/DB/
  Slurm → node-22; display/frontend/read-only boundary → node-27; ruff/openspec/pnpm → local; a change
  touching both is verified on both). Never cross-credit one node's result for the other's scope.
- **Issue-archetype split.** The deployment / live-operationalization archetype now has two flavors:
  node-22 compute_control (Slurm gateway / daemon / multi-basin) and **node-27 display_readonly**, with
  the latter bound to `docs/runbooks/node-27-bringup-checklist.md` C1–C4 (deploy receipt, read-only-DB
  denied-write, cross-plane identity live, browser e2e). `BLOCKED` examples extended (no real RO DB
  account; published artifacts not yet copy-backed to 27).
- **Dev-phase runs the display API LOCALLY on node-27, not `docker compose up`.** Iteration uses a
  locally-started service (`uv run python -m uvicorn apps.api.main:app`, bound to 127.0.0.1, env from
  `display.env`) — same `apps.api.main:app` entrypoint + role guards as the container, no image build /
  no outward container. `compose.display.yml` is the production deploy artifact; C1's
  `validate_two_node_docker_runtime.py static` validates its config WITHOUT bringing it up, so C1 closes
  in dev without a deploy.
- **Deploy gate (safety hardening).** A real `docker compose up -d` (persistent outward-facing service)
  is hard-to-reverse + state-changing → requires explicit human confirmation / standing
  pre-authorization, same governance as merge. Dev-phase local-run (127.0.0.1, torn down after the
  receipt) is exempt.
- **New `references/dual-end-flow.md` §2b — node-27 recipes** (learned during the 2026-06-07 node-27
  sync + bring-up). Connect (user **nwm**, not frd_muziyao); `--ff-only` discipline; the **far-behind
  untracked SAME-NAME ff-abort** recovery, now a **fail-CLOSED heredoc'd remote script** — it aborts
  (exit 4) unless every blocker is byte-identical to the incoming master version, compared via
  `git hash-object` vs `git rev-parse origin/master:<f>` (plain `git diff <commit> -- <f>` ignores
  untracked files and would falsely pass); paths are parameterized (`$BLOCKERS`, filled from the abort
  message, not hardcoded incident literals); back-up + stash-valuable precede any delete; never touch
  gitignored `artifacts/`/`.nhms-*`/`data/Basins/`. Dev-phase local-run + C1/C2 commands verified
  against real code: env files are `infra/env/display.env` + `infra/env/display-readonly-secrets.env`;
  C2 is the standalone module `python -m services.production_closure.readonly_db_validation`
  (`--source/--cycle-time/--strict-run-id/--model-id/--job-id`; exit 0=PASS / 2=BLOCKED / 1=fail); probe
  port `127.0.0.1:8000`, readiness `GET /health`; DSN must be the read-only account or the denied-write
  receipt is invalid.
- Phase C re-verify and the worklog role line now route by owning node.

## 0.1.2 — 2026-06-04 (m24 live-operationalization learnings)

- New **issue-archetypes** section: the base loop covers code-fix issues (m21/m22/m23 shape); m24 adds
  three new shapes the loop must adapt to — **deployment / live-operationalization** (node-22 deploy +
  live receipt, orchestrator does the SSH deploy, verify = receipt-or-typed-`BLOCKED`, record
  `execution_mode` live_proof/deterministic + `validate_receipt`), **gate / tracking** (no code; closes
  on a referenced receipt; `dependency-gate` label), and **evidence-baseline** (read-only artifact).
- New **issue-state ↔ spec-state sync** rule: closing an issue must tick the matching OpenSpec
  `tasks.md` boxes (m23 #255 was closed with `tasks.md` 3.x unchecked, which m24 then mis-read as an
  open prerequisite).
- Remote mechanics (learned this session): `set -a` before sourcing `compute.host.env` (vars have no
  `export` → `KeyError: 'DATABASE_URL'` in child `uv run python`); `psql` not on PATH → query via
  `psycopg2` with DSN-password redaction; `s3://nhms` is a filesystem object store
  (`OBJECT_STORE_ROOT`=`QHH_RUN_ROOT`), run worker CLIs through the launcher/sbatch. Added a copy-paste
  env-source + DB-query recipe to `references/dual-end-flow.md` §2.
- CI cost discipline cross-ref (CLAUDE.md): bundle docs/specs into the one terminal push; no trailing
  docs-only commit while waiting for CI green.

## 0.1.0 — 2026-06-03

- Initial project-level skill capturing the NHMS/NWM dual-end (本地编辑 + 远端 node-22 测试) issue
  workflow as run on issue #256 / PR #265.
- Defines roles (Claude Code orchestrator / fix subagent / review subagent / node-22 / CI-as-gate),
  the dual-end verification loop with remote command recipes, parallel reviewer-pack dispatch + the
  Phase 4.5 verification gate, dynamic state-aware phase entry, and the issue worklog template.
- Reuses review vocabulary from `codex-codeagent-workflow` and `risk-adaptive-cross-review` rather
  than forking it; defers out-of-scope findings to `stage-change-pipeline` / `gh-create-issue`.
- `references/dual-end-flow.md` holds detailed steps, recipes, the worklog template, and the #256
  worked example.
- Makes the review-fix round an explicit loop with a defined "clean" termination (zero in-scope
  CONFIRMED, zero merge-blocking PLAUSIBLE; rest REFUTED or filed as issues) that gates Phase 7/8 —
  no final check or merge off an un-reviewed fix.
- Aligns the loop semantics with `codex-codeagent-workflow` Phase 4/5/6.2: post-fix rounds are
  COMPREHENSIVE over the full updated diff (not narrowed to the fix area; `ci-only` repairs excepted),
  and the loop is bounded by the inherited escape gates (3rd same-class round → Review Failure Retro;
  5 rounds → hard gate / Deep Retro; 2nd shared-surface same-class → pattern-escalation invariant closure).

## 0.1.1 — 2026-06-03 (skill-review-audit remediation)

- P0: remote pull recipe now FAILS LOUD on a dirty remote tree (`git status --porcelain` + `--ff-only`)
  instead of the silent `git stash pop -q ... 2>/dev/null`, which could lose uncommitted work on the
  shared node-22 tree.
- P0: replaced the foreground `until ssh ...; do sleep 15; done` poll (blocked by the Claude Code Bash
  tool) with `run_in_background` + completion-notification waiting; documented the constraint.
- P0: description gained a DECISIVE TRIGGER clause to disambiguate from `codex-codeagent-workflow` /
  `cc-cx-workflow` — this skill only when verification runs on node-22's real DB/Slurm/SHUD over SSH.
- P1: remote test log is now uniquely named (`/tmp/verify-<issue>-<sha>.log`) to avoid concurrent
  clobber; added an explicit "node-22 unreachable = oracle offline" degradation rule.
- P2: tagged each dual-end Phase A–G with its `(≈ canonical Phase N)` mapping for navigability.
