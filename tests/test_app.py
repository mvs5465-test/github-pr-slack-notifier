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


def test_loop_resources_org_wide_route_includes_graphql_for_sweep() -> None:
    resources = app._loop_resources(_settings())
    assert resources["lightweight"] == {"search"}
    assert resources["deep"] == {"core", "search"}
    assert resources["sweep"] == {"core", "search", "graphql"}


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

        def reconcile_sweep(self):
            self.sweep_calls += 1

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
            raise GitHubRateLimitError("rate", reset_at_epoch=101, resource="search")

        def reconcile_changed(self):
            raise AssertionError("should not be called")

        def reconcile_sweep(self):
            raise AssertionError("should not be called")

    sleeps: list[float] = []
    state = {"calls": 0}
    rate_limit_events: list[tuple[str, str, int]] = []

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
    monkeypatch.setattr(app, "observe_rate_limit", lambda stage, resource, retry: rate_limit_events.append((stage, resource, retry)))
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app.time, "time", fake_time)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert sleeps == [1]
    assert rate_limit_events == [("loop", "search", 2)]


def test_run_forever_retries_on_generic_error_without_crashing(monkeypatch) -> None:
    class FakeEngine:
        def refresh_lightweight(self):
            raise RuntimeError("boom")

        def reconcile_changed(self):
            raise AssertionError("should not be called")

        def reconcile_sweep(self):
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


def test_run_forever_runs_sweep(monkeypatch) -> None:
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

        def reconcile_sweep(self):
            self.sweep_calls += 1

    state = {"engine": None}

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def fake_sleep(_seconds):
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

    assert state["engine"] is not None
    assert state["engine"].refresh_calls == 1
    assert state["engine"].changed_calls == 1
    assert state["engine"].sweep_calls == 1


def test_run_forever_deep_runs_when_search_blocked_but_core_available(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.refresh_calls = 0
            self.changed_calls = 0
            self.sweep_calls = 0

        def refresh_lightweight(self):
            self.refresh_calls += 1
            raise GitHubRateLimitError("rate", reset_at_epoch=102, resource="search")

        def reconcile_changed(self):
            self.changed_calls += 1

        def reconcile_sweep(self):
            self.sweep_calls += 1

    state = {"engine": None}
    rate_limit_events: list[tuple[str, str, int]] = []

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def fake_sleep(_seconds):
        raise RuntimeError("stop")

    def settings_with_wildcard_org() -> Settings:
        s = _settings()
        return Settings(
            github_app_id=s.github_app_id,
            github_app_private_key=s.github_app_private_key,
            github_installation_ids=s.github_installation_ids,
            slack_bot_token=s.slack_bot_token,
            polling_interval_seconds=s.polling_interval_seconds,
            deep_reconcile_interval_seconds=s.deep_reconcile_interval_seconds,
            sweep_reconcile_interval_seconds=s.sweep_reconcile_interval_seconds,
            rate_limit_backoff_seconds=s.rate_limit_backoff_seconds,
            rate_limit_backoff_max_seconds=s.rate_limit_backoff_max_seconds,
            error_retry_seconds=s.error_retry_seconds,
            enable_historical_closed_prs=s.enable_historical_closed_prs,
            dry_run=s.dry_run,
            routes=(RouteConfig(org_pattern="ac*", repo_pattern="*", channel="C1"),),
            log_level=s.log_level,
            json_logs=s.json_logs,
            metrics_enabled=s.metrics_enabled,
            metrics_port=s.metrics_port,
            otel_service_name=s.otel_service_name,
            otel_otlp_endpoint=s.otel_otlp_endpoint,
        )

    monkeypatch.setattr(app, "load_settings_from_env", settings_with_wildcard_org)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app, "observe_rate_limit", lambda stage, resource, retry: rate_limit_events.append((stage, resource, retry)))
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app.time, "time", lambda: 100.0)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].refresh_calls == 1
    assert state["engine"].changed_calls == 1
    assert state["engine"].sweep_calls == 1
    assert rate_limit_events == [("loop", "search", 3)]


