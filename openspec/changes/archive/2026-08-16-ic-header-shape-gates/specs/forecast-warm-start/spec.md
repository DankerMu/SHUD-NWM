# forecast-warm-start (delta)

## ADDED Requirements

### Requirement: Packaged-IC Qualification Rejects Malformed Header Shapes

The packaged initial-condition object probes SHALL validate the header
line's content shape whenever they read the IC bytes — this covers both
the scheduler generation gate's canonical object probe and the audit's
mirror probe — and a packaged IC whose header does not carry exactly three
or four numeric tokens SHALL be disqualified (`ic_qualified=False`) with a
dedicated content-verdict detail token distinct from every probe-failure
detail. The shape rule has a single source (the shared header-shape
helper); it is consumed at two layers: the shared classification consumes
the probe's shape verdict on the object-probe tier, while its
inventory-tier dispatch stays unchanged — the classification never fires a
probe for inventory-shaped manifests, preserving the production gate's
metadata-only inventory tier (a named limit — its compensations are the
audit sweep below, the registration gate for new deliveries, and the
runtime injector's fail-closed last line). The audit SHALL, in its own
layer after classification, run the content probe over inventory-shaped
rows as well and override the row's qualification with the same detail
token and a dedicated qualification-source value, so already-registered
baseline packages get an offline shape check; the audit receipt schema and
its limits notes SHALL be kept truthful about this probing. The existing
unreadable-probe channel keeps its semantics: a probe that cannot read the
object SHALL keep reporting unreadability and SHALL NOT be conflated with
a shape violation, and a shape violation SHALL NOT be reported as
unreadability.

#### Scenario: A malformed packaged IC is disqualified before first consumption

- **GIVEN** a packaged calibrated IC whose header line is `23106\t6`
- **WHEN** a packaged-IC object probe (gate or audit) reads its bytes and
  the shared classification runs
- **THEN** the IC is reported not qualified with the header-shape detail
  token, so the malformed package is caught at qualification time instead of
  first runtime consumption

#### Scenario: The audit sweeps inventory-shaped baseline packages

- **GIVEN** an already-registered baseline package with an inventory-shaped
  manifest whose packaged IC header is malformed
- **WHEN** the first-cycle initial-state audit runs
- **THEN** the audit's own-layer content probe reads the IC bytes and
  reports the package not qualified with the header-shape detail token and
  the dedicated qualification-source value, while the shared
  classification's inventory-tier dispatch itself fires no probe

#### Scenario: Probe unreadability stays a distinct verdict

- **GIVEN** a packaged IC object whose bytes cannot be read by the probe
- **WHEN** packaged-IC qualification runs
- **THEN** the result reports the existing unreadable verdict — not the
  header-shape detail token — and a readable-but-malformed IC conversely
  never reports unreadability
