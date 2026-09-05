"""Bounded, evidence-grounded AI investigation orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agents.fallback import EvidenceBackedFallbackProvider
from app.agents.provider import LLMProvider, ProviderResponse, configured_provider
from app.agents.prompts import INVESTIGATOR_SYSTEM_PROMPT
from app.core.config import settings
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
        use_fallback: bool | None = None,
    ) -> None:
        if max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative.")
        self._provider = provider or configured_provider()
        self._max_tool_calls = max_tool_calls
        self._tools_factory = tools_factory
        self._use_fallback = use_fallback if use_fallback is not None else settings.INVESTIGATION_FALLBACK
        self._fallback = EvidenceBackedFallbackProvider()

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
                if self._use_fallback:
                    return self._fallback.construct(
                        incident=incident_result,
                        evidence_records=evidence_result,
                        incident_id=incident_id,
                        fetched_records=_prefetch_entity_records(tools, evidence_result),
                    )
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
                    if self._use_fallback:
                        return self._fallback.construct(
                            incident=incident_result,
                            evidence_records=evidence_result,
                            incident_id=incident_id,
                            fetched_records=_prefetch_entity_records(tools, evidence_result),
                        )
                    return self._incomplete(incident_id, f"Investigation tool failed: {error}", known_ids)
                known_ids |= _record_ids(result)
                messages.append({"role": "tool", "name": call.name, "call_id": call.call_id, "content": result})
                calls_used += 1
        if last_response is None or last_response.content is None:
            return self._incomplete(incident_id, "No structured investigation response was produced.", known_ids)
        try:
            return self._validate_output(last_response.content, incident_id, evidence_result)
        except (InvalidInvestigationOutputError, ValidationError) as error:
            if self._use_fallback:
                return self._fallback.construct(
                    incident=incident_result,
                    evidence_records=evidence_result,
                    incident_id=incident_id,
                    fetched_records=_prefetch_entity_records(tools, evidence_result),
                )
            raise

    @staticmethod
    def _validate_output(content: Any, incident_id: UUID, evidence_result: Any) -> InvestigationOutput:
        try:
            output = InvestigationOutput.model_validate(content)
        except ValidationError as error:
            raise InvalidInvestigationOutputError("Investigator returned malformed structured output.") from error
        if output.incident_id != incident_id:
            raise InvalidInvestigationOutputError("Investigator output references a different incident.")
        requested_keys = {(item.entity_type, item.entity_id, item.relationship) for item in output.evidence}
        if not requested_keys:
            raise InvalidInvestigationOutputError("Investigator returned no evidence for the incident.")
        valid_keys = _evidence_tuples(evidence_result)
        unknown = [
            item.entity_id
            for item in output.evidence
            if (item.entity_type, item.entity_id, item.relationship) not in valid_keys
        ]
        if not requested_keys.issubset(valid_keys):
            raise InvalidInvestigationOutputError(
                f"Investigator returned unsupported evidence: {unknown or 'none'}."
            )
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


def _evidence_tuples(value: Any) -> set[tuple[str, str, str]]:
    """Return the exact (entity_type, entity_id, relationship) tuples of the
    incident's ``IncidentEvidence`` rows, matching the Action Planner contract.

    ``value`` is the ``get_incident_evidence`` tool result, either the raw
    wrapper ``{"tool": ..., "result": [...]}`` or the unwrapped list.
    """
    items = value.get("result") if isinstance(value, dict) and isinstance(value.get("result"), list) else value
    if not isinstance(items, list):
        return set()
    return {
        (item.get("entity_type"), item.get("entity_id"), item.get("relationship"))
        for item in items
        if item.get("entity_id")
    }


def _prefetch_entity_records(
    tools: InvestigationToolLayer,
    evidence_result: Any,
) -> list[dict[str, Any]]:
    """Pre-fetch actual Settlement / BankTransaction records for evidence entity IDs.

    The incident-evidence rows contain entity_type and entity_id but not
    the financial fields (amount, currency, etc.) the fallback needs.
    This helper fetches the actual domain records so the fallback can
    derive financial values deterministically.
    """
    records: list[dict[str, Any]] = []
    if isinstance(evidence_result, dict) and "result" in evidence_result:
        evidence_items = evidence_result["result"] if isinstance(evidence_result["result"], list) else []
    elif isinstance(evidence_result, list):
        evidence_items = evidence_result
    else:
        evidence_items = []
    for item in evidence_items:
        entity_type = item.get("entity_type", "")
        entity_id_str = item.get("entity_id", "")
        if not entity_id_str:
            continue
        try:
            from uuid import UUID as _UUID
            entity_uuid = _UUID(entity_id_str)
        except (ValueError, TypeError):
            continue
        try:
            if entity_type == "Settlement":
                records.append(tools.get_settlement(settlement_id=entity_uuid))
            elif entity_type == "BankTransaction":
                records.append(tools.get_bank_transaction(transaction_id=entity_uuid))
        except ToolError:
            continue
    return records