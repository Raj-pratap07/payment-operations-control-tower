# Action Planning and Policy Specification

## AI Payment Operations Control Tower

Version: 1.0

Status: Implementation Contract

---

## 1. Purpose

This layer converts an AI investigation recommendation into a
deterministic ActionProposal and evaluates whether that action
is permitted by policy.

The AI may recommend.

The Policy Engine decides.

The system executes only after policy authorization.

---

## 2. Core Flow

Incident
→ Investigation
→ Action Recommendation
→ ActionProposal
→ Policy Evaluation
→ Approval or Automatic Authorization
→ Future Execution

This milestone does NOT execute financial actions.

---

## 3. Action Types

V1 action types:

- RECONCILE_ADJUSTMENT
- FLAG_FOR_REVIEW
- REQUEST_EVIDENCE
- ESCALATE_INCIDENT

Do not add money-moving actions in V1 of this milestone.

---

## 4. Action Proposal

An ActionProposal must contain:

- incident_id
- action_type
- description
- amount_minor when applicable
- currency when applicable
- confidence
- requires_approval
- status
- created_by
- created_at
- updated_at

The action proposal is a recommendation, not an execution.

---

## 5. Policy Evaluation

The Policy Engine must evaluate:

- action type
- amount
- confidence
- incident status
- evidence availability
- policy active/inactive state
- configured maximum amount
- configured minimum confidence
- approval requirement

The result must be deterministic.

---

## 6. Policy Outcomes

Possible outcomes:

### ALLOW_AUTO

The action satisfies all conditions and can be automatically
authorized for future execution.

### REQUIRE_APPROVAL

The action may be valid but requires a human approval.

### REJECT

The action violates policy or lacks sufficient evidence/
confidence.

---

## 7. V1 Example Policy

For RECONCILE_ADJUSTMENT:

Maximum auto-authorized amount:

10000 minor currency units for testing unless configuration
specifies otherwise.

Minimum confidence:

0.90

Evidence required:

At least one valid evidence record.

Policy must be active.

No unresolved contradiction may be present.

If all conditions pass:

ALLOW_AUTO

Otherwise:

REQUIRE_APPROVAL or REJECT according to the policy.

The exact thresholds must remain configurable.

---

## 8. Deterministic Rules

The LLM must never:

- override policy
- change policy thresholds
- authorize its own action
- bypass approval
- execute an action
- alter financial records

Policy evaluation must occur in deterministic application code.

---

## 9. Approval

For actions requiring approval:

Create an Approval record with:

- action_proposal_id
- requested_by
- status = PENDING
- requested_at

A human may later:

- APPROVE
- REJECT

Approval decisions must be auditable.

---

## 10. Action Proposal Lifecycle

PROPOSED
↓
APPROVED / REJECTED
↓
EXECUTING
↓
EXECUTED / FAILED / CANCELLED

For this milestone, execution is not implemented.

---

## 11. Idempotency

The same investigation/action recommendation must not create
duplicate active proposals for the same incident and action type.

Use a deterministic proposal identity strategy.

Do not rely only on random UUIDs for idempotency.

---

## 12. Evidence Requirements

Actions must reference evidence supporting the proposal.

The proposal may only reference evidence that actually exists.

No AI-generated evidence IDs are allowed.

---

## 13. Validation

Before a proposal reaches ALLOW_AUTO:

- action type must be supported
- incident must exist
- incident must not be RESOLVED or DISMISSED
- evidence must exist
- confidence must satisfy policy
- amount must satisfy policy
- policy must be active

---

## 14. No Execution

This milestone MUST NOT:

- issue refunds
- create payouts
- move money
- mutate settlements
- mutate payments
- call Razorpay action APIs
- execute external financial operations

The executor comes later.

---

## 15. Testing

Test:

1. Valid proposal is created.
2. RECONCILE_ADJUSTMENT below threshold can become ALLOW_AUTO.
3. Low confidence requires approval or rejection.
4. Amount above auto threshold requires approval.
5. Missing evidence rejects the proposal.
6. Inactive policy rejects the proposal.
7. Resolved incident cannot create an actionable proposal.
8. Duplicate recommendation does not create duplicate active proposals.
9. Approval is created when required.
10. Approval transitions are deterministic.
11. AI cannot directly execute an action.
12. Policy evaluation is deterministic.

---

## 16. Architecture Boundary

AI:
- recommends

Action Planner:
- creates structured proposal

Policy Engine:
- authorizes, requires approval, or rejects

Approval Service:
- records human authorization

Executor:
- future milestone

Verification:
- future milestone

Audit:
- future milestone

---

## 17. V1 Principle

No financial action becomes executable merely because an LLM
recommended it.

Every action must pass deterministic policy and authorization.