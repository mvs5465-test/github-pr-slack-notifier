from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from opentelemetry import trace

from .models import ActionKind, PullRequestSnapshot, PullRequestState, RouteConfig, SlackMessageRef
from .observability import observe_reconcile_action, observe_reconcile_error, observe_reconcile_pr
from .plugins import Plugin
from .reconcile import plan_reconcile
from .state import parse_state_marker, render_state_marker


class GitHubAdapter(Protocol):
    def list_pull_requests(
        self,
        route: RouteConfig,
        *,
        include_enrichment: bool = True,
        updated_after: datetime | None = None,
    ) -> list[PullRequestSnapshot]:
        ...

    def get_bot_state_comment(self, pr: PullRequestSnapshot, *, force_refresh: bool = False) -> str | None:
        ...

    def upsert_bot_state_comment(self, pr: PullRequestSnapshot, body: str) -> None:
        ...


class SlackAdapter(Protocol):
    def post_message(self, channel: str, text: str) -> str:
        ...

    def update_message(self, channel: str, ts: str, text: str) -> None:
        ...


@dataclass(frozen=True)
class _PrMeta:
    updated_at: datetime | None
    state: PullRequestState
    head_sha: str


@dataclass(frozen=True)
class _PrRef:
    route_name: str
    org: str
    repo: str
    number: int


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
        self._routes_by_name = {route.name: route for route in routes}
        self.plugins = plugins
        self.disable_historical_closed_prs = disable_historical_closed_prs
        self.dry_run = dry_run
        self.log = logging.getLogger(__name__)
        self._tracer = trace.get_tracer(__name__)
        self._started_at = datetime.now(timezone.utc)
        self._route_watermarks: dict[str, datetime | None] = {route.name: None for route in routes}
        self._pr_meta: dict[_PrRef, _PrMeta] = {}
        self._pending_changed: set[_PrRef] = set()

    @staticmethod
    def _make_ref(route: RouteConfig, pr: PullRequestSnapshot) -> _PrRef:
        return _PrRef(route_name=route.name, org=pr.org, repo=pr.repo, number=pr.number)

    @staticmethod
    def _meta_from_pr(pr: PullRequestSnapshot) -> _PrMeta:
        return _PrMeta(updated_at=pr.updated_at, state=pr.state, head_sha=pr.head_sha)

    @staticmethod
    def _merge_watermark(current: datetime | None, candidate: datetime | None) -> datetime | None:
        if candidate is None:
            return current
        if current is None or candidate > current:
            return candidate
        return current

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

    def refresh_lightweight(self) -> int:
        changed = 0
        for route in self.routes:
            with self._tracer.start_as_current_span("reconcile_route_refresh") as span:
                span.set_attribute("route.name", route.name)
                watermark = self._route_watermarks.get(route.name)
                prs = self.github.list_pull_requests(
                    route,
                    include_enrichment=False,
                    updated_after=watermark,
                )
            for pr in prs:
                ref = self._make_ref(route, pr)
                meta = self._meta_from_pr(pr)
                previous = self._pr_meta.get(ref)
                if previous is None or previous != meta:
                    self._pending_changed.add(ref)
                    changed += 1
                self._pr_meta[ref] = meta
                self._route_watermarks[route.name] = self._merge_watermark(
                    self._route_watermarks.get(route.name),
                    pr.updated_at,
                )
        return changed

    def _reconcile_pr(self, route: RouteConfig, pr: PullRequestSnapshot, *, force_refresh_state: bool) -> bool:
        started = time.monotonic()
        with self._tracer.start_as_current_span("reconcile_pr") as span:
            span.set_attribute("github.org", pr.org)
            span.set_attribute("github.repo", pr.repo)
            span.set_attribute("github.pull_number", pr.number)
            try:
                existing_comment = self.github.get_bot_state_comment(pr, force_refresh=force_refresh_state)
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
                    return False

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
        return True

    def reconcile_changed(self) -> int:
        if not self._pending_changed:
            return 0

        processed = 0
        refs = sorted(self._pending_changed, key=lambda ref: (ref.route_name, ref.org, ref.repo, ref.number))
        refs_by_route: dict[str, list[_PrRef]] = {}
        for ref in refs:
            refs_by_route.setdefault(ref.route_name, []).append(ref)

        for route_name, route_refs in refs_by_route.items():
            route = self._routes_by_name.get(route_name)
            if route is None:
                continue
            prs = self.github.list_pull_requests(
                route,
                include_enrichment=True,
                updated_after=None,
            )
            prs_index = {(pr.org, pr.repo, pr.number): pr for pr in prs}
            for ref in route_refs:
                target = prs_index.get((ref.org, ref.repo, ref.number))
                if target is None:
                    self._pending_changed.discard(ref)
                    continue
                if self._reconcile_pr(route, target, force_refresh_state=False):
                    processed += 1
                self._pending_changed.discard(ref)

        # Clear any stale references that no longer match configured routes.
        for ref in refs:
            route = self._routes_by_name.get(ref.route_name)
            if route is None:
                self._pending_changed.discard(ref)
        return processed

    def reconcile_all(self, *, force_refresh_state: bool = False) -> int:
        reconciled = 0
        for route in self.routes:
            with self._tracer.start_as_current_span("reconcile_route") as span:
                span.set_attribute("route.name", route.name)
                prs = self.github.list_pull_requests(route, include_enrichment=True)
            for pr in prs:
                if self._reconcile_pr(route, pr, force_refresh_state=force_refresh_state):
                    reconciled += 1
                ref = self._make_ref(route, pr)
                self._pr_meta[ref] = self._meta_from_pr(pr)
                self._route_watermarks[route.name] = self._merge_watermark(
                    self._route_watermarks.get(route.name),
                    pr.updated_at,
                )
                self._pending_changed.discard(ref)
        return reconciled

    def run_once(self) -> int:
        return self.reconcile_all(force_refresh_state=False)
