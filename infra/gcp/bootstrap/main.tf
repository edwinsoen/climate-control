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
  secrets = toset(["DATABASE_URL", "ENCRYPTION_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_PASSWORD_HASH", "AQICN_API_TOKEN"])
}
resource "google_project_service" "api" {
  for_each           = toset(["run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "artifactregistry.googleapis.com", "cloudbuild.googleapis.com", "firebase.googleapis.com", "firebasehosting.googleapis.com", "iam.googleapis.com", "cloudresourcemanager.googleapis.com", "serviceusage.googleapis.com"])
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
  for_each     = toset(["api", "worker", "migrate", "scheduler"])
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
