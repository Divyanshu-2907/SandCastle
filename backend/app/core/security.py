import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

# Cost 12 is the common 2020s default: ~250ms per hash on commodity hardware.
_ROUNDS = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        # Malformed hash in the DB -- treat as a failed login, not a 500.
        return False


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or not ours."""


def create_access_token(user_id: uuid.UUID, expires_minutes: int | None = None) -> str:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=expires_minutes or settings.jwt_expire_minutes)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> uuid.UUID:
    """Return the user id a token was issued for, or raise `TokenError`."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc

    subject = payload.get("sub")
    if not subject:
        raise TokenError("token has no subject")
    try:
        return uuid.UUID(subject)
    except ValueError as exc:
        # A syntactically valid token whose subject isn't one of our ids.
        raise TokenError("token subject is not a user id") from exc
