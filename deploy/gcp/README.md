# Deploying Sandcastle on GCP

Two tiers, because the two halves of the system want different things:

| Component | Where | Why |
|---|---|---|
| FastAPI + WebSockets | **Cloud Run** | Stateless, request-shaped, scales to zero-ish |
| Celery worker + sandboxes | **GCE VM** (e2-medium) | Needs a Docker daemon to create sibling containers |
| Postgres 16 + pgvector | **Cloud SQL** (db-f1-micro) | Managed backups; pgvector created by the migration |
| Redis (broker + pub/sub) | **container on the worker VM** | Colocated with its only producer; Memorystore is a one-URL swap |
| Images | **Artifact Registry** | Tagged by commit SHA |
| Credentials | **Secret Manager** | Nothing in env files or service config |

**The worker cannot run on Cloud Run.** A run's whole safety story is that
agent-generated code executes in a separate container with `network_mode=none`,
capped memory/CPU/pids and a wall-clock watchdog, created through a Docker socket
proxy that denies everything but the container lifecycle. Cloud Run gives a
container no Docker daemon and no privileged mode, so that layer has nowhere to
live there. The API is the part that is genuinely stateless, so that is the part
that goes serverless.

## Networking

```
        internet
            │  HTTPS
     ┌──────▼───────┐   unix socket /cloudsql/…   ┌──────────────┐
     │  Cloud Run   │────────────────────────────►│   Cloud SQL  │
     │  (API + WS)  │                             │  Postgres 16 │
     └──────┬───────┘                             └──────▲───────┘
            │ direct VPC egress (private ranges)         │ Cloud SQL
            │ tcp 6379, subnet range only                │ Auth Proxy
     ┌──────▼─────────────────────────────────────────────┴───────┐
     │ GCE worker VM  ·  redis │ celery worker │ socket proxy      │
     │                              └─► sandbox containers (no net)│
     └──────────────────────────────────────────────────────────────┘
```

Redis is published on the VM's **internal** IP only, with `requirepass`, and the
firewall rule admits just the subnet range Cloud Run's direct VPC egress sources
from. No broker port faces the internet.

## Deploy

Prerequisites: `gcloud` authenticated, Docker running locally, billing enabled.

```bash
export PROJECT_ID=your-project
export REGION=us-central1

# 1. APIs, registry, Cloud SQL, secrets, firewall, worker VM  (~10 min, mostly Cloud SQL)
bash deploy/gcp/bootstrap.sh

# 2. The one secret that cannot be generated
printf %s "$GEMINI_API_KEY" | gcloud secrets versions add sandcastle-gemini-key --data-file=-

# 3. Build + push backend and sandbox images, ship compose.yml to the VM
bash deploy/gcp/push-images.sh

# 4. Start the worker stack and apply migrations (this creates the pgvector extension)
gcloud compute ssh sandcastle-worker --zone="${REGION}-a" --command 'sudo /opt/sandcastle/up.sh'

# 5. Deploy the API
bash deploy/gcp/deploy-api.sh
```

`deploy-api.sh` prints the service URL. `GET /health` reports each dependency
separately and always returns 200, so a Postgres blip shows as `degraded`
instead of taking the revision down.

### Redeploying after a code change

```bash
bash deploy/gcp/push-images.sh                      # prints the new SHA tag
gcloud compute ssh sandcastle-worker --zone="${REGION}-a" --command 'sudo /opt/sandcastle/up.sh'
IMAGE_TAG=<sha> bash deploy/gcp/deploy-api.sh
```

Roll back by re-running step 3 with an older tag — the images are still in
Artifact Registry, so nothing is rebuilt.

### Frontend

Not covered by these scripts. `npm run build` in `frontend/` with
`VITE_API_URL=<Cloud Run URL>` (the `https://` one -- `WS_BASE` in
`frontend/src/api/client.ts` derives `wss://` from it, and a plaintext
`ws://` upgrade will not survive Cloud Run's TLS termination), then host
`dist/` anywhere static (Firebase
Hosting or a GCS bucket behind a load balancer). Remember to add that origin to
`CORS_ORIGINS` when deploying the API.

## Cloud Run settings that are not defaults, and why

- `--no-cpu-throttling` — a WebSocket connection sits idle between steps. With
  CPU throttled outside a request, the asyncio task pumping Redis pub/sub stops
  running and clients silently stall.
- `--min-instances=1` — the WS endpoint holds a Redis subscription; scaling to
  zero drops it, and a cold start on reconnect is worse than the cost of one
  warm instance.
- `--timeout=3600` — a long-lived WebSocket counts as one request. The default
  5 minutes would cut streams mid-run.
- `--concurrency=80` — WS connections are cheap here; each is one subscription,
  not one agent run. The expensive work is on the worker.

## Cost

Roughly $25–35/month at idle: Cloud SQL db-f1-micro (~$9), e2-medium VM (~$25,
or ~$17 with a committed-use discount), Cloud Run under the free tier at
min-instances=1 if traffic is low, Artifact Registry pennies. Memorystore Basic
would add ~$35 — hence the Redis container.

Stop paying without tearing down: `gcloud compute instances stop
sandcastle-worker` and `gcloud sql instances patch sandcastle-pg
--activation-policy=NEVER`.

## What the deploy changed in the app

Three things only showed up once the topology was real:

- **Connection pool, sized for this topology.** `create_engine` took
  SQLAlchemy's defaults (5 + 10 overflow = 15 per process); four Cloud Run
  instances alone would reserve 60 of the ~25 connections a db-f1-micro allows.
  Now driven by `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, and the API deploys with
  2 + 3. `pool_recycle` was added for the same reason: Cloud SQL drops idle
  connections, and recycling under that window turns a stale-socket error into
  a silent reconnect.
- **A Celery time limit.** The sandbox watchdog only bounds time spent *inside*
  the container. A clone against a dead remote, an image pull, or an LLM socket
  that never closes is outside it, so `task_soft_time_limit` now backstops the
  whole task and the run records its own timeout.
- **An orphaned-run sweeper** (`app.jobs.sweep_stale_runs`, on a 5-minute beat).
  Sandboxes were already reaped on worker startup; the *rows* were not. A run
  whose worker was SIGKILLed stayed `running` forever, and a run whose queued
  message Redis dropped stayed `queued` forever. Both are now failed past their
  deadline, with the WebSocket told, so clients stop waiting.

## Known limits

- **One worker VM.** The queue would fan out to several unchanged — Celery
  doesn't care — but there is no autoscaling group, so a burst queues rather
  than scaling.
- **No task retries.** Deliberate, not an oversight: see the reasoning in
  `backend/app/worker.py`. Agent runs are long and non-idempotent, and there is
  no checkpoint to resume one from, so an orphaned run is failed by the sweeper
  for the user to re-queue rather than retried silently.
- **One `beat` process, and it must stay one.** The schedule lives in
  `app/worker.py` but only fires if beat is running; two schedulers would
  double every periodic task.
- **The VM has a public IP** so it can clone repos and call the Gemini API.
  Cloud NAT with no external IP is the hardening step.
