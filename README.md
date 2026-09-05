AI Payment Operations Control Tower

An AI-assisted payment operations control tower for detecting, investigating, deciding, and resolving payment exceptions.

The system is designed around a simple operational loop:

Observe → Correlate → Detect → Investigate → Quantify → Decide → Act → Verify → Audit

It is built as a modular FastAPI backend with a React/TypeScript operator dashboard, PostgreSQL persistence, deterministic controls, evidence-backed AI investigation, policy-controlled actions, and verification/audit workflows.

What the project does

The Control Tower turns payment operations data into an operator workflow:

Ingest raw Razorpay webhooks with HMAC verification and idempotency.

Normalize provider-specific events into canonical financial events.

Project payments, refunds, settlements, and state transitions.

Detect deterministic operational and financial incidents.

Investigate incidents using traceable evidence and an LLM provider when available.

Plan actions from validated investigation results.

Evaluate policy deterministically before execution.

Require approval for actions that exceed auto-approval conditions.

Execute controlled internal actions without exposing live Razorpay mutation APIs.

Verify the resulting state and resolve the incident when the control is satisfied.

Audit approval and operational transitions.

Key capabilities

Razorpay webhook ingestion

POST /webhooks/razorpay

HMAC-SHA256 signature validation

Required Razorpay event ID

Raw-body validation before JSON parsing

Idempotent persistence using (source, external_event_id)

Immutable raw financial-event storage

Deterministic normalization

Supported canonical event types include:

PAYMENT_AUTHORIZED

PAYMENT_CAPTURED

PAYMENT_FAILED

REFUND_CREATED

REFUND_PROCESSED

REFUND_FAILED

SETTLEMENT_PROCESSED

Provider-specific payloads are converted into provider-neutral structures while preserving integer minor-unit amounts and provider timestamps.

Projection and state history

The backend maintains:

Payment projections

Payment state transitions

Refund projections

Settlement projections

Source-event relationships

Domain-level idempotency

Protection against unsafe out-of-order state transitions

Deterministic controls

Implemented controls include:

Payment state conflict

Payment signal overdue

Settlement discrepancy

Refund financial drift

Event integrity

Settlement credit delay

Aggregate financial exposure

Incidents are derived records backed by explicit evidence. Financial records remain authoritative.

Evidence-backed AI investigation

The investigator uses an allow-listed read-only tool layer for retrieving:

Incidents

Evidence

Payments

Payment history

Refunds

Settlements

Bank transactions

Financial events

Related transactions

Deterministic financial comparisons

The LLM cannot execute SQL, Python, shell commands, filesystem operations, or financial mutations.

The investigation loop is bounded and validates final structured output, including evidence identifiers.

AI providers

The project supports explicit provider selection through configuration.

Supported providers:

Gemini

OpenRouter

The current demo configuration uses OpenRouter. A deterministic, evidence-backed fallback investigation is also available when the configured provider is unavailable or returns unusable structured output.

The fallback is not an LLM and is explicitly marked as fallback output. It derives financial values from retrieved domain records and does not invent identifiers, amounts, currencies, or unsupported conclusions.

Action planning and policy

Validated investigations can create existing ActionProposal records.

Supported action types include:

RECONCILE_ADJUSTMENT

FLAG_FOR_REVIEW

REQUEST_EVIDENCE

ESCALATE_INCIDENT

Policy outcomes are deterministic:

ALLOW_AUTO

REQUIRE_APPROVAL

REJECT

The backend remains authoritative for action permissions, thresholds, evidence, approval, and execution.

Execution and verification

The control tower supports:

Approved proposal → execution → verification → resolution

Execution is deliberately controlled and does not expose live payment-provider mutation APIs.

Operator dashboard

The React frontend provides screens for:

Dashboard / command center

Incident queue and filtering

Incident detail and evidence

AI investigation

Action proposal creation

Action center

Payment journey

Approval and rejection

Execution and verification state

Audit timeline

The UI does not implement financial or policy logic; it consumes backend state and services.

Technology stack

Backend

Python

FastAPI

SQLAlchemy

PostgreSQL

Alembic

Pydantic / pydantic-settings

Pytest

Frontend

React

TypeScript

Vite

React Router

Lucide React

Tailwind CSS / Vite integration

AI

Gemini provider adapter

OpenRouter provider adapter

Structured investigation output

Function/tool calling through the existing read-only evidence layer

Evidence-backed deterministic fallback

Repository structure

payment-control-tower/
├── backend/
│   ├── app/
│   │   ├── agents/          # AI provider, investigator, fallback
│   │   ├── core/            # configuration, enums, security
│   │   ├── database/        # database/session setup
│   │   ├── engines/         # normalization, projections, controls
│   │   ├── models/          # SQLAlchemy domain models
│   │   ├── policies/        # policy engine
│   │   ├── repositories/    # persistence/data access
│   │   ├── routers/         # REST/webhook endpoints
│   │   ├── schemas/         # API/canonical/investigation schemas
│   │   └── services/        # domain/application services
│   ├── alembic/              # database migrations
│   ├── tests/                # backend tests
│   ├── .env.example
│   └── requirements.txt
├── frontend/
│   ├── src/
│   └── package.json
├── simulator/
│   └── run.py               # deterministic demo scenarios
└── docs/
    ├── ARCHITECTURE.md
    └── DOMAIN_MODEL.md

