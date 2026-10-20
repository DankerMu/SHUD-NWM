## ADDED Requirements

### Requirement: API error messages SHALL never surface raw non-JSON error bodies
`getApiErrorMessage(error, fallback)` SHALL return:
- the message of an `Error`;
- the `error.message` (or top-level `message`) string of an object envelope;
- `fallback` for everything else, including raw response text such as proxy HTML pages or plain-text gateway errors.

#### Scenario: Proxy HTML page
- **WHEN** an openapi-fetch call fails with a non-JSON body `<html><head><title>502 Bad Gateway</title></head>…</html>`
- **THEN** the message shown or stored is the caller's fallback text and contains no `<html`

#### Scenario: Envelope unchanged
- **WHEN** the error is `{ error: { code: "X", message: "m" } }`
- **THEN** the message is `m`

#### Scenario: Client-generated river-forecast contract messages unchanged
- **WHEN** `capHydroMetRiverForecastMessages` caps the client-generated contract message strings
- **THEN** it formats them without calling `getApiErrorMessage`, and the stored `messages` equal the pre-change values
