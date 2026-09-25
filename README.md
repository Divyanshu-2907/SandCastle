# Sandcastle

An evaluation and sandboxing platform for AI coding agents: submit a repo + task,
watch an agent work it live, and score the result on tests, efficiency, and code quality.

> **Status:** Phase 6 — full UI: submit a task, watch it run live. See [Roadmap](#roadmap).

## Stack

| Layer     | Choice                                        |
|-----------|-----------------------------------------------|
| Backend   | FastAPI (Python 3.11), SQLAlchemy 2, Alembic  |
| Agent LLM | Google Gemini (default) or Anthropic Claude    |
| Database  | Postgres 16 + pgvector                        |
| Queue     | Redis + Celery                                 |
| Streaming | Redis pub/sub -> WebSocket                     |
| Auth      | JWT bearer tokens (bcrypt password hashing)    |
| Frontend  | React 18 + Vite + TypeScript + React Router    |
| Isolation | Docker container per run, via a socket proxy    |

## Layout

```
backend/     FastAPI app (app/) + agent loop (agent/) + Celery jobs + migrations
frontend/    Vite + React + TypeScript SPA
docker/      Dockerfiles and Postgres init SQL for the dev stack
sandbox/     Image the agent's code actually executes in
docker-compose.yml
```

`docker-compose.yml` lives at the repo root so plain `docker compose up` works;
the Dockerfiles it references are in `docker/`.

## Running it

Prerequisites: Docker Desktop (or Docker Engine + Compose v2).

```bash
cp .env.example .env                              # defaults work as-is for local dev
docker build -t sandcastle-sandbox:latest ./sandbox   # the agent's execution image
docker compose up --build
```

The sandbox image is built separately, not by compose: it is the environment
untrusted code runs in, so it has its own lifecycle and is not rebuilt whenever
an application service changes.

| Service  | URL                          |
|----------|------------------------------|
| Frontend | http://localhost:5173        |
| Backend  | http://localhost:8000        |
| Health   | http://localhost:8000/health |
| API docs | http://localhost:8000/docs   |

The frontend should show **backend healthy** with `database` and `redis` both `ok`.
A `worker` service (Celery) comes up alongside them and picks up queued runs.

> **Note:** the backend reloads on code changes; the Celery worker does not.
> After editing anything under `backend/agent/` or `backend/app/jobs.py`, run
> `docker compose restart worker` or the worker keeps executing the old code.

Tear down (add `-v` to also drop the Postgres volume):

```bash
docker compose down
```

### Backend on the host

```bash
cd backend
uv venv && uv pip install -r pyproject.toml
uvicorn app.main:app --reload
```

`DATABASE_URL` / `REDIS_URL` in `.env` already point at `localhost`, so this works
against the compose-managed Postgres and Redis.

### Migrations

```bash
cd backend
alembic revision --autogenerate -m "message"
alembic upgrade head
```

Inside the stack: `docker compose exec backend alembic upgrade head`.

### Seed data

```bash
docker compose exec backend python scripts/seed.py
```

Idempotent. Creates `demo@sandcastle.dev` / `sandcastle` plus one task and one
queued run — handy for signing into the UI with something already in the list.

### Data model

```
users ──┬─< tasks ──┬─< runs ──┬─< steps
        │           │          ├─< diffs
        │           │          └── scores (1:1)
        │           └── task_embeddings (1:1, vector(1536))
```

UUID primary keys throughout, `ON DELETE CASCADE` down every edge, and a
`(run_id, step_number)` unique constraint so streamed steps stay ordered.

## Configuration

Every setting is an env var; see [.env.example](.env.example) for the full list with
defaults. Notable ones:

- `DATABASE_URL`, `REDIS_URL` — connection strings
- `JWT_SECRET` — **change this outside local dev**
- `GEMINI_API_KEY` — required from Phase 2 on ([free key](https://aistudio.google.com/apikey))
- `LLM_PROVIDER` / `AGENT_MODEL` — `gemini` + `gemini-2.5-flash` by default



## API

Interactive docs at http://localhost:8000/docs. Everything except `/health` and
the two auth endpoints needs an `Authorization: Bearer <token>` header.

| Method | Path                  | Purpose                                      |
|--------|-----------------------|----------------------------------------------|
| POST   | `/auth/register`      | Create an account, returns a token           |
| POST   | `/auth/login`         | Exchange credentials for a token             |
| GET    | `/auth/me`            | The current user                             |
| POST   | `/tasks`              | Create a task (repo URL + description)       |
| GET    | `/tasks`              | List your tasks                              |
| POST   | `/tasks/{id}/runs`    | Queue an agent run; returns immediately      |
| GET    | `/runs`               | Run history, filterable by `?status=`        |
| GET    | `/runs/{id}`          | Status plus every step and diff so far       |
| WS     | `/ws/runs/{id}`       | Live step stream (see below)                 |

Runs execute on a Celery worker, not in the request. `POST /tasks/{id}/runs`
returns `202` with a run id, and the worker moves it
`queued -> running -> completed | failed`.

**Steps are committed one at a time, as the agent emits them** -- so polling
`GET /runs/{id}` shows work in progress rather than nothing-then-everything, and
a run that dies keeps whatever it had already done. Each committed step is also
published to Redis, which is what the WebSocket below tails.

Everything is scoped to the authenticated user. Another user's task or run
returns `404`, not `403` -- a `403` would confirm the id exists.


### Live streaming

```
ws://localhost:8000/ws/runs/{run_id}?token=<jwt>
```

One JSON object per frame:

```json
{"event": "step",   "data": { "step_number": 3, "type": "action", "tool_name": "read_file", ... }}
{"event": "status", "data": { "status": "running" }}
{"event": "ping",   "data": {}}
```

`ping` is a keepalive; ignore it. The server closes the socket once a terminal
status is sent.

Browsers cannot set headers on a WebSocket handshake, so the token goes in the
query string. An `Authorization: Bearer` header works too, for CLI clients.

**Connecting mid-run is safe.** The server subscribes to Redis *before* reading
the backlog out of Postgres, replays every step already recorded, then tails live
updates and drops anything the replay already covered. Doing it the other way
round would silently lose every step emitted between the read and the subscribe.

Postgres stays the source of truth; the stream is advisory. A client that misses
a frame can always re-read `GET /runs/{id}`, and a broken Redis degrades the
stream without failing the run.

Try it:

```bash
docker compose exec backend python scripts/watch_run.py
```

It queues a run and prints each event with its arrival time — the timestamps are
the point:

```
  +  0.10s  STATUS -> running
  +  5.39s  step  1  action       list_files
  +  9.97s  step  3  action       read_file
  + 16.25s  step  5  action       write_file
  + 27.89s  STATUS -> completed
```

### Smoke test

```bash
docker compose exec backend python scripts/e2e.py
```

Registers a throwaway user, creates a task, queues a run, and polls until it
settles, printing each step and the final diff.

## The agent

The loop lives in [backend/agent/](backend/agent/) and runs standalone -- no database,
no queue, no API:

```bash
docker compose exec backend python run_agent.py \
  https://github.com/octocat/Hello-World.git "add a build-status badge to the README"
```

Add `--json` for machine-readable output, `--keep` to leave the workspace on disk,
`--local` to point at a directory instead of a URL.

**Tools:** `read_file`, `write_file`, `list_files`, `run_command`.

**Guardrails** ([guardrails.py](backend/agent/guardrails.py)):

- `run_command` is checked against a blocklist -- recursive deletes, `sudo`,
  outbound `curl`/`wget`, remote shells, device writes, `git push`, fork bombs.
  `git clone`/`fetch`/`pull` are exempted from the network rules.
- File tools resolve every path inside the workspace and refuse `..`, absolute
  paths, and symlinks pointing out.
- `max_steps` (default 15) caps model turns, so a looping agent terminates.

This is a blocklist, and blocklists leak. It catches the realistic failure mode
-- a confused model running something destructive -- and gives the model a clear
rejection it can respond to. It is not the security boundary: that is the
container described under [Sandboxing](#sandboxing). Defence in depth, with the
blocklist as the outer, fallible layer.

**Providers.** `LLM_PROVIDER=gemini` (default) or `anthropic`. Both sit behind one
interface in [llm.py](backend/agent/llm.py), so the loop never knows which is in
use. Set `AGENT_MODEL` to pick the model.

**Free-tier quotas.** Gemini meters the free tier per minute *and* per day, and
both limits are **per model**. The daily limit is the binding one: several flash
models allow just **20 requests/day**, and one agent run costs 4-6 -- so roughly
three runs per model per day. Budget accordingly, and expect to rotate
`AGENT_MODEL` across models rather than raise a single one. Two mitigations are
built in:

- `GEMINI_RPM` (default 10) paces requests so the per-minute limit is never hit.
- Retries read the server's own `retryDelay` rather than relying on exponential
  backoff, which tops out well below a minute-long quota window.

If a run dies on quota, the error says which limit was hit and what to do about
it. Switching `AGENT_MODEL` to a different model is the quickest fix, since the
daily allowance is per-model.

### Tests

```bash
docker compose exec backend python -m pytest
```

The suite fakes the model with a scripted client, so it needs no API key and no
network.



## Frontend

http://localhost:5173 — sign in, queue a run, watch it work.

```
/login, /register   auth; the token is verified against the server, not trusted
/runs               run history, filterable by status
/runs/new           repo URL + task, with an Advanced panel for model and step cap
/runs/:id           live dashboard
```

The run dashboard opens the WebSocket for that run and renders steps as they
arrive, colour-coded by type — thought, action, observation — with the tool name
and arguments on actions, exit codes on observations, and failures marked in the
gutter. Long output collapses. A metrics strip tracks steps, tool calls, live
duration, and files changed. When the run ends it switches to the diff tab.

### Things that needed care

**Route protection fails safe.** A token in `localStorage` is a claim, not proof,
so it is verified against `/auth/me` on load. While that check is in flight the
app renders neither the protected page nor the login form — otherwise every
refresh flashes the login screen before bouncing back. There is a test for that
flash specifically.

**Reconnects must not duplicate steps.** The socket replays the backlog on
reconnect, so steps are merged by `step_number` rather than appended. The hook
also stops retrying on close code `1008` (the backend's "not authorized"), where
retrying could only loop.

**Diffs are stored as unified text**, but a side-by-side viewer needs two
separate documents. `splitUnifiedDiff` rebuilds them from the hunks — old side is
context plus removals, new side is context plus additions — and reads the
starting line number from the `@@` header. Files with several hunks are
concatenated, so numbering past the first hunk is approximate; each file has a
**raw diff** toggle showing the exact stored text.

### Tests

```bash
docker compose exec frontend npm test
```

25 tests over jsdom: the diff parser against real agent output, the stream hook
(ordering, de-duplication, keepalives, malformed frames, reconnect, terminal
refetch), and route protection (anonymous redirect, stale token rejection, no
login flash, readable 422s).

Two real bugs came out of writing them:

- The diff parser appended a phantom blank line to both sides of every file,
  because the trailing newline in a diff leaves an empty element after `split`.
- A failed login showed *"Your session has expired"* instead of the server's
  message: the 401 handler was written for expired sessions and was swallowing
  bad-credential responses too. It now only claims expiry when the request
  actually carried a token.

## Sandboxing

Phases 2-4 ran agent commands as a subprocess on the worker, behind the command
blocklist. That blocklist is still there, but it is no longer what stands between
a bad command and the host.

### What actually contains the agent

Every run gets its own container from [sandbox/Dockerfile](sandbox/Dockerfile),
created by [agent/sandbox.py](backend/agent/sandbox.py):

| Control | Setting | Stops |
|---------|---------|-------|
| Network | `network_mode=none` | Exfiltration, C2, dependency fetches |
| Memory | `mem_limit` + equal `memswap_limit` | Memory exhaustion of the host |
| CPU | `nano_cpus` | One run starving every other |
| Processes | `pids_limit` | Fork bombs |
| Wall clock | watchdog thread → `container.kill()` | Runs that never finish |
| Filesystem | `read_only` root, tmpfs `/tmp` + `/home/agent` | Tampering with its own toolchain |
| Privileges | `cap_drop=ALL`, `no-new-privileges`, uid 10001 | Escalation inside the container |
| Lifetime | `finally` teardown + startup reaper | Leaked containers and volumes |

`memswap_limit` matters more than it looks: without it the memory cap is
escapable by swapping, so the limit reads as enforced while doing nothing.

`SANDBOX_TIMEOUT` is wall-clock time the *container* is alive, which includes
time the agent spends waiting on the model. On a rate-limited free tier that
wait can be most of the budget -- a run held up by 429 backoff can be killed by
the watchdog without the agent ever misbehaving. Raise `SANDBOX_TIMEOUT` (or the
quota) if runs fail with `sandbox_timeout` while the logs show retries. Measuring
container time rather than agent-active time is deliberate: it is the number that
bounds how long host resources are held, which is what the limit is for.

**The sandbox has no network at all**, not even a restricted one. The worker
clones the repo (it is trusted and has egress) and hands the bytes to the
container over the Docker API with `put_archive`. Nothing inside ever needs to
reach out, so nothing inside ever can.

Because the workspace lives *inside* the container, all four tools — including
`read_file` and `write_file` — go through a `WorkspaceBackend`
([backends.py](backend/agent/backends.py)) rather than `pathlib`. The worker is
itself a container and cannot bind-mount its own filesystem into a sibling.

### Docker-in-Docker, and why not the raw socket

The worker has to create sibling containers. The usual way is to mount
`/var/run/docker.sock` into it — and that hands root-equivalent host access to
the one component whose entire job is running untrusted code. If the worker is
compromised, the sandbox is decoration.

So the worker never sees the socket. It talks to a
[docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) that
holds the real socket read-only and allow-lists API sections:
`CONTAINERS`, `VOLUMES`, `IMAGES`, `EXEC`, `POST`. Everything else — networks,
swarm, secrets, configs, plugins, build, system info — returns `403`. The proxy
publishes no ports, so only the compose network can reach it.

**What this does not do.** `CONTAINERS=1` plus `POST=1` is still enough to create
a container, and the proxy filters by API *section*, not by request body — so it
cannot stop a caller from asking for a container with `Binds: ["/:/host"]`. A
compromised worker could still reach the host that way. The proxy meaningfully
shrinks the attack surface and removes whole classes of daemon manipulation; it
is not a complete boundary around the worker.

The boundary this *does* fully close is the one that matters most: the **agent**
gets no Docker access of any kind. No socket, no client, no network to reach the
proxy over. Genuinely sealing the worker as well would take a rootless or
VM-isolated runtime (gVisor, Kata, Firecracker) — the right next step, and out of
scope here.

### Proving it

The containment tests drive the sandbox directly, with no agent loop and no LLM,
so they cost nothing and can run as often as you like:

```bash
docker compose exec worker python -m pytest tests/test_sandbox.py -v
```

Run the **whole** backend suite in the worker too. The backend container has no
Docker endpoint, so running it there silently skips all 33 containment tests —
the suite reports green while proving nothing about isolation:

```bash
docker compose exec worker python -m pytest    # 131 tests
docker compose exec backend python -m pytest   # 98 pass, 33 skipped
```

They try to break out rather than confirm the happy path: reach `1.1.1.1`,
resolve DNS, allocate 400MB under a 128m cap, fork-bomb under a 32-PID cap,
`rm -rf /`, write to the read-only root, escape the workspace with `..`, outlive
the wall-clock deadline, and leak a container by crashing mid-run.

Two of these initially passed for the wrong reason, which is worth recording:

- The read-only-root check probed `/usr/bin`, which the agent user cannot write
  *anyway*. It passed on file permissions and proved nothing about the mount.
  The image now ships an agent-owned `/opt/sandcastle` specifically so the probe
  fails only when the root filesystem is genuinely read-only.
- The non-root check ran through an exec that pins `user=agent`, so it would have
  passed even against a container configured to run as root. It now asserts the
  container's own `Config.User` as well.

Both were caught by running negative controls — rebuilding each sandbox with one
control deliberately removed and confirming the test then fails. A containment
test that passes on a broken sandbox is worse than no test.

The reaper has its own hazard: run on worker startup, a naive version would
destroy the sandboxes of any run already in flight under another worker. It only
removes containers that have stopped, or that have outlived twice the wall-clock
budget — past which no sandbox can legitimately still be running, because the
watchdog would have killed it. Volumes are removed without `force`, so the daemon
refuses any a live container still holds.

Crash cleanup is verified against a real `SIGKILL`:

```bash
docker compose kill -s SIGKILL worker && docker compose start worker
# worker-1 | reaped orphaned sandbox container sandcastle-run-...
```

## Roadmap

- [x] **Phase 0** — Scaffold: compose stack, health check, DB/Alembic wiring
- [x] **Phase 1** — Schema: users, tasks, runs, steps, diffs, scores, embeddings
- [x] **Phase 2** — Agent loop with file + shell tools
- [x] **Phase 3** — Celery jobs, REST API, JWT auth
- [x] **Phase 4** — WebSocket streaming of run steps
- [x] **Phase 5** — Docker-isolated execution
- [x] **Phase 6** — Frontend: submission + live run dashboard
- [ ] **Phase 7** — Test execution + LLM-as-judge scoring
- [ ] **Phase 8** — pgvector semantic search over past tasks
- [ ] **Phase 9** — Rate limits, cancellation, hardening
