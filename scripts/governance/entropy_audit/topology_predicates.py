"""Line-level predicates of the node-22 database-writer topology family.

Split out of ``scripts/governance/audit_repo_entropy.py`` by #1842. Pure text
predicates over a single line or clause: normalization, node-22 / database /
writer mention tests, the relation-clause split, and the local-postgres and
mirror drift line tests."""

from __future__ import annotations

import re


def _topology_normalized(text: str) -> str:
    normalized = text.lower().replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", normalized).strip()


def _topology_line_may_start_node22_db_writer_claim(line: str) -> bool:
    if any(
        _topology_mentions_node22(clause)
        and (
            _topology_mentions_writer(clause)
            or "active primary" in clause
            or "current primary" in clause
            or re.search(r"\b(?:is|as|host|hosts|hosted|own|owns|owned)\s+(?:the\s+)?active\b", clause)
            or "主库" in clause
        )
        for clause in _topology_relation_clauses(line)
    ):
        return True
    return _topology_line_is_standalone_node22_anchor(line)


def _topology_line_is_standalone_node22_anchor(line: str) -> bool:
    normalized = _topology_normalized(line)
    return bool(re.fullmatch(r"(?:[#>*+\-\d.)\s|`]*\|?\s*)?node[-_ ]?22\s*\|?\s*:?", normalized))


def _topology_line_may_have_node22_db_writer_drift(line: str) -> bool:
    return any(
        _topology_mentions_node22(clause)
        and _topology_mentions_database(clause)
        and (
            _topology_mentions_writer(clause)
            or _topology_mentions_active_primary_database_authority(clause)
        )
        for clause in _topology_relation_clauses(line)
    )


def _topology_node22_db_writer_drift_clauses(text: str) -> tuple[str, ...]:
    return tuple(
        clause
        for clause in _topology_relation_clauses(text)
        if _topology_mentions_node22(clause)
        and _topology_mentions_database(clause)
        and (
            _topology_mentions_writer(clause)
            or _topology_mentions_active_primary_database_authority(clause)
        )
    )


def _topology_relation_clauses(text: str) -> tuple[str, ...]:
    normalized = _topology_normalized(text)
    return tuple(
        clause.strip()
        for clause in re.split(
            r"\s*(?:[,;，；。]|\.(?:\s+|$)|\s+-\s+|\s+while\s+|\s+whereas\s+|\s+but\s+|\s+and\s+display\s+|\s+and\s+node-27\s+)\s*",
            normalized,
        )
        if clause.strip()
    )


def _topology_mentions_node22(text: str) -> bool:
    lowered = _topology_normalized(text)
    return bool(
        re.search(
            r"\bnode[-_ ]?22\b|(?:^|[|:]\s*|^(?:[-*+]|\d+[.)])\s+)22(?!\d)\s*"
            r"(?:is|as|own|owns|owned|host|hosts|node|节点|写入|写|拥有|postgres|postgresql|pg|db|数据库)",
            lowered,
        )
    )


def _topology_mentions_database(text: str) -> bool:
    lowered = _topology_normalized(text)
    return bool(re.search(r"\b(?:db|pg)\b", lowered)) or any(
        token in lowered
        for token in (
            "database",
            "postgres",
            "postgresql",
            "数据库",
            "主库",
            "55433",
        )
    )


def _topology_mentions_writer(text: str) -> bool:
    lowered = _topology_normalized(text)
    return any(
        token in lowered
        for token in (
            "active primary",
            "host active",
            "hosts active",
            "hosted active",
            "host primary",
            "hosts primary",
            "writer",
            "writes",
            "writing",
            "writable",
            "hosted writer",
            "mutation",
            "mutate",
            "owns database",
            "owns db",
            "db mutation",
            "写入",
            "写 db",
            "db 写",
            "读写",
            "拥有",
        )
    )


def _topology_mentions_active_primary_database_authority(text: str) -> bool:
    lowered = _topology_normalized(text)
    return _topology_mentions_database(lowered) and (
        "active primary" in lowered
        or "current primary" in lowered
        or "primary postgresql" in lowered
        or "primary postgres" in lowered
        or "主库" in lowered
    )


def _topology_context_has_negative_node22_writer_boundary(context: str) -> bool:
    normalized = _topology_normalized(context)
    if _topology_context_has_negative_node22_db_access(normalized):
        return True
    return any(
        token in normalized
        for token in (
            "does not connect",
            "does not use",
            "do not connect",
            "do not copy",
            "do not treat",
            "do not use",
            "not as current",
            "not current",
            "not through",
            "must not be treated",
            "must not read",
            "must not point",
            "shall not instruct",
            "shall not present",
            "not instruct",
            "not present",
            "no active",
            "no implicit",
            "not expose",
            "without relying",
            "without invoking",
            "out of current",
            "outside current",
            "不连",
            "不作为",
            "不要把",
            "不要连",
            "不要复制",
            "不应连接",
            "不使用",
            "禁止",
        )
    )


