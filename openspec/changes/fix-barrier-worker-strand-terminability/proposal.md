## Why

Four Barrier-based pytest concurrency harnesses can strand non-daemon peer threads when any participant fails before reaching the barrier. A bounded assertion or join in the main test is not enough: `threading._shutdown()` can still wait forever, turning the intended test failure into a hung local/CI run.

## What Changes

- Bound the Barrier protocol at the two affected `tests/test_gateway_reconcile.py` sites and the two affected `tests/test_scheduler_file_provider_refresh.py` sites.
- Surface pre-arrival worker failures and broken-barrier outcomes before substantive race assertions.
- Prove every started peer exits and the whole pytest process remains terminable, including a bounded subprocess/mutant proof.
- Preserve all four existing concurrency oracles, participant counts, real code-under-test calls, and deterministic winner/state assertions.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `real-integration-test-matrix`: Barrier-mediated test harnesses gain bounded whole-run termination and worker-error attribution requirements, complementing the existing spin-wait harness contract.

## Impact

- Test harnesses: `tests/test_gateway_reconcile.py` and `tests/test_scheduler_file_provider_refresh.py`.
- Contract/tests: OpenSpec delta and assertion-level failure-injection/mutation evidence.
- No production module, runtime behavior, dependency, global pytest policy, CI workflow, selector rule, DB schema, Slurm scheduling, or display behavior changes.
