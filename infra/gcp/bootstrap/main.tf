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
variable "state_bucket" {
  description = "Terraform state bucket name; defaults to PROJECT_ID-NAME-tfstate."
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
data "google_project" "project" {}
locals {
  site_id            = coalesce(var.site_id, var.project_id)
  state_bucket       = coalesce(var.state_bucket, "${var.project_id}-${var.name}-tfstate")
  secrets            = toset(["DATABASE_URL", "ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_ALLOWED_EMAILS", "ADMIN_PASSWORD_HASH", "AQICN_API_TOKEN", "OPENROUTER_API_KEY", "AGENT_SERVICE_TOKEN"])
  runtime_identities = toset(["api", "worker", "migrate", "scheduler", "planner", "agent"])
  # planner runs the scheduled planning job; agent is the Agent Runtime identity;
  # cloudbuild runs automated deployments from Cloud Build triggers.
  telemetry_roles       = ["roles/telemetry.tracesWriter", "roles/telemetry.metricsWriter"]
  cloudbuild_principals = toset(["cloudbuild", "default"])
  cloudbuild_roles = [
    "roles/run.admin",
    "roles/cloudscheduler.admin",
    "roles/secretmanager.admin",
    "roles/artifactregistry.admin",
    "roles/storage.admin",
    "roles/firebase.admin",
    "roles/aiplatform.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/logging.logWriter",
  ]
  project_roles = merge(
    { for pair in setproduct(["api", "worker", "planner", "agent"], local.telemetry_roles) : "${pair[0]}-${pair[1]}" => { identity = pair[0], role = pair[1] } },
    { for pair in setproduct(local.cloudbuild_principals, local.cloudbuild_roles) : "${pair[0]}-${pair[1]}" => { identity = pair[0], role = pair[1] } },
    {
      planner-aiplatform = { identity = "planner", role = "roles/aiplatform.user" }
      agent-aiplatform   = { identity = "agent", role = "roles/aiplatform.user" }
      agent-logging      = { identity = "agent", role = "roles/logging.logWriter" }
    }
  )
}
resource "google_project_service" "api" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "storage.googleapis.com", "firebase.googleapis.com", "firebasehosting.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com", "aiplatform.googleapis.com", "telemetry.googleapis.com", "cloudtrace.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com"])
  service            = each.key
  disable_on_destroy = false
}
resource "google_storage_bucket" "state" {
  name                        = local.state_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.name
  format        = "DOCKER"
  depends_on    = [google_project_service.api]
}
resource "google_service_account" "runtime" {
  for_each     = setunion(local.runtime_identities, toset(["cloudbuild"]))
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
  member   = each.value.identity == "default" ? "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com" : "serviceAccount:${google_service_account.runtime[each.value.identity].email}"
}
resource "google_storage_bucket_iam_member" "cloudbuild_state" {
  for_each = local.cloudbuild_principals
  bucket   = google_storage_bucket.state.name
  role     = "roles/storage.objectAdmin"
  member   = each.key == "default" ? "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com" : "serviceAccount:${google_service_account.runtime["cloudbuild"].email}"
}
resource "google_artifact_registry_repository_iam_member" "cloudbuild_images" {
  for_each   = local.cloudbuild_principals
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.writer"
  member     = each.key == "default" ? "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com" : "serviceAccount:${google_service_account.runtime["cloudbuild"].email}"
}
resource "google_service_account_iam_member" "cloudbuild_runtime" {
  for_each           = { for pair in setproduct(local.cloudbuild_principals, local.runtime_identities) : "${pair[0]}-${pair[1]}" => { principal = pair[0], identity = pair[1] } }
  service_account_id = google_service_account.runtime[each.value.identity].name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value.principal == "default" ? "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com" : "serviceAccount:${google_service_account.runtime["cloudbuild"].email}"
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
    project_id   = var.project_id
    region       = var.region
    name         = var.name
    site_id      = google_firebase_hosting_site.app.site_id
    state_bucket = google_storage_bucket.state.name
    identities   = { for key, sa in google_service_account.runtime : key => sa.email }
    secret_ids   = { for key, secret in google_secret_manager_secret.config : key => secret.secret_id }
  }
}
output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/app"
}
