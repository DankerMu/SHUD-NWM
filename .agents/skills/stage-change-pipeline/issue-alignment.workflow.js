export const meta = {
  name: "issue-alignment-review",
  description: "Post-creation review: verify GitHub issues align with OpenSpec change",
  phases: [
    { title: "Review", detail: "Check issue-change alignment" },
    { title: "Fix", detail: "Fix misaligned issues" },
    { title: "Verify", detail: "Confirm alignment" }
  ]
};

// args: { changeName: string, epicNumber: number, stageLabel?: string }

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

const VERIFY_SCHEMA = {
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

const changeName = args.changeName;
const changePath = `openspec/changes/${changeName}`;
const epicNumber = args.epicNumber;
const stageLabel = args.stageLabel || "";
const labelFilter = stageLabel ? ` --label "${stageLabel}"` : "";

// ── Review: Issue-Change Alignment ──────────────────────────────

phase("Review");
log(`Reviewing issue-change alignment: ${changeName} (Epic #${epicNumber})`);

const review = await agent(
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
  { label: "review:alignment", phase: "Review", schema: ALIGNMENT_SCHEMA }
);

if (!review || review.gaps.length === 0) {
  log("Issues aligned with change — no gaps found");
  return { verdict: "clean", rounds: 0, gaps: [], residual: [] };
}

log(
  `Found ${review.gaps.length} alignment gaps (P0: ${review.gaps.filter((g) => g.severity === "P0").length}, P1: ${review.gaps.filter((g) => g.severity === "P1").length})`
);

// ── Fix-Verify Loop ─────────────────────────────────────────────

let activeGaps = review.gaps;
let round = 0;
const MAX_ROUNDS = 2;
const resolvedSignatures = new Set();
let prevActiveCount = activeGaps.length;

while (activeGaps.length > 0 && round < MAX_ROUNDS) {
  round++;

  // ── Fix ──

  phase("Fix");
  log(`Fix round ${round}/${MAX_ROUNDS}: fixing ${activeGaps.length} alignment gaps`);

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
    { label: `fix:round-${round}`, phase: "Fix" }
  );

  // ── Verify ──

  phase("Verify");
  log(`Verify round ${round}: checking alignment fixes`);

  const verification = await agent(
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
      label: `verify:round-${round}`,
      phase: "Verify",
      schema: VERIFY_SCHEMA
    }
  );

  if (!verification) {
    log(`Verify round ${round} returned null — treating all as unresolved, continuing`);
    continue;
  }

  const resolved = verification.verdicts.filter((v) => v.status === "resolved");
  const unresolved = verification.verdicts.filter((v) => v.status !== "resolved");

  // dim 5: track resolved signatures
  for (const v of resolved) {
    const g = activeGaps.find((ag) => ag.id === v.gapId);
    if (g) resolvedSignatures.add(g.title);
  }

  log(`Round ${round}: ${resolved.length} resolved, ${unresolved.length} unresolved`);

  activeGaps = activeGaps.filter((g) => unresolved.some((u) => u.gapId === g.id));

  // dim 5: convergence detection
  if (activeGaps.length >= prevActiveCount && round > 1) {
    log(`Convergence stall: ${activeGaps.length} active (was ${prevActiveCount}) — stopping early`);
    break;
  }
  prevActiveCount = activeGaps.length;
}

const verdict = activeGaps.length === 0 ? "clean" : "capped";
log(`Final: ${verdict} after ${round} rounds — residual: ${activeGaps.length}`);

return {
  verdict,
  rounds: round,
  totalGaps: review.gaps.length,
  residual: activeGaps
};
