"""Fail closed before integration fixtures seed an explicitly disposable database."""

from urllib.parse import urlsplit

_SCRATCH_DATABASES = frozenset({"footiq_overhaul_completion", "footiq_test"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def require_scratch_database(database_url: str) -> None:
    """Permit named loopback PostgreSQL scratch stores, independent of port.

    No URL is repeated in errors: it can contain credentials. Query parameters
    are refused because libpq/driver destination overrides could bypass host
    checks. The actual CI and local scratch configurations need none.
    """
    message = "Integration seeding requires an explicitly allowlisted loopback scratch database"
    try:
        parsed = urlsplit(database_url)
        port = parsed.port
        if (
            parsed.scheme not in {"postgresql", "postgresql+asyncpg"}
            or parsed.hostname not in _LOOPBACK_HOSTS
            or parsed.path not in {f"/{name}" for name in _SCRATCH_DATABASES}
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError(message)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(message) from None
