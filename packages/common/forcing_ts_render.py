"""Per-store rendering of the forcing fact-table read templates (#1990, epic #1979).

This is the forcing counterpart of the expand/contract transition the river read
path is still mid-way through (its expand landed; ``tasks.md`` 6.3 is the
contract, and is open), and it is its own migration: 7.3's
expand migration renames ``met.forcing_station_timeseries`` to
``met.forcing_station_timeseries_legacy`` and creates a narrow, key-only table
under the canonical name. During the transition BOTH tables are live, one
forcing version's rows in exactly one of them, and every read therefore has to be
renderable twice.

Why this is NOT ``packages/common/river_ts_render.py``
------------------------------------------------------

River renders ONE template per reader and transforms it: ``legacy`` renames the
table, ``narrow`` deletes every line carrying river's verbatim transitional-aid
marker (``river_ts_render.PUSHDOWN_AID_MARKER``) together with the single aid
conjunct beneath it. That machinery exists because the
legacy RIVER table already carries the surrogate key columns, so one text can
serve both stores with a line deletion in between.

The legacy FORCING table has no key columns at all — its primary key is
``(forcing_version_id, station_id, variable, valid_time)``, four text columns
(``db/migrations/000005_met.sql``). There is nothing for a marker to mark and no
line whose deletion turns the legacy statement into the narrow one: the narrow
variant joins ``met.forcing_version`` for ``source_id`` and ``met.met_station``
for ``basin_version_id``, which the legacy variant reads off the fact row. So a
forcing reader registers TWO INDEPENDENT TEMPLATES — a
:class:`ForcingTemplatePair` — and this module SELECTS one. It performs no line
deletion, defines no marker, and introduces no ``#1342`` tag anywhere (the marker
set is river-only; see ``tasks.md`` 6.3).

Transitional table-name constants (fixture ``I11-1990.md`` D1)
---------------------------------------------------------------

The table name is NOT literal text inside a template. Templates carry
:data:`FORCING_TABLE_TOKEN` and this module substitutes the constant for the
requested store. Two consequences, and both are the point:

* 7.3's flip of the legacy name was ONE line in this file, landing in the same
  commit as the migration that makes it true — so master is never carrying a read
  path that names a relation which does not exist. River's equivalent wiring
  (``e8b30893c``) sat three days ahead of its migration (``8a61f5347``)
  fail-closed on HTTP 500, and node-27 is the active primary behind
  ``test.nwm.ac.cn``. That is the mistake D1 declines to repeat.
* the discovery-set census (``tests/test_forcing_ts_template_census.py``) sees
  exactly two schema-qualified mentions in this module and zero in each wired
  reader, so a reader that goes back to spelling the name itself is red.

A template that spells the qualified name literally defeats both, so rendering
REFUSES it rather than returning SQL — and so is one that names the table
unqualified, which the census cannot count at all (see
:data:`_BARE_FORCING_TABLE`).

Ownership note
--------------

No SQL-text machinery is shared with the river renderer. ``river_ts_render``'s
structural, alias-attribution and predicate-retention checks are all bound to
``hydro.river_timeseries`` and exist to police a line deletion; there is no line
deletion here, and ``tasks.md`` 6.3 deletes river's legacy path outright. Coupling
this module to it would make the forcing read path a casualty of the river
contract migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# D1 — transitional table-name constants
# ---------------------------------------------------------------------------
#
# FLIPPED BY TASK 7.3 (I12, #1991), in the same commit as
# `db/migrations/000061_forcing_station_timeseries_narrow_expand.sql` — the
# migration that makes the second name true by renaming the table. That
# same-commit rule is D1's whole point: master is never carrying a read path
# that names a relation which does not exist. River's equivalent wiring
# (`e8b30893c`) sat three days ahead of its migration (`8a61f5347`) fail-closed
# on HTTP 500, and node-27 is the active primary behind `test.nwm.ac.cn`.
#
# The two names are now DIFFERENT, which is what makes the per-store render a
# real choice: `legacy` reads the renamed table that 000061 left holding every
# forcing version written before the expand, `narrow` reads the key/enum table
# 000061 created under the canonical name. Both are live until task 8.2 drops
# the legacy one; 8.3 then deletes the legacy templates and this constant with
# them.
#
# Do not collapse the two constants into one before 8.3.
FORCING_TABLE = "met.forcing_station_timeseries"
FORCING_TABLE_LEGACY = "met.forcing_station_timeseries_legacy"

#: The two timeseries stores, spelled locally rather than imported from
#: ``river_ts_render``: ``tasks.md`` 6.3 removes the river renderer's legacy path
#: while the forcing transition is still open, so the forcing read path must not
#: depend on river's vocabulary surviving.
FORCING_STORES: tuple[str, ...] = ("legacy", "narrow")

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
#: Note it deliberately has no trailing ``\b``: it matches
#: ``met.forcing_station_timeseries_legacy`` as well, which is what keeps the
#: guard biting after 7.3. The cost is an asymmetry with
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
#: guard both, and after 7.3 resolve through ``search_path`` to the narrow table,
#: whose rows carry none of the legacy variant's text columns.
#:
#: Spelled exactly like river's ``_FACT_NAME_TOKEN``, trailing ``\b`` included
#: and for river's reason: ``forcing_station_timeseries_valid_time_idx`` is an
#: index name, not a read, and refusing it would make a template carrying a
#: planner note unregisterable. ``_legacy`` is matched explicitly, which is what
#: keeps this guard biting after 7.3.
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
_BARE_FORCING_TABLE = re.compile(r"\bforcing_station_timeseries(?:_legacy)?\b", re.IGNORECASE)


class ForcingTemplateError(ValueError):
    """A template the renderer refuses, named by its registry entry.

    Never a bare ``AssertionError``: these checks run in production read paths
    (``tasks.md`` 7.2 wires nine readers to this module), where ``python -O``
    would strip an assert and ship the very statement the check exists to stop.
    """


@dataclass(frozen=True)
class ForcingTemplatePair:
    """The two independently authored variants of ONE forcing reader's SQL.

    A pair, not a template plus a transform (invariant I6, ``design.md:126``):
    the legacy variant predicates on the fact row's text identity columns, the
    narrow variant predicates on ``forcing_version_key`` / ``station_key`` /
    ``variable_e`` and takes ``source_id`` from a joined ``met.forcing_version``
    and ``basin_version_id`` from a joined ``met.met_station``. Neither is
    derivable from the other by deleting lines.

    Both variants write :data:`FORCING_TABLE_TOKEN` where the fact table's name
    goes; neither may spell the qualified name itself (D1).
    """

    legacy: str
    narrow: str


@dataclass(frozen=True)
class RenderedForcingSql:
    """One rendered variant of a forcing reader's SQL, with the store it is for.

    ``store`` is carried on the result rather than left to the caller to
    remember, because the property that keeps node-27 safe until 7.3 is "no
    narrow render ever enters an executed statement" (must-preserve M6). An
    executor or an oracle can assert that against this field; against the SQL
    text it would have to guess.

    No ``removed_placeholders`` / ``removed_aids`` counterpart to river's
    :class:`~packages.common.river_ts_render.RenderedSql`: nothing is removed
    here, so the two variants' parameter shapes are whatever their authors wrote
    and are pinned by the registry's ``params``, not computed from a deletion.
    """

    sql: str
    store: str


def _physical_table(store: str) -> str:
    """The relation name the deployed schema actually has for ``store``.

    Reads the module constants at CALL time, so 7.3's one-line flip of
    :data:`FORCING_TABLE_LEGACY` takes effect everywhere without any caller
    needing to be re-imported, and so a test can simulate that flip.
    """
    return FORCING_TABLE_LEGACY if store == "legacy" else FORCING_TABLE


def render_forcing_ts_sql(
    template_pair: ForcingTemplatePair,
    store: str,
    *,
    entry: str = "<template>",
) -> RenderedForcingSql:
    """Select ``template_pair``'s variant for ``store`` and bind the table name.

    ``legacy`` renders the text-column variant against
    :data:`FORCING_TABLE_LEGACY`; ``narrow`` renders the key/enum variant against
    :data:`FORCING_TABLE`. Those two constants differed for the first time at
    task 7.3 (#1991), whose migration renamed the deployed table — so a render is
    now a real choice of relation, not only of column vocabulary. Before that
    flip they were the same string, which is what made the reader wiring in
    ``tasks.md`` 7.2 a provable zero-behaviour-change refactor.

    Raises :class:`ForcingTemplateError`, naming ``entry``, rather than returning
    SQL when:

    * ``store`` is not one of :data:`FORCING_STORES` — a silent fallback to
      ``legacy`` on a typo would route a narrow-stored version's read at the
      legacy table and return zero rows, which reads as "no data" rather than as
      an error;
    * the selected variant carries no :data:`FORCING_TABLE_TOKEN` — a registered
      fact-table template that never names the fact table is misregistered, and
      rendering it would hand back a statement this module had no effect on;
    * the selected variant spells the qualified table name literally — that is
      the D1 violation itself, and it would survive 7.3's flip pointing at
      whichever table the author happened to type;
    * the selected variant names the table WITHOUT a schema — the census counts
      qualified mentions only, so this is the one spelling that would otherwise
      clear both that guard and the one above, and after 7.3 it resolves through
      ``search_path`` to the narrow table (see :data:`_BARE_FORCING_TABLE`).

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
    and there is no escape hatch. No registered reader has that shape today, but
    it is precisely the shape of the write-side row-count payloads this epic
    exempts (``db/seeds/seed_demo.py:1021`` and the handoff protocol keys), which
    become templates in task 7.3. A statement that must emit the name as data has
    to bind it as a PARAMETER rather than spell it in the template; which constant
    it should bind after the flip is 7.3's question, not this module's. An author
    should learn the refusal exists here rather than from the refusal itself.
    """
    if store not in FORCING_STORES:
        raise ForcingTemplateError(
            f"{entry}: unknown timeseries store {store!r} (expected one of {list(FORCING_STORES)})"
        )
    template = template_pair.legacy if store == "legacy" else template_pair.narrow
    if FORCING_TABLE_TOKEN not in template:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant carries no {FORCING_TABLE_TOKEN} token, so it names no fact table; "
            "a registered forcing read template must take the table name from the renderer's constants"
        )
    literal = _QUALIFIED_FORCING_TABLE.search(template)
    if literal is not None:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant spells the fact table literally ({literal.group(0)!r}); "
            f"write {FORCING_TABLE_TOKEN} instead, so task 7.3's rename stays a one-line change here"
        )
    bare = _BARE_FORCING_TABLE.search(template)
    if bare is not None:
        raise ForcingTemplateError(
            f"{entry}: the {store!r} variant names the fact table without a schema ({bare.group(0)!r}); "
            "the census counts qualified mentions only, so this spelling escapes it, and after task 7.3 "
            f"search_path resolves it to the narrow table — write {FORCING_TABLE_TOKEN} instead"
        )
    return RenderedForcingSql(template.replace(FORCING_TABLE_TOKEN, _physical_table(store)), store)
