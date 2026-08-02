# Extend the canonical-path guard to schema_dump_host (#1268)

## Why

#1265/PR #1267 closed the "authors fine, deterministically false-refuses
at the forensic gate" middle state for `repo`/`root` — but the guard
loop traverses exactly those two labels, and `--schema-dump-host` is a
third path that walks the same asymmetry (all facts re-verified at
master 2822e408): plan_author records it verbatim into the pg_dump
argv (:181) and `artifact_associations` (:183) with zero shape
validation (param :94, CLI :348, default :38), while the verifier
normalizes the ledger-side artifact ref (`str(Path(...))`,
live_evidence.py :437/:463/:510/:521) and compares the plan side
verbatim at :1439 ("supervisor observed artifact path differs from run
plan output"). A `//`-bearing host dump path authors a plan whose
bundle can never PASS, with a message unrelated to the operator's
actual mistake. The `..` layer holds too, one stage earlier than for
root (fixture-review P3-3): prearm checks only `is_absolute`
(prearm.py:363) and `supervisor.validate_run_plan` passes, but the
supervisor's produced-artifact inspect
(`inspect_bounded_file_no_follow` over `artifact_associations`,
supervisor.py:897-909) refuses the `..` component the moment pg_dump
exits — the ledger never even gains the ref, and the verifier's own
no-follow artifact read would refuse it the same way —
authored-but-aborts-mid-window either way. The spec itself records
this as a known residual "routed to follow-up issue #1268"
(openspec/specs/hypertable-compression/spec.md:484-491) — this change
pays that recorded debt.

## What Changes

Adopted route (the issue's recommendation): **extend the existing
loop's domain**, so the guard stays one rule, not two.

1. **plan_author.py :109**: the shared loop tuple gains
   `("schema_dump_host", schema_dump_host)` — same `is_absolute`
   pre-check, same three-conjunct predicate
   (`value != str(Path(value)) or value.endswith("/") or ".." in
   Path(value).parts`), same message shape naming label + offending
   value + canonical rendering. No second loop, no reordering: repo
   and root keep failing first in the existing order, their messages
   byte-identical. All three conjuncts carry for this label: interior
   `//` → :1439 false refusal (reproduced in #1268); `..` → the
   supervisor's produced-artifact no-follow inspect aborts the moment
   pg_dump exits, before any ledger ref exists (and the verifier's
   artifact read would refuse it identically); slash-roots degenerate
   the same way.
2. **`schema_dump_container` adjudicated NOT guarded** (the issue's
   explicit decision point, judged by "what the comparisons actually
   do"; consumer sweep completed per fixture-review P2-1): its full
   consumer set is (a) plan_author :196 (pg_restore `--list` argv
   only; that command's `artifact_associations` are empty, so it
   never reaches :1439), (b) the verifier's prefix+shape argv gates
   (`argv[:5]` + `len == 6` + `startswith("/var/lib/postgresql/")`,
   live_evidence.py:744-749, and the same prefix check on the
   captured listing at :1892), and (c) the SUPERVISOR's mirror gates —
   `_assert_exact_argv`'s identical prefix+shape check
   (supervisor.py:350-364) and
   `resolve_container_pg_restore_identity`, which extracts
   `argv[-1]` verbatim (:1055-1058, invoked :1768), asserts the same
   `startswith` as a mount-containment claim, then runs
   `docker exec sha256sum <path>` on it. Every one of these
   comparisons is verbatim-symmetric — zero `Path()` normalization
   anywhere on the container path's chain — so the #1268 disease
   (verbatim-vs-normalized false refusal) cannot occur for it; the
   adjudication rests on THAT symmetry, deliberately not on any
   broader "nobody checks it" claim. The sweep did surface that
   `startswith`-as-containment is `..`-traversable
   (`/var/lib/postgresql/../../etc/passwd` passes all four gates) —
   a PRE-EXISTING defect in the supervisor/verifier prefix gates,
   not introduced or widened here, routed to a follow-up issue
   rather than silently folded into this producer-side change. The
   adjudication is recorded in the spec (residual sentence
   rewritten, scoped to the symmetry rationale) and PINNED by a
   test: an interior-`//` container path still authors and lands
   verbatim in the pg_restore list argv.
3. **New tests** (co-located, extending the #1265 section):
   the negatives parametrize domain gains the `schema_dump_host`
   label (6 shapes × 3 labels); a relative-path negative
   parametrized over all three labels (fixture-review P2-2: the
   issue's fifth acceptance shape — refused by the pre-existing
   "must be an absolute path" branch, whose message names the label
   but deliberately NOT the value/canonical rendering; the test
   asserts that actual posture and its docstring records the
   difference); a canonical custom host-path positive (association
   records it verbatim); `DEFAULT_SCHEMA_DUMP_HOST` joins the
   defaults structural pin; the container adjudication boundary test
   above.
4. **Docs/spec sync**: runbook :1041-1046 paragraph names
   `--schema-dump-host` alongside `--root`/`--repo` and records the
   container adjudication in one clause; spec delta MODIFIES the
   #1265 requirement — guard domain now "repo, root and
   schema_dump_host", residual list shrinks to `capture_repo` +
   `--schema-dump-container` (with the symmetric-prefix rationale),
   the "#1268 routing" sentence is consumed.

### Out of scope (verbatim-preserve surface)

- The verifier's comparison posture: live_evidence.py entirely zero
  diff (:1439 verbatim comparison, ledger normalization, argv gates,
  every #1250-#1265 gate). The normalize-both-sides anti-pattern
  stays rejected (#1265 adjudication).
- `EXPECTED_CAPTURE_TOOL_VALUES` pin domain: the spec sentence at
  :355 ("the `--schema-dump-*` options stay deliberately unpinned —
  legitimately parameterized data paths") is about VALUE-pinning in
  the verifier, not shape-guarding at the producer; checked, stays
  true, no MODIFIED needed for that requirement.
- `capture_repo` (hermetic-only kwarg, value-pinned by the verifier).
- #1266 exit-2 family; #1240/#1255 same-lane follow-ups.
- capture.py / supervisor.py / bundle_author.py / prearm.py /
  safe_fs.py / schemas/** / capture+supervisor test files: zero diff.

## Impact

- Affected spec: `hypertable-compression` (one MODIFIED requirement —
  the #1265-added canonical-path requirement).
- Affected code: `scripts/node27_timeseries_compression_plan_author.py`
  (one tuple entry + comment update),
  `tests/test_node27_timeseries_compression_live_evidence.py`
  (parametrize domain + three new/extended tests),
  `docs/runbooks/tier-node27-timeseries-storage.md` (one paragraph
  extended).
- Behavior change is authoring-time refusal only: the runbook's
  authorized command uses `DEFAULT_SCHEMA_DUMP_HOST` (canonical,
  newly pinned structurally); no previously PASS-able bundle is
  affected (non-canonical host paths could never PASS — that is the
  defect). Fully hermetic verification.
