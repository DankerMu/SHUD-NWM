## 1. Historical-ledger admission compatibility

- [x] 1.1 Update prepare and migration-worker admission together, preserve all historical ledger entries through
      expand/D12, and prove the contract using the existing window oracle plus the captured live pending-set input.

Evidence: `receipts/child2324-verification.json` records all eight node-27 isolated scenarios PASS on tool commit
`fe4c308977da7b013e621772cac9ddbc54691940`, including both original-f24 refusals, real changed-worker expand,
phase-dependent D12 ledger retention, fourteen CLI cases and four late-prepare cases. Production is not switched.

Scope: this change's `tools/window_execute.py` and `tools/window_smoke.py`, adopted from immutable f24 `.py.txt`
receipts with hashes and semantic delta. Historical receipts stay unchanged. No runtime re-pin or governance change in
this child.

Suggested fixture level: expanded — migration compatibility, public worker/prepare entrypoints and rollback; repair
intensity high.

Minimal mergeable slice: atomic: both admission consumers and their window oracle implement one rule; fixing prepare
alone would defer the same refusal until the migration worker after fencing.

Evidence Floor:

- Seed the seven named retired versions from design D1 into the disposable ledger only, with no SQL files. Both actual
  prepare and migration-worker admission must read that real catalog; the same scenario is f24-red/child-green at each
  site.
- Expand changes original ledger L to L plus 000059. D12 MUST retain L plus 000059, all historical rows, real legacy
  facts and narrow rollback provenance. Failure before migration commits retains L. No ledger DELETE is added.
- Zero pending and an additional pending current migration refuse without migration side effects. Both prepare and the
  migration-worker boundary are covered, not merely a helper return value.
- Existing actual late-prepare reload, fourteen CLI cases and eight recovery scenarios remain valid. External
  system/process boundaries may be simulated; SQL, ledger, OIDs and reader/parser behavior remain real.
- Consume the captured node-27 inventory as the fixture input and map it to the migration-owner contract. Do not repeat
  the production query, execute a production migration or edit its ledger.

Verification: publish exact bytes through GitHub, then run the extended `window_smoke.py --case happy` covering actual
prepare and worker, plus retained stop/session/fence/do-before-ledger/rename/source/restart cases. Use the original
guarded node-27 disposable launcher with fresh DB/state per case; record concrete argv/hashes without DSNs.

Hygiene: `uv run ruff check` on the extracted Python artifacts;
`openspec validate refresh-node27-window-admission --strict --no-interactive`. No backend pytest against the primary or
local Mac.

## 2. Frozen runtime and owned governance handoff

- [x] 2.1 Bind and qualify target `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2` across window execution and owned
      governance handoff, preserving the existing staged ownership state and proving fresh pre-T0 readiness.

Current qualification status (2026-09-14): **PASS, pre-T0 only**. Tool commit
`b55c7ef9b43eceeae439915629383dfaf0b36a3e` passed the frozen-415 eight-case window matrix; source-identical governance
tools have ten same-tool and eight original-state handoff cases. Actual 415 read-only audit returned no critical
recommendations, and the retained exact selector returned the required 168 points/digest.

Historical fresh prepare refused `REQUIRED_UNIT_NOT_ADMITTED`; its state remains preserved. Separately authorized
maintenance recovery #2349 completed and merged in PR2367: river107 compressed, original maintenance services
genuinely successful, both timers restored. A new hash-bound prepare with byte-identical tool publication92a5b4350992
passed at17:35:34Z, state `issue2325-window-92a5b4350992-auth-refresh2-prepare`, phase PREPARED.
See `receipts/child2325-post-recovery-prepare.json`. Original staged identity/pin and active a8db remain unchanged;
T0/window/recover/unstage were not executed. This completes child admission, not parent deployment.

Operational deviations: the launcher uses the existing verified replay.env nhms credential instead of the stale
container initialization password; no DB password or formal env was changed. An earlier corrected-auth prepare hit
a baseline tile timeout; unchanged route probes returned200 and the new-state retry passed without weakening checks.

Depends on task 1.1. Scope: the four executables in this change's `tools/`, their target/qualification evidence and
fixture; adopt the two governance tools from f24 with provenance. No application/library changes or historical receipt
edits.

Suggested fixture level: expanded — immutable release binding and cross-tool persisted ownership; repair intensity high.

Minimal mergeable slice: atomic: window target and governance unstage target must agree; either half alone leaves the
declared deployment unable to complete its owned handoff.

Width exception: multi-path - the window and governance oracles jointly prove one cross-tool handoff. Releasing just one
target binding is unusable; their isolated paths plus a read-only live qualification are evidence for that same
operation, not separate features.

Evidence Floor:

- Record the complete consumed-interface table from design D4 against immutable git blobs. Preserve unchanged baseline
  evidence and explicitly qualify changed imports/recommendations; do not infer compatibility from file counts.
- Update every required target literal, including the governance embedded import probe, plus fixed restore refs, target
  checkout and admission/driver hashes. Preserve OLD, container/PGDATA identity and the existing governance argument
  surface/template.
