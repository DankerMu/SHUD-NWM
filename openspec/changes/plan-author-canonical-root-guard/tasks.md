# Tasks: plan-author-canonical-root-guard

Fixture level: standard · Repair intensity: standard · Issue #1265

Review record: fixture review round 0 → REVISE (2 P2 + 2 notes);
repair iteration 1 folded P2-1 (the stability predicate alone has
exactly one escape — `root="//"` is normalization-stable yet emits
`///…` that both verifier normalizations collapse, recreating the
false refusal; the guard gains the `endswith("/")` conjunct and the
negatives gain `/` and `//`), P2-2 (spec scenario said "containing
`//`" — literally false for LEADING double slash, which POSIX
preserves and which is correctly accepted; scenario reworded to the
stability+slash-root formulation with the `//x` acceptance named,
and task 3(d) gains the `//x` boundary positive), Note 1 (the frozen
live_evidence :1179-1182 comment keeps its now-counterfactual
trailing-slash rationale — recorded in Must preserve below rather
than edited: the verbatim-rsplit posture still needs the refactor
protection, only the historical premise became unreachable), Note 2
(proposal Impact now records that the archived #1263 relation
scenario was checked and needs no MODIFIED).

Triage note: S — one validation clause in plan_author + one test
rewired + a handful of new tests + one runbook sentence; fully
hermetic. The decisive hazard is the PR #1264 trailing-slash test
(:6120-6161): its construction path (`build_run_plan(root=...+"/")`)
dies under the new guard, and a careless fix would DELETE the
dirname-swap discriminating power that test exists for — the rewire
must preserve byte-identical anchor assertions while synthesizing the
double-slash spelling by hand. Risk axes: (1) GUARD CORRECTNESS —
non-canonical repo/root refuse at authoring with an accurate message;
canonical inputs and module defaults unaffected; (2) PIN PRESERVATION —
the rewired test still reddens under a `Path.parent` derivation swap;
(3) FROZEN SURFACES — live_evidence.py entirely zero diff this time
(both verbatim comparisons, ledger normalization, all #1250/#1259/
#1262/#1263 gates), plus capture.py/supervisor.py/bundle_author.py/
schemas/capture+supervisor test files.

Line anchors (orchestrator-verified at master 05aafe8b):
plan_author.py — PlanAuthorError :79, sha check :107-108, shared
repo/root loop :109-111 (`for label, value in (("repo", repo),
("root", root)): if not Path(value).is_absolute()`), derived repo
paths :113-117 (python/wrapper/migration/decompress/benchmark
f-strings), root-derived receipts :119-120+, evidence-dir :219,
output_path :239, DEFAULT_ROOT :37, --root CLI :295 (free argument),
run_plan_id stability check :285.
live_evidence.py (FROZEN this change) — `_artifact_bytes` normalize
:437/:463, `_artifact_ref_from_raw` :510/:521, command association
verbatim equality :1439 ("supervisor observed artifact path differs
from run plan output"), capture output_path verbatim equality :1534
("supervisor capture output path differs"), relational evidence-dir
gate rsplit :1185-region.
tests/…live_evidence.py — trailing-slash test :6120-6161 (docstring
claims "a spelling the production author really does emit" — becomes
false under this change, must be rewritten; construction at :6143 via
build_run_plan with trailing slash; anchor assertions
`message == "supervisor capture output path differs"` +
`_EVIDENCE_DIR_GATE_WORDING not in message`); twelve-kind positive
control (root=str(tmp_path)); plan_author drift guards. The capture
and supervisor test files call build_run_plan only with canonical
tmp roots (verify with grep at implementation time) — frozen, zero
diff.
docs/runbooks/tier-node27-timeseries-storage.md — plan-author
authorized command ~:1034 (passes only --mutation-head-sha/--output;
no --root documented anywhere in the runbook today).
Blast radius: e2e and every other build_run_plan call site uses
canonical `str(tmp_path)`/production defaults — the guard fires for
none of them; CI selector: this PR touches the live_evidence test
file so selection covers plan_author.py (no same-name test file
exists — co-location recorded in proposal Out of scope).

Must preserve:
- `scripts/node27_timeseries_compression_live_evidence.py`,
  `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_supervisor.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `schemas/**`, `tests/test_node27_timeseries_compression_capture.py`,
  `tests/test_node27_timeseries_compression_supervisor.py`: zero diff
  (suite baselines 14/141 unchanged; live_evidence count changes only
  by the new tests — the rewired test keeps one id).
- The rewired trailing-slash test: anchor assertions byte-identical
  (`message == "supervisor capture output path differs"`,
  `_EVIDENCE_DIR_GATE_WORDING not in message`); it must still redden
  under a `str(Path(output_path).parent) + "/capture-artifacts"`
  derivation swap (dirname-swap probe re-run as proof) — the
  discriminating power is the point, the construction is the only
  thing that changes.
- Twelve-kind positive control, drift guards, all #1263 help/evidence
  tests, 28-case matrix: bodies untouched, green.
- plan_author sha check :107-108 and everything after the validation
  loop: untouched; the new clause extends the EXISTING loop (no
  second loop, no reordering — repo and root keep failing in the same
  order and the absolute-path message stays byte-identical for its
  cases).
- live_evidence.py :1179-1182 comment (the rsplit-posture rationale
  citing trailing-slash round-trip): stays byte-identical even though
  its premise becomes producer-unreachable after this change — the
  file is frozen here, the verbatim posture still needs the
  refactor protection, and the recorded honesty fix happens in the
  TEST docstring (task 2) where the "production author really emits
  this" claim lived. Recorded so no reviewer flags it as an
  unrecorded stale comment, and no implementer "fixes" a frozen
  file.

## Implementation tasks

- [ ] 1. Guard — plan_author.py :109-111: inside the existing loop,
  after the is_absolute check, add: if `value != str(Path(value))
  or value.endswith("/")`, raise `PlanAuthorError(f"{label} must be
  a canonical absolute path (no trailing slash, duplicate slashes
  or dot segments): ...")` naming the label, the offending value
  and the canonical rendering (exact wording implementer's choice,
  those three elements mandatory). The `endswith("/")` conjunct is
  load-bearing, not redundant (fixture-review P2-1): `"/"` and
  `"//"` are the ONLY strings that are Path-normalization-stable
  yet end in a slash — `str(Path("//")) == "//"` — and a `root="//"`
  would emit `///capture-<kind>.json`, which BOTH verifier-side
  normalizations collapse to `/capture-<kind>.json`, recreating
  exactly the false-refusal middle state this change eliminates.
  With both conjuncts, the enumerated-probe property holds for
  every accepted root R: `str(Path(f"{R}/x")) == f"{R}/x"`. Comment records: WHY producer-side (the
  verifier compares plan paths verbatim against Path-normalized
  ledger refs at the two equality sites — a non-canonical root
  authors a bundle that deterministically false-refuses with an
  unrelated message; the verifier deliberately invents no
  normalization, so canonicality is an authoring precondition), that
  `..` is deliberately still accepted (Path preserves it —
  normalization-stable, round-trips both sides), and that
  `capture_repo` stays unvalidated (hermetic-only kwarg, verifier
  value-pins --repo anyway).
- [ ] 2. Rewire the trailing-slash test
  (tests/…live_evidence.py:6120-6161): drop the `build_run_plan`
  construction; synthesize the double-slash spelling directly —
  `output_path = f"{tmp_path}//capture-sizes_post.json"`, argv =
  copy of the bundle's sizes_post capture argv with the
  `--evidence-dir` value rewritten to
  `f"{tmp_path}//capture-artifacts"` (by option name, not offset);
  keep the existing artifact copy + `_replace_produced_artifact` +
  `_replace_capture_argv` flow and the byte-identical anchor
  assertions. Rewrite the docstring: the production author can no
  longer emit this spelling (this change rejects it at authoring —
  cite the new guard test); the pin now guards the verifier's
  verbatim textual posture itself against a normalizing-derivation
  refactor, and the dirname-swap redness proof still holds. Also add
  one assertion pinning the relation holds pre-verification
  (`argv` binds `--evidence-dir` to exactly
  `output_path.rsplit("/", 1)[0] + "/capture-artifacts"`) so the
  synthesized spelling cannot silently drift apart.
- [ ] 3. New tests (co-located in tests/…live_evidence.py):
  (a) parametrized guard negatives: for label in {root, repo} ×
  shape in {trailing slash `/x/y/`, duplicate slash `/x//y`, dot
  segment `/x/./y`}, plus the two slash-roots `/` and `//` (the
  normalization-stable-yet-trailing-slash pair the second conjunct
  exists for): `build_run_plan` raises `PlanAuthorError`, the
  message contains the label and the canonical rendering, and (for
  one case) does NOT contain "supervisor capture output path
  differs" (non-vacuity vs the old failure mode);
  (b) canonical positive: `build_run_plan(root=str(tmp_path))`
  authors successfully (the existing twelve-kind control already
  covers this — reference it; add only a minimal
  guard-does-not-fire check if the control alone cannot attribute);
  (c) structural: `plan_author.DEFAULT_ROOT` and
  `plan_author.DEFAULT_REPO` are Path-normalization-stable
  (`v == str(Path(v))`) — the guard can never refuse the module's
  own defaults;
  (d) deliberate-boundary positives: one `..`-segment root (e.g.
  f"{tmp_path}/sub/..") authors successfully, and one
  leading-double-slash root (`//x`-style, POSIX preserves exactly
  two leading slashes so it is normalization-stable AND its
  f-string expansions are symmetric on both verifier sides —
  accepted correctly, fixture-review P2-2) authors successfully —
  pins both boundaries recorded in the guard comment.
- [ ] 4. Runbook — docs/runbooks/tier-node27-timeseries-storage.md
  plan-author section (~:1034): one sentence noting custom
  `--root`/`--repo` values must be canonical absolute paths (no
  trailing slash — the plan author refuses otherwise); the
  authorized command itself uses the canonical defaults and is
  unaffected.
- [ ] 5. Full verification (orchestrator Phase 2 reproduces):
  three suites green (live_evidence baseline 359 + new ids; 14/141
  frozen); red proof by reverting the guard clause hunk → all new
  guard negatives DID NOT RAISE (per-test-id list) while everything
  pre-existing stays green; dirname-swap probe re-run → exactly the
  rewired trailing-slash test reds (pin preserved); `uv run ruff
  check .`; `openspec validate plan-author-canonical-root-guard
  --strict --no-interactive`; frozen-surface zero-diff check
  (`git diff --stat` names only plan_author.py, the live_evidence
  test file, and the runbook).

## Evidence Floor

- Three suites green with counts (baseline 359/14/141; only
  live_evidence grows).
- Red proof: guard-hunk revert → new negatives DID NOT RAISE
  (per-id list), everything else green.
- Pin-preservation proof: dirname-swap probe reds exactly the
  rewired trailing-slash test on the fixed tree.
- Frozen surfaces zero diff (live_evidence.py included this time);
  ruff + openspec strict green.
- PR body records: the adopted route and the rejected alternative
  (verbatim-posture rationale), the `..`/`capture_repo` recorded
  boundaries, the rewired test's honesty fix (docstring no longer
  claims the production author emits the spelling), and 偏离记录
  (explicit "no deviations" otherwise).
