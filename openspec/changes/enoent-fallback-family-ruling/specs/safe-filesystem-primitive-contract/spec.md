## ADDED Requirements

### Requirement: Path canonicalization SHALL resolve strictly unless named, and a tolerated non-strict fallback SHALL be justified by dereference

Every function that canonicalizes a filesystem path with `os.path.realpath` SHALL either pass `strict=True` on at least one of those calls, or appear in a named exemption list that records which admission clause below justifies it; a function in neither state is a violation. Passing `strict=True` makes a symlink loop available to the handler as an `OSError` carrying an errno, rather than folding it into a partially resolved product before any handler can see it; it does not by itself mean the loop is reported, because a handler MAY still admit the `ENOENT` arm under the clauses below. `Path.resolve()` SHALL NOT be used as a symlink-loop predicate in this family: within the interpreter range this project supports, its non-strict form does not raise on a symlink loop on CPython 3.13+, and its strict form raises an errno-less `RuntimeError` on CPython 3.12 and earlier, so neither form states the same truth on both arms.

For the `<missing>/../<loop>` input class this requirement adjudicates — the class for which non-strict `os.path.realpath` returns a partially resolved product instead of raising — a site MAY admit the `ENOENT` arm by falling back to the non-strict form only when at least one of three clauses holds, and the clause relied upon SHALL be recorded at the site. This requirement asserts nothing about input classes it does not adjudicate: an embedded NUL byte, or a relative path whose working directory has been removed, make non-strict `os.path.realpath` raise, and those escapes are governed elsewhere.

Clause one, downstream dereference: before any decision derived from the normalized product is committed, the path under adjudication SHALL be dereferenced against the kernel (an existence probe, an `lstat`, an `open`), and a fault there SHALL change the verdict. The probe MAY target the raw input rather than the normalized product, because kernel resolution makes the two equivalent on every input for which the probe can succeed. Clause two, provable coincidence: the normalized product and the path the consequent action actually acts upon SHALL coincide on every input for which that action can succeed; this clause quantifies over inputs only and SHALL NOT be relied upon where a concurrent writer can change the path between the check and the action. Clause three, comparison operand: the normalized product SHALL be used only as a containment base or an identity-comparison operand, carrying no assertion that the path exists or is usable, while the value judged against it is dereferenced before the verdict is committed.

A site satisfying none of the three clauses SHALL loop-filter its admission, re-resolving the non-strict fallback strictly and keeping the admission only on a second `ENOENT` or a clean resolution. Whether a handler splits on errno or catches `OSError` bare is a separate question that this requirement does not adjudicate; a bare handler is broader-tolerant under the same dereference guarantee, not less safe.

#### Scenario: A new canonicalization site that never resolves strictly and is not named is refused

- **GIVEN** a function under `services/`, `workers/`, `packages/`, or `apps/` that calls `os.path.realpath`
- **WHEN** none of that function's `os.path.realpath` calls passes `strict=True` and the function is absent from the exemption list
- **THEN** the family guard reports the function as a violator, so the violator set is non-empty and the guard fails

#### Scenario: An exempt site is admitted only by name and reason

- **GIVEN** a canonicalization site with no strict call whose admission rests on a recorded clause
- **WHEN** the family guard enumerates violators
- **THEN** the site is exempt only through a named entry carrying the clause it relies on, never through a shape heuristic, so removing the entry restores the violation

#### Scenario: A loop behind a missing component stays admitted where the path is dereferenced

- **GIVEN** a configured path of the `<missing>/../<loop>` shape at a site whose adjudicated path is later opened or probed
- **WHEN** the site canonicalizes that path and takes the `ENOENT` fallback
- **THEN** the fallback product is admitted without a blocker at canonicalization time, and the loop is reported by the later dereference rather than by this step
