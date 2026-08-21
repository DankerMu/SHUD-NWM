# Design

## D1 — Provenance and anchor drift (record before editing)

The issue's anchors were verified on master `c2439f62`. Re-checked here on `c16deaa8`:

| Carrier | Issue anchor | Located on `c16deaa8` | Drift |
|---|---|---|---|
| schema `required` pins | `:269` / `:271` / `:272` | `:269` / `:271` / `:272` | none |
| schema property declarations | `:90` / `:128-129` / `:157-158` | `:90` / `:128-129` / `:157-158` | none |
| runbook "may remain" | `:770-771` | `:770-771` | none |
| verifier ledger overwrites | `:3523`, `:3554-3555`, `:3607-3608` | **`:3535`, `:3566-3567`, `:3619-3620`** | +12 |
| terminal output sites | `:3992`, `:4011`, `:4015`, `:4043`, `:4045` | **`:4004`, `:4023`, `:4027`, `:4055`, `:4057`** | +12 |
| recovery exact-key set | `:3463` | **`:3475`** | +12 |
| closure call site | `:4159` (issue) | **`:4171`** pre-edit / **`:4174`** post-edit | +12, then +3 |
| `#1261` ruling comment | `:1140-1149` | `:1148` (the `pinning argv[0]` line) | — |

Use the located anchors, not the issue's. **And note the self-inflicted drift**: inserting `description` at `:90`, `:128-129`, `:157-158` shifts every schema line below it, so `:269` / `:271` / `:272` go stale *inside this PR*. No carrier may cite post-edit schema line numbers until the edit is final; the PR body re-derives them from the landed file.

## D2 — The canonical formulation, and the two false statements it replaces

Two wrong versions of this sentence were written before this one. Both are recorded here because the whole
point of the change is to stop a false contract claim, and shipping a *new* false claim is the failure mode
closest at hand.

**Wrong #1 — the issue's shorthand, "输入值被忽略" (the input value is ignored).** False: the value is read.
`resolve_artifact_closure(bundle)` (`scripts/node27_timeseries_compression_live_evidence.py:4174` (post-edit; `:4171` pre-edit) →
`packages/common/evidence_io.py:201`) hashes the file and rejects a slot pointing at an absent, symlinked,
or hash-mismatched path with `BoundedEvidenceError`.

**Wrong #2 — this fixture's own first draft, "the authored value never reaches the terminal state."** Also
false, and worse because it would have shipped into normative SHALL text. The closure's manifest is emitted
as the terminal document's `source_manifest` (`:4093`, fed from `closure.manifest` at `:4205`; both post-edit, +3 from the three added comment lines), and
`artifact_references` (`packages/common/evidence_io.py:178-186`) collects every `{path, sha256, bytes}`
mapping in the raw input bundle — the five authored slots included. Empirically, the repo's own canonical v3
example carries all five authored invocation paths in `source_manifest`
(`/home/nwm/NWM/.nhms-issue1069-live/{recovery,migration-first,migration-second,dry,enforce}-invocation.json`,
5 of its 42 nodes) while the five slots themselves hold the ledger. The authored value survives; only the
slot is overwritten.

**Also refuted: "content never interpreted" / "constrained only as an artifact reference."** The closure
does `json.loads` on every node, applies `validate_json_complexity` depth/node/array ceilings, and
recursively resolves nested `{path, sha256, bytes}` references found inside
(`packages/common/evidence_io.py:270-280`). That recursion is live, not theoretical — the example's
`source_manifest` contains repo source files such as `packages/common/forecast_store.py` that can only have
arrived transitively. The issue's hermetic repro (its evidence item 7) used flat JSON with no nested refs,
so it never exercised this and does not license the "only"/"never parsed" wording.

What is genuinely never interpreted is the **invocation semantics** — argv, exit code, timings. That is the
defensible claim, and it is the one `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`
(`tests/test_node27_timeseries_compression_live_evidence.py:3239`) already pins.

So the canonical sentence, used identically in every carrier:

