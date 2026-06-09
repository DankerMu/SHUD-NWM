from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEDGER = Path("docs/bugs.md")
REQUIRED_BUG_IDS = (
    "BUG-20260527-003",
    "BUG-20260527-007",
    "BUG-20260527-008",
    "BUG-20260527-009",
    "BUG-20260527-010",
    "BUG-20260527-011",
    "BUG-20260527-012",
    "BUG-20260527-013",
)
ALLOWED_STATUSES = frozenset(
    {"open", "resolved", "superseded", "stale-needs-repro", "archived"}
)
ALLOWED_OWNER_AREAS = frozenset(
    {"compute_control", "display_readonly", "slurm_gateway", "shared_contract"}
)
REQUIRED_FIELDS = frozenset({"status", "owner_area", "evidence", "retest_command"})


@dataclass(frozen=True)
class LedgerEntry:
    bug_id: str
    body: str
    fields: dict[str, str]
    retest_command: str


class LedgerError(ValueError):
    pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _fold_block(lines: list[str]) -> str:
    parts: list[str] = []
    for line in lines:
        if line == "":
            if parts and not parts[-1].endswith("\n"):
                parts[-1] += "\n"
            elif not parts:
                parts.append("\n")
            else:
                parts[-1] += "\n"
            continue
        if not parts or parts[-1].endswith("\n"):
            parts.append(line)
        else:
            parts[-1] += " " + line
    return "".join(parts)


def _deindent_block(lines: list[str], bug_id: str, field_name: str) -> list[str]:
    content = list(lines)
    while content and content[-1] == "":
        content.pop()
    indents = [len(line) - len(line.lstrip(" ")) for line in content if line.strip()]
    if not indents:
        raise LedgerError(f"{bug_id}: {field_name} block has no content")
    indent = min(indents)
    if indent == 0:
        raise LedgerError(f"{bug_id}: {field_name} block content must be indented")
    return [line[indent:] if line.strip() else "" for line in content]


def _parse_retest_command(body: str, bug_id: str) -> str:
    lines = body.splitlines()
    start_index = None
    style = None
    for index, line in enumerate(lines):
        match = re.match(r"^retest_command:\s*([>|])([-+]?)\s*$", line)
        if match:
            start_index = index
            style = match.group(1)
            break
        if re.match(r"^retest_command:\s*\S", line):
            raise LedgerError(
                f"{bug_id}: retest_command must use a folded or literal block scalar"
            )
    if start_index is None or style is None:
        raise LedgerError(f"{bug_id}: missing retest_command block")

    raw_block: list[str] = []
    for line in lines[start_index + 1 :]:
        if re.match(r"^[a-z_]+:", line):
            break
        raw_block.append(line)

    deindented = _deindent_block(raw_block, bug_id, "retest_command")
    if style == "|":
        return "\n".join(deindented)
    return _fold_block(deindented)


def _parse_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = re.match(r"^([a-z_]+):(?:\s*(.*))?$", line)
        if match:
            fields[match.group(1)] = _unquote(match.group(2) or "")
    return fields


