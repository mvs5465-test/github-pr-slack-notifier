from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote_plus
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
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(GitHubApiError):
    def __init__(
        self,
        message: str,
        reset_at_epoch: float | None = None,
        resource: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reset_at_epoch = reset_at_epoch
        self.resource = resource

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
        resource = response.headers.get("x-ratelimit-resource")

        # Proactive handling: if a successful response reports no remaining quota,
        # short-circuit now and let the control loop wait until reset.
        if response.status_code < 400 and remaining is not None and remaining <= 0:
            raise GitHubRateLimitError(
                f"GitHub API {method} {path} exhausted rate limit",
                reset_at_epoch=reset_at_epoch,
                resource=resource,
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
                        resource=resource,
                    )
            raise GitHubApiError(
                f"GitHub API {method} {path} failed: {response.status_code} {response.text}",
                status_code=response.status_code,
            )
        if response.text:
            return response.json()
        return None

    @staticmethod
    def _parse_rate_limit_headers(headers: httpx.Headers) -> tuple[int | None, int | None, float | None, str | None]:
        remaining: int | None = None
        limit: int | None = None
        reset_at_epoch: float | None = None

        remaining_header = headers.get("x-ratelimit-remaining")
        if remaining_header is not None:
            try:
                remaining = int(float(remaining_header))
            except ValueError:
                remaining = None

        limit_header = headers.get("x-ratelimit-limit")
        if limit_header is not None:
            try:
                limit = int(float(limit_header))
            except ValueError:
                limit = None

        reset_header = headers.get("x-ratelimit-reset")
        if reset_header:
            try:
                reset_at_epoch = float(reset_header)
            except ValueError:
                reset_at_epoch = None

        resource = headers.get("x-ratelimit-resource")
        return remaining, limit, reset_at_epoch, resource

    def _graphql_backpressure_sleep(self, headers: httpx.Headers) -> None:
        remaining, limit, reset_at_epoch, resource = self._parse_rate_limit_headers(headers)
        if remaining is None or limit is None or reset_at_epoch is None:
            return

        now_epoch = time.time()
        seconds_to_reset = max(reset_at_epoch - now_epoch, 1.0)
        if remaining <= 1:
            raise GitHubRateLimitError(
                "GraphQL rate budget nearly exhausted",
                reset_at_epoch=reset_at_epoch,
                resource=resource or "graphql",
            )

        ratio = remaining / max(limit, 1)
        base_spacing_seconds = seconds_to_reset / max(remaining, 1)
        multiplier = 1.0 + max(0.0, (0.5 - ratio) * 4.0)
        sleep_seconds = min(base_spacing_seconds * multiplier, 30.0)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    def _graphql_request(self, token: str, query: str, variables: dict[str, Any]) -> Any:
        started = time.monotonic()
        with self._tracer.start_as_current_span("github_graphql_request") as span:
            response = self._client.post(
                f"{self.api_base_url}/graphql",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "github-pr-slack-notifier",
                },
                json={"query": query, "variables": variables},
            )
            span.set_attribute("http.status_code", response.status_code)
        observe_api_request("github", "/graphql", response.status_code, time.monotonic() - started)

        remaining, _limit, reset_at_epoch, resource = self._parse_rate_limit_headers(response.headers)
        if response.status_code == 403 and remaining is not None and remaining <= 0:
            raise GitHubRateLimitError(
                f"GitHub GraphQL request failed: {response.status_code} {response.text}",
                reset_at_epoch=reset_at_epoch,
                resource=resource or "graphql",
            )
        if response.status_code >= 400:
            raise GitHubApiError(
                f"GitHub GraphQL request failed: {response.status_code} {response.text}",
                status_code=response.status_code,
            )

        payload = response.json() if response.text else {}
        errors = payload.get("errors", [])
        if errors:
            joined = " | ".join(str(err.get("message", "")).lower() for err in errors)
            if "rate limit" in joined:
                raise GitHubRateLimitError(
                    f"GitHub GraphQL rate-limited: {errors}",
                    reset_at_epoch=reset_at_epoch,
                    resource=resource or "graphql",
                )
            raise GitHubApiError(f"GitHub GraphQL returned errors: {errors}")

        self._graphql_backpressure_sleep(response.headers)
        return payload.get("data", {})

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
    def _pr_state(merged_at: str | None, state: str, *, is_draft: bool = False) -> PullRequestState:
        if merged_at:
            return PullRequestState.MERGED
        if state.lower() == "closed":
            return PullRequestState.CLOSED
        if is_draft:
            return PullRequestState.DRAFT
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

    @staticmethod
    def _format_github_search_timestamp(value: datetime) -> str:
        utc_value = value.astimezone(timezone.utc).replace(microsecond=0)
        return utc_value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _org_repo_from_repository_url(repository_url: str) -> tuple[str, str]:
        parts = repository_url.rstrip("/").split("/")
        if len(parts) < 2:
            raise GitHubApiError(f"Invalid repository_url in search payload: {repository_url}")
        return parts[-2], parts[-1]

    def _list_pull_requests_org_wide(
        self,
        route: RouteConfig,
        *,
        include_enrichment: bool,
        updated_after: datetime | None,
    ) -> list[PullRequestSnapshot]:
        if any(ch in route.org_pattern for ch in "*?[]"):
            raise GitHubApiError("Org-wide search requires an explicit org_pattern")
        if route.repo_pattern != "*":
            raise GitHubApiError("Org-wide search requires repo_pattern='*'")
        installation_id = self.installation_ids[0]
        token = self._installation_token(installation_id)
        qualifiers = [f"org:{route.org_pattern}", "is:pr"]
        if not include_enrichment:
            qualifiers.append("is:open")
        if updated_after is not None:
            qualifiers.append(f"updated:>={self._format_github_search_timestamp(updated_after)}")
        query = " ".join(qualifiers)
        encoded_query = quote_plus(query)

        snapshots: list[PullRequestSnapshot] = []
        page = 1
        while page <= 10:
            payload = self._request(
                "GET",
                f"/search/issues?q={encoded_query}&sort=updated&order=desc&per_page=100&page={page}",
                token=token,
            )
            items = payload.get("items", [])
            if not items:
                break

            for item in items:
                org, repo = self._org_repo_from_repository_url(str(item.get("repository_url", "")))
                self._repo_installation_cache[(org, repo)] = installation_id
                if not include_enrichment:
                    state_value = str(item.get("state", "open"))
                    snapshot_payload = {
                        "number": int(item["number"]),
                        "title": str(item.get("title", "")),
                        "html_url": str(item.get("html_url", "")),
                        "state": state_value,
                        "draft": bool(item.get("draft", False)),
                        "merged_at": None,
                        "user": {"login": str(item.get("user", {}).get("login", ""))},
                        "head": {"sha": ""},
                        "updated_at": item.get("updated_at"),
                        "base": {"ref": "main"},
                        "requested_reviewers": [],
                        "labels": item.get("labels", []),
                    }
                    snapshots.append(self._snapshot_from_payload(snapshot_payload, org=org, repo=repo))
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

            if len(items) < 100:
                break
            page += 1
        return snapshots

    def _checks_from_status_rollup(self, rollup: dict[str, Any] | None) -> tuple[CheckRun, ...]:
        if not rollup:
            return ()
        contexts = (rollup.get("contexts") or {}).get("nodes") or []
        checks: list[CheckRun] = []
        for item in contexts:
            typename = item.get("__typename")
            if typename == "CheckRun":
                checks.append(
                    CheckRun(
                        name=str(item.get("name", "")),
                        status=str(item.get("status", "")).lower(),
                        conclusion=(str(item.get("conclusion")).lower() if item.get("conclusion") else None),
                    )
                )
                continue
            if typename == "StatusContext":
                state = str(item.get("state", "")).upper()
                status = "pending"
                conclusion: str | None = None
                if state == "SUCCESS":
                    status = "completed"
                    conclusion = "success"
                elif state in {"ERROR", "FAILURE"}:
                    status = "completed"
                    conclusion = "failure"
                checks.append(
                    CheckRun(
                        name=str(item.get("context", "")),
                        status=status,
                        conclusion=conclusion,
                    )
                )
        return tuple(checks)

    def _snapshot_from_graphql_pr(self, *, org: str, repo: str, node: dict[str, Any]) -> PullRequestSnapshot:
        pr_number = int(node["number"])
        merged_at = node.get("mergedAt")
        state = self._pr_state(
            merged_at,
            str(node.get("state", "OPEN")),
            is_draft=bool(node.get("isDraft", False)),
        )
        review_requests = (node.get("reviewRequests") or {}).get("nodes") or []
        requested_reviewers: list[str] = []
        for req in review_requests:
            reviewer = req.get("requestedReviewer") or {}
            if reviewer.get("__typename") == "User" and reviewer.get("login"):
                requested_reviewers.append(str(reviewer["login"]))

        labels_nodes = (node.get("labels") or {}).get("nodes") or []
        labels = tuple(str(label.get("name", "")) for label in labels_nodes if label.get("name"))

        comments_nodes = (node.get("comments") or {}).get("nodes") or []
        cache_key = (org, repo, pr_number)
        marker_body: str | None = None
        marker_id: int | None = None
        for comment in comments_nodes:
            body = comment.get("body")
            if parse_state_marker(body) is not None:
                marker_body = body
                dbid = comment.get("databaseId")
                if dbid is not None:
                    marker_id = int(dbid)
                break
        self._comment_body_cache[cache_key] = marker_body
        if marker_id is not None:
            self._comment_id_cache[cache_key] = marker_id

        commit_nodes = (node.get("commits") or {}).get("nodes") or []
        rollup = None
        if commit_nodes:
            rollup = ((commit_nodes[0].get("commit") or {}).get("statusCheckRollup"))

        return PullRequestSnapshot(
            org=org,
            repo=repo,
            number=pr_number,
            title=str(node.get("title", "")),
            url=str(node.get("url", "")),
            author=str((node.get("author") or {}).get("login", "")),
            state=state,
            head_sha=str(node.get("headRefOid", "")),
            review_decision=node.get("reviewDecision"),
            check_runs=self._checks_from_status_rollup(rollup),
            requested_reviewers=tuple(requested_reviewers),
            labels=labels,
            target_branch=str(node.get("baseRefName", "main")),
            updated_at=self._parse_updated_at(node.get("updatedAt")),
        )

    def _graphql_list_recent_pull_requests(self, token: str, org: str, limit: int = 100) -> list[PullRequestSnapshot]:
        query = """
        query($query: String!, $first: Int!) {
          search(
            type: ISSUE,
            query: $query,
            first: $first
          ) {
            nodes {
              ... on PullRequest {
                number
                title
                url
                state
                isDraft
                mergedAt
                updatedAt
                reviewDecision
                baseRefName
                headRefOid
                author { ... on User { login } }
                repository {
                  name
                  owner { login }
                }
                labels(first: 20) { nodes { name } }
                reviewRequests(first: 20) {
                  nodes {
                    requestedReviewer { __typename ... on User { login } }
                  }
                }
                comments(last: 100) {
                  nodes {
                    databaseId
                    body
                  }
                }
                commits(last: 1) {
                  nodes {
                    commit {
                      statusCheckRollup {
                        contexts(first: 50) {
                          nodes {
                            __typename
                            ... on CheckRun {
                              name
                              status
                              conclusion
                            }
                            ... on StatusContext {
                              context
                              state
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql_request(
            token=token,
            query=query,
            variables={
                "query": f"org:{org} is:pr sort:updated-desc",
                "first": limit,
            },
        )
        search_conn = data.get("search", {})
        nodes = search_conn.get("nodes", [])
        snapshots: list[PullRequestSnapshot] = []
        for node in nodes:
            repo_node = node.get("repository") or {}
            repo_name = str(repo_node.get("name", "")).strip()
            owner = str((repo_node.get("owner") or {}).get("login", "")).strip()
            if not repo_name or not owner:
                continue
            self._repo_installation_cache[(owner, repo_name)] = self.installation_ids[0]
            snapshots.append(self._snapshot_from_graphql_pr(org=owner, repo=repo_name, node=node))
        return snapshots

    def list_pull_requests_for_sweep(self, route: RouteConfig) -> list[PullRequestSnapshot]:
        # Efficient GraphQL sweep currently applies only to exact-org/all-repos routes.
        # Any wildcard org or narrower repo pattern falls back to REST full enrichment.
        if any(ch in route.org_pattern for ch in "*?[]") or route.repo_pattern != "*":
            return self.list_pull_requests(route, include_enrichment=True, updated_after=None)
        # GraphQL org-wide optimization currently assumes a single shared installation scope.
        installation_id = self.installation_ids[0]
        token = self._installation_token(installation_id)
        org = route.org_pattern
        return self._graphql_list_recent_pull_requests(token=token, org=org, limit=100)

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
            state=self._pr_state(
                payload.get("merged_at"),
                str(payload["state"]),
                is_draft=bool(payload.get("draft", False)),
            ),
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
        if self.installation_ids and not any(ch in route.org_pattern for ch in "*?[]") and route.repo_pattern == "*":
            return self._list_pull_requests_org_wide(
                route,
                include_enrichment=include_enrichment,
                updated_after=updated_after,
            )

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

    def get_pull_request(
        self,
        route: RouteConfig,
        *,
        org: str,
        repo: str,
        number: int,
        include_enrichment: bool = True,
    ) -> PullRequestSnapshot | None:
        token = self._token_for_repo(org, repo)
        try:
            details = self._request(
                "GET",
                f"/repos/{org}/{repo}/pulls/{number}",
                token=token,
            )
        except GitHubApiError as exc:
            if exc.status_code == 404:
                return None
            raise

        if not include_enrichment:
            return self._snapshot_from_payload(details, org=org, repo=repo)

        review_decision = self._fetch_review_decision(
            token=token,
            org=org,
            repo=repo,
            pr_number=int(details["number"]),
            review_decision=details.get("review_decision"),
        )
        check_runs = self._fetch_check_runs(token, org, repo, details["head"]["sha"])
        return self._snapshot_from_payload(
            details,
            org=org,
            repo=repo,
            check_runs=check_runs,
            review_decision=review_decision,
        )

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
