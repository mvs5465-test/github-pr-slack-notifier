from __future__ import annotations

from .models import ApprovalState, ChecksState, CheckRun, PullRequestSnapshot

_FAILED_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "stale",
    "timed_out",
}
_RUNNING_STATUSES = {"in_progress"}
_PENDING_STATUSES = {"queued", "pending", "requested", "waiting"}
_PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}


def derive_approval_state(pr: PullRequestSnapshot) -> ApprovalState:
    decision = (pr.review_decision or "").strip().upper()
    if decision == "APPROVED":
        return ApprovalState.APPROVED
    if decision == "CHANGES_REQUESTED":
        return ApprovalState.CHANGES_REQUESTED
    return ApprovalState.NEEDS_REVIEW


def derive_checks_state(check_runs: tuple[CheckRun, ...]) -> ChecksState:
    if not check_runs:
        return ChecksState.NO_CHECKS

    statuses = {run.status.lower() for run in check_runs}
    conclusions = {run.conclusion.lower() for run in check_runs if run.conclusion}

    if conclusions & _FAILED_CONCLUSIONS:
        return ChecksState.FAILED
    if statuses & _RUNNING_STATUSES:
        return ChecksState.RUNNING
    if statuses & _PENDING_STATUSES:
        return ChecksState.PENDING
    if conclusions and conclusions <= _PASSING_CONCLUSIONS:
        return ChecksState.PASSED
    return ChecksState.PENDING
