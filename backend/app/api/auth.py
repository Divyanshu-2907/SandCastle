"""Registration and login."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models import User
from app.schemas import Token, UserCreate, UserLogin, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue(user: User) -> Token:
    return Token(
        access_token=create_access_token(user.id),
        expires_in=settings.jwt_expire_minutes * 60,
        user=UserRead.model_validate(user),
    )


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: DbSession) -> Token:
    email = payload.email.lower()
    existing = db.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="That email is already registered"
        )

    user = User(email=email, hashed_password=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return _issue(user)


@router.post("/login", response_model=Token)
def login(payload: UserLogin, db: DbSession) -> Token:
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))

    # Same response whether the email is unknown or the password is wrong, so the
    # endpoint can't be used to enumerate accounts.
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _issue(user)


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> User:
    return user
