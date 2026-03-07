from pr_slack_notifier.engine import ReconcileEngine
from pr_slack_notifier.models import (
    Action,
    ActionKind,
    PlanResult,
    PullRequestSnapshot,
    PullRequestState,
    ReconcileState,
    RouteConfig,
    SlackMessageRef,
)
from pr_slack_notifier.state import render_state_marker


class FakeGitHub:
    def __init__(self, prs, comment=None):
        self._prs = prs
        self.comment = comment
        self.upserts = []

    def list_pull_requests(self, route):
        return self._prs

    def get_bot_state_comment(self, pr):
        return self.comment

    def upsert_bot_state_comment(self, pr, body):
        self.upserts.append((pr.number, body))


class FakeSlack:
    def __init__(self):
        self.posts = []
        self.updates = []

    def post_message(self, channel, text):
        self.posts.append((channel, text))
        return "123.456"

    def update_message(self, channel, ts, text):
        self.updates.append((channel, ts, text))


def _pr() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        org="acme",
        repo="widgets",
        number=7,
        title="Title",
        url="https://example.com/pr/7",
        author="matt",
        state=PullRequestState.OPEN,
    )


def test_run_once_posts_and_persists_comment() -> None:
    pr = _pr()
    gh = FakeGitHub([pr], comment=None)
    slack = FakeSlack()
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")

    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)
    count = engine.run_once()

    assert count == 1
    assert len(slack.posts) == 1
    assert len(gh.upserts) == 1
    assert "pr-slack-notifier" in gh.upserts[0][1]


def test_run_once_dry_run_no_side_effect_calls() -> None:
    pr = _pr()
    gh = FakeGitHub([pr], comment=None)
    slack = FakeSlack()
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")

    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=True)
    count = engine.run_once()

    assert count == 1
    assert slack.posts == []
    assert slack.updates == []
    assert gh.upserts == []


def test_run_once_updates_existing_message() -> None:
    pr = _pr()
    gh = FakeGitHub(
        [pr],
        comment=render_state_marker(
            ReconcileState(
                message=SlackMessageRef(channel="C123", ts="111.222"),
                fingerprint="old-fingerprint",
            )
        ),
    )
    slack = FakeSlack()
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")

    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)
    count = engine.run_once()

    assert count == 1
    assert slack.posts == []
    assert len(slack.updates) == 1
    assert len(gh.upserts) == 1


def test_run_once_skips_upsert_when_planner_has_no_state(monkeypatch) -> None:
    pr = _pr()
    gh = FakeGitHub([pr], comment=None)
    slack = FakeSlack()
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")

    monkeypatch.setattr(
        "pr_slack_notifier.engine.plan_reconcile",
        lambda **_kwargs: PlanResult(
            actions=(Action(kind=ActionKind.UPSERT_COMMENT, reason="forced"),),
            state=None,
        ),
    )

    engine = ReconcileEngine(github=gh, slack=slack, routes=[route], dry_run=False)
    count = engine.run_once()

    assert count == 1
    assert gh.upserts == []
