#!/usr/bin/env bash
# One-time GCP setup: APIs, registry, Cloud SQL, secrets, firewall, worker VM.
# Idempotent -- every create is guarded, so re-running after a failure is safe.
#
#   export PROJECT_ID=your-project REGION=us-central1
#   bash deploy/gcp/bootstrap.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-${REGION}-a}"
REPO="${REPO:-sandcastle}"
SQL_INSTANCE="${SQL_INSTANCE:-sandcastle-pg}"
DB_NAME="${DB_NAME:-sandcastle}"
DB_USER="${DB_USER:-sandcastle}"
VM_NAME="${VM_NAME:-sandcastle-worker}"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"

gcloud config set project "$PROJECT_ID" >/dev/null

say() { printf '\n==> %s\n' "$*"; }
# Treat "already exists" as success so the script can be re-run.
ok_if_exists() { "$@" 2>&1 | tee /dev/stderr | grep -qiE 'already exists|ALREADY_EXISTS' || true; }

say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  compute.googleapis.com \
  vpcaccess.googleapis.com

say "Artifact Registry: $REPO"
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="Sandcastle images" 2>/dev/null || echo "  exists"

say "Cloud SQL: $SQL_INSTANCE (Postgres 16)"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  # db-f1-micro is the cheapest tier that runs PG16; pgvector is created by the
  # Alembic migration, not by a flag.
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_16 \
    --tier=db-f1-micro \
    --region="$REGION" \
    --storage-size=10GB \
    --storage-auto-increase \
    --backup-start-time=03:00
else
  echo "  exists"
fi
gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE" 2>/dev/null || echo "  database exists"

say "Secrets"
# Generated once and stored in Secret Manager. Nothing here is ever written to a
# file in the repo or passed on a command line that lands in shell history.
create_secret() {  # name, value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    echo "  $1 exists (leaving as is)"
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic
  fi
}
DB_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
REDIS_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
JWT_SECRET="$(openssl rand -base64 32 | tr -d '/+=' | head -c 43)"
create_secret sandcastle-db-password  "$DB_PASSWORD"
create_secret sandcastle-redis-password "$REDIS_PASSWORD"
create_secret sandcastle-jwt-secret   "$JWT_SECRET"
if ! gcloud secrets describe sandcastle-gemini-key >/dev/null 2>&1; then
  echo "  creating sandcastle-gemini-key as empty -- set it with:"
  echo "    printf %s \"\$GEMINI_API_KEY\" | gcloud secrets versions add sandcastle-gemini-key --data-file=-"
  printf '' | gcloud secrets create sandcastle-gemini-key --data-file=- --replication-policy=automatic
fi

say "Cloud SQL user: $DB_USER"
# Read the password back from Secret Manager so a re-run reuses the stored one
# rather than silently rotating it out from under the running services.
DB_PASSWORD="$(gcloud secrets versions access latest --secret=sandcastle-db-password)"
gcloud sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD" 2>/dev/null \
  || gcloud sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASSWORD"

say "Service account for the worker VM"
WORKER_SA="sandcastle-worker@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts create sandcastle-worker \
  --display-name="Sandcastle Celery worker" 2>/dev/null || echo "  exists"
for role in roles/cloudsql.client roles/secretmanager.secretAccessor roles/artifactregistry.reader roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${WORKER_SA}" --role="$role" --condition=None >/dev/null
done

say "Firewall: Cloud Run -> Redis on the worker VM"
# Cloud Run with direct VPC egress sources traffic from the subnet's own range,
# so the broker is reachable without exposing 6379 to the internet.
SUBNET_RANGE="$(gcloud compute networks subnets describe "$SUBNET" --region="$REGION" --format='value(ipCidrRange)')"
gcloud compute firewall-rules create sandcastle-allow-redis \
  --network="$NETWORK" \
  --direction=INGRESS --action=ALLOW --rules=tcp:6379 \
  --source-ranges="$SUBNET_RANGE" \
  --target-tags=sandcastle-worker 2>/dev/null || echo "  exists"

say "Worker VM: $VM_NAME"
# e2-medium, not e2-micro: this host runs the Celery worker *and* every sandbox
# container it spawns (512m / 1 CPU each by default).
if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" \
    --zone="$ZONE" \
    --machine-type=e2-medium \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=50GB \
    --subnet="$SUBNET" \
    --tags=sandcastle-worker \
    --service-account="$WORKER_SA" \
    --scopes=cloud-platform \
    --metadata-from-file=startup-script=deploy/gcp/worker-startup.sh \
    --metadata="sandcastle-project=${PROJECT_ID},sandcastle-region=${REGION},sandcastle-repo=${REPO},sandcastle-sql=${PROJECT_ID}:${REGION}:${SQL_INSTANCE},sandcastle-db-name=${DB_NAME},sandcastle-db-user=${DB_USER}"
else
  echo "  exists"
fi

WORKER_IP="$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')"

cat <<SUMMARY

=== done ===
Cloud SQL instance : ${PROJECT_ID}:${REGION}:${SQL_INSTANCE}
Worker VM          : ${VM_NAME} (internal IP ${WORKER_IP})
Redis (broker)     : ${WORKER_IP}:6379   <- pass as WORKER_IP to deploy-api.sh

Next:
  1. printf %s "\$GEMINI_API_KEY" | gcloud secrets versions add sandcastle-gemini-key --data-file=-
  2. bash deploy/gcp/push-images.sh
  3. gcloud compute ssh ${VM_NAME} --zone=${ZONE} --command 'sudo /opt/sandcastle/up.sh'   # starts redis + worker, runs migrations
  4. WORKER_IP=${WORKER_IP} bash deploy/gcp/deploy-api.sh
SUMMARY
