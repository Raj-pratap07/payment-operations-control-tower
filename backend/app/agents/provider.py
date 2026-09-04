"""Small provider boundary for mocked or future LLM implementations."""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from app.core.config import settings


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


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


class GeminiProvider:
    """Google Gemini adapter for the investigator's bounded provider contract."""

    _TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
        "get_incident": {"incident_id": {"type": "string"}},
        "get_incident_evidence": {"incident_id": {"type": "string"}},
        "get_financial_event": {"event_id": {"type": "string"}},
        "get_payment": {
            "payment_id": {"type": "string"},
            "provider_payment_id": {"type": "string"},
        },
        "get_payment_history": {"payment_id": {"type": "string"}},
        "get_refund": {"refund_id": {"type": "string"}, "provider_refund_id": {"type": "string"}},
        "get_settlement": {"settlement_id": {"type": "string"}, "provider_settlement_id": {"type": "string"}},
        "get_bank_transaction": {"transaction_id": {"type": "string"}, "external_transaction_id": {"type": "string"}},
        "find_related_transactions": {
            "amount": {"type": "integer"}, "currency": {"type": "string"}, "utr": {"type": "string"},
        },
        "compare_financial_records": {
            "expected_amount": {"type": "integer"}, "observed_amount": {"type": "integer"}, "currency": {"type": "string"},
        },
        "calculate_financial_difference": {
            "expected_amount": {"type": "integer"}, "observed_amount": {"type": "integer"},
        },
    }
    _REQUIRED_TOOL_ARGS: dict[str, list[str]] = {
        "get_incident": ["incident_id"],
        "get_incident_evidence": ["incident_id"],
        "get_financial_event": ["event_id"],
        "get_payment_history": ["payment_id"],
        "find_related_transactions": ["amount", "currency"],
        "compare_financial_records": ["expected_amount", "observed_amount", "currency"],
        "calculate_financial_difference": ["expected_amount", "observed_amount"],
    }

    def __init__(self, api_key: str, model: str, client: Any = None) -> None:
        if not api_key:
            raise ValueError("Gemini API key is not configured.")
        self._api_key = api_key
        self._model = model
        self._interaction_id: str | None = None
        self._last_input: str | None = None
        self._sent_tool_results = 0
        if client is None:
            try:
                from google import genai
            except ImportError as error:
                raise RuntimeError("The google-genai package is unavailable.") from error
            client = genai.Client(api_key=api_key)
        self._client = client

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        try:
            response = self._create_interaction(system_prompt, messages, tools)
        except Exception as error:
            raise RuntimeError(self._safe_error(error)) from error

        calls = tuple(ToolCall(name=step.name, arguments=dict(step.arguments or {}), call_id=step.id) for step in self._steps(response) if getattr(step, "type", None) == "function_call" and getattr(step, "name", None))
        if calls:
            return ProviderResponse(tool_calls=calls)
        content = getattr(response, "output_text", None) or self._model_output_text(response)
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as error:
                raise RuntimeError("Gemini returned malformed structured output.") from error
        if not isinstance(content, dict):
            raise RuntimeError("Gemini returned malformed structured output.")
        return ProviderResponse(content=content)

    def _create_interaction(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        initial = json.dumps(messages[0].get("content", ""), default=str) if messages else ""
        if initial != self._last_input:
            self._interaction_id = None
            self._sent_tool_results = 0
            self._last_input = initial
        request: dict[str, Any] = {
            "model": self._model,
            "input": self._tool_results(messages) if self._interaction_id else initial,
            "tools": self._declarations(tools),
            "system_instruction": system_prompt,
            "response_format": [{"type": "text", "mime_type": "application/json", "schema": self._response_schema()}],
        }
        if self._interaction_id:
            request["previous_interaction_id"] = self._interaction_id
        response = self._client.interactions.create(**request)
        self._interaction_id = getattr(response, "id", None) or self._interaction_id
        return response

    def _tool_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        new_messages = tool_messages[self._sent_tool_results:]
        self._sent_tool_results = len(tool_messages)
        return [
            {
                "type": "function_result",
                "name": message.get("name", ""),
                "call_id": message.get("call_id") or message.get("name", ""),
                "result": [{"type": "text", "text": json.dumps(message.get("content", {}), default=str)}],
            }
            for message in new_messages
        ]

    @staticmethod
    def _steps(response: Any) -> list[Any]:
        return list(getattr(response, "steps", None) or ())

    def _model_output_text(self, response: Any) -> Any:
        for step in reversed(self._steps(response)):
            if getattr(step, "type", None) != "model_output":
                continue
            content = getattr(step, "content", None) or ()
            text = "".join(getattr(item, "text", "") for item in content if getattr(item, "type", None) == "text")
            if text:
                return text
        return None

    @classmethod
    def _declarations(cls, tools: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": name,
                "description": f"Read-only investigation tool: {name}.",
                "parameters": {
                    "type": "object",
                    "properties": cls._TOOL_SCHEMAS.get(name, {}),
                    "required": cls._REQUIRED_TOOL_ARGS.get(name, []),
                },
            }
            for name in tools
            if name in cls._TOOL_SCHEMAS
        ]

    @staticmethod
    def _response_schema() -> Any:
        return {
            "type": "object",
            "properties": {
                "incident_id": {"type": "string"},
                "root_cause": {"type": "string"},
                "summary": {"type": "string"},
                "observed_facts": {"type": "array", "items": {"type": "string"}},
                "derived_findings": {"type": "array", "items": {"type": "string"}},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity_type": {"type": "string"},
                            "entity_id": {"type": "string"},
                            "relationship": {"type": "string"},
                        },
                        "required": ["entity_type", "entity_id", "relationship"],
                    },
                },
                "financial_impact_minor": {"type": "integer"},
                "unresolved_amount_minor": {"type": "integer"},
                "recommended_action": {"type": "string"},
                "confidence": {"type": "number"},
                "uncertainties": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "incident_id", "root_cause", "summary", "observed_facts", "derived_findings", "evidence",
                "financial_impact_minor", "unresolved_amount_minor", "recommended_action", "confidence", "uncertainties",
            ],
        }

    def _safe_error(self, error: Exception) -> str:
        detail = str(error).replace(self._api_key, "[redacted]")
        name = type(error).__name__.lower()
        if "429" in detail or "rate" in name or "quota" in detail:
            return "Gemini rate limit or quota error."
        if "timeout" in name or "timed out" in detail.lower():
            return "Gemini provider timed out."
        if "unauthor" in detail.lower() or "permission" in detail.lower() or "auth" in name:
            return "Gemini API authentication failed."
        return f"Gemini provider unavailable: {detail or 'unknown provider error.'}"


