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
    condition     = contains(keys(var.secret_versions), "ADMIN_PASSWORD_HASH") || (contains(keys(var.secret_versions), "GOOGLE_CLIENT_ID") && contains(keys(var.secret_versions), "GOOGLE_CLIENT_SECRET") && contains(keys(var.secret_versions), "GOOGLE_ALLOWED_EMAILS"))
    error_message = "Configure administrator password authentication or all three Google secrets."
  }
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
variable "agent_provider" {
  description = "Model provider for the scheduled planner: gemini (Agent Platform), openrouter, or disabled."
  type        = string
  default     = "gemini"
  validation {
    condition     = contains(["gemini", "openrouter", "disabled"], var.agent_provider)
    error_message = "Use gemini, openrouter, or disabled."
  }
}
variable "agent_model" {
  description = "Model ID; empty uses the provider's default alias."
  type        = string
  default     = ""
}
variable "agent_runtime_resource" {
  description = "Agent Runtime resource name; when set, the planner queries the remotely deployed agent."
  type        = string
  default     = ""
  validation {
    condition     = var.agent_runtime_resource == "" || can(regex("^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+$", var.agent_runtime_resource))
    error_message = "Use a full projects/*/locations/*/reasoningEngines/* resource name."
  }
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
  app_url = coalesce(var.app_url, "https://${var.platform.site_id}.web.app")
  # A fixed OIDC audience; the API cannot reference its own generated URI.
  scheduler_audience = "${local.app_url}/internal/poll"
  # Model and agent credentials never reach the API, which also runs the minute tick.
  agent_secret_names = ["OPENROUTER_API_KEY", "AGENT_SERVICE_TOKEN"]
  api_secrets        = { for key, version in(var.api_secret_versions != null ? var.api_secret_versions : var.secret_versions) : key => version if !contains(local.agent_secret_names, key) }
  migrate_secrets    = { DATABASE_URL = var.secret_versions["DATABASE_URL"] }
  planner_secrets    = { for key, version in var.secret_versions : key => version if contains(["DATABASE_URL", "OPENROUTER_API_KEY"], key) }
  planner_enabled = var.agent_provider != "disabled" && (
    var.agent_runtime_resource != "" || var.agent_provider == "gemini" || (
      var.agent_provider == "openrouter" && contains(keys(var.secret_versions), "OPENROUTER_API_KEY")
    )
  )
  agent_env = {
    AGENT_PROVIDER         = local.planner_enabled ? var.agent_provider : "disabled"
    AGENT_MODEL            = var.agent_model
    AGENT_RUNTIME          = local.planner_enabled && var.agent_runtime_resource != "" ? "remote" : "embedded"
    AGENT_RUNTIME_RESOURCE = local.planner_enabled ? var.agent_runtime_resource : ""
    GOOGLE_CLOUD_PROJECT   = var.platform.project_id
  }
  jobs = merge(
    { migrate = "migrate" },
    local.planner_enabled ? { planner = "plan" } : {}
  )
  job_secrets = { migrate = local.migrate_secrets, planner = local.planner_secrets }
  job_env = {
    migrate = { AUTO_MIGRATE = "false" }
    planner = merge({ AUTO_MIGRATE = "false" }, local.agent_env)
  }
  grants = merge(
    { for key, version in local.api_secrets : "api-${key}" => { role = "api", secret = key } },
    { "migrate-DATABASE_URL" = { role = "migrate", secret = "DATABASE_URL" } },
    local.planner_enabled ? { for key, version in local.planner_secrets : "planner-${key}" => { role = "planner", secret = key } } : {}
  )
}
resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = local.grants
  secret_id = var.platform.secret_ids[each.value.secret]
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.platform.identities[each.value.role]}"
}
resource "google_cloud_run_v2_job" "job" {
  for_each            = local.jobs
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
        # The planner loads the agent framework and model client libraries.
        resources { limits = { cpu = "1", memory = each.key == "planner" ? "1Gi" : "512Mi" } }
        dynamic "env" {
          for_each = local.job_env[each.key]
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = local.job_secrets[each.key]
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
    # Scheduled ticks may wait on slow providers; Firebase still limits UI requests to 60s.
    timeout = "300s"
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
        # Agent settings only describe the planner on the status page; the API never plans.
        for_each = merge(local.agent_env, local.planner_enabled ? {
          # Lets ownership changes and review requests start planning immediately.
          PLANNER_JOB = "projects/${var.platform.project_id}/locations/${var.platform.region}/jobs/${var.platform.name}-planner"
          } : {}, {
          APP_URL                       = local.app_url
          SCHEDULER_ENABLED             = "false"
          SCHEDULER_SERVICE_ACCOUNT     = var.platform.identities["scheduler"]
          SCHEDULER_AUDIENCE            = local.scheduler_audience
          AUTO_MIGRATE                  = "false"
          API_OPERATION_TIMEOUT_SECONDS = "50"
        })
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
      condition     = !contains(keys(local.api_secrets), "GOOGLE_CLIENT_ID") || contains(keys(local.api_secrets), "GOOGLE_ALLOWED_EMAILS")
      error_message = "Google authentication requires an allowed email list."
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.runtime]
}
resource "google_cloud_run_v2_job_iam_member" "api_planner" {
  count    = local.planner_enabled ? 1 : 0
  project  = var.platform.project_id
  location = var.platform.region
  name     = google_cloud_run_v2_job.job["planner"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.platform.identities["api"]}"
}
resource "google_cloud_scheduler_job" "tick" {
  name             = "${var.platform.name}-tick"
  region           = var.platform.region
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  paused           = var.scheduler_paused
  attempt_deadline = "300s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    # Calls the API directly, bypassing Firebase's 60-second proxy limit. The URI is a
    # placeholder only while the API is disabled, when the schedule must stay paused.
    uri     = var.api_enabled ? "${google_cloud_run_v2_service.api[0].uri}/internal/poll" : local.scheduler_audience
    body    = base64encode("{}")
    headers = { "Content-Type" = "application/json" }
    oidc_token {
      service_account_email = var.platform.identities["scheduler"]
      audience              = local.scheduler_audience
    }
  }
  lifecycle {
    precondition {
      condition     = var.scheduler_paused || var.api_enabled
      error_message = "Enable the migrated API before unpausing the scheduler."
    }
  }
}
output "project_id" { value = var.platform.project_id }
output "app_url" { value = local.app_url }
output "api_url" { value = try(google_cloud_run_v2_service.api[0].uri, null) }
output "migration_job" { value = try(google_cloud_run_v2_job.job["migrate"].name, null) }
output "planner_job" { value = try(google_cloud_run_v2_job.job["planner"].name, null) }
output "firebase_config" {
  value = {
    hosting = {
      site   = var.platform.site_id
      public = "frontend/dist"
      ignore = ["firebase.json", "**/.*", "**/node_modules/**"]
      rewrites = concat([
        for prefix in ["api", "auth", "health", "mcp"] : {
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
        { source = "/{api,auth,health,mcp}{,/**}", headers = [{ key = "Cache-Control", value = "no-store" }] }
      ]
    }
  }
}
