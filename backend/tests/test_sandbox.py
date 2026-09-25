"""Does the sandbox actually contain anything?

These tests try to break out rather than confirm the happy path: escape the
filesystem, reach the network, exhaust memory, fork-bomb the host, outlive the
deadline, and leak containers on a crash. No LLM is involved -- the sandbox is
driven directly, so the whole file runs for free and as often as needed.

Run them where a Docker endpoint is reachable (the worker container):

    docker compose exec worker python -m pytest tests/test_sandbox.py -v
"""

import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import FileNotFound, WORKSPACE_DIR  # noqa: E402
from agent.guardrails import PathOutsideWorkspace  # noqa: E402
from agent.sandbox import (  # noqa: E402
    LABEL_MANAGED,
    LABEL_RUN,
    Sandbox,
    SandboxLimits,
    get_docker_client,
    reap_orphans,
    sandbox_for,
)

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="session")
def docker_client():
    try:
        client = get_docker_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Docker endpoint available: {exc}")
    if not client.images.list(name="sandcastle-sandbox:latest"):
        pytest.skip("sandcastle-sandbox:latest is not built")
    return client


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small git repo to seed a sandbox with."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Demo\n\nHello.\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)

    git("init", "--quiet")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "--quiet", "-m", "initial")
    return workspace


@pytest.fixture
def box(docker_client, repo: Path):
    """A started, seeded sandbox that is always torn down."""
    with sandbox_for(uuid.uuid4(), source=repo, client=docker_client) as sandbox:
        yield sandbox


# -- it works at all ------------------------------------------------------


def test_sandbox_runs_commands(box) -> None:
    outcome = box.backend().run("echo hello")
    assert outcome.exit_code == 0
    assert "hello" in outcome.stdout


def test_seeded_repo_is_present_and_is_a_git_checkout(box) -> None:
    backend = box.backend()
    assert "README.md" in backend.read_file("README.md") or True
    assert backend.read_file("README.md").startswith("# Demo")
    assert backend.run("git status --short").exit_code == 0


def test_toolchain_is_available(box) -> None:
    backend = box.backend()
    for command in ["git --version", "python3 --version", "node --version", "pytest --version"]:
        assert backend.run(command).exit_code == 0, command


# -- network --------------------------------------------------------------


def test_network_is_unavailable(box) -> None:
    """The sandbox has no route off the box, so exfiltration has nowhere to go."""
    backend = box.backend()

    # Bypass the blocklist deliberately: this asserts the *boundary*, not the
    # guardrail. Even a command the blocklist would have allowed cannot reach out.
    outcome = backend.run(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=5)\"",
        timeout=20,
    )
    assert outcome.exit_code != 0
    assert "unreachable" in outcome.stderr.lower() or "error" in outcome.stderr.lower()


def test_dns_resolution_fails(box) -> None:
    outcome = box.backend().run(
        "python3 -c \"import socket; print(socket.gethostbyname('example.com'))\"", timeout=20
    )
    assert outcome.exit_code != 0


def test_only_loopback_interface_exists(box) -> None:
    outcome = box.backend().run("cat /proc/net/dev")
    interfaces = {
        line.split(":")[0].strip()
        for line in outcome.stdout.splitlines()
        if ":" in line
    }
    assert interfaces <= {"lo"}, f"unexpected interfaces: {interfaces}"


# -- resource limits ------------------------------------------------------


def test_memory_limit_kills_a_hog(docker_client, repo: Path) -> None:
    limits = SandboxLimits(memory="128m", cpus=1.0, pids=128, wall_clock_seconds=120)
    with sandbox_for(uuid.uuid4(), source=repo, client=docker_client, limits=limits) as box:
        # Touch every page so the allocation is real, not just reserved.
        outcome = box.backend().run(
            "python3 -c \"b = bytearray(400*1024*1024); b[::4096] = b'x'*len(b[::4096])\"",
            timeout=60,
        )
        assert outcome.exit_code != 0, "a 400MB allocation should not survive a 128m cap"
        # The container itself must still be alive and usable afterwards.
        assert box.backend().run("echo still-here").stdout.strip() == "still-here"


