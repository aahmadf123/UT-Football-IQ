"""Auth utilities — JWT issuance and verification, password hashing."""

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

bearer_scheme = HTTPBearer()


# bcrypt only ever hashes the first 72 bytes of a password, and bcrypt 5+
# raises ValueError on anything longer instead of truncating. Truncate to 72
# bytes consistently in both hash and verify so a long passphrase (or a
# multi-byte one that crosses 72 bytes) is accepted rather than 500-ing the
# register/login endpoints.
_BCRYPT_MAX_BYTES = 72


def _password_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_password_bytes(plain), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(_password_bytes(plain), hashed.encode("utf-8"))


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {"sub": subject, "exp": expire, "type": "access"}
    if extra:
        payload.update(extra)
    return str(jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm))


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_expire_days)
    return str(
        jwt.encode(
            {"sub": subject, "exp": expire, "type": "refresh"},
            settings.secret_key,
            algorithm=settings.jwt_algorithm,
        )
    )


def decode_token(token: str) -> dict[str, Any]:
    try:
        result: dict[str, Any] = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
        return result
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """FastAPI dependency — returns the user ID from a valid JWT."""
    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")
    user_id: str | None = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing subject")
    return user_id
