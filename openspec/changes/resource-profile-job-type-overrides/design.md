# Design

## Today

```text
resolve_model_resource_profile(profiles, model_id, *, partition_override, exclude_nodes):
    resolved = default
    resolved.update(overrides[model_id])          # when it is a mapping
    overlay partition_override / exclude_nodes
    validate_resource_profile(resolved)
```

Two callers, both through `RealSlurmGateway.resolve_resource_profile(model_id)`:
the array submit (model of task 0) and the single-job render (the manifest's
model, when the caller passed no profile). The job type is known at both.

`load_resource_profiles` checks that `default` is a mapping and that
`overrides` is a mapping or absent. It ignores other top-level keys.

## Change

```text
load_resource_profiles(path):
    ... as today ...
    job_type_overrides: absent or null -> {}; not a mapping -> ConfigurationError
    every entry must be a mapping whose keys are within
        {cpus_per_task, memory_gb, shud_threads}, with cpus_per_task and
        shud_threads set together or neither, and whose values pass
        validate_resource_profile(entry, require_required=False)
        -> ConfigurationError otherwise, naming the job type and the
           offending keys (key names only, never values)

resolve_model_resource_profile(profiles, model_id, *, job_type=None, partition_override, exclude_nodes):
    resolved = default
    resolved.update(overrides[model_id])                 # unchanged
    resolved.update(job_type_overrides[job_type])        # new; after the model
    overlay; validate                                    # unchanged
```

`job_type` is keyword-only and defaults to `None` (no job-type entry
applied), so a caller that does not know a job type resolves as before.
`RealSlurmGateway.resolve_resource_profile(model_id, job_type=None)` passes
it through. The array submit passes its normalised job type; the single-job
render passes the `job_type` it was called with.

The profile that the array submit resolves is the one handed to the render
(`profile=profile`), so the render does not resolve a second time for
arrays; the single-job path resolves inside the render. Both must end up
with the job-type entry applied: tests go through both submit paths and
assert on the rendered script, not on the resolver alone.

`resolve_model_resource_profile` reads `profiles.get("job_type_overrides")`
defensively (`or {}`), because callers may pass an in-memory mapping that
never went through the loader. An entry that is not a mapping is ignored
there exactly as a non-mapping model override is today; the loader is what
refuses a malformed file. The scheduler's cohort split reads the file
through the same loader, so a malformed `job_type_overrides` fails the pass
before anything is submitted, as a malformed `overrides` does today, and
not halfway through a chain.

`resource_profile_override_model_ids` is not edited. A test pins that a
file with `job_type_overrides` yields the same id set and the same cohort
split as the file without it.

## Thread count

The gateway exports `SHUD_THREADS` and `OMP_NUM_THREADS` from the render
context's `shud_threads`, and the context is `{**manifest, **profile}`: the
resolved profile wins over a `shud_threads` carried by the manifest. So the
forcing entry sets both keys, `cpus_per_task: 1` and `shud_threads: 1`, and
the forcing task is rendered with one core and one thread. A job-type entry
that set `cpus_per_task` without `shud_threads` would leave the model's
thread count (4 or 12) on one core (`validate_resource_profile` has no
cross-field rule), so the loader refuses an entry that sets one of the two
without the other; the shipped entry is also pinned by a test that reads
the real `config/resource_profiles.yaml`. The solver's own thread count
comes from the registry; what this file decides is the exported
`SHUD_THREADS` / `OMP_NUM_THREADS` and the core request.

## The shipped file

```yaml
resource_profiles:
  default: ...            # unchanged
  overrides: ...          # unchanged
  job_type_overrides:
    produce_forcing_array:
      cpus_per_task: 1
      shud_threads: 1
```

with a comment that says: what the key is, the resolution order, the closed
key set, the measurement behind the entry, and that removing the entry is
the rollback.

## Deployment

1. Merge; the production checkout on w3 is updated at a pass boundary as
   usual. With the old gateway process still running, nothing changes (D7).
2. Restart the gateway inside the same deployment window
   (`docs/runbooks/w2-w3-compute-topology.md` section 6: timer stopped, no
   pass running), outside the stall-health probe window. The gateway keeps
   its job table in memory only (`RealSlurmGateway._jobs`), and an array's
   `array_task_count` there is what stops a partly accounted array from
   being folded to COMPLETED. So: after the fast-forward and before the
   timer is started again, wait until `squeue` lists no `nhms_*` job, then
   `systemctl --user restart nhms-slurm-gateway.service`, check that the
   health endpoint reports the `slurm` backend, then start the timer.
   Evidence that names the currently running gateway process (its start
   time and PID) is collected before the restart.
3. The next submitting pass is the live receipt: `sacct` for the forcing
   arrays (`ReqCPUS`, `AllocCPUS`, `ReqMem`, `Elapsed`, `TotalCPU`,
   `MaxRSS`, node), the node definitions (`scontrol show node`: `CPUTot`,
   `ThreadsPerCore`), tasks per node, stage wall against the 70-minute
   baseline, and the forecast arrays unchanged at 4 / 12 CPUs. Pass:
   `ReqCPUS=1` for every forcing task and `AllocCPUS=1` (both nodes are
   defined with `CPUs=40`, one per core, and today's four-CPU tasks show
   `AllocCPUS=4` on both; if w2 reports 2 because of its two threads per
   core, that is recorded as a finding about the node definition, not as a
   failure of this change).
4. Rollback: delete the entry from the file in a normal commit and sync; no
   restart.

## Alternatives rejected

- **Model wins over job type** (the first order listed in the issue): HLJ
  forcing would stay at twelve cores.
- **A separate `forcing` profile selected by the job type.** A second way
  of choosing a profile, with its own precedence question against model
  overrides, growing into one parallel profile per stage.
- **Hard-code `--cpus-per-task=1` in the forcing template.** No resolver
  change and no restart, but the resource request would then live in two
  places, and the exported thread count would still be the model's.
- **Raise the bound in the same change.** Two variables in one measurement.
- **Pin forcing to w3 with `exclude_nodes` in the entry.** Host names do
  not belong in the shared profile file; placement is a cluster matter.
