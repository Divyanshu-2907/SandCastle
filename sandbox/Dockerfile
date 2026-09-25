# Execution environment for untrusted, agent-generated code.
#
# Deliberately boring: enough to clone-adjacent work and run a typical repo test
# suite, and nothing else. No credentials, no cloud CLIs, no package registries
# configured -- and at runtime it gets no network at all, so anything not baked
# in here is simply unavailable.
#
#   docker build -t sandcastle-sandbox:latest ./sandbox
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agent \
    NPM_CONFIG_CACHE=/tmp/.npm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        make \
        procps \
        coreutils \
        python3 \
        python3-pip \
        python3-venv \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Common test runners, pre-installed because the sandbox cannot reach PyPI.
RUN pip3 install --break-system-packages --no-cache-dir \
        pytest \
        pytest-cov

RUN python3 -m pip --version && node --version && git --version

# Agent code never runs as root. The uid is fixed so volume ownership is
# predictable when the worker seeds the workspace.
RUN useradd --create-home --uid 10001 --shell /bin/bash agent \
    && mkdir -p /workspace /opt/sandcastle \
    && chown -R agent:agent /workspace /home/agent /opt/sandcastle

# `/opt/sandcastle` is agent-owned but sits on the root filesystem, so it is
# writable only if the read-only root mount is missing. It exists purely to make
# that control testable: without it, a read-only check against a root-owned path
# would pass on file permissions alone and prove nothing.

USER agent
WORKDIR /workspace

# The container is kept alive by the worker and driven with exec; it must not
# exit on its own or the run loses its workspace mid-flight.
CMD ["sleep", "infinity"]
