import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import auth, runs, tasks, ws
from app.core.config import settings
from app.db.session import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Evaluation and sandboxing platform for AI coding agents.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check(fn) -> str:
    try:
        fn()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - health check reports, never raises
        logger.warning("health check failed: %s", exc)
        return "error"


def _ping_db() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def _ping_redis() -> None:
    import redis

    redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()


@app.get("/health")
def health() -> dict:
    """Liveness + dependency check. Always 200 so the probe itself can't flap."""
    deps = {"database": _check(_ping_db), "redis": _check(_ping_redis)}
    return {
        "status": "healthy" if all(v == "ok" for v in deps.values()) else "degraded",
        "service": settings.app_name,
        "version": "0.1.0",
        "dependencies": deps,
    }


app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(runs.router)
app.include_router(ws.router)
