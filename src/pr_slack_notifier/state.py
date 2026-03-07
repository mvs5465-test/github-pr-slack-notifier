from __future__ import annotations

import json
import re
from hashlib import sha256

from .models import ReconcileState, SlackMessageRef

_MARKER_PREFIX = "pr-slack-notifier"
_MARKER_RE = re.compile(r"<!--\s*pr-slack-notifier:(\{.*?\})\s*-->")


def make_fingerprint(fields: list[str]) -> str:
    return sha256("|".join(fields).encode("utf-8")).hexdigest()


def render_state_marker(state: ReconcileState) -> str:
    payload = {
        "v": state.version,
        "channel": state.message.channel,
        "ts": state.message.ts,
        "fingerprint": state.fingerprint,
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"<!-- {_MARKER_PREFIX}:{serialized} -->"


def parse_state_marker(comment_body: str | None) -> ReconcileState | None:
    if not comment_body:
        return None
    match = _MARKER_RE.search(comment_body)
    if not match:
        return None

    payload = json.loads(match.group(1))
    return ReconcileState(
        message=SlackMessageRef(channel=payload["channel"], ts=payload["ts"]),
        fingerprint=payload["fingerprint"],
        version=int(payload.get("v", 1)),
    )
