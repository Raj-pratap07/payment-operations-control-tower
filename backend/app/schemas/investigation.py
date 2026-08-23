"""Validated structured output for an AI incident investigation."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InvestigationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    relationship: str = Field(min_length=1)


class InvestigationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    root_cause: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    observed_facts: list[str]
    derived_findings: list[str]
    evidence: list[InvestigationEvidence]
    financial_impact_minor: int = Field(ge=0)
    unresolved_amount_minor: int = Field(ge=0)
    recommended_action: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    uncertainties: list[str]