Prerequisites

Install:

Python 3.11+ (the project has been exercised in a Python virtual environment)

Node.js + npm

PostgreSQL

Git

Backend setup

From the repository root:

cd backend

Create/activate the backend virtual environment, then install dependencies:

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Create the backend environment file:

backend/.env

At minimum configure the PostgreSQL connection and the provider settings used by your environment.

Example shape:

DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/payment_control_tower
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_MODEL=openrouter/free
INVESTIGATION_FALLBACK=true

Keep API keys server-side. Never put provider secrets in frontend/.env.

Database

The current Alembic head is:

20260823_0002

Apply migrations with:

alembic upgrade head

Check for schema drift with:

alembic check

Run the backend

From backend/:

.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8097

Backend/API:

http://127.0.0.1:8097

Swagger/OpenAPI:

http://127.0.0.1:8097/docs

Frontend setup and run

From the repository root:

cd frontend
npm install
npm run dev

Frontend:

http://localhost:5173

The frontend uses the backend API base URL configured through VITE_API_BASE_URL. For local development, it can point to:

VITE_API_BASE_URL=http://127.0.0.1:8097

Demo scenario

The built-in simulator provides deterministic operational scenarios.

For the primary settlement-discrepancy demo:

cd C:\Users\Kabir\payment-control-tower
python simulator\run.py settlement_discrepancy

The scenario creates a realistic exception where:

Expected settlement: ₹5,00,000

Observed settlement: ₹4,72,000

Discrepancy: ₹28,000

All monetary values are stored as integer minor units in the backend (for INR, paise).

The demo also creates/uses the reconciliation policy needed to exercise the approval workflow.

Recommended demo flow

After starting PostgreSQL, the backend, and the frontend:

Open http://localhost:5173.

Open the settlement discrepancy incident.

Review the linked evidence.

Click Investigate incident.

Review the evidence-backed investigation.

Click Create action proposal.

Confirm the deterministic policy result is REQUIRE_APPROVAL.

Open Action Center.

Approve the proposal.

Execute the approved action.

Verify the resulting state.

Confirm the incident reaches its resolved state and the audit trail reflects the workflow.

REST API overview

Dashboard

GET /dashboard/summary

Incidents

GET /incidents
GET /incidents/{incident_id}
GET /incidents/{incident_id}/evidence

Investigation

POST /investigations/{incident_id}
POST /investigations/{incident_id}/action

Payments

GET /payments/{payment_id}
GET /payments/{payment_id}/journey

Actions

GET /actions
GET /actions/{action_id}
POST /actions/{action_id}/approve
POST /actions/{action_id}/reject
POST /actions/{action_id}/execute
POST /actions/{action_id}/verify

Audit

GET /audit/{entity_type}/{entity_id}

Razorpay webhook

POST /webhooks/razorpay

Safety principles

The project intentionally keeps financial authority in deterministic backend code.

Raw financial events are immutable at the application layer.

Payment state transitions are validated and recorded historically.

Incidents are derived records, not financial authority.

Evidence must be traceable to retrieved records.

AI recommendations are not authoritative financial instructions.

Policy evaluation is deterministic and backend-controlled.

Approval gates cannot be bypassed by the frontend.

Unsupported proposal/action types fail closed.

Execution uses a controlled internal adapter and does not expose live Razorpay mutation APIs.

Verification is required before an action/incident is considered complete.

Provider failures do not silently become fabricated AI findings.

Testing

Backend focused tests can be run from backend/ with:

.\.venv\Scripts\python.exe -m pytest -q

Frontend production build:

cd frontend
npm run build

Alembic consistency:

cd ..\backend
.\.venv\Scripts\alembic.exe check

Important local-demo note

The repository contains an Alembic lifecycle test that performs a downgrade/upgrade round-trip. Do not run the entire backend suite against a live demo database that you intend to preserve, because that lifecycle test can intentionally recreate the schema and wipe the demo rows.

Use a dedicated test database for destructive migration tests.

Current V1 scope

The implemented V1 covers the core operational control loop:

Razorpay-first ingestion

Canonical event normalization

Payment/refund/settlement projections

Deterministic controls and incidents

Evidence-backed AI investigation

Action proposals and deterministic policy

Human approval

Controlled execution

Verification and resolution

REST API

Operator dashboard

Deterministic demo simulator

The architecture is intentionally modular so future work can add additional providers, controls, policies, investigation providers, and operator capabilities without moving financial authority into the LLM layer.

Project philosophy

The Control Tower treats AI as an investigator and reasoning aid, not as the system of record.

The principle is:

Evidence first. Deterministic controls second. Policy before action. Verification before resolution.

That keeps the system explainable, auditable, and safe enough for payment-operations workflows.
