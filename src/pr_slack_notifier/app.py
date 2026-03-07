from __future__ import annotations

import logging
import time

from .config import load_settings_from_env
from .engine import ReconcileEngine


class UnconfiguredGitHubAdapter:
    def list_pull_requests(self, route):
        raise NotImplementedError("GitHub adapter not wired yet")

    def get_bot_state_comment(self, pr):
        raise NotImplementedError("GitHub adapter not wired yet")

    def upsert_bot_state_comment(self, pr, body):
        raise NotImplementedError("GitHub adapter not wired yet")


class UnconfiguredSlackAdapter:
    def post_message(self, channel, text):
        raise NotImplementedError("Slack adapter not wired yet")

    def update_message(self, channel, ts, text):
        raise NotImplementedError("Slack adapter not wired yet")


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings_from_env()
    engine = ReconcileEngine(
        github=UnconfiguredGitHubAdapter(),
        slack=UnconfiguredSlackAdapter(),
        routes=list(settings.routes),
        dry_run=settings.dry_run,
    )
    while True:
        engine.run_once()
        time.sleep(settings.polling_interval_seconds)


if __name__ == "__main__":
    run_forever()