def test_run_forever_blocks_sweep_when_graphql_resource_is_rate_limited(monkeypatch) -> None:
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

        def reconcile_sweep(self):
            self.sweep_calls += 1
            if self.sweep_calls == 1:
                raise GitHubRateLimitError("rate", reset_at_epoch=105, resource="graphql")

    state = {"engine": None}
    rate_limit_events: list[tuple[str, str, int]] = []
    sleeps: list[float] = []

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", _settings)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app, "observe_rate_limit", lambda stage, resource, retry: rate_limit_events.append((stage, resource, retry)))
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app.time, "time", lambda: 100.0)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].refresh_calls == 2
    assert state["engine"].changed_calls >= 1
    assert state["engine"].sweep_calls == 1
    assert rate_limit_events == [("loop", "graphql", 6)]


def test_run_forever_fast_paths_deep_when_lightweight_detects_changes(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.refresh_calls = 0
            self.changed_calls = 0
            self.sweep_calls = 0

        def refresh_lightweight(self):
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                return 0
            return 1

        def reconcile_changed(self):
            self.changed_calls += 1
            return 0

        def reconcile_sweep(self):
            self.sweep_calls += 1
            return 0

    state = {"engine": None}
    sleeps: list[float] = []

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def settings_with_slow_deep() -> Settings:
        s = _settings()
        return Settings(
            github_app_id=s.github_app_id,
            github_app_private_key=s.github_app_private_key,
            github_installation_ids=s.github_installation_ids,
            slack_bot_token=s.slack_bot_token,
            polling_interval_seconds=1,
            deep_reconcile_interval_seconds=120,
            sweep_reconcile_interval_seconds=120,
            rate_limit_backoff_seconds=s.rate_limit_backoff_seconds,
            rate_limit_backoff_max_seconds=s.rate_limit_backoff_max_seconds,
            error_retry_seconds=s.error_retry_seconds,
            enable_historical_closed_prs=s.enable_historical_closed_prs,
            dry_run=s.dry_run,
            routes=s.routes,
            log_level=s.log_level,
            json_logs=s.json_logs,
            metrics_enabled=s.metrics_enabled,
            metrics_port=s.metrics_port,
            otel_service_name=s.otel_service_name,
            otel_otlp_endpoint=s.otel_otlp_endpoint,
        )

    def fake_sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) >= 2:
            raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", settings_with_slow_deep)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].refresh_calls == 2
    # first deep run is scheduled at startup; second deep run is fast-pathed from lightweight detection
    assert state["engine"].changed_calls == 2


def test_fast_path_does_not_delay_next_deep_when_blocked(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.refresh_calls = 0
            self.changed_calls = 0
            self.sweep_calls = 0

        def refresh_lightweight(self):
            self.refresh_calls += 1
            if self.refresh_calls == 2:
                return 1
            return 0

        def reconcile_changed(self):
            self.changed_calls += 1
            if self.changed_calls == 1:
                raise GitHubRateLimitError("rate", reset_at_epoch=2, resource="core")
            return 0

        def reconcile_sweep(self):
            self.sweep_calls += 1
            return 0

    state = {"engine": None, "now": 0.0}
    sleeps: list[float] = []

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def settings_with_fast_poll() -> Settings:
        s = _settings()
        return Settings(
            github_app_id=s.github_app_id,
            github_app_private_key=s.github_app_private_key,
            github_installation_ids=s.github_installation_ids,
            slack_bot_token=s.slack_bot_token,
            polling_interval_seconds=1,
            deep_reconcile_interval_seconds=2,
            sweep_reconcile_interval_seconds=120,
            rate_limit_backoff_seconds=s.rate_limit_backoff_seconds,
            rate_limit_backoff_max_seconds=s.rate_limit_backoff_max_seconds,
            error_retry_seconds=s.error_retry_seconds,
            enable_historical_closed_prs=s.enable_historical_closed_prs,
            dry_run=s.dry_run,
            routes=s.routes,
            log_level=s.log_level,
            json_logs=s.json_logs,
            metrics_enabled=s.metrics_enabled,
            metrics_port=s.metrics_port,
            otel_service_name=s.otel_service_name,
            otel_otlp_endpoint=s.otel_otlp_endpoint,
        )

    def fake_sleep(seconds):
        sleeps.append(seconds)
        state["now"] += float(seconds)
        if len(sleeps) >= 3:
            raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", settings_with_fast_poll)
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app, "GitHubAppAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "SlackApiAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(app, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app, "maybe_start_metrics_server", lambda **_kwargs: None)
    monkeypatch.setattr(app, "configure_tracing", lambda **_kwargs: None)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(app.time, "time", lambda: state["now"])

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    # deep call 1 is rate-limited; fast-path deep stays blocked; next scheduled deep still runs once due
    assert state["engine"].changed_calls == 2
