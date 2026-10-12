# platform is supplied by platform.auto.tfvars.json exported from bootstrap.
image = "asia-southeast1-docker.pkg.dev/your-project/climate-control/app@sha256:REPLACE_WITH_DIGEST"
secret_versions = {
  DATABASE_URL         = "1"
  ENCRYPTION_KEY       = "1"
  GOOGLE_CLIENT_ID     = "1"
  GOOGLE_CLIENT_SECRET = "1"
  # ADMIN_PASSWORD_HASH = "1" # Optional alternative to Google login.
  # AQICN_API_TOKEN = "1"
}
google_allowed_emails = "owner@example.com"
api_enabled           = false
scheduler_paused      = true
