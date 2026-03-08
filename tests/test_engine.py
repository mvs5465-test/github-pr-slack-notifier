import logging
from datetime import datetime, timedelta, timezone

from pr_slack_notifier.engine import ReconcileEngine
from pr_slack_notifier.models import CheckRun, PullRequestSnapshot, PullRequestState, ReconcileState, RouteConfig, SlackMessageRef
from pr_slack_notifier.state import render_state_marker


class FakeGitHub:
    def __init__(self, *, light: dict[str, list[PullRequestSnapshot]], full: dict[str, list[PullRequestSnapshot]], comment: str | None = None):
        self.light = light
        self.full = full
        self.comment = comment
        self.upserts: list[tuple[int, str]] = []
        self.comment_calls = 0
        self.force_refresh_flags: list[bool] = []
        self.list_calls: list[tuple[str, bool, datetime | None]] = []

    def list_pull_requests(self, route, *, include_enrichment=True, updated_after=None):
        self.list_calls.append((route.name, include_enrichment, updated_after))
        source = self.full if include_enrichment else self.light
        items = source.get(route.name, [])
        if updated_after is None:
            return list(items)
        return [pr for pr in items if pr.updated_at is None or pr.updated_at >= updated_after]

    def list_pull_requests_for_sweep(self, route):
        return list(self.full.get(route.name, []))

    def get_pull_request(self, route, *, org: str, repo: str, number: int, include_enrichment: bool = True):
        source = self.full if include_enrichment else self.light
        for pr in source.get(route.name, []):
            if pr.org == org and pr.repo == repo and pr.number == number:
                return pr
        return None

    def get_bot_state_comment(self, pr, *, force_refresh=False):
        self.comment_calls += 1
        self.force_refresh_flags.append(force_refresh)
        return self.comment

    def upsert_bot_state_comment(self, pr, body):
        self.upserts.append((pr.number, body))
        self.comment = body


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.updates = []

    def post_message(self, channel, text):
        self.posts.append((channel, text))
        return "123.456"

    def update_message(self, channel, ts, text):
        self.updates.append((channel, ts, text))


def _pr(
    *,
    number: int = 7,
    state: PullRequestState = PullRequestState.OPEN,
    updated_at: datetime | None = None,
    head_sha: str = "sha-1",
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        org="acme",
        repo="widgets",
        number=number,
        title="Title",
        url=f"https://example.com/pr/{number}",
        author="matt",
        state=state,
        head_sha=head_sha,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


def test_refresh_lightweight_marks_and_reconcile_changed_posts_once() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)

    changed = engine.refresh_lightweight()
    assert changed == 1

    processed = engine.reconcile_changed()
    assert processed == 1
    assert len(slack.posts) == 1
    assert len(gh.upserts) == 1
    assert "pr-slack-notifier" in gh.upserts[0][1]

    changed_again = engine.refresh_lightweight()
    assert changed_again == 0
    assert engine.reconcile_changed() == 0


def test_refresh_lightweight_detects_head_sha_change() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr1 = _pr(head_sha="sha-1")
    pr2 = _pr(head_sha="sha-2", updated_at=pr1.updated_at)
    gh = FakeGitHub(light={"default": [pr1]}, full={"default": [pr1]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(
        github=gh,
        slack=slack,
        routes=[route],
        enable_historical_closed_prs=True,
        dry_run=True,
    )

    assert engine.refresh_lightweight() == 1
    assert engine.reconcile_changed() == 1

    gh.light = {"default": [pr2]}
    gh.full = {"default": [pr2]}
    assert engine.refresh_lightweight() == 1


def test_reconcile_all_force_refreshes_state_comment() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    marker = render_state_marker(
        ReconcileState(
            message=SlackMessageRef(channel="C123", ts="111.222"),
            fingerprint="old-fingerprint",
        )
    )
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=marker)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)

    count = engine.reconcile_all(force_refresh_state=True)
    assert count == 1
    assert gh.force_refresh_flags == [True]
    assert len(slack.updates) == 1


def test_run_once_uses_full_reconcile_without_force_refresh() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(
        github=gh,
        slack=slack,
        routes=[route],
        enable_historical_closed_prs=True,
        dry_run=True,
    )

    count = engine.run_once()
    assert count == 1
    assert gh.force_refresh_flags == [False]


