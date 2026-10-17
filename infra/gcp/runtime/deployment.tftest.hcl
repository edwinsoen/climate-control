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
      planner   = "planner@climate-test.iam.gserviceaccount.com"
      agent     = "agent@climate-test.iam.gserviceaccount.com"
    }
    secret_ids = {
      DATABASE_URL        = "test-database"
      ENCRYPTION_KEY      = "test-encryption"
      ADMIN_PASSWORD_HASH = "test-password"
      OPENROUTER_API_KEY  = "test-openrouter"
      AGENT_SERVICE_TOKEN = "test-agent-token"
    }
  }
  image               = "asia-southeast1-docker.pkg.dev/climate-test/app/image@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  secret_versions     = { DATABASE_URL = "1", ENCRYPTION_KEY = "1", ADMIN_PASSWORD_HASH = "1" }
  api_enabled         = false
  api_image           = null
  api_secret_versions = null
  scheduler_paused    = true
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
    condition     = output.firebase_config.hosting.rewrites[0].run.serviceId == google_cloud_run_v2_service.api[0].name && output.firebase_config.hosting.rewrites[3].source == "/mcp{,/**}" && output.firebase_config.hosting.rewrites[4].destination == "/index.html"
    error_message = "API rewrites must precede the static SPA fallback."
  }
}
run "premature_schedule" {
  command = plan
  variables { scheduler_paused = false }
  expect_failures = [google_cloud_scheduler_job.tick, google_cloud_scheduler_job.plan]
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

run "planner_least_privilege" {
  command = plan
  variables {
    api_enabled      = true
    scheduler_paused = false
    agent_provider   = "openrouter"
    secret_versions  = { DATABASE_URL = "1", ENCRYPTION_KEY = "1", ADMIN_PASSWORD_HASH = "1", OPENROUTER_API_KEY = "1", AGENT_SERVICE_TOKEN = "1" }
  }
  assert {
    condition     = toset([for env in google_cloud_run_v2_job.job["planner"].template[0].template[0].containers[0].env : env.name if length(env.value_source) > 0]) == toset(["DATABASE_URL", "OPENROUTER_API_KEY"])
    error_message = "The planner must receive only the database and model secrets."
  }
  assert {
    condition     = google_cloud_run_v2_job.job["planner"].template[0].template[0].containers[0].args == tolist(["-m", "climate_control.cli", "plan"]) && google_cloud_run_v2_job.job["planner"].template[0].template[0].service_account == var.platform.identities["planner"]
    error_message = "The planner job must run the plan command as the planner identity."
  }
  assert {
    condition     = length(setintersection(toset([for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env : env.name if length(env.value_source) > 0]), toset(["OPENROUTER_API_KEY", "AGENT_SERVICE_TOKEN"]))) == 0
    error_message = "The API must not receive model or agent credentials."
  }
  assert {
    condition     = one([for env in google_cloud_run_v2_service.api[0].template[0].containers[0].env : env.value if env.name == "PLANNER_JOB"]) == "projects/climate-test/locations/asia-southeast1/jobs/climate-control-planner" && google_cloud_run_v2_job_iam_member.api_planner[0].member == "serviceAccount:${var.platform.identities["api"]}"
    error_message = "The API must be able to start planning immediately."
  }
  assert {
    condition     = google_cloud_scheduler_job.plan[0].schedule == "*/5 * * * *"
    error_message = "Planning runs on a five-minute schedule."
  }
}

run "planner_disabled" {
  command = plan
  variables {
    agent_provider         = "disabled"
    agent_runtime_resource = "projects/climate-test/locations/asia-southeast1/reasoningEngines/1"
  }
  assert {
    condition     = !contains(keys(google_cloud_run_v2_job.job), "planner") && length(google_cloud_scheduler_job.plan) == 0
    error_message = "A disabled planner creates no job or schedule."
  }
}
