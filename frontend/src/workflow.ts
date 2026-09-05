import type { Action, Incident } from './api'

/**
 * persisted workflow progress for an incident.
 *
 * The incident detail page previously kept the investigation result only in
 * React state, so navigating away and back reset the workflow UI. Workflow
 * stages are instead derived here from persisted state that survives refresh:
 * the action proposal (`GET /actions`, newest-first), its approvals and
 * executions, and the incident status.
 */
export const WORKFLOW_STEPS = [
  'OBSERVE',
  'DETECT',
  'INVESTIGATE',
  'DECIDE',
  'APPROVE',
  'EXECUTE',
  'VERIFY',
  'RESOLVE',
] as const

export type WorkflowStep = (typeof WORKFLOW_STEPS)[number]

export interface WorkflowProgress {
  /** latest persisted proposal for the incident, or null */
  proposal: Action | null
  hasProposal: boolean
  approved: boolean
  executed: boolean
  verified: boolean
  resolved: boolean
  /** index of the farthest complete rail step (0..7) */
  completed: number
  /** the step the operator is currently on (first incomplete) */
  active: WorkflowStep
  /** number of completed decision-trail steps (0..8) */
  trailDone: number
}

// A proposal in any of these statuses reached at least APPROVED in the backend
// (execution only starts after an approval), so the approval stage is done.
const APPROVED_OR_LATER = new Set(['APPROVED', 'EXECUTING', 'EXECUTED', 'FAILED'])

export function workflowProgress(
  actions: Action[],
  incident: Incident,
  freshInvestigation = false,
): WorkflowProgress {
  // /actions returns proposals newest-first (ActionProposal.created_at DESC).
  const proposal = actions.length ? actions[0] : null
  const hasProposal = proposal !== null
  const approved =
    hasProposal &&
    (proposal.approvals.some((approval) => approval.status === 'APPROVED') ||
      APPROVED_OR_LATER.has(proposal.status))
  const executed = hasProposal && proposal.executions.some((execution) => execution.status === 'EXECUTED')
  const verified = hasProposal && proposal.executions.some((execution) => execution.verified_at != null)
  const resolved = incident.status === 'RESOLVED'
  const investigateDone = hasProposal || freshInvestigation

  // OBSERVE and DETECT are inherently done for a persisted incident.
  const railDone = [true, true, investigateDone, hasProposal, approved, executed, verified, resolved]
  if (resolved) {
    // A resolved incident necessarily passed Action, Policy, Approval,
    // Execution, and Verification first.
    for (let i = 3; i <= 6; i += 1) railDone[i] = true
  }

  let completed = 0
  for (let i = 0; i < railDone.length; i += 1) {
    if (railDone[i]) completed = i
    else break
  }
  const active = WORKFLOW_STEPS[Math.min(completed + 1, WORKFLOW_STEPS.length - 1)]

  const trailDone = resolved
    ? 8
    : 1 + // Incident
      (incident.evidence_count > 0 ? 1 : 0) + // Evidence
      (investigateDone ? 1 : 0) + // Investigation
      (hasProposal ? 2 : 0) + // Action, Policy
      (executed ? 1 : 0) + // Execution
      (verified ? 1 : 0) // Verification

  return { proposal, hasProposal, approved, executed, verified, resolved, completed, active, trailDone }
}