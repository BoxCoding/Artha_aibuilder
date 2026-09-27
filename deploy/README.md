# Deploying LedgerLens

Two Cloud Run services and one Cloud Run job, all from the same image:

| Name | Kind | Command | Notes |
| --- | --- | --- | --- |
| `ledgerlens` | service | uvicorn (image default) | User traffic. Stateless, so it autoscales. |
| `ledgerlens-worker` | service | `python -m app.worker` | Internal ingress, CPU always allocated, min 1 instance. Answers health checks on `$PORT`. |
| `ledgerlens-migrate` | job | `alembic upgrade head` | Runs in Cloud Build before each deploy. |

State lives in Cloud SQL for PostgreSQL. Secrets live in Secret Manager.

## One-time project setup

```bash
export PROJECT_ID=your-project
export REGION=asia-south1

gcloud services enable run.googleapis.com aiplatform.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com sqladmin.googleapis.com \
  modelarmor.googleapis.com

gcloud artifacts repositories create ledgerlens --repository-format=docker --location=$REGION

# Runtime identity: model access, Cloud SQL, its own secrets. Nothing else.
gcloud iam service-accounts create ledgerlens-run --display-name="LedgerLens Cloud Run"
SA=ledgerlens-run@$PROJECT_ID.iam.gserviceaccount.com
for role in roles/aiplatform.user roles/cloudsql.client roles/pubsub.publisher roles/bigquery.jobUser \
            roles/bigquery.dataViewer roles/documentai.apiUser roles/discoveryengine.viewer; do
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$SA" --role=$role
done
```

### Database

```bash
gcloud sql instances create ledgerlens-db --database-version=POSTGRES_17 --region=$REGION \
  --tier=db-custom-2-7680 --availability-type=REGIONAL --storage-auto-increase \
  --backup-start-time=20:30 --enable-point-in-time-recovery --deletion-protection
gcloud sql databases create ledgerlens --instance=ledgerlens-db
DB_PASSWORD=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
gcloud sql users create ledgerlens --instance=ledgerlens-db --password="$DB_PASSWORD"

# Cloud Run mounts the instance's Unix socket under /cloudsql.
printf 'postgresql+psycopg://ledgerlens:%s@/ledgerlens?host=/cloudsql/%s:%s:ledgerlens-db' "$DB_PASSWORD" "$PROJECT_ID" "$REGION" | \
  gcloud secrets create ledgerlens-database-url --data-file=-
```

Connection budget: each API instance holds up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` (default 10)
connections per uvicorn worker. At `--max-instances=10` and `WEB_CONCURRENCY=2` that is 200, plus
the workers; size the instance tier or lower the pool settings to match.

### Secrets

```bash
python -m app.cli generate-secrets   # prints SECRET_KEY=... and RECEIPT_SIGNING_SEED=...
printf '%s' '<SECRET_KEY value>'           | gcloud secrets create ledgerlens-session-key --data-file=-
printf '%s' '<RECEIPT_SIGNING_SEED value>' | gcloud secrets create ledgerlens-receipt-seed --data-file=-

for s in ledgerlens-session-key ledgerlens-receipt-seed ledgerlens-database-url; do
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor"
done
```

**Keep the receipt seed forever.** Every receipt ever issued is verified against its public key. To
rotate it, publish the old public key alongside the new one before switching.

## Google Cloud AI and data services

Each is optional: with its setting empty, the local equivalent runs. All calls use the service account;
there are no API keys.

| Service | What LedgerLens uses it for | Setting |
| --- | --- | --- |
| Gemini on Vertex AI | Memo drafting, critic, explanations, co-pilot reasoning, scan extraction | `LLM_MODE=vertex` |
| Pub/Sub | Domain events (created, scored, fraud flagged, decided, KFS accepted) from the outbox | `EVENT_SINKS=log,pubsub`, `PUBSUB_TOPIC` |
| BigQuery | Portfolio analytics over the event stream (Pub/Sub BigQuery subscription) | `ANALYTICS_BACKEND=bigquery`, `BIGQUERY_PROJECT` |
| Document AI | Scanned and photographed statements (Form Parser or Bank Statement Parser) | `DOCUMENT_AI_PROCESSOR` |
| Vertex AI Search | Policy and procedure retrieval for staff and the co-pilot | `VERTEX_SEARCH_SERVING_CONFIG` |
| Vertex AI endpoint | Serving the gradient-boosted challenger model | `VERTEX_RISK_ENDPOINT` |

```bash
# Pub/Sub → BigQuery (see analytics/bigquery/README.md for the table schema and views)
gcloud pubsub topics create ledgerlens-events
bq mk --dataset $PROJECT_ID:ledgerlens
bq mk --table $PROJECT_ID:ledgerlens.events subscription_name:STRING,message_id:STRING,publish_time:TIMESTAMP,data:JSON,attributes:JSON
gcloud pubsub subscriptions create ledgerlens-events-bq --topic=ledgerlens-events \
  --bigquery-table=$PROJECT_ID:ledgerlens.events --write-metadata
