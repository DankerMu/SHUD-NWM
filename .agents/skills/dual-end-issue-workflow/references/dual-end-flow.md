# Dual-End Flow — detailed steps, recipes, templates

Load this when actively running `dual-end-issue-workflow`. It assumes you have already read the
SKILL.md core rules.

## Table of contents

1. Phase-by-phase steps
2. Remote command recipes
3. Parallel cross-review dispatch
4. Phase 4.5 verification gate
5. Issue worklog template
6. Worked example (issue #256 / PR #265)

---

## 1. Phase-by-phase steps

> Phase labels A–G are this skill's dual-end steps; the `(≈ canonical Phase N)` tag on each maps to
> `codex-codeagent-workflow`'s numbered phases so the inherited templates are easy to locate.

### Phase A — Assess state (always first) — (≈ canonical Phase 0 state read; no DAG select)

Before touching anything, determine where the issue actually is. Cheap, parallel reads:

- `gh pr list --head <branch> --state all` and `gh pr view <PR> --json mergeable,mergeStateStatus`
- `gh pr checks <PR>` (note which jobs are red, and read the failing job log to classify failures)
- `git log --oneline origin/master..<branch>` (what's already committed)
- the milestone `tasks.md` Evidence Floor for the issue, and the issue body `Verification:` block
- whether a prior review round left artifacts (e.g. `.tmp/reviews/pr-<N>-round1/`)

Write the assessment into the worklog and pick the dynamic entry phase. Capture a **baseline**: run the
Evidence-Floor verification on node-22 once so you know what currently passes before you change anything.

### Phase B — Fix (dispatch fix subagents) — (≈ canonical Phase 1 / Phase 6)

- Group fixes by **invariant / failure class**, not by individual finding. One cohesive fix = one
  subagent. Do not split a single invariant across subagents.
- Assign **disjoint write sets**. Typical lanes: (1) core module + its direct tests; (2) downstream
  test fixtures/harness; (3) docs. Serialize lanes that depend on each other (downstream fixtures need
  the core's final contract — run the core lane first, let it report the settled contract, then the
  fixture lane).
- Each fix subagent: delegation guard, the exact failing evidence, the relevant Phase-5 fix plan text,
  the write-set boundary, "local has no DB — run ruff, don't block on DB tests", and "do not commit".
- When a subagent returns, **verify its work yourself** (git diff, ruff, read the key logic) before
  committing. Commit in logical units with `(#<issue>)` in the message.

### Phase C — Remote verify (the dual-end loop) — (≈ canonical Phase 2, on the owning node)

Push, then verify on the node that **owns** the change (Verification-oracle routing, SKILL.md core
rules):

- **Backend / DB / Slurm / SHUD** → pull on node-22, run the targeted suite on the real DB (§2). Green
  is the iteration signal.
- **display API / frontend production / read-only boundary** → pull on node-27, produce/refresh the
  live receipt (§2b; C1–C4 of `docs/runbooks/node-27-bringup-checklist.md`). A passing receipt — or a
  typed `BLOCKED` naming the missing dependency — is the signal.
- **A change touching both** (e.g. an OpenAPI field the display consumes) → verify on both: node-22
  pytest for the contract, node-27 receipt for the live display.

If red, classify and loop back to Phase B. Never cross-credit one node's result for the other's scope.

### Phase D — Cross-review (parallel panel) — (≈ canonical Phase 4)

Dispatch the reviewer-pack panel **in parallel, in one turn** (see §3). For a DB-backed / schema /
shared-root change, that is the 6-pack: spec, correctness, integration, security-perf, test-evidence,
invariant-compat. Each is read-only and emits candidate findings only.

### Phase E — Verification gate (Phase 4.5)

Dedup near-duplicate candidates, then verify each surviving candidate independently (§4). Only
CONFIRMED + risk-weighted PLAUSIBLE proceed to a fix. When reviewers conflict, the orchestrator
resolves it with code evidence and records the verdict.

### Phase F — Synthesis, then loop until clean — (≈ canonical Phase 5 + the 4/6.2 loop gates)

- CONFIRMED in-scope → another Phase B fix round (cheap convergent guards are worth doing even if
  "non-blocking" — they lock invariants).
- CONFIRMED but pre-existing / out-of-boundary → OpenSpec change + GitHub issue, not a silent fix.
- **Then loop back to Phase C/D.** A fix round is not the end — every fix changes code the last panel
  read and can introduce a fresh defect, so each fix round is followed by a remote re-verify and a
  re-review. The re-review is **comprehensive over the full updated diff with the same reviewer mix as
  round 1**, not narrowed to the fix area — a prior round can have missed something outside it (this
  mirrors `codex-codeagent-workflow` Phase 4: do not narrow follow-up rounds). The lone exception is a
  Phase-G `ci-only` repair (lint/timing/wiring, no runtime-semantic change), which skips the panel; a
  `semantic` change always re-reviews.
- **Bound the loop — inherit the escape gates, do not loop forever.** Count comprehensive rounds. Same
  failure class into a 3rd round → **Review Failure Retro** (change the plan, don't refire the same fix).
  5 comprehensive rounds → **hard gate**: stop, write the Deep Retro + Gate-Level PR Strategy Review
  before any further fix/review/merge. 2nd same-class finding in a shared surface → **pattern escalation**
  → invariant-closure fix that audits all sibling surfaces (Phase 6.2), not a per-line patch. Templates
  live in `codex-codeagent-workflow` Phase 4/5/6.2 — load and apply them; this skill does not restate them.
- **Termination — the round is "clean" when:** zero in-scope CONFIRMED findings remain, zero
  merge-blocking PLAUSIBLE remain open, and every other surviving candidate is either REFUTED (one-line
  rationale recorded) or converted to an out-of-scope artifact (issue). Record the clean verdict in the
  worklog. Only a clean round unlocks Phase G. Do not enter the final check or merge off an un-reviewed
  fix.

### Phase G — Evidence, summary, gate, merge — (≈ canonical Phase 7 final review + Phase 8)

Precondition: the review-fix loop reached a clean round (Phase F termination). If anything in-scope is
still open, you are still in the loop — go back, don't proceed.

- Assemble the validation matrix (remote targeted suite, ruff, openspec validate, CI full-suite job,
  review verdict).
- Write the Chinese work-summary to a local file first, inspect rendered markdown, then post with
  `gh pr comment --body-file`.
- CI is the gate here. Wait for it green, then merge per the authorization (human gate by default;
  auto-merge only if pre-authorized).

---

## 2. Remote command recipes

```bash
# Connect
ssh -p 32099 frd_muziyao@210.77.77.22

# Pull latest onto node-22 — FAIL LOUD if the remote tree is dirty; never auto-stash-pop.
# The remote tree is shared and may hold someone's uncommitted work; a swallowed `stash pop`
# conflict silently corrupts or loses it. Inspect, don't paper over.
ssh -p 32099 frd_muziyao@210.77.77.22 'bash -lc "cd /scratch/frd_muziyao/NWM \
  && if [ -n \"\$(git status --porcelain)\" ]; then echo DIRTY_REMOTE_TREE; git status --short; exit 3; fi \
  && git pull --ff-only origin <branch> && git log --oneline -1"'
# If it prints DIRTY_REMOTE_TREE / exits 3: STOP. Resolve the remote tree by hand (commit, or a
# deliberate named stash you will restore) before pulling. Do not script around it.

# Run a targeted suite to a UNIQUE remote log (login shell for uv!). Use a per-issue/per-sha name so
# concurrent runs never clobber each other.
LOG=/tmp/verify-<issue>-<shortsha>.log
ssh -p 32099 frd_muziyao@210.77.77.22 "bash -lc 'cd /scratch/frd_muziyao/NWM && { uv run pytest -q <files> ; echo EXIT=\$? ; } > $LOG 2>&1'"
# ^ launch this with the Bash tool's run_in_background:true — it keeps running across turns and
#   re-invokes you on exit, so you do NOT hand-roll a poll loop. (Foreground `sleep` is blocked by the
#   Claude Code Bash tool; a `until ...; do sleep; done` poll will NOT run as written.) If you must
#   wait on remote-only state the harness can't observe, use Monitor with an until-condition, not sleep.
ssh -p 32099 frd_muziyao@210.77.77.22 "tail -20 $LOG"   # read result once the bg task notifies you

# ruff on remote (fast, foreground is fine)
ssh -p 32099 frd_muziyao@210.77.77.22 'bash -lc "cd /scratch/frd_muziyao/NWM && uv run ruff check ."'

# openspec validate — run LOCALLY (not on PATH remotely)
openspec validate <change-name> --strict --no-interactive

# Source prod env into a child process (compute.host.env vars have NO `export`) and query the real DB
# without psql (not on the node-22 PATH). Redact the password on the way out.
ssh -p 32099 frd_muziyao@210.77.77.22 'bash -lc "cd /scratch/frd_muziyao/NWM \
  && set -a && source infra/env/compute.host.env && set +a \
  && uv run python - <<PY
import os, psycopg2
c = psycopg2.connect(os.environ[\"DATABASE_URL\"]); cur = c.cursor()
cur.execute(\"SELECT run_id, status FROM hydro.hydro_run WHERE run_id LIKE %s\", (\"%<cycle>%\",))
print(cur.fetchall()); c.close()
PY"' 2>&1 | sed -E "s#://[^@]*@#://REDACTED@#g"
```

Gotchas: login shell required for `uv`; **`set -a` before sourcing `compute.host.env`** (its vars have
no `export`, else `uv run python` sees `KeyError: 'DATABASE_URL'`); `psql` not on PATH → query via
`uv run python` + `psycopg2`, redacting the DSN password; `s3://nhms` is a filesystem object store
(`OBJECT_STORE_ROOT`=`QHH_RUN_ROOT`), so run worker CLIs through the launcher/sbatch not by hand; pipe
long runs to a uniquely-named file not the SSH session; poll long runs via `run_in_background` + the
completion notification, never a foreground `sleep` loop; `openspec` local-only; the cfgrib/libeccodes
GLIBCXX line on node-22 is a warning, not a failure.

If node-22 is unreachable (SSH timeout / network down), the verification oracle is offline — do not
fall back to "tests pass" off local ruff alone. Report the outage, hold at the current phase, and let
GitHub CI's full suite stand in as the gate only at Phase G (it runs the same suite on its own runner).

---

## 2b. node-27 (display_readonly) recipes

node-27 is the display/frontend live-receipt oracle. User is **nwm** (not frd_muziyao), repo at
`/home/nwm/NWM`, port 32099. It has a read-only DB replica + published artifacts and **no**
Slurm/SHUD/Docker-socket/control-plane write — never run a Slurm/SHUD test here.

```bash
# Connect
ssh -p 32099 nwm@210.77.77.27

# Pull — same fail-loud --ff-only discipline as node-22 (never auto-stash-pop).
ssh -p 32099 nwm@210.77.77.27 'bash -lc "cd /home/nwm/NWM \
  && if [ -n \"\$(git status --porcelain)\" ]; then echo DIRTY_REMOTE_TREE; git status --short; exit 3; fi \
  && git pull --ff-only origin <branch> && git log --oneline -1"'

# A FAR-BEHIND node-27 ff can abort on untracked SAME-NAME files (master newly tracks a path that
# exists untracked locally): "error: untracked working tree files would be overwritten by merge".
#
# DESTRUCTIVE — reads `git clean`/`rm`. NEVER touch gitignored data/evidence dirs (artifacts/, .nhms-*,
# data/Basins/) — they are NOT blockers and must stay out of $BLOCKERS. First stash anything uniquely
# valuable (E2E receipts!): `git stash push -m presync-<date> -- <files>` and restore it AFTER, do not
# drop it. The recovery below is fail-CLOSED: it aborts (exit 4) unless EVERY blocker is byte-identical
# to the incoming master version, so a non-identical untracked file is never silently deleted.
#
# Fill BLOCKERS with EXACTLY the paths git listed in the abort message (they vary — do NOT hardcode).
# Run as a heredoc'd remote script (avoids ssh-quote escaping; `set -e` + explicit exit make it
# fail-CLOSED — any non-identical blocker aborts before a single delete).
ssh -p 32099 nwm@210.77.77.27 'bash -ls' <<'RECOVER'
set -euo pipefail
cd /home/nwm/NWM
BLOCKERS="<paths from the abort message, space-separated>"
# Byte-identical check that WORKS FOR UNTRACKED FILES: compare the working blob sha to the master blob
# sha (plain `git diff <commit> -- <f>` ignores untracked files and would falsely pass). rev-parse
# fails if the path is absent in master → treated as not-identical → abort.
for f in $BLOCKERS; do
  [ "$(git hash-object "$f")" = "$(git rev-parse "origin/master:$f" 2>/dev/null)" ] \
    || { echo "NOT_IDENTICAL: $f — resolve by hand, do NOT clean"; exit 4; }
done
BK="$HOME/NWM-presync-backup-$(date +%Y%m%d)"; mkdir -p "$BK"; cp -r $BLOCKERS "$BK"/
git clean -fd -- $BLOCKERS          # removes the confirmed-identical blockers (files + dirs)
git merge --ff-only origin/master && git log --oneline -1
RECOVER

# DEV-PHASE: start the display API LOCALLY on node-27 (NOT `docker compose up`). Same app entrypoint &
# role guards as the container (apps.api.main:app), bound to 127.0.0.1, no image build / no outward
# container. Source display.env for NHMS_SERVICE_ROLE=display_readonly + readonly DSN + published root
# (set -a: vars have no export). nohup + PID file so it survives the SSH turn; orchestrator does this.
ssh -p 32099 nwm@210.77.77.27 'bash -lc "cd /home/nwm/NWM \
  && set -a && source infra/env/display.env && set +a \
  && nohup uv run python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 \
       > /tmp/display-api-<issue>.log 2>&1 & echo \$! > /tmp/display-api-<issue>.pid \
  && for i in 1 2 3 4 5; do curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break || true; done \
  && tail -5 /tmp/display-api-<issue>.log"'
# Tear down when the receipt is captured: ssh … 'kill \$(cat /tmp/display-api-<issue>.pid)'

# C1 deploy receipt — prove the read-only boundary against the LOCAL-RUN service. `static` validates the
# production compose/env config WITHOUT bringing it up; runtime-config + /slurm probes hit the local API.
ssh -p 32099 nwm@210.77.77.27 'bash -lc "cd /home/nwm/NWM \
  && uv run python scripts/validate_two_node_docker_runtime.py static \
  && curl -fsS http://127.0.0.1:8000/api/v1/runtime/config | grep display_readonly \
  && curl -i -s http://127.0.0.1:8000/api/v1/slurm/health | head -1"'  # expect 404 — /slurm/* unavailable

# PRODUCTION DEPLOY (NOT dev-phase) — `docker compose up -d` starts a persistent outward-facing
# container; it is hard-to-reverse + state-changing → run ONLY with explicit human confirmation /
# standing pre-authorization (same governance as merge). `smoke` (image build) belongs here too.
#   ssh … 'bash -lc "cd /home/nwm/NWM \
#     && docker compose --env-file infra/env/display.env -f infra/compose.display.yml up -d \
#     && docker compose --env-file infra/env/display.env -f infra/compose.display.yml ps"'

# C2 read-only-DB denied-write — a SEPARATE module (not a validate_two_node subcommand). Source the
# readonly secrets (set -a, vars have no export) so NHMS_DISPLAY_READONLY_DATABASE_URL /
# NHMS_READONLY_DB_VALIDATION_DATABASE_URL reach the child; pass strict identity for the identity-bound
# routes. Exit 0=PASS, 2=BLOCKED (no real RO DB — NOT a mock PASS), 1=fail. Payload is auto-redacted.
ssh -p 32099 nwm@210.77.77.27 'bash -lc "cd /home/nwm/NWM \
  && set -a && source infra/env/display-readonly-secrets.env && set +a \
  && uv run python -m services.production_closure.readonly_db_validation \
       --source <src> --cycle-time <cycle> --strict-run-id <run_id> --model-id <model> --job-id <job>"' \
  2>&1 | sed -E "s#://[^@]*@#://REDACTED@#g"
```

Gotchas specific to node-27: the env files are `infra/env/display.env` (display app env, from
`display.example`) and `infra/env/display-readonly-secrets.env` (readonly DB validation secrets) — both
0600, not synced, prepared on 27. **Dev-phase iteration runs the app locally** (`uv run python -m
uvicorn apps.api.main:app`, 127.0.0.1) — fast and no outward container; `docker compose up -d` is the
**production deploy** only and is human-gated. The production two-node files are `compose.compute.yml` /
`compose.display.yml` (`docker-compose.dev.yml` does NOT count as 22/27 acceptance); `validate_two_node_
docker_runtime.py static` validates that compose/env config without bringing it up, so C1 closes in dev
without a deploy. The validation script's display forbidden set (`/etc/slurm`, `/run/munge`,
`/run/docker.sock`, …) is the authoritative boundary; the curl probes are supplementary. The DB DSN must
be the **read-only** account — a writable DSN invalidates the denied-write receipt. The in-node API
probe is `127.0.0.1:8000` (compose maps `…:8000:8000`; the local-run binds the same port) and readiness
is `GET /health`.
Published artifacts must already be copy-backed to 27's readable path (`/home/ghdc/nwm/published`) or C3
cross-plane is `BLOCKED` on that dependency, not a fail; `openspec`/`pnpm` are local-only as on node-22.
If node-27 is unreachable, the display oracle is offline — record `BLOCKED`, do not claim the live
receipt off local ruff.

---

## 3. Parallel cross-review dispatch

Dispatch all reviewer packs **in the same turn** (one message, multiple Agent calls) so they run
concurrently. Each pack prompt contains: the delegation guard (read-only variant), the diff range
(`git show <sha>` / `git diff <base>..<head>`), the governing invariant, its single lens, and the
finding contract (each candidate: severity | failure class | invariant | constructible scenario |
required test | sibling surfaces | self-tag CONFIRMED/PLAUSIBLE/REFUTED + file:line). Tell each
reviewer it is recall-biased — surface any nameable candidate; the verifier, not the reviewer, decides
REFUTED.

The 6 packs (for DB-backed / schema / shared-root changes):

1. **Spec compliance** — each spec scenario has implementation + evidence; no fixture bypasses a blocker scenario.
2. **Correctness** — the changed logic, numeric/units, boundaries, error paths.
3. **Integration & cross-file** — caller closure of a hardened contract; production-vs-fixture isolation; shared-fixture reuse ripple.
4. **Security & performance** — path/filename safety, resource bounds enforced pre-write, evidence redaction, memory/streaming.
5. **Test & evidence coverage** — invariant coverage across surfaces; did fixtures hide a should-fail scenario; Evidence-Floor fields asserted.
6. **Invariant / state-machine / compatibility** — identity binding closure; idempotency/stale-state; "never break userspace" for behavior changes (units, roles, defaults).

---

## 4. Phase 4.5 verification gate

After the panel returns: dedup near-duplicate candidates across packs. For each surviving candidate,
dispatch one verifier (verifier ≠ originating reviewer) that returns exactly one of:

- **CONFIRMED** — the failing scenario is constructible from the diff/fixture/contracts. Cite the evidence.
- **PLAUSIBLE** — reachable but not fully constructible (rare error path, falsy-zero, off-by-one at an
  unexcluded boundary, stale row). Default here for realistic runtime states.
- **REFUTED** — not reachable; record a one-line rationale and drop it.

When two packs disagree (one says BLOCKING, another says fine), the orchestrator verifies the decisive
fact with code reads and records the resolved verdict. Out-of-scope CONFIRMED findings go to §Phase F
as issues. Bias: high-risk fixtures keep PLAUSIBLE merge-blocking; low-risk block only on CONFIRMED.

---

## 5. Issue worklog template

Maintain at `openspec/changes/<milestone>/issue-<N>-worklog.md`:

```markdown
# Issue #<N> 动态动作流 + 进度工作日志

## 1. 目标与边界            (issue, PR, in/out of scope)
## 2. 角色分工              (orchestrator / fix subagent / review subagent / node-22 / node-27 / CI + 用户决策)
## 3. 状态机入口            (table: phase | 状态 ✅/⏳/⬜ | 说明)
## 4. 待修复清单            (fix groups + CI 回归, grouped by failure class, checkboxes)
## 5. 动态循环规则          (ordinary-loop gates inherited from codex-codeagent-workflow)
## 6. 验证门 (Evidence Floor) (远端命令 + 必产证据字段)
## 7. 进度日志 (倒序)        (table: 时间 | 阶段 | 动作 | 结果)
```

Update §7 as each step completes; flip §3 statuses; record every Phase 4.5 verdict and every
scope decision (with evidence) so a resumed session can continue without re-deriving them.

---

## 6. Worked example — issue #256 / PR #265

State on entry: PR open, implementation done, round-1 review done (11 candidates), Phase-5 fix plan
written, CI red. Dynamic entry = Phase B.

1. **Assess + baseline.** Classified the 13 CI failures as two same-root-cause classes (new producer
   requires `forcing_grid` stations; downstream fixtures only seeded `forcing_proxy`). Remote baseline:
   the 3 Evidence-Floor files = 576 passed → regressions were all in downstream files.
2. **Fix lane 1 (core).** One fix subagent hardened the producer (Phase-5 G1-G4); found the real bug —
   ERA5 `mm/day` was accepted but never converted. Settled the station contract (dynamic N≥1, contiguous
   `shud_forcing_index` 1..N, safe unique filename). Verified, committed.
3. **Fix lane 2 (fixtures).** A second fix subagent, given the settled contract, seeded `forcing_grid`
   stations across the 13 downstream tests + the met-validation deterministic fixture. Verified, committed.
4. **Remote verify.** Targeted 8-file suite on node-22 = 624 passed / 0 failed. CI full-suite job
   flipped red→green.
5. **Cross-review.** 6-pack parallel panel → 0 merge-blocking. The invariant pack raised a BLOCKING
   candidate (seed/migration still `forcing_proxy`); Phase 4.5 verification downgraded it (production
   uses the bootstrap which writes `forcing_grid`; the cited spots are smoke/demo/column-default).
6. **Synthesis.** Two convergent low-cost guard tests added (mm/s rejection, qc[units]) in-scope. One
   pre-existing out-of-scope finding (IFS precip unit vs SHUD `mm/day` contract) → OpenSpec change
   `forcing-prcp-unit-reconciliation` + GitHub issue #266 + spec PR #267.
7. **Gate.** Chinese work-summary posted; awaiting CI green for pre-authorized auto-merge.

Lesson encoded into the rules: drive iteration off the remote real-DB result, treat CI as the merge
gate, dispatch reviews as a parallel panel + verifier gate, and turn out-of-scope findings into
issues instead of widening the PR.
