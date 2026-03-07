from __future__ import annotations

import json
import logging
import re
from collections import Counter as CollectionCounter
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from .models import PullRequestSnapshot
from .status import derive_approval_state, derive_checks_state

_EXTRA_KEYS = {
    "route",
    "org",
    "repo",
    "pull_number",
    "action_kind",
    "dry_run",
    "status_code",
    "operation",
    "system",
}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value

        span = trace.get_current_span()
        span_ctx = span.get_span_context()
        if span_ctx.is_valid:
            payload["trace_id"] = format(span_ctx.trace_id, "032x")
            payload["span_id"] = format(span_ctx.span_id, "016x")
        return json.dumps(payload, separators=(",", ":"))


RECONCILE_CYCLE_DURATION_SECONDS = Histogram(
    "pr_notifier_reconcile_cycle_duration_seconds",
    "Duration of one reconciliation cycle.",
)
RECONCILE_LOOP_DURATION_SECONDS = Histogram(
    "pr_notifier_reconcile_loop_duration_seconds",
    "Duration of a reconcile loop execution.",
    ["loop"],
)
RECONCILE_LOOP_RUNS_TOTAL = Counter(
    "pr_notifier_reconcile_loop_runs_total",
    "Number of reconcile loop executions by loop and result.",
    ["loop", "result"],
)
RECONCILE_LOOP_ITEMS_TOTAL = Counter(
    "pr_notifier_reconcile_loop_items_total",
    "Number of items produced/processed by each reconcile loop.",
    ["loop"],
)
RECONCILE_PR_PROCESSED_TOTAL = Counter(
    "pr_notifier_reconcile_pr_processed_total",
    "Number of PRs processed by route/state/approval/checks.",
    ["route", "state", "approval", "checks"],
)
RECONCILE_ACTIONS_TOTAL = Counter(
    "pr_notifier_reconcile_actions_total",
    "Actions emitted by the reconcile planner/executor.",
    ["kind"],
)
RECONCILE_ERRORS_TOTAL = Counter(
    "pr_notifier_reconcile_errors_total",
    "Reconcile/runtime errors by stage.",
    ["stage"],
)
RATE_LIMIT_EVENTS_TOTAL = Counter(
    "pr_notifier_rate_limit_events_total",
    "Rate-limit events observed by stage.",
    ["stage"],
)
RATE_LIMIT_RETRY_SECONDS = Gauge(
    "pr_notifier_rate_limit_retry_seconds",
    "Computed retry delay in seconds after rate-limit response.",
    ["stage"],
)
EXTERNAL_API_REQUESTS_TOTAL = Counter(
    "pr_notifier_external_api_requests_total",
    "Outgoing API request count by system/operation/status.",
    ["system", "operation", "status_code"],
)
EXTERNAL_API_REQUEST_DURATION_SECONDS = Histogram(
    "pr_notifier_external_api_request_duration_seconds",
    "Outgoing API request duration by system/operation.",
    ["system", "operation"],
)
ROUTE_PULL_REQUESTS_TOTAL = Gauge(
    "pr_notifier_route_pull_requests",
    "Current PR count seen for a route on latest enriched scan.",
    ["route"],
)
ROUTE_PULL_REQUESTS_BY_STATE = Gauge(
    "pr_notifier_route_pull_requests_by_state",
    "Current PR counts by state on latest enriched scan.",
    ["route", "state"],
)
ROUTE_PULL_REQUESTS_BY_APPROVAL = Gauge(
    "pr_notifier_route_pull_requests_by_approval",
    "Current PR counts by approval state on latest enriched scan.",
    ["route", "approval"],
)
ROUTE_PULL_REQUESTS_BY_CHECKS = Gauge(
    "pr_notifier_route_pull_requests_by_checks",
    "Current PR counts by checks state on latest enriched scan.",
    ["route", "checks"],
)

