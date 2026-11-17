// B5 Desk -> Stage C handoff, as pure functions for pages/handoff.tsx (app/stage_c_handoff.py is the authority).
// The page records a code task (POST /stage-c/tasks), shows its approval state (GET /tasks/{key}) and lets only the
// verified founder release it (POST /tasks/{key}/handoff). The API re-checks everything; this only refuses early.
import type { Tone } from "./api.ts"

/** app/stage_c_handoff.py desk_view: the handoff as production stored it, plus the last authenticated Stage C report. */
export interface HandoffView {
  id: string; payload_sha256: string; delivery: string | null; remote_task_key: string | null
  released_at: string | null; expires_at: string | null; last_error_code: string | null
  cancellation: string; freshness: "FRESH" | "UNKNOWN"
  snapshot: { authorization?: { status?: string; expires_at?: string | null } | null } | null
}
/** GET /desk/handoffs (founder only): `enabled` is the sender's fail-closed configuration. */
export interface HandoffList { enabled: boolean; handoffs: { key: string; title: string; status: string; handoff: HandoffView }[] }

/** stage_c_handoff.RELEASABLE */
export const RELEASABLE = ["BACKLOG", "READY", "BLOCKED"] as const

/** Why the founder cannot release now (the button stays off and says so), or null when the API would accept it.
 *  `founder`: GET /desk/handoffs answered (it is founder only); null while unknown. A SUSPENDED or REFUSED delivery
 *  may be retried with the same text (the API restarts it; different text is RELEASE_CONFLICT). */
export function releaseBlock(o: {
  founder: boolean | null; enabled: boolean; status: string | null; risk?: string; handoff: HandoffView | null; bodyError: string | null
}): string | null {
  if (o.founder === null) return "Checking who is signed in…"
  if (!o.founder) return "Only the verified founder may release a task to Stage C."
  if (!o.enabled) return "The Stage C handoff is off on this host, so nothing can be sent."
  if (o.status === null) return "Record the task first."
  if (o.handoff) {
    if (o.handoff.cancellation !== "NONE") return "This handoff was cancelled."
    if (o.handoff.delivery === "SUSPENDED" || o.handoff.delivery === "REFUSED") return o.bodyError
    return "Already released."
  }
  // Stage C imports every packet as LOW risk and the release route does not check it, so the screen does.
  if (o.risk !== undefined && o.risk !== "LOW") return `A ${o.risk}-risk task cannot be sent to Stage C (LOW only).`
  if (!(RELEASABLE as readonly string[]).includes(o.status)) return `A ${o.status} task cannot be released (BACKLOG, READY or BLOCKED only).`
  return o.bodyError
}

/** The founder's release (production) and the one-time coding approval (Stage C's own Access page), each as a label.
 *  The coding approval is what Stage C last reported; without a fresh report it is unknown, never "authorized". */
export function approvalState(h: HandoffView | null): { release: [string, Tone]; coding: [string, Tone] } {
  if (!h) return { release: ["Not released", "neutral"], coding: ["Not requested: release first", "neutral"] }
  const release: [string, Tone] = h.cancellation !== "NONE" ? [`Cancelled (${h.cancellation.toLowerCase()})`, "warning"]
    : h.delivery === "DELIVERED" ? ["Released · delivered to Stage C", "success"]
    : h.delivery === "PENDING" ? ["Released · delivery pending", "info"]
    : [`Released · delivery ${(h.delivery ?? "unknown").toLowerCase()}${h.last_error_code ? ` (${h.last_error_code})` : ""}`, "danger"]
  const grant = h.snapshot?.authorization?.status
  const coding: [string, Tone] = h.freshness !== "FRESH" || !grant ? ["Unknown: no fresh Stage C report", "warning"]
    : grant === "ACTIVE" ? ["Approved on Stage C", "success"]
    : grant === "CONSUMED" ? ["Approved and used", "success"]
    : grant === "NONE" ? ["Waiting for your approval on Stage C", "warning"]
    : [`${grant[0]}${grant.slice(1).toLowerCase()}: approve again on Stage C`, "danger"]
  return { release, coding }
}
