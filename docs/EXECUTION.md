# Execution, Verification and Audit Specification

## AI Payment Operations Control Tower

Version: 1.0

Status: Implementation Contract

---

## 1. Purpose

This layer executes only actions that have already been authorized
by deterministic policy.

It verifies whether execution produced the expected result and
records the complete action lifecycle in the audit trail.

The executor is NOT an AI decision-maker.

---

## 2. Core Flow

ActionProposal
→ Policy Decision
→ Approval if required
→ Execution
→ Verification
→ Audit
→ Incident Update

---

## 3. Execution Rules

An action may execute only when:

- action type is supported
- ActionProposal exists
- ActionProposal is not already executed
- policy authorization exists
- required approval exists
- incident is actionable
- idempotency requirements are satisfied

If any condition fails:

Do not execute.

Return a structured execution failure.

---

## 4. V1 Executable Actions

V1 may execute only these controlled internal actions:

- RECONCILE_ADJUSTMENT
- FLAG_FOR_REVIEW
- REQUEST_EVIDENCE
- ESCALATE_INCIDENT

Do NOT execute:

- live refunds
- live payouts
- live transfers
- real money movement

---

## 5. Execution Adapter

Create an execution adapter abstraction.

The initial implementation may be an internal/test adapter.

Future adapters may include:

- Razorpay refund adapter
- Razorpay payout adapter
- other controlled financial adapters

The executor must not be coupled directly to a single provider.

---

## 6. Execution Idempotency

The same ActionProposal must never execute twice.

Use a stable execution identity.

Repeated execution attempts must return the existing execution
result instead of performing the action again.

---

## 7. Action Execution Lifecycle

ActionProposal
→ APPROVED
→ EXECUTING
→ EXECUTED

Failure:

ActionProposal
→ EXECUTING
→ FAILED

Cancellation:

ActionProposal
→ CANCELLED

---

## 8. Verification

Execution is not considered successful until verification passes.

Verification must determine whether the expected state/result
actually occurred.

Examples:

RECONCILE_ADJUSTMENT:
- expected internal adjustment exists

FLAG_FOR_REVIEW:
- incident status reflects review requirement

REQUEST_EVIDENCE:
- evidence-request state is recorded

ESCALATE_INCIDENT:
- incident escalation state is recorded

---

## 9. Incident Resolution

Only successful verified actions may cause an incident to become
RESOLVED.

A failed or unverified action must NOT mark the incident resolved.

If verification fails:

- keep incident open or return it to ACTION_REQUIRED
- record execution failure
- preserve audit history

---

## 10. Audit

Every action lifecycle event must produce an AuditEvent.

Audit events must capture:

- actor type
- actor ID when applicable
- action type
- entity type
- entity ID
- reason
- evidence
- metadata
- timestamp

Important lifecycle events:

- action proposed
- policy authorized
- approval granted
- execution started
- execution completed
- execution failed
- verification completed
- verification failed
- incident resolved
- incident reopened

---

## 11. Actor Types

Supported V1 actor types:

- AI_AGENT
- HUMAN
- SYSTEM

The executor itself should normally record SYSTEM as the actor.

---

## 12. No AI Authority

The executor must not:

- decide policy
- change policy
- invent actions
- override approvals
- change financial truth
- bypass idempotency
- execute unsupported actions

The executor executes an already-authorized action.

---

## 13. Transactions

Execution state changes and related audit events must remain
transactionally consistent.

Do not leave an ActionExecution record showing EXECUTED if the
corresponding audit event failed.

External provider calls, when introduced later, must be designed
for retry safety and provider-level idempotency.

---

## 14. Testing

Test:

1. Authorized action executes.
2. Unauthorized action is rejected.
3. Approval-required action cannot execute without approval.
4. Duplicate execution returns the existing execution.
5. Execution failure is recorded.
6. Verification success resolves the incident.
7. Verification failure does not resolve the incident.
8. Audit events are created for lifecycle transitions.
9. Unsupported action types are rejected.
10. Action execution is idempotent.
11. No live Razorpay money-moving APIs are called.
12. Executor does not contain policy-decision logic.

---

## 15. V1 Principle

The system can autonomously perform only controlled, non-money-moving
actions.

Real financial execution is a future adapter-level capability and
requires additional provider-specific safeguards.