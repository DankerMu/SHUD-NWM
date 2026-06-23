export const meta = {
  name: "stage-review-loop",
  description: "Enforce Stage 3→4→4.5 review-fix-verify loop for stage-change-pipeline",
  phases: [
    { title: "Review", detail: "Three-way parallel review of OpenSpec change" },
    { title: "Fix", detail: "Fix P0 and P1 findings" },
    { title: "Verify", detail: "Independent adversarial verification" }
  ]
};

// args: { changeName: string, designDocs?: string[] }

const FINDINGS_SCHEMA = {
  type: "object",
  properties: {
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: {
            type: "string",
            description: "Unique ID: DC-1, SC-1, TE-1 etc."
          },
          severity: { type: "string", enum: ["P0", "P1"] },
          title: { type: "string" },
          failureClass: { type: "string" },
          evidence: { type: "string" },
          impact: { type: "string" },
          fixDirection: { type: "string" }
        },
        required: ["id", "severity", "title", "evidence", "fixDirection"]
      }
    }
  },
  required: ["findings"]
};

const VERDICT_SCHEMA = {
  type: "object",
  properties: {
    verdicts: {
      type: "array",
      items: {
        type: "object",
        properties: {
          findingId: { type: "string" },
          status: {
            type: "string",
            enum: ["resolved", "unresolved", "regressed"]
          },
          evidence: { type: "string" }
        },
        required: ["findingId", "status", "evidence"]
      }
    },
    regressions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          severity: { type: "string", enum: ["P0", "P1"] },
          title: { type: "string" },
          evidence: { type: "string" },
          fixDirection: { type: "string" }
        },
        required: ["id", "severity", "title", "evidence"]
      }
    }
  },
  required: ["verdicts", "regressions"]
};

const changeName = args.changeName;
const changePath = `openspec/changes/${changeName}`;
const designDocs = (args.designDocs || []).join(", ");

// ── Stage 3: Three-Way Parallel Review ──────────────────────────

phase("Review");
log(`Starting 3-way parallel review: ${changeName}`);

const REVIEWERS = [
  {
    key: "design-consistency",
    prompt: `Review the OpenSpec change "${changePath}" for design consistency.
Design docs: ${designDocs}

Focus: table/field/ENUM naming consistency across proposal, design, specs, tasks; API endpoint coverage; ID spec compliance; manifest field alignment.

Return P0/P1 findings with IDs prefixed "DC-". Each finding needs: id, severity, title, evidence (quote the inconsistency with file paths), fixDirection.
Reject vague or style-only observations — only concrete, anchored issues with file-level evidence.`
  },
  {
    key: "spec-completeness",
    prompt: `Review the OpenSpec change "${changePath}" for spec completeness.
Design docs: ${designDocs}

Focus: every proposal capability has a spec with requirements; each requirement has testable WHEN/THEN scenarios; boundary conditions covered; cross-spec consistency; no functional gaps vs design.md.

Return P0/P1 findings with IDs prefixed "SC-". Each finding needs: id, severity, title, evidence, fixDirection.
Reject vague or style-only observations — only concrete, anchored issues.`
  },
  {
    key: "tasks-executability",
    prompt: `Review the OpenSpec change "${changePath}" for task executability.
Design docs: ${designDocs}

Focus: every spec requirement maps to a task; task granularity (single-session); dependency ordering; no orphan tasks; verification methods clear; design decisions reflected.

Return P0/P1 findings with IDs prefixed "TE-". Each finding needs: id, severity, title, evidence, fixDirection.
Reject vague or style-only observations — only concrete, anchored issues.`
  }
];

const reviews = await parallel(
  REVIEWERS.map(
    (r) => () =>
      agent(r.prompt, {
        label: `review:${r.key}`,
        phase: "Review",
        schema: FINDINGS_SCHEMA
      })
  )
);

const allFindings = reviews.filter(Boolean).flatMap((r) => r.findings);
log(
  `Review done: ${allFindings.length} findings (P0: ${allFindings.filter((f) => f.severity === "P0").length}, P1: ${allFindings.filter((f) => f.severity === "P1").length})`
);

if (allFindings.length === 0) {
  log("Clean — no findings, ready for Stage 5");
  return { verdict: "clean", rounds: 0, findings: [], residual: [] };
}

// ── Fix-Verify Loop (Stage 4 + 4.5) ────────────────────────────

