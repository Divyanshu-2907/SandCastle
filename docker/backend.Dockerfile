FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer: only busted when pyproject or the lockfile changes.
#
# Installed from uv.lock, not from pyproject's ranges. The ranges are lower
# bounds with no ceiling, so resolving them at build time meant an unrelated
# rebuild could move a dependency across a major version -- which is how
# google-genai went 1.x -> 3.x and broke function calling on a real run. The
# lock is exported rather than `uv sync`'d so packages stay installed
# system-wide and nothing has to know about a virtualenv.
COPY backend/pyproject.toml backend/uv.lock /app/
# No --no-dev here: this is the image the test suite runs in, so the dev group
# (pytest, pytest-asyncio) is wanted.
RUN uv export --frozen --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY backend /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
