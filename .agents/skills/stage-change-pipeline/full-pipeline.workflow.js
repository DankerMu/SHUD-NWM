export const meta = {
  name: "stage-pipeline-run",
  description:
    "Single invocation for Stage 3→5.5: review, fix-verify loop, issue creation, alignment review (all inlined, no workflow() nesting)",
  phases: [
    { title: "Review", detail: "Three-way parallel review of OpenSpec change" },
    { title: "Fix", detail: "Fix P0 and P1 findings" },
    { title: "Verify", detail: "Independent adversarial verification" },
    { title: "Issue Creation", detail: "Stage 5 GitHub issue creation" },
    {
      title: "Alignment Review",
      detail: "Stage 5.5 issue-change alignment review"
    },
    { title: "Alignment Fix", detail: "Fix alignment gaps" },
    { title: "Alignment Verify", detail: "Verify alignment fixes" }
  ]
};

// args: { changeName: string, designDocs?: string[], stageLabel?: string, grillGate: "passed" | "skipped:<reason>" }
// Defensive: handle args passed as JSON string (caller-side serialization bug)
const _args = typeof args === "string" ? JSON.parse(args) : args || {};
const changeName = _args.changeName;
const changePath = `openspec/changes/${changeName}`;
const designDocs = (_args.designDocs || []).join(", ");
const stageLabel = _args.stageLabel || "";
const grillGate = typeof _args.grillGate === "string" ? _args.grillGate : "";

if (!changeName) {
  log("FATAL: changeName is missing from args — aborting");
  return { verdict: "error", reason: "changeName is undefined" };
}

// Grill-gate attestation. The Stage 1→2 grill-me stress-test is multi-round and
// user-interactive, so it can only happen in the main conversation BEFORE this
// script launches — workflow subagents cannot talk to the user, and this script
// cannot run it retroactively. All it can enforce is refusing to start until the
// orchestrator attests the gate decision was made: grill-me ran ("passed") or the
// skip was recorded with a reason ("skipped:<reason>").
if (grillGate !== "passed" && !/^skipped:.+/.test(grillGate)) {
  log(
    'FATAL: grillGate missing or invalid — run grill-me in the main conversation first (grillGate: "passed"), or record an explicit skip (grillGate: "skipped:<reason>")'
  );
  return {
    verdict: "error",
    reason:
      'grillGate must be "passed" or "skipped:<reason>" — decide the Stage 1→2 grill gate before Stage 3 starts'
  };
}

// Count of subagent (agent()) invocations across the whole run — for the accountability log.
let subagentCalls = 0;

// ── Schemas ─────────────────────────────────────────────────────

// NOTE: duplicated in review-loop.workflow.js — keep in sync
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

// NOTE: duplicated in review-loop.workflow.js — keep in sync
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

const ISSUE_RESULT_SCHEMA = {
  type: "object",
  properties: {
    epicNumber: {
      type: "number",
      description: "The GitHub issue number of the created Epic"
    },
    subIssueNumbers: {
      type: "array",
      items: { type: "number" },
      description: "GitHub issue numbers of created sub-issues"
    }
  },
  required: ["epicNumber", "subIssueNumbers"]
};

// NOTE: duplicated in issue-alignment.workflow.js + SKILL.md Stage 5.5 — keep in sync
const ALIGNMENT_SCHEMA = {
  type: "object",
  properties: {
    gaps: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: {
            type: "string",
            description: "Unique ID: IA-1, IA-2 etc."
          },
          severity: { type: "string", enum: ["P0", "P1"] },
          type: {
            type: "string",
            enum: [
              "missing-coverage",
              "wrong-boundary",
              "wrong-dependency",
              "scope-mismatch",
              "missing-reference",
              "content-drift"
            ]
          },
          title: { type: "string" },
          evidence: { type: "string" },
          fixDirection: { type: "string" },
          affectedIssue: {
            type: "string",
            description: "Issue number, e.g. #42"
          }
        },
        required: ["id", "severity", "type", "title", "evidence", "fixDirection"]
      }
    }
  },
  required: ["gaps"]
};

