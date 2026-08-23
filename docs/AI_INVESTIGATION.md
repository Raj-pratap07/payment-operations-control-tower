# AI Investigation Specification

## AI Payment Operations Control Tower

Version: 1.0

Status: Implementation Contract

---

## 1. Purpose

The AI Investigator explains incidents that have already been
deterministically detected by the Control Engine.

The AI is an investigator and reasoning layer.

It is NOT the source of financial truth.

It is NOT allowed to directly modify financial state.

It is NOT allowed to execute financial actions.

---

## 2. Core Flow

Incident
→ Evidence Retrieval
→ Structured Investigation
→ Root Cause
→ Evidence-backed Explanation
→ Financial Impact Explanation
→ Confidence
→ Recommended Action

---

## 3. Responsibilities

The Investigator may:

- inspect an incident
- inspect linked evidence
- retrieve related payments
- retrieve payment state history
- retrieve refunds
- retrieve settlements
- retrieve bank transactions
- compare financial records using deterministic tools
- identify likely root cause
- summarize the causal chain
- explain financial impact
- identify unresolved amounts
- recommend a next action
- provide confidence

The Investigator may NOT:

- create financial truth
- modify payments
- modify refunds
- modify settlements
- modify financial events
- change incident type
- change incident severity arbitrarily
- bypass policies
- execute actions
- issue refunds
- issue payouts
- invent evidence
- fabricate transaction IDs
- invent financial amounts
- directly access unrestricted SQL

---

## 4. Input

Primary input:

- incident_id

The investigator retrieves all required information through
explicit tools.

The LLM must not receive unrestricted database access.

---

## 5. Required Tools

The Investigator may call tools equivalent to:

- get_incident
- get_incident_evidence
- get_financial_event
- get_payment
- get_payment_history
- get_refund
- get_settlement
- get_bank_transaction
- find_related_transactions
- compare_financial_records
- calculate_financial_difference

Tools must return structured data.

Tools must not return secrets.

Tools must not expose database credentials.

---

## 6. Evidence Requirement

Every factual conclusion about financial state must be supported
by retrieved evidence.

The Investigator must distinguish:

1. Observed facts
2. Derived calculations
3. Reasoned conclusions
4. Uncertainty

Example:

Observed:
Settlement S123 = ₹500,000.

Observed:
Bank transaction B991 = ₹472,000.

Derived:
Difference = ₹28,000.

Reasoned conclusion:
The settlement appears to contain an unresolved discrepancy.

The AI must never present a reasoned conclusion as an observed fact.

---

## 7. Evidence IDs

Every investigation must reference the IDs of the records used.

Examples:

- payment ID
- refund ID
- settlement ID
- bank transaction ID
- financial event ID

Evidence references must come from actual tool results.

Never invent identifiers.

---

## 8. Investigation Output

The Investigator must return structured output equivalent to:

{
    "incident_id": "...",
    "root_cause": "...",
    "summary": "...",
    "observed_facts": [
        "..."
    ],
    "derived_findings": [
        "..."
    ],
    "evidence": [
        {
            "entity_type": "...",
            "entity_id": "...",
            "relationship": "..."
        }
    ],
    "financial_impact_minor": 0,
    "unresolved_amount_minor": 0,
    "recommended_action": "...",
    "confidence": 0.0,
    "uncertainties": [
        "..."
    ]
}

---

## 9. Financial Rules

The LLM must not perform authoritative financial calculations
when the result can be calculated deterministically.

For example:

Do NOT ask the LLM to calculate:

500000 - 472000

Instead call:

calculate_financial_difference()

The deterministic backend provides:

28000

The LLM explains what that difference means.

All monetary values use integer smallest currency units.

Never use floating point for authoritative financial calculations.

---

## 10. Confidence

Confidence must represent confidence in the investigation,
not confidence in a financial calculation.

Confidence should consider:

- evidence completeness
- consistency of evidence
- number of independent supporting records
- ambiguity
- contradictory records

The investigator must lower confidence when important evidence
is missing or contradictory.

The investigator must not claim high confidence simply because
the language model is confident.

---

## 11. Uncertainty

When evidence is insufficient, the Investigator must say so.

Example:

"The available records establish a ₹28,000 settlement difference,
but the current evidence is insufficient to determine whether the
difference is caused by fees, refunds, or a bank-side timing issue."

Do not fabricate a root cause.

---

## 12. Incident Integrity

The Investigator must treat the existing Incident record as the
authoritative detected condition.

It may explain the incident.

It must not silently change:

- incident_type
- incident status
- deterministic severity
- financial state

---

## 13. Recommended Actions

The Investigator may recommend an action.

It must NOT execute the action.

Example:

Recommended action:
"Review the three unmatched settlement records."

or:

Recommended action:
"Reconcile the identified refund offsets."

The recommendation will later be processed by a separate
Action Planner and Policy Engine.

---

## 14. Failure Handling

If required evidence cannot be retrieved:

- return a structured investigation failure
- explain what evidence is missing
- do not invent the missing evidence

If tools return contradictory information:

- surface the contradiction
- reduce confidence
- do not force a single explanation without justification

If no root cause can be established:

- explicitly state that root cause is undetermined

---

## 15. No Direct Execution

The Investigator MUST NOT have tools for:

- refund execution
- payout execution
- payment mutation
- settlement mutation
- bank transaction mutation

Execution belongs to later controlled action services.

---

## 16. Architecture Boundary

The architecture is:

Deterministic Financial State
→ Deterministic Controls
→ Incident
→ AI Investigation
→ Action Proposal
→ Policy Engine
→ Approval
→ Action Execution
→ Verification
→ Audit

The LLM exists only inside the AI Investigation stage.

---

## 17. Testing Requirements

Test:

1. Investigation retrieves an incident.
2. Investigation retrieves evidence.
3. Investigator produces structured output.
4. Evidence references correspond to actual records.
5. Financial calculations come from deterministic tools.
6. Missing evidence lowers confidence.
7. Contradictory evidence is surfaced.
8. Unsupported root cause is not invented.
9. AI cannot directly mutate financial models.
10. AI cannot execute actions.
11. Invalid tool output is handled safely.
12. Structured output validation rejects malformed responses.

---

## 18. V1 Scope

V1 contains one Investigator.

Do not introduce:

- multiple autonomous agents
- autonomous action execution
- RAG/vector databases
- long-term memory
- autonomous financial authority
- multi-agent loops

Those may be evaluated later.

---

## 19. Design Principle

The AI should answer:

"What happened, why does it appear to have happened,
what evidence supports that conclusion, and what should
the operator investigate or do next?"

The deterministic system remains the authority for:

- financial state
- calculations
- permissions
- policies
- execution
- audit