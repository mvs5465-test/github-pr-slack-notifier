from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .models import RouteConfig


@dataclass(frozen=True)
class Settings:
    github_app_id: str
    github_app_private_key: str
    github_installation_ids: tuple[int, ...]
    slack_bot_token: str
    polling_interval_seconds: int
    deep_reconcile_interval_seconds: int
    sweep_reconcile_interval_seconds: int
    enable_sweep_reconcile: bool
    rate_limit_backoff_seconds: int
    rate_limit_backoff_max_seconds: int
    error_retry_seconds: int
    enable_historical_closed_prs: bool
    dry_run: bool
    routes: tuple[RouteConfig, ...]
    log_level: str
    json_logs: bool
    metrics_enabled: bool
    metrics_port: int
    otel_service_name: str
    otel_otlp_endpoint: str



def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_routes(value: str | None) -> tuple[RouteConfig, ...]:
    if not value:
        return ()
    parsed = json.loads(value)
    routes = []
    for item in parsed:
        routes.append(
            RouteConfig(
                name=item.get("name", "default"),
                org_pattern=item["org_pattern"],
                repo_pattern=item.get("repo_pattern", "*"),
                channel=item["channel"],
            )
        )
    return tuple(routes)


def load_settings_from_env() -> Settings:
    install_ids = tuple(
        int(v)
        for v in os.getenv("GITHUB_INSTALLATION_IDS", "").split(",")
        if v.strip()
    )
    return Settings(
        github_app_id=os.getenv("GITHUB_APP_ID", ""),
        github_app_private_key=os.getenv("GITHUB_APP_PRIVATE_KEY", ""),
        github_installation_ids=install_ids,
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        polling_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "30")),
        deep_reconcile_interval_seconds=int(os.getenv("DEEP_RECONCILE_INTERVAL_SECONDS", "30")),
        sweep_reconcile_interval_seconds=int(os.getenv("SWEEP_RECONCILE_INTERVAL_SECONDS", "600")),
        enable_sweep_reconcile=_parse_bool(os.getenv("ENABLE_SWEEP_RECONCILE"), default=False),
        rate_limit_backoff_seconds=int(os.getenv("RATE_LIMIT_BACKOFF_SECONDS", "60")),
        rate_limit_backoff_max_seconds=int(os.getenv("RATE_LIMIT_BACKOFF_MAX_SECONDS", "900")),
        error_retry_seconds=int(os.getenv("ERROR_RETRY_SECONDS", "10")),
        enable_historical_closed_prs=_parse_bool(os.getenv("ENABLE_HISTORICAL_CLOSED_PRS"), default=False),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), default=False),
        routes=_parse_routes(os.getenv("ROUTES_JSON")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        json_logs=_parse_bool(os.getenv("JSON_LOGS"), default=True),
        metrics_enabled=_parse_bool(os.getenv("METRICS_ENABLED"), default=True),
        metrics_port=int(os.getenv("METRICS_PORT", "9000")),
        otel_service_name=os.getenv("OTEL_SERVICE_NAME", "github-pr-slack-notifier"),
        otel_otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
    )
