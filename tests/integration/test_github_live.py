import os
import time
from dataclasses import dataclass
from typing import Any

import pytest

import httpx
import jwt

from pr_slack_notifier.adapters import GitHubApiError, GitHubAppAdapter, SlackApiAdapter, normalize_private_key
from pr_slack_notifier.engine import ReconcileEngine
from pr_slack_notifier.models import RouteConfig
from pr_slack_notifier.reconcile import derive_status
from pr_slack_notifier.state import parse_state_marker

_REQUIRED_ENV = {
    "INTEGRATION_GITHUB_APP_ID",
    "INTEGRATION_GITHUB_APP_PRIVATE_KEY",
    "INTEGRATION_GITHUB_INSTALLATION_IDS",
    "INTEGRATION_GITHUB_ORG",
    "INTEGRATION_GITHUB_REPO",
    "INTEGRATION_PULL_NUMBER",
}
_SLACK_REQUIRED_ENV = {
    "INTEGRATION_SLACK_BOT_TOKEN",
    "INTEGRATION_SLACK_CHANNEL",
}
_STATEFUL_REQUIRED_ENV = {
    "INTEGRATION_ENABLE_STATEFUL_RECONCILE_TEST",
}
_E2E_POLLING_REQUIRED_ENV = {
    "INTEGRATION_ENABLE_E2E_POLLING_TEST",
    "INTEGRATION_GITHUB_WORKFLOW_TOKEN",
}


pytestmark = pytest.mark.integration



def _missing_required_env() -> list[str]:
    return sorted(key for key in _REQUIRED_ENV if not os.getenv(key))


def _missing_slack_env() -> list[str]:
    return sorted(key for key in _SLACK_REQUIRED_ENV if not os.getenv(key))


def _build_github_app_headers(app_id: str, private_key: str) -> dict[str, str]:
    now = int(time.time())
    app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, algorithm="RS256")
    return {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-pr-slack-notifier-integration",
    }


def _installation_token(app_id: str, private_key: str, installation_id: int) -> str:
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=_build_github_app_headers(app_id, private_key),
        )
        response.raise_for_status()
        return response.json()["token"]


def _workflow_dispatch(
    owner: str,
    repo: str,
    workflow_id: str,
    token: str,
    pull_number: int,
    review_action: str,
    review_body: str,
) -> None:
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-pr-slack-notifier-integration",
            },
            json={
                "ref": "main",
                "inputs": {
                    "pull_number": str(pull_number),
                    "review_action": review_action,
                    "review_body": review_body,
                },
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"workflow dispatch failed: {response.status_code} {response.text}")


def _slack_message_text(channel: str, ts: str, bot_token: str) -> str:
    with httpx.Client(timeout=15.0) as client:
        response = client.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={
                "channel": channel,
                "latest": ts,
                "oldest": ts,
                "inclusive": "true",
                "limit": "1",
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        if not data.get("ok", False):
            error = str(data.get("error", "unknown_error"))
            if error == "missing_scope":
                pytest.skip(
                    "Slack token missing scope for conversations.history "
                    "(add channels:history and/or groups:history to run e2e_polling)."
                )
            raise RuntimeError(f"slack conversations.history failed: {error}")

        messages = data.get("messages", [])
        if not messages:
            raise RuntimeError(f"Slack message not found for channel={channel} ts={ts}")
        return str(messages[0].get("text", ""))


@pytest.mark.skipif(_missing_required_env(), reason="integration env vars not configured")
def test_live_github_adapter_pr_status_contract() -> None:
    app_id = os.environ["INTEGRATION_GITHUB_APP_ID"]
    private_key = normalize_private_key(os.environ["INTEGRATION_GITHUB_APP_PRIVATE_KEY"])
    installation_ids = tuple(
        int(value.strip())
        for value in os.environ["INTEGRATION_GITHUB_INSTALLATION_IDS"].split(",")
        if value.strip()
    )
    org = os.environ["INTEGRATION_GITHUB_ORG"]
    repo = os.environ["INTEGRATION_GITHUB_REPO"]
    pull_number = int(os.environ["INTEGRATION_PULL_NUMBER"])

    adapter = GitHubAppAdapter(
        app_id=app_id,
        private_key_pem=private_key,
        installation_ids=installation_ids,
    )

    route = RouteConfig(name="integration", org_pattern=org, repo_pattern=repo, channel="integration")
    prs = adapter.list_pull_requests(route)

    target = next((pr for pr in prs if pr.number == pull_number), None)
    assert target is not None, f"PR #{pull_number} not found in {org}/{repo}"

    status = derive_status(target)

    expected_approval = os.getenv("INTEGRATION_EXPECTED_APPROVAL", "").strip().lower()
    expected_checks = os.getenv("INTEGRATION_EXPECTED_CHECKS", "").strip().lower()

    if expected_approval:
        assert status.approval.value == expected_approval
    if expected_checks:
        assert status.checks.value == expected_checks


@pytest.mark.skipif(_missing_slack_env(), reason="slack integration env vars not configured")
def test_live_slack_adapter_post_update_contract() -> None:
    token = os.environ["INTEGRATION_SLACK_BOT_TOKEN"]
    channel = os.environ["INTEGRATION_SLACK_CHANNEL"]
    adapter = SlackApiAdapter(bot_token=token)

    nonce = str(time.time_ns())
    original_text = f"[integration] slack adapter post test nonce={nonce}"
    updated_text = f"[integration] slack adapter update test nonce={nonce}"

    ts = adapter.post_message(channel=channel, text=original_text)
    assert ts
    adapter.update_message(channel=channel, ts=ts, text=updated_text)

    # Best-effort cleanup; chat.delete scope may not be enabled in all workspaces.
    try:
        response = httpx.post(
            "https://slack.com/api/chat.delete",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "ts": ts},
            timeout=15.0,
        )
        response.raise_for_status()
    except Exception:
        pass


