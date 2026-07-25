"""Offline Roboflow / StatsBomb-AMF coverage harness (Issue #167 — spike).

OFFLINE EVALUATION ONLY. Like ``eval.sportqa_sportr_eval`` this module:

  * never downloads a Roboflow dataset/model or the StatsBomb AMF dump
    (callers point it at a local copy / a pasted class list),
  * never commits the real datasets or model weights,
  * never runs in the same-session or nightly production pipeline,
  * is not wired into the model router (`pipeline.model_router`),
  * makes no hosted-inference / Roboflow API call (no API key is read),
  * makes no claim that dataset coverage equals Toledo-clip validation.

What it *does*:

  * **Roboflow** — take a candidate dataset's *class list* (the labels it was
    annotated with) and report which Football-IQ CV classes it covers
    (players, ball, helmets, officials, team), explicitly flag soccer /
    association-football classes (per ``docs/external-resource-rubric.md`` §2),
    and **hard-flag any face / biometric class** so a face-recognition path can
    never be introduced silently (Issue #167 acceptance criterion).
  * **StatsBomb AMF** — take a list of present data layers (plays, events,
    low-frequency tracking, high-frequency tracking) and report which
    route/formation/coverage research signals they support.

The spike's question is "do these resources transfer to single-camera Toledo
MP4 clips, and which classes/signals are useful?", which is a *coverage*
question, not an accuracy one — so this harness runs no model and scores no
metric. A small **synthetic** sample (fabricated — *not* the real datasets) lets
``python -m eval.roboflow_statsbomb_eval demo`` and the unit test run with no
external data and no network. See
``reports/spike-issue167-roboflow-statsbomb-amf.md``.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

USAGE_MARKER = "offline-evaluation-only"

# Football-IQ CV classes the spike cares about (the issue's "classes match
# Football-IQ needs: players, helmets, ball, officials, teams"). Each maps to a
# set of synonyms a Roboflow dataset might use for the same concept.
FOOTBALL_IQ_CLASSES: dict[str, tuple[str, ...]] = {
    "player": ("player", "players", "person", "athlete", "offense", "defense"),
    "ball": ("ball", "football", "american football", "pigskin"),
    "helmet": ("helmet", "helmets", "head gear", "headgear"),
    "official": ("official", "officials", "referee", "ref", "umpire", "linesman"),
    "team": ("team", "home", "away", "home team", "away team", "jersey"),
}

# Soccer / association-football class names — excluded from Football-IQ use
# (``docs/external-resource-rubric.md`` §2). A bare "football" is intentionally
# NOT treated as soccer here because in a CV class list "football" most often
# means the ball object; soccer is detected via its distinctive vocabulary.
SOCCER_TERMS: tuple[str, ...] = (
    "soccer",
    "association football",
    "futbol",
    "fútbol",
    "goalkeeper",
    "goal post",
    "goalpost",
    "corner flag",
    "penalty spot",
    "pitch",
)

# Face / biometric class names. Any of these is a HARD FLAG: Football-IQ must
# not introduce a face-recognition path (Issue #167). A dataset carrying these
# classes is rejected for transfer unless the offending classes are dropped.
FACE_BIOMETRIC_TERMS: tuple[str, ...] = (
    "face",
    "facial",
    "face_id",
    "faceid",
    "face recognition",
    "facial recognition",
    "identity",
    "person_id",
    "fingerprint",
    "iris",
    "gait",
)

# StatsBomb AMF data layers and the research signals each one supports.
STATSBOMB_LAYERS: dict[str, str] = {
    "plays": "play-level metadata (down, distance, personnel) — formation context",
    "events": "discrete in-play events — coverage / route outcome research",
    "lft": "low-frequency tracking — coarse player positions for formation study",
    "tracking": "high-frequency tracking — full route / coverage geometry",
}


def normalize_label(label: str) -> str:
    return " ".join(label.lower().replace("_", " ").replace("-", " ").split())


def is_soccer_label(label: str) -> bool:
    norm = normalize_label(label)
    tokens = set(norm.split())
    for term in SOCCER_TERMS:
        t = normalize_label(term)
        if (" " in t and t in norm) or (" " not in t and t in tokens):
            return True
    return False


def is_face_biometric_label(label: str) -> bool:
    norm = normalize_label(label)
    tokens = set(norm.split())
    for term in FACE_BIOMETRIC_TERMS:
        t = normalize_label(term)
        if (" " in t and t in norm) or (" " not in t and t in tokens):
            return True
    return False

def matched_football_iq_class(label: str) -> str | None:
    """Return the Football-IQ class a Roboflow label maps to, or ``None``."""
    norm = normalize_label(label)
    for fiq_class, synonyms in FOOTBALL_IQ_CLASSES.items():
        # Match a whole-word synonym (covers single-word labels and tokens of a
        # compound label like "away team") or a multi-word synonym appearing as
        # a phrase. Single-token substring matching is intentionally avoided so
        # e.g. "ref" cannot match an unrelated label.
        if any(
            syn == norm or syn in norm.split() or (" " in syn and syn in norm)
            for syn in synonyms
        ):
            return fiq_class
    return None


@dataclass(frozen=True)
class RoboflowDataset:
    """A candidate Roboflow dataset described by its class list (offline)."""

    name: str
    classes: tuple[str, ...]
    # "broadcast" | "nfl-broadcast" | "single-camera" | "sideline" | "unknown".
    # The spike must know if examples match Toledo single-camera film or are
    # mostly broadcast/NFL imagery.
    perspective: str = "unknown"
    license: str = "unknown"


# Fabricated rows — NOT the real datasets. Shapes mirror the kinds of class
# lists Roboflow NFL projects publish so the loader and report logic are
# exercised without any download or API key.
SYNTHETIC_ROBOFLOW: tuple[RoboflowDataset, ...] = (
    RoboflowDataset(
        name="nfl-competition-style",
        classes=("helmet", "player"),
        perspective="nfl-broadcast",
        license="CC BY 4.0 (verify per version)",
    ),
    RoboflowDataset(
        name="nflfootball-objects-style",
        classes=("player", "ball", "referee", "helmet"),
        perspective="broadcast",
        license="unknown (verify per version)",
    ),
    RoboflowDataset(
        name="soccer-mislabeled-football",
        classes=("player", "ball", "goalkeeper", "goal post"),
        perspective="broadcast",
        license="unknown",
    ),
    RoboflowDataset(
        name="face-id-dataset",
        classes=("player", "face", "identity"),
        perspective="unknown",
        license="unknown",
    ),
)

SYNTHETIC_STATSBOMB_LAYERS: tuple[str, ...] = ("plays", "events", "lft", "tracking")


def _row_get(row: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _row_list(row: dict[str, object], *keys: str) -> tuple[str, ...]:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value if str(v).strip())
        if isinstance(value, dict):
            # YOLO data.yaml "names" can be a {index: name} mapping.
            return tuple(str(v) for v in value.values() if str(v).strip())
    return ()


def load_roboflow_jsonl(path: str | Path) -> list[RoboflowDataset]:
    """Load Roboflow dataset descriptors from a local JSONL file.

    Each line is a dict with at least a ``classes``/``names`` list. Never
    downloads — the file must already exist locally. Malformed or class-less
    lines are skipped so a partial export degrades gracefully.
    """
    out: list[RoboflowDataset] = []
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
            classes = _row_list(row, "classes", "names", "labels")
            if not classes:
                continue
            out.append(
                RoboflowDataset(
                    name=_row_get(row, "name", "dataset", "project") or "unnamed",
                    classes=classes,
                    perspective=_row_get(row, "perspective", "view", "source") or "unknown",
                    license=_row_get(row, "license", "licence") or "unknown",
                )
            )
    return out


@dataclass
class RoboflowReport:
    datasets: int = 0
    covered_classes: set[str] = field(default_factory=set)
    missing_classes: set[str] = field(default_factory=set)
    soccer_flagged: Counter[str] = field(default_factory=Counter)
    face_biometric_flagged: Counter[str] = field(default_factory=Counter)
    by_perspective: Counter[str] = field(default_factory=Counter)

    @property
    def has_face_biometric(self) -> bool:
        return sum(self.face_biometric_flagged.values()) > 0

    @property
    def has_soccer(self) -> bool:
        return sum(self.soccer_flagged.values()) > 0


def build_roboflow_report(datasets: list[RoboflowDataset]) -> RoboflowReport:
    report = RoboflowReport()
    for ds in datasets:
        report.datasets += 1
        report.by_perspective[normalize_label(ds.perspective) or "unknown"] += 1
        for label in ds.classes:
            if is_face_biometric_label(label):
                report.face_biometric_flagged[normalize_label(label)] += 1
            if is_soccer_label(label):
                report.soccer_flagged[normalize_label(label)] += 1
            matched = matched_football_iq_class(label)
            if matched is not None:
                report.covered_classes.add(matched)
    report.missing_classes = set(FOOTBALL_IQ_CLASSES) - report.covered_classes
    return report


@dataclass
class StatsBombReport:
    layers_present: tuple[str, ...] = ()
    research_signals: dict[str, str] = field(default_factory=dict)
    unknown_layers: tuple[str, ...] = ()

    @property
    def supports_route_coverage_research(self) -> bool:
        # High-frequency tracking is what makes route/coverage geometry usable.
        return "tracking" in self.layers_present


def build_statsbomb_report(layers: list[str] | tuple[str, ...]) -> StatsBombReport:
    present = tuple(normalize_label(layer_) for layer_ in layers)
    known = tuple(layer_ for layer_ in present if layer_ in STATSBOMB_LAYERS)
    unknown = tuple(layer_ for layer_ in present if layer_ not in STATSBOMB_LAYERS)
    signals = {layer_: STATSBOMB_LAYERS[layer_] for layer_ in known}
    return StatsBombReport(
        layers_present=known,
        research_signals=signals,
        unknown_layers=unknown,
    )


def render_markdown(roboflow: RoboflowReport, statsbomb: StatsBombReport) -> str:
    lines: list[str] = []
    lines.append("# Roboflow / StatsBomb-AMF coverage report (offline)\n")
    lines.append(f"_Usage: {USAGE_MARKER}. Synthetic or local data only; no API key, no download._\n")

    lines.append("## Roboflow")
    lines.append(f"- Datasets reviewed: **{roboflow.datasets}**")
    lines.append("- Football-IQ classes covered: "
                 f"**{', '.join(sorted(roboflow.covered_classes)) or 'none'}**")
    lines.append("- Football-IQ classes missing: "
                 f"**{', '.join(sorted(roboflow.missing_classes)) or 'none'}**")
    lines.append(f"- Soccer classes flagged: **{roboflow.has_soccer}** "
                 f"({', '.join(sorted(roboflow.soccer_flagged)) or 'none'})")
    lines.append(f"- Face / biometric classes flagged: **{roboflow.has_face_biometric}** "
                 f"({', '.join(sorted(roboflow.face_biometric_flagged)) or 'none'})")
    if roboflow.has_face_biometric:
        lines.append("  - ⛔ HARD FLAG: face/biometric class present — reject for transfer "
                     "or drop the class. No face-recognition path may be introduced (#167).")
    lines.append("- By perspective:")
    for name, count in sorted(roboflow.by_perspective.items()):
        lines.append(f"  - {name}: {count}")

    lines.append("\n## StatsBomb AMF")
    lines.append("- Data layers present: "
                 f"**{', '.join(statsbomb.layers_present) or 'none'}**")
    lines.append("- Supports route/coverage research (needs hi-freq tracking): "
                 f"**{statsbomb.supports_route_coverage_research}**")
    for layer_, signal in sorted(statsbomb.research_signals.items()):
        lines.append(f"  - {layer_}: {signal}")
    if statsbomb.unknown_layers:
        lines.append(f"- Unknown layers (ignored): {', '.join(statsbomb.unknown_layers)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval.roboflow_statsbomb_eval",
        description="Offline Roboflow/StatsBomb-AMF coverage report (Issue #167).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="Run on the bundled synthetic sample (no external data).")

    report_cmd = sub.add_parser("report", help="Run on a local Roboflow class-list JSONL.")
    report_cmd.add_argument("--roboflow", type=str, default=None,
                            help="Path to a local Roboflow class-list JSONL.")
    report_cmd.add_argument("--statsbomb-layers", type=str, default=None,
                            help="Comma-separated AMF layers present locally "
                                 "(e.g. 'plays,events,lft,tracking').")

    args = parser.parse_args(argv)

    if args.command == "demo":
        roboflow_datasets = list(SYNTHETIC_ROBOFLOW)
        statsbomb_layers: list[str] = list(SYNTHETIC_STATSBOMB_LAYERS)
    else:
        roboflow_datasets = load_roboflow_jsonl(args.roboflow) if args.roboflow else []
        statsbomb_layers = (
            [s for s in args.statsbomb_layers.split(",") if s.strip()]
            if args.statsbomb_layers
            else []
        )
        if not roboflow_datasets and not statsbomb_layers:
            parser.error("Provide --roboflow and/or --statsbomb-layers.")

    roboflow_report = build_roboflow_report(roboflow_datasets)
    statsbomb_report = build_statsbomb_report(statsbomb_layers)
    print(render_markdown(roboflow_report, statsbomb_report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
