import os

from pr_slack_notifier.config import load_settings_from_env


def test_load_settings_from_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("GITHUB_INSTALLATION_IDS", "100, 101")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-token")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "15")
    monkeypatch.setenv("DEEP_RECONCILE_INTERVAL_SECONDS", "20")
    monkeypatch.setenv("SWEEP_RECONCILE_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("RATE_LIMIT_BACKOFF_SECONDS", "45")
    monkeypatch.setenv("RATE_LIMIT_BACKOFF_MAX_SECONDS", "500")
    monkeypatch.setenv("ERROR_RETRY_SECONDS", "12")
    monkeypatch.setenv("DISABLE_HISTORICAL_CLOSED_PRS", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("JSON_LOGS", "true")
    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.setenv("METRICS_PORT", "9191")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-notifier")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://alloy.monitoring.svc:4318/v1/traces")
    monkeypatch.setenv(
        "ROUTES_JSON",
        '[{"name":"acme-main","org_pattern":"acme","repo_pattern":"*","channel":"C1"}]',
    )

    settings = load_settings_from_env()
    assert settings.github_app_id == "12345"
    assert settings.github_app_private_key == "pem"
    assert settings.github_installation_ids == (100, 101)
    assert settings.slack_bot_token == "xoxb-token"
    assert settings.polling_interval_seconds == 15
    assert settings.deep_reconcile_interval_seconds == 20
    assert settings.sweep_reconcile_interval_seconds == 300
    assert settings.rate_limit_backoff_seconds == 45
    assert settings.rate_limit_backoff_max_seconds == 500
    assert settings.error_retry_seconds == 12
    assert settings.disable_historical_closed_prs is True
    assert settings.dry_run is True
    assert settings.log_level == "DEBUG"
    assert settings.json_logs is True
    assert settings.metrics_enabled is True
    assert settings.metrics_port == 9191
    assert settings.otel_service_name == "my-notifier"
    assert settings.otel_otlp_endpoint == "http://alloy.monitoring.svc:4318/v1/traces"
    assert settings.routes[0].name == "acme-main"
    assert settings.routes[0].channel == "C1"


def test_defaults_when_missing_env(monkeypatch) -> None:
    for key in [
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_INSTALLATION_IDS",
        "SLACK_BOT_TOKEN",
        "POLL_INTERVAL_SECONDS",
        "DEEP_RECONCILE_INTERVAL_SECONDS",
        "SWEEP_RECONCILE_INTERVAL_SECONDS",
        "RATE_LIMIT_BACKOFF_SECONDS",
        "RATE_LIMIT_BACKOFF_MAX_SECONDS",
        "ERROR_RETRY_SECONDS",
        "DISABLE_HISTORICAL_CLOSED_PRS",
        "DRY_RUN",
        "LOG_LEVEL",
        "JSON_LOGS",
        "METRICS_ENABLED",
        "METRICS_PORT",
        "OTEL_SERVICE_NAME",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "ROUTES_JSON",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = load_settings_from_env()
    assert settings.github_app_id == ""
    assert settings.github_app_private_key == ""
    assert settings.github_installation_ids == ()
    assert settings.slack_bot_token == ""
    assert settings.polling_interval_seconds == 30
    assert settings.deep_reconcile_interval_seconds == 30
    assert settings.sweep_reconcile_interval_seconds == 600
    assert settings.rate_limit_backoff_seconds == 60
    assert settings.rate_limit_backoff_max_seconds == 900
    assert settings.error_retry_seconds == 10
    assert settings.disable_historical_closed_prs is True
    assert settings.dry_run is False
    assert settings.log_level == "INFO"
    assert settings.json_logs is True
    assert settings.metrics_enabled is True
    assert settings.metrics_port == 9000
    assert settings.otel_service_name == "github-pr-slack-notifier"
    assert settings.otel_otlp_endpoint == ""
    assert settings.routes == ()


# ensure standard library import is used in test env
def test_os_import_present() -> None:
    assert os.getenv is not None
