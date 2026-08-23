# AI Payment Operations Control Tower

AI Payment Operations Control Tower

Working product name: AI Payment Operations Control Tower

Core thesis: Observe → Correlate → Detect → Investigate → Quantify → Decide → Act → Verify → Audit

1. Product Purpose

The AI Payment Operations Control Tower is a Razorpay-first operational control system for payment-heavy digital businesses. It continuously consumes payment and financial events, reconstructs payment journeys, detects inconsistent, delayed, incomplete, or financially important states, investigates incidents using structured evidence, quantifies financial impact, recommends remediation, and executes only policy-approved actions.

The system is not a generic AI CFO, accounting ERP, payment gateway, fraud detector, or generic reconciliation product. Its core object is the payment/financial incident and its core job is to determine what went wrong, why, how much it matters, and what should safely happen next.

Initial customer profile

Finance / payment-operations teams

Growing SaaS and digital businesses

Meaningful payment volume and enough operational complexity that manual investigation becomes expensive

Razorpay is the initial payment provider integration

North-star operational metrics

Mean Time to Resolve (MTTR) Payment Incidents

Unexplained Money remaining

Incident detection accuracy

Correct root-cause identification

Policy-violation count: must remain zero in evaluated scenarios

2. Architectural Principles

2.1 AI is not the financial authority

LLMs may interpret, investigate, correlate, classify, prioritize, explain, forecast, and propose actions.

Deterministic application code owns:

Financial calculations

Current state

Authorization

Policy enforcement

Idempotency

Execution

Verification

Audit records

2.2 Raw evidence is immutable

External events received from Razorpay must be stored before downstream processing. The original payload is retained as evidence.

2.3 Raw events, domain state, and incidents are distinct

Raw event: what the provider sent

Domain state: what our system currently reconstructs from events

Incident: a meaningful condition requiring attention or action

An incident is never the primary source of financial truth.

2.4 Event delivery is not state ordering

Webhook events may be duplicated or arrive out of order. State reconstruction must therefore use event history and deterministic transition rules rather than arrival order alone.

2.5 Fast ingestion, asynchronous processing

Webhook handling should validate, deduplicate, persist, and acknowledge quickly. Expensive processing such as normalization, controls, correlation, and AI investigation happens asynchronously after persistence.

2.6 Evidence-backed AI

Every material AI conclusion must reference the structured records used to reach it. The AI must not invent evidence, financial values, or action results.

2.7 Policy before execution

An AI recommendation is a proposal. A deterministic policy engine decides whether the proposal may be executed automatically, requires human approval, or must be rejected.

2.8 Verify after acting

A successful API response is not equivalent to a resolved financial incident. After an action executes, the system must re-evaluate the relevant state and verify whether the intended outcome occurred.

2.9 Modular monolith for the MVP

The MVP uses a modular FastAPI application with clear internal boundaries rather than many networked microservices. External service boundaries can be introduced later if justified.

2.10 Provider-specific adapters, provider-neutral domain model

Razorpay is the first deeply implemented adapter. The canonical event and financial domain models should not permanently depend on Razorpay-specific payload shapes.

3. High-Level System Architecture

                         RAZORPAY
                            │
                  ┌─────────┴─────────┐
                  │                   │
                 APIs              Webhooks
                  │                   │
                  └─────────┬─────────┘
                            ▼
                  ┌─────────────────────┐
                  │   EVENT GATEWAY     │
                  │                     │
                  │ Signature           │
                  │ Validation          │
                  │ Idempotency         │
                  │ Raw Event Storage   │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │ CANONICAL EVENT     │
                  │ NORMALIZER          │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │ PAYMENT JOURNEY     │
                  │ STATE ENGINE        │
                  └──────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
          Payments        Refunds       Settlements
              │              │              │
              └──────────────┼──────────────┘
                             │
                  ┌──────────▼───────────┐
                  │   CONTROL ENGINE     │
                  │ state/timing/consist │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │   INCIDENT ENGINE   │
                  └──────────┬──────────┘
                             │
                             ▼
                  ┌─────────────────────┐
                  │  AGENT ORCHESTRATOR │
                  └──────────┬──────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Investigator     Impact          Planner
              │             Agent           │
              └──────────────┼──────────────┘
                             ▼
                      EVIDENCE GRAPH
                             │
                             ▼
                       POLICY ENGINE
                         /       \
                        /         \
                       ▼           ▼
                   AUTO-ACTION  APPROVAL
                        \         /
                         \       /
                          ▼     ▼
                           EXECUTOR
                              │
                              ▼
                           VERIFY
                              │
                              ▼
                         AUDIT TRAIL
                              │
                              ▼
                      COMMAND CENTER