const ALIGN_VERIFY_SCHEMA = {
  type: "object",
  properties: {
    verdicts: {
      type: "array",
      items: {
        type: "object",
        properties: {
          gapId: { type: "string" },
          status: { type: "string", enum: ["resolved", "unresolved"] },
          evidence: { type: "string" }
        },
        required: ["gapId", "status", "evidence"]
      }
    }
  },
  required: ["verdicts"]
};

// ═══════════════════════════════════════════════════════════════════
// STAGE 3: Three-Way Parallel Review
// ═══════════════════════════════════════════════════════════════════

phase("Review");
log(`Starting 3-way parallel review: ${changeName}`);

// NOTE: duplicated in review-loop.workflow.js — keep in sync
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
    `FATAL: only ${okReviews.length}/${REVIEWERS.length} reviewers returned — below the 2-of-3 floor. Aborting pipeline.`
  );
  return { verdict: "review-round-failed", reviewersOk: okReviews.length };
}

const allFindings = okReviews.flatMap((r) => r.findings);
log(
  `Review done: ${allFindings.length} findings (P0: ${allFindings.filter((f) => f.severity === "P0").length}, P1: ${allFindings.filter((f) => f.severity === "P1").length})`
);

// ═══════════════════════════════════════════════════════════════════
// STAGE 4 + 4.5: Fix-Verify Loop
// ═══════════════════════════════════════════════════════════════════

const p0In = allFindings.filter((f) => f.severity === "P0").length;
const p1In = allFindings.filter((f) => f.severity === "P1").length;

let activeFindings = allFindings;
let reviewRound = 0;
const REVIEW_MAX_ROUNDS = 3;
let gateNetCatch = 0;
let totalRegressions = 0;
const resolvedSignatures = new Set();
let prevActiveCount = activeFindings.length;
let whackAMoleCount = 0;
let selfAuditPassed = false;

// Loop while findings remain OR the completion self-audit has not yet passed.
while ((activeFindings.length > 0 || !selfAuditPassed) && reviewRound < REVIEW_MAX_ROUNDS) {
  reviewRound++;

  if (activeFindings.length > 0) {
    phase("Fix");
    log(
      `Stage 4 round ${reviewRound}/${REVIEW_MAX_ROUNDS}: fixing ${activeFindings.length} findings`
    );

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
      { label: `fix:round-${reviewRound}`, phase: "Fix" }
    );

    phase("Verify");
    log(`Stage 4.5 round ${reviewRound}: started — independent verification`);

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
      {
        label: `verify:round-${reviewRound}`,
        phase: "Verify",
        schema: VERDICT_SCHEMA
      }
    );

    if (!verification) {
      log(`Verify round ${reviewRound} returned null — treating all as unresolved, continuing`);
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

    for (const v of resolved) {
      const f = activeFindings.find((af) => af.id === v.findingId);
      if (f) resolvedSignatures.add(f.title);
    }

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
      `round ${reviewRound} verdict: ${resolved.length}/${verification.verdicts.length} resolved, residual P0=${residualP0} P1=${residualP1}, ${regressions.length} regressions${whackAMoles.length > 0 ? `, ${whackAMoles.length} whack-a-mole` : ""}`
    );

    if (activeFindings.length >= prevActiveCount && reviewRound > 1) {
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
    log(`Stage 4.5 round ${reviewRound}: completion self-audit`);

    subagentCalls++;
    const audit = await agent(
      `You are a completion self-auditor for the OpenSpec change "${changePath}".
Design docs: ${designDocs}

The fix-verify loop reports all review findings resolved. Before exiting to issue creation, confirm the change actually satisfies the Stage 1 goals.

Steps:
1. Re-derive the Stage 1 stage goals and acceptance criteria from the design docs.
2. For each goal/criterion, verify it maps to a concrete spec requirement AND a task in the change. A dropped goal, missing boundary, or internally contradictory fix is a gap.
3. Run: openspec status --change "${changeName}" — expect 4/4 artifacts complete. Anything less is a gap.

Set pass=true only if every criterion is covered and openspec status is 4/4. Otherwise pass=false and list gaps.
Each gap needs: id (SA-1...), severity (P0/P1), title, evidence (quote the design criterion and what is missing), fixDirection, failureClass (commonly design-consistency / spec-completeness / task-executability), impact.`,
      {
        label: `self-audit:round-${reviewRound}`,
        phase: "Verify",
        schema: SELF_AUDIT_SCHEMA
      }
    );

    if (!audit || audit.pass) {
      selfAuditPassed = true;
      log(`Completion self-audit passed after round ${reviewRound}`);
    } else {
      log(`Completion self-audit failed: ${audit.gaps.length} gaps — re-entering fix loop`);
      activeFindings = audit.gaps;
      prevActiveCount = activeFindings.length;
    }
  }
}

