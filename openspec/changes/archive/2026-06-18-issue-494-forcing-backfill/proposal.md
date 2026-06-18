## Context

Issue #494 is part of #492 and follows #493. New q_down publications now validate and mirror their referenced forcing packages to `NHMS_OBJECT_STORE_COPYBACK_ROOT`, but runs that were already parsed, frequency-computed, or published before that fix can still reference forcing packages that exist only on node-22's local `OBJECT_STORE_ROOT`.

The remediation needs an auditable operator command that scans historical q_down-capable runs, reports what can be copied, and only writes to the shared object-store when an operator explicitly chooses apply mode.

Risk triage:

- Issue type: feature
- Project profile: NHMS
- Blast radius: high
- Fixture level: expanded
- Repair intensity: high
- Why: CLI/operator entrypoint, DB-backed run discovery, shared object-store writes, #493 path/checksum validation reuse, legacy forcing key rejection, rerunnable production remediation.

## Goals

- Add a one-shot, default dry-run backfill command for historical forcing package copyback.
- Scan `hydro.hydro_run` rows in `parsed`, `frequency_done`, or `published` state that have q_down publish value, then join to `met.forcing_version`.
- Dedupe work by the normalized forcing package key validated by #493 helper logic.
- In dry-run, produce a JSON report without changing `NHMS_OBJECT_STORE_COPYBACK_ROOT`.
- In `--apply`, copy only packages that pass #493 path, source-tree, manifest, and checksum validation.
- Report counts and per-failure details suitable for node-22 audit and rerun.
- Document node-22 execution, required environment variables, rerun behavior, and rollback boundaries.

## Non-Goals

- No guessing or migration of legacy keys such as `forcing/{forcing_version_id}/`.
- No DB schema migration and no mutation of hydro or met rows.
- No production execution as part of tests or CI.
- No change to the q_down publish-time copyback behavior from #493 except extracting reusable helper surfaces if required.
