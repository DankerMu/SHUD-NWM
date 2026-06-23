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

// args: { changeName: string, designDocs?: string[], stageLabel?: string }
// Defensive: handle args passed as JSON string (caller-side serialization bug)
const _args = typeof args === "string" ? JSON.parse(args) : args || {};
const changeName = _args.changeName;
const changePath = `openspec/changes/${changeName}`;
const designDocs = (_args.designDocs || []).join(", ");
const stageLabel = _args.stageLabel || "";

if (!changeName) {
  log("FATAL: changeName is missing from args — aborting");
  return { verdict: "error", reason: "changeName is undefined" };
}

// ── Schemas ─────────────────────────────────────────────────────

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

// ═══════════════════════════════════════════════════════════════════
// STAGE 4 + 4.5: Fix-Verify Loop
// ═══════════════════════════════════════════════════════════════════

let activeFindings = allFindings;
let reviewRound = 0;
const REVIEW_MAX_ROUNDS = 3;
let gateNetCatch = 0;
const resolvedSignatures = new Set();
let prevActiveCount = activeFindings.length;
let whackAMoleCount = 0;

while (activeFindings.length > 0 && reviewRound < REVIEW_MAX_ROUNDS) {
  reviewRound++;

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

  await agent(
    `Fix ALL of the following OpenSpec change review findings. The change is at "${changePath}".

${findingsList}

Edit the change files directly. Fix every finding — both P0 and P1 are blocking.
After fixing, run: openspec status --change "${changeName}" — confirm 4/4 artifacts complete.`,
    { label: `fix:round-${reviewRound}`, phase: "Fix" }
  );

  phase("Verify");
  log(`Stage 4.5 round ${reviewRound}: independent verification`);

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

  gateNetCatch += unresolved.length;

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

  log(
    `Round ${reviewRound} verdict: ${resolved.length} resolved, ${unresolved.length} unresolved, ${regressions.length} regressions${whackAMoles.length > 0 ? `, ${whackAMoles.length} whack-a-mole` : ""}`
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

  if (activeFindings.length >= prevActiveCount && reviewRound > 1) {
    log(
      `Convergence stall: ${activeFindings.length} active (was ${prevActiveCount}) — stopping early`
    );
    break;
  }
  prevActiveCount = activeFindings.length;
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

const issueResult = await agent(
  `Create GitHub issues for the reviewed OpenSpec change at "${changePath}".

Instructions:
1. Run: gh auth status — confirm authenticated
2. Create labels (epic, sub-task, priority, stage labels) using: gh label create <name> --color <hex> --description "<desc>" --force
3. Create an Epic issue with: overview, design doc references, sub-task placeholders, dependency graph
4. Read tasks.md from the change, generate sub-issue groupings — one module per issue, small-PR scope
5. Create sub-issues, each with:
   - Part of #<epic> link
   - Dependencies: #<dep1>, #<dep2>
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

const alignReview = await agent(
  `Review alignment between GitHub issues and the OpenSpec change.

Change path: "${changePath}"
Epic: #${epicNumber}

Steps:
1. Read the OpenSpec change artifacts: proposal.md, design.md, specs/*, tasks.md
2. List all sub-issues of Epic #${epicNumber}: gh issue list${labelFilter} --json number,title,body --limit 100
3. For each issue, also read its full body: gh issue view <number> --json body
4. Compare and find gaps in these dimensions:

   - **missing-coverage**: a task in tasks.md is not covered by any issue
   - **wrong-boundary**: an issue mixes multiple modules or ownership scopes
   - **wrong-dependency**: issue dependency chain doesn't match task dependency order
   - **scope-mismatch**: issue "In Scope" / "Out of Scope" doesn't match actual task content
   - **missing-reference**: issue is missing required spec or design doc references from the change
   - **content-drift**: issue content (task checklist, acceptance criteria, PR boundary) contradicts or diverges from change artifacts

Return structured gaps. Each gap needs: id (IA-1, IA-2...), severity (P0/P1), type, title, evidence (quote both the change artifact and the issue), fixDirection, affectedIssue (#number).
Reject vague concerns — only concrete, anchored gaps with evidence from both sides.`,
  {
    label: "review:alignment",
    phase: "Alignment Review",
    schema: ALIGNMENT_SCHEMA
  }
);

let activeGaps = alignReview ? alignReview.gaps : [];
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

    await agent(
      `Fix the following alignment gaps between GitHub issues and OpenSpec change "${changePath}".
Epic: #${epicNumber}

${gapsList}

For each gap type, use gh CLI:
- missing-coverage: create a new sub-issue linked to Epic #${epicNumber}, with proper module boundary and Implementation Ready contract
- wrong-boundary: split the issue or re-scope using gh issue edit
- wrong-dependency: update the Dependencies section in issue body
- scope-mismatch: edit In Scope / Out of Scope to match change artifacts
- missing-reference: add spec/design doc references to issue body
- content-drift: edit issue body to match change artifacts (task checklist, acceptance criteria, PR boundary)

Both P0 and P1 are blocking — fix all of them.`,
      { label: `align-fix:round-${alignRound}`, phase: "Alignment Fix" }
    );

    phase("Alignment Verify");
    log(`Alignment verify round ${alignRound}: checking fixes`);

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
  alignResidual: activeGaps
};
