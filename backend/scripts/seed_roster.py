"""Seed the season roster into the ``players`` table (idempotent).

Usage:

    python -m scripts.seed_roster [--roster PATH] [--deactivate-missing]

Reads ``app/data/roster_2026.json`` by default. Safe to re-run: rows are
matched by ``metadata->>'roster_key'`` and updated in place. Players absent
from the file are only deactivated (never deleted) and only with
``--deactivate-missing`` — tracklets and profile history reference player
rows, so history always survives roster churn.

Identity note: jersey numbers repeat across sides of the ball, so film-side
identity resolution keys on ``(jersey_number, position_group)`` — enforced by
the ``uq_players_jersey_posgroup_active`` partial unique index.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import get_settings
from app.roster_seeding import seed_roster


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to a roster JSON file (default: app/data/roster_2026.json)",
    )
    parser.add_argument(
        "--deactivate-missing",
        action="store_true",
        help="Set is_active=false for previously seeded players absent from the file",
    )
    args = parser.parse_args()

    stats = await seed_roster(
        database_url=get_settings().database_url,
        roster_path=args.roster,
        deactivate_missing=args.deactivate_missing,
    )
    print(
        "Roster seed complete: "
        f"created={stats['created']} updated={stats['updated']} "
        f"unchanged={stats['unchanged']} deactivated={stats['deactivated']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
