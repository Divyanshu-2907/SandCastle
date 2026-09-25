"""Docker-isolated execution for one agent run.

What actually contains the agent:

- **No network.** `network_mode="none"` gives the container a loopback interface
  and nothing else. The repo is seeded over the Docker API by the worker (which
  does have network), so the sandbox never needs to reach out and never can.
- **Memory, CPU and PID ceilings**, so a fork bomb or a memory hog degrades its
  own container instead of the host.
- **A hard wall-clock deadline** enforced by a watchdog thread that kills the
  container outright, independent of any per-command timeout.
- **Dropped capabilities, no privilege escalation, read-only root filesystem**,
  a non-root user, and writable space only where work actually happens.
- **Guaranteed teardown** in `finally`, plus a reaper for anything a hard crash
  left behind.

The guardrail blocklist in `guardrails.py` still runs before every command. It is
defence in depth now rather than the boundary: the boundary is this file.
"""

import logging
import os
import tarfile
import threading
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agent.backends import WORKSPACE_DIR, DockerBackend

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "sandcastle-sandbox:latest")

# Every object this module creates carries these, so the reaper can find orphans
# without touching anything else on the daemon.
LABEL_MANAGED = "sandcastle.managed"
LABEL_RUN = "sandcastle.run_id"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource ceilings for one run."""

    memory: str = os.getenv("SANDBOX_MEMORY", "512m")
    cpus: float = float(os.getenv("SANDBOX_CPUS", "1.0"))
    pids: int = int(os.getenv("SANDBOX_PIDS", "256"))
    wall_clock_seconds: int = int(os.getenv("SANDBOX_TIMEOUT", "300"))
    tmpfs_size: str = os.getenv("SANDBOX_TMPFS", "64m")

    @property
    def nano_cpus(self) -> int:
        return int(self.cpus * 1_000_000_000)


class SandboxError(Exception):
    """The sandbox could not be created or has been torn down."""


class SandboxTimeout(SandboxError):
    """The run exceeded its wall-clock budget and the container was killed."""


def get_docker_client():
    """Connect to Docker.

    In the stack this resolves to `DOCKER_HOST=tcp://docker-socket-proxy:2375`:
    the worker never sees the real socket. Falls back to the ambient environment
    so the CLI and local test runs still work.
    """
    import docker

    try:
        return docker.from_env()
    except Exception as exc:  # noqa: BLE001 - surfaced as a run failure
        raise SandboxError(f"cannot reach the Docker API: {exc}") from exc


class Sandbox:
    """One locked-down container, with the run's workspace inside it."""

    def __init__(
        self,
        run_id: str | uuid.UUID,
        *,
        client=None,
        image: str = SANDBOX_IMAGE,
        limits: SandboxLimits | None = None,
    ) -> None:
        self.run_id = str(run_id)
        self.client = client or get_docker_client()
        self.image = image
        self.limits = limits or SandboxLimits()

        self.container = None
        self.volume = None
        self._deadline_timer: threading.Timer | None = None
        self._killed_by_watchdog = threading.Event()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "Sandbox":
        labels = {LABEL_MANAGED: "true", LABEL_RUN: self.run_id}
        name = f"sandcastle-run-{self.run_id[:18]}"

        self.volume = self.client.volumes.create(name=f"{name}-ws", labels=labels)

        try:
            self.container = self.client.containers.run(
                self.image,
                name=name,
                detach=True,
                labels=labels,
                # -- isolation --
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user="agent",
                # -- resource ceilings --
                mem_limit=self.limits.memory,
                # Equal to mem_limit so the container cannot escape the cap by
                # swapping; without it the limit is advisory on many hosts.
                memswap_limit=self.limits.memory,
                nano_cpus=self.limits.nano_cpus,
                pids_limit=self.limits.pids,
                # -- filesystem --
                # Only the workspace and a small scratch area are writable, so a
                # compromised process cannot rewrite its own toolchain.
                read_only=True,
                volumes={self.volume.name: {"bind": WORKSPACE_DIR, "mode": "rw"}},
                tmpfs={
                    "/tmp": f"size={self.limits.tmpfs_size},mode=1777",
                    "/home/agent": f"size={self.limits.tmpfs_size},mode=0700,uid=10001,gid=10001",
                },
                working_dir=WORKSPACE_DIR,
                auto_remove=False,  # we remove it ourselves, so failures stay inspectable
            )
        except Exception as exc:  # noqa: BLE001
            self.cleanup()
            raise SandboxError(f"could not start the sandbox container: {exc}") from exc

        self._arm_watchdog()
        logger.info("sandbox %s started for run %s", self.container.short_id, self.run_id)
        return self

    def _arm_watchdog(self) -> None:
        """Kill the container if the run outlives its budget.

        Deliberately independent of per-command timeouts: an agent can stay
        under every individual command limit and still run forever.
        """

        def kill_it() -> None:
            self._killed_by_watchdog.set()
            logger.warning(
                "run %s exceeded %ss; killing sandbox",
                self.run_id,
                self.limits.wall_clock_seconds,
            )
            try:
                self.container.kill()
            except Exception:  # noqa: BLE001 - already gone is a fine outcome
                logger.debug("sandbox for run %s was already gone", self.run_id)

        self._deadline_timer = threading.Timer(self.limits.wall_clock_seconds, kill_it)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    @property
    def timed_out(self) -> bool:
        return self._killed_by_watchdog.is_set()

    def cleanup(self) -> None:
        """Remove the container and volume. Safe to call twice, never raises."""
        if self._deadline_timer is not None:
            self._deadline_timer.cancel()
            self._deadline_timer = None

        if self.container is not None:
            try:
                self.container.remove(force=True, v=True)
                logger.info("removed sandbox container for run %s", self.run_id)
            except Exception:  # noqa: BLE001 - best effort; the reaper is the backstop
                logger.warning("could not remove sandbox container for run %s", self.run_id, exc_info=True)
            self.container = None

        if self.volume is not None:
            try:
                self.volume.remove(force=True)
            except Exception:  # noqa: BLE001
                logger.warning("could not remove sandbox volume for run %s", self.run_id, exc_info=True)
            self.volume = None

    # -- seeding ----------------------------------------------------------

    def seed_from(self, source: Path) -> None:
        """Copy a directory from the worker into the sandbox workspace.

        This is why the sandbox needs no network: the worker clones the repo (it
        is trusted and has egress), then hands the bytes over the Docker API.
        """
        if self.container is None:
            raise SandboxError("sandbox is not running")

        import io

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path in sorted(Path(source).rglob("*")):
                relative = path.relative_to(source)
                info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                info.uid = info.gid = 10001
                info.uname = info.gname = "agent"
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
                else:
                    archive.addfile(info)

        if not self.container.put_archive(WORKSPACE_DIR, buffer.getvalue()):
            raise SandboxError("could not seed the sandbox workspace")

    def backend(self) -> DockerBackend:
        if self.container is None:
            raise SandboxError("sandbox is not running")
        return DockerBackend(self.container)


