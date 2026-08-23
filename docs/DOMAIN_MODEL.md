# Domain Model Specification

## AI Payment Operations Control Tower

Version: 1.0

This document is the implementation contract for the V1 domain model.

The implementation must follow this document and `docs/ARCHITECTURE.md`.

---

## 1. FinancialEvent

Purpose: immutable record of an external financial event received by the system.

Fields:

- id: UUID primary key
- source: string, required
- external_event_id: string, required
- event_type: string, required
- occurred_at: timezone-aware datetime, required
- received_at: timezone-aware datetime, required
- signature_valid: boolean, required
- raw_payload: PostgreSQL JSONB, required
- processing_status: enum
- processing_error: nullable text
- created_at: timezone-aware datetime, required

Processing status values:

- RECEIVED
- PROCESSING
- PROCESSED
- FAILED

Constraint:

- unique(source, external_event_id)

---

## 2. Payment

Purpose: current projection of a payment.

Fields:

- id: UUID primary key
- provider_payment_id: string, required, unique
- provider_order_id: nullable string
- amount: BIGINT, required
- currency: string, required
- status: enum, required
- created_at: timezone-aware datetime
- authorized_at: nullable timezone-aware datetime
- captured_at: nullable timezone-aware datetime
- failed_at: nullable timezone-aware datetime
- updated_at: timezone-aware datetime

Money must use integer smallest currency units.

Never use floating point for money.

Payment status values:

- CREATED
- AUTHORIZED
- CAPTURED
- FAILED
- REFUND_PENDING
- REFUNDED

---

## 3. PaymentStateTransition

Purpose: immutable historical record of payment state changes.

Fields:

- id: UUID primary key
- payment_id: foreign key to payments.id
- from_state: nullable payment status
- to_state: payment status
- financial_event_id: foreign key to financial_events.id
- occurred_at: timezone-aware datetime
- created_at: timezone-aware datetime

Relationship:

- one Payment has many PaymentStateTransition records
- one FinancialEvent may cause a state transition

Do not overwrite transition history.

---

## 4. Refund

Purpose: represents a provider refund lifecycle.

Fields:

- id: UUID primary key
- provider_refund_id: string, required, unique
- payment_id: foreign key to payments.id
- amount: BIGINT, required
- currency: string, required
- status: enum, required
- created_at: timezone-aware datetime
- processed_at: nullable timezone-aware datetime
- failed_at: nullable timezone-aware datetime
- updated_at: timezone-aware datetime

Refund status values:

- CREATED
- PENDING
- PROCESSED
- FAILED

Relationship:

- one Payment can have many Refunds

---

## 5. Settlement

Purpose: represents provider settlement information.

Fields:

- id: UUID primary key
- provider_settlement_id: string, required, unique
- amount: BIGINT, required
- currency: string, required
- fees: BIGINT, required
- tax: BIGINT, required
- utr: nullable string
- status: enum, required
- settlement_period_start: nullable timezone-aware datetime
- settlement_period_end: nullable timezone-aware datetime
- processed_at: nullable timezone-aware datetime
- created_at: timezone-aware datetime
- updated_at: timezone-aware datetime

Settlement status values:

- CREATED
- PROCESSED
- FAILED

Do not assume one settlement belongs to one payment.

---

## 6. BankTransaction

Purpose: represents imported or simulated bank-side transaction data.

Fields:

- id: UUID primary key
- external_transaction_id: string, required, unique
- transaction_type: string, required
- amount: BIGINT, required
- currency: string, required
- utr: nullable string
- reference: nullable string
- transaction_at: timezone-aware datetime
- source: string
- created_at: timezone-aware datetime

---

## 7. Incident

Purpose: operational representation of a meaningful abnormal, incomplete, contradictory, delayed, or financially significant condition.

Fields:

- id: UUID primary key
- incident_code: string, required, unique
- incident_type: enum, required
- severity: enum, required
- status: enum, required
- title: string, required
- description: nullable text
- financial_exposure: BIGINT, nullable
- currency: nullable string
- detected_at: timezone-aware datetime
- resolved_at: nullable timezone-aware datetime
- created_at: timezone-aware datetime
- updated_at: timezone-aware datetime

