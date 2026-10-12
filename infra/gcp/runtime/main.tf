variable "platform" {
  type = object({
    project_id = string
    region     = string
    name       = string
    site_id    = string
    identities = map(string)
    secret_ids = map(string)
  })
}
variable "image" {
  description = "Backend image pinned to its sha256 digest."
  type        = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Supply an immutable image digest."
  }
}
variable "api_image" {
  description = "Retain the current API digest while new jobs run migrations; defaults to image."
  type        = string
  default     = null
  validation {
    condition     = var.api_image == null || can(regex("@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "The retained API image must use an immutable digest."
  }
}
variable "api_secret_versions" {
  description = "Retain API secret versions during migration; defaults to secret_versions."
  type        = map(string)
  default     = null
}
variable "secret_versions" {
  description = "Numeric versions to bind. Required: DATABASE_URL and ENCRYPTION_KEY plus API authentication."
  type        = map(string)
  validation {
    condition     = alltrue([for version in values(var.secret_versions) : can(regex("^[1-9][0-9]*$", version))]) && contains(keys(var.secret_versions), "DATABASE_URL") && contains(keys(var.secret_versions), "ENCRYPTION_KEY")
    error_message = "Use numeric secret versions and include DATABASE_URL and ENCRYPTION_KEY."
  }
  validation {
    condition     = contains(keys(var.secret_versions), "ADMIN_PASSWORD_HASH") || (contains(keys(var.secret_versions), "GOOGLE_CLIENT_ID") && contains(keys(var.secret_versions), "GOOGLE_CLIENT_SECRET"))
    error_message = "Configure administrator password authentication or both Google secrets."
  }
}
variable "google_allowed_emails" {
  type    = string
  default = ""
}
variable "app_url" {
  description = "Canonical HTTPS UI origin; defaults to the Firebase web.app origin."
  type        = string
  default     = null
}
variable "api_enabled" {
  description = "Enable only after executing the migration job successfully."
  type        = bool
  default     = false
}
variable "scheduler_paused" {
  description = "Keep paused until migrations and UI deployment are complete."
  type        = bool
  default     = true
}
provider "google" {
  project = var.platform.project_id
  region  = var.platform.region
}
locals {
  app_url         = coalesce(var.app_url, "https://${var.platform.site_id}.web.app")
  api_secrets     = var.api_secret_versions != null ? var.api_secret_versions : var.secret_versions
  worker_secrets  = { for key, version in var.secret_versions : key => version if contains(["DATABASE_URL", "ENCRYPTION_KEY", "AQICN_API_TOKEN"], key) }
  migrate_secrets = { DATABASE_URL = var.secret_versions["DATABASE_URL"] }
  grants = merge(
    { for key, version in local.api_secrets : "api-${key}" => { role = "api", secret = key } },
    { for key, version in local.worker_secrets : "worker-${key}" => { role = "worker", secret = key } },
    { "migrate-DATABASE_URL" = { role = "migrate", secret = "DATABASE_URL" } }
  )
}
resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = local.grants
  secret_id = var.platform.secret_ids[each.value.secret]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.platform.identities[each.value.role]}"
}
resource "google_cloud_run_v2_job" "job" {
  for_each            = { worker = "tick", migrate = "migrate" }
  name                = "${var.platform.name}-${each.key}"
  location            = var.platform.region
  deletion_protection = false
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = var.platform.identities[each.key]
      max_retries     = 0
      timeout         = "1800s"
      containers {
        image   = var.image
        command = ["python"]
        args    = ["-m", "climate_control.cli", each.value]
        resources { limits = { cpu = "1", memory = "512Mi" } }
        env {
          name  = "AUTO_MIGRATE"
          value = "false"
        }
        dynamic "env" {
          for_each = each.key == "worker" ? local.worker_secrets : local.migrate_secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = var.platform.secret_ids[env.key]
                version = env.value
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime]
}
resource "google_cloud_run_v2_service" "api" {
  count                = var.api_enabled ? 1 : 0
  name                 = "${var.platform.name}-api"
  location             = var.platform.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  deletion_protection  = false
  invoker_iam_disabled = true
  template {
    service_account                  = var.platform.identities["api"]
    max_instance_request_concurrency = 20
    timeout                          = "60s"
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image   = coalesce(var.api_image, var.image)
      command = ["python"]
      args    = ["-m", "climate_control.cli", "serve-api"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = true
      }
      ports { container_port = 8080 }
      startup_probe {
        initial_delay_seconds = 0
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 24
        http_get { path = "/health/ready" }
      }
      dynamic "env" {
        for_each = {
          APP_URL                       = local.app_url
          GOOGLE_ALLOWED_EMAILS         = var.google_allowed_emails
          SCHEDULER_ENABLED             = "false"
          AUTO_MIGRATE                  = "false"
          API_OPERATION_TIMEOUT_SECONDS = "50"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.api_secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = var.platform.secret_ids[env.key]
              version = env.value
            }
          }
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = !contains(keys(local.api_secrets), "GOOGLE_CLIENT_ID") || trimspace(var.google_allowed_emails) != ""
      error_message = "Google authentication requires an allowed email list."
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime]
}
resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  project  = var.platform.project_id
  location = var.platform.region
  name     = google_cloud_run_v2_job.job["worker"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.platform.identities["scheduler"]}"
}
resource "google_cloud_scheduler_job" "tick" {
  name             = "${var.platform.name}-tick"
  region           = var.platform.region
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  paused           = var.scheduler_paused
  attempt_deadline = "60s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.platform.project_id}/locations/${var.platform.region}/jobs/${google_cloud_run_v2_job.job["worker"].name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = var.platform.identities["scheduler"]
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  lifecycle {
    precondition {
      condition     = var.scheduler_paused || var.api_enabled
      error_message = "Enable the migrated API before unpausing the scheduler."
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}
output "project_id" { value = var.platform.project_id }
output "app_url" { value = local.app_url }
output "api_url" { value = try(google_cloud_run_v2_service.api[0].uri, null) }
output "migration_job" { value = google_cloud_run_v2_job.job["migrate"].name }
output "worker_job" { value = google_cloud_run_v2_job.job["worker"].name }
output "firebase_config" {
  value = {
    hosting = {
      site   = var.platform.site_id
      public = "frontend/dist"
      ignore = ["firebase.json", "**/.*", "**/node_modules/**"]
      rewrites = concat([
        for prefix in ["api", "auth", "health"] : {
          source = "/${prefix}{,/**}"
          run    = { serviceId = "${var.platform.name}-api", region = var.platform.region }
        }
      ], [{ source = "**", destination = "/index.html" }])
      headers = [
        { source = "**", headers = [
          { key = "Cache-Control", value = "no-cache" },
          { key = "X-Content-Type-Options", value = "nosniff" },
          { key = "X-Frame-Options", value = "DENY" },
          { key = "Referrer-Policy", value = "no-referrer" },
          { key = "Strict-Transport-Security", value = "max-age=31536000" },
          { key = "Content-Security-Policy", value = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'" }
        ] },
        { source = "/assets/**", headers = [{ key = "Cache-Control", value = "public, max-age=31536000, immutable" }] },
        { source = "/{api,auth,health}{,/**}", headers = [{ key = "Cache-Control", value = "no-store" }] }
      ]
    }
  }
}