const reviewVerdict = activeFindings.length === 0 ? "clean" : "capped";
log(
  `Review loop: ${reviewVerdict} after ${reviewRound} rounds — residual: ${activeFindings.length} (P0: ${activeFindings.filter((f) => f.severity === "P0").length}, P1: ${activeFindings.filter((f) => f.severity === "P1").length})`
);

const residualNote =
  activeFindings.length > 0
    ? `\n\nThe following findings were NOT fully resolved after ${reviewRound} rounds. Mark each in the relevant sub-issue body with **needs-followup** and record it in the Epic:\n${activeFindings.map((f) => `- [${f.severity}] ${f.id}: ${f.title}`).join("\n")}`
    : "";

// ═══════════════════════════════════════════════════════════════════
// STAGE 5: Issue Creation
// ═══════════════════════════════════════════════════════════════════

phase("Issue Creation");
log("Creating GitHub issues from reviewed change");

const stageLabelInstruction = stageLabel
  ? `\n   Stage label (REQUIRED): run \`gh label create "${stageLabel}" --force\`, then apply --label "${stageLabel}" to the Epic AND every sub-issue. Stage 5.5 filters sub-issues by this label, so any issue missing it is silently dropped from the alignment review.`
  : "";

subagentCalls++;
const issueResult = await agent(
  `Create GitHub issues for the reviewed OpenSpec change at "${changePath}".

Instructions:
1. Run: gh auth status — confirm authenticated
2. Create labels (epic, sub-task, priority, stage labels) using: gh label create <name> --color <hex> --description "<desc>" --force${stageLabelInstruction}
3. Create an Epic issue with: overview, design doc references, sub-task placeholders, dependency graph
4. Read tasks.md from the change, generate sub-issue groupings — one module per issue, small-PR scope
5. Create sub-issues, each with:
   - Part of #<epic> link
   - One \`Depends on #NN\` line per dependency (downstream DAG readers grep for literal \`Depends on #NN\` lines — do NOT collapse them into a single comma-separated list)
   - Module / Scope: <module-or-path>
   - In Scope / Out of Scope
   - PR Boundary
   - Task checklist (from tasks.md, checkbox format)
   - Required reading docs with priority and focus sections
   - Acceptance criteria
   - Implementation Ready: yes
6. Backfill the Epic with the final sub-issue list and dependency graph
${residualNote}

Return the Epic number and all sub-issue numbers as structured output.`,
  {
    label: "create-issues",
    phase: "Issue Creation",
    schema: ISSUE_RESULT_SCHEMA
  }
);

if (!issueResult || !issueResult.epicNumber) {
  log("Issue creation failed or returned no Epic number — cannot run alignment review");
  return {
    verdict: "error",
    reviewVerdict,
    reviewRounds: reviewRound,
    residual: activeFindings,
    gateNetCatch,
    whackAMoleCount
  };
}

const epicNumber = issueResult.epicNumber;
log(`Created Epic #${epicNumber} with ${issueResult.subIssueNumbers.length} sub-issues`);

// ═══════════════════════════════════════════════════════════════════
// STAGE 5.5: Issue-Change Alignment Review
// ═══════════════════════════════════════════════════════════════════