let activeFindings = allFindings;
let round = 0;
const MAX_ROUNDS = 3;
let gateNetCatch = 0;
const resolvedSignatures = new Set();
let prevActiveCount = activeFindings.length;
let whackAMoleCount = 0;

while (activeFindings.length > 0 && round < MAX_ROUNDS) {
  round++;

  // ── Stage 4: Fix ──

  phase("Fix");
  log(`Stage 4 round ${round}/${MAX_ROUNDS}: fixing ${activeFindings.length} findings`);

  const findingsList = activeFindings
    .map(
      (f) =>
        `- [${f.severity}] ${f.id}: ${f.title}\n  Evidence: ${f.evidence}\n  Fix direction: ${f.fixDirection}`
    )
    .join("\n");

  await agent(
    `Fix ALL of the following OpenSpec change review findings. The change is at "${changePath}".

${findingsList}

Edit the change files directly. Fix every finding — both P0 and P1 are blocking.
After fixing, run: openspec status --change "${changeName}" — confirm 4/4 artifacts complete.`,
    { label: `fix:round-${round}`, phase: "Fix" }
  );

  // ── Stage 4.5: Independent Verify ──

  phase("Verify");
  log(`Stage 4.5 round ${round}: independent verification`);

  const verification = await agent(
    `You are an INDEPENDENT verifier — you did NOT fix these findings. Adversarially verify each one.

Change: "${changePath}"

Findings to verify:
${findingsList}

Rules:
- DEFAULT to "unresolved". Only mark "resolved" with concrete file+line evidence that the fix is correct.
- If a fix introduced a new inconsistency, mark the original "regressed" and add the new issue to regressions.
- Delta-scan: for each changed file, check naming/count/coverage alignment with adjacent artifacts.
- Evidence-or-bust: no "looks fixed" — quote the specific line or section that proves resolution.`,
    { label: `verify:round-${round}`, phase: "Verify", schema: VERDICT_SCHEMA }
  );

  if (!verification) {
    log(`Verify round ${round} returned null — treating all as unresolved, continuing`);
    continue;
  }

  const resolved = verification.verdicts.filter((v) => v.status === "resolved");
  const unresolved = verification.verdicts.filter((v) => v.status !== "resolved");
  const regressions = verification.regressions || [];

  gateNetCatch += unresolved.length;

  // dim 5: track resolved signatures for whack-a-mole detection
  for (const v of resolved) {
    const f = activeFindings.find((af) => af.id === v.findingId);
    if (f) resolvedSignatures.add(f.title);
  }

  // dim 5: flag regressions that match previously-resolved signatures
  const whackAMoles = regressions.filter((r) => resolvedSignatures.has(r.title));
  whackAMoleCount += whackAMoles.length;
  if (whackAMoles.length > 0) {
    log(
      `Whack-a-mole: ${whackAMoles.map((w) => `"${w.title}"`).join(", ")} — previously resolved, now regressed`
    );
  }

  log(
    `Round ${round} verdict: ${resolved.length} resolved, ${unresolved.length} unresolved, ${regressions.length} regressions${whackAMoles.length > 0 ? `, ${whackAMoles.length} whack-a-mole` : ""}`
  );

  activeFindings = [
    ...activeFindings.filter((f) => unresolved.some((u) => u.findingId === f.id)),
    ...regressions.map((r) => ({
      id: r.id,
      severity: r.severity || "P0",
      title: r.title,
      evidence: r.evidence,
      fixDirection: r.fixDirection || "Fix the regression"
    }))
  ];

  // dim 5: convergence detection — if active count not decreasing, stop early
  if (activeFindings.length >= prevActiveCount && round > 1) {
    log(
      `Convergence stall: ${activeFindings.length} active (was ${prevActiveCount}) — stopping early`
    );
    break;
  }
  prevActiveCount = activeFindings.length;
}

const verdict = activeFindings.length === 0 ? "clean" : "capped";
log(
  `Final: ${verdict} after ${round} rounds — residual: ${activeFindings.length} (P0: ${activeFindings.filter((f) => f.severity === "P0").length}, P1: ${activeFindings.filter((f) => f.severity === "P1").length})`
);

return {
  verdict,
  rounds: round,
  totalFindings: allFindings.length,
  residual: activeFindings,
  p0Residual: activeFindings.filter((f) => f.severity === "P0").length,
  p1Residual: activeFindings.filter((f) => f.severity === "P1").length,
  gateNetCatch,
  whackAMoleCount
};
