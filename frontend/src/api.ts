const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export type IncidentSeverity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'
export type IncidentStatus = 'OPEN' | 'INVESTIGATING' | 'ACTION_REQUIRED' | 'RESOLVING' | 'RESOLVED' | 'DISMISSED'
export type PaymentStatus = 'CREATED' | 'AUTHORIZED' | 'CAPTURED' | 'FAILED' | 'REFUND_PENDING' | 'REFUNDED'

export interface IncidentSummary { id: string; incident_code: string; incident_type: string; severity: IncidentSeverity; status: IncidentStatus; title: string; financial_exposure: number | null; currency: string | null; detected_at: string }
export interface Incident extends IncidentSummary { description: string | null; resolved_at: string | null; evidence_count: number }
export interface Evidence { id: string; evidence_type: string; entity_type: string; entity_id: string; relationship: string; created_at: string }
export interface Payment { id: string; provider_payment_id: string; provider_order_id: string | null; amount: number; currency: string; status: PaymentStatus; created_at: string; authorized_at: string | null; captured_at: string | null; failed_at: string | null; updated_at: string }
export interface Transition { id: string; from_state: PaymentStatus | null; to_state: PaymentStatus; financial_event_id: string; occurred_at: string; created_at: string }
export interface PaymentJourney { payment_id: string; transitions: Transition[] }
export interface Investigation { incident_id: string; root_cause: string; summary: string; observed_facts: string[]; derived_findings: string[]; evidence: { entity_type: string; entity_id: string; relationship: string }[]; financial_impact_minor: number; unresolved_amount_minor: number; recommended_action: string; confidence: number; uncertainties: string[] }
export interface PolicyDecision { outcome: string; reason: string; policy_id: string | null; evaluated_conditions: Record<string, unknown> }
export interface Approval { id: string; requested_by: string; approved_by: string | null; status: string; reason: string | null; requested_at: string; approved_at: string | null }
export interface Execution { id: string; execution_id: string; status: string; provider_reference: string | null; error: string | null; executed_at: string | null; verified_at: string | null }
export interface VerificationResult { outcome: string; passed: boolean; reason: string }
export interface ExecutionCommandResult { success: boolean; reason: string; execution: Execution | null; verification: VerificationResult | null; action: Action | null }
export interface Action { id: string; incident_id: string; action_type: string; description: string; amount: number | null; currency: string | null; confidence: number | null; requires_approval: boolean; status: string; created_by: string; created_at: string; updated_at: string; policy_decision: PolicyDecision | null; approvals: Approval[]; executions: Execution[] }
export interface AuditEvent { id: string; actor_type: string; actor_id: string | null; action_type: string; entity_type: string; entity_id: string | null; reason: string | null; evidence: Record<string, unknown> | null; metadata: Record<string, unknown> | null; created_at: string }
export interface Dashboard { payment_health: Record<string, number>; open_incident_count: number; critical_incident_count: number; financial_exposure: number; unexplained_money: number; auto_approved_action_count: number; auto_approvable_action_count: number; approval_required_action_count: number; recent_critical_incidents: IncidentSummary[] }

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail ?? `Request failed with status ${response.status}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  dashboard: () => request<Dashboard>('/dashboard/summary'),
  incidents: (filters?: { status?: string; severity?: string; incident_type?: string }) => request<IncidentSummary[]>(`/incidents?${new URLSearchParams(filters).toString()}`),
  incident: (id: string) => request<Incident>(`/incidents/${id}`),
  evidence: (id: string) => request<Evidence[]>(`/incidents/${id}/evidence`),
  investigate: (id: string) => request<Investigation>(`/investigations/${id}`, { method: 'POST' }),
  createActionFromInvestigation: (id: string, actionType?: string) => request<{ id: string; incident_id: string; action_type: string; description: string; amount: number | null; currency: string | null; confidence: number | null; requires_approval: boolean; status: string; created_by: string; created_at: string; updated_at: string; policy_decision: PolicyDecision; approval_id: string | null }>(`/investigations/${id}/action`, { method: 'POST', body: JSON.stringify({ action_type: actionType ?? null }) }),
  payment: (id: string) => request<Payment>(`/payments/${id}`),
  journey: (id: string) => request<PaymentJourney>(`/payments/${id}/journey`),
  actions: () => request<Action[]>('/actions'),
  action: (id: string) => request<Action>(`/actions/${id}`),
  approve: (id: string, actor_id: string) => request<Approval>(`/actions/${id}/approve`, { method: 'POST', body: JSON.stringify({ actor_id }) }),
  reject: (id: string, actor_id: string, reason: string) => request<Approval>(`/actions/${id}/reject`, { method: 'POST', body: JSON.stringify({ actor_id, reason }) }),
  execute: (id: string) => request<ExecutionCommandResult>(`/actions/${id}/execute`, { method: 'POST' }),
  verify: (id: string) => request<ExecutionCommandResult>(`/actions/${id}/verify`, { method: 'POST' }),
  audit: (entityType: string, entityId: string) => request<AuditEvent[]>(`/audit/${entityType}/${entityId}`),
}