# Make self-test seams structurally visible to the verifier (#1250)

## Why

PR #1249 (#1090) closed the producer side: a deviating `--docker`
without `--self-test-docker-seam` refuses at capture time
(capture.py:511-514). But the seam tokens themselves are invisible to
every downstream forensic gate: supervisor validates capture argv
concrete-only (supervisor.py:593, :892 — no `exact=True`, unlike the
command side at :557), the verifier applies only `_concrete_argv`
(live_evidence.py:1001), the ledger↔plan binding is pure equality
(:1253), and the literal pin at :1620 checks the RECORDED constant
(`version_argv`, hardcoded HOST_DOCKER_CLI at capture.py:532-533),
not the EXECUTED argv. Result: a run plan whose capture argv carries
`--docker /opt/whatever/docker --self-test-docker-seam` executes the
rogue binary yet records `/usr/bin/docker`, and every gate passes.
The sibling `--self-test-free-bytes` (capture.py:773, consumed at
:490-502) lets a bundle carry a fabricated 300 GiB headroom through
the `free_bytes >= MIN_FREE_BYTES` gates (live_evidence.py:1862,
:2017) — the rollback-feasibility precondition. The #1069/#1090
threat class — "the verifier trusts what I recorded, not what I ran"
— remains reachable inside a PASS verdict, one hidden flag away.

## What Changes

Recommended route from the issue (zero schema changes), single
verifier-side rejection plus test restructure:

1. `scripts/node27_timeseries_compression_live_evidence.py`: in
   `_validate_supervisor_execution`'s capture loop (:1001 area),
   reject any `run_plan.captures[*].argv` token that starts with the
   frozen prefix `--self-test-` (module constant
   `SELF_TEST_SEAM_PREFIX = "--self-test-"`), raising `EvidenceError`
   with a lower-case message naming the offending token — matching
   the file's single-string message convention. PREFIX, not a manual
   enumerated list: this is a deliberate strengthening of the
   issue's "集中式常量清单" — a registry can silently miss a new
   seam; a prefix rejection makes every future `--self-test-*` flag
   rejected by construction, and the registration burden inverts
   into a structural test (point 3). The ledger side needs no
   separate check: the equality binding at :1253 already forces
   ledger capture argv to equal plan capture argv, so a seam can
   only reach the ledger through a plan that is now rejected first.
2. `tests/test_node27_timeseries_compression_live_evidence.py`
   (`test_real_state_machine_bundle_verifies_task_4_5_pass`, :4901):
   resolve the issue's design tension via **option 1 implemented
   with the test's own existing pattern** — the command side already
   swaps stub argv into `plan_exec` post-deepcopy and post-hoc
   rewrites ledger `child_exit` events to production identities
   (:5057-5062). Do the same for captures: plan_prod stays seam-free
   (so `run_plan_id`, the bundle's run plan, and the verifier all
   see a production plan), the seam flags are appended to
   `plan_exec["captures"][*].argv` only (the state machine still
   really executes capture.py's argparse with the seams on CI), and
   the ledger capture events are post-hoc rewritten back to the
   seam-free plan_prod argv so the :1253 equality binding holds.
   Fidelity is pinned: before the rewrite, the test asserts the
   executed ledger capture argv actually carried the seam tokens
   (the rewrite must be demonstrably real, never vacuous).
   The verifier-side acceptance seam (option 2) is REFUSED — it
   would relocate the same invisibility hole one layer up.
3. New tests: (a)/(b) negative — a PASS-shaped bundle whose plan
   capture argv (and equality-bound ledger event) carries
   `--self-test-docker-seam` / `--self-test-free-bytes` is rejected
   by `verify_bundle` with `EvidenceError` naming the token;
   (c) structural registration — every `help=argparse.SUPPRESS`
   optional flag in capture.py's parser matches
   `SELF_TEST_SEAM_PREFIX` (a future hidden flag that dodges the
   prefix reddens this test, closing the leak-by-forgetting hole
   the issue's registry test aimed at).
4. `scripts/node27_timeseries_compression_supervisor.py` is
   deliberately UNTOUCHED — recorded decision, not an omission: the
   hermetic executor MUST be able to validate and run seam-carrying
   plans (that is the seams' reason to exist), so the forensic
   boundary is the verifier; a supervisor-side gate would break
   `test_authored_plan_survives_the_real_state_machine_and_verifier_validators`
   (capture tests :627, seam injected at :664) and the e2e execution
   path itself. The issue lists the supervisor gate as optional; the
   explorer evidence shows it is structurally incompatible.

Spec relation: the delta lands in the existing
`hypertable-compression` capability, which already holds the sibling
requirements of this domain (the deviating-docker capture refusal
and the no-unwired-trust-boundary-validator rule); it refines the
forensic-trust posture of the live-evidence lane (the #1069 G-series
"validate execution, not records" class); no existing requirement
conflicts — the capture-argv surface had no exactness requirement
anywhere.

Known accepted cost (recorded so review does not re-litigate it):
after the restructure the e2e-verified `plan_prod` carries capture
argv that a real run would refuse (stub docker path without the
seam) — the property "verified capture argv == executed capture
argv" is given up for captures, exactly as the command side already
gave it up at :5057-5062. This is the issue's own named cost of
option 1; option 2 (verifier acceptance seam) is strictly worse and
stays refused.

## Non-goals

- Removing the seams themselves (hermetic CI depends on them).
- `schemas/timeseries_compression_live_evidence.schema.json` changes.
- Any change to capture.py (producer guard from #1249 stands) or
  supervisor.py (recorded decision above).
- `--container`-style other injection surfaces (separate issue if
  found).
- The exact-argv-per-capture-kind alternative (M/L scale, brittle
  path-variable pins; rejected in favor of the S-scale seam gate).
