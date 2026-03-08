import json
from datetime import datetime, timezone

import httpx
import pytest

from pr_slack_notifier.adapters import GitHubAppAdapter, GitHubRateLimitError, SlackApiAdapter, normalize_private_key
from pr_slack_notifier.models import (
    PullRequestSnapshot,
    PullRequestState,
    ReconcileState,
    RouteConfig,
    SlackMessageRef,
)
from pr_slack_notifier.state import render_state_marker


def _json_response(data: dict | list, status: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status, json=data)


def test_normalize_private_key_handles_escaped_newlines() -> None:
    key = "line1\\nline2"
    assert normalize_private_key(key) == "line1\nline2"


def test_rate_limit_error_retry_uses_reset_without_max_clamp() -> None:
    err = GitHubRateLimitError("rate", reset_at_epoch=300.0, resource="core")
    assert err.retry_after_seconds(now_epoch=100.0, default_seconds=60, max_seconds=120) == 201


def test_github_adapter_lists_prs_and_comments() -> None:
    marker = render_state_marker(
        ReconcileState(
            message=SlackMessageRef(channel="C1", ts="123.456"),
            fingerprint="abc",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/search/issues":
            return _json_response(
                {
                    "items": [
                        {
                            "number": 7,
                            "repository_url": "https://api.github.com/repos/acme/service",
                        }
                    ]
                }
            )
        if path == "/repos/acme/service/pulls/7":
            return _json_response(
                {
                    "number": 7,
                    "title": "Hello",
                    "html_url": "https://github.com/acme/service/pull/7",
                    "state": "open",
                    "merged_at": None,
                    "review_decision": None,
                    "user": {"login": "matt"},
                    "head": {"sha": "deadbeef"},
                    "updated_at": "2026-01-01T00:00:00Z",
                    "base": {"ref": "main"},
                    "requested_reviewers": [{"login": "dev1"}],
                    "labels": [{"name": "safe"}],
                }
            )
        if path == "/repos/acme/service/pulls/7/reviews":
            return _json_response(
                [
                    {"state": "APPROVED"},
                    {"state": "COMMENTED"},
                    {"state": "CHANGES_REQUESTED"},
                ]
            )
        if path == "/repos/acme/service/commits/deadbeef/check-runs":
            return _json_response(
                {
                    "check_runs": [
                        {"name": "ci", "status": "completed", "conclusion": "success"},
                    ]
                }
            )
        if path == "/repos/acme/service/issues/7/comments" and request.method == "GET":
            return _json_response([{"id": 99, "body": marker}])
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]

    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C1")
    prs = adapter.list_pull_requests(route)
    assert len(prs) == 1
    assert prs[0].repo == "service"
    assert prs[0].author == "matt"
    assert prs[0].head_sha == "deadbeef"
    assert prs[0].requested_reviewers == ("dev1",)
    assert prs[0].review_decision == "CHANGES_REQUESTED"

    comment = adapter.get_bot_state_comment(prs[0])
    assert comment is not None
    assert "pr-slack-notifier" in comment


def test_github_adapter_upsert_updates_existing_comment() -> None:
    calls: list[tuple[str, str, dict | None]] = []
    marker = render_state_marker(
        ReconcileState(
            message=SlackMessageRef(channel="C1", ts="123.456"),
            fingerprint="abc",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        payload = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, path, payload))

        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return _json_response({"repositories": [{"name": "service", "owner": {"login": "acme"}}]})
        if path == "/repos/acme/service/issues/7/comments" and request.method == "GET":
            return _json_response([{"id": 77, "body": marker}])
        if path == "/repos/acme/service/issues/comments/77" and request.method == "PATCH":
            return _json_response({"id": 77})
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]

    pr = PullRequestSnapshot(
        org="acme",
        repo="service",
        number=7,
        title="Title",
        url="https://github.com/acme/service/pull/7",
        author="matt",
        state=PullRequestState.OPEN,
    )
    adapter.upsert_bot_state_comment(pr, "<!-- pr-slack-notifier:{\"v\":1,\"x\":2} -->")

    assert any(method == "PATCH" and path.endswith("/issues/comments/77") for method, path, _ in calls)