Incident types:

- PAYMENT_STATE_CONFLICT
- PAYMENT_SIGNAL_OVERDUE
- SETTLEMENT_DISCREPANCY
- REFUND_FINANCIAL_DRIFT
- EVENT_INTEGRITY
- SETTLEMENT_CREDIT_DELAY
- FINANCIAL_EXPOSURE
- PAYMENT_PATH_DEGRADATION

Incident severity:

- LOW
- MEDIUM
- HIGH
- CRITICAL

Incident status:

- OPEN
- INVESTIGATING
- ACTION_REQUIRED
- RESOLVING
- RESOLVED
- DISMISSED

---

## 8. IncidentEvidence

Purpose: associates an incident with the financial records that support the incident.

Fields:

- id: UUID primary key
- incident_id: foreign key to incidents.id
- evidence_type: string, required
- entity_type: string, required
- entity_id: UUID/string reference, required
- relationship: string, required
- created_at: timezone-aware datetime

Evidence must be traceable to real records.

---

## 9. Policy

Purpose: deterministic rules controlling whether an action can be executed automatically.

Fields:

- id: UUID primary key
- name: string, required
- description: nullable text
- action_type: string, required
- max_amount: nullable BIGINT
- min_confidence: nullable numeric
- requires_approval: boolean, required
- is_active: boolean, required
- created_at: timezone-aware datetime
- updated_at: timezone-aware datetime

The LLM must never override a policy.

---

## 10. ActionProposal

Purpose: structured action recommended by an AI agent.

Fields:

- id: UUID primary key
- incident_id: foreign key to incidents.id
- action_type: string, required
- description: string, required
- amount: nullable BIGINT
- currency: nullable string
- confidence: nullable numeric
- requires_approval: boolean
- status: enum
- created_by: string
- created_at: timezone-aware datetime
- updated_at: timezone-aware datetime

Action proposal status:

- PROPOSED
- APPROVED
- REJECTED
- EXECUTING
- EXECUTED
- FAILED
- CANCELLED

An AI agent creates proposals. It does not directly execute financial actions.

---

## 11. Approval

Purpose: human authorization for actions requiring approval.

Fields:

- id: UUID primary key
- action_proposal_id: foreign key to action_proposals.id
- requested_by: string
- approved_by: nullable string
- status: enum
- reason: nullable text
- requested_at: timezone-aware datetime
- approved_at: nullable timezone-aware datetime

Approval status:

- PENDING
- APPROVED
- REJECTED
- EXPIRED

---

## 12. ActionExecution

Purpose: actual execution record of an approved action.

Fields:

- id: UUID primary key
- action_proposal_id: foreign key to action_proposals.id
- execution_id: string, required
- status: string, required
- provider_reference: nullable string
- before_state: nullable JSONB
- after_state: nullable JSONB
- error: nullable text
- executed_at: nullable timezone-aware datetime
- verified_at: nullable timezone-aware datetime

Proposal and execution are separate concepts.

---

## 13. AuditEvent

Purpose: immutable operational audit history.

Fields:

- id: UUID primary key
- actor_type: string, required
- actor_id: nullable string
- action_type: string, required
- entity_type: string, required
- entity_id: nullable string
- reason: nullable text
- evidence: nullable JSONB
- metadata: nullable JSONB
- created_at: timezone-aware datetime

Audit records must never be silently overwritten.

---

## Global Architectural Rules

1. UUIDs are used for internal primary keys.
2. Provider IDs remain strings.
3. Money is stored as integer smallest currency units.
4. Financial timestamps are timezone-aware.
5. Raw external events are immutable.
6. Payment state history must never be overwritten.
7. AI recommendations are not financial authority.
8. Deterministic policy controls action execution.
9. High-risk financial actions require human approval.
10. Duplicate external events must be safely handled.
11. Database schema changes must use Alembic.
12. Do not introduce unrelated domain entities in V1.