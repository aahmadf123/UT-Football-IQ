"""The locked detector taxonomy and legacy-class remap table.

Single source of truth for what the consolidated Roboflow project may
contain. The canonical classes are exactly the classes the pipeline's
detection stage emits (see ``pipeline/stage_detect.py``): ``player``,
``official``, ``ball``. Team and position are deliberately NOT detector
classes — team comes from the k-means team classifier and position from
downstream analytics, so encoding them in boxes would only fragment the
training signal.

The remap table folds every class name observed across the four legacy
workspace projects (ballgame3, football-players, american-football-analyst,
find-american-football) into the canonical set. Anything unmapped is dropped
— with counts, never silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CANONICAL_CLASSES: tuple[str, ...] = ("player", "official", "ball")

# Lowercased source-class → canonical class. Position subclasses collapse to
# ``player`` on purpose (see module docstring); ball possession states
# collapse to ``ball`` (the ball state machine re-derives possession).
REMAP: dict[str, str] = {
    # players
    "player": "player",
    "players": "player",
    "football-players": "player",
    "american-football-players": "player",
    "player-white": "player",
    "player-color": "player",
    "skill": "player",
    "center": "player",
    "c": "player",
    "qb": "player",
    "rb": "player",
    "wr": "player",
    "te": "player",
    "lb": "player",
    "db": "player",
    "s": "player",
    "cb": "player",
    "de": "player",
    "dt": "player",
    "ol": "player",
    "dl": "player",
    # officials
    "referee": "official",
    "ref": "official",
    "official": "official",
    "officials": "official",
    # ball
    "ball": "ball",
    "football": "ball",
    "american football": "ball",
    "american-football": "ball",
    "balllls": "ball",
    "ball-grounded": "ball",
    "ball-possessed": "ball",
}


def remap_class(name: str) -> str | None:
    """Map a source class name to its canonical class, or None to drop it."""
    return REMAP.get(name.strip().lower())


@dataclass
class RemapStats:
    """Reconciliation counts for one remapped dataset."""

    kept: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)

    def record(self, source_name: str, canonical: str | None) -> None:
        if canonical is None:
            self.dropped[source_name] = self.dropped.get(source_name, 0) + 1
        else:
            self.kept[canonical] = self.kept.get(canonical, 0) + 1

    def table(self) -> str:
        lines = ["class          kept"]
        for cls in CANONICAL_CLASSES:
            lines.append(f"{cls:<14} {self.kept.get(cls, 0)}")
        if self.dropped:
            lines.append("-- dropped (unmapped source classes) --")
            for name, count in sorted(self.dropped.items(), key=lambda kv: -kv[1]):
                lines.append(f"{name:<14} {count}")
        return "\n".join(lines)


def remap_coco(coco: dict[str, Any]) -> tuple[dict[str, Any], RemapStats]:
    """Rewrite a COCO dict onto the canonical taxonomy.

    Returns a new COCO dict whose ``categories`` are exactly the canonical
    classes (ids 1..3 in canonical order) and whose annotations are remapped
    or dropped, plus the reconciliation stats. Images are left untouched —
    images whose annotations all drop simply become unannotated (they are
    still useful as negatives).
    """
    stats = RemapStats()
    canonical_ids = {name: i + 1 for i, name in enumerate(CANONICAL_CLASSES)}
    source_names = {c["id"]: str(c.get("name", "")) for c in coco.get("categories", [])}

    annotations = []
    for ann in coco.get("annotations", []):
        source = source_names.get(ann.get("category_id"), "<unknown-category>")
        canonical = remap_class(source)
        stats.record(source, canonical)
        if canonical is None:
            continue
        remapped = dict(ann)
        remapped["category_id"] = canonical_ids[canonical]
        annotations.append(remapped)

    out = dict(coco)
    out["categories"] = [
        {"id": canonical_ids[name], "name": name, "supercategory": "none"}
        for name in CANONICAL_CLASSES
    ]
    out["annotations"] = annotations
    return out, stats
