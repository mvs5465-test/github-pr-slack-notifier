# AGENTS.md

Instructions for human + AI contributors in this repository.

## Product

- `github-pr-slack-notifier` is a reconciliation-based service that keeps one Slack message per GitHub pull request in sync.
- The repo contains the Python application, tests, container assets, and Helm chart used for deployment.

## Architecture

- `src/pr_slack_notifier/engine.py` and `reconcile.py` drive the core control loop.
- `adapters.py` owns GitHub App and Slack API interactions.
- `routing.py`, `state.py`, and `status.py` shape destination routing, persisted message state, and rendered PR status.
- `observability.py` owns metrics and tracing support.
- `chart/` and `Dockerfile` cover cluster deployment.

## Working Rules

- Preserve deterministic reconcile behavior and idempotent message updates.
- Treat live integration tests as opt-in and explicit; they mutate real GitHub and Slack resources.
- Keep config, routing, and adapter changes covered by tests when possible.
- If you touch chart files, keep deployment assumptions aligned with the current runtime behavior.

## Verification

- Run `pytest` for normal repo changes.
- Run targeted integration tests only when the required live credentials are intentionally available.
