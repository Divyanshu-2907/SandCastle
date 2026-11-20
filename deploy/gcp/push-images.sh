#!/usr/bin/env bash
# Build and push the backend + sandbox images, and ship compose.yml to the VM.
#
#   export PROJECT_ID=... REGION=us-central1
#   bash deploy/gcp/push-images.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-${REGION}-a}"
REPO="${REPO:-sandcastle}"
VM_NAME="${VM_NAME:-sandcastle-worker}"
AR="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
# Tag by commit so a rollback is `deploy-api.sh` with an older SHA, not a rebuild.
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)"

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "==> backend ${TAG}"
docker build -f docker/backend.prod.Dockerfile -t "${AR}/sandcastle-backend:${TAG}" -t "${AR}/sandcastle-backend:latest" .
docker push "${AR}/sandcastle-backend:${TAG}"
docker push "${AR}/sandcastle-backend:latest"

echo "==> sandbox ${TAG}"
docker build -t "${AR}/sandcastle-sandbox:${TAG}" -t "${AR}/sandcastle-sandbox:latest" ./sandbox
docker push "${AR}/sandcastle-sandbox:${TAG}"
docker push "${AR}/sandcastle-sandbox:latest"

echo "==> shipping compose.yml to ${VM_NAME}"
gcloud compute scp deploy/gcp/worker-compose.yml "${VM_NAME}:/tmp/compose.yml" --zone="$ZONE"
gcloud compute ssh "$VM_NAME" --zone="$ZONE" --command \
  'sudo install -m 0640 /tmp/compose.yml /opt/sandcastle/compose.yml && rm /tmp/compose.yml'

echo
echo "pushed ${TAG}. Start/refresh the worker with:"
echo "  gcloud compute ssh ${VM_NAME} --zone=${ZONE} --command 'sudo /opt/sandcastle/up.sh'"
echo "Then deploy the API with:  IMAGE_TAG=${TAG} bash deploy/gcp/deploy-api.sh"
