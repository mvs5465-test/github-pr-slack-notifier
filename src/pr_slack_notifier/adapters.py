from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from opentelemetry import trace

from .models import CheckRun, PullRequestSnapshot, PullRequestState, RouteConfig
from .observability import observe_api_request
from .state import parse_state_marker


@dataclass(frozen=True)
class _InstallationToken:
    token: str
    expires_at_epoch: float


class GitHubApiError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubApiError):
    def __init__(self, message: str, reset_at_epoch: float | None = None) -> None:
        super().__init__(message)
        self.reset_at_epoch = reset_at_epoch

    def retry_after_seconds(self, now_epoch: float, default_seconds: int, max_seconds: int) -> int:
        if self.reset_at_epoch is None:
            return min(default_seconds, max_seconds)
        retry = int(self.reset_at_epoch - now_epoch) + 1
        if retry < 1:
            retry = 1
        return min(retry, max_seconds)


class SlackApiError(RuntimeError):
    pass


class GitHubAppAdapter:
    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_ids: tuple[int, ...],
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.installation_ids = installation_ids
        self.api_base_url = api_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._token_cache: dict[int, _InstallationToken] = {}
        self._comment_id_cache: dict[tuple[str, str, int], int] = {}
        self._comment_body_cache: dict[tuple[str, str, int], str | None] = {}
        self._repo_installation_cache: dict[tuple[str, str], int] = {}
        self._repo_list_cache: dict[int, tuple[float, list[tuple[str, str]]]] = {}
        self._repo_list_cache_ttl_seconds = 300
        self._tracer = trace.get_tracer(__name__)

    def _build_app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")

    def _request(self, method: str, path: str, token: str, json_body: dict[str, Any] | None = None) -> Any:
        started = time.monotonic()
        with self._tracer.start_as_current_span("github_api_request") as span:
            span.set_attribute("http.method", method)
            span.set_attribute("http.target", path)
            response = self._client.request(
                method,
                f"{self.api_base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "github-pr-slack-notifier",
                },
                json=json_body,
            )
            span.set_attribute("http.status_code", response.status_code)
        observe_api_request("github", path, response.status_code, time.monotonic() - started)

        remaining: int | None = None
        remaining_header = response.headers.get("x-ratelimit-remaining")
        if remaining_header is not None:
            try:
                remaining = int(float(remaining_header))
            except ValueError:
                remaining = None

        reset_at_epoch: float | None = None
        reset_header = response.headers.get("x-ratelimit-reset")
        if reset_header:
            try:
                reset_at_epoch = float(reset_header)
            except ValueError:
                reset_at_epoch = None

        # Proactive handling: if a successful response reports no remaining quota,
        # short-circuit now and let the control loop wait until reset.
        if response.status_code < 400 and remaining is not None and remaining <= 0:
            raise GitHubRateLimitError(
                f"GitHub API {method} {path} exhausted rate limit",
                reset_at_epoch=reset_at_epoch,
            )

        if response.status_code >= 400:
            if response.status_code == 403:
                body_text = response.text or ""
                try:
                    payload = response.json()
                except json.JSONDecodeError:
                    payload = {}
                message = str(payload.get("message", body_text)).lower()
                if "rate limit" in message or (remaining is not None and remaining <= 0):
                    raise GitHubRateLimitError(
                        f"GitHub API {method} {path} failed: {response.status_code} {response.text}",
                        reset_at_epoch=reset_at_epoch,
                    )
            raise GitHubApiError(f"GitHub API {method} {path} failed: {response.status_code} {response.text}")
        if response.text:
            return response.json()
        return None

    def _installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        now = time.time()
        if cached and (cached.expires_at_epoch - 60) > now:
            return cached.token

        app_jwt = self._build_app_jwt()
        data = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=app_jwt,
        )
        expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._token_cache[installation_id] = _InstallationToken(token=data["token"], expires_at_epoch=expires)
        return data["token"]

    @staticmethod
    def _pr_state(merged_at: str | None, state: str) -> PullRequestState:
        if merged_at:
            return PullRequestState.MERGED
        if state.lower() == "closed":
            return PullRequestState.CLOSED
        return PullRequestState.OPEN

    def _iter_matching_repositories(self, route: RouteConfig, force_refresh: bool = False) -> list[tuple[int, str, str]]:
        repos: list[tuple[int, str, str]] = []
        now = time.time()
        for installation_id in self.installation_ids:
            cached = self._repo_list_cache.get(installation_id)
            repo_pairs: list[tuple[str, str]]
            if (
                not force_refresh
                and cached is not None
                and now < (cached[0] + self._repo_list_cache_ttl_seconds)
            ):
                repo_pairs = cached[1]
            else:
                token = self._installation_token(installation_id)
                repo_pairs = []
                page = 1
                while True:
                    data = self._request(
                        "GET",
                        f"/installation/repositories?per_page=100&page={page}",
                        token=token,
                    )
                    page_items = data.get("repositories", [])
                    for repo in page_items:
                        org = repo["owner"]["login"]
                        name = repo["name"]
                        repo_pairs.append((org, name))
                        self._repo_installation_cache[(org, name)] = installation_id
                    if len(page_items) < 100:
                        break
                    page += 1
                self._repo_list_cache[installation_id] = (now, repo_pairs)
            for org, name in repo_pairs:
                self._repo_installation_cache[(org, name)] = installation_id
                if fnmatch.fnmatch(org, route.org_pattern) and fnmatch.fnmatch(name, route.repo_pattern):
                    repos.append((installation_id, org, name))
        return repos

    def _fetch_check_runs(self, token: str, org: str, repo: str, head_sha: str) -> tuple[CheckRun, ...]:
        data = self._request(
            "GET",
            f"/repos/{org}/{repo}/commits/{head_sha}/check-runs?per_page=100",
            token=token,
        )
        return tuple(
            CheckRun(name=item["name"], status=item["status"], conclusion=item.get("conclusion"))
            for item in data.get("check_runs", [])
        )

    def _fetch_review_decision(
        self,
        token: str,
        org: str,
        repo: str,
        pr_number: int,
        review_decision: str | None,
    ) -> str | None:
        if review_decision:
            return review_decision

        reviews = self._request(
            "GET",
            f"/repos/{org}/{repo}/pulls/{pr_number}/reviews?per_page=100",
            token=token,
        )

        latest_decision: str | None = None
        for review in reviews:
            state = str(review.get("state", "")).upper()
            if state == "APPROVED":
                latest_decision = "APPROVED"
            elif state == "CHANGES_REQUESTED":
                latest_decision = "CHANGES_REQUESTED"
        return latest_decision

    @staticmethod
    def _parse_updated_at(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    def _snapshot_from_payload(
        self,
        payload: dict[str, Any],
        *,
        org: str,
        repo: str,
        check_runs: tuple[CheckRun, ...] = (),
        review_decision: str | None = None,
    ) -> PullRequestSnapshot:
        return PullRequestSnapshot(
            org=org,
            repo=repo,
            number=int(payload["number"]),
            title=str(payload["title"]),
            url=str(payload["html_url"]),
            author=str(payload["user"]["login"]),
            state=self._pr_state(payload.get("merged_at"), str(payload["state"])),
            head_sha=str(payload.get("head", {}).get("sha", "")),
            review_decision=review_decision if review_decision is not None else payload.get("review_decision"),
            check_runs=check_runs,
            requested_reviewers=tuple(r["login"] for r in payload.get("requested_reviewers", [])),
            labels=tuple(label["name"] for label in payload.get("labels", [])),
            target_branch=str(payload.get("base", {}).get("ref", "main")),
            updated_at=self._parse_updated_at(payload.get("updated_at")),
        )

    def list_pull_requests(
        self,
        route: RouteConfig,
        *,
        include_enrichment: bool = True,
        updated_after: datetime | None = None,
    ) -> list[PullRequestSnapshot]:
        snapshots: list[PullRequestSnapshot] = []
        for installation_id, org, repo in self._iter_matching_repositories(route):
            token = self._installation_token(installation_id)
            page = 1
            stop_paging = False
            while not stop_paging:
                prs = self._request(
                    "GET",
                    f"/repos/{org}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=50&page={page}",
                    token=token,
                )
                if not prs:
                    break
                for item in prs:
                    item_updated_at = self._parse_updated_at(item.get("updated_at"))
                    if (
                        updated_after is not None
                        and item_updated_at is not None
                        and item_updated_at < updated_after
                    ):
                        stop_paging = True
                        break

                    if not include_enrichment:
                        snapshots.append(self._snapshot_from_payload(item, org=org, repo=repo))
                        continue

                    details = self._request(
                        "GET",
                        f"/repos/{org}/{repo}/pulls/{item['number']}",
                        token=token,
                    )
                    review_decision = self._fetch_review_decision(
                        token=token,
                        org=org,
                        repo=repo,
                        pr_number=int(details["number"]),
                        review_decision=details.get("review_decision"),
                    )
                    check_runs = self._fetch_check_runs(token, org, repo, details["head"]["sha"])
                    snapshots.append(
                        self._snapshot_from_payload(
                            details,
                            org=org,
                            repo=repo,
                            check_runs=check_runs,
                            review_decision=review_decision,
                        )
                    )
                if len(prs) < 50:
                    break
                page += 1
        return snapshots

    def get_bot_state_comment(self, pr: PullRequestSnapshot, *, force_refresh: bool = False) -> str | None:
        key = (pr.org, pr.repo, pr.number)
        if not force_refresh and key in self._comment_body_cache:
            return self._comment_body_cache[key]
        token = self._token_for_repo(pr.org, pr.repo)
        comments = self._request(
            "GET",
            f"/repos/{pr.org}/{pr.repo}/issues/{pr.number}/comments?per_page=100",
            token=token,
        )
        for comment in comments:
            if parse_state_marker(comment.get("body")) is not None:
                self._comment_id_cache[key] = int(comment["id"])
                body = comment.get("body")
                self._comment_body_cache[key] = body
                return body
        self._comment_body_cache[key] = None
        return None

    def _token_for_repo(self, org: str, repo: str) -> str:
        installation_id = self._repo_installation_cache.get((org, repo))
        if installation_id is not None:
            return self._installation_token(installation_id)
        for installation_id, match_org, match_repo in self._iter_matching_repositories(
            RouteConfig(name="lookup", org_pattern=org, repo_pattern=repo, channel="")
        ):
            if match_org == org and match_repo == repo:
                return self._installation_token(installation_id)
        raise GitHubApiError(f"No installation token can access repo {org}/{repo}")

    def upsert_bot_state_comment(self, pr: PullRequestSnapshot, body: str) -> None:
        key = (pr.org, pr.repo, pr.number)
        token = self._token_for_repo(pr.org, pr.repo)
        comment_id = self._comment_id_cache.get(key)

        if comment_id is None:
            comments = self._request(
                "GET",
                f"/repos/{pr.org}/{pr.repo}/issues/{pr.number}/comments?per_page=100",
                token=token,
            )
            for comment in comments:
                if parse_state_marker(comment.get("body")) is not None:
                    comment_id = int(comment["id"])
                    self._comment_id_cache[key] = comment_id
                    break

        if comment_id is not None:
            self._request("PATCH", f"/repos/{pr.org}/{pr.repo}/issues/comments/{comment_id}", token=token, json_body={"body": body})
            self._comment_body_cache[key] = body
            return

        created = self._request(
            "POST",
            f"/repos/{pr.org}/{pr.repo}/issues/{pr.number}/comments",
            token=token,
            json_body={"body": body},
        )
        self._comment_id_cache[key] = int(created["id"])
        self._comment_body_cache[key] = body


class SlackApiAdapter:
    def __init__(
        self,
        bot_token: str,
        api_base_url: str = "https://slack.com/api",
        timeout_seconds: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.api_base_url = api_base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._tracer = trace.get_tracer(__name__)

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        with self._tracer.start_as_current_span("slack_api_request") as span:
            span.set_attribute("rpc.method", method)
            response = self._client.post(
                f"{self.api_base_url}/{method}",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            )
            span.set_attribute("http.status_code", response.status_code)
        observe_api_request("slack", method, response.status_code, time.monotonic() - started)
        if response.status_code >= 400:
            raise SlackApiError(f"Slack API {method} failed: {response.status_code} {response.text}")
        data = response.json()
        if not data.get("ok", False):
            raise SlackApiError(f"Slack API {method} returned ok=false: {data.get('error', 'unknown_error')}")
        return data

    def post_message(self, channel: str, text: str) -> str:
        data = self._post(
            "chat.postMessage",
            {
                "channel": channel,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            },
        )
        return data["ts"]

    def update_message(self, channel: str, ts: str, text: str) -> None:
        self._post("chat.update", {"channel": channel, "ts": ts, "text": text})


def normalize_private_key(value: str) -> str:
    # Kubernetes env injection keeps multiline values intact; this handles escaped newlines too.
    return value.replace("\\n", "\n") if "\\n" in value else value
