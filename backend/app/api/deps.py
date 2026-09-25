"""Shared FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import TokenError, decode_access_token
from app.db.session import get_db
from app.models import User

# auto_error=False so a missing header produces our 401 with a WWW-Authenticate
# header, rather than FastAPI's bare 403.
bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise _UNAUTHORIZED

    try:
        user_id = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise _UNAUTHORIZED from exc

    user = db.scalar(select(User).where(User.id == user_id))
    if user is None:
        # Valid signature, but the account is gone.
        raise _UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
