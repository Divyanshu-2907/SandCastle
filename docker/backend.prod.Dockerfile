# Production image for the API (Cloud Run) and the worker (GCE).
#
# One image, two entrypoints: Cloud Run overrides nothing and serves on $PORT,
# the worker VM overrides the command with `celery ... worker`. Keeping them
# identical means the code that enqueues a task and the code that runs it can
# never drift apart in a deploy.
#
# Differs from backend.Dockerfile (dev) in three ways that matter in production:
# no --reload, no test dependencies, and it runs as a non-root user.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg2, git because the worker clones task repos itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

COPY backend/pyproject.toml /app/pyproject.toml
RUN uv pip install --system -r /app/pyproject.toml

COPY backend /app

# The API has no reason to run privileged. The worker talks to Docker over TCP
# (the socket proxy), not a bind-mounted socket, so it does not need root either.
RUN useradd --create-home --uid 10001 sandcastle \
    && chown -R sandcastle:sandcastle /app
USER sandcastle

# Cloud Run injects PORT and ignores EXPOSE; the default keeps `docker run`
# working the same way locally.
ENV PORT=8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
