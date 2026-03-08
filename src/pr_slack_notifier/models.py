from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class PullRequestState(str, Enum):
    OPEN = "open"
    DRAFT = "draft"
    CLOSED = "closed"
    MERGED = "merged"


class ApprovalState(str, Enum):
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"


class ChecksState(str, Enum):
    NO_CHECKS = "no_checks"
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    PASSED = "passed"


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None = None


@dataclass(frozen=True)
class PullRequestSnapshot:
    org: str
    repo: str
    number: int
    title: str
    url: str
    author: str
    state: PullRequestState
    head_sha: str = ""
    review_decision: str | None = None
    check_runs: tuple[CheckRun, ...] = ()
    requested_reviewers: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    target_branch: str = "main"
    updated_at: datetime | None = None


@dataclass(frozen=True)
class RouteConfig:
    org_pattern: str
    repo_pattern: str = "*"
    channel: str = ""
    name: str = "default"


@dataclass(frozen=True)
class DerivedStatus:
    approval: ApprovalState
    checks: ChecksState


@dataclass(frozen=True)
class SlackMessageRef:
    channel: str
    ts: str


@dataclass(frozen=True)
class ReconcileState:
    message: SlackMessageRef
    fingerprint: str
    version: int = 1


class ActionKind(str, Enum):
    POST_MESSAGE = "post_message"
    UPDATE_MESSAGE = "update_message"
    UPSERT_COMMENT = "upsert_comment"
    LOG_ONLY = "log_only"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    reason: str
    payload: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanResult:
    actions: tuple[Action, ...]
    state: ReconcileState | None