def test_pid_limit_contains_a_fork_bomb(docker_client, repo: Path) -> None:
    """A classic fork bomb, contained by the cgroup PID cap.

    Safe to run: the kernel refuses the 33rd process in this container, so the
    bomb starves immediately instead of reaching the host.
    """
    limits = SandboxLimits(memory="128m", cpus=0.5, pids=32, wall_clock_seconds=120)
    with sandbox_for(uuid.uuid4(), source=repo, client=docker_client, limits=limits) as box:
        started = time.monotonic()
        outcome = box.backend().run(":(){ :|:& };:", timeout=10)
        elapsed = time.monotonic() - started

        assert elapsed < 40, "the fork bomb was not contained promptly"
        assert outcome.exit_code != 0 or outcome.timed_out

        # The host's Docker daemon is still responsive, which is the thing that
        # would not be true if the bomb had escaped.
        assert docker_client.ping()


def test_cpu_limit_is_applied(box) -> None:
    info = box.container.attrs["HostConfig"]
    assert info["NanoCpus"] == box.limits.nano_cpus
    assert info["PidsLimit"] == box.limits.pids
    assert info["Memory"] == 512 * 1024 * 1024
    # Swap capped at the same value, or the memory limit is escapable.
    assert info["MemorySwap"] == info["Memory"]


# -- filesystem -----------------------------------------------------------


def test_root_filesystem_is_read_only(box) -> None:
    backend = box.backend()

    # `/opt/sandcastle` is owned by the agent user, so this fails only because
    # the root mount is read-only. Probing a root-owned path like /usr/bin would
    # pass on file permissions alone and prove nothing about the mount.
    assert backend.run("touch /opt/sandcastle/backdoor").exit_code != 0
    assert box.container.attrs["HostConfig"]["ReadonlyRootfs"] is True

    # Root-owned paths are unwritable too, for the ordinary reason.
    assert backend.run("touch /usr/bin/backdoor").exit_code != 0

    # ...but the workspace and scratch space are writable, or the agent could
    # not do any work.
    assert backend.run("touch scratch.txt").exit_code == 0
    assert backend.run("touch /tmp/scratch").exit_code == 0


def test_agent_is_not_root(box) -> None:
    # Two independent controls, because either alone can pass while the other is
    # broken: the container is *configured* non-root, and the exec path pins the
    # user regardless of how the container was configured.
    assert box.container.attrs["Config"]["User"] == "agent"

    outcome = box.backend().run("id -u")
    assert outcome.stdout.strip() == "10001"

    raw = box.container.exec_run(["id", "-u"])  # no user= override
    assert raw.output.decode().strip() == "10001"


def test_privilege_escalation_is_blocked(box) -> None:
    info = box.container.attrs["HostConfig"]
    assert "no-new-privileges" in info["SecurityOpt"]
    assert info["CapDrop"] == ["ALL"]
    # sudo/su are not installed, and cannot be installed without a network.
    assert box.backend().run("sudo -n true").exit_code != 0


def test_rm_rf_cannot_reach_the_host(box, tmp_path: Path) -> None:
    """The canonical disaster, aimed at the container's own root."""
    canary = tmp_path / "canary.txt"
    canary.write_text("host file, must survive", encoding="utf-8")

    backend = box.backend()
    backend.run("rm -rf / --no-preserve-root 2>/dev/null; true", timeout=60)

    # The host's file is untouched...
    assert canary.read_text(encoding="utf-8") == "host file, must survive"
    # ...and so is this test process's own filesystem.
    assert Path(__file__).is_file()


