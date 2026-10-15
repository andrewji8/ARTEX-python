"""API routes package."""

from src.api.routes.tasks import router as tasks_router
from src.api.routes.findings import router as findings_router
from src.api.routes.dashboard import router as dashboard_router

__all__ = ["tasks_router", "findings_router", "dashboard_router"]
