from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from opentelemetry import trace

from .models import ActionKind, PullRequestSnapshot, PullRequestState, RouteConfig, SlackMessageRef
from .observability import (
    observe_reconcile_action,
    observe_reconcile_error,
    observe_reconcile_pr,
    observe_route_pr_snapshot,
)
from .plugins import Plugin
from .reconcile import derive_status, plan_reconcile
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

    def get_pull_request(
        self,
        route: RouteConfig,
        *,
        org: str,
        repo: str,
        number: int,
        include_enrichment: bool = True,
    ) -> PullRequestSnapshot | None:
        ...

    def list_pull_requests_for_sweep(self, route: RouteConfig) -> list[PullRequestSnapshot]:
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
        enable_historical_closed_prs: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.github = github
        self.slack = slack
        self.routes = routes
        self._routes_by_name = {route.name: route for route in routes}
        self.plugins = plugins
        self.enable_historical_closed_prs = enable_historical_closed_prs
        self.dry_run = dry_run
        self.log = logging.getLogger(__name__)
        self._tracer = trace.get_tracer(__name__)
        self._started_at = datetime.now(timezone.utc)
        self._route_watermarks: dict[str, datetime | None] = {route.name: None for route in routes}
        self._pr_meta: dict[_PrRef, _PrMeta] = {}
        self._pending_changed: set[_PrRef] = set()
        self._recent_open_refs: dict[_PrRef, float] = {}
        self._recent_open_ref_ttl_seconds = 3600
        self._recent_open_probe_limit = 25

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

    @staticmethod
    def _is_graphql_sweep_route(route: RouteConfig) -> bool:
        return not any(ch in route.org_pattern for ch in "*?[]") and route.repo_pattern == "*"

    @staticmethod
    def _plan_has_state_change(actions: tuple[Action, ...]) -> bool:
        for action in actions:
            if action.kind in {ActionKind.POST_MESSAGE, ActionKind.UPDATE_MESSAGE}:
                return True
            if action.kind == ActionKind.LOG_ONLY and action.reason == "dry_run":
                would = str(action.payload.get("would", ""))
                if would in {"post_message", "update_message"}:
                    return True
        return False

    def _track_recent_open(self, ref: _PrRef, *, now_monotonic: float | None = None) -> None:
        self._recent_open_refs[ref] = now_monotonic if now_monotonic is not None else time.monotonic()

    def _untrack_recent_open(self, ref: _PrRef) -> None:
        self._recent_open_refs.pop(ref, None)

    def _reconcile_recent_open_transitions(self, *, skip_refs: set[_PrRef] | None = None) -> int:
        if not self._recent_open_refs:
            return 0

        now_monotonic = time.monotonic()
        cutoff = now_monotonic - self._recent_open_ref_ttl_seconds
        for ref, seen_at in list(self._recent_open_refs.items()):
            if seen_at < cutoff or self._routes_by_name.get(ref.route_name) is None:
                self._recent_open_refs.pop(ref, None)

        reconciled = 0
        probes = 0
        for ref in sorted(self._recent_open_refs, key=lambda item: (item.route_name, item.org, item.repo, item.number)):
            if probes >= self._recent_open_probe_limit:
                break
            if skip_refs and ref in skip_refs:
                continue
            if ref in self._pending_changed:
                continue
            route = self._routes_by_name.get(ref.route_name)
            if route is None:
                self._untrack_recent_open(ref)
                continue

            current = self.github.get_pull_request(
                route,
                org=ref.org,
                repo=ref.repo,
                number=ref.number,
                include_enrichment=False,
            )
            probes += 1
            if current is None:
                self._pending_changed.discard(ref)
                self._pr_meta.pop(ref, None)
                self._untrack_recent_open(ref)
                continue

            self._pr_meta[ref] = self._meta_from_pr(current)
            self._route_watermarks[route.name] = self._merge_watermark(
                self._route_watermarks.get(route.name),
                current.updated_at,
            )

            full_pr = self.github.get_pull_request(
                route,
                org=ref.org,
                repo=ref.repo,
                number=ref.number,
                include_enrichment=True,
            )
            if full_pr is None:
                full_pr = current
            if self._reconcile_pr(route, full_pr, force_refresh_state=False):
                reconciled += 1
            self._pr_meta[ref] = self._meta_from_pr(full_pr)
            self._pending_changed.discard(ref)
            if full_pr.state == PullRequestState.OPEN:
                self._track_recent_open(ref, now_monotonic=now_monotonic)
            else:
                self._untrack_recent_open(ref)

        return reconciled

    def _should_skip_historical_closed(self, pr: PullRequestSnapshot, existing_comment: str | None) -> bool:
        if self.enable_historical_closed_prs:
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
                if pr.state == PullRequestState.OPEN:
                    self._track_recent_open(ref)
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
                status = derive_status(pr)
                plan = plan_reconcile(
                    pr=pr,
                    route=route,
                    existing=existing,
                    dry_run=self.dry_run,
                    plugins=self.plugins,
                )
                changed = self._plan_has_state_change(plan.actions)
                message_ref = existing.message if existing else SlackMessageRef(channel=route.channel, ts="")

                for action in plan.actions:
                    self.log.info(
                        "reconcile.action kind=%s reason=%s",
                        action.kind.value,
                        action.reason or "",
                        extra={
                            "route": route.name,
                            "org": pr.org,
                            "repo": pr.repo,
                            "pull_number": pr.number,
                            "action_kind": action.kind.value,
                            "dry_run": self.dry_run,
                        },
                    )
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

        observe_reconcile_pr(
            route.name,
            pr.state.value,
            status.approval.value,
            status.checks.value,
        )
        return changed

    def reconcile_changed(self) -> int:
        if not self._pending_changed:
            return self._reconcile_recent_open_transitions()

        processed = 0
        processed_refs: set[_PrRef] = set()
        refs = sorted(self._pending_changed, key=lambda ref: (ref.route_name, ref.org, ref.repo, ref.number))
        refs_by_route: dict[str, list[_PrRef]] = {}
        for ref in refs:
            refs_by_route.setdefault(ref.route_name, []).append(ref)

        for route_name, route_refs in refs_by_route.items():
            route = self._routes_by_name.get(route_name)
            if route is None:
                continue
            for ref in route_refs:
                target = self.github.get_pull_request(
                    route,
                    org=ref.org,
                    repo=ref.repo,
                    number=ref.number,
                    include_enrichment=True,
                )
                if target is None:
                    self._pending_changed.discard(ref)
                    self._untrack_recent_open(ref)
                    continue
                processed_refs.add(ref)
                if self._reconcile_pr(route, target, force_refresh_state=False):
                    processed += 1
                if target.state == PullRequestState.OPEN:
                    self._track_recent_open(ref)
                else:
                    self._untrack_recent_open(ref)
                self._pending_changed.discard(ref)

        # Clear any stale references that no longer match configured routes.
        for ref in refs:
            route = self._routes_by_name.get(ref.route_name)
            if route is None:
                self._pending_changed.discard(ref)
        return processed + self._reconcile_recent_open_transitions(skip_refs=processed_refs)

    def reconcile_all(self, *, force_refresh_state: bool = False) -> int:
        reconciled = 0
        for route in self.routes:
            with self._tracer.start_as_current_span("reconcile_route") as span:
                span.set_attribute("route.name", route.name)
                prs = self.github.list_pull_requests(route, include_enrichment=False)
            observe_route_pr_snapshot(route.name, prs)
            candidates: list[_PrRef] = []
            for pr in prs:
                ref = self._make_ref(route, pr)
                meta = self._meta_from_pr(pr)
                previous = self._pr_meta.get(ref)
                if force_refresh_state or previous is None or previous != meta:
                    candidates.append(ref)
                self._pr_meta[ref] = self._meta_from_pr(pr)
                if pr.state == PullRequestState.OPEN:
                    self._track_recent_open(ref)
                else:
                    self._untrack_recent_open(ref)
                self._route_watermarks[route.name] = self._merge_watermark(
                    self._route_watermarks.get(route.name),
                    pr.updated_at,
                )
                self._pending_changed.discard(ref)
            for ref in candidates:
                full_pr = self.github.get_pull_request(
                    route,
                    org=ref.org,
                    repo=ref.repo,
                    number=ref.number,
                    include_enrichment=True,
                )
                if full_pr is None:
                    continue
                if self._reconcile_pr(route, full_pr, force_refresh_state=force_refresh_state):
                    reconciled += 1
                self._pr_meta[ref] = self._meta_from_pr(full_pr)
                if full_pr.state == PullRequestState.OPEN:
                    self._track_recent_open(ref)
                else:
                    self._untrack_recent_open(ref)
        return reconciled

    def reconcile_sweep(self) -> int:
        reconciled = 0
        for route in self.routes:
            with self._tracer.start_as_current_span("reconcile_route_sweep") as span:
                span.set_attribute("route.name", route.name)
                prs = self.github.list_pull_requests_for_sweep(route)
            observe_route_pr_snapshot(route.name, prs)
            force_refresh_state = not self._is_graphql_sweep_route(route)
            for pr in prs:
                if self._reconcile_pr(route, pr, force_refresh_state=force_refresh_state):
                    reconciled += 1
                ref = self._make_ref(route, pr)
                self._pr_meta[ref] = self._meta_from_pr(pr)
                if pr.state == PullRequestState.OPEN:
                    self._track_recent_open(ref)
                else:
                    self._untrack_recent_open(ref)
                self._route_watermarks[route.name] = self._merge_watermark(
                    self._route_watermarks.get(route.name),
                    pr.updated_at,
                )
                self._pending_changed.discard(ref)
        return reconciled

    def run_once(self) -> int:
        return self.reconcile_all(force_refresh_state=False)