4. Domain Model

4.1 Merchant

Represents the business whose payment operations are monitored.

MVP fields:

id

name

status

created_at

updated_at

The MVP can initially operate with a single demo merchant while retaining the tenant boundary in the model.

4.2 FinancialEvent

Immutable record of an externally received or internally generated financial event.

Purpose: preserve exact source evidence and support replay, debugging, deduplication, normalization, and auditability.

Core fields:

id

merchant_id

source

external_event_id

event_type

occurred_at

received_at

signature_valid

raw_payload

processing_status

processing_error

created_at

updated_at

Constraints:

unique(source, external_event_id)

raw_payload is retained

processing status is separate from source truth

4.3 Payment

Current projection of a payment in the canonical domain model.

Core fields:

id

merchant_id

provider_payment_id

provider_order_id

amount_minor

currency

status

created_at_provider

authorized_at

captured_at

failed_at

created_at

updated_at

Monetary amounts are stored as integer minor units, not floating point.

4.4 PaymentStateTransition

Historical state transition associated with a payment and the event that caused it.

Core fields:

id

payment_id

financial_event_id

from_state

to_state

occurred_at

created_at

Purpose: reconstruct the payment journey without overwriting history.

4.5 Refund

Canonical refund projection.

Core fields:

id

merchant_id

provider_refund_id

payment_id

amount_minor

currency

status

created_at_provider

processed_at

failed_at

created_at

updated_at

4.6 Settlement

Canonical settlement projection.

Core fields:

id

merchant_id

provider_settlement_id

amount_minor

currency

fees_minor

tax_minor

utr

status

settlement_period_start

settlement_period_end

processed_at

created_at

updated_at

A settlement is not assumed to equal one payment. Reconciliation occurs through settlement-level transaction mappings and source records.

4.7 BankTransaction

Canonical bank-side transaction imported from a connector or simulator.

Core fields:

id

merchant_id

external_transaction_id

transaction_type

amount_minor

currency

utr

reference

transaction_at

source

created_at

MVP implementation: CSV/synthetic bank data.

4.8 Incident

Central operational object representing a detected condition that requires attention or action.

Core fields:

id

merchant_id

incident_code

incident_type

severity

status

title

description

financial_exposure_minor

currency

detected_at

resolved_at

created_at

updated_at

Incident types for MVP:

PAYMENT_STATE_CONFLICT

PAYMENT_SIGNAL_OVERDUE

SETTLEMENT_DISCREPANCY

REFUND_FINANCIAL_DRIFT

EVENT_INTEGRITY

SETTLEMENT_CREDIT_DELAY

FINANCIAL_EXPOSURE

Bonus:

PAYMENT_PATH_DEGRADATION

4.9 IncidentEvidence

Associates an incident with records supporting its detection or investigation.

Core fields:

id

incident_id

evidence_type

entity_type

entity_id

relationship

created_at

Examples:

incident → caused_by → refund

incident → compared_with → bank_transaction

incident → derived_from → settlement

4.10 ActionProposal

Structured action proposed by an agent or deterministic control.

Core fields:

id

incident_id

action_type

description

amount_minor

currency

confidence

requires_approval

status

created_by_type

created_at

updated_at

An action proposal does not mutate financial state.

4.11 Approval

Human decision associated with an action proposal.

Core fields:

id

action_proposal_id

requested_by

approved_by

status

reason

requested_at

approved_at

4.12 ActionExecution

Execution record for an approved action.

Core fields:

id

action_proposal_id

execution_id

status

provider_reference

before_state

after_state

error

executed_at

verified_at

4.13 AuditEvent

Immutable operational history.

Core fields:

id

merchant_id

actor_type

actor_id

action_type

entity_type

entity_id

reason

evidence

metadata

created_at

5. Database Relationships

Merchant
  │
  ├── FinancialEvent
  ├── Payment
  │     └── PaymentStateTransition
  ├── Refund
  ├── Settlement
  ├── BankTransaction
  ├── Incident
  │     └── IncidentEvidence
  ├── ActionProposal
  │     └── Approval
  │     └── ActionExecution
  └── AuditEvent

FinancialEvent
  ├── may produce Payment state changes
  ├── may produce Refund state changes
  └── may produce Settlement state changes

A normalized domain record should retain a link back to the source FinancialEvent whenever practical.

6. Payment State Machine

