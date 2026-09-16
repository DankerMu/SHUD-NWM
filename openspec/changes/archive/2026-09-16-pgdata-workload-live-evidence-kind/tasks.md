# Tasks: pgdata-workload-live-evidence-kind

## 1. CLI evidence kind and live admission

- [x] 1.1 Add `--evidence-kind {isolated,live}` to the `measure` parser (`scripts/node27_pgdata_workload.py:42-57`)
  with default `isolated`, and pass it through at the `measure_workload` call site (`:101-110`) so no literal
  `evidence_kind="isolated"` remains in the script. Invalid values are rejected by argparse through the existing
  static `QUERY_USAGE` error (no user input echoed).

- [x] 1.2 Gate the `live` kind: before any measurement work and before publication, require
  (a) the existing read-only session proof (`prove_readonly_session`,
  `packages/common/node27_pgdata_workload_io.py:199-219`) and (b) `--reviewed-sha` bound to the executing
  checkout's HEAD. Failure is a typed fail-closed refusal with no file created at `--output`.

  Note on (a): `prove_readonly_session` already runs unconditionally at `scripts/node27_pgdata_workload.py:100`, so
  for the `live` path it is pre-existing behaviour and task 2.3 is regression coverage. The genuinely new admission
  is (b), covered only by 2.4 — treat that as the load-bearing case.

  The SHA-binding policy is decided here, not by the implementer. Follow the in-repo precedent
  `scripts/node27_timeseries_compression_live_evidence.py:4155-4182` (`_current_verifier_head`) and
  `packages/common/node27_pgdata_host.py:717-719`:
  - **Anchor**: the repository root of the executing script itself (a module-level constant derived from
    `Path(__file__).resolve().parents[...]`), never the process cwd. The anchor is only believed when it is
    *identical* to the repository git answers for: resolve `rev-parse --show-toplevel` alongside HEAD and refuse
    unless it is the same directory, and run git with the redirecting `GIT_*` variables (`GIT_DIR`,
    `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`) removed. Otherwise a deployed tree with no `.git` of its own nested inside
    another checkout, or an inherited `GIT_DIR`, hands back the *surrounding* repository's HEAD and reports clean
    (the nested tree is merely untracked there).
  - **Module anchor**: the entrypoint's own path binds nothing by itself. On the `live` path only, before
    resolving HEAD, refuse unless the modules that do the work (`packages.common.node27_pgdata_workload`, `_io`,
    `_query`, `_plan`, `_measure`, `_http`, `_types`) resolve under the anchor. The documented #1987 runtime is
    exactly the split form — published script bytes under `~/.local/state/issue1987-tools/<commit>/` run with
    `PYTHONPATH=/home/nwm/NWM` (runbook §4.10.6) — so without this the receipt can attribute its samples to a
    checkout that did not produce them.
  - **Dirty tree**: `git diff --quiet HEAD --` over *tracked* files only, i.e. untracked files do not refuse. A
    whole-tree-clean rule would refuse on node-27, whose checkout routinely carries untracked evidence
    directories. A tracked-file modification refuses: the receipt would otherwise claim a SHA that is not the code
    that ran.
  - **Non-git / git unavailable / non-40-hex or oversized output / subprocess timeout**: refuse. Never fall back to
    the supplied value.
  - **Refusal codes** (part of the public refusal surface via `format_refusal`, and consistent with the existing
    `INPUT_*` / `SQL_*` vocabulary in `packages/common/node27_pgdata_workload_io.py`):
    `INPUT_SHA_UNBOUND` when a well-formed `--reviewed-sha` is not the executing HEAD or the tree is dirty,
    `INPUT_SHA_HEAD_UNAVAILABLE` when HEAD cannot be determined at all (including when the anchor is not itself
    the repository root git answered for), and `INPUT_RUNTIME_UNBOUND` when the working modules do not resolve
    under the anchor. The last is its own code rather than a reuse: HEAD *is* determinable there, so
    `INPUT_SHA_HEAD_UNAVAILABLE` would send an operator to debug `git` when the defect is the runtime layout.
    Keep `INPUT_SHA_INVALID` for the existing shape check.
  - **Hermetic seam**: resolve HEAD through an injectable dependency in the existing `main(argv, **injected)` /
    `measure(args, *, connect=..., opener=...)` style (e.g. a `head_resolver` or `repo_root` keyword), so tests
    2.2 and 2.4 do not depend on the ambient checkout state. Add a guard test that the default resolver is the
    real checkout root, per `tests/test_node27_timeseries_compression_live_evidence.py:1495-1537`.

