import pytest

from pr_slack_notifier import app
from pr_slack_notifier.config import Settings
from pr_slack_notifier.models import RouteConfig


def _settings() -> Settings:
    return Settings(
        github_app_id="123",
        github_app_private_key="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
        github_installation_ids=(99,),
        slack_bot_token="xoxb-test",
        polling_interval_seconds=1,
        dry_run=True,
        routes=(RouteConfig(org_pattern="acme", repo_pattern="*", channel="C1"),),
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
            dry_run=False,
            routes=(),
        ),
    )
    with pytest.raises(RuntimeError, match="missing required env vars"):
        app._validate_settings()


def test_run_forever_runs_single_iteration(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.called = 0

        def run_once(self):
            self.called += 1

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
    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].called == 1
    assert state["github"]["app_id"] == "123"
    assert "BEGIN PRIVATE KEY" in state["github"]["private_key_pem"]
    assert state["slack"]["bot_token"] == "xoxb-test"
