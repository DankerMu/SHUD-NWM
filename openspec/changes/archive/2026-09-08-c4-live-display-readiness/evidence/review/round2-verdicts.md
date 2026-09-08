# PR #2126 round 2 independent verdict

Reviewed head SHA: `391b6590e4160aaccad278e7dcd8591a2f576444`.

| ID | Class | Severity | Verdict | Disposition | Verifier |
| --- | --- | --- | --- | --- | --- |
| C4-R2-1 | contract | P2 | CONFIRMED | DEFER | a476b57dbcbc98192 |

Two scenarios can both match when origin/receipt path and a pin are invalid. Shipping owner/config intentionally evaluate path/URL first and produce BLOCKED; the pin-only scenario text can predict FAIL CONFIG_INVALID. README and implementation expose precedence by order but do not make the archived normative requirement unambiguous. Both states reject PASS; no authorization bypass, browser start or successful evidence acceptance results.

Disposition follows the P2-only policy: no P1 or coverage gap remains to buy a fix/re-review round, and this wording-only issue must not be smuggled into a post-review local repair. Route through issue-scribe with exact follow-up: state BLOCKED precedence in the origin/path scenario or require valid origin/path in the pin scenario; mirror design regression row; no implementation change or new test required beyond strict OpenSpec validation.

Deferral routed to #2130 (`https://github.com/DankerMu/SHUD-NWM/issues/2130`), verified/deduplicated by issue-scribe. It is implementation-ready docs/spec-only and explicitly waits for #2126 merge; no current-PR tail commit, runtime change, new test, or parent issue closure. Round 2 is therefore recorded clean under P2-only routing policy. The routed catch remains in accountability evidence and is not renamed REFUTED or omitted.
