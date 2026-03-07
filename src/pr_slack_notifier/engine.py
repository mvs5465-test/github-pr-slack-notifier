from __future__ import annotations

import logging
from typing import Protocol

from .models import ActionKind, PullRequestSnapshot, RouteConfig, SlackMessageRef
from .plugins import Plugin
from .reconcile import plan_reconcile
from .state import parse_state_marker, render_state_marker


class GitHubAdapter(Protocol):
    def list_pull_requests(self, route: RouteConfig) -> list[PullRequestSnapshot]:
        ...

    def get_bot_state_comment(self, pr: PullRequestSnapshot) -> str | None:
        ...

    def upsert_bot_state_comment(self, pr: PullRequestSnapshot, body: str) -> None:
        ...


class SlackAdapter(Protocol):
    def post_message(self, channel: str, text: str) -> str:
        ...

    def update_message(self, channel: str, ts: str, text: str) -> None:
        ...


class ReconcileEngine:
    def __init__(
        self,
        github: GitHubAdapter,
        slack: SlackAdapter,
        routes: list[RouteConfig],
        plugins: tuple[Plugin, ...] = (),
        dry_run: bool = False,
    ) -> None:
        self.github = github
        self.slack = slack
        self.routes = routes
        self.plugins = plugins
        self.dry_run = dry_run
        self.log = logging.getLogger(__name__)

    def run_once(self) -> int:
        reconciled = 0
        for route in self.routes:
            prs = self.github.list_pull_requests(route)
            for pr in prs:
                existing = parse_state_marker(self.github.get_bot_state_comment(pr))
                plan = plan_reconcile(
                    pr=pr,
                    route=route,
                    existing=existing,
                    dry_run=self.dry_run,
                    plugins=self.plugins,
                )
                message_ref = existing.message if existing else SlackMessageRef(channel=route.channel, ts="")

                for action in plan.actions:
                    if action.kind == ActionKind.POST_MESSAGE:
                        ts = self.slack.post_message(action.payload["channel"], action.payload["text"])
                        message_ref = SlackMessageRef(channel=action.payload["channel"], ts=ts)
                    elif action.kind == ActionKind.UPDATE_MESSAGE:
                        self.slack.update_message(
                            action.payload["channel"],
                            action.payload["ts"],
                            action.payload["text"],
                        )
                    elif action.kind == ActionKind.UPSERT_COMMENT:
                        state = plan.state
                        if state is None:
                            continue
                        state = type(state)(
                            message=message_ref,
                            fingerprint=state.fingerprint,
                            version=state.version,
                        )
                        self.github.upsert_bot_state_comment(pr, render_state_marker(state))
                    else:
                        self.log.info("reconcile.noop", reason=action.reason, payload=action.payload)

                reconciled += 1
        return reconciled