@contextmanager
def sandbox_for(
    run_id: str | uuid.UUID,
    source: Path | None = None,
    *,
    client=None,
    image: str = SANDBOX_IMAGE,
    limits: SandboxLimits | None = None,
) -> Iterator[Sandbox]:
    """Start a sandbox, optionally seed it, and always tear it down."""
    box = Sandbox(run_id, client=client, image=image, limits=limits)
    try:
        box.start()
        if source is not None:
            box.seed_from(source)
        yield box
    finally:
        box.cleanup()


# -- reaper ---------------------------------------------------------------


def reap_orphans(client=None, max_age_seconds: int | None = None) -> int:
    """Remove sandbox containers and volumes nothing is using any more.

    `finally` covers the ordinary paths; this covers the ones it cannot -- the
    worker being SIGKILLed, the machine losing power mid-run. Called on worker
    startup, so a crashed run cannot leak a container indefinitely.

    Must not touch a sandbox that is legitimately in flight, which a second
    worker starting up would otherwise do. Two rules keep it safe:

    - a container that is no longer running is always reapable;
    - a *running* container is reapable only once it is older than any legitimate
      sandbox could be, since the watchdog kills one at its wall-clock budget.

    Volumes are removed without `force`, so the daemon refuses any that a live
    container still has attached.
    """
    client = client or get_docker_client()
    if max_age_seconds is None:
        # Twice the budget: comfortably past the watchdog, nowhere near a live run.
        max_age_seconds = SandboxLimits().wall_clock_seconds * 2
    removed = 0

    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{LABEL_MANAGED}=true"}
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not list sandbox containers to reap", exc_info=True)
        return 0

    for container in containers:
        alive = container.status == "running"
        if alive and _age_of(container) < max_age_seconds:
            logger.debug("leaving live sandbox %s alone", container.name)
            continue
        try:
            container.remove(force=True, v=True)
            removed += 1
            logger.info("reaped orphaned sandbox container %s", container.name)
        except Exception:  # noqa: BLE001
            logger.warning("could not reap container %s", container.name, exc_info=True)

    try:
        for volume in client.volumes.list(filters={"label": f"{LABEL_MANAGED}=true"}):
            try:
                volume.remove()
                removed += 1
            except Exception:  # noqa: BLE001 - in use by a live run; leave it
                logger.debug("volume %s still in use", volume.name)
    except Exception:  # noqa: BLE001
        logger.warning("could not list sandbox volumes to reap", exc_info=True)

    return removed


def _age_of(container) -> float:
    started = container.attrs.get("Created", "")
    try:
        from datetime import datetime

        created = datetime.fromisoformat(started.replace("Z", "+00:00"))
        return time.time() - created.timestamp()
    except Exception:  # noqa: BLE001 - unknown age counts as old enough
        return float("inf")
