"""API package."""

from src.api.dependencies import (
    authenticate_user,
    create_access_token,
    decode_access_token,
    get_current_active_user,
    get_current_user,
    hash_password,
    verify_password,
)
from src.api.stream_manager import (
    ConnectionManager,
    get_connection_manager,
    reset_connection_manager,
)
from src.api.routes.tasks import router as tasks_router

__all__ = [
    # Auth
    "get_current_user",
    "get_current_active_user",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "verify_password",
    "authenticate_user",
    # SSE
    "ConnectionManager",
    "get_connection_manager",
    "reset_connection_manager",
    # Routes
    "tasks_router",
]
