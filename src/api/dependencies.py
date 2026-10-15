"""JWT authentication dependency for FastAPI routes."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

from src.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)


class User:
    """Lightweight user representation after JWT validation."""

    def __init__(self, username: str, user_id: str, roles: list[str] | None = None):
        self.username = username
        self.user_id = user_id
        self.roles: list[str] = roles or ["user"]


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str, extras: dict | None = None) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expires, "type": "access"}
    if extras:
        payload.update(extras)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """
    Extract and validate the JWT Bearer token.
    Returns a User object on success; raises 401 on failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    sub: str = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    return User(username=sub, user_id=sub, roles=payload.get("roles", []))


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the user has at least 'user' role."""
    if "user" not in current_user.roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return current_user


def require_role(*roles: str):
    """Decorator-like dependency factory that checks one of the given roles."""
    async def _check(user: User = Depends(get_current_active_user)) -> User:
        if not any(r in user.roles for r in roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return user
    return _check


# ---- Mock user store (replace with DB lookup in production) ----

MOCK_USERS: dict[str, dict[str, str]] = {
    "admin": {"hashed_password": hash_password("admin123"), "roles": ["admin", "user"]},
    "operator": {"hashed_password": hash_password("operator123"), "roles": ["user"]},
}


async def authenticate_user(username: str, password: str) -> Optional[User]:
    """Mock auth: look up username in MOCK_USERS and verify password."""
    record = MOCK_USERS.get(username)
    if not record:
        return None
    if not verify_password(password, record["hashed_password"]):
        return None
    return User(username=username, user_id=username, roles=record["roles"])
