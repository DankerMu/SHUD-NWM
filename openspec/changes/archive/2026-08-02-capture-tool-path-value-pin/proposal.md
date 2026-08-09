# Pin capture argv tool-path values on the verifier side (#1261)

## Why

After #1259/PR #1260 the verifier pins WHO runs (argv[1] producer
identity, kind + mutation-sha bindings, abbreviation-proof), but not
WITH WHAT: the values of `--psql --systemctl --docker --journalctl
--git --repo --container --database` in a run-plan capture argv pass
through `_concrete_argv` with zero constraint (measured: the four
capture gates at live_evidence.py:1050-1113 have no fifth gate). A
hostile plan can keep a perfectly anchored argv[0:4] and point the
committed producer at stub binaries, fabricating all twelve
snapshots without any seam token — while the COMMAND side has had
`expected_executable` literal pins since the G-series
(live_evidence.py:651-661). capture.py's only producer-side guard is
the docker half-guard (capture.py:511-513), scoped to ONE kind
(`_capture_schema_dump_list`); `_container_state` (:274) and all
other tool sites are unguarded. Production reality (runbook
tier-node27 :1034-1038): the authored plan passes only
`--mutation-head-sha`/`--output`, so every pinned value below is
exactly the plan_author module default.

## What Changes

All facts explorer-measured at master 3851185f:

1. **Verifier tool-value map** (live_evidence.py): new module
   constant `EXPECTED_CAPTURE_TOOL_VALUES` — a mapping, restated
   literals (NOT imported from plan_author/supervisor: the verifier
   keeps its independent non-derived-oracle posture, same as the
   existing `"nhms-db"`/`"nhms"` inline literals):
   `--psql: /usr/bin/psql`, `--systemctl: /usr/bin/systemctl`,
   `--docker: /usr/bin/docker`, `--journalctl: /usr/bin/journalctl`,
   `--git: /usr/bin/git`, `--repo: EXPECTED_REPO_PATH`,
   `--container: "nhms-db"` (values match plan_author defaults
   :99-103, DEFAULT_REPO :36, DEFAULT_CONTAINER :47; a drift-guard
   test binds the two modules). Fifth capture gate, after the
   mutation-sha gate (:1077-1081): for each mapped option,
   `_argv_option_values(capture_argv, option) == [expected]` —
   presence + exactly-once + value equality in one check (absent,
   duplicated, dangling and mismatched all refuse; EvidenceError
   names the option, the offending values and the expected value).
   Additionally `--database` is bound dynamically:
   `_argv_option_values(capture_argv, "--database") ==
   [plan database]` (the field the verifier already validates at
   :981 — reuse, do not re-derive).
2. **Abbreviation closure over the pinned set** (the #1259 round-1
   lesson applied at fixture time, not discovered in review): the
   per-token proper-prefix rejection (currently over
   `ANCHORED_CAPTURE_OPTIONS`, :1102-1113) widens to every pinned
   option — reject any token whose `base = token.split("=", 1)[0]`
   satisfies `len(base) >= 3 and base != option and
   option.startswith(base)` for any option in the anchored set,
   the tool-value map, or `--database`. Without this, a later
   `--ps /tmp/stub` rebinds `--psql` last-wins past the exactly-once
   full-name check — the exact bypass class verifier-CONFIRMED in
   PR #1260 round 1. Measured zero collision: no registered capture
   flag is a proper prefix of another pinned option; plan_author
   emits full flags only; seam tokens (`--self-test-*`) are proper
   prefixes of nothing pinned. Ambiguous bases (e.g. `--d` for
   --database/--docker) are rejected the same way (argparse would
   refuse them anyway; rejecting is strictly safe).
3. **Deliberately unpinned, recorded**: `--evidence-dir` (run-scoped
   path, varies per run even in production) and
   `--schema-dump-host`/`--schema-dump-container` (data-file paths,
   legitimately parameterized — the e2e overrides them on plan_prod;
   the pg_dump/docker COMMAND identities that consume them are
   already exact-pinned on the command side). Their values stay
   unconstrained and their presence is not required — the gate
   iterates the pinned map, it does not assert parser-viability of
   the whole argv.
