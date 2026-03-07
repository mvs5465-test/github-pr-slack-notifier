from __future__ import annotations

from fnmatch import fnmatch

from .models import RouteConfig


def resolve_route(org: str, repo: str, routes: list[RouteConfig]) -> RouteConfig | None:
    for route in routes:
        if fnmatch(org, route.org_pattern) and fnmatch(repo, route.repo_pattern):
            return route
    return None
