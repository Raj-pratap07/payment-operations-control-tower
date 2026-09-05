"""Evidence-backed deterministic fallback for investigation failures.

This is NOT an LLM. It constructs InvestigationOutput directly from
evidence retrieved by the existing InvestigationToolLayer, using the
same InvestigationOutput schema.
"""

from __future__ import annotations

from typing import Any

from app.schemas.investigation import InvestigationEvidence, InvestigationOutput


_FALLBACK_UNCERTAINTY_PREFIX = "[fallback]"


class EvidenceBackedFallbackProvider:
    """Deterministic fallback that builds InvestigationOutput from tool-retrieved evidence.

    When the real provider fails (malformed output, 429, 500, 502, 503,
    timeout, unavailable), this provider constructs a valid InvestigationOutput
    from the incident and evidence already retrieved by the tool layer.

    It is NEVER an LLM and does not pretend to be one.
    """

    def construct(
        self,
        *,
        incident: dict[str, Any],
        evidence_records: list[dict[str, Any]],
        incident_id: Any,
        fetched_records: list[dict[str, Any]] | None = None,
    ) -> InvestigationOutput:
        """Build InvestigationOutput from evidence retrieved by the tool layer.

        All financial values come from the retrieved records. No IDs,
        amounts, currencies, or root causes are invented.

        ``incident`` may be either the raw incident dict or the tool-layer
        wrapper ``{"tool": "get_incident", "result": {...}}``.  We unwrap
        it here so callers don't need to know the wrapper shape.

        ``fetched_records`` are the actual Settlement / BankTransaction
        dicts (with ``amount`` etc.) pre-fetched by the caller via
        ``get_settlement`` / ``get_bank_transaction``.
        """
        incident = self._unwrap_incident(incident)
        evidence_records = self._unwrap_list(evidence_records)
        fetched_records = self._unwrap_list(fetched_records) if fetched_records else []

        known_evidence = [
            InvestigationEvidence(
                entity_type=item.get("entity_type", "record"),
                entity_id=item.get("entity_id", ""),
                relationship=item.get("relationship", "supports"),
            )
            for item in evidence_records
            if item.get("entity_id")
        ]

        incident_type = incident.get("incident_type", "")
        if incident_type == "SETTLEMENT_DISCREPANCY":
            return self._settlement_discrepancy(
                incident_id=incident_id,
                incident=incident,
                evidence_records=evidence_records,
                fetched_records=fetched_records,
                known_evidence=known_evidence,
            )

        return self._conservative(
            incident_id=incident_id,
            incident=incident,
            known_evidence=known_evidence,
        )

    def _settlement_discrepancy(
        self,
        *,
        incident_id: Any,
        incident: dict[str, Any],
        evidence_records: list[dict[str, Any]],
        fetched_records: list[dict[str, Any]],
        known_evidence: list[InvestigationEvidence],
    ) -> InvestigationOutput:
        """Construct investigation for settlement discrepancy from actual evidence.

        Financial values are derived deterministically from evidence records:
        - Expected amount from incident.financial_exposure
        - Observed amount from fetched Settlement / BankTransaction records
        - Difference computed from the two

        A missing optional BankTransaction does not fail the investigation;
        it is recorded as an uncertainty.  A missing authoritative Settlement
        record yields a conservative incomplete investigation rather than an
        invented amount.
        """
        financial_exposure = incident.get("financial_exposure") or 0
        currency = incident.get("currency") or "INR"
        status = incident.get("status", "OPEN")

        referenced_settlement_ids = {
            item.get("entity_id") for item in evidence_records
            if item.get("entity_type") == "Settlement" and item.get("entity_id")
        }
        referenced_bank_ids = {
            item.get("entity_id") for item in evidence_records
            if item.get("entity_type") == "BankTransaction" and item.get("entity_id")
        }
        retrieved_settlement_ids = {
            str(record.get("id")) for record in fetched_records
            if "provider_settlement_id" in record and record.get("id")
        }
        retrieved_bank_ids = {
            str(record.get("id")) for record in fetched_records
            if "external_transaction_id" in record and record.get("id")
        }
        missing_bank = referenced_bank_ids - retrieved_bank_ids

        # The Settlement record is the authoritative source for the observed
        # amount.  Without a retrieved Settlement we cannot derive financials
        # deterministically, so return a conservative incomplete investigation
        # rather than inventing amounts.
        if not retrieved_settlement_ids:
            return self._conservative(
                incident_id=incident_id,
                incident=incident,
                known_evidence=known_evidence,
            )

        observed_amount = self._extract_observed_amount(fetched_records or evidence_records)
        difference = abs(int(financial_exposure))
        expected_amount = observed_amount + difference if observed_amount else difference

        settlement_ids = [
            item.get("entity_id", "")
            for item in evidence_records
            if item.get("entity_type") == "Settlement" and item.get("entity_id")
        ]
        bank_ids = [
            item.get("entity_id", "")
            for item in evidence_records
            if item.get("entity_type") == "BankTransaction" and item.get("entity_id")
        ]

        observed_facts = [
            f"Incident type: {incident_type}" if (incident_type := incident.get("incident_type")) else "Incident type unknown.",
            f"Expected amount: {expected_amount} minor units.",
            f"Observed amount: {observed_amount} minor units.",
            f"Difference: {difference} minor units.",
            f"Currency: {currency}.",
            f"Settlement status: {status}.",
        ]
        if settlement_ids:
            observed_facts.append(f"Settlement evidence IDs: {', '.join(settlement_ids)}.")
        if bank_ids:
            observed_facts.append(f"Bank transaction evidence IDs: {', '.join(bank_ids)}.")

        derived_findings = []
        if observed_amount and expected_amount and observed_amount < expected_amount:
            derived_findings.append(
                f"Observed amount ({observed_amount}) is lower than expected ({expected_amount})."
            )
        if difference:
            derived_findings.append(f"Difference is {difference} minor units.")
        if status in {"OPEN", "INVESTIGATING"}:
            derived_findings.append(f"Discrepancy remains unresolved while incident is {status}.")

        uncertainties = [
            f"{_FALLBACK_UNCERTAINTY_PREFIX} Constructed from retrieved evidence; no LLM analysis was performed.",
        ]
        if missing_bank:
            uncertainties.append(
                f"{_FALLBACK_UNCERTAINTY_PREFIX} Related bank transaction evidence could not be retrieved."
            )
        if not observed_amount:
            uncertainties.append(
                f"{_FALLBACK_UNCERTAINTY_PREFIX} Observed amount could not be determined from evidence records."
            )

        return InvestigationOutput(
            incident_id=incident_id,
            root_cause="Settlement amount discrepancy between expected and observed settlement amount.",
            summary="Deterministic evidence-backed investigation: settlement amount differs from expected records.",
            observed_facts=observed_facts,
            derived_findings=derived_findings,
            evidence=known_evidence,
            financial_impact_minor=difference,
            unresolved_amount_minor=difference,
            recommended_action="Review and reconcile the settlement discrepancy.",
            confidence=0.95,
            uncertainties=uncertainties,
        )

    def _conservative(
        self,
        *,
        incident_id: Any,
        incident: dict[str, Any],
        known_evidence: list[InvestigationEvidence],
    ) -> InvestigationOutput:
        """Conservative fallback for unsupported incident types.

        Returns a valid InvestigationOutput with no invented findings.
        Financial values are zero; the root cause is indeterminate.
        """
        incident_type = incident.get("incident_type", "unknown")

        return InvestigationOutput(
            incident_id=incident_id,
            root_cause="Undetermined",
            summary=f"Fallback: no deterministic analysis available for incident type {incident_type}.",
            observed_facts=[
                f"Incident type: {incident_type}.",
                f"Incident status: {incident.get('status', 'unknown')}.",
            ],
            derived_findings=[],
            evidence=known_evidence,
            financial_impact_minor=0,
            unresolved_amount_minor=0,
            recommended_action="Review the available incident evidence manually.",
            confidence=0.0,
            uncertainties=[
                f"{_FALLBACK_UNCERTAINTY_PREFIX} No deterministic analysis available for incident type {incident_type}.",
                f"{_FALLBACK_UNCERTAINTY_PREFIX} Constructed from retrieved evidence; no LLM analysis was performed.",
            ],
        )

    @staticmethod
    def _unwrap_incident(incident: dict[str, Any]) -> dict[str, Any]:
        """Unwrap the tool-layer incident wrapper if present.

        InvestigationToolLayer.execute() wraps results as
        ``{"tool": "get_incident", "result": {...}}``.
        Return the inner ``result`` dict when that shape is detected.
        """
        if "tool" in incident and "result" in incident and isinstance(incident["result"], dict):
            return incident["result"]
        return incident

    @staticmethod
    def _unwrap_list(value: Any) -> list[dict[str, Any]]:
        """Unwrap the tool-layer list wrapper if present.

        InvestigationToolLayer.execute() wraps results as
        ``{"tool": "...", "result": [...]}``.  Return the inner list
        when that shape is detected.
        """
        if isinstance(value, dict) and "result" in value and isinstance(value["result"], list):
            return value["result"]
        if isinstance(value, list):
            return value
        return []

    @staticmethod
    def _extract_observed_amount(records: list[dict[str, Any]]) -> int:
        """Extract the observed amount from fetched Settlement or BankTransaction records.

        Handles two shapes:
        - Evidence rows (entity_type + entity_id, no amount)
        - Fetched domain records (amount, currency, no entity_type)

        Returns 0 if no observed amount found.
        """
        for record in records:
            entity_type = record.get("entity_type", "")
            if entity_type == "Settlement" and "amount" in record:
                return record["amount"]
        for record in records:
            entity_type = record.get("entity_type", "")
            if entity_type == "BankTransaction" and "amount" in record:
                return record["amount"]
        for record in records:
            if "amount" in record and isinstance(record["amount"], (int, float)):
                return record["amount"]
        return 0
