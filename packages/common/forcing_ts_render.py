"""Rendering of the forcing fact-table read templates (#1990, epic #1979).

Every reader of ``met.forcing_station_timeseries`` registers its SQL as a
:class:`ForcingTemplatePair` and obtains the executed text from
:func:`render_forcing_ts_sql`. The table is the narrow key/enum table that
``db/migrations/000061_forcing_station_timeseries_narrow_expand.sql`` created
under the canonical name, and it is the only one this module renders against.

History, for whoever meets the ``store`` argument: during the expand window the
wide text-column table that 000061 renamed aside was still read, one forcing
version's rows lived in exactly one of the two tables, and every reader
registered TWO independently authored templates. #1993's first cut removed that
second template and every read of the routing column; the renderer accepts the
single store ``narrow`` and refuses anything else. The pair class and the
``store`` parameter keep their shape so the nine construction sites and the
test-side register did not have to move.

Why this is NOT ``packages/common/river_ts_render.py``
------------------------------------------------------

No SQL-text machinery is shared with the river renderer. ``river_ts_render``'s
structural, alias-attribution and predicate-retention checks are all bound to
``hydro.river_timeseries``; this module only selects a registered text and binds
the table name into it.

The table name is not literal text inside a template
----------------------------------------------------

Templates carry :data:`FORCING_TABLE_TOKEN` and this module substitutes
:data:`FORCING_TABLE` (fixture ``I11-1990.md`` D1). Two consequences, and both
are the point:

* a rename of the relation is ONE line in this file, landing in the same commit
  as the migration that makes it true — so master is never carrying a read path
  that names a relation which does not exist;
* the discovery-set census (``tests/test_forcing_ts_template_census.py``) sees
  exactly one schema-qualified mention in this module and zero in each wired
  reader, so a reader that goes back to spelling the name itself is red.

A template that spells the qualified name literally defeats both, so rendering
REFUSES it rather than returning SQL — and so is one that names the table
unqualified, which the census cannot count at all (see
:data:`_BARE_FORCING_TABLE`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The one relation every registered template is rendered against (D1). A rename
# is a one-line change here, in the same commit as the migration that makes it
# true.
FORCING_TABLE = "met.forcing_station_timeseries"

#: The stores the renderer accepts. One value since #1993's first cut removed the
#: legacy templates; kept as a tuple so the refusal below names what is expected.
FORCING_STORES: tuple[str, ...] = ("narrow",)

#: What a registered template writes where the fact table's name goes. Braces
#: rather than ``str.format`` fields on purpose — substitution is a plain
#: :meth:`str.replace`, so a template may carry ``'{"a": 1}'`` jsonb literals,
#: ``%s`` / ``%(name)s`` / ``:name`` placeholders and ``%`` operators without any
#: of them needing to be escaped.
FORCING_TABLE_TOKEN = "{{forcing_table}}"

#: A SCHEMA-QUALIFIED mention of the fact table, however it is quoted, spaced or
#: cased — the same class the census counts over
#: (``tests/forcing_ts_template_registry.forcing_table_mentions``), spelled once
#: per side. Matched as a class and not as one spelling because
#: ``'met.forcing_station_timeseries' in template`` sees neither
#: ``"met"."forcing_station_timeseries"`` nor ``MET . forcing_station_timeseries``,
#: and either of those would slip a hardcoded name past the D1 guard below.
#:
#: Note it deliberately has no trailing ``\b``: any qualified name that merely
#: STARTS with the fact table's is refused too. The cost is an asymmetry with
#: :data:`_BARE_FORCING_TABLE`, which DOES end in ``\b``: a QUALIFIED index name
#: (``met.forcing_station_timeseries_pkey``) is refused here even inside a
#: planner comment, while the same index named unqualified is tolerated there.
_QUALIFIED_FORCING_TABLE = re.compile(r'(?:"met"|\bmet)\s*\.\s*"?forcing_station_timeseries', re.IGNORECASE)

#: The fact table's BARE name as a whole token, in any case — the spelling the
#: census cannot police. The counter in
#: ``tests/forcing_ts_template_registry.forcing_table_mentions`` deliberately
#: counts only QUALIFIED mentions, because several sites in the tree spell the
#: table unqualified on purpose (a manifest's logical name, a column list) and
#: counting those would move most per-file numbers for no gain. River affords the
#: same blindness because its renderer is "counter permissive, walk strict,
#: disagreement refuses" (``river_ts_render.fact_table_name_occurrences``), which
#: refuses an unqualified read at render time. Nothing on the forcing side plays
#: that role, so THIS is the compensation: a template written
#: ``FROM forcing_station_timeseries`` would otherwise pass the census and the D1
#: guard both, and resolve through ``search_path`` to whatever relation of that
#: name comes first.
#:
#: Spelled exactly like river's ``_FACT_NAME_TOKEN``, trailing ``\b`` included
#: and for river's reason: ``forcing_station_timeseries_valid_time_idx`` is an
#: index name, not a read, and refusing it would make a template carrying a
#: planner note unregisterable.
#:
#: SAME PATTERN AS RIVER'S, WIDER SCOPE — the operand differs. River applies its
#: token to ``_blank_comments_and_literals(sql)``
#: (``river_ts_render.fact_table_name_occurrences``), which blanks comments and
#: string literals first; this searches the RAW template. So forcing is strictly
#: harsher than river: a planner note reading
#: ``-- the forcing_station_timeseries window read`` is tolerated on the river
#: side and refused here. That is deliberate — the forcing side has no
#: counter/walk disagreement to fall back on — but a template author needs to
#: know the boundary is the raw text, not the code.
#:
#: The index-name tolerance above holds for the UNQUALIFIED spelling only.
#: :data:`_QUALIFIED_FORCING_TABLE` has no trailing ``\b``, so
#: ``met.forcing_station_timeseries_pkey`` is refused by THAT guard, with the D1
#: "spells the fact table literally" reason, while bare
#: ``forcing_station_timeseries_pkey`` passes both. Name an index unqualified in
#: a template.
_BARE_FORCING_TABLE = re.compile(r"\bforcing_station_timeseries\b", re.IGNORECASE)


class ForcingTemplateError(ValueError):
    """A template the renderer refuses, named by its registry entry.

    Never a bare ``AssertionError``: these checks run in production read paths
    (``tasks.md`` 7.2 wired nine readers to this module), where ``python -O``
    would strip an assert and ship the very statement the check exists to stop.
    """


@dataclass(frozen=True)
class ForcingTemplatePair:
    """ONE forcing reader's registered SQL.

    A pair in name only since #1993's first cut deleted the legacy variant; the
    class and its single field survive so the construction sites and the
    class-name-based discovery in ``tests/forcing_ts_template_registry.py`` kept
    their shape.

    ``narrow`` predicates on ``forcing_version_key`` / ``station_key`` /
    ``variable_e`` and takes ``source_id`` from a joined ``met.forcing_version``
    and ``basin_version_id`` from a joined ``met.met_station``. It writes
    :data:`FORCING_TABLE_TOKEN` where the fact table's name goes and may not
    spell the qualified name itself (D1).
    """

    narrow: str


@dataclass(frozen=True)
class RenderedForcingSql:
    """A forcing reader's rendered SQL, with the store it is for."""

    sql: str
    store: str