- [x] 1.3 Runbook: in the section that owns this CLI (`docs/runbooks/tier-node27-timeseries-storage.md`
  ~`:698-733`), document how a `live` receipt is obtained (the flag, the two admission conditions, and that an
  isolated receipt still never implies live acceptance). Keep the existing invocation template accurate.

## 2. Tests (requirement-driven; each maps to design.md "Required evidence")

- [x] 2.1 Default: `main(["measure", ...])` with no `--evidence-kind` publishes a receipt whose
  `evidence_kind`/`isolated`/`live` are `"isolated"`/`true`/`false`. Existing assertions
  (`tests/test_node27_pgdata_workload.py:810-812`, `tests/test_node27_pgdata_workload_io.py:114`) stay green.
- [x] 2.2 Live success: with an injected read-only `nhms_display_ro` session and an injected head resolver
  returning the same value as `--reviewed-sha`, the published receipt is `"live"`/`false`/`true`. Compare against
  an isolated run over the same frozen inputs **and the same `--reviewed-sha`**: only the kind triple and
  `measured_at` may differ.
- [x] 2.3 Live refusal — session not read-only, and separately session `current_user` not `nhms_display_ro`:
  refusal codes `SQL_NOT_READONLY` / `DSN_ROLE_INVALID`, non-zero exit, and `--output` does not exist afterwards.
  (Regression coverage: this proof already runs today on every path.)
- [x] 2.4 Live refusal — well-formed `--reviewed-sha` that is not the resolved HEAD, and separately a dirty
  tracked tree: refusal code `INPUT_SHA_UNBOUND`, non-zero exit, `--output` does not exist afterwards.
- [x] 2.4b Live refusal — HEAD cannot be determined (resolver raises / non-git / timeout): refusal code
  `INPUT_SHA_HEAD_UNAVAILABLE`, non-zero exit, no file at `--output`. Plus the guard test that the default
  resolver is anchored at the executing script's repository root rather than the process cwd.
- [x] 2.4c Anchor identity — `resolve_repository_head` refuses `INPUT_SHA_HEAD_UNAVAILABLE` for a plain directory
  nested inside an outer checkout and for a redirecting `GIT_DIR`/`GIT_WORK_TREE`, instead of returning the
  surrounding/redirected HEAD; and the `live` CLI path refuses `INPUT_RUNTIME_UNBOUND` with no file at `--output`
  when the working modules resolve outside the anchor, before the head resolver is consulted.
- [x] 2.5 `--evidence-kind rehearsal` is rejected by the parser; do not restate the library-level
  `INPUT_KIND_INVALID` case already covered at `tests/test_node27_pgdata_workload.py:904-905`.

## 3. Risk packs

- Public API / CLI / script entry — **selected**: a new flag on the `measure` subcommand. Covered by 2.1, 2.2, 2.5
  and by 1.1's default-preserving constraint.
- Auth / permissions / secrets — **selected**: `live` asserts a credential and session property. Covered by 2.3
  (both refusal branches) and design review focus 4 and 5; the DSN stays in a mode-0600 file, never argv.
- File IO / path safety / overwrite — **selected**: a refused live run must leave no file, and publication keeps
  staged-sibling / 0600 / no-clobber. Covered by 2.3 and 2.4 asserting the absence of `--output`, and by the
  existing publication tests staying green.
- Error handling / rollback / partial outputs — **selected**: same as above, plus typed refusal codes. Covered by
  2.3 and 2.4.
- Documentation / migration notes — **selected**: operators must know how a live receipt is obtained. Covered by
  1.3.
- Config / project setup — not selected: no configuration file or environment contract changes.
- Schema / columns / units / field names — not selected: the receipt gains no field; the existing kind triple is
  populated differently.
- Concurrency / shared state / ordering — not selected: a single operator-invoked process.
- Resource limits / large input / discovery — not selected: sample counts and thresholds are unchanged.
- Legacy compatibility / examples — not selected: the default path is preserved literally; the one archived
  receipt (`openspec/changes/compressed-chunk-cold-tablespace-tiering/evidence/receipts/retirement-pgdata-workload-smoke.json`)
  stays valid as an isolated receipt.
