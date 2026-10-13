#!/bin/sh
# Deploy the claude-learn demo to Cloud Run.
#
#   ANTHROPIC_API_KEY=sk-ant-... scripts/deploy.sh <gcp-project-id> [region]
#
# The API key is stored in Secret Manager (never in the image or repo). One
# instance keeps each visitor's sandbox on the same server.
set -eu
PROJECT="$1"
REGION="${2:-us-central1}"
SERVICE=claude-learn
SECRET=anthropic-api-key

P="--project=$PROJECT"
gcloud services enable "$P" run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  if gcloud secrets describe "$SECRET" "$P" >/dev/null 2>&1; then
    printf %s "$ANTHROPIC_API_KEY" | gcloud secrets versions add "$SECRET" "$P" --data-file=-
  else
    printf %s "$ANTHROPIC_API_KEY" | gcloud secrets create "$SECRET" "$P" --data-file=- --replication-policy=automatic
  fi
fi

NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding "$SECRET" "$P" \
  --member="serviceAccount:${NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor >/dev/null

gcloud run deploy "$SERVICE" "$P" --source . --region "$REGION" \
  --allow-unauthenticated --max-instances 1 --memory 1Gi --timeout 300 \
  --set-secrets "ANTHROPIC_API_KEY=${SECRET}:latest"