phase("Alignment Review");
const labelFilter = stageLabel ? ` --label "${stageLabel}"` : "";

log(`Reviewing issue-change alignment: ${changeName} (Epic #${epicNumber})`);

const alignReviewPrompt = `Review alignment between GitHub issues and the OpenSpec change.

Change path: "${changePath}"
Epic: #${epicNumber}

Steps:
1. Read the OpenSpec change artifacts: proposal.md, design.md, specs/*, tasks.md
2. List all sub-issues of Epic #${epicNumber}: gh issue list${labelFilter} --json number,title,body --limit 100. If exactly 100 issues come back, the list is likely truncated — re-list with --limit 500 and flag the truncation in your output so coverage is not silently incomplete.
3. For each issue, also read its full body: gh issue view <number> --json body
4. Compare and find gaps in these dimensions:

   - **missing-coverage**: a task in tasks.md is not covered by any issue
   - **wrong-boundary**: an issue mixes multiple modules or ownership scopes
   - **wrong-dependency**: issue dependency chain doesn't match task dependency order
   - **scope-mismatch**: issue "In Scope" / "Out of Scope" doesn't match actual task content
   - **missing-reference**: issue is missing required spec or design doc references from the change
   - **content-drift**: issue content (task checklist, acceptance criteria, PR boundary) contradicts or diverges from change artifacts

Return structured gaps. Each gap needs: id (IA-1, IA-2...), severity (P0/P1), type, title, evidence (quote both the change artifact and the issue), fixDirection, affectedIssue (#number).
Reject vague concerns — only concrete, anchored gaps with evidence from both sides.`;

subagentCalls++;
let alignReview = await agent(alignReviewPrompt, {
  label: "review:alignment",
  phase: "Alignment Review",
  schema: ALIGNMENT_SCHEMA
});

if (!alignReview) {
  log("Alignment review returned null — retrying once");
  subagentCalls++;
  alignReview = await agent(alignReviewPrompt, {
    label: "review:alignment-retry",
    phase: "Alignment Review",
    schema: ALIGNMENT_SCHEMA
  });
}

if (!alignReview) {
  log("Alignment review failed twice — aborting; cannot certify issue-change alignment");
  return {
    verdict: "error",
    reason: "alignment-review-failed",
    reviewVerdict,
    reviewRounds: reviewRound,
    reviewResidual: activeFindings,
    gateNetCatch,
    whackAMoleCount,
    epicNumber,
    subIssueCount: issueResult.subIssueNumbers.length
  };
}

let activeGaps = alignReview.gaps;
let alignRound = 0;
const ALIGN_MAX_ROUNDS = 2;
const alignResolvedSigs = new Set();
let alignPrevCount = activeGaps.length;

