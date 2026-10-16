"""Async, backend-only CFBD HTTP client.

Bearer-token auth, configurable base URL, exponential backoff with rate-limit
handling, explicit error classes, and structured logging that never emits the
API key or the ``Authorization`` header. Build one with
:meth:`CFBDClient.from_settings` (which fails safely when ``CFBD_API_KEY`` is
unset) or directly for tests with an injected ``httpx`` transport.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, TypeVar

import httpx
import structlog
from pydantic import BaseModel

from app.cfbd.errors import (
    CFBDAuthError,
    CFBDClientError,
    CFBDConfigError,
    CFBDRateLimitError,
    CFBDServerError,
    CFBDTransportError,
)
from app.cfbd.schemas import (
    CFBDDrive,
    CFBDGame,
    CFBDGameTeamStats,
    CFBDPlay,
    CFBDTeam,
    CFBDWinProbability,
)
from app.config import get_settings

log = structlog.get_logger("app.cfbd.client")

_T = TypeVar("_T", bound=BaseModel)

# Default retry/backoff tuning. Kept small so a transient blip is smoothed over
# without turning a hard outage into a long stall on the request path.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 0.5
_DEFAULT_BACKOFF_CAP = 8.0
_DEFAULT_TIMEOUT = 30.0


class CFBDClient:
    """Typed async client for the College Football Data API (backend-only)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.collegefootballdata.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_cap: float = _DEFAULT_BACKOFF_CAP,
        timeout: float = _DEFAULT_TIMEOUT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key or not api_key.strip():
            # Backend-only failure: no value to leak, just point at the env var.
            raise CFBDConfigError(
                "CFBD_API_KEY is not configured; set it in the backend "
                "environment (see .env.example / docs/runbook-local.md). "
                "CFBD is never called from the frontend."
            )
        self._api_key = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        # The Authorization header lives only on the client instance and is
        # never logged. structlog calls below deliberately omit headers.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, **kwargs: Any) -> CFBDClient:
        """Build a client from application settings.

        Raises :class:`CFBDConfigError` when ``CFBD_API_KEY`` is unset so the
        failure is an explicit backend-only error rather than an opaque 401.
        """
        settings = get_settings()
        return cls(
            api_key=settings.cfbd_api_key,
            base_url=settings.cfbd_base_url,
            **kwargs,
        )

    async def __aenter__(self) -> CFBDClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── Core request loop ──────────────────────────────────────────────────

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_cap)
        return float(min(self._backoff_base * (2**attempt), self._backoff_cap))

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            # HTTP-date form is not used by CFBD; ignore rather than crash.
            return None

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        """Issue a GET with retries; return decoded JSON or raise a CFBD error."""
        clean_params = {k: v for k, v in params.items() if v is not None}
        last_transport_exc: httpx.TransportError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path, params=clean_params)
            except httpx.TransportError as exc:
                last_transport_exc = exc
                # Never log the exception's request headers; log the class only.
                log.warning(
                    "cfbd.request.transport_error",
                    path=path,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt, None))
                    continue
                raise CFBDTransportError(
                    f"CFBD transport error after {attempt + 1} attempts: {type(exc).__name__}"
                ) from exc

            status = response.status_code
            log.info(
                "cfbd.request",
                path=path,
                params=clean_params,
                status=status,
                attempt=attempt,
            )

            if 200 <= status < 300:
                return response.json()

            if 300 <= status < 400:
                raise CFBDClientError(
                    f"CFBD returned unexpected redirect {status}; "
                    "set follow_redirects=True if redirects are expected",
                    status_code=status,
                )

            if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                raise CFBDAuthError()

            if status == httpx.codes.TOO_MANY_REQUESTS:
                retry_after = self._parse_retry_after(response)
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt, retry_after))
                    continue
                raise CFBDRateLimitError(retry_after=retry_after)

            if status >= httpx.codes.INTERNAL_SERVER_ERROR:
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_delay(attempt, None))
                    continue
                raise CFBDServerError(
                    f"CFBD server error {status} after {attempt + 1} attempts",
                    status_code=status,
                )

            # Any other 4xx is a caller/permanent error — do not retry.
            raise CFBDClientError(f"CFBD returned client error {status}", status_code=status)

        # Unreachable in practice: the loop either returns or raises. Guard for
        # the type checker and as a defensive backstop.
        raise CFBDTransportError("CFBD request exhausted retries") from last_transport_exc

    async def _get_list(self, path: str, params: dict[str, Any], model: type[_T]) -> list[_T]:
        payload = await self._request(path, params)
        if not isinstance(payload, list):
            raise CFBDClientError(f"CFBD {path} returned a non-list payload", status_code=200)
        return [model.model_validate(item) for item in payload]

    async def _get_raw_list(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """For player endpoints whose rows are stored as-is (JSONB): no schema."""
        payload = await self._request(path, params)
        if not isinstance(payload, list):
            raise CFBDClientError(f"CFBD {path} returned a non-list payload", status_code=200)
        return [item for item in payload if isinstance(item, dict)]

    # ── Endpoint families (Issue #161) ──────────────────────────────────────

    async def get_teams(self, *, conference: str | None = None) -> list[CFBDTeam]:
        """``GET /teams`` — team metadata (optionally filtered by conference)."""
        return await self._get_list("/teams", {"conference": conference}, CFBDTeam)

    async def get_games(
        self,
        *,
        year: int,
        team: str | None = None,
        conference: str | None = None,
        season_type: str | None = None,
        week: int | None = None,
    ) -> list[CFBDGame]:
        """``GET /games`` — schedules / results."""
        return await self._get_list(
            "/games",
            {
                "year": year,
                "team": team,
                "conference": conference,
                "seasonType": season_type,
                "week": week,
            },
            CFBDGame,
        )

    async def get_drives(
        self,
        *,
        year: int,
        team: str | None = None,
        conference: str | None = None,
        season_type: str | None = None,
        week: int | None = None,
    ) -> list[CFBDDrive]:
        """``GET /drives`` — drive summaries."""
        return await self._get_list(
            "/drives",
            {
                "year": year,
                "team": team,
                "conference": conference,
                "seasonType": season_type,
                "week": week,
            },
            CFBDDrive,
        )

    async def get_plays(
        self,
        *,
        year: int,
        week: int,
        team: str | None = None,
        conference: str | None = None,
        season_type: str | None = None,
    ) -> list[CFBDPlay]:
        """``GET /plays`` — play-by-play (CFBD requires year + week)."""
        return await self._get_list(
            "/plays",
            {
                "year": year,
                "week": week,
                "team": team,
                "conference": conference,
                "seasonType": season_type,
            },
            CFBDPlay,
        )

    async def get_team_game_stats(
        self,
        *,
        year: int,
        team: str | None = None,
        conference: str | None = None,
        season_type: str | None = None,
        week: int | None = None,
    ) -> list[CFBDGameTeamStats]:
        """``GET /games/teams`` — per-team box-score stats grouped by game."""
        return await self._get_list(
            "/games/teams",
            {
                "year": year,
                "team": team,
                "conference": conference,
                "seasonType": season_type,
                "week": week,
            },
            CFBDGameTeamStats,
        )

    async def get_win_probability(self, *, game_id: int) -> list[CFBDWinProbability]:
        """``GET /metrics/wp`` — per-play win probability for one game."""
        return await self._get_list("/metrics/wp", {"gameId": game_id}, CFBDWinProbability)

    # ── Player endpoints (roster + per-player season data) ─────────────────

    async def get_roster(self, *, team: str, year: int) -> list[dict[str, Any]]:
        """``GET /roster`` — id, names, jersey, position, height, weight, class, hometown."""
        return await self._get_raw_list("/roster", {"team": team, "year": year})

    async def get_player_season_stats(
        self, *, team: str, year: int, season_type: str | None = None
    ) -> list[dict[str, Any]]:
        """``GET /stats/player/season`` — one row per (player, category, statType)."""
        return await self._get_raw_list(
            "/stats/player/season", {"team": team, "year": year, "seasonType": season_type}
        )

    async def get_player_usage(self, *, team: str, year: int) -> list[dict[str, Any]]:
        """``GET /player/usage`` — share of team plays by down/situation."""
        return await self._get_raw_list("/player/usage", {"team": team, "year": year})

    async def get_player_ppa(self, *, team: str, year: int) -> list[dict[str, Any]]:
        """``GET /ppa/players/season`` — average/total predicted points added."""
        return await self._get_raw_list("/ppa/players/season", {"team": team, "year": year})
