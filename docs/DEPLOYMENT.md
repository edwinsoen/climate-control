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

Prerequisites: a billed GCP project, Terraform 1.7+, Google Cloud CLI, Docker, Node 24, Firebase CLI, and uv/Invoke. Sign in with `gcloud auth login`, `gcloud auth application-default login`, and `firebase login`; accept Firebase terms. The provisioning identity needs project resource, service-account impersonation, Artifact Registry write, Secret Manager version listing, Cloud Run deployment/job execution, and Hosting release permissions. Project policy must permit public Cloud Run invocation, which the app protects with authentication. The billing plan must support Firebase Hosting's Cloud Run integration.

```sh
cp infra/gcp/bootstrap/terraform.example.tfvars infra/gcp/bootstrap/terraform.tfvars
# Set project_id, and optionally region and globally unique site_id.
terraform -chdir=infra/gcp/bootstrap init
terraform -chdir=infra/gcp/bootstrap plan
terraform -chdir=infra/gcp/bootstrap apply
```

Bootstrap enables APIs (including Agent Platform and the Telemetry API) and creates the registry, six service accounts, eight empty Secret Manager containers, and the Firebase site. It grants trace and metric writers to the API, worker, planner, and Agent Runtime identities, Agent Platform access to the planner identities, and access to the agent secrets only to the Agent Runtime identity. Existing Firebase project/site resources must be imported before apply:

```sh
terraform -chdir=infra/gcp/bootstrap import google_firebase_project.app projects/YOUR_PROJECT
terraform -chdir=infra/gcp/bootstrap import google_firebase_hosting_site.app projects/YOUR_PROJECT/sites/YOUR_SITE
```

Secret containers have `prevent_destroy`. Keep Terraform state access controlled; use a remote backend for shared operation. Terraform state and local input files are ignored by Git.

### Populate secrets once

Find container IDs in the bootstrap `platform.secret_ids` output. Add versions with the console or a file outside this repository:

```sh
gcloud secrets versions add SECRET_ID --project YOUR_PROJECT --data-file /outside/repo/secret-file
```

Required: `DATABASE_URL`, `ENCRYPTION_KEY`, and either `ADMIN_PASSWORD_HASH` or both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. `AQICN_API_TOKEN` is optional. `OPENROUTER_API_KEY` is needed only for an OpenRouter planner, and `AGENT_SERVICE_TOKEN` only for [Agent Runtime](AGENT.md#deploying-to-agent-runtime). Leave unused containers empty. Generate the key and hash with `uv run python -m climate_control.cli key` and `uv run python -m climate_control.cli hash-password`. The key must be Fernet base64 text, not hex. Retain it with database backups. Surrounding whitespace in deployment secret text is trimmed, including a final newline; whitespace inside credentials is preserved.

### Deploy or update

Run from the repository root:

```sh
invoke deploy
```

`uv run invoke deploy` works without a global Invoke installation. Use `invoke deploy --dry-run` to inspect the target and selected secret versions without building or publishing. The task updates bootstrap, selects the newest enabled version of each populated secret from metadata, and pins numeric versions without downloading payloads. It builds the UI and a backend-only `linux/amd64` image, pushes an immutable digest, pauses the schedule and drains existing jobs, updates jobs, runs migrations, activates and checks the API, publishes Hosting with `LICENSE`, checks the Hosting origin, then resumes the schedule. On a failed release after the pause, the schedule stays paused and the ignored input files are ready for a retry; fix the error and rerun the same command. A local lock prevents concurrent deploys from one checkout.

The deploy task applies Terraform automatically. Generated `platform.auto.tfvars.json` and `deployment.auto.tfvars.json` hold non-secret inputs. `APP_URL` defaults to `https://SITE_ID.web.app`; use the runtime Terraform `app_url` variable only for an already connected custom Hosting origin. If Google login is enabled, the allowlist comes from `--allowed-emails`, `GOOGLE_ALLOWED_EMAILS`, the saved Terraform value, or `.env`, in that order. The first interactive run prompts if none exists; noninteractive runs must provide one. The CLI's current Google account is never added implicitly.

The API uses request-based billing, one CPU, 512 MiB, concurrency 20, and at most two instances by default. Jobs run one task, allow no automatic retries, and time out after 30 minutes; the planner Job has 1 GiB. Scheduler uses OAuth to invoke only the worker and planner Jobs.

The planner uses Gemini through Agent Platform by default. To change it, write `infra/gcp/runtime/agent.auto.tfvars.json`, for example `{"agent_provider": "openrouter", "agent_model": "MODEL_ID"}` with an `OPENROUTER_API_KEY` version, or `{"agent_provider": "disabled"}` to omit the planner Job, then rerun `invoke deploy`. The planner receives only `DATABASE_URL` and its model key; the API and worker never receive model or agent credentials. `invoke deploy-agent` moves planning to Agent Runtime; see [agents](AGENT.md#deploying-to-agent-runtime). If Agent Runtime rejects the custom runtime identity, grant the permissions in its custom service account guide. Keep the Cloud Scheduler service-agent role so it can mint tokens. [Scheduled job authentication](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule).

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

Local source upgrades require stopping the backend, backing up SQLite and its encryption key, running `uv run python -m climate_control.cli migrate`, then restarting. Compose handles migration on startup; GCP `invoke deploy` runs the migration Job before switching the API. The backend does not reload automatically.
