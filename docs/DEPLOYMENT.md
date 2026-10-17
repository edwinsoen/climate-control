# Deployment

## Local container

Follow the [README](../README.md#run-locally), then run `docker compose up --build -d`. Compose runs migrations and the local scheduler, binds to `127.0.0.1:8080`, and persists SQLite in the `climate-data` volume. Run one application container with SQLite. For home-network access, use an HTTPS reverse proxy, update `APP_URL` and the port binding, and preserve the original `Origin` header.

## GCP setup

Firebase Hosting serves the UI and rewrites `/api`, `/auth`, `/health`, and `/mcp` to a Cloud Run API. Cloud Scheduler starts a separate `tick` Cloud Run Job every minute for collection, automation, and cleanup, and a `plan` Job every five minutes for the [rule planner](AGENT.md). A migration Job updates the schema before each new API image is activated. All runtimes share an **external PostgreSQL database**; Terraform does not provision a database or private network. Cloud Run must reach the database over public TLS egress. For example:

```text
postgresql+psycopg://USER:URL_ENCODED_PASSWORD@DB_HOST:5432/DATABASE?sslmode=require&connect_timeout=10
```

Use `sslmode=verify-full` if supported by your database certificate setup.

### Bootstrap once

Prerequisites: a billed GCP project, Terraform 1.7+, Google Cloud CLI, and Firebase CLI (plus Docker, Node 24, and uv/Invoke when running local releases). Sign in with `gcloud auth login`, `gcloud auth application-default login`, and `firebase login`; accept Firebase terms. The provisioning identity needs project resource, service-account and IAM administration, Artifact Registry, Cloud Storage, Secret Manager, Cloud Run, and Hosting permissions. Project policy must permit public Cloud Run invocation, which the app protects with authentication. The billing plan must support Firebase Hosting's Cloud Run integration.

```sh
cp infra/gcp/bootstrap/terraform.example.tfvars infra/gcp/bootstrap/terraform.tfvars
# Set project_id, and optionally region and globally unique site_id.
terraform -chdir=infra/gcp/bootstrap init
terraform -chdir=infra/gcp/bootstrap plan
terraform -chdir=infra/gcp/bootstrap apply
```

Bootstrap enables APIs (including Cloud Build, Cloud Storage, Agent Platform, and the Telemetry API) and creates the Artifact Registry repository, a versioned GCS bucket (`PROJECT_ID-climate-control-tfstate`) for Terraform state, seven service accounts (`api`, `worker`, `migrate`, `scheduler`, `planner`, `agent`, and `cloudbuild`), nine empty Secret Manager containers, and the Firebase Hosting site. It grants trace and metric writers to the API, worker, planner, and Agent Runtime identities, Agent Platform access to the planner identities, access to the agent secrets only to the Agent Runtime identity, and deployment permissions to `climate-control-cloudbuild` and the default Cloud Build service account so Cloud Build can reconcile infrastructure and auto-provision internal secrets on first deploy without downloading external secret values. Existing Firebase project/site resources must be imported before manual local apply:

```sh
terraform -chdir=infra/gcp/bootstrap import google_firebase_project.app projects/YOUR_PROJECT
terraform -chdir=infra/gcp/bootstrap import google_firebase_hosting_site.app projects/YOUR_PROJECT/sites/YOUR_SITE
```

Secret containers and the Terraform state bucket have `prevent_destroy`. Keep Terraform state access controlled. Local bootstrap state and input files are ignored by Git; runtime state is stored in the `PROJECT_ID-climate-control-tfstate` GCS bucket so Cloud Build and local commands share the same state.

### Populate secrets in Secret Manager

All secrets live in **Secret Manager**—never in [`cloudbuild.yaml`](../cloudbuild.yaml), Cloud Build trigger substitutions, or Git. The `bootstrap` step creates the nine Secret Manager containers (`climate-control-<secret-name>` by default) automatically. On the first deployment, only external values (`DATABASE_URL` and login credentials) need to be added by the operator; internal machine secrets (`ENCRYPTION_KEY` and `AGENT_SERVICE_TOKEN`) are generated and wired up automatically if their Secret Manager containers have no enabled version yet.

Add external secret versions in the Secret Manager console or from a file outside this repository:

```sh
gcloud secrets versions add SECRET_ID --project YOUR_PROJECT --data-file /outside/repo/secret-file
```

| Secret key | Default Secret Manager ID | Requirement |
| --- | --- | --- |
| `DATABASE_URL` | `climate-control-database-url` | Required (`postgresql+psycopg://...`) |
| `GOOGLE_CLIENT_ID` | `climate-control-google-client-id` | Required for Google OAuth (populate together with `GOOGLE_CLIENT_SECRET` and `GOOGLE_ALLOWED_EMAILS`) |
| `GOOGLE_CLIENT_SECRET` | `climate-control-google-client-secret` | Required for Google OAuth (populate together with `GOOGLE_CLIENT_ID` and `GOOGLE_ALLOWED_EMAILS`) |
| `GOOGLE_ALLOWED_EMAILS` | `climate-control-google-allowed-emails` | Required for Google OAuth (comma-separated allowed emails, e.g. `owner@example.com`) |
| `ADMIN_PASSWORD_HASH` | `climate-control-admin-password-hash` | Required if Google OAuth is not configured (`uv run python -m climate_control.cli hash-password`) |
| `AQICN_API_TOKEN` | `climate-control-aqicn-api-token` | Optional outdoor AQI station token |
| `OPENROUTER_API_KEY` | `climate-control-openrouter-api-key` | Required only when `_AGENT_PROVIDER=openrouter` |
| `ENCRYPTION_KEY` | `climate-control-encryption-key` | Auto-generated on first deploy if empty (Fernet base64 text; retain with database backups) |
| `AGENT_SERVICE_TOKEN` | `climate-control-agent-service-token` | Auto-generated and registered in PostgreSQL (`access_tokens`) on first deploy if empty |

Leave unused optional containers empty. Retain `ENCRYPTION_KEY` with database backups. Surrounding whitespace in deployment secret text is trimmed, including a final newline; whitespace inside credentials is preserved. On every release, `scripts/deploy_gcp.py` inspects Secret Manager version metadata (`gcloud secrets versions list`) and pins the highest `ENABLED` version number of each populated secret without downloading secret values. After adding or rotating a secret version, trigger a build (or push to `main`) to bind the new version.

### Deploy on push to `main` with Cloud Build

[`cloudbuild.yaml`](../cloudbuild.yaml) builds and deploys the application and its Agent Runtime planner automatically on push to `main`. It contains no secrets or project-specific identifiers; Cloud Build supplies `$PROJECT_ID` and `$BUILD_ID` automatically, and non-default environment settings are configured on the trigger.

1. **Connect the GitHub repository** in **Cloud Build → Repositories** (2nd gen or 1st gen) in your deployment region (default `asia-southeast1`).
2. **Create a push trigger** in **Cloud Build → Triggers** (or with `gcloud builds triggers create github`):
   - **Event:** Push to a branch
   - **Source:** Your connected repository, branch pattern `^main$`
   - **Configuration:** Cloud Build configuration file (`/cloudbuild.yaml`)
   - **Service account:** `climate-control-cloudbuild@YOUR_PROJECT.iam.gserviceaccount.com` (`CLOUD_LOGGING_ONLY` is already set in `cloudbuild.yaml`)
3. **Configure trigger substitution variables** (only needed when overriding defaults):
   - `_SITE_ID`: Firebase Hosting site ID if different from `$PROJECT_ID`.
   - `_REGION`: GCP region (default `asia-southeast1`).
   - `_NAME`: resource prefix (default `climate-control`).
   - `_STATE_BUCKET`: runtime Terraform state bucket override (default `${PROJECT_ID}-${_NAME}-tfstate`).
   - `_APP_URL`: custom HTTPS origin if not using `https://SITE_ID.web.app`.
   - `_AGENT_PROVIDER`: `gemini` (default on first deploy; empty preserves the currently deployed provider), `openrouter`, or `disabled`.
   - `_AGENT_MODEL`: model ID override (default uses the provider alias).
   - `_AGENT_RUNTIME_RESOURCE`: optional override for the `projects/*/locations/*/reasoningEngines/*` resource name (by default, the deployment creates or updates the Agent Runtime resource automatically and persists its resource name in Terraform state).

   Equivalent CLI command to create the trigger:

   ```sh
   gcloud builds triggers create github \
     --project=YOUR_PROJECT \
     --region=asia-southeast1 \
     --name=climate-control-main \
     --repo-name=climate-control \
     --repo-owner=YOUR_GITHUB_OWNER \
     --branch-pattern='^main$' \
     --build-config=cloudbuild.yaml \
     --service-account="projects/YOUR_PROJECT/serviceAccounts/climate-control-cloudbuild@YOUR_PROJECT.iam.gserviceaccount.com" \
     --substitutions="_SITE_ID=YOUR_SITE"
   ```

Each build compiles the frontend and stages the Firebase CLI and Terraform 1.9 binary in a shared build volume concurrently with building the `linux/amd64` backend image (and staging the image's `adk` CLI). The `bootstrap` step runs `python3 -m scripts.deploy_gcp --bootstrap-only`, which creates the `${PROJECT_ID}-${_NAME}-tfstate` GCS bucket if missing, initializes `infra/gcp/bootstrap` against that bucket (automatically importing any pre-existing registry, service accounts, secret containers, and Firebase site), and applies `bootstrap`. After `push-backend` pushes the image, the `deploy` step runs `python3 -m scripts.deploy_gcp --skip-build`, which initializes `infra/gcp/runtime` against the same GCS state bucket (automatically importing any pre-existing Cloud Run service, Jobs, and Cloud Scheduler jobs), resolves the newest enabled secret versions from metadata (generating `ENCRYPTION_KEY` if missing) and the pushed image's `@sha256` digest, pauses Cloud Scheduler and drains active job executions, updates the `worker`, `planner`, and `migrate` Jobs while retaining the running API image and secret versions, executes `migrate` (and registers a newly minted read-scoped `AGENT_SERVICE_TOKEN` hash in PostgreSQL and stores the token in Secret Manager if `climate-control-agent-service-token` was empty), updates and readiness-checks the API, publishes Firebase Hosting with `LICENSE`, verifies `/health/ready` on the Hosting origin, deploys or updates the rule planner on [Agent Runtime](AGENT.md#deploying-to-agent-runtime) (`adk deploy agent_engine`), and wires `agent_runtime_resource` into the `planner` Job and `api` service while resuming the schedules. On a failed release after the pause, schedules remain paused until the next successful build or `invoke deploy`.

To test [`cloudbuild.yaml`](../cloudbuild.yaml) directly from a local checkout before pushing to `main`:

```sh
gcloud builds submit . \
  --project=YOUR_PROJECT \
  --region=asia-southeast1 \
  --config=cloudbuild.yaml \
  --substitutions="_SITE_ID=YOUR_SITE"
```

(`_SITE_ID` can be omitted if it matches `YOUR_PROJECT`.) [`.gcloudignore`](../.gcloudignore) excludes `.env`, local Terraform state/variable files, `.venv`, and `node_modules` so a manual submission runs in the same clean workspace as a branch trigger.

### Manual local deploy

You can also deploy or preview from a local checkout:

```sh
invoke deploy
```

`uv run invoke deploy` works without a global Invoke installation. Use `invoke deploy --dry-run` to inspect the target and selected secret versions without building or publishing. Local runs update `infra/gcp/bootstrap`, configure the `PROJECT_ID-climate-control-tfstate` GCS backend for `infra/gcp/runtime` (automatically importing any pre-existing runtime resources into GCS state), and execute the same release sequence. A local lock prevents concurrent deploys from one checkout.

The API uses request-based billing, one CPU, 512 MiB, concurrency 20, and at most two instances by default. Jobs run one task, allow no automatic retries, and time out after 30 minutes; the planner Job has 1 GiB. Scheduler uses OAuth to invoke only the worker and planner Jobs.

The planner uses Gemini through Agent Platform by default and is deployed to Agent Runtime automatically during release (`invoke deploy-agent` can also update the Agent Runtime planner standalone). To change the model provider, set `_AGENT_PROVIDER` / `_AGENT_MODEL` on the Cloud Build trigger (or write `infra/gcp/runtime/agent.auto.tfvars.json` locally, for example `{"agent_provider": "openrouter", "agent_model": "MODEL_ID"}` with an `OPENROUTER_API_KEY` version, or `{"agent_provider": "disabled"}` to omit the planner Job). The planner receives only `DATABASE_URL` and its model key; the API and worker never receive model or agent credentials; see [agents](AGENT.md#deploying-to-agent-runtime). If Agent Runtime rejects the custom runtime identity, grant the permissions in its custom service account guide. Keep the Cloud Scheduler service-agent role so it can mint tokens. [Scheduled job authentication](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule).

## Authentication and operations

Register the exact `APP_URL` as an authorized JavaScript origin in the Google OAuth web client, and `APP_URL/auth/google/callback` as an authorized redirect URI. For the Firebase site `es-climate-control`, these are `https://es-climate-control.web.app` and `https://es-climate-control.web.app/auth/google/callback`; login begins at `/auth/google/login`. Use the same canonical host for UI and API sessions. Firebase forwards the `__session` cookie; CSRF checks, PKCE, nonce, and replay protection remain active. Sessions using older cookie names must sign in again. [Firebase cookie forwarding](https://firebase.google.com/docs/hosting/manage-cache).

Firebase has a 60-second proxy limit, so deployed interactive provider operations have a 50-second budget, including command queue waits. A timeout returns a sanitized 504 and preserves cached state; an uncertain command may have reached the device, so inspect observed status before retrying. The worker has longer budgets. [Hosting request timeout](https://firebase.google.com/docs/hosting/cloud-run).

Check `/health/ready`, sign-in, account connection, and controls at the Hosting URL. To check one worker execution:

```sh
gcloud run jobs execute climate-control-worker --project YOUR_PROJECT --region asia-southeast1 --wait
```

Use your configured region if different. Tick results include `checked`, `refreshed`, `outdoor_checked`, and `automation_checked`; planner results include `planning_checked`, `planning_completed`, and `planning_failed`. Traces for agent runs and MCP calls appear in Cloud Trace. Scheduler success confirms launch; inspect Cloud Run execution completion and logs for failures. Cached retrieval times should advance with no browser open. If migration execution fails, inspect that Job's logs, database reachability, and Secret Manager versions; rerun `invoke deploy` after correcting the issue.

Back up PostgreSQL and retain the encryption key before upgrades. The old API remains available during migration, so schema changes must be compatible with it until replacement; take it offline for an incompatible migration. Image and UI rollback also requires schema compatibility. A Hosting rollback alone does not roll back API or worker code.

When moving from the old combined Cloud Run service, pause its scheduler and drain pending requests before enabling the new one. Reuse its database URI and encryption key, register the Firebase callback, validate the new deployment, then stop the old service. Legacy `/internal/poll` exists only in local combined mode when explicitly configured.

To validate Terraform locally:

```sh
terraform fmt -check -recursive infra/gcp
terraform -chdir=infra/gcp/bootstrap validate
terraform -chdir=infra/gcp/runtime validate
terraform -chdir=infra/gcp/runtime test
```

## Upgrade notes

- `0007` replaces OpenAQ selection with AQICN. Re-select a station; legacy history remains separate and expires under normal retention. Set `AQICN_API_TOKEN` in Secret Manager, then redeploy. The selected station collects every five minutes by default, without historical backfill.
- `0008` adds observed purifier settings to history. Existing air-quality readings remain, but past settings are unknown; new settings appear after valid polls or readbacks.
- `0009` adds automation and planning records. Existing and new devices start with automation disabled. See [automation](AUTOMATION.md).
- `0010` adds MCP access tokens, planner run records, and revision authors. Existing revisions have no author. Rerun the bootstrap (through `invoke deploy`) to create the planner identities, roles, and agent secrets.
- `0013` adds Xiaomi Home account support (`(provider, email, country_code)` account uniqueness and 3-state display warning modes) plus indoor `temperature` and `humidity` columns in `device_readings`.

Local source upgrades require stopping the backend, backing up SQLite and its encryption key, running `uv run python -m climate_control.cli migrate`, then restarting. Compose handles migration on startup; GCP `invoke deploy` runs the migration Job before switching the API. The backend does not reload automatically.
