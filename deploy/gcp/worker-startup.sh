#!/usr/bin/env bash
# GCE startup script for the Sandcastle worker VM. Runs as root on every boot.
#
# Installs Docker, fetches secrets, writes /opt/sandcastle/{compose.yml,.env},
# and leaves an `up.sh` that pulls images, migrates, and starts the stack.
# Re-running is safe: every step is a no-op when already done.
set -euo pipefail
exec > >(logger -t sandcastle-startup -s 2>/dev/console) 2>&1

meta() { curl -fsH 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/$1"; }

PROJECT_ID="$(meta project/project-id)"
REGION="$(meta instance/attributes/sandcastle-region)"
REPO="$(meta instance/attributes/sandcastle-repo)"
SQL_CONNECTION_NAME="$(meta instance/attributes/sandcastle-sql)"
DB_NAME="$(meta instance/attributes/sandcastle-db-name)"
DB_USER="$(meta instance/attributes/sandcastle-db-user)"
INTERNAL_IP="$(meta instance/network-interfaces/0/ip)"

if ! command -v docker >/dev/null 2>&1; then
  echo "installing docker"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# Lets the daemon pull from Artifact Registry without a long-lived key file.
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

install -d -m 0750 /opt/sandcastle
AR="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

secret() { gcloud secrets versions access latest --secret="$1" 2>/dev/null || echo ""; }

# 0600: this file holds the DB, Redis and LLM credentials.
umask 077
cat > /opt/sandcastle/.env <<ENVFILE
WORKER_IMAGE=${AR}/sandcastle-backend:latest
SANDBOX_IMAGE=${AR}/sandcastle-sandbox:latest
SQL_CONNECTION_NAME=${SQL_CONNECTION_NAME}
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASSWORD=$(secret sandcastle-db-password)
REDIS_PASSWORD=$(secret sandcastle-redis-password)
JWT_SECRET=$(secret sandcastle-jwt-secret)
GEMINI_API_KEY=$(secret sandcastle-gemini-key)
# Published on the internal IP only, so the Cloud Run API can reach the broker
# over direct VPC egress while the internet cannot.
REDIS_BIND=${INTERNAL_IP}
ENVFILE
chmod 600 /opt/sandcastle/.env

cat > /opt/sandcastle/up.sh <<'UPSH'
#!/usr/bin/env bash
# Pull the current images, apply migrations, then (re)start the worker stack.
set -euo pipefail
cd /opt/sandcastle
docker compose pull
# The sandbox image is pulled explicitly: it is never started by compose, the
# socket proxy denies BUILD, and a run that pulls its own image first would pay
# that cost inside its own wall-clock timeout.
docker pull "$(grep -E '^SANDBOX_IMAGE=' .env | cut -d= -f2-)"
docker compose --profile tools run --rm migrate
docker compose up -d --remove-orphans
docker compose ps
UPSH
chmod 750 /opt/sandcastle/up.sh

# compose.yml is deployed alongside this script by push-images.sh / scp; if a
# boot finds one already there, keep it.
if [[ -f /opt/sandcastle/compose.yml ]]; then
  cd /opt/sandcastle && docker compose up -d --remove-orphans || true
fi
echo "startup complete"
