"""Bounded, evidence-grounded AI investigation orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agents.provider import LLMProvider, ProviderResponse, configured_provider
from app.agents.prompts import INVESTIGATOR_SYSTEM_PROMPT
from app.schemas.investigation import InvestigationEvidence, InvestigationOutput
from app.services.investigation_tools import InvestigationToolLayer, ToolError


class InvestigationError(RuntimeError):
    """Base class for safe investigator failures."""


class InvalidInvestigationOutputError(InvestigationError):
    pass


class InvestigationService:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        max_tool_calls: int = 8,
        tools_factory: type[InvestigationToolLayer] = InvestigationToolLayer,
    ) -> None:
        if max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative.")
        self._provider = provider or configured_provider()
        self._max_tool_calls = max_tool_calls
        self._tools_factory = tools_factory

    def investigate(self, db: Session, incident_id: UUID) -> InvestigationOutput:
        tools = self._tools_factory(db)
        try:
            incident_result = tools.execute("get_incident", {"incident_id": incident_id})
            evidence_result = tools.execute("get_incident_evidence", {"incident_id": incident_id})
        except ToolError as error:
            return self._incomplete(incident_id, f"Required incident evidence could not be retrieved: {error}")

        known_ids = _record_ids(incident_result) | _record_ids(evidence_result)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": {"incident_id": str(incident_id), "incident": incident_result, "evidence": evidence_result}}
        ]
        calls_used = 0
        last_response: ProviderResponse | None = None
        while calls_used < self._max_tool_calls + 1:
            try:
                response = self._provider.complete(
                    system_prompt=INVESTIGATOR_SYSTEM_PROMPT,
                    messages=messages,
                    tools=sorted(InvestigationToolLayer.TOOL_NAMES),
                )
            except Exception as error:
                return self._incomplete(incident_id, f"Investigation provider failed: {error}", known_ids)
            last_response = response
            if not response.tool_calls:
                break
            if calls_used + len(response.tool_calls) > self._max_tool_calls:
                return self._incomplete(incident_id, "Investigation stopped after reaching the tool-call limit.", known_ids)
            for call in response.tool_calls:
                try:
                    result = tools.execute(call.name, call.arguments)
                except (ToolError, TypeError, ValueError) as error:
                    return self._incomplete(incident_id, f"Investigation tool failed: {error}", known_ids)
                known_ids |= _record_ids(result)
                messages.append({"role": "tool", "name": call.name, "call_id": call.call_id, "content": result})
                calls_used += 1
        if last_response is None or last_response.content is None:
            return self._incomplete(incident_id, "No structured investigation response was produced.", known_ids)
        return self._validate_output(last_response.content, incident_id, known_ids)

    @staticmethod
    def _validate_output(content: Any, incident_id: UUID, known_ids: set[str]) -> InvestigationOutput:
        try:
            output = InvestigationOutput.model_validate(content)
        except ValidationError as error:
            raise InvalidInvestigationOutputError("Investigator returned malformed structured output.") from error
        if output.incident_id != incident_id:
            raise InvalidInvestigationOutputError("Investigator output references a different incident.")
        unknown = [item.entity_id for item in output.evidence if item.entity_id not in known_ids]
        if unknown:
            raise InvalidInvestigationOutputError(f"Investigator returned unsupported evidence IDs: {unknown}.")
        return output

    @staticmethod
    def _incomplete(incident_id: UUID, uncertainty: str, known_ids: set[str] | None = None) -> InvestigationOutput:
        evidence = [
            InvestigationEvidence(entity_type="record", entity_id=record_id, relationship="retrieved")
            for record_id in sorted(known_ids or set())
        ]
        return InvestigationOutput(
            incident_id=incident_id,
            root_cause="Undetermined",
            summary="The investigation could not establish a supported root cause.",
            observed_facts=[],
            derived_findings=[],
            evidence=evidence,
            financial_impact_minor=0,
            unresolved_amount_minor=0,
            recommended_action="Review the available incident evidence manually.",
            confidence=0.0,
            uncertainties=[uncertainty],
        )


def _record_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"id", "entity_id", "incident_id", "payment_id", "financial_event_id", "payment_id", "refund_id", "settlement_id", "transaction_id"} and isinstance(item, str):
                ids.add(item)
            ids |= _record_ids(item)
    elif isinstance(value, list):
        for item in value:
            ids |= _record_ids(item)
    return ids