if (activeGaps.length === 0) {
  log("Issues aligned with change — no gaps found");
} else {
  log(
    `Found ${activeGaps.length} alignment gaps (P0: ${activeGaps.filter((g) => g.severity === "P0").length}, P1: ${activeGaps.filter((g) => g.severity === "P1").length})`
  );

  while (activeGaps.length > 0 && alignRound < ALIGN_MAX_ROUNDS) {
    alignRound++;

    phase("Alignment Fix");
    log(`Alignment fix round ${alignRound}/${ALIGN_MAX_ROUNDS}: fixing ${activeGaps.length} gaps`);

    const gapsList = activeGaps
      .map(
        (g) =>
          `- [${g.severity}] ${g.id} (${g.type}): ${g.title}\n  Evidence: ${g.evidence}\n  Fix: ${g.fixDirection}${g.affectedIssue ? `\n  Issue: ${g.affectedIssue}` : ""}`
      )
      .join("\n");

    subagentCalls++;
    await agent(
      `Fix the following alignment gaps between GitHub issues and OpenSpec change "${changePath}".
Epic: #${epicNumber}

${gapsList}

For each gap type, use gh CLI:
- missing-coverage: create a new sub-issue linked to Epic #${epicNumber}, with proper module boundary and Implementation Ready contract
- wrong-boundary: split the issue or re-scope using gh issue edit
- wrong-dependency: update the \`Depends on #NN\` lines in the issue body (one line per dependency)
- scope-mismatch: edit In Scope / Out of Scope to match change artifacts
- missing-reference: add spec/design doc references to issue body
- content-drift: edit issue body to match change artifacts (task checklist, acceptance criteria, PR boundary)

The OpenSpec change artifacts, design documents, implementation plan, and Stage 1 acceptance criteria are an immutable oracle — never edit them to make an issue align. Fix the GitHub issues only.
Both P0 and P1 are blocking — fix all of them.`,
      { label: `align-fix:round-${alignRound}`, phase: "Alignment Fix" }
    );

    phase("Alignment Verify");
    log(`Alignment verify round ${alignRound}: checking fixes`);

    subagentCalls++;
    const alignVerify = await agent(
      `Independently verify that the following alignment gaps have been fixed.

Change: "${changePath}", Epic: #${epicNumber}

Gaps to verify:
${gapsList}

For each gap:
1. Read the current issue state: gh issue view <number> --json body,title
2. Read the relevant change artifact
3. Confirm the gap is resolved with concrete evidence

Default to "unresolved". Only mark "resolved" if the issue content now matches the change artifact.`,
      {
        label: `align-verify:round-${alignRound}`,
        phase: "Alignment Verify",
        schema: ALIGN_VERIFY_SCHEMA
      }
    );

    if (!alignVerify) {
      log(`Alignment verify round ${alignRound} returned null — treating all as unresolved`);
      continue;
    }

    const aResolved = alignVerify.verdicts.filter((v) => v.status === "resolved");
    const aUnresolved = alignVerify.verdicts.filter((v) => v.status !== "resolved");

    for (const v of aResolved) {
      const g = activeGaps.find((ag) => ag.id === v.gapId);
      if (g) alignResolvedSigs.add(g.title);
    }

    log(
      `Alignment round ${alignRound}: ${aResolved.length} resolved, ${aUnresolved.length} unresolved`
    );

    activeGaps = activeGaps.filter((g) => aUnresolved.some((u) => u.gapId === g.id));

    if (activeGaps.length >= alignPrevCount && alignRound > 1) {
      log(
        `Alignment convergence stall: ${activeGaps.length} active (was ${alignPrevCount}) — stopping`
      );
      break;
    }
    alignPrevCount = activeGaps.length;
  }
}

const alignVerdict = activeGaps.length === 0 ? "clean" : "capped";
const overallVerdict = reviewVerdict === "clean" && alignVerdict === "clean" ? "clean" : "residual";

log(`Final: review=${reviewVerdict}, alignment=${alignVerdict}, overall=${overallVerdict}`);

// Accountability log line for docs/stage-pipeline-log.jsonl. The sandbox has no clock,
// so "date" is omitted here — the orchestrator adds it before appending (see SKILL.md).
const p0Residual = activeFindings.filter((f) => f.severity === "P0").length;
const p1Residual = activeFindings.filter((f) => f.severity === "P1").length;
const logEntry = {
  change: changeName,
  grill_gate: grillGate,
  rounds: reviewRound,
  gate_net_catch: gateNetCatch,
  p0: { in: p0In, resolved: Math.max(0, p0In - p0Residual), residual: p0Residual },
  p1: {
    resolved: Math.max(0, p1In - p1Residual),
    ...(reviewVerdict === "capped" ? { carried: p1Residual } : {})
  },
  regressions: totalRegressions,
  approx_subagent_calls: subagentCalls,
  verdict: overallVerdict
};

return {
  verdict: overallVerdict,
  reviewVerdict,
  reviewRounds: reviewRound,
  totalFindings: allFindings.length,
  reviewResidual: activeFindings,
  gateNetCatch,
  whackAMoleCount,
  epicNumber,
  subIssueCount: issueResult.subIssueNumbers.length,
  alignVerdict,
  alignRounds: alignRound,
  alignResidual: activeGaps,
  logEntry
};
