"""American-football concept lexicon for zero-shot concept search (Issue #144).

Coaches want to ask *"find me all the Mesh concepts"* or *"show every Cover 3
trips look"* without first labelling a thousand plays. This module is the
**zero-shot-first** half of that feature (Issue #144, updated to zero-shot-first
in the backlog): it maps a free-text query onto the *structured football
metadata Football-IQ already trusts* — the ``formation`` / ``coverage`` values in
``clips.label_data`` and the ``personnel_grouping`` / ``field_zone`` columns — so
concept search works **without any Toledo-specific fine-tuning** and without a
new model in the backend container.

It is American football only. There is a small soccer / association-football
denylist (``SOCCER_DENYLIST``) so a "football" query about *xG / 4-4-2 / offside*
is rejected with a clear reason instead of silently returning nothing, per
``docs/external-resource-rubric.md``.

The router (`app.routers.concept_search`) turns the matched concepts into
SQLAlchemy predicates over ``clips`` and — optionally and clearly flagged
*experimental* — expands the grounded matches with the existing pgvector play
embeddings (Issue #8). No second vector store is introduced; see
``docs/concept-search.md``.

This module is pure logic: no database, no I/O, no model weights. That keeps it
trivially unit-testable and dependency-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ConceptCategory(StrEnum):
    """Which structured surface a concept is grounded against."""

    FORMATION = "formation"
    COVERAGE = "coverage"
    PERSONNEL = "personnel"
    MOTION = "motion"
    CONCEPT = "concept"  # pass/run play concepts (mesh, smash, power, …)
    FIELD_ZONE = "field_zone"


@dataclass(frozen=True)
class ConceptDefinition:
    """One recognizable American-football concept.

    ``aliases`` are the phrases a coach might type. ``match_terms`` are the
    substrings looked for inside the structured labels (default: the aliases).
    ``json_path`` names a two-level path inside ``clips.label_data`` (e.g.
    ``("coverage", "generic")``) for precise matching; ``column`` names a
    first-class ``clips`` column (``personnel_grouping`` / ``field_zone``).
    The router always also applies a serialized-``label_data`` text fallback so a
    concept stored under a different key still matches.
    """

    concept_id: str
    display_name: str
    category: ConceptCategory
    aliases: tuple[str, ...]
    match_terms: tuple[str, ...] = ()
    json_path: tuple[str, str] | None = None
    column: str | None = None

    def terms(self) -> tuple[str, ...]:
        return self.match_terms or self.aliases


# ── The lexicon (American football only) ──────────────────────────────────────
#
# Kept intentionally readable and easy to extend. Coverage / formation entries
# ground against ``label_data`` JSON paths that the existing search router
# (`app.routers.search`) already filters on; concept / motion entries ground
# against the serialized ``label_data`` fallback because the label taxonomy for
# play concepts is not pinned to a single column.

CONCEPTS: tuple[ConceptDefinition, ...] = (
    # ── Formations ────────────────────────────────────────────────────────
    ConceptDefinition(
        "trips",
        "Trips (3x1)",
        ConceptCategory.FORMATION,
        aliases=("trips", "3x1", "trey", "trio"),
        match_terms=("trips", "3x1"),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "doubles",
        "Doubles (2x2)",
        ConceptCategory.FORMATION,
        aliases=("doubles", "2x2", "pro spread"),
        match_terms=("doubles", "2x2"),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "bunch",
        "Bunch",
        ConceptCategory.FORMATION,
        aliases=("bunch", "cluster"),
        match_terms=("bunch",),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "empty",
        "Empty",
        ConceptCategory.FORMATION,
        aliases=("empty", "five wide", "5 wide"),
        match_terms=("empty",),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "shotgun",
        "Shotgun",
        ConceptCategory.FORMATION,
        aliases=("shotgun", "gun"),
        match_terms=("shotgun",),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "pistol",
        "Pistol",
        ConceptCategory.FORMATION,
        aliases=("pistol",),
        match_terms=("pistol",),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "singleback",
        "Singleback / Ace",
        ConceptCategory.FORMATION,
        aliases=("singleback", "single back", "ace"),
        match_terms=("singleback", "ace"),
        json_path=("formation", "generic"),
    ),
    ConceptDefinition(
        "i_formation",
        "I-Formation",
        ConceptCategory.FORMATION,
        aliases=("i formation", "i-formation", "i-form", "iform"),
        match_terms=("i_formation", "i-formation", "iform"),
        json_path=("formation", "generic"),
    ),
    # ── Coverages ─────────────────────────────────────────────────────────
    ConceptDefinition(
        "cover_0",
        "Cover 0",
        ConceptCategory.COVERAGE,
        aliases=("cover 0", "cover zero", "cover-0", "c0"),
        match_terms=("cover_0", "cover 0", "cover0"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "cover_1",
        "Cover 1",
        ConceptCategory.COVERAGE,
        aliases=("cover 1", "cover one", "cover-1", "man free"),
        match_terms=("cover_1", "cover 1", "cover1"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "cover_2",
        "Cover 2",
        ConceptCategory.COVERAGE,
        aliases=("cover 2", "cover two", "cover-2", "tampa 2", "tampa two"),
        match_terms=("cover_2", "cover 2", "cover2", "tampa"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "cover_3",
        "Cover 3",
        ConceptCategory.COVERAGE,
        aliases=("cover 3", "cover three", "cover-3", "c3"),
        match_terms=("cover_3", "cover 3", "cover3"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "cover_4",
        "Cover 4 / Quarters",
        ConceptCategory.COVERAGE,
        aliases=("cover 4", "cover four", "cover-4", "quarters"),
        match_terms=("cover_4", "cover 4", "cover4", "quarters"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "cover_6",
        "Cover 6",
        ConceptCategory.COVERAGE,
        aliases=("cover 6", "cover six", "cover-6", "quarter quarter half"),
        match_terms=("cover_6", "cover 6", "cover6"),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "man_coverage",
        "Man coverage",
        ConceptCategory.COVERAGE,
        aliases=("man coverage", "man to man", "press man"),
        match_terms=("man",),
        json_path=("coverage", "generic"),
    ),
    ConceptDefinition(
        "zone_coverage",
        "Zone coverage",
        ConceptCategory.COVERAGE,
        aliases=("zone coverage", "zone defense"),
        match_terms=("zone",),
        json_path=("coverage", "generic"),
    ),
    # ── Personnel groupings (explicit phrases only — never a bare number) ──
    ConceptDefinition(
        "personnel_10",
        "10 personnel",
        ConceptCategory.PERSONNEL,
        aliases=("10 personnel", "10 pers", "10p"),
        match_terms=("10",),
        column="personnel_grouping",
    ),
    ConceptDefinition(
        "personnel_11",
        "11 personnel",
        ConceptCategory.PERSONNEL,
        aliases=("11 personnel", "11 pers", "11p"),
        match_terms=("11",),
        column="personnel_grouping",
    ),
    ConceptDefinition(
        "personnel_12",
        "12 personnel",
        ConceptCategory.PERSONNEL,
        aliases=("12 personnel", "12 pers", "12p"),
        match_terms=("12",),
        column="personnel_grouping",
    ),
    ConceptDefinition(
        "personnel_21",
        "21 personnel",
        ConceptCategory.PERSONNEL,
        aliases=("21 personnel", "21 pers", "21p"),
        match_terms=("21",),
        column="personnel_grouping",
    ),
    ConceptDefinition(
        "personnel_22",
        "22 personnel",
        ConceptCategory.PERSONNEL,
        aliases=("22 personnel", "22 pers", "22p"),
        match_terms=("22",),
        column="personnel_grouping",
    ),
    # ── Motion ────────────────────────────────────────────────────────────
    ConceptDefinition(
        "jet_motion",
        "Jet motion / sweep",
        ConceptCategory.MOTION,
        aliases=("jet motion", "jet sweep", "jet"),
        match_terms=("jet",),
    ),
    ConceptDefinition(
        "orbit_motion",
        "Orbit motion",
        ConceptCategory.MOTION,
        aliases=("orbit motion", "orbit"),
        match_terms=("orbit",),
    ),
    ConceptDefinition(
        "shift",
        "Shift",
        ConceptCategory.MOTION,
        aliases=("shift", "reset"),
        match_terms=("shift",),
    ),
    # ── Pass / run play concepts ──────────────────────────────────────────
    ConceptDefinition(
        "mesh",
        "Mesh",
        ConceptCategory.CONCEPT,
        aliases=("mesh", "mesh concept", "drive mesh"),
        match_terms=("mesh",),
    ),
    ConceptDefinition(
        "smash",
        "Smash",
        ConceptCategory.CONCEPT,
        aliases=("smash", "smash concept"),
        match_terms=("smash",),
    ),
    ConceptDefinition(
        "four_verts",
        "Four verticals",
        ConceptCategory.CONCEPT,
        aliases=("four verts", "4 verts", "four verticals", "verticals"),
        match_terms=("four_verts", "four verticals", "verts"),
    ),
    ConceptDefinition(
        "flood",
        "Flood / Sail",
        ConceptCategory.CONCEPT,
        aliases=("flood", "sail concept", "flood concept"),
        match_terms=("flood", "sail"),
    ),
    ConceptDefinition(
        "stick",
        "Stick",
        ConceptCategory.CONCEPT,
        aliases=("stick concept", "stick"),
        match_terms=("stick",),
    ),
    ConceptDefinition(
        "play_action",
        "Play action",
        ConceptCategory.CONCEPT,
        aliases=("play action", "play-action", "play action pass", "pa boot", "bootleg", "boot"),
        match_terms=("play_action", "play action", "bootleg", "boot"),
    ),
    ConceptDefinition(
        "rpo",
        "RPO (run-pass option)",
        ConceptCategory.CONCEPT,
        aliases=("rpo", "run pass option", "run-pass option"),
        match_terms=("rpo", "run_pass_option"),
    ),
    ConceptDefinition(
        "screen",
        "Screen",
        ConceptCategory.CONCEPT,
        aliases=("screen", "bubble screen", "tunnel screen", "screen pass"),
        match_terms=("screen",),
    ),
    ConceptDefinition(
        "draw",
        "Draw",
        ConceptCategory.CONCEPT,
        aliases=("draw play", "qb draw", "draw"),
        match_terms=("draw",),
    ),
    ConceptDefinition(
        "power",
        "Power",
        ConceptCategory.CONCEPT,
        aliases=("power", "power run", "gap power", "power right", "power left"),
        match_terms=("power",),
    ),
    ConceptDefinition(
        "counter",
        "Counter",
        ConceptCategory.CONCEPT,
        aliases=("counter", "counter trey", "gt counter"),
        match_terms=("counter",),
    ),
    ConceptDefinition(
        "outside_zone",
        "Outside zone",
        ConceptCategory.CONCEPT,
        aliases=("outside zone", "wide zone", "zone stretch"),
        match_terms=("outside_zone", "outside zone", "wide zone"),
    ),
    ConceptDefinition(
        "inside_zone",
        "Inside zone",
        ConceptCategory.CONCEPT,
        aliases=("inside zone", "tight zone"),
        match_terms=("inside_zone", "inside zone"),
    ),
    ConceptDefinition(
        "sweep",
        "Sweep / Toss",
        ConceptCategory.CONCEPT,
        aliases=("sweep", "toss sweep", "toss"),
        match_terms=("sweep", "toss"),
    ),
    ConceptDefinition(
        "scramble",
        "QB scramble",
        ConceptCategory.CONCEPT,
        aliases=("scramble", "qb scramble", "scrambled"),
        match_terms=("scramble",),
    ),
    # ── Field zone ────────────────────────────────────────────────────────
    ConceptDefinition(
        "red_zone",
        "Red zone",
        ConceptCategory.FIELD_ZONE,
        aliases=("red zone", "redzone"),
        match_terms=("red_zone", "red zone"),
        column="field_zone",
    ),
    ConceptDefinition(
        "goal_line",
        "Goal line",
        ConceptCategory.FIELD_ZONE,
        aliases=("goal line", "goalline", "goal-line"),
        match_terms=("goal_line", "goal line"),
        column="field_zone",
    ),
)


# Soccer / association-football terms. A "football" query that is really about
# soccer is rejected with a clear reason rather than silently empty. Mirrors the
# rule-of-thumb in docs/external-resource-rubric.md §2.
SOCCER_DENYLIST: tuple[str, ...] = (
    "xg",
    "expected goals",
    "4-4-2",
    "4-3-3",
    "3-5-2",
    "offside",
    "pitch",
    "penalty kick",
    "corner kick",
    "clean sheet",
    "midfielder",
    "striker",
    "goalkeeper",
    "nil nil",
    "nil-nil",
    "la liga",
    "premier league",
    "fixture",
    "match day",
)


@dataclass(frozen=True)
class ConceptMatch:
    """A lexicon concept the query resolved to, with a parse confidence."""

    concept: ConceptDefinition
    confidence: float
    matched_alias: str


_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase and collapse non-alphanumeric runs to single spaces."""
    return " ".join(_WORD.findall(text.lower()))


