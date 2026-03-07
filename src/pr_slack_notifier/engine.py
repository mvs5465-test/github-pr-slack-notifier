from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Protocol

from opentelemetry import trace

from .models import ActionKind, PullRequestSnapshot, PullRequestState, RouteConfig, SlackMessageRef
from .observability import observe_reconcile_action, observe_reconcile_error, observe_reconcile_pr
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
        disable_historical_closed_prs: bool = True,
        dry_run: bool = False,
    ) -> None:
        self.github = github
        self.slack = slack
        self.routes = routes
        self.plugins = plugins
        self.disable_historical_closed_prs = disable_historical_closed_prs
        self.dry_run = dry_run
        self.log = logging.getLogger(__name__)
        self._tracer = trace.get_tracer(__name__)
        self._started_at = datetime.now(timezone.utc)

    def _should_skip_historical_closed(self, pr: PullRequestSnapshot, existing_comment: str | None) -> bool:
        if not self.disable_historical_closed_prs:
            return False
        if existing_comment:
            return False
        if pr.state not in {PullRequestState.CLOSED, PullRequestState.MERGED}:
            return False
        if pr.updated_at is None:
            return False
        return pr.updated_at < self._started_at

    def run_once(self) -> int:
        reconciled = 0
        for route in self.routes:
            with self._tracer.start_as_current_span("reconcile_route") as span:
                span.set_attribute("route.name", route.name)
                prs = self.github.list_pull_requests(route)
            for pr in prs:
                started = time.monotonic()
                with self._tracer.start_as_current_span("reconcile_pr") as span:
                    span.set_attribute("github.org", pr.org)
                    span.set_attribute("github.repo", pr.repo)
                    span.set_attribute("github.pull_number", pr.number)
                    try:
                        existing_comment = self.github.get_bot_state_comment(pr)
                        if self._should_skip_historical_closed(pr, existing_comment):
                            self.log.info(
                                "reconcile.skip_historical_closed",
                                extra={
                                    "route": route.name,
                                    "org": pr.org,
                                    "repo": pr.repo,
                                    "pull_number": pr.number,
                                    "pr_state": pr.state.value,
                                    "updated_at": pr.updated_at.isoformat(),
                                },
                            )
                            continue

                        existing = parse_state_marker(existing_comment)
                        plan = plan_reconcile(
                            pr=pr,
                            route=route,
                            existing=existing,
                            dry_run=self.dry_run,
                            plugins=self.plugins,
                        )
                        message_ref = existing.message if existing else SlackMessageRef(channel=route.channel, ts="")

                        for action in plan.actions:
                            observe_reconcile_action(action.kind.value)
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
                                self.log.info(
                                    "reconcile.noop reason=%s payload=%s",
                                    action.reason,
                                    action.payload,
                                    extra={
                                        "route": route.name,
                                        "org": pr.org,
                                        "repo": pr.repo,
                                        "pull_number": pr.number,
                                        "action_kind": action.kind.value,
                                        "dry_run": self.dry_run,
                                    },
                                )
                    except Exception:
                        observe_reconcile_error("pr")
                        raise
                    finally:
                        span.set_attribute("reconcile.duration_ms", (time.monotonic() - started) * 1000)
                observe_reconcile_pr(route.name)

                reconciled += 1
        return reconciled