class OpenRouterProvider:
    """OpenAI-compatible OpenRouter adapter for bounded investigations."""

    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, api_key: str, model: str, http_client: Any = None, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is not configured.")
        self._api_key = api_key
        self._model = model
        self._http_client = http_client or requests
        self._timeout = timeout
        self._conversation: list[dict[str, Any]] = []

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        try:
            payload = self._payload(system_prompt, messages, tools)
            response = self._http_client.post(
                f"{self.BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
            message = body["choices"][0]["message"]
        except Exception as error:
            raise RuntimeError(self._safe_error(error)) from error

        tool_calls = tuple(
            ToolCall(
                name=call["function"]["name"],
                arguments=json.loads(call["function"].get("arguments", "{}")),
                call_id=call.get("id"),
            )
            for call in message.get("tool_calls", ())
        )
        if tool_calls:
            self._conversation.append(message)
            return ProviderResponse(tool_calls=tool_calls)
        content = message.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as error:
                raise RuntimeError("OpenRouter returned malformed structured output.") from error
        if not isinstance(content, dict):
            raise RuntimeError("OpenRouter returned malformed structured output.")
        return ProviderResponse(content=content)

    def _payload(self, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> dict[str, Any]:
        if not self._conversation:
            self._conversation.append({"role": "system", "content": system_prompt})
            self._conversation.append({"role": "user", "content": json.dumps(messages[0].get("content", {}), default=str) if messages else ""})
        known_tool_messages = sum(message.get("role") == "tool" for message in self._conversation)
        incoming_tool_messages = [message for message in messages if message.get("role") == "tool"]
        for message in incoming_tool_messages[known_tool_messages:]:
            self._conversation.append({
                "role": "tool",
                "tool_call_id": message.get("call_id") or message.get("name", ""),
                "content": json.dumps(message.get("content", {}), default=str),
            })
        return {
            "model": self._model,
            "messages": self._conversation,
            "tools": self._declarations(tools),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "investigation_output", "strict": True, "schema": self._response_schema()},
            },
        }

    @classmethod
    def _declarations(cls, tools: list[str]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": item["name"], "description": item["description"], "parameters": item["parameters"]}}
            for item in GeminiProvider._declarations(tools)
        ]

    @staticmethod
    def _response_schema() -> dict[str, Any]:
        return GeminiProvider._response_schema()

    def _safe_error(self, error: Exception) -> str:
        detail = str(error).replace(self._api_key, "[redacted]")
        response = getattr(error, "response", None)
        code = getattr(response, "status_code", None)
        if code in {401, 403}:
            return "OpenRouter authentication or authorization failed."
        if code == 404:
            return "OpenRouter model or endpoint was not found."
        if code == 429:
            return "OpenRouter rate limit exceeded."
        if code in {500, 502, 503}:
            return f"OpenRouter provider unavailable ({code})."
        if isinstance(error, requests.Timeout) or "timeout" in detail.lower() or "timed out" in detail.lower():
            return "OpenRouter provider timed out."
        return f"OpenRouter provider unavailable: {detail or 'unknown provider error.'}"


def configured_provider() -> LLMProvider:
    if settings.AI_PROVIDER == "openrouter":
        if not settings.OPENROUTER_API_KEY:
            return NoProvider()
        return OpenRouterProvider(settings.OPENROUTER_API_KEY, settings.OPENROUTER_MODEL)
    if settings.AI_PROVIDER == "gemini":
        if not settings.GEMINI_API_KEY:
            return NoProvider()
        return GeminiProvider(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
    return NoProvider()