"""Celery application.

Run it with:

    celery -A app.worker.celery_app worker --loglevel=info
"""

from celery import Celery
from celery.signals import worker_ready

from app.core.config import settings

celery_app = Celery(
    "sandcastle",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.jobs"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Agent runs are long and not idempotent: redelivering one on worker loss
    # would double-write steps, so acknowledge up front.
    task_acks_late=False,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=60 * 60 * 24,
    # The sandbox watchdog bounds time spent inside the container. It cannot
    # bound a clone that hangs on a dead remote or an LLM socket that never
    # closes, so Celery holds the outer limit. Soft first, so the task raises
    # SoftTimeLimitExceeded and gets to mark its own run failed; the hard limit
    # is the kill for when even that wedges.
    task_soft_time_limit=settings.task_soft_time_limit,
    task_time_limit=settings.task_time_limit,
    # A run that was never started (broker lost the message) or never finished
    # (worker killed) would otherwise sit in `queued`/`running` forever. Redis
    # is not a durable queue, so this sweep is the only thing that closes those
    # rows out.
    beat_schedule={
        "sweep-stale-runs": {
            "task": "app.jobs.sweep_stale_runs",
            "schedule": float(settings.stale_sweep_interval_seconds),
        },
    },
)


@worker_ready.connect
def _reap_orphaned_sandboxes(**_kwargs) -> None:
    """Clear sandboxes a previous worker died holding.

    `finally` handles the ordinary paths; it cannot handle SIGKILL or a power
    cut. Reaping on startup means a hard crash leaks a container until the next
    restart rather than forever.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from agent.sandbox import reap_orphans

        removed = reap_orphans()
        if removed:
            logger.warning("reaped %d orphaned sandbox object(s) from a previous run", removed)
    except Exception:  # noqa: BLE001 - never block worker startup on cleanup
        logger.warning("sandbox reaping failed at startup", exc_info=True)
