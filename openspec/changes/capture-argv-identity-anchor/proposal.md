# Anchor capture argv to the committed producer identity (#1259)

## Why

The verifier's trust anchor for run-plan captures is argv SHAPE, not
argv IDENTITY. `_concrete_argv` (live_evidence.py:597-606) checks
non-empty strings / no placeholders / absolute argv[0]; #1250 added
the seam-prefix scan (:1014-1016). Nothing asks WHO produced the
snapshots: the in-repo `_bundle()` fixture gives all twelve captures
`"argv": ["/usr/bin/printf", "{}"]` (tests:1154) and verifies to
PASS — a standing existence proof that a hostile plan author needs
no seam token at all. The supervisor has the same shape-only gap
(`_assert_concrete_argv` without `exact=True` at supervisor.py:593,
:892 — contrast the command side's `exact=True` at :557). Commands,
by contrast, are pinned to production binaries per kind
(`_validate_exact_command_argv`, live_evidence.py:609-735).

Facet B: #1250's seam gate rejects `--self-test-` prefixed tokens,
but capture.py's parser has `allow_abbrev=True` (default, :755-756).
Today `--self-t` is rejected only because TWO seams share the prefix
(argparse ambiguity — measured in #1259); with a single seam,
`--self-t 500000000000` is ACCEPTED by capture.py yet does not start
with `--self-test-`, silently re-opening the fabricated-free_bytes
path. #1250's protection rests on an accidental, unrecorded, untested
premise.

## What Changes

Explorer-verified design decisions (all line refs measured at master
post-#1258):

1. **Verifier identity anchor** (live_evidence.py, captures loop
   :990-1024): every `run_plan.captures[*].argv` must satisfy
   (a) `argv[1] == f"{EXPECTED_REPO_PATH}/scripts/node27_timeseries_compression_capture.py"`
   — the bundle claims production forensics, so the producer script
   is pinned absolutely, same posture as the command-side
   `expected_executable` dict (:610-620);
   (b) `argv[2:4] == ["--kind", capture["kind"]]` — plan_author
   builds exactly this for all twelve kinds (plan_author.py:238,
   loop :227-241), so cross-kind argv swaps become structurally
   impossible;
   (c) the token pair `["--mutation-head-sha", <sha>]` MUST be
   present and `<sha>` MUST equal the run plan's mutation head SHA
   (position-independent scan — capture_common puts it at a fixed
   offset today but we do not pin the full option layout).
   **argv[0] (the interpreter) is deliberately NOT pinned** —
   recorded decision: production argv[0] is `sys.executable`
   resolved at plan-author runtime (plan_author.py:97; runbook
   invokes via `.venv/bin/python`, tier-node27 runbook :1034-1038),
   an environment fact, not a committed identity; pinning it would
   also force the hermetic e2e to fake an interpreter path it never
   runs. Full per-kind option-layout pinning stays rejected (the
   #1250-named brittleness).
2. **Facet B — abbreviation-proof seam rejection** (same loop):
   generalize the #1250 scan: for each token, take
   `base = token.split("=", 1)[0]`; reject when
   `base.startswith(SELF_TEST_SEAM_PREFIX)` (the #1250 rule,
   `=value` form included) OR when `len(base) >= 3 and
   "--self-test-".startswith(base)` (i.e. base is `--s`, `--se`,
   …, `--self-test-` — every prefix argparse could accept as an
   abbreviation of a lone seam). Bound is >= 3, not >= 4
   (fixture-review F3): rejecting `--s` outright removes any
   reliance on `--systemctl`/`--schema-dump-*` keeping `--s`
   ambiguous — the exact class of unrecorded premise this facet
   exists to kill. Recorded premise: no legitimate plan token is
   ever `--s`; plan_author emits full flags only (:214-225).
   Collision analysis (measured): capture.py's non-seam flags
   starting with `--s` are `--systemctl` (--sy…),
   `--schema-dump-host`/`--schema-dump-container` (--sc…) — NONE
   start with `--se`, so the rejection domain has zero overlap with
   legitimate flags; a structural test pins that invariant against
   future flag additions.
3. **Supervisor identity anchor** (supervisor.py :593 and :892 via a
   shared helper): `argv[1].endswith("/scripts/node27_timeseries_compression_capture.py")`
   AND `argv[2:4] == ["--kind", kind]` (kind is in scope at both
   sites — :582, :890). Suffix, not EXPECTED_REPO pin — recorded
   asymmetry: the supervisor is the executor of hermetic plans whose
   capture script legitimately lives under the test checkout
   (e2e plan_exec argv[1] = str(ROOT/…)); the production-path claim
   belongs to the verifier alone. NO seam check in the supervisor —
   #1250's recorded decision stands (the executor must run
   seam-carrying plans). Known blast radius (fixture-review F1):
   ~10 tests in tests/test_node27_timeseries_compression_supervisor.py
   build capture argvs as `[sys.executable, "-c", "print('{}')"]`
   (`_plan()` :244-248) or execute inline `-c` stubs — they get
   mechanical updates (producer-shaped template for validate-only
   plans; stub written to `<tmp>/scripts/node27_timeseries_compression_capture.py`
   for executing tests) that preserve every original failure
   signature; the anchor is never weakened to accommodate them.
4. **Fixture and e2e updates** (tests/…live_evidence.py):
   - `_bundle()`'s twelve capture argvs (:1150-1158, single
     comprehension) become producer-shaped: absolute interpreter,
     production capture-script path, `--kind <kind>`,
     `--mutation-head-sha <the sha _bundle already uses>`. One-point
     fix; ~90 `_bundle` call sites inherit it.
   - The #1250 e2e: `plan_prod` drops its `capture_script` override
     (:4986) so captures carry the production default
     `/home/nwm/NWM/scripts/…capture.py` (plan_author.py:40) —
     `repo_path`/commands already use production defaults; the
     existing `plan_exec` divergence point (:5009-5025) additionally
     swaps capture argv[1] to `str(ROOT/…capture.py)` so the state
     machine still executes the real script; the #1250 capture
     ledger rewrite (:5073-5088) already maps executed argv back to
     `plan_prod` argv — no new rewrite machinery needed. Fidelity
     assertions keep working (they check seam tokens and snapshot
     values, not argv[1]).
5. **Tests** (append-only beyond the mechanical updates above,
   which now include the supervisor test file per F1):
   printf-bundle rejection, kind-swap rejection, mutation-sha
   mismatch rejection, missing-sha-pair rejection, `--se`-abbrev
   rejection, wrong-script-suffix and kind-swap rejection on the
   supervisor side at BOTH validate_run_plan and run_capture_step
   (F2), `--se`-collision structural test plus a literal-path pin
   on `EXPECTED_CAPTURE_SCRIPT` (F4, anti-tautology), plus green
   confirmation that #1250's seam tests and all existing PASS
   assertions survive.

Spec relation: delta lands in `hypertable-compression` beside the
#1250 seam-visibility requirement it extends; the new requirement
makes "these snapshots were produced by the committed capture
producer" a structural fact, which is the #1069 G-series lane's
original threat model (validate execution, not records). No existing
requirement conflicts — capture argv had no identity requirement
anywhere.

## Non-goals

- The #1250 seam gate and its tests (preserved verbatim; facet B
  only widens the rejection domain).
- capture.py behavior changes (`allow_abbrev=False` is the cleaner
  root fix but changes the production CLI acceptance surface —
  needs its own design pass, stays out per the issue).
- Pinning argv[0] / full per-kind option layouts (recorded
  decisions above).
- Output-fingerprint validation (the issue's facet-A alternative —
  proves "looks right", not "produced by the committed producer").
- schemas/**, plan_author.py, bundle_author.py, capture.py: zero
  diff.
- #1240 (INVOCATION_ARGV dead island — separate issue; note
  `_invocation_execution_identity` at live_evidence.py:278-305 is
  call-site-dead and NOT touched here).
