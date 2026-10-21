"""Report aggregation and rendering (Issue #111).

Pulls real counts/aggregates from the database into a stable
:class:`CoachingSummaryData` shape, then renders that shape to JSON, CSV, or
PDF bytes. The data layer is deliberately separated from the renderers so
rendering is a pure function of an aggregate snapshot — easier to test and
easier to extend with new formats.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Clip,
    CoachCorrection,
    Player,
    PlayView,
    ReportFormat,
    Video,
)
from app.services.metric_policy import canonical_play_eligible

# Section labels mirror the existing Reports UI (page-renderer.tsx). Keep
# these in sync with the frontend so the section toggles drive real output.
SECTION_SELF_SCOUT = "Self-scout exposure"
SECTION_DEVELOPMENT = "Position group development"
SECTION_MODEL_QUALITY = "Model quality"
SECTION_OPPONENT_PREP = "Opponent prep package"

ALL_SECTIONS: tuple[str, ...] = (
    SECTION_SELF_SCOUT,
    SECTION_DEVELOPMENT,
    SECTION_MODEL_QUALITY,
    SECTION_OPPONENT_PREP,
)

REPORT_TYPE_COACHING_SUMMARY = "coaching_summary"


@dataclass
class SectionRow:
    label: str
    value: str


@dataclass
class Section:
    title: str
    rows: list[SectionRow] = field(default_factory=list)


@dataclass
class CoachingSummaryData:
    generated_at: str
    sections: list[Section]
    totals: dict[str, int]
    scope: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "sections": [
                {
                    "title": s.title,
                    "rows": [asdict(r) for r in s.rows],
                }
                for s in self.sections
            ],
            "totals": self.totals,
            "scope": self.scope,
            "description": (
                "Film inventory and reviewed tagging summary; "
                "roster counts are not player development grades."
            ),
        }


# ── Aggregation ───────────────────────────────────────────────────────────────


async def _count(db: AsyncSession, stmt: Any) -> int:
    result = await db.execute(stmt)
    n = result.scalar_one_or_none()
    return int(n or 0)


async def aggregate_coaching_summary(
    db: AsyncSession,
    *,
    selected_sections: Iterable[str] | None = None,
    scope: dict[str, Any] | None = None,
) -> CoachingSummaryData:
    """Build a :class:`CoachingSummaryData` snapshot from real DB rows.

    ``selected_sections`` filters which sections appear in the output; ``None``
    means "include all known sections."
    """
    wanted = set(selected_sections) if selected_sections is not None else set(ALL_SECTIONS)

    scope = scope or {}
    videos = select(Video.id)
    for name in ("practice_session_id", "session_kind", "library_role", "opponent_team"):
        if scope.get(name) is not None:
            videos = videos.where(getattr(Video, name) == scope[name])
    if scope.get("video_id"):
        videos = videos.where(Video.id == scope["video_id"])
    for name, operator in (("recorded_after", "after"), ("recorded_before", "before")):
        if scope.get(name):
            value = (
                datetime.fromisoformat(scope[name]) if isinstance(scope[name], str) else scope[name]
            )
            videos = videos.where(
                Video.recorded_at >= value if operator == "after" else Video.recorded_at <= value
            )
    clip_scope = Clip.video_id.in_(videos) & canonical_play_eligible()
    play_key = func.coalesce(PlayView.play_id, Clip.id)
    canonical_count = (
        select(func.count(func.distinct(play_key)))
        .select_from(Clip)
        .outerjoin(PlayView, PlayView.clip_id == Clip.id)
        .where(clip_scope)
    )
    total_videos = await _count(
        db, select(func.count()).select_from(Video).where(Video.id.in_(videos))
    )
    total_clips = await _count(db, canonical_count)
    total_players = await _count(
        db, select(func.count()).select_from(Player).where(Player.is_active.is_(True))
    )
    total_corrections = await _count(
        db,
        select(func.count())
        .select_from(CoachCorrection)
        .where(CoachCorrection.clip_id.in_(select(Clip.id).where(clip_scope))),
    )
    reviewed_clips = await _count(db, canonical_count.where(Clip.is_reviewed.is_(True)))

    sections: list[Section] = []

    if SECTION_SELF_SCOUT in wanted:
        # Clip count per personnel grouping (NULLs grouped as "Unknown").
        result = await db.execute(
            select(Clip.personnel_grouping, func.count(func.distinct(play_key)))
            .outerjoin(PlayView, PlayView.clip_id == Clip.id)
            .where(clip_scope, Clip.is_reviewed.is_(True))
            .group_by(Clip.personnel_grouping)
            .order_by(func.count().desc())
        )
        rows = [
            SectionRow(label=(grouping or "Unknown"), value=str(n)) for grouping, n in result.all()
        ]
        if not rows:
            rows = [SectionRow(label="No clips tagged yet", value="0")]
        sections.append(Section(title=SECTION_SELF_SCOUT, rows=rows))

    if SECTION_DEVELOPMENT in wanted:
        result = await db.execute(
            select(Player.position_group, func.count())
            .where(Player.is_active.is_(True))
            .group_by(Player.position_group)
            .order_by(Player.position_group.asc())
        )
        rows = [SectionRow(label=(group or "Unknown"), value=str(n)) for group, n in result.all()]
        if not rows:
            rows = [SectionRow(label="No active players", value="0")]
        sections.append(Section(title="Current roster by position group", rows=rows))

    if SECTION_MODEL_QUALITY in wanted:
        avg_result = await db.execute(select(func.avg(Clip.confidence)).where(clip_scope))
        avg_conf = avg_result.scalar_one_or_none()
        rows = [
            SectionRow(label="Clips reviewed", value=f"{reviewed_clips} of {total_clips}"),
            SectionRow(label="Coach corrections", value=str(total_corrections)),
            SectionRow(
                label="Average clip confidence",
                value=f"{float(avg_conf):.2f}" if avg_conf is not None else "n/a",
            ),
        ]
        sections.append(Section(title="Review progress and model confidence", rows=rows))

    if SECTION_OPPONENT_PREP in wanted:
        result = await db.execute(
            select(Video.opponent_team, func.count())
            .where(Video.opponent_team.isnot(None), Video.id.in_(videos))
            .group_by(Video.opponent_team)
            .order_by(func.count().desc())
        )
        rows = [
            SectionRow(label=opponent or "Unknown", value=str(n)) for opponent, n in result.all()
        ]
        if not rows:
            rows = [SectionRow(label="No opponent-tagged film yet", value="0")]
        sections.append(Section(title="Opponent film inventory", rows=rows))

    totals = {
        "videos": total_videos,
        "clips": total_clips,
        "active_players": total_players,
        "reviewed_clips": reviewed_clips,
        "corrections": total_corrections,
    }
    return CoachingSummaryData(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sections=sections,
        totals=totals,
        scope=scope,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_json(data: CoachingSummaryData) -> bytes:
    return json.dumps(data.to_dict(), indent=2).encode("utf-8")


def render_csv(data: CoachingSummaryData) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Section", "Label", "Value"])
    writer.writerow(
        [
            "Context",
            "Description",
            "Film inventory and reviewed tags; roster rows count active players.",
        ]
    )
    for key, value in sorted(data.scope.items()):
        writer.writerow(["Selection", key, value])
    for section in data.sections:
        for row in section.rows:
            writer.writerow([section.title, row.label, row.value])
    writer.writerow([])
    writer.writerow(["Totals", "", ""])
    for key, value in data.totals.items():
        writer.writerow(["Totals", key, value])
    writer.writerow([])
    writer.writerow(["Generated at", data.generated_at, ""])
    return buf.getvalue().encode("utf-8")


def render_pdf(data: CoachingSummaryData) -> bytes:
    """Render the coaching summary as a simple multi-section PDF.

    Uses ``reportlab.platypus`` (pure-Python, no system deps). Kept deliberately
    plain — the goal is a real PDF download in default mode, not a styled
    coaching deck.
    """
    # Imported lazily so a missing optional dep doesn't break the rest of the
    # module at import time (which would also break the aggregation tests).
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, title="Football-IQ Coaching Report")
    styles = getSampleStyleSheet()
    story: list[Any] = [
        Paragraph("Football-IQ — Coaching Report", styles["Title"]),
        Paragraph(f"Generated: {data.generated_at}", styles["Normal"]),
        Paragraph(
            "Film inventory and reviewed tags. Roster rows count active players.", styles["Normal"]
        ),
        Paragraph(
            f"Selection: {escape(json.dumps(data.scope, sort_keys=True))}"
            if data.scope
            else "Selection: all film",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]

    totals_rows = [["Metric", "Value"]] + [[k, str(v)] for k, v in data.totals.items()]
    totals_table = Table(totals_rows, hAlign="LEFT")
    totals_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story += [Paragraph("Totals", styles["Heading2"]), totals_table, Spacer(1, 14)]

    for section in data.sections:
        story.append(Paragraph(section.title, styles["Heading2"]))
        rows = [["Label", "Value"]] + [[r.label, r.value] for r in section.rows]
        table = Table(rows, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ]
            )
        )
        story += [table, Spacer(1, 12)]

    doc.build(story)
    return buf.getvalue()


RENDERERS = {
    ReportFormat.json: (render_json, "application/json"),
    ReportFormat.csv: (render_csv, "text/csv"),
    ReportFormat.pdf: (render_pdf, "application/pdf"),
}


def render(data: CoachingSummaryData, fmt: ReportFormat) -> tuple[bytes, str]:
    """Render ``data`` in ``fmt`` and return ``(body, content_type)``."""
    renderer, content_type = RENDERERS[fmt]
    return renderer(data), content_type


__all__ = [
    "ALL_SECTIONS",
    "CoachingSummaryData",
    "REPORT_TYPE_COACHING_SUMMARY",
    "SECTION_DEVELOPMENT",
    "SECTION_MODEL_QUALITY",
    "SECTION_OPPONENT_PREP",
    "SECTION_SELF_SCOUT",
    "Section",
    "SectionRow",
    "aggregate_coaching_summary",
    "render",
    "render_csv",
    "render_json",
    "render_pdf",
    "Counter",  # exposed for tests that want to verify section composition
]
