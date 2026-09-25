FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer: only busted when pyproject changes.
COPY backend/pyproject.toml /app/pyproject.toml
RUN uv pip install --system -r /app/pyproject.toml
# Dev image -- the test suite runs inside this container.
RUN uv pip install --system pytest pytest-asyncio

COPY backend /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
