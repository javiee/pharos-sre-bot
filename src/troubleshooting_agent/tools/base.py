from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolInfo:
    """A single tool a signal provider advertises (name + description)."""
    name: str
    description: str
    parameters: dict | None = None

class SignalProvider(Protocol):
    """The shape every provider satisfies (GrafanaSignals, KubernetesSignals).

    A Protocol is structural typing: any object with these two methods qualifies,
    no inheritance required. Both wrappers already match it.
    """

    def list_tools(self) -> list[ToolInfo]: ...
    def call_tool(self, name: str, arguments: dict) -> str: ...