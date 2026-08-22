# Design

## Risk triage

- Fixture level: **compact**. Test-only diff, one file, no production code, no
  runtime/contract/schema/DB/security surface.
- Divergence from any upstream `Suggested fixture level`: none recorded — #1717
  carries no suggested level.
- Risk packs selected: **oracle integrity** (mandatory here, see D3).
- Risk packs not selected, with reason:
  - Geospatial/CRS, hydro-met windows, SHUD numerics, PostGIS/Timescale, Slurm
    lifecycle, provider snapshot reproducibility, run-manifest provenance,
    display identity — none of these surfaces appear in the diff; the change
    adds no production behavior and no I/O outside pytest `tmp_path`.
  - Concurrency/atomicity: the *subject* is a concurrency guard, but the diff
    adds no concurrency; both tests are single-threaded and drive the sequence
    through a deterministic monkeypatch counter, not through real racing.

## Must-preserve behavior

- `packages/common/provider_atomic.py` stays byte-identical. Any diff there
  invalidates this change's entire premise.
- The remaining 294 tests in `tests/test_scheduler_file_provider_refresh.py`
  stay green on both platforms.
- The reason string `provider_preimage_changed` and the guard's three disjuncts
  at `provider_atomic.py:139-143` are the contract under test, not something to
  be adjusted to fit the test.

## Seams under test

- Module-attribute seam `provider_atomic_module.read_bytes_limited_no_follow`.
  It is shared by both call sites (`capture_provider_preimage:99` and
  `read_provider_snapshot:135`), which is precisely why call **ordering** —
  not merely patching — is the thing the test must control.
- `os.utime(..., ns=...)` as the metadata seam, to force `before == after`
  independent of filesystem timestamp granularity.

## Decisions

### D1 — Fire on the second call, not the first

Call 1 is `capture_provider_preimage(before)`'s own read at `:99`. Injecting
there is the original defect. The replacement must fire on call 2 (`:135`), so
`before` holds the pre-replacement digest and `content` holds the
post-replacement bytes.

### D2 — Restore bytes *and* `mtime_ns` before the `after` capture (ABA)

`ProviderPreimage` carries a `sha256` field, so `before != after` fires on the
digest alone whenever the file is left replaced. That makes the
issue's own 建议修法 section (replace on call 2, no restore) unable to satisfy
the issue's **acceptance criterion 2**: the mutant with the `:142` content-hash
comparison deleted still raises, via `before != after`. Measured, both shapes,
same machine:

| shape | unmutated | `:142` hash-compare deleted |
|---|---|---|
| issue's 建议修法 (replace on call 2, no restore) | raises `provider_preimage_changed` | **still raises** — criterion 2 fails |
| ABA (replace on call 2, restore bytes + `mtime_ns` before call 3) | raises `provider_preimage_changed` | **NO RAISE** — criterion 2 holds |

So the ABA shape is not decoration; it is the only shape that satisfies the
issue's own acceptance criteria. This is a recorded deviation from the issue's
建议修法 section; the acceptance criteria themselves are unchanged and are met.

The `mtime_ns` restore is **not an exotic attacker**. It is the deterministic
simulation of what ext4's 4 ms tick does for free: a same-size replacement
inside one tick leaves every `ProviderPreimage` metadata field identical, and
the content-hash disjunct is the guard's only remaining defense. The utime call
buys determinism on APFS too, nothing else.

### D3 — Oracle integrity is the governing risk

The literal shape of this PR is "a test that is red on the production oracle
gets edited until it is green". The rebuttal is a mutation receipt on both
platforms: delete the `:142` content-hash comparison, the ABA test must go red;
restore, it must go green. Without that receipt this change is indistinguishable
from silencing a failure, and reviewers should treat it as such.

### D4 — Two tests

- ABA test: **isolates** disjunct 3 (`sha256(content) != before.sha256`). It is
  the only one of the three that can raise, which is what makes D3's mutation
  proof possible.
- Different-length test: covers the realistic "replaced and left replaced"
  case, deterministically on both platforms via `size`. It does **not** isolate
  a disjunct — with the replacement left in place, `before != after` (size and
  digest) and `sha256(content) != before.sha256` both fire. Its value is that
  the divergence no longer rides on `mtime_ns` granularity, which is the
  accident the current test depends on.

This is where the issue's "用不同长度的替换内容" suggestion belongs. Putting a
different length into the ABA test instead would re-arm the `size` field and
break D3's mutation proof.

Known limit, routed at close: disjunct 1 (`before != after`) still has no test
that isolates it — that needs a scenario where the content digest matches but a
metadata field does not (for example a `chmod` between the payload read and the
second capture). Out of scope here; #1717's acceptance criteria are all about
the content-digest disjunct.

### D5 — Assert the call count

The counter-based hook silently no-ops if the read sequence is ever refactored,
which would restore exactly today's vacuous green. Both tests assert the
observed count (3 for the ABA test) so that failure is loud.

## Evidence mapping

| Acceptance criterion (#1717) | Evidence |
|---|---|
| Test green on node-27 **and** macOS | `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` on both |
| Mutation: deleting `provider_atomic.py:142`'s content-hash comparison turns the test red on **both** platforms | mutate → run → restore → `git status` clean, transcript captured per platform |
| Whole file green on node-27 | node-27 run of the full test file, ≥296 passed, 0 failed |

Red baseline already captured on node-27 at master `34940600`:
`1 failed, 294 passed in 46.53s`.

## Non-goals

- Any change to `provider_atomic.py` (D1/D3).
- Any other known-red on the node-27 full suite — #1707 is a separate change.
- Making the guard itself stronger, or adding real concurrency to the test.
- The `atomic_replace_provider_bytes` writer-CAS path, which has its own tests.
