"""Offline SportQA / SportR coverage harness (Issue #168 — spike).

OFFLINE EVALUATION ONLY. Like ``datasets/bdb`` this module:

  * never downloads SportQA / SportR (callers point it at a local copy),
  * never commits the real datasets,
  * never runs in the same-session or nightly production pipeline,
  * is not wired into the model router (`pipeline.model_router`),
  * makes no claim that benchmark accuracy equals Toledo-clip validation.

What it *does*: load SportQA-style (text MCQ) and SportR-style (multimodal)
question rows from local JSONL, then report **American-football coverage** —
how many examples are American football, what tasks/modalities they cover, and
explicitly how many soccer / association-football rows were excluded (per
``docs/external-resource-rubric.md`` §2). It runs no model and scores no LLM;
the spike's question is "is American football present and useful?", which is a
coverage question, not an accuracy one.

A small **synthetic** sample (`SYNTHETIC_SAMPLE`, fabricated — *not* the real
datasets) lets ``python -m eval.sportqa_sportr_eval demo`` and the unit test run
with no external data. See ``reports/spike-issue168-sportqa-sportr.md``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

USAGE_MARKER = "offline-evaluation-only"

# Explicit American-football vocabulary. A bare "football" is intentionally NOT
# here: outside the US "football" means soccer, so we never silently count it as
# American football.
AMERICAN_FOOTBALL_TERMS: tuple[str, ...] = (
    "american football",
    "american_football",
    "football (american)",
    "gridiron",
    "nfl",
    "ncaa football",
    "ncaaf",
    "college football",
)

# Soccer / association football — excluded from Football-IQ evaluation.
SOCCER_TERMS: tuple[str, ...] = (
    "soccer",
    "association football",
    "futbol",
    "fútbol",
)


def normalize_sport(sport: str) -> str:
    return " ".join(sport.lower().replace("_", " ").split())


def is_american_football(sport: str) -> bool:
    norm = normalize_sport(sport)
    return any(term in norm for term in AMERICAN_FOOTBALL_TERMS)


def is_soccer(sport: str) -> bool:
    norm = normalize_sport(sport)
    if any(term in norm for term in SOCCER_TERMS):
        return True
    # A bare "football" (no "american") is treated as soccer-ambiguous and
    # excluded from the American-football subset to stay on the safe side.
    return norm == "football"


@dataclass(frozen=True)
class QAExample:
    """One benchmark row, normalized across SportQA / SportR schemas."""

    dataset: str  # "sportqa" | "sportr"
    sport: str
    task: str  # difficulty level / reasoning task
    question: str
    has_image: bool = False
    has_video: bool = False

    @property
    def modality(self) -> str:
        if self.has_video:
            return "video"
        if self.has_image:
            return "image"
        return "text"


# Fabricated rows — NOT the real datasets. Shapes mirror the documented schemas
# so the loader and report logic are exercised without any download.
SYNTHETIC_SAMPLE: tuple[QAExample, ...] = (
    QAExample("sportqa", "American Football", "L1-foundational",
              "How many points is a touchdown worth?"),
    QAExample("sportqa", "American Football", "L2-rules",
              "On 4th and 1, what are the offense's options?"),
    QAExample("sportqa", "American Football", "L3-scenario",
              "Trips right vs Cover 3 — which underneath defender carries the seam?"),
    QAExample("sportqa", "Basketball", "L1-foundational",
              "How many players per team are on the court?"),
    QAExample("sportqa", "Soccer", "L2-rules",
              "When is a player in an offside position?"),
    QAExample("sportr", "American Football", "rule-grounding",
              "Is this a holding penalty? Ground the offending lineman.",
              has_image=True),
    QAExample("sportr", "American Football", "tactics-reasoning",
              "Explain the coverage rotation after the snap.", has_video=True),
    QAExample("sportr", "Soccer", "rule-grounding",
              "Was this a handball in the box?", has_image=True),
    QAExample("sportr", "Basketball", "tactics-reasoning",
              "Explain the pick-and-roll coverage.", has_video=True),
)


def _row_get(row: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _row_bool(row: dict[str, object], *keys: str) -> bool:
    for key in keys:
        if key in row:
            value = row[key]
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                norm = value.strip().lower()
                if not norm:
                    return False
                if norm in {"0", "false", "no", "none", "null"}:
                    return False
                return True
            if value:
                return True
    return False


def load_examples_jsonl(path: str | Path, dataset: str) -> list[QAExample]:
    """Load benchmark rows from a local JSONL file (tolerant of field names).

    Never downloads — the file must already exist locally. Malformed lines are
    skipped so a partial export degrades gracefully.
    """
    out: list[QAExample] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            sport = _row_get(row, "sport", "Sport", "sport_type", "category")
            if not sport:
                continue
            out.append(
                QAExample(
                    dataset=dataset,
                    sport=sport,
                    task=_row_get(row, "task", "level", "difficulty", "split") or "unknown",
                    question=_row_get(row, "question", "Q", "text", "prompt"),
                    has_image=_row_bool(row, "has_image", "image", "has_images"),
                    has_video=_row_bool(row, "has_video", "video", "has_videos"),
                )
            )
    return out


@dataclass
class CoverageReport:
    total: int = 0
    by_dataset: Counter[str] = field(default_factory=Counter)
    by_sport: Counter[str] = field(default_factory=Counter)
    american_football: int = 0
    soccer_excluded: int = 0
    af_by_task: Counter[str] = field(default_factory=Counter)
    af_by_modality: Counter[str] = field(default_factory=Counter)

    @property
    def american_football_present(self) -> bool:
        return self.american_football > 0


def build_report(examples: list[QAExample]) -> CoverageReport:
    report = CoverageReport()
    for ex in examples:
        report.total += 1
        report.by_dataset[ex.dataset] += 1
        report.by_sport[ex.sport] += 1
        if is_american_football(ex.sport):
            report.american_football += 1
            report.af_by_task[ex.task] += 1
            report.af_by_modality[ex.modality] += 1
        elif is_soccer(ex.sport):
            report.soccer_excluded += 1
    return report


def render_markdown(report: CoverageReport) -> str:
    lines: list[str] = []
    lines.append("# SportQA / SportR coverage report (offline)\n")
    lines.append(f"_Usage: {USAGE_MARKER}. Synthetic or local data only._\n")
    lines.append(f"- Total examples: **{report.total}**")
    lines.append(f"- American-football examples: **{report.american_football}** "
                 f"(present: {report.american_football_present})")
    lines.append(f"- Soccer examples excluded: **{report.soccer_excluded}**")
    lines.append("\n## By dataset")
    for name, count in sorted(report.by_dataset.items()):
        lines.append(f"- {name}: {count}")
    lines.append("\n## American football — by task")
    for name, count in sorted(report.af_by_task.items()):
        lines.append(f"- {name}: {count}")
    lines.append("\n## American football — by modality")
    for name, count in sorted(report.af_by_modality.items()):
        lines.append(f"- {name}: {count}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.sportqa_sportr_eval",
        description="Offline SportQA/SportR American-football coverage report (Issue #168).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="Run on the bundled synthetic sample (no external data).")

    report_cmd = sub.add_parser("report", help="Run on local SportQA/SportR JSONL exports.")
    report_cmd.add_argument("--sportqa", type=str, default=None, help="Path to SportQA JSONL.")
    report_cmd.add_argument("--sportr", type=str, default=None, help="Path to SportR JSONL.")

    args = parser.parse_args(argv)

    if args.command == "demo":
        examples = list(SYNTHETIC_SAMPLE)
    else:
        examples = []
        if args.sportqa:
            examples += load_examples_jsonl(args.sportqa, "sportqa")
        if args.sportr:
            examples += load_examples_jsonl(args.sportr, "sportr")
        if not examples:
            parser.error("Provide --sportqa and/or --sportr pointing at local JSONL exports.")

    print(render_markdown(build_report(examples)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
