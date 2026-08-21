# Design

## D1 — Provenance and anchor drift (record before editing)

The issue's anchors were verified on master `c2439f62`. Re-checked here on `c16deaa8`:

| Carrier | Issue anchor | Located on `c16deaa8` | Drift |
|---|---|---|---|
| schema `required` pins | `:269` / `:271` / `:272` | `:269` / `:271` / `:272` | none |
| schema property declarations | `:90` / `:128-129` / `:157-158` | `:90` / `:128-129` / `:157-158` | none |
| runbook "may remain" | `:770-771` | `:770-771` | none |
| verifier ledger overwrites | `:3523`, `:3554-3555`, `:3607-3608` | `:3535`, `:3566-3567`, `:3619-3620` pre-edit / **`:3536`, `:3568-3569`, `:3622-3623`** post-edit | +12, then +3 |
| terminal output sites | `:3992`, `:4011`, `:4015`, `:4043`, `:4045` | `:4004`, `:4023`, `:4027`, `:4055`, `:4057` pre-edit / **`:4007`, `:4026`, `:4030`, `:4058`, `:4060`** post-edit | +12, then +3 |
| recovery exact-key set | `:3463` | **`:3475`** (unshifted; above the first comment) | +12 |
| closure call site | `:4159` (issue) | **`:4171`** pre-edit / **`:4174`** post-edit | +12, then +3 |
| `#1261` ruling comment | `:1140-1149` | `:1148` (the `pinning argv[0]` line) | — |

Use the located anchors, not the issue's. **Self-inflicted drift, and how it was avoided**: a multi-line `description` insertion at `:90`, `:128-129`, `:157-158` would have shifted every schema line below it, staling `:269` / `:271` / `:272` inside this PR. The implementation therefore used the same-line form `{"$ref": ..., "description": ...}`, and the schema is 510 lines before and after — **no schema anchor moved**. The three added verifier comment lines *did* shift downstream `.py` anchors by +3; every carrier's `.py` anchors are re-derived from the landed file, and pre-edit values are labelled as such wherever they are kept for provenance.

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

**Wrong #3 — this fixture's second draft, refuted in round-1 cross-review.** Two clauses failed, both
universally quantified and both false on the shape the repo's own bundle author emits:

- *"this slot in the terminal document is never the authored reference."* `scripts/node27_timeseries_compression_bundle_author.py:237,247,249,258,260` writes `ledger_ref` into all five slots (its docstring `:21-25` says so outright), so on author-produced bundles the authored value and the terminal value are byte-identical. A verifier probe measured `slots(bundle) == slots(terminal)` → `True`.
- *"enforced only as an artifact-closure node: the file must exist…"* Closure enforcement is gated on the value being exactly a three-key mapping — `packages/common/evidence_io.py:192`, `if set(current) == {"path", "sha256", "bytes"}`. A four-key value, a string, or `null` is never collected and receives **no** enforcement; a probe with all five slots set to a four-key value naming `/nonexistent/nope.json` still returned `qualifies_task_4_5 is True` and produced a schema-valid terminal document. **The evidence schema is never applied to the input bundle at all** — the only `jsonschema` application is `scripts/node27_timeseries_compression_live_evidence.py:4208`, over the *terminal* document, after `verify_bundle` returns.

**Wrong #4 — the third draft's final clause, caught by the implementer during the round-1 fix pass and
reported rather than silently patched.** *"A value of any other shape is not closure-checked at all"* is false
for a mapping that **wraps** a well-formed reference: `artifact_references`
(`packages/common/evidence_io.py:189-197`) descends into a non-three-key mapping's values
(`stack.extend(current.values())`), so a nested `{path, sha256, bytes}` is collected as a root-level closure
node in its own right and an unavailable path inside it still fails the run closed. The escape is real only
for shapes containing no nested reference — a mapping whose extra key holds a scalar, a bare string, `null`.

The lesson, recorded because it caught four drafts in a row: this contract has **two** live bundle shapes — the
legacy hand-assembled one (five distinct `*-invocation.json` files; the committed example and the suite's
`_bundle` fixture) and the production author one (all five slots literally the ledger ref). An unqualified
sentence must be true of both. Every earlier draft was written against one shape and checked against the same
one.

What is genuinely never interpreted is the **invocation semantics** — argv, exit code, timings. That is the
defensible claim, and it is the one `test_legacy_authored_invocations_do_not_contribute_to_v3_truth`
(`tests/test_node27_timeseries_compression_live_evidence.py:3240`) already pins.

So the canonical sentence, used identically in every carrier:

