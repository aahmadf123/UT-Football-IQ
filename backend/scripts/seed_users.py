"""Seed the admin and GPU-worker service accounts (idempotent).

Usage (env-driven so compose can run it unattended):

    SEED_ADMIN_EMAIL=admin@example.com SEED_ADMIN_PASSWORD=... \\
    SEED_WORKER_EMAIL=worker@example.com SEED_WORKER_PASSWORD=... \\
    python -m scripts.seed_users

Use a routable domain: the login endpoint validates emails with
``EmailStr``, which REJECTS reserved special-use domains like ``.local`` —
an account seeded at ``worker@footballiq.local`` could never authenticate.

Existing accounts are left untouched by default; set
``SEED_RESET_PASSWORDS=1`` to rotate seeded-user passwords intentionally.
The worker account gets the ``analyst`` role, which every writeback route
accepts via ``require_any_staff``.
"""

from __future__ import annotations

import asyncio
import os
import sys

import structlog
from app.config import get_settings
from app.user_seeding import parse_reset_passwords_flag, seed_configured_users

log = structlog.get_logger(__name__)


async def main() -> int:
    admin_email = os.environ.get("SEED_ADMIN_EMAIL", "")
    admin_password = os.environ.get("SEED_ADMIN_PASSWORD", "")
    worker_email = os.environ.get("SEED_WORKER_EMAIL", "")
    worker_password = os.environ.get("SEED_WORKER_PASSWORD", "")
    reset_passwords = parse_reset_passwords_flag(os.environ.get("SEED_RESET_PASSWORDS"))

    if not any([admin_email, worker_email]):
        print(
            "Nothing to seed: set SEED_ADMIN_EMAIL/SEED_ADMIN_PASSWORD and/or "
            "SEED_WORKER_EMAIL/SEED_WORKER_PASSWORD.",
            file=sys.stderr,
        )
        return 2
    if admin_email and not admin_password:
        print("SEED_ADMIN_PASSWORD is required with SEED_ADMIN_EMAIL", file=sys.stderr)
        return 2
    if worker_email and not worker_password:
        print("SEED_WORKER_PASSWORD is required with SEED_WORKER_EMAIL", file=sys.stderr)
        return 2

    stats = await seed_configured_users(
        database_url=get_settings().database_url,
        admin_email=admin_email,
        admin_password=admin_password,
        worker_email=worker_email,
        worker_password=worker_password,
        reset_passwords=reset_passwords,
    )
    print(
        "Seed complete: "
        f"created={stats['created']} updated={stats['updated']} unchanged={stats['unchanged']} "
        f"reset_passwords={reset_passwords}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
