import pytest

from pr_slack_notifier import app
from pr_slack_notifier.adapters import GitHubRateLimitError
from pr_slack_notifier.config import Settings
from pr_slack_notifier.models import RouteConfig


def _settings() -> Settings:
    return Settings(
        github_app_id="123",
        github_app_private_key="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
        github_installation_ids=(99,),
        slack_bot_token="xoxb-test",
        polling_interval_seconds=1,
        deep_reconcile_interval_seconds=1,
        sweep_reconcile_interval_seconds=60,
        rate_limit_backoff_seconds=30,
        rate_limit_backoff_max_seconds=120,
        error_retry_seconds=5,
        enable_historical_closed_prs=False,
        dry_run=True,
        routes=(RouteConfig(org_pattern="acme", repo_pattern="*", channel="C1"),),
        log_level="INFO",
        json_logs=True,
        metrics_enabled=False,
        metrics_port=9000,
        otel_service_name="github-pr-slack-notifier",
        otel_otlp_endpoint="",
    )


def test_validate_settings_missing_required(monkeypatch) -> None:
    monkeypatch.setattr(
        app,
        "load_settings_from_env",
        lambda: Settings(
            github_app_id="",
            github_app_private_key="",
            github_installation_ids=(),
            slack_bot_token="",
            polling_interval_seconds=30,
            deep_reconcile_interval_seconds=30,
            sweep_reconcile_interval_seconds=600,
            rate_limit_backoff_seconds=60,
            rate_limit_backoff_max_seconds=900,
            error_retry_seconds=10,
            enable_historical_closed_prs=False,
            dry_run=False,
            routes=(),
            log_level="INFO",
            json_logs=True,
            metrics_enabled=False,
            metrics_port=9000,
            otel_service_name="github-pr-slack-notifier",
            otel_otlp_endpoint="",
        ),
    )
    with pytest.raises(RuntimeError, match="missing required env vars"):
        app._validate_settings()


def test_run_forever_runs_single_iteration(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.refresh_calls = 0
            self.changed_calls = 0
            self.sweep_calls = 0

        def refresh_lightweight(self):
            self.refresh_calls += 1

        def reconcile_changed(self):
            self.changed_calls += 1

        def reconcile_all(self, *, force_refresh_state: bool):
            self.sweep_calls += 1
            assert force_refresh_state is True

    state = {"engine": None, "github": None, "slack": None}

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def fake_gh_ctor(**kwargs):
        state["github"] = kwargs
        return object()

    def fake_slack_ctor(**kwargs):
        state["slack"] = kwargs
        return object()

    def fake_sleep(_seconds):
        raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", _settings)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", fake_gh_ctor)
    monkeypatch.setattr(app, "SlackApiAdapter", fake_slack_ctor)
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].refresh_calls == 1
    assert state["engine"].changed_calls == 1
    assert state["engine"].sweep_calls == 1
    assert state["github"]["app_id"] == "123"
    assert "BEGIN PRIVATE KEY" in state["github"]["private_key_pem"]
    assert state["slack"]["bot_token"] == "xoxb-test"
    assert state["engine"].kwargs["enable_historical_closed_prs"] is False


def test_run_forever_retries_on_rate_limit_without_crashing(monkeypatch) -> None:
    class FakeEngine:
        def refresh_lightweight(self):
            raise GitHubRateLimitError("rate", reset_at_epoch=101)

        def reconcile_changed(self):
            raise AssertionError("should not be called")

        def reconcile_all(self, *, force_refresh_state: bool):
            raise AssertionError("should not be called")

    sleeps: list[float] = []
    state = {"calls": 0}

    def fake_engine_ctor(**_kwargs):
        return FakeEngine()

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            raise RuntimeError("stop")

    def fake_time():
        state["calls"] += 1
        return 100.0

    monkeypatch.setattr(app, "load_settings_from_env", _settings)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app.time, "time", fake_time)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert sleeps == [2]


def test_run_forever_retries_on_generic_error_without_crashing(monkeypatch) -> None:
    class FakeEngine:
        def refresh_lightweight(self):
            raise RuntimeError("boom")

        def reconcile_changed(self):
            raise AssertionError("should not be called")

        def reconcile_all(self, *, force_refresh_state: bool):
            raise AssertionError("should not be called")

    sleeps: list[float] = []

    def fake_engine_ctor(**_kwargs):
        return FakeEngine()

    def fake_sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", _settings)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert sleeps == [5]