@dataclass
class _CountingSlack:
    delegate: SlackApiAdapter
    posts: int = 0
    updates: int = 0

    def post_message(self, channel: str, text: str) -> str:
        self.posts += 1
        return self.delegate.post_message(channel, text)

    def update_message(self, channel: str, ts: str, text: str) -> None:
        self.updates += 1
        self.delegate.update_message(channel, ts, text)


@dataclass
class _SinglePrGitHub:
    delegate: GitHubAppAdapter
    route: RouteConfig
    pull_number: int

    def list_pull_requests(self, _route: RouteConfig):
        prs = self.delegate.list_pull_requests(self.route)
        target = next((pr for pr in prs if pr.number == self.pull_number), None)
        assert target is not None, f"PR #{self.pull_number} not found"
        return [target]

    def get_bot_state_comment(self, pr):
        return self.delegate.get_bot_state_comment(pr)

    def upsert_bot_state_comment(self, pr, body: str):
        self.delegate.upsert_bot_state_comment(pr, body)


@pytest.mark.skipif(
    _missing_required_env() + _missing_slack_env(),
    reason="stateful integration env vars not configured",
)
def test_live_reconcile_persists_hidden_comment_state_and_reuses_message() -> None:
    if os.getenv("INTEGRATION_ENABLE_STATEFUL_RECONCILE_TEST", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        pytest.skip("stateful integration test disabled; set INTEGRATION_ENABLE_STATEFUL_RECONCILE_TEST=true")

    app_id = os.environ["INTEGRATION_GITHUB_APP_ID"]
    private_key = normalize_private_key(os.environ["INTEGRATION_GITHUB_APP_PRIVATE_KEY"])
    installation_ids = tuple(
        int(value.strip())
        for value in os.environ["INTEGRATION_GITHUB_INSTALLATION_IDS"].split(",")
        if value.strip()
    )
    org = os.environ["INTEGRATION_GITHUB_ORG"]
    repo = os.environ["INTEGRATION_GITHUB_REPO"]
    pull_number = int(os.environ["INTEGRATION_PULL_NUMBER"])
    channel = os.environ["INTEGRATION_SLACK_CHANNEL"]

    route = RouteConfig(name="integration", org_pattern=org, repo_pattern=repo, channel=channel)
    github_delegate = GitHubAppAdapter(
        app_id=app_id,
        private_key_pem=private_key,
        installation_ids=installation_ids,
    )
    github = _SinglePrGitHub(delegate=github_delegate, route=route, pull_number=pull_number)
    slack = _CountingSlack(delegate=SlackApiAdapter(bot_token=os.environ["INTEGRATION_SLACK_BOT_TOKEN"]))

    # First run should post/update and persist marker state for the target PR.
    engine = ReconcileEngine(github=github, slack=slack, routes=[route], dry_run=False)
    try:
        assert engine.run_once() == 1
    except GitHubApiError as exc:
        message = str(exc)
        if (
            "403" in message
            and "Resource not accessible by integration" in message
            and f"/repos/{org}/{repo}/issues/{pull_number}/comments" in message
        ):
            pytest.fail(
                "GitHub App cannot write PR comments. "
                "Grant repository permission 'Issues: Read and write' "
                "(or equivalent comment-write permission), then reinstall/update the app installation."
            )
        raise

    pr = github.list_pull_requests(route)[0]
    marker_body = github.get_bot_state_comment(pr)
    parsed = parse_state_marker(marker_body)
    assert parsed is not None
    assert parsed.message.channel == channel
    assert parsed.message.ts
    assert slack.posts >= 1

    # Second immediate run should not post another message for same PR.
    slack.posts = 0
    slack.updates = 0
    assert engine.run_once() == 1
    assert slack.posts == 0


@pytest.mark.e2e_polling
@pytest.mark.skipif(
    _missing_required_env() + _missing_slack_env(),
    reason="e2e polling integration env vars not configured",
)
def test_e2e_polling_sla_review_transition_to_slack() -> None:
    if os.getenv("INTEGRATION_ENABLE_E2E_POLLING_TEST", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("e2e polling test disabled; set INTEGRATION_ENABLE_E2E_POLLING_TEST=true")

    missing = sorted(k for k in _E2E_POLLING_REQUIRED_ENV if not os.getenv(k))
    if missing:
        pytest.skip(f"missing required e2e polling env vars: {', '.join(missing)}")

    app_id = os.environ["INTEGRATION_GITHUB_APP_ID"]
    private_key = normalize_private_key(os.environ["INTEGRATION_GITHUB_APP_PRIVATE_KEY"])
    installation_id = int(os.environ["INTEGRATION_GITHUB_INSTALLATION_IDS"].split(",")[0].strip())
    org = os.environ["INTEGRATION_GITHUB_ORG"]
    repo = os.environ["INTEGRATION_GITHUB_REPO"]
    pull_number = int(os.environ["INTEGRATION_PULL_NUMBER"])
    workflow_token = os.environ["INTEGRATION_GITHUB_WORKFLOW_TOKEN"]

    slack_token = os.environ["INTEGRATION_SLACK_BOT_TOKEN"]
    expected_review_action = os.getenv("INTEGRATION_E2E_REVIEW_ACTION", "REQUEST_CHANGES").strip().upper()
    workflow_id = os.getenv("INTEGRATION_E2E_WORKFLOW_ID", "pr-review-bot.yml").strip()
    poll_seconds = float(os.getenv("INTEGRATION_E2E_POLL_SECONDS", "3"))
    timeout_seconds = float(os.getenv("INTEGRATION_E2E_TIMEOUT_SECONDS", "45"))
    expected_text = {
        "APPROVE": "✅ approved",
        "REQUEST_CHANGES": "❌ changes requested",
    }.get(expected_review_action)
    if not expected_text:
        pytest.skip("INTEGRATION_E2E_REVIEW_ACTION must be APPROVE or REQUEST_CHANGES")

    route = RouteConfig(name="integration", org_pattern=org, repo_pattern=repo, channel=os.environ["INTEGRATION_SLACK_CHANNEL"])
    github = GitHubAppAdapter(app_id=app_id, private_key_pem=private_key, installation_ids=(installation_id,))
    pr = next((item for item in github.list_pull_requests(route) if item.number == pull_number), None)
    assert pr is not None, f"PR #{pull_number} not found in {org}/{repo}"

    marker_body = github.get_bot_state_comment(pr)
    parsed = parse_state_marker(marker_body)
    assert parsed is not None, "State marker missing; run one reconcile first so message ts is known"

    channel = parsed.message.channel
    ts = parsed.message.ts
    baseline_text = _slack_message_text(channel=channel, ts=ts, bot_token=slack_token)

    review_body = f"[integration e2e_polling] set {expected_review_action} at {int(time.time())}"
    _workflow_dispatch(
        owner=org,
        repo=repo,
        workflow_id=workflow_id,
        token=workflow_token,
        pull_number=pull_number,
        review_action=expected_review_action,
        review_body=review_body,
    )

    started = time.monotonic()
    deadline = started + timeout_seconds
    latest_text = baseline_text
    while time.monotonic() < deadline:
        latest_text = _slack_message_text(channel=channel, ts=ts, bot_token=slack_token)
        if expected_text in latest_text and latest_text != baseline_text:
            elapsed = time.monotonic() - started
            assert elapsed <= timeout_seconds
            return
        time.sleep(poll_seconds)

    pytest.fail(
        f"Slack message did not converge within {timeout_seconds:.0f}s; "
        f"expected snippet={expected_text!r}; baseline={baseline_text!r}; latest={latest_text!r}"
    )
