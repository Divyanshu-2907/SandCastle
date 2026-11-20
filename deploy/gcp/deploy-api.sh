#!/usr/bin/env bash
# Deploy the FastAPI service to Cloud Run.
#
#   export PROJECT_ID=... REGION=us-central1 WORKER_IP=10.128.0.x
#   bash deploy/gcp/deploy-api.sh            # deploys :latest
#   IMAGE_TAG=abc1234 bash deploy/gcp/deploy-api.sh   # or a specific build
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-${REGION}-a}"
REPO="${REPO:-sandcastle}"
SERVICE="${SERVICE:-sandcastle-api}"
SQL_INSTANCE="${SQL_INSTANCE:-sandcastle-pg}"
DB_NAME="${DB_NAME:-sandcastle}"
DB_USER="${DB_USER:-sandcastle}"
VM_NAME="${VM_NAME:-sandcastle-worker}"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:5173}"

AR="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
SQL_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
WORKER_IP="${WORKER_IP:-$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')}"

secret() { gcloud secrets versions access latest --secret="$1"; }
DB_PASSWORD="$(secret sandcastle-db-password)"
REDIS_PASSWORD="$(secret sandcastle-redis-password)"

# Cloud Run cannot interpolate a secret into an env var, and these three values
# are credentials with a hostname attached. So the assembled URLs are themselves
# secrets, rebuilt here from the component secrets and mounted as secret env
# vars -- never stored in the service's plaintext configuration.
put_secret() {  # name, value
  gcloud secrets describe "$1" >/dev/null 2>&1 \
    || gcloud secrets create "$1" --replication-policy=automatic --data-file=/dev/null >/dev/null
  printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  echo "  updated $1"
}
echo "==> URL secrets"
# The API reaches Postgres over the Cloud SQL unix socket Cloud Run mounts for
# --add-cloudsql-instances; no proxy container and no IP allowlist needed.
put_secret sandcastle-api-database-url \
  "postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${SQL_CONNECTION_NAME}"
put_secret sandcastle-api-redis-url   "redis://:${REDIS_PASSWORD}@${WORKER_IP}:6379/0"
put_secret sandcastle-api-broker-url  "redis://:${REDIS_PASSWORD}@${WORKER_IP}:6379/1"
put_secret sandcastle-api-result-url  "redis://:${REDIS_PASSWORD}@${WORKER_IP}:6379/2"

RUN_SA="sandcastle-api@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts create sandcastle-api --display-name="Sandcastle API" 2>/dev/null || true
for role in roles/cloudsql.client roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUN_SA}" --role="$role" --condition=None >/dev/null
done

# 2 + 3 connections per instance x 4 instances = 20, leaving room under the
# ~25 a db-f1-micro allows for the worker, beat and the odd psql session.
# Raise these together with the Cloud SQL tier, not on their own.
echo "==> deploying ${SERVICE} (${IMAGE_TAG})"
gcloud run deploy "$SERVICE" \
  --image="${AR}/sandcastle-backend:${IMAGE_TAG}" \
  --region="$REGION" \
  --service-account="$RUN_SA" \
  --allow-unauthenticated \
  --port=8000 \
  --cpu=1 --memory=1Gi \
  --add-cloudsql-instances="$SQL_CONNECTION_NAME" \
  --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only \
  --min-instances=1 --max-instances=4 \
  --concurrency=80 \
  --timeout=3600 \
  --no-cpu-throttling \
  --set-env-vars="ENVIRONMENT=production,CORS_ORIGINS=${CORS_ORIGINS},LLM_PROVIDER=${LLM_PROVIDER:-gemini},AGENT_MODEL=${AGENT_MODEL:-gemini-2.5-flash},DB_POOL_SIZE=${DB_POOL_SIZE:-2},DB_MAX_OVERFLOW=${DB_MAX_OVERFLOW:-3}" \
  --set-secrets="DATABASE_URL=sandcastle-api-database-url:latest,REDIS_URL=sandcastle-api-redis-url:latest,CELERY_BROKER_URL=sandcastle-api-broker-url:latest,CELERY_RESULT_BACKEND=sandcastle-api-result-url:latest,JWT_SECRET=sandcastle-jwt-secret:latest,GEMINI_API_KEY=sandcastle-gemini-key:latest"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
echo
echo "API: ${URL}"
echo "Check it:  curl -s ${URL}/health"
echo "A 'degraded' answer names the dependency that is unreachable -- the probe"
echo "itself always returns 200, so Cloud Run will not flap on a DB blip."
