"""Assemble the readiness evidence index + emit §2.7 and §3.1 pass logs.

Task 3.1: index every artifact under ``evidence/`` with its sha256 + binder
header binders (baseline_commit, manifest_sha256, optional code_carrier_sha).
Then run the cross-artifact consistency check:

    - Every pass log's line 1 MUST match the binder pattern with
      ``baseline_commit`` == the pinned baseline AND ``manifest_sha256`` ==
      the pinned manifest SHA-256 (bbbc4143...).
    - Every pass log's optional line 2 (``# code_carrier_sha=<40-hex>``) is
      parsed; when present and distinct from baseline_commit, the driver
      asserts ``git diff <baseline>..<carrier> -- workers/ apps/ services/
      packages/ db/ schemas/`` returns EMPTY (no manifest-identity drift).
    - Grandfather clause: ``db-registration-2.3.node-27.pass.log`` was
      captured before the code_carrier_sha contract landed. The driver
      verifies the sibling ``smoke-2.4.node-27.pass.log`` retro-attest
      shares the same code_carrier_sha and passes the tree-equivalence
      check, then permits the missing second line on §2.3.

Task 2.7: emit the certification statement + drift record + evidence-set
enumeration. Explicit non-goal reaffirmation per design.md
§"Why checkbox state is not evidence".

Task 3.2 is out of this driver's scope (openspec validate + ruff), but the
same emitted binder header format is reused by the §3.2 capture step.

Read-only against production paths. Emits three artifacts under this dir:

    - evidence-index.v1.json
    - cross-artifact-consistency-3.1.pass.log
    - certification-2.7.pass.log

stdlib-only. Exit 0 with PASS on success, 1 with FAIL:<key>:<reason>.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]  # openspec/changes/<change>/evidence/ -> repo root

MANIFEST_FILENAME = "readiness-manifest.v1.json"
MANIFEST_SHA_FILENAME = "readiness-manifest.v1.json.sha256"
PINNED_BASELINE_COMMIT = "5e518c151375b798c29ee3cafb3260413ac8905f"
PINNED_MANIFEST_SHA256 = "bbbc4143d228dc36d6f0973a51060a9debe54b81f49505682de709ded88eeeaf"

# Manifest-identity paths tree-equivalence is verified against when a
# pass log records a code_carrier_sha distinct from the baseline commit.
# Ordered per design.md §"How evidence is bound to commits".
TREE_EQUIVALENCE_PATHS = ("workers/", "apps/", "services/", "packages/", "db/", "schemas/")

# Binder header pattern: line 1 of every pass log.
# Format per design.md line 42:
#   # captured at <ISO-8601 UTC> host=<h> bound to baseline_commit=<40-hex> manifest_sha256=<64-hex>
BINDER_LINE_RE = re.compile(
    r"^# captured at (?P<captured_utc>\S+) "
    r"host=(?P<host>\S+) bound to "
    r"baseline_commit=(?P<baseline_commit>[0-9a-f]{40}) "
    r"manifest_sha256=(?P<manifest_sha256>[0-9a-f]{64})\s*$"
)
CODE_CARRIER_LINE_RE = re.compile(r"^# code_carrier_sha=(?P<carrier>[0-9a-f]{40})\s*$")

# §2.3 grandfather clause: this log was captured before the code_carrier_sha
# contract landed (§2.4 Round-1 fix). Its retro-attest lives in the sibling
# smoke-2.4 log (same code_carrier_sha, same manifest-identity paths verified
# empty-diff). The indexer records the linkage and does not block.
GRANDFATHERED_LOG = "db-registration-2.3.node-27.pass.log"
GRANDFATHER_ATTESTER = "smoke-2.4.node-27.pass.log"

# The seven pass logs that constitute the §2.7 readiness certification set.
CERTIFICATION_PASS_LOGS = (
    "check_manifest_completeness.v1.pass.log",  # §1.3
    "pytest-2.1.node-27.pass.log",              # §2.1
    "pytest-2.2.node-27.pass.log",              # §2.2
    "db-registration-2.3.node-27.pass.log",     # §2.3
    "smoke-2.4.node-27.pass.log",               # §2.4
    "staging-2.5.node-22.pass.log",             # §2.5
    "capacity-2.6.node-27.pass.log",            # §2.6
)

# Artifacts to index in evidence-index.v1.json. All resolved relative to HERE.
INDEXED_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("manifest", MANIFEST_FILENAME),
    ("manifest_sha256", MANIFEST_SHA_FILENAME),
    ("check_manifest_completeness", "check_manifest_completeness.v1.pass.log"),
    ("pytest_2_1", "pytest-2.1.node-27.pass.log"),
    ("pytest_2_2", "pytest-2.2.node-27.pass.log"),
    ("db_registration_2_3", "db-registration-2.3.node-27.pass.log"),
    ("smoke_2_4", "smoke-2.4.node-27.pass.log"),
    ("staging_2_5", "staging-2.5.node-22.pass.log"),
    ("capacity_2_6", "capacity-2.6.node-27.pass.log"),
    ("spec_code_drift_log", "spec-code-drift-log.md"),
)


def fail(key: str, reason: str) -> None:
    print(f"FAIL:{key}:{reason}", file=sys.stderr)
    sys.exit(1)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_binder(log_path: Path) -> tuple[dict, str | None]:
    """Return (binder_dict, code_carrier_sha_or_None) for a pass log.

    binder_dict carries {captured_utc, host, baseline_commit, manifest_sha256}.
    """
    with log_path.open("r", encoding="utf-8") as f:
        line1 = f.readline().rstrip("\n")
        line2 = f.readline().rstrip("\n")
    m1 = BINDER_LINE_RE.match(line1)
    if m1 is None:
        fail(log_path.name, f"binder header line 1 does not match contract: {line1!r}")
    binder = {
        "captured_utc": m1.group("captured_utc"),
        "host": m1.group("host"),
        "baseline_commit": m1.group("baseline_commit"),
        "manifest_sha256": m1.group("manifest_sha256"),
    }
    m2 = CODE_CARRIER_LINE_RE.match(line2)
    carrier = m2.group("carrier") if m2 else None
    return binder, carrier


def tree_equivalent(baseline: str, carrier: str) -> tuple[bool, str]:
    """Return (True, "") when git diff between baseline..carrier is empty on
    manifest-identity paths; (False, non-empty-diff) otherwise.
    """
    cmd = [
        "git", "-C", str(REPO_ROOT), "diff",
        f"{baseline}..{carrier}", "--", *TREE_EQUIVALENCE_PATHS,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        fail(
            f"git-diff:{baseline[:8]}..{carrier[:8]}",
            f"git diff failed rc={proc.returncode} stderr={proc.stderr.strip()!r}",
        )
    return (len(proc.stdout) == 0, proc.stdout)


def now_utc_iso() -> str:
    # Second-precision ISO-8601 UTC, matches existing pass log format.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_index() -> tuple[dict, list[dict], str]:
    """Build the evidence-index.v1.json body.

    Returns (index_dict, per_log_rows_for_consistency_check, current_head_sha).
    """
    current_head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    artifacts: list[dict] = []
    per_log_rows: list[dict] = []
    for logical_key, filename in INDEXED_ARTIFACTS:
        path = HERE / filename
        if not path.is_file():
            fail(filename, "artifact missing under evidence/")
        entry: dict = {
            "key": logical_key,
            "filename": filename,
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
        }
        if filename.endswith(".pass.log"):
            binder, carrier = parse_binder(path)
            entry["binder"] = binder
            entry["code_carrier_sha"] = carrier  # None when omitted (§2.3)
            entry["grandfathered_missing_code_carrier"] = (
                carrier is None and filename == GRANDFATHERED_LOG
            )
            per_log_rows.append({
                "filename": filename,
                "binder": binder,
                "code_carrier_sha": carrier,
            })
        artifacts.append(entry)

    # Also index the synthetic-package README (the package aggregate sha256
    # is captured inside package/package.manifest.sha256; the README carries
    # the construction provenance for §2.3 D1). The package itself is a
    # sibling asset — a full-file sha256 index would rehash every station
    # CSV; instead, we reference the committed package.manifest.sha256 as
    # the aggregate binder.
    synth_readme = HERE / "synthetic-package" / "README.md"
    synth_aggregate = HERE / "synthetic-package" / "package" / "package.manifest.sha256"
    if not synth_readme.is_file():
        fail("synthetic-package/README.md", "artifact missing")
    if not synth_aggregate.is_file():
        fail(
            "synthetic-package/package/package.manifest.sha256",
            "aggregate sha256 sidecar missing",
        )
    artifacts.append({
        "key": "synthetic_package_readme",
        "filename": "synthetic-package/README.md",
        "sha256": sha256_of(synth_readme),
        "bytes": synth_readme.stat().st_size,
    })
    artifacts.append({
        "key": "synthetic_package_aggregate_sha256",
        "filename": "synthetic-package/package/package.manifest.sha256",
        "sha256": sha256_of(synth_aggregate),
        "bytes": synth_aggregate.stat().st_size,
        "recorded_aggregate": synth_aggregate.read_text(encoding="utf-8").strip(),
    })

    index = {
        "index_schema_version": "v1",
        "index_created_utc": now_utc_iso(),
        "pinned_baseline_commit": PINNED_BASELINE_COMMIT,
        "pinned_manifest_sha256": PINNED_MANIFEST_SHA256,
        "index_generator_head_sha": current_head,
        "tree_equivalence_paths": list(TREE_EQUIVALENCE_PATHS),
        "grandfathered_missing_code_carrier": {
            "log": GRANDFATHERED_LOG,
            "retro_attester": GRANDFATHER_ATTESTER,
            "note": (
                "§2.3 D3 was captured before the code_carrier_sha binder line "
                "was contracted (§2.4 Round-1 fix, per design.md §\"How evidence "
                "is bound to commits\" scope note). Its retro-attest lives in "
                "the sibling §2.4 smoke log which carries the same "
                "code_carrier_sha and asserts empty tree-diff on manifest-identity "
                "paths. Indexer records the linkage and permits the missing line."
            ),
        },
        "artifacts": artifacts,
    }
    return index, per_log_rows, current_head


def run_consistency_check(per_log_rows: list[dict]) -> tuple[str, list[dict]]:
    """Run cross-artifact consistency check per §3.1 spec.

    Returns (verdict, table_rows). verdict is "PASS" on success (else the
    function exits via fail()).
    """
    table_rows: list[dict] = []
    for row in per_log_rows:
        filename = row["filename"]
        binder = row["binder"]
        carrier = row["code_carrier_sha"]
        # Assert baseline_commit identity.
        if binder["baseline_commit"] != PINNED_BASELINE_COMMIT:
            fail(
                filename,
                f"baseline_commit={binder['baseline_commit']!r} != "
                f"pinned={PINNED_BASELINE_COMMIT!r}",
            )
        # Assert manifest_sha256 identity.
        if binder["manifest_sha256"] != PINNED_MANIFEST_SHA256:
            fail(
                filename,
                f"manifest_sha256={binder['manifest_sha256']!r} != "
                f"pinned={PINNED_MANIFEST_SHA256!r}",
            )
        # Tree-equivalence for code_carrier_sha distinct from baseline.
        tree_ok: bool | None
        tree_note: str
        if carrier is None:
            if filename == GRANDFATHERED_LOG:
                # Look up sibling retro-attester carrier.
                attester_row = next(
                    (r for r in per_log_rows if r["filename"] == GRANDFATHER_ATTESTER),
                    None,
                )
                if attester_row is None:
                    fail(
                        filename,
                        f"grandfather retro-attester {GRANDFATHER_ATTESTER!r} not indexed",
                    )
                attester_carrier = attester_row["code_carrier_sha"]
                if attester_carrier is None:
                    fail(
                        filename,
                        f"grandfather retro-attester {GRANDFATHER_ATTESTER!r} "
                        f"has no code_carrier_sha line",
                    )
                # Re-verify tree-equivalence on the retro-attester's carrier
                # so the grandfather clause carries a live proof, not just
                # a string reference.
                tree_ok, diff_output = tree_equivalent(
                    PINNED_BASELINE_COMMIT, attester_carrier,
                )
                if not tree_ok:
                    fail(
                        filename,
                        f"grandfather retro-attest tree-diff non-empty: "
                        f"{diff_output[:200]!r}",
                    )
                tree_note = (
                    f"grandfathered — retro-attest via {GRANDFATHER_ATTESTER} "
                    f"(carrier={attester_carrier[:8]}), tree-diff empty on "
                    f"manifest-identity paths"
                )
            else:
                # No code_carrier_sha line and not grandfathered means the
                # carrier == baseline_commit (evidence artifact existed at
                # baseline). This is the §2.1/§2.2 case (pytest re-runs on
                # existing tests) plus §1.3 (check_manifest_completeness.py
                # committed before manifest freeze). We record it explicitly.
                tree_ok = True
                tree_note = "code_carrier_sha omitted (carrier == baseline_commit)"
        else:
            if carrier == PINNED_BASELINE_COMMIT:
                tree_ok = True
                tree_note = "code_carrier_sha == baseline_commit (no diff to check)"
            else:
                tree_ok, diff_output = tree_equivalent(PINNED_BASELINE_COMMIT, carrier)
                if not tree_ok:
                    fail(
                        filename,
                        f"tree-diff between baseline..carrier={carrier[:8]} "
                        f"non-empty on manifest-identity paths: {diff_output[:200]!r}",
                    )
                tree_note = (
                    f"tree-diff empty on manifest-identity paths for "
                    f"baseline..{carrier[:8]}"
                )
        table_rows.append({
            "filename": filename,
            "captured_utc": binder["captured_utc"],
            "host": binder["host"],
            "baseline_commit_match": True,
            "manifest_sha256_match": True,
            "code_carrier_sha": carrier if carrier else "(omitted)",
            "tree_equivalence": tree_note,
        })
    return "PASS", table_rows


def emit_consistency_log(table_rows: list[dict], current_head: str) -> Path:
    """Write cross-artifact-consistency-3.1.pass.log with binder header."""
    out = HERE / "cross-artifact-consistency-3.1.pass.log"
    captured = now_utc_iso()
    lines: list[str] = []
    lines.append(
        f"# captured at {captured} host=local bound to "
        f"baseline_commit={PINNED_BASELINE_COMMIT} "
        f"manifest_sha256={PINNED_MANIFEST_SHA256}"
    )
    lines.append(f"# code_carrier_sha={current_head}")
    lines.append(
        "# task: cmfd-direct-grid-platform-readiness §3.1 cross-artifact "
        "consistency check"
    )
    lines.append(
        "# generator: openspec/changes/cmfd-direct-grid-platform-readiness/"
        "evidence/assemble-evidence-index.py"
    )
    lines.append(
        "# scope: parses every §1.3/§2.1-§2.6 pass log's binder header lines "
        "(1 = captured/host/baseline/manifest; 2 = optional code_carrier_sha) "
        "and asserts:"
    )
    lines.append(
        "#   (1) baseline_commit identity across all indexed pass logs"
    )
    lines.append(
        "#   (2) manifest_sha256 identity across all indexed pass logs"
    )
    lines.append(
        "#   (3) when code_carrier_sha is present and != baseline_commit, "
        "empty tree-diff on manifest-identity paths"
    )
    lines.append(
        f"#       ({' '.join(TREE_EQUIVALENCE_PATHS)})"
    )
    lines.append(
        f"#   (4) grandfather clause: {GRANDFATHERED_LOG} missing line-2 is "
        f"covered by {GRANDFATHER_ATTESTER} retro-attest sharing the same "
        "code_carrier_sha; the driver re-verifies the retro-attest tree-diff "
        "live rather than trusting the string reference."
    )
    lines.append("")

    # Per-artifact table.
    lines.append("## Per-artifact consistency table")
    header = "filename | captured_utc | host | baseline_match | manifest_match | code_carrier_sha | tree_equivalence"
    lines.append(header)
    lines.append("-" * len(header))
    for row in table_rows:
        lines.append(
            f"{row['filename']} | {row['captured_utc']} | {row['host']} | "
            f"{row['baseline_commit_match']} | {row['manifest_sha256_match']} | "
            f"{row['code_carrier_sha']} | {row['tree_equivalence']}"
        )
    lines.append("")

    lines.append("## Verdict")
    lines.append(
        f"PASS — all {len(table_rows)} indexed pass logs reference the pinned "
        f"baseline_commit ({PINNED_BASELINE_COMMIT}) and the pinned "
        f"manifest_sha256 ({PINNED_MANIFEST_SHA256[:16]}...). Every pass log "
        f"that records a code_carrier_sha distinct from baseline was verified "
        f"to have empty tree-diff on manifest-identity paths. The "
        f"{GRANDFATHERED_LOG} missing line-2 is covered by the "
        f"{GRANDFATHER_ATTESTER} retro-attest (indexer re-verified the "
        f"retro-attester's tree-equivalence live at generation time)."
    )
    lines.append("PASS")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def emit_certification_log(index: dict, current_head: str) -> Path:
    """Write certification-2.7.pass.log with binder header + certification body."""
    out = HERE / "certification-2.7.pass.log"
    captured = now_utc_iso()
    lines: list[str] = []
    lines.append(
        f"# captured at {captured} host=local bound to "
        f"baseline_commit={PINNED_BASELINE_COMMIT} "
        f"manifest_sha256={PINNED_MANIFEST_SHA256}"
    )
    lines.append(f"# code_carrier_sha={current_head}")
    lines.append(
        "# task: cmfd-direct-grid-platform-readiness §2.7 certification "
        "statement + drift record + evidence-set enumeration"
    )
    lines.append(
        "# generator: openspec/changes/cmfd-direct-grid-platform-readiness/"
        "evidence/assemble-evidence-index.py"
    )
    lines.append("")

    lines.append("## C1. Certification statement (design.md §\"Why checkbox state is not evidence\")")
    lines.append(
        "Readiness for the cmfd-direct-grid-platform-readiness change is "
        "CERTIFIED on the pinned-commit evidence set enumerated in C2 below, "
        "NOT on OpenSpec checkbox state. Per design.md §\"Why checkbox state "
        "is not evidence\" and specs/direct-grid-readiness-evidence/spec.md "
        "Requirement \"Readiness is judged on pinned-commit evidence not "
        "checkbox state\", a checked box in tasks.md means \"a task was marked "
        "done\" — it does NOT mean \"the pinned release passes\". This receipt "
        "explicitly records that certification requires the pinned manifest, "
        "passing re-run evidence, the node-27 smoke, the node-22 minimal-basin "
        "staging execution, and the G9 capacity baseline with no unresolved "
        "limit breach. Any future audit against this change MUST verify the C2 "
        "artifact set on the pinned baseline, not the tasks.md checkbox state."
    )
    lines.append("")

    lines.append("## C2. Certification evidence set")
    lines.append(
        f"Pinned baseline_commit  = {PINNED_BASELINE_COMMIT}"
    )
    lines.append(
        f"Pinned manifest_sha256  = {PINNED_MANIFEST_SHA256}"
    )
    lines.append(
        "The following artifacts constitute the readiness certification set. "
        "Every pass log carries the binder header (line 1 = "
        "captured_utc/host/baseline_commit/manifest_sha256; line 2 = optional "
        "code_carrier_sha for post-baseline carriers). The evidence-index.v1.json "
        "record enumerates each artifact's sha256 + binder + optional "
        "code_carrier_sha. The cross-artifact-consistency-3.1.pass.log records "
        "the per-artifact identity and tree-equivalence verdict."
    )
    lines.append("")

    # Enumerate every artifact from the index.
    lines.append("### C2.1 Manifest + companion")
    for entry in index["artifacts"]:
        if entry["key"] in {"manifest", "manifest_sha256"}:
            lines.append(f"  * {entry['filename']}  sha256={entry['sha256']}")
    lines.append("")

    lines.append("### C2.2 §1.3 manifest completeness gate")
    for entry in index["artifacts"]:
        if entry["key"] == "check_manifest_completeness":
            lines.append(f"  * {entry['filename']}  sha256={entry['sha256']}")
    lines.append("")

    lines.append("### C2.3 §2.1-§2.6 pass logs")
    section_map = {
        "pytest_2_1": "§2.1",
        "pytest_2_2": "§2.2",
        "db_registration_2_3": "§2.3",
        "smoke_2_4": "§2.4",
        "staging_2_5": "§2.5",
        "capacity_2_6": "§2.6",
    }
    for entry in index["artifacts"]:
        if entry["key"] in section_map:
            sec = section_map[entry["key"]]
            carrier = entry.get("code_carrier_sha") or "(omitted)"
            lines.append(
                f"  * {sec} {entry['filename']}  sha256={entry['sha256']}  "
                f"code_carrier_sha={carrier}"
            )
    lines.append("")

    lines.append("### C2.4 §2.3 synthetic package binders")
    for entry in index["artifacts"]:
        if entry["key"] in {"synthetic_package_readme", "synthetic_package_aggregate_sha256"}:
            extra = (
                f"  aggregate={entry.get('recorded_aggregate', '')}"
                if entry["key"] == "synthetic_package_aggregate_sha256"
                else ""
            )
            lines.append(f"  * {entry['filename']}  sha256={entry['sha256']}{extra}")
    lines.append("")

    lines.append("### C2.5 §2.7 drift record")
    for entry in index["artifacts"]:
        if entry["key"] == "spec_code_drift_log":
            lines.append(f"  * {entry['filename']}  sha256={entry['sha256']}")
    lines.append("")

    lines.append("## C3. Drift record (spec-code-drift-log.md)")
    lines.append(
        "The following spec-vs-code drift entries were observed during PR "
        "review and are recorded in spec-code-drift-log.md rather than assumed "
        "absent (per specs/direct-grid-readiness-evidence/spec.md Scenario "
        "\"Checkbox completion does not certify readiness\" clause \"any drift "
        "between OpenSpec task state and code state is recorded in the "
        "evidence rather than assumed absent\"):"
    )
    lines.append("")
    lines.append(
        "  * Drift entry 1: shud-runtime spec says `shud`, code default at "
        "workers/shud_runtime/runtime.py:74/85/95 says `shud_omp`. Reconciled "
        "at the deployment envelope via SHUD_EXECUTABLE env override (deployed "
        "binary sha256=ef2f6181..., per staging-2.5.node-22.pass.log D1). "
        "Follow-up change proposed to flip the Python default; deferred as "
        "out of Epic #886 readiness-gate scope."
    )
    lines.append(
        "  * Drift entry 2: Minimal-basin-execution spec scenario narrowed "
        "in-PR from \"SHALL execute end-to-end\" to \"SHALL stage and record "
        "production binary identity\" because the §2.3 synthetic package is a "
        "direct-grid CONTRACT fixture (.sp.att + .tsd.forc + station CSVs + "
        "binding-manifest) — not a full SHUD project. End-to-end simulation "
        "requires either a full-tree synth basin or an operator-staged small "
        "real basin and is deferred to a follow-up change."
    )
    lines.append("")

    lines.append("## C4. G9 capacity-limit breach verdict")
    lines.append(
        "capacity-2.6.node-27.pass.log D7 records verdict = NO BREACH: the "
        "live legacy baseline (13 basins / 6,290 stations / ~121M rows per "
        "2 weeks) fits within all pinned producer + runtime staging limits. "
        "0 unresolved capacity-limit breaches; §2.7 certification precondition "
        "\"no unresolved limit breach\" is satisfied."
    )
    lines.append("")

    lines.append("## C5. Non-goal reaffirmation")
    lines.append(
        "Per tasks.md §2.7 non-goal: this receipt makes no change to OpenSpec "
        "tooling or checkbox semantics. It is an evidence statement only. "
        "Per §1.3 non-goal (post-§3.1): the readiness manifest v1 is NOT "
        "amended by this driver — its checksum remains bbbc4143... The "
        "§3.1 cross-artifact consistency check is the certification gate; "
        "post-§3.1 manifest immutability is absolute per design.md §\"How "
        "evidence is bound to commits\"."
    )
    lines.append("")

    lines.append("## Verdict")
    lines.append(
        "PASS — readiness for cmfd-direct-grid-platform-readiness is "
        "certified on the pinned-commit evidence set enumerated above. "
        "Checkbox state is not treated as evidence."
    )
    lines.append("PASS")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    # Sanity: pinned manifest sha256 matches the committed .sha256 file.
    committed_sha = (HERE / MANIFEST_SHA_FILENAME).read_text(encoding="utf-8").strip().split()[0]
    if committed_sha != PINNED_MANIFEST_SHA256:
        fail(
            MANIFEST_SHA_FILENAME,
            f"pinned sha ({PINNED_MANIFEST_SHA256}) != committed "
            f"({committed_sha}); manifest may have drifted",
        )
    # Sanity: file bytes hash equals recorded sha256.
    recomputed = sha256_of(HERE / MANIFEST_FILENAME)
    if recomputed != PINNED_MANIFEST_SHA256:
        fail(
            MANIFEST_FILENAME,
            f"pinned sha ({PINNED_MANIFEST_SHA256}) != file recompute "
            f"({recomputed}); manifest was mutated",
        )

    index, per_log_rows, current_head = build_index()

    # Write evidence-index.v1.json before consistency check so a fail-fast
    # still leaves the index observable for debugging.
    index_path = HERE / "evidence-index.v1.json"
    index_path.write_text(
        json.dumps(index, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    verdict, table_rows = run_consistency_check(per_log_rows)
    if verdict != "PASS":
        fail("consistency", f"unexpected verdict {verdict!r}")

    consistency_path = emit_consistency_log(table_rows, current_head)
    certification_path = emit_certification_log(index, current_head)

    # Terminal summary (stdout).
    print(f"PASS: emitted {index_path.name}")
    print(f"PASS: emitted {consistency_path.name}")
    print(f"PASS: emitted {certification_path.name}")
    print(f"PASS: current_head={current_head}")
    print(
        f"PASS: {len(per_log_rows)} pass logs indexed, all bound to "
        f"baseline_commit={PINNED_BASELINE_COMMIT[:8]} + "
        f"manifest_sha256={PINNED_MANIFEST_SHA256[:8]}..."
    )
    print("PASS")


if __name__ == "__main__":
    main()