def test_github_adapter_comment_cache_and_force_refresh() -> None:
    calls: list[tuple[str, str]] = []
    marker = render_state_marker(
        ReconcileState(
            message=SlackMessageRef(channel="C1", ts="123.456"),
            fingerprint="abc",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return _json_response({"repositories": [{"name": "service", "owner": {"login": "acme"}}]})
        if path == "/repos/acme/service/issues/7/comments" and request.method == "GET":
            return _json_response([{"id": 77, "body": marker}])
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]
    pr = PullRequestSnapshot(
        org="acme",
        repo="service",
        number=7,
        title="Title",
        url="https://github.com/acme/service/pull/7",
        author="matt",
        state=PullRequestState.OPEN,
    )

    assert adapter.get_bot_state_comment(pr) is not None
    assert adapter.get_bot_state_comment(pr) is not None
    assert adapter.get_bot_state_comment(pr, force_refresh=True) is not None

    comment_gets = [call for call in calls if call == ("GET", "/repos/acme/service/issues/7/comments")]
    assert len(comment_gets) == 2


def test_github_adapter_lightweight_mode_and_updated_after_filter() -> None:
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path, request.url.query.decode("utf-8")))
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/search/issues":
            return _json_response(
                {
                    "items": [
                        {
                            "number": 8,
                            "title": "New",
                            "html_url": "https://github.com/acme/service/pull/8",
                            "state": "open",
                            "user": {"login": "matt"},
                            "updated_at": "2026-03-07T12:00:00Z",
                            "repository_url": "https://api.github.com/repos/acme/service",
                            "labels": [{"name": "ready"}],
                        },
                    ]
                }
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C1")

    cutoff = datetime(2026, 3, 7, 11, 0, tzinfo=timezone.utc)
    prs = adapter.list_pull_requests(route, include_enrichment=False, updated_after=cutoff)
    assert [pr.number for pr in prs] == [8]
    assert prs[0].head_sha == ""
    assert prs[0].labels == ("ready",)
    search_calls = [call for call in calls if call[1] == "/search/issues"]
    assert len(search_calls) == 1
    assert "is%3Apr" in search_calls[0][2]
    assert "updated%3A%3E%3D2026-03-07T11%3A00%3A00Z" in search_calls[0][2]


def test_github_adapter_lightweight_mode_wildcard_org_falls_back_to_repo_listing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return _json_response({"repositories": [{"name": "service", "owner": {"login": "acme"}}]})
        if path == "/repos/acme/service/pulls":
            return _json_response(
                [
                    {
                        "number": 8,
                        "title": "New",
                        "html_url": "https://github.com/acme/service/pull/8",
                        "state": "open",
                        "merged_at": None,
                        "review_decision": None,
                        "user": {"login": "matt"},
                        "head": {"sha": "newsha"},
                        "updated_at": "2026-03-07T12:00:00Z",
                        "base": {"ref": "main"},
                    }
                ]
            )
        if path == "/search/issues":
            raise AssertionError("search endpoint should not be used for wildcard org routes")
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]
    route = RouteConfig(name="default", org_pattern="ac*", repo_pattern="*", channel="C1")
    prs = adapter.list_pull_requests(route, include_enrichment=False)
    assert [pr.number for pr in prs] == [8]


def test_github_adapter_raises_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/search/issues":
            return httpx.Response(
                status_code=403,
                headers={"x-ratelimit-reset": "200", "x-ratelimit-resource": "core"},
                json={"message": "API rate limit exceeded for installation ID 1"},
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C1")

    with pytest.raises(GitHubRateLimitError) as exc:
        adapter.list_pull_requests(route, include_enrichment=False)
    assert exc.value.reset_at_epoch == 200.0
    assert exc.value.resource == "core"


def test_github_adapter_proactively_uses_rate_limit_headers_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/installations/1/access_tokens":
            return _json_response({"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/search/issues":
            return httpx.Response(
                status_code=200,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "300",
                    "x-ratelimit-resource": "search",
                },
                json={"items": []},
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.github.com")
    adapter = GitHubAppAdapter(
        app_id="123",
        private_key_pem="unused",
        installation_ids=(1,),
        client=client,
    )
    adapter._build_app_jwt = lambda: "app-jwt"  # type: ignore[method-assign]
    route = RouteConfig(name="default", org_pattern="acme", repo_pattern="*", channel="C1")

    with pytest.raises(GitHubRateLimitError) as exc:
        adapter.list_pull_requests(route, include_enrichment=False)
    assert exc.value.reset_at_epoch == 300.0
    assert exc.value.resource == "search"


def test_slack_adapter_post_and_update() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append((request.url.path, payload))
        if request.url.path == "/api/chat.postMessage":
            return _json_response({"ok": True, "ts": "111.222"})
        if request.url.path == "/api/chat.update":
            return _json_response({"ok": True, "ts": "111.222"})
        raise AssertionError(f"unexpected slack path {request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://slack.com")
    adapter = SlackApiAdapter(bot_token="xoxb", client=client)

    ts = adapter.post_message("C1", "hello")
    adapter.update_message("C1", ts, "hello2")

    assert ts == "111.222"
    assert calls[0][1]["channel"] == "C1"
    assert calls[0][1]["unfurl_links"] is False
    assert calls[0][1]["unfurl_media"] is False
    assert calls[1][1]["text"] == "hello2"


def test_slack_adapter_raises_on_ok_false() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response({"ok": False, "error": "channel_not_found"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://slack.com")
    adapter = SlackApiAdapter(bot_token="xoxb", client=client)

    with pytest.raises(RuntimeError, match="channel_not_found"):
        adapter.post_message("C1", "hello")
