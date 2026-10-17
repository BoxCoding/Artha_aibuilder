#!/usr/bin/env bash
# Public hackathon demo on Cloud Run: one service anyone can open, demo accounts on, the smallest
# Cloud SQL for PostgreSQL, and Gemini on Vertex AI. Scales to zero when nobody is using it.
#
#   PROJECT_ID=my-project deploy/demo.sh
#
# Safe to run again: existing resources are reused and only a new revision is deployed.
# For the full production layout (HA database, worker service, private access) see deploy/README.md.
set -euo pipefail

PROJECT_ID=${PROJECT_ID:?set PROJECT_ID}
REGION=${REGION:-asia-south1}
SERVICE=ledgerlens
INSTANCE=ledgerlens-db
SA=ledgerlens-run@$PROJECT_ID.iam.gserviceaccount.com
IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/ledgerlens/api:$(git rev-parse --short HEAD)
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }
secret() {  # create the secret on first run; never overwrite one that exists
  exists gcloud secrets describe "$1" || printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --quiet
  gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}

gcloud config set project "$PROJECT_ID" --quiet

step "APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com sqladmin.googleapis.com aiplatform.googleapis.com --quiet

step "Image repository and runtime identity"
exists gcloud artifacts repositories describe ledgerlens --location="$REGION" ||
  gcloud artifacts repositories create ledgerlens --repository-format=docker --location="$REGION" --quiet
exists gcloud iam service-accounts describe "$SA" ||
  gcloud iam service-accounts create ledgerlens-run --display-name="LedgerLens Cloud Run" --quiet
for role in roles/aiplatform.user roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role" \
    --condition=None --quiet >/dev/null
done

step "Database (Cloud SQL for PostgreSQL, smallest tier; first run takes ~10 minutes)"
if ! exists gcloud sql instances describe "$INSTANCE"; then
  gcloud sql instances create "$INSTANCE" --database-version=POSTGRES_17 --edition=ENTERPRISE \
    --tier=db-f1-micro --region="$REGION" --storage-size=10 --storage-auto-increase --quiet
  gcloud sql databases create ledgerlens --instance="$INSTANCE" --quiet
fi
if ! exists gcloud secrets describe ledgerlens-database-url; then
  DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  gcloud sql users create ledgerlens --instance="$INSTANCE" --password="$DB_PASSWORD" --quiet
  secret ledgerlens-database-url \
    "postgresql+psycopg://ledgerlens:$DB_PASSWORD@/ledgerlens?host=/cloudsql/$PROJECT_ID:$REGION:$INSTANCE"
fi

step "Secrets"
GENERATED=$(python3 -c "import base64, secrets
print(secrets.token_urlsafe(48)); print(base64.b64encode(secrets.token_bytes(32)).decode())")
secret ledgerlens-session-key "$(sed -n 1p <<<"$GENERATED")"
secret ledgerlens-receipt-seed "$(sed -n 2p <<<"$GENERATED")"

step "Build $IMAGE (on Cloud Build)"
gcloud builds submit --config deploy/cloudbuild-image.yaml --substitutions=_IMAGE="$IMAGE" --quiet

SECRETS=SECRET_KEY=ledgerlens-session-key:latest,RECEIPT_SIGNING_SEED=ledgerlens-receipt-seed:latest,DATABASE_URL=ledgerlens-database-url:latest

step "Migrate the database"
gcloud run jobs deploy ledgerlens-migrate --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --set-cloudsql-instances="$PROJECT_ID:$REGION:$INSTANCE" --set-secrets="$SECRETS" \
  --set-env-vars=APP_ENV=demo --command=alembic --args=upgrade,head --max-retries=0 --task-timeout=600 --quiet
gcloud run jobs execute ledgerlens-migrate --region="$REGION" --wait --quiet

step "Deploy the service"
# Inline pipeline: an application is analysed on a thread in the same instance, so CPU must stay
# allocated after the response (--no-cpu-throttling). Instances still scale to zero when idle.
gcloud run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$SA" \
  --allow-unauthenticated --add-cloudsql-instances="$PROJECT_ID:$REGION:$INSTANCE" \
  --no-cpu-throttling --min-instances=0 --max-instances=3 --concurrency=40 --cpu=1 --memory=2Gi --timeout=300 \
  --set-env-vars=APP_ENV=demo,DEMO_MODE=true,PIPELINE_MODE=inline,LLM_MODE=vertex,TRUST_PROXY_HEADERS=true,GOOGLE_CLOUD_PROJECT="$PROJECT_ID",GOOGLE_CLOUD_LOCATION=global,WEB_CONCURRENCY=1,DB_POOL_SIZE=3,DB_MAX_OVERFLOW=2 \
  --set-secrets="$SECRETS" --quiet

step "Live at"
gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)'