def test_reconcile_changed_skips_historical_closed_without_state_comment() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr(
        number=8,
        state=PullRequestState.CLOSED,
        updated_at=datetime.now(timezone.utc) - timedelta(days=7),
    )
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)

    assert engine.refresh_lightweight() == 1
    count = engine.reconcile_changed()

    assert count == 0
    assert gh.comment_calls == 1
    assert slack.posts == []
    assert slack.updates == []
    assert gh.upserts == []


def test_reconcile_changed_noop_logging_works_at_info_level(caplog) -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=True)
    engine.log.setLevel(logging.INFO)

    assert engine.refresh_lightweight() == 1
    with caplog.at_level(logging.INFO, logger="pr_slack_notifier.engine"):
        count = engine.reconcile_changed()

    assert count == 1
    assert "reconcile.noop reason=dry_run" in caplog.text


def test_reconcile_changed_logs_actions_for_emit_visibility(caplog) -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)
    engine.log.setLevel(logging.INFO)

    assert engine.refresh_lightweight() == 1
    with caplog.at_level(logging.INFO, logger="pr_slack_notifier.engine"):
        assert engine.reconcile_changed() == 1

    assert "reconcile.action kind=post_message" in caplog.text


def test_reconcile_changed_fetches_only_pending_prs() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr1 = _pr(number=1, head_sha="sha-1")
    pr2 = _pr(number=2, head_sha="sha-2")
    gh = FakeGitHub(light={"default": [pr1, pr2]}, full={"default": [pr1, pr2]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=True)

    assert engine.refresh_lightweight() == 2
    assert engine.reconcile_changed() == 2
    # Deep reconcile should not request a full enriched route listing anymore.
    assert ("default", True, None) not in gh.list_calls


def test_reconcile_changed_detects_open_to_merged_transition_without_waiting_for_sweep() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    open_pr = _pr(number=42, state=PullRequestState.OPEN)
    merged_pr = _pr(number=42, state=PullRequestState.MERGED, updated_at=open_pr.updated_at + timedelta(minutes=1))

    class TransitionGitHub:
        def __init__(self):
            self.phase = 0
            self.comment = None
            self.upserts: list[tuple[int, str]] = []
            self.comment_calls = 0
            self.force_refresh_flags: list[bool] = []
            self.probe_calls: list[bool] = []

        def list_pull_requests(self, _route, *, include_enrichment=True, updated_after=None):
            if include_enrichment:
                return []
            items = [open_pr] if self.phase == 0 else []
            if updated_after is None:
                return list(items)
            return [pr for pr in items if pr.updated_at is None or pr.updated_at >= updated_after]

        def list_pull_requests_for_sweep(self, _route):
            return []

        def get_pull_request(self, _route, *, org: str, repo: str, number: int, include_enrichment: bool = True):
            assert org == "acme"
            assert repo == "widgets"
            assert number == 42
            self.probe_calls.append(include_enrichment)
            if self.phase == 0:
                return open_pr
            return merged_pr

        def get_bot_state_comment(self, _pr, *, force_refresh=False):
            self.comment_calls += 1
            self.force_refresh_flags.append(force_refresh)
            return self.comment

        def upsert_bot_state_comment(self, pr, body):
            self.upserts.append((pr.number, body))

    gh = TransitionGitHub()
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=True)

    assert engine.refresh_lightweight() == 1
    assert engine.reconcile_changed() == 1  # initial open reconcile

    gh.phase = 1
    assert engine.refresh_lightweight() == 0  # open listing no longer returns merged PR
    assert engine.reconcile_changed() == 1  # deep watchlist picks up merged transition
    assert gh.probe_calls[-2:] == [False, True]

    # Transitioned PR is evicted from open-watch set and not repeatedly reconciled.
    assert engine.reconcile_changed() == 0


