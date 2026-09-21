"""The display-env sourcing and data-plane writer half of the topology family.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Finds where
a file sources the display environment file, which writer or mirror use is
associated with that source, and the allowance rules (prohibition prose,
negated sources, psql/heredoc mutation detection) for that association."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.governance.entropy_audit.constants import (
    TOPOLOGY_DISPLAY_ENV_PATH_PATTERN,
    TOPOLOGY_DISPLAY_ENV_SOURCE_TO_WRITER_MAX_LINES,
    TOPOLOGY_PSQL_HEREDOC_MAX_LOOKAHEAD,
    TOPOLOGY_SQL_MUTATION_VERB_PATTERN,
)
from scripts.governance.entropy_audit.schema import (
    _TopologyDisplayEnvFacts,
    _TopologyDisplayEnvSource,
    _TopologyDisplayEnvWriterUse,
)
from scripts.governance.entropy_audit.topology_context_rules import _topology_context_is_guardrail_or_test_meta
from scripts.governance.entropy_audit.topology_predicates import _topology_normalized, _topology_relation_clauses


def _topology_display_env_facts(lines: list[str]) -> _TopologyDisplayEnvFacts:
    aliases: set[str] = set()
    sources: list[_TopologyDisplayEnvSource] = []
    writer_uses: list[_TopologyDisplayEnvWriterUse] = []
    for line_no, line in enumerate(lines, start=1):
        normalized = _topology_normalized(line)
        if _topology_line_is_comment_only(normalized):
            continue
        alias = _topology_line_display_env_alias_name(normalized)
        if alias is not None:
            aliases.add(alias)
        if _topology_line_sources_display_env(normalized, aliases):
            sources.append(_TopologyDisplayEnvSource(line_no=line_no, line=line))
        if _topology_line_has_unnegated_data_plane_writer_or_mirror_use(
            normalized
        ) or _topology_line_has_psql_context_mutation_command(lines, line_no - 1):
            writer_uses.append(_TopologyDisplayEnvWriterUse(line_no=line_no, line=line))
    return _TopologyDisplayEnvFacts(sources=tuple(sources), writer_uses=tuple(writer_uses))


def _topology_display_env_associated_writer(
    facts: _TopologyDisplayEnvFacts,
    source_line: int,
    *,
    allow_file_level: bool,
) -> _TopologyDisplayEnvWriterUse | None:
    for writer_use in facts.writer_uses:
        if writer_use.line_no < source_line:
            continue
        if writer_use.line_no - source_line <= 6:
            return writer_use
    if not allow_file_level:
        return None
    for writer_use in facts.writer_uses:
        if writer_use.line_no < source_line:
            continue
        if writer_use.line_no - source_line <= TOPOLOGY_DISPLAY_ENV_SOURCE_TO_WRITER_MAX_LINES:
            return writer_use
    return None


def _topology_display_env_file_allows_file_level_association(relative_path: str) -> bool:
    path = Path(relative_path)
    return relative_path.startswith("scripts/") or path.suffix in {".sh", ".py"}


def _topology_display_env_association_context(
    lines: list[str],
    source_line: int,
    writer_line: int,
) -> str:
    start = max(0, min(source_line, writer_line) - 1)
    end = min(len(lines), max(source_line, writer_line))
    return _topology_normalized("\n".join(lines[start:end]))


def _topology_line_may_source_display_env(line: str) -> bool:
    lowered = _topology_normalized(line)
    return _topology_line_sources_display_env(lowered, set())


def _topology_context_sources_display_env(context: str) -> bool:
    lowered = _topology_normalized(context)
    if not _topology_context_references_display_env_path(lowered):
        return False
    if _topology_context_has_direct_display_env_source(lowered):
        return True
    if _topology_context_has_indirect_display_env_source(lowered):
        return True
    return _topology_context_has_display_env_authority_prose(lowered)


def _topology_context_has_direct_display_env_source(context: str) -> bool:
    return bool(
        re.search(
            rf"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
            context,
        )
        or "--env-file" in context
        and _topology_context_references_display_env_path(context)
        or re.search(
            rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}",
            context,
        )
        or "加载 infra/env/display.env" in context
    )


def _topology_context_has_indirect_display_env_source(context: str) -> bool:
    assignment = re.search(
        rf"\b(?P<name>[a-z_][a-z0-9_]*)\s*=\s*['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
        context,
    )
    if assignment is None:
        return False
    variable = re.escape(assignment.group("name"))
    return bool(
        re.search(
            rf"(?:^|[;&|]\s*|\s+|\bthen\s+)(?:source|\.)\s+"
            rf"['\"]?\$({variable}|\{{{variable}\}})['\"]?",
            context,
        )
    )


def _topology_context_has_display_env_authority_prose(context: str) -> bool:
    if not _topology_context_references_display_env_path(context):
        return False
    if not any(token in context for token in ("database_url", "db url", "dsn", "writer", "ingest")):
        return False
    return any(
        token in context
        for token in (
            " from ",
            " in ",
            "来自 ",
            "authority",
            "权威",
        )
    )


def _topology_line_has_shell_source_command(line: str) -> bool:
    lowered = _topology_normalized(line)
    return bool(
        re.search(
            r"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+"
            rf"(?:['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?|['\"]?\$\{{?[a-z_][a-z0-9_]*\}}?['\"]?)",
            lowered,
        )
    )


def _topology_line_sources_display_env(line: str, aliases: set[str]) -> bool:
    normalized = _topology_normalized(line)
    if _topology_line_has_unnegated_direct_display_env_source(normalized):
        return True
    if _topology_line_sources_display_env_alias(normalized, aliases):
        return True
    return _topology_context_has_display_env_authority_prose(normalized)


def _topology_line_display_env_alias_name(line: str) -> str | None:
    match = re.search(
        rf"\b(?:export\s+)?(?P<name>[a-z_][a-z0-9_]*)\s*=\s*['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?",
        line,
    )
    return match.group("name") if match else None


def _topology_line_sources_display_env_alias(line: str, aliases: set[str]) -> bool:
    for alias in aliases:
        variable = re.escape(alias)
        if re.search(
            rf"(?:^|[;&|]\s*|\bthen\s+)(?:source|\.)\s+['\"]?\$({variable}|\{{{variable}\}})['\"]?",
            line,
        ):
            return True
    return False


def _topology_line_has_unnegated_direct_display_env_source(line: str) -> bool:
    if not _topology_context_references_display_env_path(line):
        return False
    if "--env-file" in line:
        return True
    source_pattern = re.compile(
        rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?"
    )
    for match in source_pattern.finditer(line):
        prefix = line[max(0, match.start() - 28) : match.start()]
        if not _topology_prefix_has_display_env_source_negation(prefix):
            return True
    return "加载 infra/env/display.env" in line and "不要加载" not in line


def _topology_context_references_display_env_path(context: str) -> bool:
    return bool(TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.search(_topology_normalized(context)))


def _topology_prefix_has_display_env_source_negation(prefix: str) -> bool:
    return any(
        token in prefix
        for token in (
            "do not ",
            "never ",
            "not ",
            "no ",
            "不要",
            "不得",
        )
    )


def _topology_line_has_unnegated_data_plane_writer_or_mirror_use(line: str) -> bool:
    normalized = _topology_normalized(line)
    command = _topology_strip_shell_comment(normalized)
    if _topology_line_has_data_plane_writer_command(command):
        return True
    if _topology_line_has_psql_mutation_command(command):
        return True
    if _topology_context_has_negative_writer_or_mirror_terms(normalized):
        return False
    weak_writer_terms = (
        "data-plane",
        "database_url",
        "db url",
        "dsn",
        "ingest",
        "mirror",
        "writer",
        "write",
        "writes",
        "writing",
        "写入",
    )
    return any(
        any(token in clause for token in weak_writer_terms)
        and (
            _topology_context_has_display_env_authority_prose(clause)
            or "authority" in clause
            or "权威" in clause
        )
        for clause in _topology_relation_clauses(normalized)
    )


def _topology_strip_shell_comment(line: str) -> str:
    return re.split(r"\s+#", line, maxsplit=1)[0].strip()


def _topology_line_is_comment_only(line: str) -> bool:
    return _topology_normalized(line).startswith("#")


def _topology_line_has_data_plane_writer_command(line: str) -> bool:
    strong_command_terms = (
        "autopipe",
        "autopipeline",
        "node27_autopipeline",
        "node27_autopipe",
        "node27_mirror_forcing",
        "node27_ingest_run.py",
        "node27_refresh_coverage.py",
        "scripts.node27_autopipeline",
        "scripts.node27_mirror_forcing",
        "scripts.node27_ingest_run",
        "scripts.node27_refresh_coverage",
        "n22_dsn",
    )
    if any(token in line for token in strong_command_terms):
        return True
    return "import-basins-registry" in line and (
        "workers.model_registry.cli" in line or "nhms-model" in line
    )


def _topology_line_has_psql_mutation_command(line: str) -> bool:
    if not _topology_line_invokes_psql(line):
        return False
    return bool(
        TOPOLOGY_SQL_MUTATION_VERB_PATTERN.search(line)
        or re.search(r"(?:^|[\s;&|])(?:-f|--file)(?:\s|=)", line)
    )


def _topology_line_has_psql_context_mutation_command(lines: list[str], index: int) -> bool:
    line = _topology_strip_shell_comment(_topology_normalized(lines[index]))
    if not _topology_line_invokes_psql(line):
        return False
    if _topology_line_has_psql_mutation_command(line):
        return True
    heredoc_token = _topology_psql_heredoc_token(line)
    if heredoc_token is None:
        return False
    end = min(len(lines), index + TOPOLOGY_PSQL_HEREDOC_MAX_LOOKAHEAD + 1)
    for body_line in lines[index + 1 : end]:
        body = _topology_strip_shell_comment(_topology_normalized(body_line))
        if not body:
            continue
        if body == heredoc_token:
            return False
        if TOPOLOGY_SQL_MUTATION_VERB_PATTERN.search(body):
            return True
    return False


def _topology_line_invokes_psql(line: str) -> bool:
    return bool(re.search(r"(?:^|[\s;&|])psql(?:\s|$)", line))


def _topology_psql_heredoc_token(line: str) -> str | None:
    match = re.search(r"<<-?\s*['\"]?(?P<token>[a-z_][a-z0-9_]*)['\"]?", line)
    return match.group("token") if match else None


def _topology_context_has_negative_writer_or_mirror_terms(context: str) -> bool:
    normalized = _topology_normalized(context)
    return any(
        token in normalized
        for token in (
            "no writer credentials",
            "without writer",
            "without a writer",
            "not writer",
            "not a writer",
            "no data-plane writer",
            "not for ingest",
            "do not source",
            "instead of deriving",
            "not derive",
            "without deriving",
            "must not fall back",
            "never reads",
            "never read",
            "不要 source",
            "不要加载",
            "不得 source",
        )
    )


def _topology_context_has_data_plane_writer_or_mirror_terms(context: str) -> bool:
    return _topology_line_has_unnegated_data_plane_writer_or_mirror_use(context)


def _topology_display_env_context_is_allowed(context: str) -> bool:
    if _topology_context_is_guardrail_or_test_meta(context):
        return True
    if _topology_context_has_display_env_prohibition(context) and not (
        _topology_context_has_unnegated_display_env_source(context)
    ):
        return True
    readonly_terms = (
        "display api",
        "display_readonly",
        "readonly",
        "read-only",
        "start-display-api",
        "compose.display",
        "display runtime",
        "只读",
        "展示",
    )
    return any(token in context for token in readonly_terms) and not (
        _topology_context_has_data_plane_writer_or_mirror_terms(context)
    )


def _topology_context_has_display_env_prohibition(context: str) -> bool:
    return any(
        token in context
        for token in (
            "do not source",
            "never reads",
            "never read",
            "not for ingest",
            "forbidden_sources",
            "不要 source",
            "不要加载",
            "不得 source",
        )
    )


def _topology_context_has_unnegated_display_env_source(context: str) -> bool:
    normalized = _topology_normalized(context)
    if "--env-file" in normalized and _topology_context_references_display_env_path(normalized):
        return True
    if _topology_context_has_indirect_display_env_source(normalized):
        return True
    source_pattern = re.compile(
        rf"(?:\b(?:source|sources|sourcing)|\.)\s+['\"]?{TOPOLOGY_DISPLAY_ENV_PATH_PATTERN.pattern}['\"]?"
    )
    for match in source_pattern.finditer(normalized):
        prefix = normalized[max(0, match.start() - 24) : match.start()]
        if not _topology_prefix_has_display_env_source_negation(prefix):
            return True
    return False
