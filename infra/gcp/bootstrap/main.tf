variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "asia-southeast1"
}
variable "name" {
  type    = string
  default = "climate-control"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.name))
    error_message = "Use a lowercase resource prefix of at most 20 characters."
  }
}
variable "site_id" {
  description = "Globally unique Firebase Hosting site ID; defaults to the project ID."
  type        = string
  default     = null
}
provider "google" {
  project = var.project_id
  region  = var.region
}
provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
}
locals {
  site_id = coalesce(var.site_id, var.project_id)
  secrets = toset(["DATABASE_URL", "ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_PASSWORD_HASH", "AQICN_API_TOKEN", "OPENROUTER_API_KEY", "AGENT_SERVICE_TOKEN"])
  # planner runs the scheduled planning job; agent is the Agent Runtime identity.
  telemetry_roles = ["roles/telemetry.tracesWriter", "roles/telemetry.metricsWriter"]
  project_roles = merge(
    { for pair in setproduct(["api", "worker", "planner", "agent"], local.telemetry_roles) : "${pair[0]}-${pair[1]}" => { identity = pair[0], role = pair[1] } },
    {
      planner-aiplatform = { identity = "planner", role = "roles/aiplatform.user" }
      agent-aiplatform   = { identity = "agent", role = "roles/aiplatform.user" }
      agent-logging      = { identity = "agent", role = "roles/logging.logWriter" }
    }
  )
}
resource "google_project_service" "api" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "firebase.googleapis.com", "firebasehosting.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com", "aiplatform.googleapis.com", "telemetry.googleapis.com", "cloudtrace.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com"])
  service            = each.key
  disable_on_destroy = false
}
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  depends_on    = [google_project_service.api]
}
resource "google_service_account" "runtime" {
  for_each     = toset(["api", "worker", "migrate", "scheduler", "planner", "agent"])
  account_id   = "${var.name}-${each.key}"
  display_name = "Climate Control ${each.key}"
  depends_on   = [google_project_service.api]
}
resource "google_secret_manager_secret" "config" {
  for_each  = local.secrets
  secret_id = "${var.name}-${lower(replace(each.key, "_", "-"))}"
  replication {
    auto {}
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}
resource "google_project_iam_member" "runtime" {
  for_each = local.project_roles
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.runtime[each.value.identity].email}"
}
resource "google_secret_manager_secret_iam_member" "agent" {
  # Agent Runtime reads its MCP token and model key from Secret Manager at runtime.
  for_each  = toset(["AGENT_SERVICE_TOKEN", "OPENROUTER_API_KEY"])
  secret_id = google_secret_manager_secret.config[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime["agent"].email}"
}
resource "google_firebase_project" "app" {
  provider   = google-beta
  project    = var.project_id
  depends_on = [google_project_service.api]
}
resource "google_firebase_hosting_site" "app" {
  provider   = google-beta
  project    = var.project_id
  site_id    = local.site_id
  depends_on = [google_firebase_project.app]
}
output "platform" {
  value = {
    project_id = var.project_id
    region     = var.region
    name       = var.name
    site_id    = google_firebase_hosting_site.app.site_id
    identities = { for key, sa in google_service_account.runtime : key => sa.email }
    secret_ids = { for key, secret in google_secret_manager_secret.config : key => secret.secret_id }
  }
}
output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/app"
}
