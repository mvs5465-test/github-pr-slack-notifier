# github-pr-slack-notifier

Reliable reconciliation-based notifier that keeps one Slack message per GitHub PR in sync.

## Current status

This is a production-ready scaffold with tested core logic:
- deterministic reconciliation planner
- hidden GitHub comment state marker (`<!-- pr-slack-notifier:{...} -->`)
- dry-run/evaluation mode
- multi-route channel mapping (org/repo pattern to Slack channel)
- plugin contract (`on_plan`) and sample behavior in tests
- GitHub App + Slack API adapters
- Docker + GitHub Actions + Helm chart

## Design summary

- Control loop: poll GitHub on a fixed interval, reconcile each PR, and apply minimal actions.
- Optional webhook path: hook handlers can enqueue immediate reconcile keys; polling remains source of truth.
- Persisted state: message ref (`channel`, `ts`) + content fingerprint in a bot comment marker.
- Idempotency: no-op when fingerprint is unchanged.
- Dry-run: convert all actions to logs.
- Extensibility: plugins can attach extra actions from a stable context object.

## Message format

Example:

`_🟢 opened_ | *repo* | <https://github.com/org/repo/pull/123|Add feature x> by @author | _✅ approved_ | _✅ passed_`

## Configuration

Environment variables:
- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_INSTALLATION_IDS` (comma-separated)
- `SLACK_BOT_TOKEN`
- `POLL_INTERVAL_SECONDS`
- `DRY_RUN`
- `ROUTES_JSON` example:
  - `[ {"name":"acme-main","org_pattern":"acme","repo_pattern":"*","channel":"C123"} ]`
- `LOG_LEVEL` (default `INFO`)
- `JSON_LOGS` (default `true`)
- `METRICS_ENABLED` (default `true`)
- `METRICS_PORT` (default `9000`, serves Prometheus `/metrics`)
- `OTEL_SERVICE_NAME` (default `github-pr-slack-notifier`)
- `OTEL_EXPORTER_OTLP_ENDPOINT` (optional, e.g. `http://alloy.monitoring.svc:4318/v1/traces`)

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
ruff check .
pytest
```

## Live integration harness

An opt-in GitHub API contract test is available at:
- `tests/integration/test_github_live.py`

Run locally (no coverage gate for this targeted check):

```bash
INTEGRATION_GITHUB_APP_ID=... \
INTEGRATION_GITHUB_APP_PRIVATE_KEY=\"$(cat path/to/private-key.pem)\" \
INTEGRATION_GITHUB_INSTALLATION_IDS=... \
INTEGRATION_GITHUB_ORG=mvs5465-test \
INTEGRATION_GITHUB_REPO=github-pr-slack-notifier \
INTEGRATION_PULL_NUMBER=3 \
INTEGRATION_EXPECTED_APPROVAL=changes_requested \
pytest tests/integration/test_github_live.py -m integration --no-cov -q
```

Optional Slack contract check (post + update) in your test channel:

```bash
INTEGRATION_SLACK_BOT_TOKEN=\"$(cat ~/.secrets/github-pr-slack-notifier/slack_bot_token)\" \
INTEGRATION_SLACK_CHANNEL=C0AJRCM5J8P \
pytest tests/integration/test_github_live.py -m integration --no-cov -q
```

Stateful reconcile contract check (posts/updates + hidden comment marker persistence):

```bash
INTEGRATION_GITHUB_APP_ID=... \
INTEGRATION_GITHUB_APP_PRIVATE_KEY=\"$(cat path/to/private-key.pem)\" \
INTEGRATION_GITHUB_INSTALLATION_IDS=... \
INTEGRATION_GITHUB_ORG=mvs5465-test \
INTEGRATION_GITHUB_REPO=github-pr-slack-notifier \
INTEGRATION_PULL_NUMBER=3 \
INTEGRATION_SLACK_BOT_TOKEN=\"$(cat ~/.secrets/github-pr-slack-notifier/slack_bot_token)\" \
INTEGRATION_SLACK_CHANNEL=C0AJRCM5J8P \
INTEGRATION_ENABLE_STATEFUL_RECONCILE_TEST=true \
pytest tests/integration/test_github_live.py -m integration --no-cov -q
```

This stateful test mutates live resources (posts/updates Slack + updates bot comment on PR).

End-to-end polling SLA probe (uses deployed poller, optionally triggers review-bot workflow, then times Slack convergence):

```bash
INTEGRATION_GITHUB_APP_ID=... \
INTEGRATION_GITHUB_APP_PRIVATE_KEY=\"$(cat path/to/private-key.pem)\" \
INTEGRATION_GITHUB_INSTALLATION_IDS=... \
INTEGRATION_GITHUB_ORG=mvs5465-test \
INTEGRATION_GITHUB_REPO=github-pr-slack-notifier \
INTEGRATION_PULL_NUMBER=3 \
INTEGRATION_SLACK_BOT_TOKEN=\"$(cat ~/.secrets/github-pr-slack-notifier/slack_bot_token)\" \
INTEGRATION_ENABLE_E2E_POLLING_TEST=true \
INTEGRATION_GITHUB_WORKFLOW_TOKEN=ghp_... \
INTEGRATION_E2E_REVIEW_ACTION=REQUEST_CHANGES \
INTEGRATION_E2E_TIMEOUT_SECONDS=45 \
pytest tests/integration/test_github_live.py -m e2e_polling --no-cov -q
```

Notes:
- This test dispatches `.github/workflows/pr-review-bot.yml` by default.
- Slack history read is required (`channels:history` and/or `groups:history`) to poll message text.
- The PR must already have a notifier state marker comment so Slack message `ts` is known.

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

## Helm secret wiring

- Non-secret settings should be passed via chart values (`env.*`):
  - `GITHUB_APP_ID`
  - `GITHUB_INSTALLATION_IDS`
  - `ROUTES_JSON`
  - `POLL_INTERVAL_SECONDS`
  - `DRY_RUN`
- Secret settings are loaded from a Kubernetes Secret referenced by `secretEnv.name`:
  - key `GITHUB_APP_PRIVATE_KEY`
  - key `SLACK_BOT_TOKEN`

## Reliability/testing goals

- unit-test pure reconciliation logic first
- add adapter contract tests with recorded HTTP fixtures
- add retry/backoff, rate-limit handling, and dead-letter logging for API failures
- keep coverage gate high (`90%` now; can raise once adapters are implemented)