def test_host_root_is_not_mounted(box) -> None:
    backend = box.backend()
    mounts = backend.run("cat /proc/mounts").stdout
    assert "/var/run/docker.sock" not in mounts
    # The workspace volume is the only bind the agent can see.
    assert WORKSPACE_DIR in mounts


def test_no_docker_socket_inside_the_sandbox(box) -> None:
    """The agent must not be able to ask Docker for anything at all."""
    backend = box.backend()
    assert backend.run("test -S /var/run/docker.sock").exit_code != 0
    assert backend.run("which docker").exit_code != 0


def test_path_traversal_is_rejected(box) -> None:
    backend = box.backend()
    for path in ["../../etc/passwd", "/etc/passwd", "src/../../../escape"]:
        with pytest.raises(PathOutsideWorkspace):
            backend.read_file(path)


# -- timeouts -------------------------------------------------------------


def test_per_command_timeout_kills_the_command(box) -> None:
    started = time.monotonic()
    outcome = box.backend().run("sleep 60", timeout=3)
    elapsed = time.monotonic() - started

    assert outcome.timed_out
    assert elapsed < 30, "the command outlived its timeout"
    # The sandbox survives a single slow command.
    assert box.backend().run("echo alive").stdout.strip() == "alive"


def test_wall_clock_watchdog_kills_the_container(docker_client, repo: Path) -> None:
    """An agent can stay under every per-command limit and still run forever."""
    limits = SandboxLimits(memory="128m", cpus=0.5, pids=64, wall_clock_seconds=5)
    with sandbox_for(uuid.uuid4(), source=repo, client=docker_client, limits=limits) as box:
        # Well under the per-command timeout, well over the wall-clock budget.
        for _ in range(10):
            box.backend().run("sleep 1", timeout=30)
            if box.timed_out:
                break

        assert box.timed_out, "the watchdog never fired"
        box.container.reload()
        assert box.container.status != "running"


# -- cleanup --------------------------------------------------------------


def test_container_and_volume_are_removed_on_exit(docker_client, repo: Path) -> None:
    with sandbox_for(uuid.uuid4(), source=repo, client=docker_client) as box:
        container_id = box.container.id
        volume_name = box.volume.name
        assert docker_client.containers.get(container_id).status == "running"

    import docker.errors

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(container_id)
    with pytest.raises(docker.errors.NotFound):
        docker_client.volumes.get(volume_name)


def test_cleanup_happens_even_when_the_body_raises(docker_client, repo: Path) -> None:
    captured = {}

    with pytest.raises(RuntimeError, match="boom"):
        with sandbox_for(uuid.uuid4(), source=repo, client=docker_client) as box:
            captured["container"] = box.container.id
            captured["volume"] = box.volume.name
            raise RuntimeError("boom")

    import docker.errors

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(captured["container"])
    with pytest.raises(docker.errors.NotFound):
        docker_client.volumes.get(captured["volume"])


def test_cleanup_is_idempotent(docker_client, repo: Path) -> None:
    box = Sandbox(uuid.uuid4(), client=docker_client).start()
    box.cleanup()
    box.cleanup()  # must not raise


def _abandon(client) -> tuple[str, str]:
    """Start a sandbox and drop the handles, exactly as a SIGKILL would."""
    orphan = Sandbox(uuid.uuid4(), client=client).start()
    container_id, volume_name = orphan.container.id, orphan.volume.name
    orphan._deadline_timer.cancel()
    orphan.container = None
    orphan.volume = None
    return container_id, volume_name


def test_reaper_removes_a_dead_abandoned_sandbox(docker_client) -> None:
    """A stopped orphan is always reapable: nothing can still be using it."""
    container_id, volume_name = _abandon(docker_client)
    docker_client.containers.get(container_id).kill()

    assert reap_orphans(client=docker_client) >= 1

    import docker.errors

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(container_id)
    with pytest.raises(docker.errors.NotFound):
        docker_client.volumes.get(volume_name)


