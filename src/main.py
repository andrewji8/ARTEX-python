"""FastAPI application — ARTEX Penetration Testing Platform."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncGenerator, Dict
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from src.api.dependencies import create_access_token, User, get_current_active_user
from src.api.stream_manager import ConnectionManager, get_connection_manager
from src.api.routes import tasks_router
from src.api.routes.findings import router as findings_router
from src.api.routes.dashboard import router as dashboard_router
from src.api.routes.traffic import router as traffic_router
from src.api.shutdown import install_signal_handlers, register_engine, unregister_engine
from src.config import settings
from src.core.engine import Engine
from src.core.event_bus import EventBus
from src.tools.real_tools import ToolExecutor
from src.db.repository import AsyncGraphRepository
from src.db.session import session_factory

logging.basicConfig(
    level=logging.INFO if settings.debug else logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

_engine: Engine | None = None
_event_bus: EventBus | None = None
_connection_manager: ConnectionManager | None = None

# Active engines keyed by task_id, plus their owned DB sessions for cleanup.
_engines: Dict[str, Engine] = {}
_engine_sessions: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _event_bus, _connection_manager

    # Initialize components
    _event_bus = EventBus()
    _connection_manager = get_connection_manager()
    await _event_bus.start()

    logger.info("ARTEX API started (debug=%s)", settings.debug)

    yield

    # Cleanup
    if _event_bus:
        await _event_bus.stop()
    logger.info("ARTEX API stopped")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    description="Autonomous AI Penetration Testing Platform",
    version="0.3.0",
    lifespan=lifespan,
)

# CORS — restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                          "http://localhost:5173", "http://127.0.0.1:5173",
                          "http://localhost:5500", "http://127.0.0.1:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inject repo + manager into request state for route handlers
@app.middleware("http")
async def inject_state(request: Request, call_next):
    from contextlib import asynccontextmanager
    async with session_factory() as db_session:
        request.state.repo = AsyncGraphRepository(db_session)
        request.state.connection_manager = _connection_manager or get_connection_manager()
        try:
            response = await call_next(request)
        except Exception:
            await db_session.rollback()
            raise
        else:
            await db_session.commit()
            return response


@app.on_event("shutdown")
async def _on_shutdown():
    """Uvicorn graceful-shutdown hook: flush planner state, stop engines, close DB."""
    logger.info("Shutdown signal received — flushing state and stopping engines...")
    from src.api.shutdown import shutdown_all_engines
    await shutdown_all_engines(timeout=30.0)
    if _event_bus:
        await _event_bus.stop()
    try:
        from src.db.session import close_db
        await close_db()
    except Exception:
        logger.exception("Error during DB shutdown")
    logger.info("ARTEX API shutdown complete")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(tasks_router)
app.include_router(findings_router)
app.include_router(dashboard_router)
app.include_router(traffic_router)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(payload: LoginRequest):
    """Login endpoint — accepts JSON {username, password}, returns JWT token."""
    from src.api.dependencies import authenticate_user, create_access_token
    user = await authenticate_user(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(subject=user.username, extras={"roles": user.roles})
    return {"access_token": token, "token_type": "bearer", "username": user.username}


@app.get("/api/auth/me")
async def me(
    user: User = Depends(get_current_active_user),
):
    """Return current user info."""
    return {"username": user.username, "roles": user.roles}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """Health check endpoint."""
    manager = get_connection_manager()
    stats = await manager.get_stats()
    return {
        "status": "ok",
        "version": "0.3.0",
        "sse_connections": stats["total_connections"],
        "active_tasks": stats["active_tasks"],
    }


@app.get("/")
async def root():
    return {
        "name": settings.app_name,
        "version": "0.3.0",
        "docs": "/docs",
        "endpoints": {
            "login": "POST /api/auth/login",
            "tasks": "/api/tasks",
            "stream": "/api/tasks/{task_id}/stream",
            "health": "/api/health",
            "drain": "POST /api/drain",
        },
    }


# ---------------------------------------------------------------------------
# Drain endpoint (called by supervisor during zero-downtime updates)
# ---------------------------------------------------------------------------

@app.post("/api/drain")
async def drain_connections(
    user: User = Depends(get_current_active_user),
):
    """
    Signal to begin draining: stop accepting new connections,
    wait for active SSE streams to close, then report active_tasks=0.
    """
    manager = get_connection_manager()
    stats = await manager.get_stats()
    logger.info("Drain requested — current active_tasks=%s", stats.get("active_tasks", 0))
    # The manager stops accepting new connections immediately.
    # Existing connections will close naturally as clients reconnect
    # to the new container. We return immediately and let supervisor
    # poll /api/health until active_tasks reaches 0.
    return {
        "status": "draining",
        "active_tasks": stats.get("active_tasks", 0),
        "sse_connections": stats.get("total_connections", 0),
        "message": "Accepting no new connections; existing streams closing naturally",
    }


# ---------------------------------------------------------------------------
# Engine management (dev/op convenience)
# ---------------------------------------------------------------------------

@app.post("/api/engine/{task_id}/start")
async def start_engine(
    task_id: UUID,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Start the event-driven engine for a task (idempotent)."""
    key = str(task_id)
    if key in _engines:
        return {"task_id": key, "status": "already_running"}

    # Own a dedicated DB session for the engine's lifetime.
    session = session_factory()
    repo = AsyncGraphRepository(session)

    task = await repo.get_task(task_id)
    if not task:
        await session.close()
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # Route B: real tool execution (built-in http_probe + external CLI if installed)
    tool_executor = ToolExecutor()
    engine = Engine(
        task_id=task_id,
        repo=repo,
        event_bus=_event_bus or EventBus(),
        num_workers=4,
        debounce_delay=2.0,
        llm_pool=None,  # optional LLM decision layer; rule-based when None
        tool_executor=tool_executor,
    )
    await engine.start()
    register_engine(key, engine)
    _engines[key] = engine
    _engine_sessions[key] = session
    logger.info("Engine started for task_id=%s", task_id)
    return {"task_id": key, "status": "running"}


@app.post("/api/engine/{task_id}/stop")
async def stop_engine(
    task_id: UUID,
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Stop the event-driven engine for a task and release its DB session."""
    key = str(task_id)
    engine = _engines.pop(key, None)
    if engine is None:
        raise HTTPException(status_code=404, detail=f"No running engine for task {task_id}")

    try:
        await engine.stop()
    finally:
        unregister_engine(key)
        session = _engine_sessions.pop(key, None)
        if session is not None:
            await session.close()

    logger.info("Engine stopped for task_id=%s", task_id)
    return {"task_id": key, "status": "stopped"}


@app.get("/api/engine")
async def list_engines(
    user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """List all running engines (task_id -> cycle_count)."""
    return {
        "engines": [
            {"task_id": key, "cycle_count": eng.cycle_count}
            for key, eng in _engines.items()
        ],
        "count": len(_engines),
    }


# ---------------------------------------------------------------------------
# SSE broadcast helper (called by engine events in production)
# ---------------------------------------------------------------------------

async def broadcast_to_task(task_id: UUID, data: dict, *, event_type: str = "activity") -> int:
    """
    Public helper: broadcast an event to all SSE subscribers for a task.
    Called by the Engine after producing new activities/nodes/anchors.
    """
    manager = get_connection_manager()
    return await manager.broadcast(task_id, data, event_type=event_type)
