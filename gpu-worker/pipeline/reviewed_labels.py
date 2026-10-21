"""Select actual effective staff labels and format explicitly reviewed captions.

Raw pipeline labels are candidates. Their public flags cannot establish the
server's completed producer proof and model approval during an in-flight render.
Those labels remain available through the qualified interactive overlay API.
"""

from __future__ import annotations

import uuid
from typing import Any


def reviewed_label_data(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Select unambiguous clip-level human rows from the authenticated snapshot.

    Clip metadata and caller-declared qualification flags are not annotation
    receipts. The API owns ``annotated_by`` on its effective Label records.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for label in labels:
        if label.get("source") not in {"human", "manual"} or label.get("tracklet_id"):
            continue
        try:
            uuid.UUID(str(label.get("annotated_by")))
        except (ValueError, TypeError, AttributeError):
            continue
        value = label.get("label_value")
        if not isinstance(value, dict):
            continue
        provenance = value.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        if any(layer.get("source") in (
            "synthetic_test", "synthetic", "stub", "heuristic", "unavailable", "disabled",
            "unverified_adapter",
        )
               for layer in (value, provenance)):
            continue
        if provenance.get("state") == "superseded" or provenance.get("publication_state") == "pending":
            continue
        grouped.setdefault(str(label.get("label_type")), []).append(value)
    return {kind: values[0] for kind, values in grouped.items()
            if all(value == values[0] for value in values)}


def reviewed_hud(label_data: dict[str, Any] | None) -> tuple[str, str]:
    """Return explicitly attributed HUD text and an optional reviewed direction."""
    if not isinstance(label_data, dict):
        return "", ""
    parts: list[str] = []
    direction = ""
    for kind, key, caption in (
        ("offensive_formation", "formation", "OFF"),
        ("defensive_front", "front", "DEF"),
        ("play_direction", "direction", "DIR"),
    ):
        value = label_data.get(kind)
        scalar = value.get(key) if isinstance(value, dict) else None
        if not isinstance(scalar, str):
            continue
        scalar = scalar.strip()
        if not scalar or scalar.lower() in {"unknown", "unresolved", "unobserved"}:
            continue
        # Do not turn nested objects or control characters into a movie caption.
        if len(scalar) > 80 or any(not character.isprintable() for character in scalar):
            continue
        parts.append(f"{caption}: {scalar}")
        if kind == "play_direction" and scalar.lower() in {"left", "right"}:
            direction = scalar.lower()
    return ("STAFF  " + "  ".join(parts) if parts else "", direction)
