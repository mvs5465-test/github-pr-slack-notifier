# github-pr-slack-notifier

Reliable reconciliation-based notifier that keeps one Slack message per GitHub PR in sync.

## Current status

This is a production-ready scaffold with tested core logic:
- deterministic reconciliation planner
- hidden GitHub comment state marker (`<!-- pr-slack-notifier:{...} -->`)
- dry-run/evaluation mode
- multi-route channel mapping (org/repo pattern to Slack channel)
- plugin contract (`on_plan`) and sample behavior in tests
- Docker + GitHub Actions + Helm chart

GitHub and Slack API adapters are intentionally left as explicit integration boundaries for the next slice.

## Design summary

- Control loop: poll GitHub on a fixed interval, reconcile each PR, and apply minimal actions.
- Optional webhook path: hook handlers can enqueue immediate reconcile keys; polling remains source of truth.
- Persisted state: message ref (`channel`, `ts`) + content fingerprint in a bot comment marker.
- Idempotency: no-op when fingerprint is unchanged.
- Dry-run: convert all actions to logs.
- Extensibility: plugins can attach extra actions from a stable context object.

## Message format

Example:

`[repo] <https://github.com/org/repo/pull/123|PR #123> by @author | state: opened | approval: approved | checks: passed`

## Configuration

Environment variables:
- `GITHUB_APP_ID`
- `GITHUB_INSTALLATION_IDS` (comma-separated)
- `SLACK_BOT_TOKEN`
- `POLL_INTERVAL_SECONDS`
- `DRY_RUN`
- `ROUTES_JSON` example:
  - `[ {"name":"acme-main","org_pattern":"acme","repo_pattern":"*","channel":"C123"} ]`

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
ruff check .
pytest
```

## GitHub App setup (first pass)

1. Create a GitHub App in your org (or personal account first).
2. Grant minimum permissions:
   - Repository permissions:
     - Pull requests: Read
     - Checks: Read
     - Commit statuses: Read
     - Issues: Read/Write (to manage bot comment marker)
     - Metadata: Read
3. Subscribe to events (for later webhook acceleration):
   - `pull_request`, `pull_request_review`, `check_suite`, `check_run`
4. Install app to target org/repos.
5. Store app ID, installation ID(s), and private key in k8s secret.

## Slack App setup (first pass)

1. Create a Slack app in your workspace.
2. Add bot token scopes:
   - `chat:write`
   - `chat:write.public` (if posting to channels bot has not joined)
   - `channels:read` (optional if channel lookup needed)
   - `groups:read` (optional for private channels)
3. Install app to workspace and invite bot to target channels.
4. Store bot token in k8s secret.

## Reliability/testing goals

- unit-test pure reconciliation logic first
- add adapter contract tests with recorded HTTP fixtures
- add retry/backoff, rate-limit handling, and dead-letter logging for API failures
- keep coverage gate high (`90%` now; can raise once adapters are implemented)