4. **Supervisor: zero diff** — recorded reasoning, not an oversight:
   the executor legitimately runs hermetic plans whose tool paths
   are test stubs (capture tests :651-655, e2e), so it cannot pin
   values; and it needs no abbreviation extension either, because
   the forensic claim is verifier-owned — an argv carrying
   `--ps /tmp/stub` is refused by the VERIFIER's widened scan
   regardless of what the executor would do with it. The #1250/#1259
   executor/verifier asymmetry stands unchanged.
5. **Test surface**:
   - Two argv template helpers extend, non-identically: `_bundle()`
     (tests :1154-1161) keeps its baked `--mutation-head-sha` and
     gains the full pinned option set with production values
     (`--database` from the bundle's database, the five
     `/usr/bin/*` tools, `--repo` = EXPECTED_REPO_PATH,
     `--container nhms-db`); `_producer_argv` (tests :5260-5263)
     gains the same pinned options but its `--mutation-head-sha`
     STAYS caller-supplied via `*extra` — the `[pair_missing]`
     negative (:5318) needs a template with no SHA binding.
     One-point change per helper; no consuming test body changes.
   - **e2e restructure** (the one gap the issue body missed,
     explorer-measured): `plan_prod`'s `build_run_plan` call
     (:4989-5001) currently passes stub `capture_psql/…/capture_git`
     (from `capture_bin`, :4927) and `capture_repo=fixture_repo`,
     and THAT plan is what reaches the real `verify_bundle` (:5108,
     :5121) — a value pin breaks it as structured. Fix mirrors the
     #1259 argv[1] split: plan_prod drops the five `capture_*` tool
     kwargs and `capture_repo` (captures carry production
     defaults), and the existing plan_prod cleanup `--repo` rewrite
     block (:5002-5010) is DELETED; `plan_exec` (deepcopy,
     :5018-5043) rewrites — in addition to the existing argv[1]
     swap — the five tool option VALUES to the stub paths and the
     `--repo` value PER KIND: `cleanup` → `str(ROOT)` (its
     verifier checks repo_units paths against the canonical
     checkout), the other eleven → `str(fixture_repo)`; new
     fidelity pins assert the executed argvs really carry the stub
     paths before the ledger rewrite (a no-op rewrite would
     silently execute real host binaries). The existing capture
     ledger rewrite already maps executed argv back to plan_prod
     argv by capture_id; no new machinery. `schema_dump_*`
     overrides stay on plan_prod (unpinned).
     `capture_python=sys.executable` stays (argv[0] unpinned).
6. **Tests** (append-only beyond the three mechanical updates):
   per-option mismatch/missing/duplicate rejection over the pinned
   map, `--database` mismatch rejection, abbreviation-rebind
   rejection (`--ps`, `--do`, `--rep=` shapes), structural
   zero-collision + plan_author drift-guard tests, positive control
   that a default `plan_author.build_run_plan()` capture argv
   passes the full gate stack, plus green confirmation of all
   #1250/#1259 tests and the e2e PASS.

Spec relation: extends the same `hypertable-compression` capability
requirement family (#1250 seam visibility → #1259 producer identity
→ this: producer TOOLING identity). Closes the residual that capped
PR #1260's round-1 finding at P2.

## Non-goals

- capture.py changes (`allow_abbrev=False`, generalizing the docker
  half-guard to all tools — producer-side hardening is #1261's
  recorded alternative 2, out of scope here).
- plan_author.py, bundle_author.py, supervisor.py, schemas/**,
  tests/test_node27_timeseries_compression_capture.py,
  tests/test_node27_timeseries_compression_supervisor.py: zero diff.
- Pinning `--evidence-dir` / `--schema-dump-*` values (recorded in
  What Changes 3).
- Full per-kind option-layout pinning (the #1250-named brittleness,
  still rejected).
- node-27 live verification (hermetic pytest oracle only — the pin
  is a records gate, not a runtime behavior change).
