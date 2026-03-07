from pr_slack_notifier.models import CheckRun, ChecksState, PullRequestSnapshot, PullRequestState
from pr_slack_notifier.status import derive_approval_state, derive_checks_state


def _pr(review_decision: str | None = None) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        org="acme",
        repo="service",
        number=1,
        title="hello",
        url="https://example.com/pr/1",
        author="matt",
        state=PullRequestState.OPEN,
        review_decision=review_decision,
    )


def test_approval_defaults_to_needs_review() -> None:
    assert derive_approval_state(_pr()) == "needs_review"


def test_approval_approved() -> None:
    assert derive_approval_state(_pr("approved")) == "approved"


def test_approval_changes_requested() -> None:
    assert derive_approval_state(_pr("CHANGES_REQUESTED")) == "changes_requested"


def test_checks_no_checks() -> None:
    assert derive_checks_state(()) is ChecksState.NO_CHECKS


def test_checks_failed_wins() -> None:
    checks = (
        CheckRun(name="ci", status="completed", conclusion="success"),
        CheckRun(name="security", status="completed", conclusion="failure"),
    )
    assert derive_checks_state(checks) is ChecksState.FAILED


def test_checks_running() -> None:
    checks = (CheckRun(name="ci", status="in_progress", conclusion=None),)
    assert derive_checks_state(checks) is ChecksState.RUNNING


def test_checks_pending() -> None:
    checks = (CheckRun(name="ci", status="queued", conclusion=None),)
    assert derive_checks_state(checks) is ChecksState.PENDING


def test_checks_passed() -> None:
    checks = (
        CheckRun(name="ci", status="completed", conclusion="success"),
        CheckRun(name="lint", status="completed", conclusion="neutral"),
    )
    assert derive_checks_state(checks) is ChecksState.PASSED


def test_checks_unknown_completion_defaults_pending() -> None:
    checks = (CheckRun(name="ci", status="completed", conclusion=None),)
    assert derive_checks_state(checks) is ChecksState.PENDING
