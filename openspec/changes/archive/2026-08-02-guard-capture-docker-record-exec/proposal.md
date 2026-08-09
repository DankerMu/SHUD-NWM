# Guard the capture RECORD/EXEC docker split with a fail-closed assertion (#1090)

## Why

`_capture_schema_dump_list` records forensic `version_argv`/`list_argv`
with the compile-time constant `HOST_DOCKER_CLI` (capture.py:514-515)
while the subprocesses actually run `ctx.docker` (:512-513, :518-530),
injectable via the required `--docker` seam (:747). Today the two
coincide only because the sole production caller defaults
`capture_docker="/usr/bin/docker"` (plan_author.py:101/:304). Any
future caller passing a different docker binary produces a bundle that
attests `/usr/bin/docker` while having executed something else — a
silent false-attestation, and the verifier's literal pin
(live_evidence.py:1616-1623) would accept it. This is a miniature
replay of the #1069 G6..G14 failure class: the verifier trusts what
was recorded, not what ran, and the equivalence is maintained by
convention instead of code.

## What Changes

Issue #1090's recommended route — zero schema change, zero verifier
change, RECORD==EXEC promoted from caller-default coincidence to a
code-enforced invariant:

1. capture.py: hidden boolean opt-in flag `--self-test-docker-seam`
   (`store_true`, `help=argparse.SUPPRESS`) + Context field
   `self_test_docker_seam: bool = False`, mirroring the
   `--self-test-free-bytes` precedent (:113-117, :755, :779) exactly.
   At the TOP of `_capture_schema_dump_list`, before any subprocess or
   bundle write: if `ctx.docker != HOST_DOCKER_CLI` and the seam is
   not set, `raise CaptureError` with a message naming the observed
   `ctx.docker` value. The :504-511 comment is updated: the RECORD
   names the production binary AND the split is now enforced (a
   deviating EXEC requires the explicit self-test opt-in).
2. plan_author is NOT changed and never emits the seam flag — this is
   the load-bearing design decision. Auto-emitting the flag whenever
   `capture_docker != "/usr/bin/docker"` (the superficially DRY route)
   would let any future production caller silently re-open the exact
   false-attestation hole this change closes. Instead the two hermetic
   test execution sites post-patch the `schema_dump_list` capture argv,
   mirroring how `--self-test-free-bytes` is threaded today
   (live_evidence tests :4991-4993 patch `plan_prod["captures"]`
   post-hoc; plan_author has zero knowledge of that flag either).
3. Tests: (a) the two genuine stub-docker execution sites —
   `test_authored_plan_survives_the_real_state_machine_and_verifier_validators`
   (capture tests) and the merged G-series hermetic e2e
   (live_evidence tests) — append `--self-test-docker-seam`
   to the `schema_dump_list` capture argv only; (b) NEW negative test
   in capture tests: run the capture producer with a RUNNABLE stub
   docker at a path != HOST_DOCKER_CLI and NO seam flag → non-zero
   exit, the error names the observed docker value, stdout is EMPTY
   (the producer emits documents on stdout via `_emit` :203-209, it
   writes no document file) and the `--evidence-dir` directory —
   created unconditionally at main() :765 before dispatch — stays
   empty; (c) NEW guard test: `plan_author.build_run_plan` with
   production defaults emits NO capture argv containing
   `--self-test-docker-seam`, and the `schema_dump_list` capture argv
   carries `--docker /usr/bin/docker` — this pins the load-bearing
   "plan_author never emits the seam" invariant in code instead of
   convention; the same test ALSO builds a plan with a deviating
   `capture_docker` and asserts the seam flag is still absent, because
   the production-default plan alone cannot see a deviation-conditional
   auto-emission (review round 1, verifier CONFIRMED); (e) NEW
   in-process pass-through test binding spec scenario 3: with
   `docker == HOST_DOCKER_CLI` and no seam, `_capture_schema_dump_list`
   falls through the guard to the pre-existing schema-dump-args
   CaptureError — observable locally with no docker binary (review
   round 1, verifier CONFIRMED); (d) the capture argv gains a flag only in test plans —
   safe because captures are structurally excluded from every
   exact-argv gate (supervisor `_assert_exact_argv` covers only
   `commands` kinds; captures get `_assert_concrete_argv` without
   `exact=True` at supervisor :593/:892, verifier :982-1006 same).

## Non-goals

- Removing the `--docker` seam (legitimate test-only redirection) or
  changing `HOST_DOCKER_CLI`, `plan_author.py` defaults, or the
  recorded `version_argv`/`list_argv` content.
- The alternative both-argvs-recorded route (schema bump + verifier
  branch) — over-engineering for the current single-caller reality,
  per the issue's own tradeoff analysis.
- Other `ctx.docker` call sites in the same function (:518-530
  `docker inspect`/`readlink`/`sha256sum`): they do not write forensic
  argv fields, and the single assertion at function top covers their
  EXEC path anyway since it runs before them — no separate guard
  needed.
- `_container_state` (capture.py:263-285, used by the preflight
  kinds via `_preflight_core` :358): runs `docker inspect` with
  `ctx.docker` and stays UNGUARDED by design — it records measured
  container facts, not an argv attestation, so there is no RECORD/EXEC
  attestation pair to protect; per the issue's boundary it is out of
  scope, and hermetic preflight tests keep injecting stub docker there
  with no seam flag.
- `--container` or other seams; verifier/schema files;
  `schemas/timeseries_compression_live_evidence.schema.json`.