def test_reaper_leaves_a_live_sandbox_alone(docker_client) -> None:
    """A second worker starting must not destroy the first worker's live run.

    This is the failure mode the age check exists for: without it, `reap_orphans`
    on startup would kill every sandbox currently doing work.
    """
    container_id, volume_name = _abandon(docker_client)
    try:
        reap_orphans(client=docker_client)

        # Still running, and its volume survived with it.
        assert docker_client.containers.get(container_id).status == "running"
        assert docker_client.volumes.get(volume_name) is not None
    finally:
        docker_client.containers.get(container_id).remove(force=True, v=True)
        try:
            docker_client.volumes.get(volume_name).remove(force=True)
        except Exception:  # noqa: BLE001 - already gone with the container
            pass


def test_reaper_removes_a_live_sandbox_that_outlived_any_budget(docker_client) -> None:
    """Past the wall-clock budget, a still-running sandbox cannot be legitimate."""
    container_id, _ = _abandon(docker_client)

    # max_age_seconds=0 stands in for "older than any real run could be".
    assert reap_orphans(client=docker_client, max_age_seconds=0) >= 1

    import docker.errors

    with pytest.raises(docker.errors.NotFound):
        docker_client.containers.get(container_id)


def test_sandbox_objects_are_labelled_for_the_reaper(box) -> None:
    labels = box.container.labels
    assert labels[LABEL_MANAGED] == "true"
    assert labels[LABEL_RUN] == box.run_id


# -- file operations through the Docker API -------------------------------


def test_write_read_round_trip(box) -> None:
    backend = box.backend()
    assert backend.write_file("notes/todo.md", "line one\nline two\n") is False
    assert backend.read_file("notes/todo.md") == "line one\nline two\n"
    # Second write reports that it replaced an existing file.
    assert backend.write_file("notes/todo.md", "changed\n") is True
    assert backend.read_file("notes/todo.md") == "changed\n"


def test_written_files_are_owned_by_the_agent_user(box) -> None:
    backend = box.backend()
    backend.write_file("owned.txt", "x\n")
    assert backend.run("stat -c %u owned.txt").stdout.strip() == "10001"


def test_reading_a_missing_file_raises(box) -> None:
    with pytest.raises(FileNotFound):
        box.backend().read_file("nope.txt")


def test_listing_hides_git_internals(box) -> None:
    entries = box.backend().list_dir(".", recursive=True)
    assert "README.md" in entries
    assert "src/app.py" in entries
    assert not any(e.startswith(".git") for e in entries)


def test_listing_a_missing_directory_raises(box) -> None:
    with pytest.raises(FileNotFound):
        box.backend().list_dir("does-not-exist")


def test_filenames_with_shell_metacharacters_are_safe(box) -> None:
    """Paths must not be reinterpreted as shell syntax."""
    backend = box.backend()
    tricky = "weird; touch pwned.txt #.md"
    backend.write_file(tricky, "content\n")

    assert backend.read_file(tricky) == "content\n"
    assert backend.run("test -f pwned.txt").exit_code != 0, "filename was executed as shell"


# -- diffs ----------------------------------------------------------------


def test_diff_collection_works_inside_the_sandbox(box) -> None:
    from agent.workspace import collect_diff_via, collect_file_diffs_via, prepare_git_identity

    backend = box.backend()
    prepare_git_identity(backend)
    backend.write_file("README.md", "# Demo\n\nChanged.\n")
    backend.write_file("NEW.md", "brand new\n")

    diff = collect_diff_via(backend)
    assert "README.md" in diff
    assert "NEW.md" in diff
    assert "+Changed." in diff

    per_file = {d.file_path for d in collect_file_diffs_via(backend)}
    assert per_file == {"README.md", "NEW.md"}


def test_diff_is_empty_when_nothing_changed(box) -> None:
    from agent.workspace import collect_diff_via, prepare_git_identity

    backend = box.backend()
    prepare_git_identity(backend)
    assert collect_diff_via(backend).strip() == ""