- Release / packaging / dependency compatibility — **selected**: the `live` path makes a working `git` binary a
  runtime dependency of the CLI. Covered by 2.4b's HEAD-unavailable refusal (missing or failing `git` refuses
  rather than degrading) and by 1.2's fail-closed rule; the `isolated` path gains no new dependency.

## 4. Change-level verification floor

- [x] 4.1 `openspec validate pgdata-workload-live-evidence-kind --strict --no-interactive` PASS.
- [x] 4.2 `uv run ruff check .` PASS.
- [x] 4.3 `uv run pytest -q tests/test_node27_pgdata_workload.py tests/test_node27_pgdata_workload_io.py
  tests/test_node27_pgdata_workload_plan.py` PASS, including the new cases.
- [x] 4.4 Markdown lint on the changed runbook section, per the repo's existing docs lint.
- [ ] 4.5 node-27 live smoke (issue #2410 `Verification:`), read-only, DSN only via a mode-0600 private file:
  one `--evidence-kind live` run producing a receipt with `evidence_kind: "live"` / `isolated: false` /
  `live: true` and its `status`, and one no-flag run on the same inputs producing the isolated triple. For both,
  record `stat -c '%a' <output>` = `600`. Per `CLAUDE.md` the real-DB oracle is node-27; §4.1-4.4 are local-only.

  **ATTEMPTED, BLOCKED UPSTREAM — deliberately left unchecked.** node-27 2026-09-16T13:11Z, detached worktree
  `/home/nwm/tmp/2410-wt` at PR head `652b520739db0785549b126509de45bb472cab9b`, role `nhms_display_ro`, DSN via
  a mode-0600 private file (never in argv). Pin: basin `basins_wj_vbasins`, network `basins_wj_rivnet_vbasins`,
  segment `basins_wj_shud_reach_000001`, run `fcst_ifs_2026091412_dg_a8116c66fb52fe0b47c86034e7d46b02`, model
  `dg_a8116c66fb52fe0b47c86034e7d46b02`, IFS, cycle 2026-09-14T12:00:00Z.

  Both runs refused identically and wrote nothing:

  ```text
  == isolated (no flag)   rc=1   PLAN_BUFFERS_EXCEEDED: shared buffers exceed the 5000 ceiling
  == live                 rc=1   PLAN_BUFFERS_EXCEEDED: shared buffers exceed the 5000 ceiling
  isolated MISSING
  live MISSING
  ```

  The refusal is downstream of every admission this change adds. `measure()`
  (`scripts/node27_pgdata_workload.py:113-125`) runs `require_runtime_anchored` and `bind_reviewed_sha` before
  any database work, and `prove_readonly_session` before `measure_workload`. None of `INPUT_RUNTIME_UNBOUND`,
  `INPUT_SHA_HEAD_UNAVAILABLE`, `INPUT_SHA_UNBOUND`, `SQL_NOT_READONLY` or `DSN_ROLE_INVALID` fired, so on
  production the live path passed runtime anchoring, reviewed-SHA binding and the read-only session proof and
  reached the measurement stage. The receipt bytes are unchanged publication code, covered by 2.2.

  No pin can satisfy this today. `PLAN_BUFFER_LIMIT = 5000`
  (`packages/common/node27_pgdata_workload_plan.py:27`, enforced `:520`) has no CLI knob — it *is* the D11
  gate — and the shipped forecast-series query measures 18621 shared hits warm for 168 rows, 13608 of them
  (73%) from two un-memoized per-row `hydro_run` index lookups inside the UNION branches of
  `packages/common/forecast_store.py:28-53`. Every active basin carries 75 forecast runs, so every honest pin
  lands at ~2350 rows; an earlier `basins_shj_nj_vbasins` pin measured 18626. That is a #1987 task 5.2 /
  #1988 finding about the API query, filed separately and out of scope here.

  A live receipt for this CLI is therefore gated on a D11-passing query, not on this change.

- [ ] 4.6 Not part of this change: producing #1987 task 5.2's D11 curve receipts. The follow-up run belongs to
  #1987 and needs pins re-derived live per the runbook.
