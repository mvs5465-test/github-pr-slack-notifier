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
    dry_run: bool
    routes: tuple[RouteConfig, ...]



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
        dry_run=_parse_bool(os.getenv("DRY_RUN"), default=False),
        routes=_parse_routes(os.getenv("ROUTES_JSON")),
    )
