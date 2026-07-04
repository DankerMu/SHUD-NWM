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

// NOTE: duplicated in full-pipeline.workflow.js — keep in sync
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
        required: ["id", "severity", "title", "failureClass", "evidence", "impact", "fixDirection"]
      }
    }
  },
  required: ["findings"]
};

// NOTE: duplicated in full-pipeline.workflow.js — keep in sync
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

const SELF_AUDIT_SCHEMA = {
  type: "object",
  properties: {
    pass: { type: "boolean" },
    gaps: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          severity: { type: "string", enum: ["P0", "P1"] },
          title: { type: "string" },
          evidence: { type: "string" },
          fixDirection: { type: "string" },
          failureClass: { type: "string" },
          impact: { type: "string" }
        },
        required: ["id", "severity", "title", "evidence", "fixDirection", "failureClass", "impact"]
      }
    }
  },
  required: ["pass", "gaps"]
};

// Defensive: handle args passed as JSON string (caller-side serialization bug)
const _args = typeof args === "string" ? JSON.parse(args) : args || {};
const changeName = _args.changeName;
const changePath = `openspec/changes/${changeName}`;
const designDocs = (_args.designDocs || []).join(", ");

if (!changeName) {
  log("FATAL: changeName is missing from args — aborting");
  return { verdict: "error", reason: "changeName is undefined" };
}

// Count of subagent (agent()) invocations across the run — for the accountability log.
let subagentCalls = 0;

// ── Stage 3: Three-Way Parallel Review ──────────────────────────

phase("Review");
log(`Starting 3-way parallel review: ${changeName}`);

// NOTE: duplicated in full-pipeline.workflow.js — keep in sync
const REVIEWERS = [
  {
    key: "design-consistency",
    prompt: `Review the OpenSpec change "${changePath}" for design consistency.
Design docs: ${designDocs}

Focus: table/field/ENUM naming consistency across proposal, design, specs, tasks; API endpoint coverage; ID spec compliance; manifest field alignment.

Return P0/P1 findings with IDs prefixed "DC-". Each finding needs: id, severity, title, failureClass (from the risk-adaptive-cross-review finding-contract Failure-Class Vocabulary — commonly design-consistency / spec-completeness / task-executability), evidence (quote the inconsistency with file paths), impact (what breaks if left unfixed), fixDirection.
Reject vague or style-only observations — only concrete, anchored issues with file-level evidence.`
  },
  {
    key: "spec-completeness",
    prompt: `Review the OpenSpec change "${changePath}" for spec completeness.
Design docs: ${designDocs}

Focus: every proposal capability has a spec with requirements; each requirement has testable WHEN/THEN scenarios; boundary conditions covered; cross-spec consistency; no functional gaps vs design.md.

Return P0/P1 findings with IDs prefixed "SC-". Each finding needs: id, severity, title, failureClass (from the risk-adaptive-cross-review finding-contract Failure-Class Vocabulary — commonly design-consistency / spec-completeness / task-executability), evidence (quote the gap with file paths), impact (what breaks if left unfixed), fixDirection.
Reject vague or style-only observations — only concrete, anchored issues.`
  },
  {
    key: "tasks-executability",
    prompt: `Review the OpenSpec change "${changePath}" for task executability.
Design docs: ${designDocs}

Focus: every spec requirement maps to a task; task granularity (single-session); dependency ordering; no orphan tasks; verification methods clear; design decisions reflected.

Return P0/P1 findings with IDs prefixed "TE-". Each finding needs: id, severity, title, failureClass (from the risk-adaptive-cross-review finding-contract Failure-Class Vocabulary — commonly design-consistency / spec-completeness / task-executability), evidence (quote the gap with file paths), impact (what breaks if left unfixed), fixDirection.
Reject vague or style-only observations — only concrete, anchored issues.`
  }
];

subagentCalls += REVIEWERS.length;
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

// Success floor: crashed reviewers must NOT read as a clean review. Need >=2 of 3.
const okReviews = reviews.filter(Boolean);
if (okReviews.length < 2) {
  log(
    `FATAL: only ${okReviews.length}/${REVIEWERS.length} reviewers returned — below the 2-of-3 floor. Aborting.`
  );
  return { verdict: "review-round-failed", reviewersOk: okReviews.length };
}

const allFindings = okReviews.flatMap((r) => r.findings);
log(
  `Review done: ${allFindings.length} findings (P0: ${allFindings.filter((f) => f.severity === "P0").length}, P1: ${allFindings.filter((f) => f.severity === "P1").length})`
);

// ── Fix-Verify Loop (Stage 4 + 4.5) ────────────────────────────
// No early return on a clean Stage 3: even zero findings must clear the
// completion self-audit before exit, which the loop below drives.

const p0In = allFindings.filter((f) => f.severity === "P0").length;
const p1In = allFindings.filter((f) => f.severity === "P1").length;

let activeFindings = allFindings;
let round = 0;
const MAX_ROUNDS = 3;
let gateNetCatch = 0;
let totalRegressions = 0;
const resolvedSignatures = new Set();
let prevActiveCount = activeFindings.length;
let whackAMoleCount = 0;
let selfAuditPassed = false;