> Required — by the verifier's exact-key check on the input bundle, and by this schema in a v3 qualifying (non-failure) terminal document. The invocation semantics inside the value — argv, exit code, timings — are never interpreted, and the verifier re-derives this slot from `execution.ledger` rather than copying what was authored here; the committed bundle author already writes that same ledger reference into this slot, so on its output the authored and terminal values coincide. The value is not otherwise inert: when it is exactly a `{path, sha256, bytes}` mapping it becomes an artifact-closure node — the file must exist as a regular non-symlink whose `sha256`/`bytes` match, and if it parses as JSON it is complexity-bounded and its own nested artifact references are resolved transitively — and it is retained, deduplicated by normalized path, in the terminal `source_manifest`. A value of any other shape is not itself a closure node, though any well-formed reference nested inside it still is, collected in its own right.

Carriers, in two tiers — the split is deliberate and recorded in tasks.md E6, not an accident.

**Tier 1, verbatim**: the five schema `description`s, the runbook narrative (modulo markdown line wrapping),
this D2 blockquote, the proposal blockquote, and the PR body. Copy the sentence; do not paraphrase.

**Tier 2, load-bearing assertion plus pointer**: the three verifier comments, which carry
`execution.ledger` and a pointer to `scripts/node27_timeseries_compression_bundle_author.py:21-25` rather
than the whole sentence — `pyproject.toml` sets `line-length = 120` with `E` selected and the sentence is
1032 characters, so a single-line comment would trip E501 while the D5 fence forbids wrapping the verifier
diff past three lines.

The spec delta is in **neither** tier: it is a SHALL-form restatement by construction, and it is checked for
semantic agreement, not byte identity.

The sentence's two load-bearing tokens for the guard test are `execution.ledger` and `re-derive`.

## D3 — The oracles, and what each one actually pins

A spec requirement without an oracle is the thing this issue is about. Round-1 cross-review caught this
fixture claiming that the pre-existing sentinel
`test_legacy_authored_invocations_do_not_contribute_to_v3_truth`
(`tests/test_node27_timeseries_compression_live_evidence.py:3239-3253`) pinned more than it does: it asserts
only `terminal["qualifies_task_4_5"] is True`, never reads a terminal `*_invocation` slot, and mutates two of
the five. So each new scenario gets its own oracle, and no scenario borrows credit from that sentinel beyond
the semantics half it genuinely covers.

**G1 — annotation guard.** Derive the slot list from the schema: every property declared under a `properties`
map whose name ends in `invocation`. Assert the derived set is exactly the five known slots, then assert each
carries a non-empty `description` containing `execution.ledger` and `re-derive`.

The derivation is deliberately **declaration-style-independent**. The first draft additionally required
`value.get("$ref") == "#/$defs/artifact_ref"` directly on the property object, which round-1 review
demonstrated would stay green on a sixth undescribed slot written with an `allOf`, `anyOf`, or inline-object
wrapper — i.e. it defended only against a slot written in the identical style, while the requirement says
"every". Matching on the `properties` position instead survives all three wrappers. Verified safe: the plural
`authorization.*_invocations` const integers end in `invocations`, not `invocation`, and the `required`
entries at `:269`/`:271`/`:272` are list elements rather than property keys, so neither is swept in — a walk
of the real schema returns exactly the five.

**Substring, deliberately, not full-text equality.** A byte-pinned description turns any future wording
improvement into red CI — a brittle test is its own kind of dead surface. The test pins the two load-bearing
tokens, not the prose.

**G2 — enforcement boundary.** Two halves, matching the scenario: a well-formed `{path, sha256, bytes}` slot
naming a nonexistent path fails closed with `BoundedEvidenceError`; a four-key mapping (and a string, and
`null`) naming the same nonexistent path is never closure-checked and still qualifies. This is the honest
statement of the boundary, and it is the one the second draft got wrong.

**G3 — manifest retention and dedup.** On a bundle whose five slots name five distinct well-formed
references, all five authored paths appear in `source_manifest`, distinct from the ledger reference in the
slots. On the bundle-author shape (all five slots = the ledger ref), that reference appears **once**, because
`resolve_artifact_closure` skips `manifest.append` for a repeated identical normalized path
(`packages/common/evidence_io.py`, the `if previous == identity: continue` branch).

What no test claims: that the description is *true*. Truth rests on the code facts in D2, each of which was
checked against the source and, for the contested clauses, against an executed probe.

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
- `description` is a JSON Schema annotation with no validation effect in any draft, so `packages/common/compression_terminal_state.py` (`CANONICAL_SCHEMA` at `:44`, `validate_terminal_document` at `:285`) and the three test modules that load this schema (`live_evidence.py:45`, `supervisor.py:754/:1111/:3213`, `capture.py:368`) are unaffected — but all are run as evidence, since "annotations are inert" is a claim, not an assumption.
- If #1398's slots are ever removed (方案 b, some future issue), this guard test and its requirement retire with them.
