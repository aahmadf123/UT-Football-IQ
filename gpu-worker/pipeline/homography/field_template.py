"""NCAA field template — canonical pixel-to-yard landmark coordinates (Issue #127).

The homography stack maps pixel coordinates to a standardized field frame:

  X: 0 → 100 yards (goal line to goal line, excluding end zones)
  Y: -26.665 → +26.665 yards (south sideline → north sideline)

These are *NCAA-standard* field dimensions, not Toledo-specific constants —
a 53.33 yd (160 ft) wide field with inbound hash marks 60 ft from each
sideline, i.e. 40 ft (13.33 yd) apart (NCAA Rule 1, since 1993). The values are exposed as a dataclass so a venue with non-standard
markings can supply an override without editing call sites; nothing here is
keyed to a single stadium.

References:
- Issue #127 (NCAA field template constants)
- NCAA Football Rules, Rule 1 (the field)
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ── Canonical NCAA constants (yards) ──────────────────────────────────────────

FIELD_LENGTH: float = 100.0           # goal line to goal line
FIELD_WIDTH: float = 53.33            # sideline to sideline (160 ft)
HALF_WIDTH: float = FIELD_WIDTH / 2.0  # 26.665
# Inbound hash marks: 60 ft (20 yd) from each sideline, 40 ft (13.33 yd)
# apart. An earlier value put them 40 ft from each sideline (80 ft apart),
# which doubled every lateral distance on Toledo film (players at y = +/-48 on
# a field that ends at +/-26.7) and inflated speeds accordingly.
HASH_FROM_SIDELINE: float = 20.0
HASH_Y_SOUTH: float = -(HALF_WIDTH - HASH_FROM_SIDELINE)   # ~ -6.665
HASH_Y_NORTH: float = +(HALF_WIDTH - HASH_FROM_SIDELINE)   # ~ +6.665
# Yard lines painted every 5 yards between the goal lines.
YARD_LINES_X: tuple[int, ...] = tuple(range(5, 100, 5))


@dataclass(frozen=True)
class FieldTemplate:
    """Standardized field geometry used to build pixel↔yard correspondences.

    Defaults are NCAA-standard. Callers may pass an override (e.g. a venue
    with repainted hashes) but the common path uses :func:`default_template`.
    """

    length: float = FIELD_LENGTH
    width: float = FIELD_WIDTH
    hash_y_south: float = HASH_Y_SOUTH
    hash_y_north: float = HASH_Y_NORTH
    yard_lines_x: tuple[int, ...] = field(default=YARD_LINES_X)

    @property
    def half_width(self) -> float:
        return self.width / 2.0

    @property
    def sideline_y_south(self) -> float:
        return -self.half_width

    @property
    def sideline_y_north(self) -> float:
        return +self.half_width

    def yardline_segments(self) -> list[tuple[float, float, float, float]]:
        """Each yard line as a sideline-to-sideline segment ``(x, y0, x, y1)``."""
        return [
            (float(x), self.sideline_y_south, float(x), self.sideline_y_north)
            for x in self.yard_lines_x
        ]

    def landmark_points(self) -> dict[str, tuple[float, float]]:
        """Named field-frame landmarks: every yard line × {sideline, hash}.

        Returns a mapping of ``"<x>_<row>" -> (x_yd, y_yd)`` where ``row`` is
        one of ``south``/``north`` (sidelines) or ``hash_s``/``hash_n``
        (inbound hashes). Detection matches observed line intersections to
        these so the DLT solver receives labeled correspondences.
        """
        rows = {
            "south": self.sideline_y_south,
            "hash_s": self.hash_y_south,
            "hash_n": self.hash_y_north,
            "north": self.sideline_y_north,
        }
        points: dict[str, tuple[float, float]] = {}
        for x in self.yard_lines_x:
            for row_name, y in rows.items():
                points[f"{x}_{row_name}"] = (float(x), float(y))
        return points


def default_template() -> FieldTemplate:
    """Return the NCAA-standard field template."""
    return FieldTemplate()
