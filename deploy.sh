#!/usr/bin/env bash
set -eo pipefail

echo "============================================================"
echo "PROJECT SENTINEL: Autonomous Agent Fleet Deployment"
echo "============================================================"

# Load configuration from .env or config/settings.py
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

PROJECT_ID=${GCP_PROJECT_ID:-${PROJECT_ID}}
if [ -z "$PROJECT_ID" ]; then
  echo "ERROR: GCP_PROJECT_ID is not set." >&2
  exit 1
fi

REGION=${GCP_REGION:-us-central1}
INFERENCE_LOC=${VERTEX_INFERENCE_LOCATION:-global}
if [ -z "$MODEL_ID" ]; then
  echo "ERROR: MODEL_ID is unset. Must be exported in environment." >&2
  exit 1
fi
MODEL="$MODEL_ID"
DATASET=${BQ_DATASET:-sentinel}
TOPIC="sentinel-deviations"
SERVICE_NAME="sentinel-orchestrator"
SUBSCRIPTION="sentinel-orchestrator-push"

echo "Project ID:           $PROJECT_ID"
echo "Compute & Data:       $REGION"
echo "Inference Location:   $INFERENCE_LOC"
echo "Dataset:              $DATASET"
echo "Pub/Sub Topic:        $TOPIC"
echo "Cloud Run Service:    $SERVICE_NAME"
echo "------------------------------------------------------------"

# 1. Preflight Gate (§2.1, §8.9, I-13)
echo "[STEP 1/5] Executing Preflight Model Pinning Gate..."
./.venv/bin/python -m scripts.preflight || python3 -m scripts.preflight

# 2. Service Accounts & IAM Roles (§2.2, §5)
echo "[STEP 2/5] Provisioning Per-Agent Service Accounts..."
for SA_NAME in sa-hygiene sa-sourcing sa-orchestrator sa-pubsub sa-de-agent sa-ca-agent; do
  SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "  Creating $SA_EMAIL..."
    gcloud iam service-accounts create "$SA_NAME" \
      --display-name="Sentinel $SA_NAME" \
      --project="$PROJECT_ID" --quiet || true
  else
    echo "  $SA_EMAIL already exists."
  fi
done

# Bind IAM roles for sa-orchestrator
echo "  Configuring IAM permissions for sa-orchestrator..."
ORCH_SA="sa-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com"
for ROLE in roles/bigquery.dataEditor roles/bigquery.jobUser roles/aiplatform.user roles/cloudtrace.agent roles/datastore.user roles/pubsub.publisher; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$ORCH_SA" \
    --role="$ROLE" \
    --condition=None --quiet >/dev/null
done

# Bind IAM for sa-pubsub (invoker)
PUBSUB_SA="sa-pubsub@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$PUBSUB_SA" \
  --role="roles/run.invoker" \
  --condition=None --quiet >/dev/null

# 3. Provision Pub/Sub Topic (§5, §8.12)
echo "[STEP 3/5] Provisioning Pub/Sub Topic..."
if ! gcloud pubsub topics describe "$TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  Creating topic $TOPIC..."
  gcloud pubsub topics create "$TOPIC" --project="$PROJECT_ID" --quiet
else
  echo "  Topic $TOPIC already exists."
fi

# 4. Build and Deploy Cloud Run Orchestrator Service (§5)
echo "[STEP 4/5] Deploying Cloud Run Service ($SERVICE_NAME in $REGION)..."
gcloud run deploy "$SERVICE_NAME" \
  --source="." \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --service-account="$ORCH_SA" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},VERTEX_INFERENCE_LOCATION=${INFERENCE_LOC},MODEL_ID=${MODEL},BQ_DATASET=${DATASET}" \
  --min-instances=0 \
  --max-instances=5 \
  --memory=2Gi \
  --cpu=2 \
  --timeout=300 \
  --no-allow-unauthenticated \
  --quiet

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(status.url)")
echo "  Cloud Run Service URL: $SERVICE_URL"

# Allow sa-pubsub to invoke Cloud Run service
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$PUBSUB_SA" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

# 5. Provision Pub/Sub Push Subscription (§5, §8.12)
echo "[STEP 5/5] Provisioning Pub/Sub Push Subscription ($SUBSCRIPTION)..."
PUSH_ENDPOINT="${SERVICE_URL}/push"

if gcloud pubsub subscriptions describe "$SUBSCRIPTION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  Updating push subscription $SUBSCRIPTION to $PUSH_ENDPOINT..."
  gcloud pubsub subscriptions update "$SUBSCRIPTION" \
    --project="$PROJECT_ID" \
    --push-endpoint="$PUSH_ENDPOINT" \
    --push-auth-service-account="$PUBSUB_SA" \
    --ack-deadline=120 \
    --quiet
else
  echo "  Creating push subscription $SUBSCRIPTION targeting $PUSH_ENDPOINT..."
  gcloud pubsub subscriptions create "$SUBSCRIPTION" \
    --project="$PROJECT_ID" \
    --topic="$TOPIC" \
    --push-endpoint="$PUSH_ENDPOINT" \
    --push-auth-service-account="$PUBSUB_SA" \
    --ack-deadline=120 \
    --quiet
fi

# Seed Registry Manifests (§2.2, §8.8)
./.venv/bin/python -m scripts.seed_registry || python3 -m scripts.seed_registry

echo "============================================================"
echo "SENTINEL FLEET DEPLOYMENT COMPLETE (Gate 3 Green)"
echo "Service URL:   $SERVICE_URL"
echo "Push Target:   $PUSH_ENDPOINT"
echo "============================================================"