def test_reconcile_changed_recent_open_probe_updates_open_check_transition() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    base_updated_at = datetime.now(timezone.utc)
    light_open = _pr(number=51, state=PullRequestState.DRAFT, updated_at=base_updated_at, head_sha="sha-51")
    open_running = PullRequestSnapshot(
        org=light_open.org,
        repo=light_open.repo,
        number=light_open.number,
        title=light_open.title,
        url=light_open.url,
        author=light_open.author,
        state=light_open.state,
        updated_at=light_open.updated_at,
        head_sha=light_open.head_sha,
        check_runs=(CheckRun(name="review / review", status="in_progress", conclusion=None),),
    )
    open_passed = PullRequestSnapshot(
        org=light_open.org,
        repo=light_open.repo,
        number=light_open.number,
        title=light_open.title,
        url=light_open.url,
        author=light_open.author,
        state=light_open.state,
        updated_at=light_open.updated_at,
        head_sha=light_open.head_sha,
        check_runs=(CheckRun(name="review / review", status="completed", conclusion="success"),),
    )

    class OpenTransitionGitHub:
        def __init__(self):
            self.phase = 0
            self.comment = None

        def list_pull_requests(self, _route, *, include_enrichment=True, updated_after=None):
            if include_enrichment:
                return []
            items = [light_open]
            if updated_after is None:
                return list(items)
            return [pr for pr in items if pr.updated_at is None or pr.updated_at >= updated_after]

        def list_pull_requests_for_sweep(self, _route):
            return []

        def get_pull_request(self, _route, *, org: str, repo: str, number: int, include_enrichment: bool = True):
            assert org == "acme"
            assert repo == "widgets"
            assert number == 51
            if not include_enrichment:
                return light_open
            return open_running if self.phase == 0 else open_passed

        def get_bot_state_comment(self, _pr, *, force_refresh=False):
            return self.comment

        def upsert_bot_state_comment(self, _pr, body):
            self.comment = body

    gh = OpenTransitionGitHub()
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)

    assert engine.refresh_lightweight() == 1
    assert engine.reconcile_changed() == 1
    assert len(slack.posts) == 1
    assert len(slack.updates) == 0
    assert "running" in slack.posts[0][1]

    # Lightweight metadata is unchanged, but check-runs transitioned to passed.
    gh.phase = 1
    assert engine.refresh_lightweight() == 0
    assert engine.reconcile_changed() == 1
    assert len(slack.updates) == 1
    assert "✅ passed" in slack.updates[0][2]


def test_reconcile_all_soft_sweep_reconciles_only_changed_refs() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    original = _pr(number=10, head_sha="sha-1")
    gh = FakeGitHub(light={"default": [original]}, full={"default": [original]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=True)

    # Prime metadata cache with an initial lightweight/deep pass.
    assert engine.refresh_lightweight() == 1
    assert engine.reconcile_changed() == 1
    gh.list_calls.clear()

    # Unchanged snapshot should be skipped by soft sweep.
    assert engine.reconcile_all(force_refresh_state=False) == 0
    assert gh.comment_calls == 1  # from initial deep reconcile only
    assert ("default", False, None) in gh.list_calls
    assert ("default", True, None) not in gh.list_calls

    # A changed head SHA should be reconciled on next soft sweep.
    changed = _pr(number=10, head_sha="sha-2", updated_at=original.updated_at)
    gh.light = {"default": [changed]}
    gh.full = {"default": [changed]}
    assert engine.reconcile_all(force_refresh_state=False) == 1


def test_reconcile_sweep_processes_full_route_list() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr_open = _pr(number=21, state=PullRequestState.OPEN)
    pr_closed = _pr(number=22, state=PullRequestState.CLOSED)
    gh = FakeGitHub(light={"default": [pr_open]}, full={"default": [pr_open, pr_closed]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(
        github=gh,
        slack=slack,
        routes=[route],
        enable_historical_closed_prs=True,
        dry_run=True,
    )

    count = engine.reconcile_sweep()
    assert count == 2
    assert len(slack.posts) == 0
    assert gh.force_refresh_flags == [False, False]


def test_reconcile_sweep_forces_comment_refresh_for_rest_fallback_routes() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="service-*", channel="C123")
    pr = _pr(number=30, state=PullRequestState.OPEN)
    gh = FakeGitHub(light={"default": [pr]}, full={"default": [pr]}, comment=None)
    slack = FakeSlack()
    engine = ReconcileEngine(
        github=gh,
        slack=slack,
        routes=[route],
        enable_historical_closed_prs=True,
        dry_run=True,
    )

    assert engine.reconcile_sweep() == 1
    assert gh.force_refresh_flags == [True]
