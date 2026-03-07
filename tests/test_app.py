import pytest

from pr_slack_notifier import app
from pr_slack_notifier.config import Settings
from pr_slack_notifier.models import RouteConfig


def test_unconfigured_adapters_raise() -> None:
    gh = app.UnconfiguredGitHubAdapter()
    slack = app.UnconfiguredSlackAdapter()

    with pytest.raises(NotImplementedError):
        gh.list_pull_requests(None)
    with pytest.raises(NotImplementedError):
        gh.get_bot_state_comment(None)
    with pytest.raises(NotImplementedError):
        gh.upsert_bot_state_comment(None, "")
    with pytest.raises(NotImplementedError):
        slack.post_message("C1", "test")
    with pytest.raises(NotImplementedError):
        slack.update_message("C1", "123", "test")


def test_run_forever_runs_single_iteration(monkeypatch) -> None:
    class FakeEngine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.called = 0

        def run_once(self):
            self.called += 1

    state = {"engine": None}

    def fake_engine_ctor(**kwargs):
        engine = FakeEngine(**kwargs)
        state["engine"] = engine
        return engine

    def fake_sleep(_seconds):
        raise RuntimeError("stop")

    monkeypatch.setattr(app, "load_settings_from_env", lambda: Settings(
        github_app_id="",
        github_installation_ids=(),
        slack_bot_token="",
        polling_interval_seconds=1,
        dry_run=True,
        routes=(RouteConfig(org_pattern="acme", repo_pattern="*", channel="C1"),),
    ))
    monkeypatch.setattr(app, "ReconcileEngine", fake_engine_ctor)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    with pytest.raises(RuntimeError, match="stop"):
        app.run_forever()

    assert state["engine"] is not None
    assert state["engine"].called == 1
