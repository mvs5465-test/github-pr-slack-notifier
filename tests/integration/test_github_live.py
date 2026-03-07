import os

import pytest

from pr_slack_notifier.adapters import GitHubAppAdapter, normalize_private_key
from pr_slack_notifier.models import RouteConfig
from pr_slack_notifier.reconcile import derive_status

_REQUIRED_ENV = {
    "INTEGRATION_GITHUB_APP_ID",
    "INTEGRATION_GITHUB_APP_PRIVATE_KEY",
    "INTEGRATION_GITHUB_INSTALLATION_IDS",
    "INTEGRATION_GITHUB_ORG",
    "INTEGRATION_GITHUB_REPO",
    "INTEGRATION_PULL_NUMBER",
}


pytestmark = pytest.mark.integration



def _missing_required_env() -> list[str]:
    return sorted(key for key in _REQUIRED_ENV if not os.getenv(key))


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
