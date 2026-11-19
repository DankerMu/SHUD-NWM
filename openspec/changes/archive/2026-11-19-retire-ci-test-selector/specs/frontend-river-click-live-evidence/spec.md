## MODIFIED Requirements

### Requirement: Live and mocked browser evidence MUST remain distinct

The dedicated `live-river-click` Playwright profile SHALL run the river-click
receipt producer. The new test SHALL remain in `e2e/live-display.spec.ts` so the
existing exact live-spec matcher and broad `page.route('**/api/v1/**')` mock
prohibition cover it. The dedicated profile SHALL use one worker and zero
retries. The new metric SHALL visit only viewer-accessible `/` and SHALL add no `/ops` visit or claim. The legacy
two-URL `/monitoring` (`@live-monitoring`) lane and its `live-display` profile are retired by #3202, because
`/monitoring` is not open to `display_readonly` and that lane could never PASS on the public deployment. The C4 lane
(`test:e2e:live-c4-display`) is the live `display_readonly` `/ops` browser evidence. The node-27 display runbook SHALL
invoke `test:e2e:live-river-click`.

Focused Node publisher/unit tests MAY import the Node owner and write only their
private temporary fixtures. React/component tests and mocked browser specs SHALL
not import it or emit a live receipt. Screenshots, traces, local builds, mocked
responses, `/monitoring`, and `/ops` SHALL not satisfy or imply the `/` click
metric. Existing frontend `pnpm test`/build ownership, the static live no-mock
guard, and the root schema/example loop SHALL cover this change. Node-27 live
execution belongs to this capability: after the frontend build carrying it is
deployed, the runbook SHALL produce one receipt per pin for three current product
river networks - the largest, one near the median, and the smallest by segment
count - each pinned to that network's discharge-layer segment id and checked
against live `/api/v1` first, and SHALL accept the gate only when all three bind
as PASS.

#### Scenario: Mocked or sibling evidence cannot satisfy live readiness

- **WHEN** pure helpers, hook components, mocked browser tests, screenshots, `/monitoring`, or `/ops` pass
- **THEN** none emits or implies a live river-click PASS

#### Scenario: Live spec attempts a broad API mock

- **WHEN** `live-display.spec.ts` registers a broad `/api/v1/**` route mock
- **THEN** the existing static guard fails before browser execution and cannot produce PASS

#### Scenario: Runbook consumes exact merged evidence

- **WHEN** the node-27 runbook runs the merged live profile for each of the three current pins with an absent private receipt path
- **THEN** it accepts the click gate only from the three current commands' schema-1.1 mode-0600 PASS receipts with `click_dispatch=trusted_pointer_event`, matching origins, feature/products, counts, nearest-rank method, and P95 below 2000 ms
