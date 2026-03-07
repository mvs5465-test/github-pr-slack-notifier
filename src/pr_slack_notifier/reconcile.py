from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import (
    Action,
    ActionKind,
    ApprovalState,
    ChecksState,
    DerivedStatus,
    PlanResult,
    PullRequestSnapshot,
    ReconcileState,
    RouteConfig,
    SlackMessageRef,
)
from .plugins import Plugin, PluginContext
from .state import make_fingerprint
from .status import derive_approval_state, derive_checks_state


@dataclass(frozen=True)
class PlannedMessage:
    text: str
    fingerprint: str


class Logger(Protocol):
    def info(self, msg: str, **kwargs: object) -> None:
        ...


def compact_state_label(state: str) -> str:
    return {
        "open": "opened",
        "closed": "closed",
        "merged": "merged",
    }.get(state.lower(), state.lower())


def _approval_label(state: ApprovalState) -> str:
    return {
        ApprovalState.NEEDS_REVIEW: "needs review",
        ApprovalState.APPROVED: "approved",
        ApprovalState.CHANGES_REQUESTED: "changes requested",
    }[state]


def _checks_label(state: ChecksState) -> str:
    return {
        ChecksState.NO_CHECKS: "no checks",
        ChecksState.PENDING: "pending",
        ChecksState.RUNNING: "running",
        ChecksState.FAILED: "failed",
        ChecksState.PASSED: "passed",
    }[state]


def build_message(pr: PullRequestSnapshot, status: DerivedStatus) -> PlannedMessage:
    text = (
        f"[{pr.repo}] <{pr.url}|PR #{pr.number}> by `{pr.author}` | "
        f"state: {compact_state_label(pr.state.value)} | "
        f"approval: {_approval_label(status.approval)} | "
        f"checks: {_checks_label(status.checks)}"
    )
    fingerprint = make_fingerprint(
        [
            pr.repo,
            str(pr.number),
            pr.state.value,
            status.approval.value,
            status.checks.value,
            ",".join(pr.requested_reviewers),
        ]
    )
    return PlannedMessage(text=text, fingerprint=fingerprint)


def derive_status(pr: PullRequestSnapshot) -> DerivedStatus:
    return DerivedStatus(
        approval=derive_approval_state(pr),
        checks=derive_checks_state(pr.check_runs),
    )


def plan_reconcile(
    pr: PullRequestSnapshot,
    route: RouteConfig,
    existing: ReconcileState | None,
    dry_run: bool,
    plugins: tuple[Plugin, ...] = (),
) -> PlanResult:
    status = derive_status(pr)
    planned = build_message(pr, status)
    next_state = ReconcileState(
        message=existing.message if existing else SlackMessageRef(channel=route.channel, ts=""),
        fingerprint=planned.fingerprint,
    )

    actions: list[Action] = []

    if existing is None or not existing.message.ts:
        actions.append(
            Action(
                kind=ActionKind.POST_MESSAGE,
                reason="missing_message_ref",
                payload={"channel": route.channel, "text": planned.text},
            )
        )
    elif existing.fingerprint != planned.fingerprint:
        actions.append(
            Action(
                kind=ActionKind.UPDATE_MESSAGE,
                reason="state_changed",
                payload={"channel": existing.message.channel, "ts": existing.message.ts, "text": planned.text},
            )
        )
    else:
        actions.append(Action(kind=ActionKind.LOG_ONLY, reason="already_reconciled"))

    context = PluginContext(pr=pr, route=route, status=status)
    for plugin in plugins:
        actions.extend(plugin.on_plan(context))

    needs_comment_update = any(
        a.kind in {ActionKind.POST_MESSAGE, ActionKind.UPDATE_MESSAGE} for a in actions
    )
    if needs_comment_update:
        actions.append(
            Action(
                kind=ActionKind.UPSERT_COMMENT,
                reason="persist_message_ref",
                payload={"channel": route.channel, "fingerprint": planned.fingerprint},
            )
        )

    if dry_run:
        actions = [
            Action(kind=ActionKind.LOG_ONLY, reason="dry_run", payload={"would": a.kind.value, **a.payload})
            for a in actions
        ]

    return PlanResult(actions=tuple(actions), state=next_state)
