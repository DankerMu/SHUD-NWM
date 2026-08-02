# Reject non-canonical roots at the plan-author entrance (#1265)

## Why

A trailing-slash `--root` makes every real plan_author-authored bundle
deterministically FAIL the forensic PASS gate with a misleading message
(all facts measured; defect predates #1263, introduced with 71125485):

- plan_author's ONLY root/repo validation is `Path(value).is_absolute()`
  (plan_author.py:109-111, shared loop over ("repo", repo) and
  ("root", root)); a trailing-slash root passes.
- Both capture fields are verbatim f-strings from the same root
  (`output_path` :239, `--evidence-dir` :219), so a trailing-slash root
  emits double-slash spellings on both.
- The verifier normalizes the LEDGER side (`_artifact_bytes` returns
  `str(Path(ref["path"]))`, live_evidence.py:437/:463; same in
  `_artifact_ref_from_raw` :510/:521) but compares the PLAN side
  verbatim at two sites: capture `output_path` equality :1534
  ("supervisor capture output path differs") and command
  `artifact_associations` equality :1439 ("supervisor observed artifact
  path differs from run plan output"). Normalized single-slash can never
  equal verbatim double-slash → deterministic false refusal whose
  message has nothing to do with the operator's actual mistake (an
  extra `/`).
- The #1263 relational `--evidence-dir` gate is NOT the problem: its
  textual `rsplit` derivation round-trips the double slash consistently
  (the PR #1264 trailing-slash test proves exactly that).

## What Changes

Adopted route (the issue's recommendation): **reject non-canonical
roots at the producer entrance**, keeping the verifier's
verbatim-forensic posture untouched — the verifier judges the recorded
bytes and invents no normalization; bad input fails at authoring time
with an accurate message instead of minutes later at the forensic gate
with an unrelated one.

1. **plan_author input canonicalization** (plan_author.py:109-111): the
   existing shared loop gains one check per label — the value must be
   Path-normalization-stable, not end in a slash, and contain no `..`
   component: `value != str(Path(value)) or value.endswith("/") or
   ".." in Path(value).parts` → refuse. The stability half rejects
   trailing slashes (`/x/y/`), interior duplicate slashes (`/x//y`),
   and `/./` segments; the `endswith` half is load-bearing for exactly
   two inputs — `/` and `//`, the only normalization-stable strings
   ending in a slash (`str(Path("//")) == "//"` under POSIX), where
   `root="//"` would emit `///capture-<kind>.json` that both
   verifier-side normalizations collapse to `/capture-<kind>.json`,
   recreating the false-refusal middle state; the `..` conjunct
   (fix round 1, verifier-confirmed) closes a DIFFERENT failure mode:
   `..` is normalization-stable and textually symmetric on both
   verifier sides, but `safe_fs` (`_absolute_parts`,
   packages/common/safe_fs.py:597-602) rejects any `..` component in
   the no-follow walkers that BOTH the supervisor's first capture
   write (supervisor.py:1122-1136 → atomic_write_bytes_no_follow) and
   the verifier's artifact reads (live_evidence.py:437) use — a `..`
   root authors fine, passes prearm (which normalizes first), then
   aborts inside the one-shot replay window with "Unsafe path
   component: '..'". Together the conjuncts give the probe property:
   for every accepted root R, `str(Path(f"{R}/x")) == f"{R}/x"`, and
   every accepted root's parts survive the no-follow walkers. Applied
   to BOTH `repo` and `root` in the one shared clause (a
   trailing-slash repo poisons command argv paths against the
   `expected_executable` literal pins the same way root poisons
   capture paths). `PlanAuthorError` message names the label, the
   offending value and its canonical rendering. Deliberately still
   accepted, recorded: LEADING double slash (`//x` — POSIX preserves
   exactly two leading slashes, normalization-stable, symmetric on
   both verifier sides, and its parts carry no `..`/anchor component
   so the no-follow walkers accept it). `capture_repo` (hermetic-only
   kwarg, value-pinned by the verifier anyway) and
   `--schema-dump-host`/`--schema-dump-container` (recorded verbatim
   into command artifact associations, compared at the same verbatim
   :1439 site, no canonicality guard — routed to a follow-up issue)
   stay unvalidated, recorded.
2. **The PR #1264 trailing-slash test rewires its construction**
   (tests/test_node27_timeseries_compression_live_evidence.py:6120-6161):
   it can no longer obtain the double-slash spelling from
   `build_run_plan` (that call now raises). The test keeps its full
   discriminating power by synthesizing the spelling directly — copy
   the bundle's `sizes_post` capture argv, rewrite `--evidence-dir` to
   `f"{tmp_path}//capture-artifacts"`, set `output_path` to
   `f"{tmp_path}//capture-sizes_post.json"` (rsplit still derives the
   matching double-slash sibling), and keep the identical
   `_replace_produced_artifact`/`_replace_capture_argv` flow and the
   identical anchor assertions (`message == "supervisor capture output
   path differs"`, no evidence-dir gate wording). Docstring rewritten
   honestly: the pin now guards the verifier's verbatim textual posture
   itself (a `Path.parent` refactor still reddens it via the
   dirname-swap divergence), no longer the claim that the production
   author emits this spelling — after this change it cannot.
3. **New tests**: parametrized `PlanAuthorError` negatives for
   root/repo × trailing-slash/double-slash/dot-segment/dot-dot-segment
   (message names label + canonical rendering); a canonical-root
   positive (clean root still authors; the existing twelve-kind
   positive control unchanged); a structural pin that `DEFAULT_ROOT`
   and `DEFAULT_REPO` are themselves Path-normalization-stable (the
   guard must never refuse the module's own defaults); the `//x`
   leading-double-slash boundary stays a recorded positive.
4. **Docs/spec wording sync** (acceptance requires it for this route):
   one ADDED requirement in the `hypertable-compression` spec (plan
   author rejects non-canonical roots so recorded plan paths are
   canonical byte-for-byte); one sentence in the runbook's plan-author
   section (custom `--root`/`--repo` must be canonical absolute paths —
   the authorized command uses defaults and is unaffected).

### Out of scope (verbatim-preserve surface)

- The two verbatim comparisons (:1534, :1439) and the ledger-side
  normalization (:437/:463/:510/:521): zero diff — the alternative
  route (normalize both sides) is explicitly rejected in the issue and
  here.
- The #1263 relational gate and its `rsplit` posture: zero diff (the
  rewired test continues to guard against the `Path.parent` refactor).
- `capture.py`, `supervisor.py`, `bundle_author.py`,
  `live_evidence.py` (entirely), `schemas/**`,
  `tests/test_node27_timeseries_compression_capture.py`,
  `tests/test_node27_timeseries_compression_supervisor.py`: zero diff.
- No dedicated `tests/test_..._plan_author.py` file: all plan_author
  oracles (drift guards, twelve-kind control) already live in the
  live_evidence test file; the new tests co-locate there, recorded.

## Impact

- Affected spec: `hypertable-compression` (one ADDED requirement).
- Affected code: `scripts/node27_timeseries_compression_plan_author.py`
  (one validation clause), `tests/test_node27_timeseries_compression_live_evidence.py`
  (one test rewired + new negatives/positives/structural),
  `docs/runbooks/tier-node27-timeseries-storage.md` (one sentence).
- Behavior change is authoring-time refusal only: no previously
  PASS-able bundle is affected (double-slash plans could never PASS —
  that is the defect), and the runbook's authorized command uses the
  canonical defaults. Fully hermetic verification.
- Checked, no MODIFIED needed: the archived #1263 relation scenario
  (openspec/specs/hypertable-compression/spec.md:448-region, "the plan
  author derives both fields from the same `--root`, so all
  plan-author-authored plans satisfy the relation") stays true after
  this change — the acceptance item pointing at it is satisfied by
  verification, not edit.
