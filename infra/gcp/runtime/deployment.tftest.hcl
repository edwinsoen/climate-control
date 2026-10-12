mock_provider "google" {}
variables {
  platform = {
    project_id = "climate-test"
    region     = "asia-southeast1"
    name       = "climate-control"
    site_id    = "climate-test"
    identities = {
      api       = "api@climate-test.iam.gserviceaccount.com"
      worker    = "worker@climate-test.iam.gserviceaccount.com"
      migrate   = "migrate@climate-test.iam.gserviceaccount.com"
      scheduler = "scheduler@climate-test.iam.gserviceaccount.com"
    }
    secret_ids = {
      DATABASE_URL        = "test-database"
      ENCRYPTION_KEY      = "test-encryption"
      ADMIN_PASSWORD_HASH = "test-password"
    }
  }
  image           = "asia-southeast1-docker.pkg.dev/climate-test/app/image@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  secret_versions = { DATABASE_URL = "1", ENCRYPTION_KEY = "1", ADMIN_PASSWORD_HASH = "1" }
}
run "migration_stage" {
  command = plan
  assert {
    condition     = length(google_cloud_run_v2_service.api) == 0 && google_cloud_scheduler_job.tick.paused
    error_message = "Initial deployment must not start the API or scheduling before migrations."
  }
  assert {
    condition     = google_cloud_run_v2_job.job["worker"].template[0].template[0].containers[0].args == tolist(["-m", "climate_control.cli", "tick"]) && google_cloud_run_v2_job.job["worker"].template[0].template[0].max_retries == 0
    error_message = "Worker must execute one tick without blind automatic retries."
  }
  assert {
    condition     = toset([for env in google_cloud_run_v2_job.job["migrate"].template[0].template[0].containers[0].env : env.name]) == toset(["AUTO_MIGRATE", "DATABASE_URL"])
    error_message = "Migration job must not receive encryption or login credentials."
  }
  assert {
    condition     = !contains([for env in google_cloud_run_v2_job.job["worker"].template[0].template[0].containers[0].env : env.name], "ADMIN_PASSWORD_HASH")
    error_message = "Worker must not receive login credentials."
  }
}
run "active_stage" {
  command = plan
  variables {
    api_enabled      = true
    scheduler_paused = false
  }
  assert {
    condition     = google_cloud_scheduler_job.tick.schedule == "* * * * *" && google_cloud_scheduler_job.tick.http_target[0].oauth_token[0].service_account_email == var.platform.identities["scheduler"]
    error_message = "Minute schedule must use the dedicated OAuth scheduler identity."
  }
  assert {
    condition     = google_cloud_run_v2_service.api[0].template[0].containers[0].args == tolist(["-m", "climate_control.cli", "serve-api"])
    error_message = "Web service must use API-only mode."
  }
  assert {
    condition     = google_cloud_run_v2_service.api[0].template[0].containers[0].resources[0].cpu_idle
    error_message = "Web service must use request-based billing."
  }
  assert {
    condition     = output.firebase_config.hosting.rewrites[0].run.serviceId == google_cloud_run_v2_service.api[0].name && output.firebase_config.hosting.rewrites[3].destination == "/index.html"
    error_message = "API rewrites must precede the static SPA fallback."
  }
}
run "premature_schedule" {
  command = plan
  variables { scheduler_paused = false }
  expect_failures = [google_cloud_scheduler_job.tick]
}

run "retain_api_during_migration" {
  command = plan
  variables {
    api_enabled         = true
    api_image           = "asia-southeast1-docker.pkg.dev/climate-test/app/image@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    api_secret_versions = { DATABASE_URL = "1", ENCRYPTION_KEY = "1", ADMIN_PASSWORD_HASH = "1" }
    secret_versions     = { DATABASE_URL = "2", ENCRYPTION_KEY = "2", ADMIN_PASSWORD_HASH = "2" }
  }
  assert {
    condition     = google_cloud_run_v2_service.api[0].template[0].containers[0].image == var.api_image && google_cloud_run_v2_job.job["migrate"].template[0].template[0].containers[0].image == var.image
    error_message = "Migration staging must preserve the current API while updating the jobs."
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].version if env.name == "DATABASE_URL"]) == "1" && one([for env in google_cloud_run_v2_job.job["migrate"].template[0].template[0].containers[0].env : env.value_source[0].secret_key_ref[0].version if env.name == "DATABASE_URL"]) == "2"
    error_message = "The API must retain its existing secrets until migrations succeed."
  }
}
