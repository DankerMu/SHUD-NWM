# Project Profiles

A project profile maps the generic risk packs and expanded-triggers in
`issue-risk-contract.md` onto one concrete project's surfaces. Profiles are
pluggable: pick the one that matches the repository, or use the **Generic**
profile. Nothing in the core workflow assumes a specific profile.

This file is the **shared template catalog**: the Generic default plus
SHUD/rSHUD/AutoSHUD examples. The **active** profile for a project is
project-local at `openspec/project-profile.md`, bootstrapped in Phase 0.0 and
maintained in Phase 0.5. It lives under `openspec/` (project content), so it
survives skill reinstalls and never accretes into this shared skill. Use the
sections here only as starting templates to copy from.

A profile contributes:

- entry surfaces (where changes and risk concentrate)
- contracts (what callers/consumers depend on)
- risk axes (how this kind of project breaks)
- typical evidence (what proves a change is safe)
- command entry points (the repo's real setup/check/lint/typecheck/test/build commands)
- a verification matrix (surface -> command -> evidence) consumed by Phase 2 and the Phase 8 completion self-audit
- optional domain risk packs (added to the core packs)
- optional domain expanded-triggers (added to the core triggers)

Selection: in Phase 0 step 0, infer the profile from repo files (language,
build system, directory layout, manifests). Record it in the fixture as
`Project profile: <name>`. When no profile matches, use `Generic`.

## Generic (default)

- Entry surfaces: the changed files plus their direct callers and any public entrypoint they sit behind.
- Contracts: public API/function signatures, CLI flags, config keys, data schema/format, return shapes, persisted state.
- Risk axes: input validation, error/rollback paths, backward compatibility, concurrency/ordering, resource bounds, security/path safety.
- Typical evidence: build/lint, unit plus integration tests on touched modules, boundary/failure-case tests, unchanged-consumer regression.
- Command entry points: discover the real commands from package scripts, Makefile/justfile targets, or CI workflow steps; record them, not generic placeholders.
- Verification matrix: one `<surface> -> <command> -> <evidence>` row per major surface, plus one row for the default build+test pipeline. Evidence names what proves the run (exit code, test report, produced artifact, log line).
- Domain risk packs: none beyond the core packs.
- Domain expanded-triggers: none beyond the core triggers.

Use Generic for ordinary application, service, library, or tooling repositories.
Extend it by writing a new profile section below when a domain has recurring
risk surfaces the core packs do not name.

## SHUD Solver

- Entry surfaces: `src/main.cpp`, `src/Model*`, `src/classes/*`, `Makefile`, example inputs.
- Contracts: model input files, config/control files, initial condition/restart files, output file names and units.
- Risk axes: numerical stability, CVODE/SUNDIALS integration, OpenMP/thread behavior, runtime performance, file output cadence, restart/IC compatibility, mass/water balance.
- Typical evidence: compile, small example run, output schema check, conservation/numerical sanity, crash/NaN guard.
- Domain risk packs: Numerical stability / conservation / NaN; Solver runtime / performance / threading.
- Domain expanded-triggers: `solver`, `CVODE`, `SUNDIALS`, `OpenMP`, `thread`, `restart`, `initial condition`.

## rSHUD Package

- Entry surfaces: `R/*`, `src/*`, `tests/testthat`, `DESCRIPTION`, exported functions.
- Contracts: R function signatures, return classes, column names, units, CRS, parser/writer formats, compatibility wrappers.
- Risk axes: public API compatibility, testthat/R CMD check, GIS geometry/CRS, timeseries parsing, SHUD input/output readers, Rcpp behavior, CRAN-sensitive dependencies.
- Typical evidence: focused testthat, `R CMD check` or package test command, parser round trip, fixture read/write.
- Domain risk packs: Geospatial / CRS / shapefile sidecars; Time series / forcing / temporal boundaries.
- Domain expanded-triggers: `Rcpp`, `S3`, `S4`, exported R function, compatibility wrapper, `CRS`, `geometry`, `projection`.

## AutoSHUD Pipeline

- Entry surfaces: `GetReady.R`, `Step*.R`, `Rfunction/*`, `SubScript/*`, `Example/*`.
- Contracts: project config, global/caller environment, Step artifacts, SHUD input files, GIS sidecars, forcing CSV and `meteoCov`.
- Risk axes: step ordering, legacy branch compatibility, `source()` environment, geospatial CRS/schema, NetCDF/raster discovery, file publish/rollback, external command paths.
- Typical evidence: focused Rscript tests, Step-style synthetic fixture, legacy branch smoke, output file/schema assertions.
- Domain risk packs: Geospatial / CRS / shapefile sidecars; Time series / forcing / temporal boundaries.
- Domain expanded-triggers: `NetCDF`, `raster`, `shapefile`, `CRS`, `geometry`, `projection`, `Step`, `config`, `global`, `source()`, generated SHUD input.

## Domain Risk Packs

These packs are contributed by scientific/geospatial profiles above. Add them to
the core Risk Packs only when the active profile lists them.

- Geospatial / CRS / shapefile sidecars
- Time series / forcing / temporal boundaries
- Numerical stability / conservation / NaN
- Solver runtime / performance / threading

## Authoring a Project-Local Profile

For a new project, do not edit this shared file. Instead, Phase 0.0 bootstrap
writes `openspec/project-profile.md` for that project, copying the closest
template below (or Generic) and filling the eight fields from a repo scan:

```text
Project profile: <name>
Entry surfaces: <where changes/risk concentrate>
Contracts: <what callers/consumers depend on>
Risk axes: <how this project breaks>
Typical evidence: <what proves a change is safe>
Command entry points: <setup/check/lint/typecheck/test/build -> real commands, or "none">
Verification matrix:
- <surface> -> <command> -> <evidence type>
Domain risk packs: <added packs, or "none">
Domain expanded-triggers: <added trigger keywords, or "none">
```

Keep domain packs and triggers in the project-local profile, not in
`issue-risk-contract.md`, so the core contract stays portable. Add a new section
to this shared catalog only when a profile is genuinely reusable across many
repositories (as SHUD/rSHUD/AutoSHUD are).

### Size budget

The profile is read at the start of every workflow run, so keep it lean. The
limit is not a flat line count; it scales with system breadth, with two hard
rules:

- **Do not restate core**: record only surfaces, packs, and triggers that the
  core risk packs and triggers in `issue-risk-contract.md` do not already cover.
  Restating core is the main source of bloat — delete it.
- **Short bullets, not prose**: each of the eight fields is a short bulleted list. Verification-matrix rows are one line per surface, covering the repo's real surfaces, not hypothetical ones; the rows count toward the budget.

Soft size targets (excluding the short header note):

- Generic or single-purpose project: under ~25 lines.
- Broad multi-subsystem system: under ~60 lines.

If a profile exceeds its target, it is a signal that it restates core, drifts
into prose, or the repository is really several subsystems that each deserve a
narrower profile. Trim or split rather than growing it unbounded.