def render_forcing_ts_sql(
    template_pair: ForcingTemplatePair,
    store: str,
    *,
    entry: str = "<template>",
) -> RenderedForcingSql:
    """Bind the table name into ``template_pair``'s ``narrow`` template.

    Raises :class:`ForcingTemplateError`, naming ``entry``, rather than returning
    SQL when:

    * ``store`` is not ``narrow`` (:data:`FORCING_STORES`) — the legacy store has
      no template any more, and a silent fallback on a stale ``"legacy"`` or a
      typo would hand back the narrow statement for a question that was not
      asked;
    * the template carries no :data:`FORCING_TABLE_TOKEN` — a registered
      fact-table template that never names the fact table is misregistered, and
      rendering it would hand back a statement this module had no effect on;
    * the template spells the qualified table name literally — that is the D1
      violation itself, and it would survive a rename pointing at whichever
      table the author happened to type;
    * the template names the table WITHOUT a schema — the census counts
      qualified mentions only, so this is the one spelling that would otherwise
      clear both that guard and the one above (see :data:`_BARE_FORCING_TABLE`).

    The token is substituted textually and unconditionally, including inside
    comments and string literals. That is a recorded limit rather than an
    oversight: river needs a code/data distinction because it REWRITES a name an
    author wrote, and a name inside a literal is data; here the token exists for
    no other purpose than to be replaced, so a template that puts one inside a
    literal is asking for exactly what it gets.

    The same raw-text reading makes the two refusals above OVER-REJECT the table
    name used as SQL DATA: ``SELECT 'forcing_station_timeseries' AS table_name``
    trips the unqualified guard and
    ``jsonb_build_object('table', 'met.forcing_station_timeseries')`` trips the
    D1 one, both inside a literal that no planner ever resolves to a relation,
    and there is no escape hatch. No registered reader has that shape. A
    statement that must emit the name as data has to bind it as a PARAMETER
    rather than spell it in the template. An author should learn the refusal
    exists here rather than from the refusal itself.
    """
    if store not in FORCING_STORES:
        raise ForcingTemplateError(
            f"{entry}: unknown timeseries store {store!r} (expected one of {list(FORCING_STORES)})"
        )
    template = template_pair.narrow
    if FORCING_TABLE_TOKEN not in template:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant carries no {FORCING_TABLE_TOKEN} token, so it names no fact table; "
            "a registered forcing read template must take the table name from the renderer's constants"
        )
    literal = _QUALIFIED_FORCING_TABLE.search(template)
    if literal is not None:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant spells the fact table literally ({literal.group(0)!r}); "
            f"write {FORCING_TABLE_TOKEN} instead, so a rename stays a one-line change here"
        )
    bare = _BARE_FORCING_TABLE.search(template)
    if bare is not None:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant names the fact table without a schema ({bare.group(0)!r}); "
            "the census counts qualified mentions only, so this spelling escapes it and resolves through "
            f"search_path — write {FORCING_TABLE_TOKEN} instead"
        )
    return RenderedForcingSql(template.replace(FORCING_TABLE_TOKEN, FORCING_TABLE), store)
