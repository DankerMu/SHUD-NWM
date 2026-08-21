# Name the truth source of the five compression `*_invocation` slots

Issue: #1398 (origin: PR #1396 / #1240 deletion-safety review spillover; pre-existing, not introduced there). Fixture level: **compact** (documentation-of-contract + one guard test, S, no behavior change; the issue predates the `Suggested fixture level` field — triage recorded here, not diverged from).

## Why

`schemas/timeseries_compression_live_evidence.schema.json` pins five `*_invocation` artifact slots as v3-required — `recovery.invocation` (`:269`), `migration.first_invocation` / `second_invocation` (`:271`), `receipts.dry_run_invocation` / `enforce_invocation` (`:272`) — and **no code interprets their content**. `grep -c '\["invocation"\]\|\["first_invocation"\]\|\["second_invocation"\]\|\["dry_run_invocation"\]\|\["enforce_invocation"\]' scripts/node27_timeseries_compression_live_evidence.py` returns 0. The verifier instead overwrites all five with the ledger reference (`:3535`, `:3566-3567`, `:3619-3620`) and emits them at `:4004`, `:4023`, `:4027`, `:4055`, `:4057` — the same object as `execution.ledger` at `:3992`. The repo's own canonical v3 example has all five byte-identical to `execution.ledger`.

A reader of the schema or of a terminal receipt sees five separately-named "invocation" fields and reasonably infers five distinct launch records (decompress / migration×2 / dry-run / enforce). There is one: the supervisor ledger. #1069 moved provenance truth to that ledger and had the verifier re-derive these references, but left the slot names and the `required` pins standing on the contract. #1086 (PR #1239) and #1240 (PR #1396) each deliberately kept the schema/bundle contract out of scope. This is the last undecided surface on that cleanup chain, and the same failure class those three closed: a surface that looks like an oracle and is not.

## What changes

方案 a, **as decided by the user on 2026-08-19** (recorded in the issue; 方案 b is explicitly no longer an option and MUST NOT be re-selected during implementation): keep the slots, name the truth source.

1. Add a `description` to each of the five schema properties (`:90`, `:128-129`, `:157-158`) carrying one canonical formulation of what the slot actually is.
2. Add a one-line comment at each of the three verifier overwrite points pointing at `scripts/node27_timeseries_compression_bundle_author.py:21-25`.
3. Fix `docs/runbooks/tier-node27-timeseries-storage.md:770-771`: "Legacy authored invocation JSON **may** remain" reads as optional; the five keys are mandatory. Name them in that referenced-contracts narrative.
4. **Beyond the issue's literal 方案-a list**: one committed guard test asserting each of the five schema properties carries a description naming `execution.ledger`. Reason: adding a spec requirement obligates a scenario oracle, and prose with no oracle is exactly what rots — the #1069→#1086→#1240 chain exists because a dead surface stayed silent. It mechanizes the issue's own AC greps. It adds none of the costs that killed 方案 b (no contract change, no example regeneration, no rewrite of the `:3239` negative test, no node-27 receipt).

## The canonical formulation (copy verbatim, do not paraphrase)

The issue's shorthand "输入值被忽略" is **false**, and the first draft of this fixture replaced it with a second false statement ("the authored value never reaches the terminal state"). Both were refuted from primary sources in fixture review; see design D2 for the evidence. The single sentence every carrier uses is:

> Required, and enforced only as an artifact-closure node: the file must exist as a regular non-symlink whose `sha256`/`bytes` match, and if it parses as JSON it is complexity-bounded and its own nested artifact references are resolved transitively; its authored `path`/`sha256`/`bytes` is retained in the terminal `source_manifest`. The invocation semantics inside it (argv, exit code, timings) are never interpreted, and this slot in the terminal document is never the authored reference — the verifier re-derives it from `execution.ledger`.

It is deliberately longer than a slogan. Each clause is load-bearing and each was checked against code, not inferred.

## Non-goals

- 方案 b (removing the slots from `required` / the exact-key sets / the terminal output). Ruled out by the user; not a fallback.
- Any new launcher/interpreter identity verifier gate. The #1261 ruling at `scripts/node27_timeseries_compression_live_evidence.py:1148` stands byte-untouched, and this PR must not be described as "adding launcher identity validation".
- Weakening `_validate_exact_command_argv` / `_concrete_argv`, or touching the `database_audit_proof` `{"const": false}` pins (schema `:61`, `:80`).
- Re-litigating #1240 / re-introducing `INVOCATION_ARGV`.
- Regenerating `schemas/examples/timeseries_compression_live_evidence.example.json` (a 方案-b cost; annotations do not invalidate it).

## Risk triage

- Risk surface: contract/cognitive entropy on the node-27 compression live-evidence lane. No live harm today (production authoring always fills the ledger; the terminal value is always overwritten; artifact closure still proves the file exists). Blast radius of the change itself: annotation-only, no validation semantics.
- Selected risk packs: **spec-conformance / oracle-integrity** (the change is about what a contract truthfully claims; the guard test must be a real oracle, not a tautology) and **documentation-truthfulness** (the canonical formulation must be true in every carrier — this is the exact class that cost three review rounds on #1414).
- Not selected: performance (no runtime path), security/trust-boundary (explicitly no new gate; the #1261 fence is an AC), data-correctness (no data path).