def _loose_contains(haystack: str, needle: str) -> bool:
    """Token-boundary containment of a normalized phrase in a normalized query.

    e.g. normalized "show me cover 3 trips" contains "cover 3".
    """
    if not needle:
        return False
    return bool(re.search(rf"(?:^| ){re.escape(needle)}(?: |$)", haystack))


def detect_soccer_terms(query: str) -> list[str]:
    """Return any soccer denylist terms present in the query (normalized)."""
    norm = normalize(query)
    hits: list[str] = []
    for term in SOCCER_DENYLIST:
        if _loose_contains(norm, normalize(term)):
            hits.append(term)
    return hits


def parse_query(query: str) -> list[ConceptMatch]:
    """Resolve a free-text query to American-football concepts.

    Returns at most one ``ConceptMatch`` per concept, preferring the
    longest-alias (most specific) match. Confidence is a coarse parse signal:
    ``0.9`` for a token-boundary phrase hit, scaled down slightly for shorter
    aliases. The mapping is intentionally approximate — every result carries an
    *approximate* flag downstream.
    """
    norm = normalize(query)
    if not norm:
        return []

    matches: dict[str, ConceptMatch] = {}
    for concept in CONCEPTS:
        # Longest aliases first so "cover 3" wins over a stray "cover".
        for alias in sorted(concept.aliases, key=len, reverse=True):
            if _loose_contains(norm, normalize(alias)):
                # Longer, multi-word aliases are higher-signal.
                tokens = len(normalize(alias).split())
                confidence = 0.95 if tokens >= 2 else 0.85
                matches[concept.concept_id] = ConceptMatch(
                    concept=concept, confidence=confidence, matched_alias=alias
                )
                break
    # Stable order: by category then concept id, for deterministic responses.
    return sorted(
        matches.values(),
        key=lambda m: (m.concept.category.value, m.concept.concept_id),
    )


def group_by_category(matches: list[ConceptMatch]) -> dict[ConceptCategory, list[ConceptMatch]]:
    """Group matches by category (OR within a category, AND across — see router)."""
    grouped: dict[ConceptCategory, list[ConceptMatch]] = {}
    for m in matches:
        grouped.setdefault(m.concept.category, []).append(m)
    return grouped