Payment lifecycle

CREATED
   │
   ├───────────────► FAILED
   │
   ▼
AUTHORIZED
   │
   ▼
CAPTURED

Refund lifecycle (separate lifecycle)

CAPTURED PAYMENT
       │
       ▼
REFUND_CREATED
       │
       ├────────────► REFUND_FAILED
       │
       ▼
REFUND_PROCESSED

Settlement lifecycle (separate lifecycle)

ELIGIBLE_FOR_SETTLEMENT
          │
          ▼
SETTLEMENT_PROCESSED
          │
          ▼
BANK_CREDIT_EXPECTED
          │
          ▼
BANK_CREDIT_CONFIRMED

The state engine must support late and out-of-order events. A late event updates the event history and may change the projected state according to deterministic transition rules; it must not simply overwrite records based on arrival time.

7. Incident Lifecycle

OPEN
  │
  ▼
INVESTIGATING
  │
  ├────────────► FALSE_POSITIVE / IGNORED
  │
  ▼
ACTION_REQUIRED
  │
  ▼
RESOLVING
  │
  ├────────────► REOPENED
  │                  │
  │                  └──► INVESTIGATING
  ▼
RESOLVED

The exact state transitions are deterministic and auditable.

8. Detection / Control Engine

The control engine is deterministic and runs before AI investigation.

Control: duplicate event

Condition:

Same provider/source event identifier already processed

Outcome:

Deduplicate

Preserve audit evidence

Do not create duplicate domain movement

Control: payment state conflict

Condition:

Incoming event or reconstructed history produces an impossible/contradictory transition

Outcome:

Create PAYMENT_STATE_CONFLICT

Control: overdue payment signal

Condition:

Expected payment lifecycle event has not arrived within a configured window

Outcome:

Create PAYMENT_SIGNAL_OVERDUE

Control: settlement discrepancy

Condition:

Expected net movement differs from settlement or bank-side observation beyond configured tolerance

Outcome:

Create SETTLEMENT_DISCREPANCY

Control: refund financial drift

Condition:

Refund state and expected downstream financial state do not agree

Outcome:

Create REFUND_FINANCIAL_DRIFT

Control: settlement credit delay

Condition:

Settlement is processed, expected bank-credit window has expired, and corresponding bank credit is absent

Outcome:

Create SETTLEMENT_CREDIT_DELAY

Control: financial exposure

Condition:

One or more incidents create meaningful aggregated cash/revenue exposure

Outcome:

Create or update FINANCIAL_EXPOSURE

9. AI Architecture

Controller Agent

Responsibilities:

Interpret the user's operational question

Select the appropriate tools and specialist agent

Combine structured results into a final answer

Investigator Agent

Answers:

What happened and why?

Tools may include:

get_incident

get_payment

get_payment_history

get_refunds

get_settlement

get_settlement_reconciliation

get_bank_transaction

find_related_transactions

compare_financial_records

Impact Agent

Answers:

How financially important is this?

Responsibilities:

Calculate exposure using deterministic tools

Explain the causal financial impact

Identify major contributing components

Action Planner

Answers:

What should happen next?

The planner returns a structured proposal, not direct execution.

AI output requirement

Material agent outputs must use structured schemas. Minimum investigation result:

root_cause

summary

evidence_ids

financial_impact_minor

unresolved_amount_minor

recommended_action

confidence

The backend validates the result before it is persisted or acted upon.

10. AI / Deterministic Boundary

AI owns

Natural-language interpretation

Evidence selection

Investigation orchestration

Root-cause explanation

Prioritization explanation

Action recommendation

Narrative summaries

Deterministic code owns

Amount calculations

Currency and unit handling

Current financial state

Event deduplication

State transitions

Severity baseline calculation

Policy evaluation

Authorization

API execution

Verification

Audit persistence

The system must never allow an LLM to directly write financial balances, bypass policies, or execute unrestricted money movement.

11. Policy Engine

Actions are evaluated by deterministic rules.

Example policy dimensions

Action type

Amount threshold

Incident type

Evidence count/quality

AI confidence threshold

User role/permission

Existing execution state

Duplicate-action detection

Policy outcomes

AUTO_EXECUTE
APPROVAL_REQUIRED
REJECTED

Money-moving or ambiguous actions are approval-required by default for the MVP.

12. Webhook Processing Contract

The Razorpay webhook endpoint follows this sequence:

HTTP request
    ↓
Read raw body
    ↓
Verify signature
    ↓
Read provider event identifier
    ↓
