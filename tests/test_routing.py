from pr_slack_notifier.models import RouteConfig
from pr_slack_notifier.routing import resolve_route


def test_resolve_route_uses_first_match() -> None:
    routes = [
        RouteConfig(name="org", org_pattern="acme", repo_pattern="*", channel="C1"),
        RouteConfig(name="specific", org_pattern="acme", repo_pattern="api", channel="C2"),
    ]
    route = resolve_route("acme", "api", routes)
    assert route is not None
    assert route.channel == "C1"


def test_resolve_route_no_match() -> None:
    routes = [RouteConfig(org_pattern="acme", repo_pattern="*", channel="C1")]
    assert resolve_route("other", "api", routes) is None