- Original hash-verified f24 code creates staged state at retained runtime 1a32; the new tool consumes it with identical
  ownership arguments. Independently bind OLD, retained 1a32 and active 415 roots. No single-NEW Git fake or
  unconditional import success.
- Prove pre-cutover, active-runner and foreign-drift refusal; interruption after owned pin removal; post-removal audit
  failure and recovery. Completion audits the unpinned new runtime before restoring timer activity. Never rewrite
  identity/template/digests.
- Preserve the complete window matrix from child 1 under the final-target runtime. Do not rerun unchanged large-write
  timing merely to relabel it as new evidence.
- On node-27, run final-target governance code as a read-only audit using the existing private environment and actual
  primary binding. Any critical recommendation blocks; never filter `AUTOVACUUM_OUTPUT_STALLED` or restore retired cold
  options.
- Re-prove `receipts/small-selector-oracle.json` on retained isolated real data using final-target code: 168 points,
  response SHA-256 `84a95266f402a20d9f26834bba5f16972266f1075e6859798eea37b82738b483`. Preserve its exact request and
  scientific/source identities.
- Run actual fresh prepare with final-target inputs, no T0/window/recover or production pin removal. A refusal is not
  readiness; preserve its diagnostics and resolve only in-scope admission defects through the workflow.

Verification: run the extended original-state/new-active governance smoke and target-bound window oracle on node-27,
then actual read-only audit/selector and fresh prepare. Record concrete commands, hashes, old-state identity and limits.
Same-tool stage/unstage alone is insufficient; production unstage remains parent work.

Hygiene: targeted Ruff on extracted tool/oracle Python; strict OpenSpec validation. No node-22 commands, service
installation, shared permission changes or retention execution.

For both children, design D5 governs immutable bundle publication/launch and the explicit CI gap. Do not archive this
shared change before parent window/handoff ends; copied hash-bound bundles are the runtime paths, not a moving OpenSpec
checkout.

## 3. Parent execution handoff (not a child implementation task)

The user conditionally authorized a later window only after the new admission, history preservation, target
qualification and rollback/handoff proofs pass. Parent #1987 retains T0, actual 000059 activation, immediate owned
governance unstage and full task 5.2 evidence; #2280 retains real uncached four-route recovery validation.

No child marks those parent items complete. Retention is already genuinely successful; its requested one-time read-only
jq check passed. Do not run it again to manufacture evidence. Node-22 producer persistence and gap sweeps remain
separate authority.

## 4. Stable unit configuration comparison (#2370)

- [x] 4.1 Replace volatile show-string comparisons with typed complete stable command/calendar semantics; normal
      service reruns and next-elapse changes pass.
- [x] 4.2 Preserve true command/calendar/env/file/path/timeout drift refusal and malformed-data fail-closed behavior.
- [x] 4.3 Refuse prior snapshot format before writes, require fresh state, and preserve source/config/driver/ledger
      and ownership guards.
- [x] 4.4 Prove focused original-red/changed-green isolated admission and real node27 readonly semantic comparison;
      run fresh prepare, without child T0 (1b8b2b5b final tools, focused PASS, 8/8 matrix PASS, PREPARED20:01:20Z;
      `receipts/child2370-verification.json`).
- [x] 4.5 Confirm governance tool hashes unchanged, complete expanded review/CI and publish immutable parent handoff
      (PR2372 three seats: no findings; code1b8b2b5b/hash70598ef0; CI34890555569 PASS; shared change stays active).

Evidence Floor: actual typed payload and drift records from the failed parent attempt; focused real-comparator smoke
with volatile success plus path/argv/ignore_errors/calendar and existing config/file refusal; malformed signatures/rows;
old-state bytes unchanged; actual window admission before a deliberately stopped isolated mutation boundary; fresh
node27 prepare. Reuse existing ledger/governance matrices where bytes are unchanged. Local targeted Ruff and strict
OpenSpec; runtime smoke on node27 with TMPDIR=/home/nwm/tmp. No live unit changes or second production window here.

## 5. Bounded display readiness (#2373)

- [ ] 5.1 Wait for actual local health200 after one service start, with bounded transient retries and terminal failure
      refusal; preserve source/proxy checks before basic_ready.
- [ ] 5.2 Bound attempts/status/sleeps by startup and applicable forward deadlines; preserve bounded late recovery.
- [ ] 5.3 Prove old-red/changed-green delayed listener, never-ready, failed-unit/permanent-error and both startup paths,
      plus the retained eight-case isolated window matrix; no production restart.
- [ ] 5.4 Complete focused review/CI and publish immutable source/evidence for parent recovery and re-forward work.

Evidence Floor: real recorded ConnectionRefused failure in both startup paths and separate manual OLD restoration;
focused actual HTTP listener oracle plus deterministic deadline failures, source/public gating and no timer release
on failure. Local Ruff/strict OpenSpec; node27 isolated runtime verification with TMPDIR=/home/nwm/tmp. Production
remains OLD with retained narrow rollback and ledger; #2374 owns new re-forward admission, not this child.
