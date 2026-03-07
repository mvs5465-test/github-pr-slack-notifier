from pr_slack_notifier.models import ReconcileState, SlackMessageRef
from pr_slack_notifier.state import make_fingerprint, parse_state_marker, render_state_marker


def test_fingerprint_is_stable() -> None:
    one = make_fingerprint(["a", "b", "c"])
    two = make_fingerprint(["a", "b", "c"])
    assert one == two


def test_round_trip_state_marker() -> None:
    state = ReconcileState(
        message=SlackMessageRef(channel="C123", ts="1.23"),
        fingerprint="abc",
        version=1,
    )
    marker = render_state_marker(state)
    parsed = parse_state_marker(marker)
    assert parsed == state


def test_parse_state_marker_absent() -> None:
    assert parse_state_marker("hello") is None
    assert parse_state_marker(None) is None
