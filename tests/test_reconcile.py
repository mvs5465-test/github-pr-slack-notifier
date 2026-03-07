from pr_slack_notifier.models import (
    CheckRun,
    Action,
    ActionKind,
    ChecksState,
    PullRequestSnapshot,
    PullRequestState,
    ReconcileState,
    RouteConfig,
    SlackMessageRef,
)
from pr_slack_notifier.plugins import PluginContext
from pr_slack_notifier.reconcile import build_message, compact_state_label, derive_status, plan_reconcile


class MainBranchAlertPlugin:
    name = "main-branch-alert"

    def on_plan(self, context: PluginContext):
        if context.pr.target_branch == "main" and context.pr.state is PullRequestState.CLOSED:
            return (
                Action(
                    kind=ActionKind.LOG_ONLY,
                    reason="main_branch_closed",
                    payload={"repo": context.pr.repo, "number": str(context.pr.number)},
                ),
            )
        return ()


def _pr(state: PullRequestState = PullRequestState.OPEN, decision: str | None = None) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        org="acme",
        repo="widgets",
        number=42,
        title="Add thing",
        url="https://example.com/pr/42",
        author="matt",
        state=state,
        review_decision=decision,
    )


def test_compact_state_label() -> None:
    assert compact_state_label("open") == "open"
    assert compact_state_label("merged") == "merged"


def test_build_message_contains_required_fields() -> None:
    pr = _pr(decision="APPROVED")
    status = derive_status(pr)
    msg = build_message(pr, status)
    assert msg.text.startswith("🟢 open | <https://example.com/pr/42|Add thing> by `matt` | *widgets* |")
    assert "|Add thing>" in msg.text
    assert "matt" in msg.text
    assert "*widgets*" in msg.text
    assert "_✅ approved_" in msg.text
    assert "_⚪ no checks_" in msg.text
    assert len(msg.fingerprint) == 64


def test_build_message_escapes_pipe_in_title() -> None:
    pr = PullRequestSnapshot(
        org="acme",
        repo="widgets",
        number=42,
        title="Add | thing",
        url="https://example.com/pr/42",
        author="matt",
        state=PullRequestState.OPEN,
        review_decision="APPROVED",
    )
    status = derive_status(pr)
    msg = build_message(pr, status)
    assert "<https://example.com/pr/42|Add ¦ thing>" in msg.text


def test_build_message_hides_approval_and_checks_for_closed() -> None:
    pr = _pr(state=PullRequestState.CLOSED, decision="APPROVED")
    status = derive_status(pr)
    msg = build_message(pr, status)
    assert msg.text.startswith("_⚫ closed_ | <https://example.com/pr/42|Add thing> by `matt` | *widgets*")
    assert "approved" not in msg.text
    assert "checks" not in msg.text


def test_build_message_hides_approval_and_checks_for_merged() -> None:
    pr = _pr(state=PullRequestState.MERGED, decision="APPROVED")
    status = derive_status(pr)
    msg = build_message(pr, status)
    assert msg.text.startswith("_🟣 merged_ | <https://example.com/pr/42|Add thing> by `matt` | *widgets*")
    assert "approved" not in msg.text
    assert "checks" not in msg.text


def test_build_message_fingerprint_changes_when_title_changes() -> None:
    pr1 = _pr(decision="APPROVED")
    pr2 = PullRequestSnapshot(
        org=pr1.org,
        repo=pr1.repo,
        number=pr1.number,
        title="Different title",
        url=pr1.url,
        author=pr1.author,
        state=pr1.state,
        review_decision=pr1.review_decision,
        check_runs=pr1.check_runs,
        requested_reviewers=pr1.requested_reviewers,
        labels=pr1.labels,
        target_branch=pr1.target_branch,
    )
    m1 = build_message(pr1, derive_status(pr1))
    m2 = build_message(pr2, derive_status(pr2))
    assert m1.fingerprint != m2.fingerprint


def test_plan_posts_when_missing_state() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    plan = plan_reconcile(pr=_pr(), route=route, existing=None, dry_run=False)
    assert [a.kind for a in plan.actions] == [ActionKind.POST_MESSAGE, ActionKind.UPSERT_COMMENT]
    assert plan.state is not None
    assert plan.state.message.channel == "C123"


def test_plan_updates_when_fingerprint_changes() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    existing = ReconcileState(
        message=SlackMessageRef(channel="C123", ts="111.222"),
        fingerprint="old",
    )
    plan = plan_reconcile(pr=_pr(), route=route, existing=existing, dry_run=False)
    assert [a.kind for a in plan.actions] == [ActionKind.UPDATE_MESSAGE, ActionKind.UPSERT_COMMENT]


def test_plan_noop_when_fingerprint_same() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    pr = _pr()
    msg = build_message(pr, derive_status(pr))
    existing = ReconcileState(
        message=SlackMessageRef(channel="C123", ts="111.222"),
        fingerprint=msg.fingerprint,
    )
    plan = plan_reconcile(pr=pr, route=route, existing=existing, dry_run=False)
    assert [a.kind for a in plan.actions] == [ActionKind.LOG_ONLY]


def test_plan_dry_run_logs_only() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    plan = plan_reconcile(pr=_pr(), route=route, existing=None, dry_run=True)
    assert all(a.kind is ActionKind.LOG_ONLY for a in plan.actions)


def test_plan_runs_plugin() -> None:
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C123")
    plan = plan_reconcile(
        pr=_pr(state=PullRequestState.CLOSED),
        route=route,
        existing=None,
        dry_run=False,
        plugins=(MainBranchAlertPlugin(),),
    )
    assert any(a.reason == "main_branch_closed" for a in plan.actions)


def test_derive_status_with_failed_check() -> None:
    pr = PullRequestSnapshot(
        org="acme",
        repo="widgets",
        number=42,
        title="Add thing",
        url="https://example.com/pr/42",
        author="matt",
        state=PullRequestState.OPEN,
        review_decision="REVIEW_REQUIRED",
        check_runs=(
            # failure takes priority over pending/running
            CheckRun(name="ci", status="completed", conclusion="failure"),
        ),
    )
    status = derive_status(pr)
    assert status.checks is ChecksState.FAILED
