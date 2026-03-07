import json
import logging
from datetime import datetime, timezone

import pr_slack_notifier.observability as obs
from pr_slack_notifier.models import CheckRun, PullRequestSnapshot, PullRequestState
from pr_slack_notifier.observability import JsonLogFormatter, normalize_operation


def test_normalize_operation_rewrites_numeric_and_sha_segments() -> None:
    path = "/repos/acme/widgets/pulls/123/reviews/abcdef1234567890/check-runs"
    assert normalize_operation(path) == "/repos/acme/widgets/pulls/:id/reviews/:sha/check-runs"


def test_json_log_formatter_emits_expected_payload() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.repo = "widgets"
    record.pull_number = 99
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello"
    assert payload["repo"] == "widgets"
    assert payload["pull_number"] == 99


def test_configure_logging_sets_formatter() -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    try:
        root.handlers = [logging.StreamHandler()]
        obs.configure_logging(level="debug", json_logs=True)
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
        obs.configure_logging(level="info", json_logs=False)
        assert not isinstance(root.handlers[0].formatter, JsonLogFormatter)
    finally:
        root.handlers = old_handlers


def test_maybe_start_metrics_server(monkeypatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(obs, "start_http_server", lambda port, addr: called.append(port))
    obs.maybe_start_metrics_server(enabled=False, port=9100)
    obs.maybe_start_metrics_server(enabled=True, port=9101)
    assert called == [9101]


def test_configure_tracing_idempotent(monkeypatch) -> None:
    class FakeProvider:
        def __init__(self, resource):
            self.resource = resource
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    providers = []
    monkeypatch.setattr(obs, "TracerProvider", lambda resource: providers.append(FakeProvider(resource)) or providers[-1])
    monkeypatch.setattr(obs, "BatchSpanProcessor", lambda exporter: ("processor", exporter))
    monkeypatch.setattr(obs, "OTLPSpanExporter", lambda endpoint: ("exporter", endpoint))
    monkeypatch.setattr(obs.trace, "set_tracer_provider", lambda provider: None)

    obs._OTEL_CONFIGURED = False
    obs.configure_tracing(service_name="svc", otlp_endpoint="")
    assert providers == []
    obs.configure_tracing(service_name="svc", otlp_endpoint="http://alloy:4318/v1/traces")
    assert len(providers) == 1
    obs.configure_tracing(service_name="svc", otlp_endpoint="http://alloy:4318/v1/traces")
    assert len(providers) == 1


def test_observe_route_pr_snapshot_sets_route_gauges() -> None:
    prs = [
        PullRequestSnapshot(
            org="acme",
            repo="widgets",
            number=1,
            title="one",
            url="https://example.com/1",
            author="dev",
            state=PullRequestState.OPEN,
            review_decision="APPROVED",
            check_runs=(CheckRun(name="ci", status="completed", conclusion="success"),),
            updated_at=datetime.now(timezone.utc),
        ),
        PullRequestSnapshot(
            org="acme",
            repo="widgets",
            number=2,
            title="two",
            url="https://example.com/2",
            author="dev",
            state=PullRequestState.OPEN,
            review_decision="CHANGES_REQUESTED",
            check_runs=(CheckRun(name="ci", status="completed", conclusion="failure"),),
            updated_at=datetime.now(timezone.utc),
        ),
    ]

    obs.observe_route_pr_snapshot("default", prs)

    assert obs.ROUTE_PULL_REQUESTS_TOTAL.labels(route="default")._value.get() == 2
    assert obs.ROUTE_PULL_REQUESTS_BY_APPROVAL.labels(route="default", approval="approved")._value.get() == 1
    assert (
        obs.ROUTE_PULL_REQUESTS_BY_APPROVAL.labels(route="default", approval="changes_requested")._value.get() == 1
    )
    assert obs.ROUTE_PULL_REQUESTS_BY_CHECKS.labels(route="default", checks="passed")._value.get() == 1
    assert obs.ROUTE_PULL_REQUESTS_BY_CHECKS.labels(route="default", checks="failed")._value.get() == 1
