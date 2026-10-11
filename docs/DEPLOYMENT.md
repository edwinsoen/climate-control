# Deployment

## Local container

Use the [README setup](../README.md) and `docker compose up --build -d`. The app listens only on `127.0.0.1:8080` by default and stores its SQLite database in `climate-data`. To expose it on a home network, place it behind an HTTPS reverse proxy, change `APP_URL`, and update the port binding. The proxy must preserve the original Origin header; the service does not trust forwarded client headers.

Compose runs migrations before starting the service and enables the local scheduler. Run one application container with SQLite. PostgreSQL is required for multiple instances.

## Cloud Run

Use PostgreSQL for durable state and Cloud Scheduler for polling. Keep `SCHEDULER_ENABLED=false` and `AUTO_MIGRATE=false` on the service. Cloud Run may scale to zero between scheduler requests; no background worker is required.

The commands below are deployment templates. Replace placeholder project, region, image, database, and secret values. Provisioning these resources incurs normal GCP charges.

### Resources and image

```sh
PROJECT=your-project
REGION=asia-southeast1
SERVICE=climate-control
INSTANCE=climate-db
IMAGE="$REGION-docker.pkg.dev/$PROJECT/climate/app:v0.1.0"
RUNTIME_SA="climate-runtime@$PROJECT.iam.gserviceaccount.com"
SCHEDULER_SA="climate-scheduler@$PROJECT.iam.gserviceaccount.com"

gcloud config set project "$PROJECT"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com
gcloud artifacts repositories create climate --repository-format=docker --location="$REGION"
gcloud builds submit --tag "$IMAGE" .

gcloud iam service-accounts create climate-runtime
gcloud iam service-accounts create climate-scheduler
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/cloudsql.client
```

Create a PostgreSQL database and user, either in an existing instance or a new Cloud SQL instance:

```sh
gcloud sql instances create "$INSTANCE" --database-version=POSTGRES_16 \
  --tier=db-f1-micro --region="$REGION"
gcloud sql databases create climate --instance="$INSTANCE"
```

