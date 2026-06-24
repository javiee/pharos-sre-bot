from __future__ import annotations
from typing import Protocol

from .base import ToolInfo, SignalProvider


class SignalRegistry:
    """A registry of signal providers, keyed by name.

    The agent uses this to find the right provider for a given tool.
    """

    def __init__(self, providers: list[SignalProvider]) -> None:
        self._providers = providers
        self._owner: dict[str, SignalProvider] | None = None

    def _index(self) -> dict[str, SignalProvider]:
        """Build a dict mapping tool names to the provider that owns them."""
        if self._owner is None:
            self._owner = {}
            for provider in self._providers:
                for tool in provider.list_tools():
                    self._owner[tool.name] = provider
        return self._owner
    
    def list_tools(self) -> list[ToolInfo]:
        merged: list[ToolInfo] = [] 
        for provider in self._providers:
            merged.extend(provider.list_tools())
        return merged
    def call_tool(self, name: str, arguments: dict) -> str:
        """Call the tool with the given name and arguments.

        Raises KeyError if no provider owns that tool.
        """
        provider = self._index().get(name)

        if provider is None:
            return(f"No provider owns tool {name}")
        return provider.call_tool(name, arguments)