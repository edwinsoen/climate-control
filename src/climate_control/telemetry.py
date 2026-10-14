"""Logging and OpenTelemetry setup.

On GCP, traces and agent metrics go to Cloud Trace and Cloud Monitoring through the
Telemetry API, and logs are JSON on stdout, which Cloud Run forwards to Cloud Logging with
trace correlation. Elsewhere, logs stay plain text and standard OTEL_EXPORTER_OTLP_*
variables still enable export. Model prompts and responses are never recorded in spans.
"""

import json
import logging
import os

from opentelemetry import trace


class CloudLoggingFormatter(logging.Formatter):
    def __init__(self, project: str):
        super().__init__()
        self.project = project

    def format(self, record):
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        span = trace.get_current_span().get_span_context()
        if span.is_valid and self.project:
            entry["logging.googleapis.com/trace"] = (
                f"projects/{self.project}/traces/{span.trace_id:032x}"
            )
            entry["logging.googleapis.com/spanId"] = f"{span.span_id:016x}"
            entry["logging.googleapis.com/trace_sampled"] = span.trace_flags.sampled
        return json.dumps(entry, ensure_ascii=False)


def configure(settings, service: str) -> bool:
    """Configure logging and telemetry once per process; returns whether GCP export is on."""
    for variable in (
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    ):
        os.environ.setdefault(variable, "false")
    os.environ.setdefault("OTEL_SERVICE_NAME", f"climate-control-{service}")
    if not settings.telemetry_enabled:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        return False
    handler = logging.StreamHandler()
    handler.setFormatter(CloudLoggingFormatter(settings.google_cloud_project))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    from google.adk.telemetry import google_cloud
    from google.adk.telemetry.setup import maybe_set_otel_providers

    try:
        hooks = google_cloud.get_gcp_exporters(
            enable_cloud_tracing=True, enable_cloud_metrics=True, enable_cloud_logging=False
        )
        maybe_set_otel_providers([hooks])
    except Exception:
        # Telemetry must never prevent the service or worker from running.
        logging.getLogger(__name__).warning("telemetry_setup_failed")
        return False
    return True