def _extract_entry(text: str, bug_id: str) -> LedgerEntry:
    pattern = re.compile(
        rf"^### {re.escape(bug_id)}:.*?\n\n```yaml\n(?P<body>.*?)\n```",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise LedgerError(f"{bug_id}: missing fenced yaml ledger block")

    body = match.group("body")
    fields = _parse_fields(body)
    retest_command = _parse_retest_command(body, bug_id)
    return LedgerEntry(
        bug_id=bug_id,
        body=body,
        fields=fields,
        retest_command=retest_command,
    )


def _validate_fields(entry: LedgerEntry) -> None:
    missing = sorted(REQUIRED_FIELDS - entry.fields.keys())
    if missing:
        raise LedgerError(f"{entry.bug_id}: missing required fields: {', '.join(missing)}")

    status = entry.fields["status"]
    if status not in ALLOWED_STATUSES:
        allowed = ", ".join(sorted(ALLOWED_STATUSES))
        raise LedgerError(f"{entry.bug_id}: invalid status {status!r}; allowed: {allowed}")

    owner_area = entry.fields["owner_area"]
    if owner_area not in ALLOWED_OWNER_AREAS:
        allowed = ", ".join(sorted(ALLOWED_OWNER_AREAS))
        raise LedgerError(
            f"{entry.bug_id}: invalid owner_area {owner_area!r}; allowed: {allowed}"
        )

    if not re.search(r"(?m)^evidence:\n(?:  .*\n)*  - \S", entry.body):
        raise LedgerError(f"{entry.bug_id}: evidence must contain at least one list item")

    if status == "resolved" and not entry.fields.get("resolved_by"):
        raise LedgerError(f"{entry.bug_id}: resolved entry must include resolved_by")
    if status == "superseded" and not entry.fields.get("superseded_by"):
        raise LedgerError(f"{entry.bug_id}: superseded entry must include superseded_by")


def _validate_shell(entry: LedgerEntry) -> None:
    result = subprocess.run(
        ["bash", "-n"],
        input=entry.retest_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "bash -n failed"
        raise LedgerError(f"{entry.bug_id}: retest_command shell syntax failed: {stderr}")


def _require_bug_003_psql_scalar(entry: LedgerEntry) -> None:
    command = entry.retest_command
    grep_index = command.find("grep -qx t")
    if grep_index == -1:
        raise LedgerError(f"{entry.bug_id}: retest_command must end the psql probe with grep -qx t")
    before_grep = command[:grep_index]
    if not re.search(r"\bpsql\b(?:(?!\|).)*\s-Atq?\b", before_grep, re.DOTALL):
        raise LedgerError(
            f"{entry.bug_id}: psql probe must use scalar output flags -At or -Atq before grep -qx t"
        )


def _require_bug_008_tests(entry: LedgerEntry) -> None:
    command = entry.retest_command
    required_parts = (
        "tests/test_qhh_production_bootstrap.py",
        "tests/test_basins_registry_import.py",
        "tests/test_production_scheduler.py",
        "-k output_segment_count",
    )
    missing = [part for part in required_parts if part not in command]
    if missing:
        raise LedgerError(
            f"{entry.bug_id}: retest_command missing intended test selector(s): {', '.join(missing)}"
        )


def _require_bug_010_excludes_job_not_found(entry: LedgerEntry) -> None:
    if "JOB_NOT_FOUND" in entry.retest_command:
        raise LedgerError(f"{entry.bug_id}: retest_command must not accept JOB_NOT_FOUND")


def _validate_targeted_invariants(entry: LedgerEntry) -> None:
    if entry.bug_id == "BUG-20260527-003":
        _require_bug_003_psql_scalar(entry)
    elif entry.bug_id == "BUG-20260527-008":
        _require_bug_008_tests(entry)
    elif entry.bug_id == "BUG-20260527-010":
        _require_bug_010_excludes_job_not_found(entry)


def validate_ledger(path: Path) -> list[LedgerEntry]:
    text = path.read_text(encoding="utf-8")
    entries = [_extract_entry(text, bug_id) for bug_id in REQUIRED_BUG_IDS]
    for entry in entries:
        _validate_fields(entry)
        _validate_shell(entry)
        _validate_targeted_invariants(entry)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate governed bugs ledger entries and retest shell snippets."
    )
    parser.add_argument(
        "ledger",
        nargs="?",
        type=Path,
        default=DEFAULT_LEDGER,
        help=f"Ledger markdown file to validate. Defaults to {DEFAULT_LEDGER}.",
    )
    args = parser.parse_args(argv)

    try:
        entries = validate_ledger(args.ledger)
    except (OSError, LedgerError) as exc:
        print(f"bugs ledger validation failed: {exc}", file=sys.stderr)
        return 1

    for entry in entries:
        print(
            f"{entry.bug_id}: {entry.fields['status']} "
            f"{entry.fields['owner_area']} retest-shell-ok"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
