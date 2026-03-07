from __future__ import annotations

import logging
import time

from .adapters import GitHubAppAdapter, SlackApiAdapter, normalize_private_key
from .config import load_settings_from_env
from .engine import ReconcileEngine


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
    logging.basicConfig(level=logging.INFO)
    _validate_settings()
    settings = load_settings_from_env()
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
        dry_run=settings.dry_run,
    )
    while True:
        engine.run_once()
        time.sleep(settings.polling_interval_seconds)


if __name__ == "__main__":
    run_forever()