_OTEL_CONFIGURED = False
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+")
_SHA_SEGMENT_RE = re.compile(r"/[0-9a-f]{7,40}")


def normalize_operation(path: str) -> str:
    op = _SHA_SEGMENT_RE.sub("/:sha", path)
    op = _NUMERIC_SEGMENT_RE.sub("/:id", op)
    return op


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    formatter: logging.Formatter
    if json_logs:
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in root.handlers:
        handler.setFormatter(formatter)


def maybe_start_metrics_server(enabled: bool, port: int) -> None:
    if enabled:
        start_http_server(port=port, addr="0.0.0.0")


def configure_tracing(service_name: str, otlp_endpoint: str) -> None:
    global _OTEL_CONFIGURED
    if _OTEL_CONFIGURED or not otlp_endpoint:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _OTEL_CONFIGURED = True


def observe_reconcile_cycle(duration_seconds: float) -> None:
    RECONCILE_CYCLE_DURATION_SECONDS.observe(duration_seconds)


def observe_reconcile_loop(loop_name: str, result: str, duration_seconds: float, items: int | None) -> None:
    RECONCILE_LOOP_DURATION_SECONDS.labels(loop=loop_name).observe(duration_seconds)
    RECONCILE_LOOP_RUNS_TOTAL.labels(loop=loop_name, result=result).inc()
    safe_items = 0 if items is None else max(items, 0)
    RECONCILE_LOOP_ITEMS_TOTAL.labels(loop=loop_name).inc(safe_items)


def observe_reconcile_pr(route_name: str, state: str, approval: str, checks: str) -> None:
    RECONCILE_PR_PROCESSED_TOTAL.labels(
        route=route_name,
        state=state,
        approval=approval,
        checks=checks,
    ).inc()


def observe_reconcile_action(kind: str) -> None:
    RECONCILE_ACTIONS_TOTAL.labels(kind=kind).inc()


def observe_reconcile_error(stage: str) -> None:
    RECONCILE_ERRORS_TOTAL.labels(stage=stage).inc()


def observe_rate_limit(stage: str, retry_seconds: int) -> None:
    RATE_LIMIT_EVENTS_TOTAL.labels(stage=stage).inc()
    RATE_LIMIT_RETRY_SECONDS.labels(stage=stage).set(max(retry_seconds, 0))


def observe_api_request(system: str, operation: str, status_code: int, duration_seconds: float) -> None:
    EXTERNAL_API_REQUESTS_TOTAL.labels(
        system=system,
        operation=normalize_operation(operation),
        status_code=str(status_code),
    ).inc()
    EXTERNAL_API_REQUEST_DURATION_SECONDS.labels(
        system=system,
        operation=normalize_operation(operation),
    ).observe(duration_seconds)


def observe_route_pr_snapshot(route_name: str, prs: list[PullRequestSnapshot]) -> None:
    state_counts: CollectionCounter[str] = CollectionCounter()
    approval_counts: CollectionCounter[str] = CollectionCounter()
    checks_counts: CollectionCounter[str] = CollectionCounter()
    for pr in prs:
        state_counts[pr.state.value] += 1
        approval_counts[derive_approval_state(pr).value] += 1
        checks_counts[derive_checks_state(pr.check_runs).value] += 1

    ROUTE_PULL_REQUESTS_TOTAL.labels(route=route_name).set(len(prs))

    for state in ("open", "closed", "merged"):
        ROUTE_PULL_REQUESTS_BY_STATE.labels(route=route_name, state=state).set(state_counts.get(state, 0))
    for approval in ("needs_review", "approved", "changes_requested"):
        ROUTE_PULL_REQUESTS_BY_APPROVAL.labels(route=route_name, approval=approval).set(
            approval_counts.get(approval, 0)
        )
    for checks in ("no_checks", "pending", "running", "failed", "passed"):
        ROUTE_PULL_REQUESTS_BY_CHECKS.labels(route=route_name, checks=checks).set(checks_counts.get(checks, 0))