Create a built-in database user named `climate` and set its password in the Cloud SQL console. [User configuration reference](https://docs.cloud.google.com/sql/docs/postgres/create-manage-users)

Create Secret Manager secrets for `climate-encryption-key`, `climate-database-url`, `climate-google-client-id`, and `climate-google-client-secret`. Use the console or `gcloud secrets create NAME --data-file=FILE`; keep temporary secret files outside the repository. Grant the runtime service account `roles/secretmanager.secretAccessor` on these secrets.

The database URL for Cloud SQL's Unix socket is:

```text
postgresql+psycopg://climate:URL_ENCODED_PASSWORD@/climate?host=/cloudsql/PROJECT:REGION:INSTANCE
```

Generate the encryption key with `python -m climate_control.cli key`. Use a web Google OAuth client for sign-in and configure its consent screen. Set allowed household emails explicitly.

### Migrate, then deploy

Create a migration job using the same image, database, and encryption key. Its administrator hash is unused; provide Google configuration so settings validation matches production.

```sh
SECRETS="ENCRYPTION_KEY=climate-encryption-key:1,DATABASE_URL=climate-database-url:1,GOOGLE_CLIENT_ID=climate-google-client-id:1,GOOGLE_CLIENT_SECRET=climate-google-client-secret:1"

gcloud run jobs create climate-migrate --image="$IMAGE" --region="$REGION" \
  --service-account="$RUNTIME_SA" --set-cloudsql-instances="$PROJECT:$REGION:$INSTANCE" \
  --set-secrets="$SECRETS" \
  --set-env-vars="GOOGLE_ALLOWED_EMAILS=owner@example.com" \
  --command=python --args=-m,climate_control.cli,migrate \
  --tasks=1 --max-retries=0
gcloud run jobs execute climate-migrate --region="$REGION" --wait

gcloud run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
  --service-account="$RUNTIME_SA" --add-cloudsql-instances="$PROJECT:$REGION:$INSTANCE" \
  --set-secrets="$SECRETS" \
  --set-env-vars="APP_URL=https://YOUR_SERVICE_URL,GOOGLE_ALLOWED_EMAILS=owner@example.com,SCHEDULER_ENABLED=false,SCHEDULER_SERVICE_ACCOUNT=$SCHEDULER_SA,SCHEDULER_AUDIENCE=https://YOUR_SERVICE_URL/internal/poll,AUTO_MIGRATE=false" \
  --no-invoker-iam-check --min-instances=0 --max-instances=2 \
  --concurrency=20 --timeout=1800 --memory=512Mi --cpu=1
```

For the first deployment, supply a temporary HTTPS placeholder for `APP_URL` and `SCHEDULER_AUDIENCE`, obtain the service URL from the deployment output, then update both values before using the app. Register that exact URL plus `/auth/google/callback` in the Google OAuth client. A custom domain can be supplied directly if already configured.

The service allows requests to reach the login page; application authentication protects accounts and devices. Disabling the Cloud Run invoker IAM check lets the app verify the scheduler's complete OIDC token itself. Keep the scheduler audience equal to the full `/internal/poll` URL in both configurations. [Cloud Run public access configuration](https://docs.cloud.google.com/run/docs/authenticating/public)

### Schedule polling

```sh
SERVICE_URL=https://YOUR_SERVICE_URL
gcloud scheduler jobs create http climate-poll --location="$REGION" \
  --schedule='* * * * *' --uri="$SERVICE_URL/internal/poll" --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA" \
  --oidc-token-audience="$SERVICE_URL/internal/poll" \
  --attempt-deadline=1800s --max-retry-attempts=2
```

The person creating the job needs permission to act as the scheduler service account. Keep the Cloud Scheduler service agent's `roles/cloudscheduler.serviceAgent` role so it can mint tokens. The application validates token signature, issuer, audience, expiry, verified email, and the exact configured scheduler identity. [Scheduler authentication documentation](https://docs.cloud.google.com/scheduler/docs/http-target-auth)

### Check operation and updates

Check `/health/ready`, sign in, connect a VeSync account, and compare its device state with the VeSync app. Confirm the retrieval time advances after a scheduler execution, with no browser open. Check account errors and scheduler execution logs for failures.

For updates, build a new image tag, update and execute the migration job, then deploy the service with that image. Back up the database and retain the encryption key. Restore both together. Avoid running incompatible schema migrations alongside old revisions; follow the applicable upgrade notes below.

Persistent leases protect account refreshes during retries and revision overlap. Each account poll times out after four minutes; a request can process three accounts at once. The 30-minute request deadline accommodates ordinary household deployments.

## Optional outdoor monitoring

Configure `AQICN_API_TOKEN` using the [README instructions](../README.md#outdoor-air-quality). On Cloud Run, keep the token in Secret Manager and bind `AQICN_API_TOKEN=YOUR_SECRET:VERSION`. Local Compose reads it from `.env`. A token from OpenAQ is incompatible. The backend needs outbound HTTPS access to `api.waqi.info`; AQICN uses the token in its query string, so request URL logging is disabled. Users search for their city/station inside the app; no location environment variable or geocoding service is used.

Apply migrations through `0007` before running this version. The migration clears old OpenAQ selection/search state, retains legacy records separately, and creates AQICN tables. Stop the old backend before migration locally. Re-select a station in the app after upgrading. The local scheduler or `/internal/poll` Cloud Scheduler job records fetched AQI observations even with no devices connected. Current fetches default to five minutes (`OUTDOOR_POLL_INTERVAL_SECONDS`); measurement staleness defaults to three hours (`OUTDOOR_STALE_AFTER_SECONDS`). No automatic backfill runs, because the public station-feed API has no documented history endpoint.

Check station name, overall AQI and pollutant indices, agency attribution, inline measurement ages, and errors after a scheduler execution. `/api/outdoor` also exposes the last successful retrieval timestamp. `/health/ready` verifies migration readiness. Scheduler JSON includes `outdoor_checked`; fixed log outcomes exclude tokens and locations. Selection and recorded history persist across restarts. Changing the token requires a service restart/deployment; changing or removing the selected station is immediate. Invalid-token errors pause polling until credentials or station selection change. AQICN use remains within this app's private household scope; consult its published data-use terms before any public/paid deployment or data redistribution.

Browser location requires HTTPS in deployed environments; localhost is supported for development. City/station search remains available when browser positioning fails. The unresolved local Chrome issue is recorded in [browser location behavior](FOUNDATION.md#browser-location).
