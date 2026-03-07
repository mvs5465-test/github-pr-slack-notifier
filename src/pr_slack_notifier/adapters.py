from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from datetime import datetime
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
        self._repo_installation_cache: dict[tuple[str, str], int] = {}
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
        if response.status_code >= 400:
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

    def _iter_matching_repositories(self, route: RouteConfig) -> list[tuple[int, str, str]]:
        repos: list[tuple[int, str, str]] = []
        for installation_id in self.installation_ids:
            token = self._installation_token(installation_id)
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
                    self._repo_installation_cache[(org, name)] = installation_id
                    if fnmatch.fnmatch(org, route.org_pattern) and fnmatch.fnmatch(name, route.repo_pattern):
                        repos.append((installation_id, org, name))
                if len(page_items) < 100:
                    break
                page += 1
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

    def list_pull_requests(self, route: RouteConfig) -> list[PullRequestSnapshot]:
        snapshots: list[PullRequestSnapshot] = []
        for installation_id, org, repo in self._iter_matching_repositories(route):
            token = self._installation_token(installation_id)
            prs = self._request(
                "GET",
                f"/repos/{org}/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=50",
                token=token,
            )
            for item in prs:
                details = self._request(
                    "GET",
                    f"/repos/{org}/{repo}/pulls/{item['number']}",
                    token=token,
                )
                review_decision = self._fetch_review_decision(
                    token=token,
                    org=org,
                    repo=repo,
                    pr_number=details["number"],
                    review_decision=details.get("review_decision"),
                )
                check_runs = self._fetch_check_runs(token, org, repo, details["head"]["sha"])
                snapshots.append(
                    PullRequestSnapshot(
                        org=org,
                        repo=repo,
                        number=details["number"],
                        title=details["title"],
                        url=details["html_url"],
                        author=details["user"]["login"],
                        state=self._pr_state(details.get("merged_at"), details["state"]),
                        review_decision=review_decision,
                        check_runs=check_runs,
                        requested_reviewers=tuple(r["login"] for r in details.get("requested_reviewers", [])),
                        labels=tuple(label["name"] for label in details.get("labels", [])),
                        target_branch=details["base"]["ref"],
                    )
                )
        return snapshots

    def get_bot_state_comment(self, pr: PullRequestSnapshot) -> str | None:
        key = (pr.org, pr.repo, pr.number)
        token = self._token_for_repo(pr.org, pr.repo)
        comments = self._request(
            "GET",
            f"/repos/{pr.org}/{pr.repo}/issues/{pr.number}/comments?per_page=100",
            token=token,
        )
        for comment in comments:
            if parse_state_marker(comment.get("body")) is not None:
                self._comment_id_cache[key] = int(comment["id"])
                return comment.get("body")
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
            return

        created = self._request(
            "POST",
            f"/repos/{pr.org}/{pr.repo}/issues/{pr.number}/comments",
            token=token,
            json_body={"body": body},
        )
        self._comment_id_cache[key] = int(created["id"])


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
