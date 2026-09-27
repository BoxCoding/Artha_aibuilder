# Deploying ARTHA

## One-time project setup

```bash
export PROJECT_ID=your-project
export REGION=asia-south1

gcloud services enable run.googleapis.com aiplatform.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com modelarmor.googleapis.com

gcloud artifacts repositories create artha --repository-format=docker --location=$REGION

# Runtime identity: model access only, nothing else.
gcloud iam service-accounts create artha-run --display-name="ARTHA Cloud Run"
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:artha-run@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

python -c "import secrets; print(secrets.token_urlsafe(48))" | \
  gcloud secrets create artha-session-key --data-file=-
gcloud secrets add-iam-policy-binding artha-session-key \
  --member="serviceAccount:artha-run@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## Optional: Model Armor template

```bash
gcloud model-armor templates create artha-guard --location=$REGION \
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

The build runs the test suite and the evaluation gates before it pushes an image, so a regression in
tampering recall or memo groundedness fails the deploy.

## Notes

- The service deploys with `--no-allow-unauthenticated`. For a public demo, front it with Identity-Aware
  Proxy or add `--allow-unauthenticated` deliberately, understanding that the demo sign-in is not real
  authentication.
- `--workers 1` is intentional: the prototype's case store is in-memory per instance. The store interface
  in `app/domain/store.py` is the seam for a Firestore adapter, which is what makes multi-instance work.
- Set `--min-instances=1` before a judged demo so the first case does not pay cold-start cost.