> Required, and enforced only as an artifact-closure node: the file must exist as a regular non-symlink whose `sha256`/`bytes` match, and if it parses as JSON it is complexity-bounded and its own nested artifact references are resolved transitively; its authored `path`/`sha256`/`bytes` is retained in the terminal `source_manifest`. The invocation semantics inside it (argv, exit code, timings) are never interpreted, and this slot in the terminal document is never the authored reference — the verifier re-derives it from `execution.ledger`.

Carriers that must agree: the five schema `description`s, the three verifier comments, the runbook
narrative, the spec delta, this D2, and the PR body. Copy the sentence; do not paraphrase it per carrier.
Its two load-bearing tokens for the guard test are `execution.ledger` and `re-derive`.

## D3 — Why a guard test, and what shape it takes

A spec requirement without an oracle is the thing this issue is about. The test is the smallest possible oracle for the thing being claimed: that the five slots are annotated at all, and that the annotation names the real truth source.

Shape (mirrors the existing schema-shape guard `tests/test_node27_timeseries_compression_live_evidence.py:3745`, reusing the module-level schema already loaded at `:45`):

- **Derive** the slot list from the schema rather than hardcoding it: scan every property whose name ends in `invocation` and whose subschema `$ref`s `#/$defs/artifact_ref`. Assert the derived set is exactly the five known slots (so the test fails loudly if a slot is added *or* removed rather than silently skipping it), then assert the annotation on each.
- Assert each such property object has a non-empty `description`.
- Assert the description contains `execution.ledger` and `re-derive` (substring, case-insensitive on the latter).

Deriving rather than hardcoding is what makes the test match the requirement's own word "every". A hardcoded five-pair list would pass green on a future sixth undescribed slot while the spec says every slot must be annotated.

**Substring, deliberately, not full-text equality.** A byte-pinned description turns any future wording improvement into red CI — a brittle test is its own kind of dead surface. The test pins the two load-bearing facts (annotated; names the ledger as the source), not the prose.

What the test does **not** claim: it does not prove the description is true. Truth of the canonical statement rests on the code facts recorded in D2, which the existing `test_legacy_authored_invocations_do_not_contribute_to_v3_truth` (`:3239`) already pins on the semantics side (authored content with `exit_code=1` / `timeout_seconds=901` / a reused invocation still yields `qualifies_task_4_5 is True`). That test stays untouched — it is the negative twin of this one.

## D4 — Runbook placement

The AC says name the five keys in `:755-771`. `:755-763` is the **top-level** bundle key list; the five slots are nested (`recovery.invocation`, `migration.first_invocation` / `second_invocation`, `receipts.dry_run_invocation` / `enforce_invocation`). Grafting nested keys into a top-level list would make that list wrong. They are named in the referenced-contracts narrative at `:768-771`, where the "may remain" sentence lives and where `execution.ledger` is already the subject.

## D5 — Fences (each is an acceptance criterion with diff self-evidence)

- `scripts/node27_timeseries_compression_live_evidence.py:1148` (#1261: the launcher/interpreter closure is producer-side attestation, not a verifier gate) is byte-untouched, and no new launcher/interpreter gate appears anywhere in the diff.
- `_validate_exact_command_argv` / `_concrete_argv` untouched.
- Schema `:61` / `:80` `database_audit_proof` `{"const": false}` untouched.
- The verifier diff is exactly three comment lines. No logic edits, no reordering, no changed `required` sets, no changed exact-key sets, no changed terminal output.
- `schemas/examples/timeseries_compression_live_evidence.example.json` untouched.

## D6 — Couplings recorded, not solved

- The five slots stay duplicated in every terminal receipt (five byte-identical copies of `execution.ledger`). That is the accepted cost of 方案 a: readers still see the duplication, but the schema now tells them why. Removing it is 方案 b, ruled out by the user.
- `description` is a JSON Schema annotation with no validation effect in any draft, so `packages/common/compression_terminal_state.py:44` (`validate_terminal_document`) and the three test modules that load this schema (`live_evidence.py:45`, `supervisor.py:754/:1111/:3213`, `capture.py:368`) are unaffected — but all are run as evidence, since "annotations are inert" is a claim, not an assumption.
- If #1398's slots are ever removed (方案 b, some future issue), this guard test and its requirement retire with them.
