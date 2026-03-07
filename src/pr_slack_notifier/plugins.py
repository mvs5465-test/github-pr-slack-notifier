from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import Action, DerivedStatus, PullRequestSnapshot, RouteConfig


@dataclass(frozen=True)
class PluginContext:
    pr: PullRequestSnapshot
    route: RouteConfig
    status: DerivedStatus


class Plugin(Protocol):
    name: str

    def on_plan(self, context: PluginContext) -> tuple[Action, ...]:
        ...
