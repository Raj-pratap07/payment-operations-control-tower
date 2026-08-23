"""Small provider boundary for mocked or future LLM implementations."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResponse:
    content: Any = None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMProvider(Protocol):
    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        """Return a bounded response or explicit read-only tool calls."""


class NoProvider:
    """Provider used when no API-backed LLM is configured."""

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        return ProviderResponse(content=None)