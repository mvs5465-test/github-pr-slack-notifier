from __future__ import annotations

import logging
import time
from opentelemetry import trace

from .adapters import GitHubAppAdapter, GitHubRateLimitError, SlackApiAdapter, normalize_private_key
from .config import load_settings_from_env
from .engine import ReconcileEngine
from .observability import (
    configure_logging,
    configure_tracing,
    maybe_start_metrics_server,
    observe_reconcile_cycle,
    observe_reconcile_error,
    observe_reconcile_loop,
    observe_rate_limit,
)


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
    log = logging.getLogger(__name__)

    def run_loop(loop_name: str, fn) -> int:
        started_loop = time.monotonic()
        try:
            items = fn()
            observe_reconcile_loop(loop_name, "ok", time.monotonic() - started_loop, items)
            return items
        except GitHubRateLimitError:
            observe_reconcile_loop(loop_name, "rate_limited", time.monotonic() - started_loop, 0)
            raise
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
                    run_loop("sweep", lambda: engine.reconcile_all(force_refresh_state=True))
                    next_sweep_at = now + settings.sweep_reconcile_interval_seconds
            except GitHubRateLimitError as err:
                observe_reconcile_error("cycle_rate_limit")
                retry_seconds = err.retry_after_seconds(
                    now_epoch=time.time(),
                    default_seconds=settings.rate_limit_backoff_seconds,
                    max_seconds=settings.rate_limit_backoff_max_seconds,
                )
                observe_rate_limit("cycle", retry_seconds)
                log.warning("reconcile.rate_limited sleeping_seconds=%s", retry_seconds)
                time.sleep(retry_seconds)
                continue
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
