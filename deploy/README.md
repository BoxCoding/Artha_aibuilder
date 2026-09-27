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
for role in roles/aiplatform.user roles/cloudsql.client; do
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