while ((activeFindings.length > 0 || !selfAuditPassed) && round < MAX_ROUNDS) {
  round++;

  if (activeFindings.length > 0) {
    // ── Stage 4: Fix ──

    phase("Fix");
    log(`Stage 4 round ${round}/${MAX_ROUNDS}: fixing ${activeFindings.length} findings`);

    const findingsList = activeFindings
      .map(
        (f) =>
          `- [${f.severity}] ${f.id}: ${f.title}\n  Evidence: ${f.evidence}\n  Fix direction: ${f.fixDirection}`
      )
      .join("\n");

    subagentCalls++;
    await agent(
      `Fix ALL of the following OpenSpec change review findings. The change is at "${changePath}".

${findingsList}

Edit the change files directly. Fix every finding — both P0 and P1 are blocking.
The design documents, implementation plan, and Stage 1 acceptance criteria are an immutable oracle — never edit them to make a finding pass. Fix the change files only.
After fixing, run: openspec status --change "${changeName}" — confirm 4/4 artifacts complete.`,
      { label: `fix:round-${round}`, phase: "Fix" }
    );

    // ── Stage 4.5: Independent Verify ──

    phase("Verify");
    log(`Stage 4.5 round ${round}: started — independent verification`);

    subagentCalls++;
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

    // gate_net_catch = findings the fixer claimed resolved but the verifier judged
    // unresolved/regressed (unresolved.length) + newly-introduced regressions
    // (regressions.length) — i.e. what would slip past without this gate.
    gateNetCatch += unresolved.length + regressions.length;
    totalRegressions += regressions.length;

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

    const residualP0 = activeFindings.filter((f) => f.severity === "P0").length;
    const residualP1 = activeFindings.filter((f) => f.severity === "P1").length;
    log(
      `round ${round} verdict: ${resolved.length}/${verification.verdicts.length} resolved, residual P0=${residualP0} P1=${residualP1}, ${regressions.length} regressions${whackAMoles.length > 0 ? `, ${whackAMoles.length} whack-a-mole` : ""}`
    );

    // dim 5: convergence detection — if active count not decreasing, stop early
    if (activeFindings.length >= prevActiveCount && round > 1) {
      log(
        `Convergence stall: ${activeFindings.length} active (was ${prevActiveCount}) — stopping early`
      );
      break;
    }
    prevActiveCount = activeFindings.length;
  }

  // Completion self-audit: once findings clear, confirm the 4-condition exit
  // (Stage 1 goals mapped to spec+task, openspec 4/4) before leaving the loop.
  if (activeFindings.length === 0 && !selfAuditPassed) {
    phase("Verify");
    log(`Stage 4.5 round ${round}: completion self-audit`);

    subagentCalls++;
    const audit = await agent(
      `You are a completion self-auditor for the OpenSpec change "${changePath}".
Design docs: ${designDocs}

The fix-verify loop reports all review findings resolved. Before exiting, confirm the change actually satisfies the Stage 1 goals.

Steps:
1. Re-derive the Stage 1 stage goals and acceptance criteria from the design docs.
2. For each goal/criterion, verify it maps to a concrete spec requirement AND a task in the change. A dropped goal, missing boundary, or internally contradictory fix is a gap.
3. Run: openspec status --change "${changeName}" — expect 4/4 artifacts complete. Anything less is a gap.

Set pass=true only if every criterion is covered and openspec status is 4/4. Otherwise pass=false and list gaps.
Each gap needs: id (SA-1...), severity (P0/P1), title, evidence (quote the design criterion and what is missing), fixDirection, failureClass (commonly design-consistency / spec-completeness / task-executability), impact.`,
      {
        label: `self-audit:round-${round}`,
        phase: "Verify",
        schema: SELF_AUDIT_SCHEMA
      }
    );

    if (!audit || audit.pass) {
      selfAuditPassed = true;
      log(`Completion self-audit passed after round ${round}`);
    } else {
      log(`Completion self-audit failed: ${audit.gaps.length} gaps — re-entering fix loop`);
      activeFindings = audit.gaps;
      prevActiveCount = activeFindings.length;
    }
  }
}

const verdict = activeFindings.length === 0 ? "clean" : "capped";
log(
  `Final: ${verdict} after ${round} rounds — residual: ${activeFindings.length} (P0: ${activeFindings.filter((f) => f.severity === "P0").length}, P1: ${activeFindings.filter((f) => f.severity === "P1").length})`
);

const p0Residual = activeFindings.filter((f) => f.severity === "P0").length;
const p1Residual = activeFindings.filter((f) => f.severity === "P1").length;

// Accountability log line for docs/stage-pipeline-log.jsonl. The sandbox has no clock,
// so "date" is omitted here — the orchestrator adds it before appending (see SKILL.md).
const logEntry = {
  change: changeName,
  rounds: round,
  gate_net_catch: gateNetCatch,
  p0: { in: p0In, resolved: Math.max(0, p0In - p0Residual), residual: p0Residual },
  p1: {
    resolved: Math.max(0, p1In - p1Residual),
    ...(verdict === "capped" ? { carried: p1Residual } : {})
  },
  regressions: totalRegressions,
  approx_subagent_calls: subagentCalls,
  verdict: verdict === "capped" ? "residual" : "clean"
};

return {
  verdict,
  rounds: round,
  totalFindings: allFindings.length,
  residual: activeFindings,
  p0Residual,
  p1Residual,
  gateNetCatch,
  whackAMoleCount,
  logEntry
};
