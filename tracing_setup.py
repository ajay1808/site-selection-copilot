"""Real OpenTelemetry spans on every agent call, exported to a local JSONL
file. No LangSmith account is available in this environment, so this uses
the OTel SDK directly with a file exporter instead -- still real spans with
timing and status, just without a hosted UI."""

import functools
import json
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

TRACE_FILE = os.path.join(os.path.dirname(__file__), "traces.jsonl")


class JSONLFileExporter(SpanExporter):
    def export(self, spans):
        with open(TRACE_FILE, "a") as f:
            for span in spans:
                f.write(
                    json.dumps(
                        {
                            "name": span.name,
                            "start_time_ns": span.start_time,
                            "duration_ms": round((span.end_time - span.start_time) / 1e6, 1),
                            "attributes": dict(span.attributes),
                            "status": span.status.status_code.name,
                        }
                    )
                    + "\n"
                )
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(JSONLFileExporter()))
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("site-selection-copilot")


def traced(agent_name: str):
    """Wraps a tool/agent function in an OTel span, tagging candidate address
    and result status when present on the arguments/return value."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with tracer.start_as_current_span(agent_name) as span:
                for a in list(args) + list(kwargs.values()):
                    if hasattr(a, "address") and hasattr(a, "lat"):
                        span.set_attribute("candidate.address", a.address)
                        break
                result = fn(*args, **kwargs)
                status = getattr(result, "status", None)
                if status is not None:
                    span.set_attribute("result.status", status)
                return result

        return wrapper

    return decorator