Idempotency lookup
    ↓
Persist raw FinancialEvent
    ↓
Acknowledge quickly
    ↓
Asynchronous processing

The webhook handler must not perform long-running AI work before responding to the provider.

Processing after persistence:

FinancialEvent
   ↓
Normalize
   ↓
Project domain state
   ↓
Run controls
   ↓
Create/update incident
   ↓
Queue AI investigation when needed

13. Security Requirements

MVP minimum:

Secrets only in environment variables

.env never committed

Razorpay webhook signature validation

No plaintext secret logging

Request validation

Provider event idempotency

Action authorization

Policy enforcement before execution

Immutable audit records

No direct LLM access to unrestricted database mutation

14. Monetary Data Rules

Store monetary values as integer minor units.

Store ISO currency codes.

Do not use floating-point arithmetic for money.

All derived amounts must be calculated by deterministic application code.

AI receives computed amounts and may explain them, but must not invent replacements.

15. MVP Scope

Must have

Razorpay webhook ingestion

Signature verification

Idempotent event handling

Raw event storage

Payment state reconstruction

Refund and settlement projection

Bank/ledger CSV simulator

Deterministic incident detection

Evidence relationships

AI investigation

Financial impact calculation

Action proposals

Policy engine

Human approval workflow

Controlled execution for safe actions

Verification

Audit trail

Operations dashboard

Nice to have

Payment-path degradation detection

Natural-language command center queries

Multi-provider adapters beyond Razorpay

More sophisticated forecasting

Slack/email notifications

Explicitly out of scope for MVP

Full accounting ERP

GST engine

Payroll

Full banking platform

Full multi-PSP orchestration

Custom ML fraud model

Kubernetes

Large microservice fleet

Unrestricted autonomous money movement

16. Development Strategy

The project is intentionally AI-assisted in implementation, but architecture is human-controlled.

Workflow:

Research
  ↓
Product specification
  ↓
Architecture contract
  ↓
Component contracts
  ↓
LLM implementation
  ↓
Automated tests
  ↓
Independent LLM review
  ↓
Human review
  ↓
Integration

No LLM may expand the architecture without an explicit design decision.

No model may silently change domain contracts, security boundaries, or financial rules.

17. Testing Strategy

Unit tests

Signature verification

Event deduplication

Monetary calculations

State transitions

Control rules

Policy rules

Integration tests

Webhook → event → projection

Payment lifecycle

Refund lifecycle

Settlement reconciliation

Incident creation

Action approval/execution

Verification/reopen behavior

Scenario tests

Known-ground-truth scenarios:

Clean payment

Duplicate webhook

Out-of-order webhook

Payment state conflict

Late payment signal

Refund drift

Settlement discrepancy

Missing bank credit

Mixed complex incident

AI evaluation

Measure:

Detection correctness

Root-cause correctness

Evidence precision

Action recommendation accuracy

Hallucinated evidence count

Policy violation count

18. Demo Flow

The primary demo should use one deliberately injected financial incident.

Payment captured
    ↓
Refund processed
    ↓
Settlement processed
    ↓
Bank record differs
    ↓
Control Engine detects discrepancy
    ↓
Incident created
    ↓
AI investigates
    ↓
Evidence displayed
    ↓
Financial impact calculated
    ↓
Action proposed
    ↓
Policy evaluated
    ↓
Safe action executed / human approval
    ↓
System rechecks state
    ↓
Incident resolved or reopened
    ↓
Audit timeline updated

Core demo statement:

We do not merely tell the merchant that money doesn't match. We explain why, show the evidence, quantify the impact, and safely resolve what we are authorized to fix.

19. Repository Structure

payment-control-tower/
│
├── backend/
│   ├── app/
│   │   ├── core/
│   │   ├── database/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── repositories/
│   │   ├── services/
│   │   ├── engines/
│   │   ├── agents/
│   │   ├── policies/
│   │   ├── routers/
│   │   └── main.py
│   └── tests/
│
├── frontend/
├── simulator/
├── scripts/
├── docs/
├── .gitignore
├── README.md
└── docker-compose.yml

20. Architectural Freeze Rules

Before the MVP is functional:

Do not add new core domain entities without a design review.

Do not add infrastructure technologies merely for appearance.

Do not let an AI model change API/database contracts silently.

Do not permit financial execution without deterministic policy checks.

Do not delete raw financial evidence because a domain projection was updated.

Do not claim incident resolution without verification.

This document is the source of truth for the initial implementation unless a later architecture decision explicitly supersedes a section.