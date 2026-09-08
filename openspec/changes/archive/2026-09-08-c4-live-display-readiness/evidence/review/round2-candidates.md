# PR #2126 round 2 reports and candidate

Reviewed head SHA: `391b6590e4160aaccad278e7dcd8591a2f576444`。Actual code fix SHA `7a4ada4c694c0c4d7583abb28cd10c37dcd2b260`; later commit only adds exact-SHA evidence.

Seats actually run (within post-fix cap 3): correctness full-PR; test-evidence+spec-compliance delta-focused; security-perf+integration delta-focused. All read-only; no tests/CI/live rerun.

## correctness full-PR — a1374e044b0fdfb89

All four round-1 confirmed findings verified closed. Full base..HEAD scan of lane, preflight, DOM, request matching, receipt/schema, owner/terminal, private publisher/river wrapper, binder/CLI, RBAC/store, CI/spec/tests found no new actionable candidate. Supplied evidence gaps retained: new-tip CI pending; no node-27/live; affected-row verification supplied, not rerun.

## security-perf+integration delta — adf2fc4ad88988540

No actionable candidate. Confirmed raw false flag preserved while safe control fields remain forced closed; real fetch path denies viewer. Monitoring/fetchAll remain fail-closed; overview may issue one read-only queue GET only for a contradictory pair current API cannot emit, not an authorization bypass. Binder/lane tests hit shipping gates; spec classification matches deliberate behavior.

## test-evidence+spec-compliance delta — a84a2174e97ddbeb5

All four round-1 findings verified closed. Static test counts corroborate affected set 82. One candidate:

C4-R2-1 — proposed P2 / contract. New spec has one scenario “frontend/API origin or receipt path missing → BLOCKED” and another “basin/segment pin missing/blank/invalid → FAIL CONFIG_INVALID” but does not state precedence when both predicates hold. Implementation checks URL before pin and owner checks receipt path before config, so combined missing path/URL+bad pin yields BLOCKED; scenario 2 read alone could predict FAIL. Both refuse PASS; no security bypass. Suggested minimal disposition if real: add valid URL/receipt precondition to pin scenario or explicit BLOCKED precedence; mirror design row. Existing config/owner tests already establish individual branches, no new code test required.

Non-candidates: exact error precedence under duplicate POSIX gates, implementer 87-vs-orchestrator 82 test count already corrected, contradictory overview read-only GET already disclosed. Mutant logs ignored and not independently replayed; evidence states this honestly.

## Candidate status

C4-R2-1 is not yet independently verified. Do not call round 2 clean or fix it before verdict. If it survives as P2-only, workflow default is routed deferral with a recorded reason/issue and round clean; no P1/coverage candidate currently buys another fix/re-review round.