sed "s/\${PROJECT}/$PROJECT_ID/g" analytics/bigquery/views.sql | bq query --use_legacy_sql=false

# Document AI: create a Form Parser processor in the console (Document AI → Create processor),
# then note its resource name: projects/<n>/locations/us/processors/<id>

# Vertex AI Search: create a data store from a Cloud Storage bucket holding your credit policy manual
# and circulars, attach a search app, and use its serving config:
# projects/<n>/locations/global/collections/default_collection/engines/<app>/servingConfigs/default_search

# Vertex AI endpoint for the challenger model (scikit-learn prebuilt container)
gsutil cp app/risk/artifacts/risk_model.joblib gs://$PROJECT_ID-models/risk-gbm-v1/model.joblib
gcloud ai models upload --region=$REGION --display-name=risk-gbm-v1 \
  --container-image-uri=asia-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-5:latest \
  --artifact-uri=gs://$PROJECT_ID-models/risk-gbm-v1
gcloud ai endpoints create --region=$REGION --display-name=ledgerlens-risk
gcloud ai endpoints deploy-model <endpoint-id> --region=$REGION --model=<model-id> --display-name=risk-gbm-v1 \
  --machine-type=n1-standard-2 --min-replica-count=1
```

Then add the settings to both Cloud Run services, for example:
`--update-env-vars=EVENT_SINKS=log,pubsub,PUBSUB_TOPIC=projects/$PROJECT_ID/topics/ledgerlens-events,ANALYTICS_BACKEND=bigquery,DOCUMENT_AI_PROCESSOR=...,VERTEX_SEARCH_SERVING_CONFIG=...,VERTEX_RISK_ENDPOINT=...`

## Optional: Model Armor template

```bash
gcloud model-armor templates create ledgerlens-guard --location=$REGION \
  --pi-and-jailbreak-filter-settings-enforcement=enabled \
  --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE \
  --malicious-uri-filter-settings-enforcement=enabled
```

Then set `MODEL_ARMOR_TEMPLATE` to the full template resource name. The local heuristic layer runs
either way; Model Armor is a second, managed layer, not a replacement.

## Deploy

```bash
gcloud builds submit --config deploy/cloudbuild.yaml --substitutions=_REGION=$REGION
```

Order: tests, evaluation gates, image build and push, migrations, API, worker. A regression in
tampering recall or memo groundedness fails the build before anything is migrated or deployed.
Migrations must stay backwards compatible with the revision still serving (add columns; drop them a
release later).

## First staff account

```bash
gcloud run jobs create ledgerlens-admin --image=$REGION-docker.pkg.dev/$PROJECT_ID/ledgerlens/api:<sha> \
  --region=$REGION --service-account=$SA --set-cloudsql-instances=$PROJECT_ID:$REGION:ledgerlens-db \
  --set-secrets=DATABASE_URL=ledgerlens-database-url:latest,SECRET_KEY=ledgerlens-session-key:latest,RECEIPT_SIGNING_SEED=ledgerlens-receipt-seed:latest \
  --set-env-vars=APP_ENV=prod,DEMO_MODE=false,PIPELINE_MODE=queue \
  --command=python --args=-m,app.cli,create-user,risk@lender.example,"Risk Lead",risk_admin
gcloud run jobs execute ledgerlens-admin --region=$REGION --update-env-vars=LEDGERLENS_PASSWORD='<a long passphrase>' --wait
```

Or, from a workstation with the Cloud SQL Auth Proxy running, set `DATABASE_URL` locally and run
`python -m app.cli create-user …` directly. Other commands: `set-password`, `disable-user`,
`list-users`, `verify-audit`, `sweep`.

## Access

The API deploys with `--no-allow-unauthenticated`. Borrowers need to reach it, so for a public launch
put an external HTTPS load balancer in front with Cloud Armor (edge rate limits, OWASP rules), and
put Identity-Aware Proxy in front of staff access until staff SSO and MFA exist.

## Operations

- **Health**: `/api/healthz` (liveness, no dependencies) and `/api/readyz` (database reachable, receipt
  key loaded). Configure the latter as the Cloud Run startup probe.
- **Logs**: single-line JSON on stdout, parsed by Cloud Logging. Every line carries `requestId`; 500
  responses return the same id to the user. Payloads are never logged.
- **Queue depth**: the risk console shows `pipeline_jobs` by state. Alert when `queued` stays above zero
  for more than a few minutes (worker down) or `failed` grows.
- **Audit**: `python -m app.cli verify-audit` exits non-zero if the chain is broken. Run it on a schedule.
- **Backups**: Cloud SQL automated backups with point-in-time recovery (see instance flags above).
- **Retention**: the worker deletes cases older than `CASE_RETENTION_DAYS` hourly.
