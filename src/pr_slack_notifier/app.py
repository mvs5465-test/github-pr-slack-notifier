from __future__ import annotations

import logging
import time
from collections.abc import Callable
from opentelemetry import trace

from .adapters import GitHubAppAdapter, GitHubRateLimitError, SlackApiAdapter, normalize_private_key
from .config import load_settings_from_env
from .engine import ReconcileEngine
from .models import RouteConfig
from .observability import (
    configure_logging,
    configure_tracing,
    maybe_start_metrics_server,
    observe_reconcile_cycle,
    observe_reconcile_error,
    observe_reconcile_loop,
    observe_rate_limit,
)


def _is_org_wide_route(route: RouteConfig) -> bool:
    return not any(ch in route.org_pattern for ch in "*?[]") and route.repo_pattern == "*"


def _loop_resources(settings) -> dict[str, set[str]]:
    has_non_org_wide = any(not _is_org_wide_route(route) for route in settings.routes)
    has_org_wide = any(_is_org_wide_route(route) for route in settings.routes)

    lightweight_resources = {"search"} if not has_non_org_wide else {"search", "core"}
    deep_resources = {"core"}
    if has_org_wide:
        deep_resources.add("search")
    return {
        "lightweight": lightweight_resources,
        "deep": deep_resources,
        "sweep": set(deep_resources),
    }


def _validate_settings() -> None:
    settings = load_settings_from_env()
    missing = []
    if not settings.github_app_id:
        missing.append("GITHUB_APP_ID")
    if not settings.github_app_private_key:
        missing.append("GITHUB_APP_PRIVATE_KEY")
    if not settings.github_installation_ids:
        missing.append("GITHUB_INSTALLATION_IDS")
    if not settings.slack_bot_token:
        missing.append("SLACK_BOT_TOKEN")
    if not settings.routes:
        missing.append("ROUTES_JSON")
    if missing:
        missing_joined = ", ".join(missing)
        raise RuntimeError(f"missing required env vars: {missing_joined}")


def run_forever() -> None:
    settings = load_settings_from_env()
    configure_logging(level=settings.log_level, json_logs=settings.json_logs)
    maybe_start_metrics_server(enabled=settings.metrics_enabled, port=settings.metrics_port)
    configure_tracing(service_name=settings.otel_service_name, otlp_endpoint=settings.otel_otlp_endpoint)
    _validate_settings()
    github = GitHubAppAdapter(
        app_id=settings.github_app_id,
        private_key_pem=normalize_private_key(settings.github_app_private_key),
        installation_ids=settings.github_installation_ids,
    )
    slack = SlackApiAdapter(bot_token=settings.slack_bot_token)
    engine = ReconcileEngine(
        github=github,
        slack=slack,
        routes=list(settings.routes),
        enable_historical_closed_prs=settings.enable_historical_closed_prs,
        dry_run=settings.dry_run,
    )
    tracer = trace.get_tracer(__name__)
    next_deep_at = 0.0
    next_sweep_at = 0.0
    blocked_until: dict[str, float] = {}
    log = logging.getLogger(__name__)
    loop_resources = _loop_resources(settings)

    def run_loop(loop_name: str, fn: Callable[[], int]) -> int:
        started_loop = time.monotonic()
        now_epoch = time.time()
        required = loop_resources.get(loop_name, {"core", "search"})
        active = [resource for resource in required if blocked_until.get(resource, 0.0) > now_epoch]
        if active:
            retry_seconds = max(int(blocked_until[resource] - now_epoch) + 1 for resource in active)
            log.info(
                "reconcile.loop_blocked loop=%s resources=%s retry_seconds=%s",
                loop_name,
                ",".join(sorted(active)),
                retry_seconds,
            )
            observe_reconcile_loop(loop_name, "blocked", time.monotonic() - started_loop, 0)
            return 0
        try:
            items = fn()
            observe_reconcile_loop(loop_name, "ok", time.monotonic() - started_loop, items)
            return items
        except GitHubRateLimitError as err:
            resource = (err.resource or "unknown").lower()
            retry_seconds = err.retry_after_seconds(
                now_epoch=time.time(),
                default_seconds=settings.rate_limit_backoff_seconds,
                max_seconds=settings.rate_limit_backoff_max_seconds,
            )
            unblock_at = time.time() + retry_seconds
            if err.reset_at_epoch is not None:
                unblock_at = err.reset_at_epoch
            blocked_until[resource] = max(blocked_until.get(resource, 0.0), unblock_at)
            observe_rate_limit("loop", resource, retry_seconds)
            log.warning(
                "reconcile.rate_limited loop=%s resource=%s sleeping_seconds=%s",
                loop_name,
                resource,
                retry_seconds,
            )
            observe_reconcile_loop(loop_name, "rate_limited", time.monotonic() - started_loop, 0)
            return 0
        except Exception:
            observe_reconcile_loop(loop_name, "error", time.monotonic() - started_loop, 0)
            raise

    while True:
        started = time.monotonic()
        with tracer.start_as_current_span("reconcile_cycle"):
            try:
                run_loop("lightweight", engine.refresh_lightweight)
                now = time.monotonic()
                if now >= next_deep_at:
                    run_loop("deep", engine.reconcile_changed)
                    next_deep_at = now + settings.deep_reconcile_interval_seconds
                if settings.enable_sweep_reconcile and now >= next_sweep_at:
                    run_loop("sweep", lambda: engine.reconcile_all(force_refresh_state=False))
                    next_sweep_at = now + settings.sweep_reconcile_interval_seconds
            except Exception:
                observe_reconcile_error("cycle")
                log.exception("reconcile_cycle_failed")
                time.sleep(settings.error_retry_seconds)
                continue
            finally:
                observe_reconcile_cycle(time.monotonic() - started)
        time.sleep(settings.polling_interval_seconds)


if __name__ == "__main__":
    run_forever()