def _topology_context_has_negative_node22_db_access(context: str) -> bool:
    node22_db = r"node[-_ ]?22\s+(?:active\s+primary\s+)?(?:db|database|postgres|postgresql)"
    access_verb = (
        r"(?:(?:querying|reading|using|accessing)\s+(?:an?\s+)?(?:active\s+)?"
        r"|connecting\s+to\s+(?:an?\s+)?(?:active\s+)?"
        r"|relying\s+on\s+(?:an?\s+)?(?:active\s+)?"
        r")?"
    )
    if re.search(
        rf"\b(?:without|no)\s+{access_verb}(?:an?\s+)?(?:active\s+)?{node22_db}"
        r"(?:\s+(?:access|query|queries|connection|read|reads|writer))?\b",
        context,
    ):
        return True
    domain_modifier = (
        r"(?:an?\s+|the\s+|any\s+)?"
        r"(?:(?:production|active|primary|current|local|env(?:ironment)?|unit)[\s/]+)*"
    )
    database_noun = r"(?:db|database|postgres|postgresql)"
    mutation_noun = r"(?:mutation|mutations|write|writes)"
    node22 = r"node[-_ ]?22"
    access_noun = r"(?:access|query|queries|connection|read|reads)"
    return bool(
        re.fullmatch(
            rf"[\s\"']*(?:without|no)\s+{domain_modifier}{database_noun}\s+{mutation_noun}"
            rf"\s+(?:or|nor)\s+{node22}\s+{access_noun}[\s.。!！?？\"']*",
            context,
        )
    )


def _topology_line_has_node22_local_postgres_or_mirror_drift(line: str) -> bool:
    lowered = _topology_normalized(line)
    if ":55433" in lowered or " 55433" in lowered:
        return True
    if _topology_mentions_node22(lowered) and any(
        token in lowered
        for token in (
            "local postgres",
            "local postgresql",
            "local pg",
            "本地 pg",
            "本机 pg",
            "本地 postgresql",
            "本机 postgresql",
        )
    ):
        return True
    if (
        "n22_dsn" in lowered
        or "node22-url" in lowered
        or "node22-dsn-file" in lowered
        or "node22_dsn_file" in lowered
    ) and _topology_line_mentions_mirror(lowered):
        return True
    # #1707: node-22 + "mirror" alone is not drift - the state-index file mirror is a
    # legitimate non-database mirror. Require rollback wording or a database token that
    # survives removal of explicit "no database here" claims on the same line.
    if not (_topology_mentions_node22(lowered) and _topology_line_mentions_mirror(lowered)):
        return False
    # The rollback leg matches a lexeme, so it also fired on object-store copyback wording
    # ("temp-tree + rollback copy pattern"), which names a file copy and no database at
    # all - the same class of legitimate non-database mirror #1707 carved out for the
    # state-index file mirror. Skip the rollback leg on an object-store copy line only; the
    # database legs below still run, so object-store wording cannot launder a real database
    # token (node-22 + mirror + rollback + db token stays a finding).
    if _topology_line_mentions_rollback(lowered) and not _topology_line_is_object_store_copy(lowered):
        return True
    stripped = _topology_strip_db_absence_claims(lowered)
    if _topology_mentions_database(stripped):
        return True
    # #1707 D9 (as amended by D10): fallback-only database vocabulary.
    # _topology_mentions_database has three other callers and is pinned must-preserve, so
    # the widening lives here. 库 must stay compound - 仓库/代码库/图库 are ordinary words in
    # this repository. The list is bounded by decision (D10): every token here was measured
    # free repo-wide, and further synonyms are a separate proposition.
    return any(
        token in stripped
        for token in (
            "本地库",
            "本机库",
            "standby",
            "instance",
            "replica",
            "secondary",
            "从库",
            "备库",
            "生产库",
        )
    )


def _topology_line_mentions_mirror(text: str) -> bool:
    lowered = _topology_normalized(text)
    return "mirror" in lowered or "镜像" in lowered


def _topology_line_is_object_store_copy(text: str) -> bool:
    # Bounded like #1707 D10: only the three surface forms of the one term the object-store
    # copyback contract actually uses. `object_store` covers OBJECT_STORE_ROOT and
    # NHMS_OBJECT_STORE_COPYBACK_ROOT after normalisation. Deliberately not `copyback`,
    # `keyspace` or `temp-tree` - those are separate propositions, and this predicate only
    # suppresses the rollback leg, never the database legs.
    lowered = _topology_normalized(text)
    return any(token in lowered for token in ("object_store", "object store", "object-store", "对象存储"))


def _topology_line_mentions_rollback(text: str) -> bool:
    # #1707 D3/D10: the fixture commits to rollback wording as a concept, so recognise the
    # whole lexeme - rollback / roll back / roll-back / rolled back / rolling back / rolls
    # back - plus both Chinese surface forms 回滚 and 回退. The left \b keeps it a lexeme
    # rather than a substring: scrollback (a CI/terminal log buffer) is not a rollback.
    # There is deliberately no right boundary, so rollbacks still matches.
    lowered = _topology_normalized(text)
    return bool(re.search(r"\broll(?:ed|ing|s)?[\s-]?back|回滚|回退", lowered))


def _topology_strip_db_absence_claims(text: str) -> str:
    # #1707: strip, do not exclude - a line may deny a database in one clause and name a
    # real one in another, and only the denied token should stop counting as a signal.
    lowered = _topology_normalized(text)
    db = r"(?:postgresql|postgres|database|数据库|db)"
    stripped = re.sub(
        rf"no\s+{db}\s+handle|{db}[\s-]?free\b|(?:无|不取任何)\s?{db}",
        " ",
        lowered,
    )
    return _topology_normalized(stripped)
