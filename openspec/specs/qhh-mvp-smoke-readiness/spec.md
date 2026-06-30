# qhh-mvp-smoke-readiness Specification

## Purpose
TBD - created by archiving change m21-qhh-hydro-met-ops-mvp. Update Purpose after archive.
## Requirements
### Requirement: QHH MVP smoke chain

The MVP SHALL have a verifiable QHH smoke chain covering backend data flow, frontend display, and operations controls.

#### Scenario: Full GFS smoke
- **WHEN** the QHH GFS MVP smoke runs in an accepted environment
- **THEN** evidence covers download, canonical conversion, forcing production, SHUD execution, output parse, station series API, forecast-series API, hydro-met display, ops stage/job/log display, and retry capability when applicable.

#### Scenario: IFS parallel smoke
- **WHEN** the MVP is marked ready for internal launch with IFS as a parallel source
- **THEN** evidence covers IFS source/cycle selection, forcing or run availability, station/river display where data exists, and explicit shorter-horizon labeling for 06/18 UTC cycles that only provide 144h or otherwise shorter-than-seven-day horizons
- **AND** if live IFS proof is skipped, the release evidence records the exact missing dependency and does not claim IFS live readiness.

#### Scenario: No synthetic data
- **WHEN** any smoke step lacks required data
- **THEN** the evidence records an unavailable, restricted, missing, or failed reason
- **AND** it does not count fabricated station curves, fabricated river curves, or padded IFS horizons as success.

### Requirement: MVP release checklist

The MVP release checklist SHALL distinguish required internal MVP proof from later production-readiness proof.

#### Scenario: Required MVP proof
- **WHEN** the MVP is marked ready for internal launch
- **THEN** evidence shows a QHH station list near the expected forcing-station count, one station returning the six forcing variables with units and quality flags, one river segment returning nonempty `q_down`, latest-product bootstrap without manual IDs, GFS and IFS source handling, IFS shorter-horizon labeling where applicable, current `/` display interactive chart updates, `/hydro-met -> /` only as a legacy redirect compatibility smoke when checked, `/ops` stage/job/log visibility, and controlled failure retry evidence.

#### Scenario: Scoped exclusions
- **WHEN** release notes or progress documents describe MVP readiness
- **THEN** they list nationwide all-basin coverage, water level `stage`, CLDAS, ERA5 near-real-time, real national MVT/PBF, live IdP, live alert sink, live rollback, and final production readiness as excluded from the MVP
- **AND** they do not claim final production readiness without accepted live receipts.
- **AND** retired supplemental quality states are acceptable for MVP display only when they are not fabricated.

#### Scenario: Validation commands
- **WHEN** implementation PRs for this change are completed
- **THEN** they record backend tests, OpenAPI drift checks, frontend type/build/tests, and any opt-in QHH smoke commands that were run
- **AND** skipped live smoke steps include the exact missing dependency or environment reason.

### Requirement: Documentation and progress alignment

The implementation SHALL keep MVP documentation, runbooks, and progress status aligned with the delivered scope.

#### Scenario: Progress update
- **WHEN** a task in this change lands
- **THEN** `progress.md` and related runbooks continue to identify `nhms-pipeline plan-production` as the formal scheduler path
- **AND** qhh scripts remain documented as diagnostic, regression, or evidence collection tools only.

#### Scenario: Launch plan update
- **WHEN** MVP scope or acceptance criteria change during implementation
- **THEN** `docs/plans/2026-05-25-mvp-launch-plan.md` is updated in the same PR or a linked documentation PR
- **AND** changes preserve the distinction between `q_down` MVP flow curves and out-of-scope `stage` water levels.
