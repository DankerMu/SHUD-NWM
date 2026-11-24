# Tasks

Spec-only change; the issue body's `Verification:` is the evidence floor.

- [x] 1.1 Rewrite scenario "Retry gets new job_id" to the `<base>_retry_<n>`
  form.
- [x] 1.2 `openspec validate state-retry-job-id-format --strict --no-interactive`
  passes.
- [x] 1.3 After archive, `grep -n "UUID" openspec/specs/job-retry-mechanism/spec.md`
  no longer describes retry job ids.
