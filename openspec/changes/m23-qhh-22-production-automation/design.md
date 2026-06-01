## Context

M20 defines the generic production scheduler direction, M21 defines the QHH hydro-met/ops MVP, and M22 defines the two-node Docker/read-only display boundary. Current local evidence shows node 22 is only partially wired:

- `nhms-compute-compute-api-1` runs with `NHMS_SERVICE_ROLE=compute_control`, but `SHUD_EXECUTABLE=/bin/true`.
- The compute container does not have Slurm CLI tools, and the configured Slurm gateway points to the compute API itself instead of a working gateway path.
- The live DB has canonical meteorology but no active `core.model_instance`, no `met.forcing_version`, no station forcing rows, no hydro runs, no river time series, and no pipeline job/event history.
- The processed QHH package does exist under `NHMS_BASINS_ROOT`, including `qhh.tsd.forc`; therefore the missing piece is production bootstrap and dynamic per-cycle forcing generation, not regenerating the basin package.
- `nhms-pipeline plan-production` can plan only when explicitly given the configured workspace root; the CLI currently falls back to `.nhms-workspace`, which is unsafe inside the container.

The corrected architecture is: rSHUD/AutoSHUD informs the static SHUD project/forcing format; SHUD performs hydrologic computation; the processed basin supplies fixed forcing stations and river/output identities; each forecast cycle downloads fresh data and interpolates/extracts that data to the fixed stations before running SHUD.

## Goals / Non-Goals

**Goals:**

- Make QHH on node 22 a complete automated production slice from forecast discovery through DB/published outputs.
- Use existing generic scheduler/orchestrator and worker modules rather than depending on QHH diagnostic shell scripts.
- Make all live blockers explicit and machine-readable: unavailable forecast source, missing model bootstrap, missing SHUD library, unhealthy Slurm path, parser/publish failure.
- Keep artifacts and evidence under the repository `artifacts/` tree or `/scratch/frd_muziyao`, with display products under `/ghdc/data/nwm/published`.
- Preserve node 27 as a readonly display plane that consumes database state and published artifacts only.

**Non-Goals:**

- No nationwide rollout or new basin onboarding beyond QHH.
- No frontend feature work except preserving data contracts that 27 already consumes.
- No fake SHUD success, synthetic forcing rows, or placeholder Slurm receipts.
- No attempt to make Docker itself a Slurm cluster; a host Slurm gateway is acceptable for MVP if it is preflighted and documented.

## Decisions

### 1. Bootstrap fixed model state before scheduling

The scheduler must treat "no active model" as a blocker, not as an empty success. A bootstrap command or service task will import/publish the QHH Basins package, create/activate the model instance, seed fixed forcing stations from `qhh.tsd.forc`, and seed output river/segment identities before candidate discovery can submit work.

Alternative considered: keep invoking `scripts/run_qhh_cycle.sh` because it can perform several bootstrap steps. Rejected for production automation because M20 requires generic scheduler behavior and because diagnostic scripts make idempotency, locks, and pipeline evidence harder to prove.

### 2. Dynamic forcing targets fixed SHUD stations

Fresh GFS/IFS cycles are downloaded and canonicalized every run. The forcing producer then maps canonical grids to fixed `met.met_station` rows with `station_role="forcing_grid"` and writes `met.forcing_version`, `met.forcing_station_timeseries`, and SHUD forcing package files. This matches the processed basin contract without pretending stations were pre-extracted for future forecasts.

Alternative considered: require regenerating station definitions per forecast cycle. Rejected because the processed basin already defines SHUD forcing stations; only meteorological values are dynamic.

### 3. Real runtime readiness is a preflight gate

`/bin/true` is treated as invalid for production. The runtime preflight must resolve the configured SHUD executable, required shared libraries, project inputs, workspace/object-store/published roots, and Slurm gateway/host submission path before a candidate is submitted. Missing Slurm CLI inside the app container is acceptable only when a configured gateway or host service can submit and account for jobs.

Alternative considered: allow local foreground SHUD execution as the first production mode. Rejected for this change because the user specifically wants SHUD Slurm running on node 22; local execution can remain a deterministic test fixture, not the business path.

### 4. Published artifacts are the cross-node boundary

Node 22 writes logs, manifests, and display products under the configured published artifact root and records supported `published://` or allowlisted URIs in DB state. Node 27 does not read private workspaces, Slurm files, or compute-only paths.

Alternative considered: share the entire workspace through NFS. Rejected because M22 already established a narrower readonly display boundary and because private workspaces may contain intermediate files, secrets, or unstable paths.

### 5. Scheduler operationalization includes env defaults and service loop

The production scheduler CLI must honor `WORKSPACE_ROOT`, `OBJECT_STORE_ROOT`, and evidence root env values when flags are absent. Docker/systemd `scheduler-once` and continuous/timer modes must set locks, evidence directories, and source/model filters explicitly enough to avoid duplicate submissions and accidental system-disk output.

Alternative considered: document manual `--workspace-root` invocation only. Rejected because the requested target is business automation, not an operator-run diagnostic command.

## Risks / Trade-offs

- Forecast source 403/lag or partial variables can block a cycle. Mitigation: source availability and canonical completeness are recorded as blocked/unavailable without marking readiness true.
- SHUD binaries on node 22 may have missing shared libraries. Mitigation: binary/library preflight fails before pipeline mutation or Slurm submission and records exact missing libraries without leaking secrets.
- Slurm may be reachable only from the host, not the container. Mitigation: support a bounded gateway/host-service pattern with health/accounting receipts instead of requiring Slurm CLI in the app image.
- Existing DB bootstrap scripts may be QHH-specific. Mitigation: M23 allows QHH-specific bootstrap for this closure, but scheduler runtime must consume generic model/registry records afterwards.
- Live E2E can be slow or blocked by external systems. Mitigation: tests separate deterministic fixtures from opt-in live receipts and cannot claim business readiness from deterministic-only runs.

## Migration Plan

1. Define the end-to-end production identity/status/URI contract so scheduler, forcing, Slurm, parser, publisher, and evidence use the same run/model/source/cycle keys.
2. Add/fix bootstrap commands and validation so QHH model/station/output identities are present and idempotent in the node-22 DB.
3. Fix scheduler env defaults and Docker scheduler commands so dry-run works without manual workspace flags.
4. Prove fresh forecast download/canonical readiness and station forcing generation for at least one accepted QHH cycle.
5. Configure real SHUD executable/library path and Slurm gateway/host submission path; add pre-submit preflight and accounting/log receipt capture.
6. Parse SHUD output, publish q_down display products/logs/manifests, separately mark frequency/flood products unavailable or ready, and validate strict run identity for downstream display.
7. Add node-22 E2E command/tests and update runbooks with pass/blocked evidence locations.

Rollback is operational: disable scheduler timer/container, leave DB terminal evidence intact, and revert to diagnostic scripts only for investigation. Published artifacts are append-only by run identity and should not be deleted as rollback unless explicitly marked invalid.

## Open Questions

- Which SHUD binary is the accepted production executable on node 22 after library resolution: `SHUD/shud`, `/scratch/frd_muziyao/SHUD-GPU/shud_omp`, or another managed path?
- Is the production Slurm gateway expected to run as a host systemd service, an API sidecar with mounted Slurm/Munge, or direct host CLI invoked outside Docker?
- What GFS/IFS horizon and cycle lag should be the default business policy for QHH once live source availability is stable?
