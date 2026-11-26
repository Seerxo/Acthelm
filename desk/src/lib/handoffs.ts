// B5 Desk -> Stage C handoffs as production reports them: GET /desk/handoffs (app/stage_c_handoff.py desk_list) and
// GET /tasks/{key}.remote_stage_c (desk_view). Everything Stage C said is "Stage C reported", never a production fact.
// The production task of a released handoff stays WAITING_EXTERNAL, so the lane comes from the Stage C report, and
// without a fresh report (the API's `freshness`, STALE_AFTER 180s) the lane is UNKNOWN, never the last good value.
// MERGED, VERIFIED and DONE are three facts from three places (app/stage_c_handoff.py): MERGED is Stage C's report of
// the founder's merge (with main CI on the merge commit), VERIFIED is production's own decision (main CI green AND
// production itself runs the merge commit), DONE is the founder's close of the production task. Never conflated.
// Pure functions, parsed defensively: anything missing or unrecognised is null / UNKNOWN. `npm test`.
import type { Tone } from "./api.ts"
import { ciView, parseStageC, reviewView, testView, type Task } from "./stage-c.ts"

export const LANES = ["BACKLOG", "READY", "RUNNING", "REVIEW", "COMPLETED", "BLOCKED", "UNKNOWN"] as const
export type Lane = (typeof LANES)[number]
export const LANE_LABEL: Record<Lane, string> = {
  BACKLOG: "Backlog", READY: "Ready", RUNNING: "Running", REVIEW: "Review", COMPLETED: "Completed", BLOCKED: "Blocked",
  UNKNOWN: "Unknown",
}
export const LANE_TONE: Record<Lane, Tone> = {
  BACKLOG: "neutral", READY: "neutral", RUNNING: "info", REVIEW: "warning", COMPLETED: "success", BLOCKED: "danger",
  UNKNOWN: "warning",
}

export interface Handoff {
  /** The production task (desk_list row); null when read from a task detail that already names it. */
  key: string | null; title: string | null; status: string | null
  id: string | null
  delivery: "PENDING" | "DELIVERED" | "SUSPENDED" | "REFUSED" | "UNKNOWN"
  remote_task_key: string | null
  released_at: string | null; expires_at: string | null; last_success_at: string | null; observed_at: string | null
  last_error_code: string | null
  cancellation: "NONE" | "PENDING" | "ACKNOWLEDGED" | "UNKNOWN"
  freshness: "FRESH" | "UNKNOWN"
  seq: number | null
  /** When Stage C produced the newest accepted report. */
  generated_at: string | null
  /** The Stage C task as reported (lib/stage-c.ts Task); null = not reported yet. */
  task: Task | null
  authorization: string | null
  write_enabled: boolean | null
  /** Stage C reported: the founder's merge, and main CI on that merge commit (null unless bound to it). */
  merge: { state: "NOT_OBSERVED" | "MERGED" | "UNKNOWN"; commit: string | null; at: string | null
           main_ci: "SUCCESS" | "FAILURE" | "PENDING" | null }
  /** Production's own decision (never Stage C's): VERIFIED only with green main CI and production running the commit. */
  verification: { state: "VERIFIED" | "NOT_VERIFIED" | "UNKNOWN"; reason: string | null; production_api_sha: string | null }
}

export type Delivery = "MERGED" | "VERIFIED" | "DONE"
export const DELIVERY_LABEL: Record<Delivery, string> = {
  MERGED: "Merged · Stage C reported", VERIFIED: "Verified · production", DONE: "Done · founder",
}
/** How far the change got, each step from its own source: DONE only from the production task status, VERIFIED only from
 *  production's verification of a fresh report, MERGED only from Stage C's report. null before the founder's merge. */
export function deliveryOf(h: Handoff): Delivery | null {
  if (h.status === "DONE") return "DONE"
  if (h.verification.state === "VERIFIED" && h.freshness === "FRESH") return "VERIFIED"
  return h.merge.state === "MERGED" ? "MERGED" : null
}

type Obj = Record<string, unknown>
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? v as Obj : {})
const str = (v: unknown, max = 200): string | null => (typeof v === "string" && v.length > 0 && v.length <= max ? v : null)
const ts = (v: unknown): string | null => (typeof v === "string" && v.length <= 40 && !Number.isNaN(Date.parse(v)) ? v : null)
const code = (v: unknown): string | null => (typeof v === "string" && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(v) ? v : null)
const key = (v: unknown): string | null => (typeof v === "string" && /^[A-Z]+-\d{1,6}$/.test(v) ? v : null)
const sha = (v: unknown): string | null => (typeof v === "string" && /^[a-f0-9]{40}$/.test(v) ? v : null)
const oneOf = <T extends string>(v: unknown, allowed: readonly T[]): T | "UNKNOWN" =>
  typeof v === "string" && (allowed as readonly string[]).includes(v) ? v as T : "UNKNOWN"

/** One desk_view dict (`row` = the desk_list row around it). null when there is no handoff. */
export function parseHandoff(view: unknown, row: unknown = null): Handoff | null {
  if (!view || typeof view !== "object" || Array.isArray(view)) return null
  const h = obj(view), r = obj(row), snap = obj(h.snapshot), m = obj(snap.merge), v = obj(h.verification)
  const ci = obj(m.main_ci), commit = sha(m.commit)
  // ponytail: the snapshot task has the /stage-c/state task shape, so the Stage C page's own parser reads it.
  const task = snap.task && typeof snap.task === "object"
    ? parseStageC({ tasks: [{ ...obj(snap.task), key: snap.task_key }] }).tasks![0] : null
  const seq = typeof h.seq === "number" && Number.isSafeInteger(h.seq) && h.seq >= 0 ? h.seq : null
  return {
    key: key(r.key), title: str(r.title, 500), status: code(r.status),
    id: str(h.id, 64), delivery: oneOf(h.delivery, ["PENDING", "DELIVERED", "SUSPENDED", "REFUSED"] as const),
    remote_task_key: key(h.remote_task_key),
    released_at: ts(h.released_at), expires_at: ts(h.expires_at), last_success_at: ts(h.last_success_at),
    observed_at: ts(h.observed_at), last_error_code: code(h.last_error_code),
    cancellation: oneOf(h.cancellation, ["NONE", "PENDING", "ACKNOWLEDGED"] as const),
    freshness: h.freshness === "FRESH" ? "FRESH" : "UNKNOWN",
    seq, generated_at: ts(snap.generated_at), task,
    authorization: code(obj(snap.authorization).status),
    write_enabled: typeof obj(snap.runtime).write_enabled === "boolean" ? obj(snap.runtime).write_enabled as boolean : null,
    merge: { state: h.snapshot ? oneOf(m.state, ["NOT_OBSERVED", "MERGED"] as const) : "UNKNOWN", commit, at: ts(m.at),
             main_ci: commit && sha(ci.sha) === commit && ["SUCCESS", "FAILURE", "PENDING"].includes(ci.state as string)
               ? ci.state as "SUCCESS" | "FAILURE" | "PENDING" : null },
    verification: { state: oneOf(v.state, ["VERIFIED", "NOT_VERIFIED"] as const), reason: str(v.reason, 300),
                    production_api_sha: sha(v.production_api_sha) },
  }
}

/** GET /desk/handoffs. `enabled` null = not reported. */
export function parseHandoffs(raw: unknown): { enabled: boolean | null; handoffs: Handoff[] } {
  const r = obj(raw)
  return {
    enabled: typeof r.enabled === "boolean" ? r.enabled : null,
    handoffs: Array.isArray(r.handoffs) ? r.handoffs.map(x => parseHandoff(obj(x).handoff, x)).filter((h): h is Handoff => h !== null) : [],
  }
}

const STATUS_LANE: Record<string, Lane> = {
  IDEA: "BACKLOG", BACKLOG: "BACKLOG", READY: "READY", QUEUED: "READY", IN_PROGRESS: "RUNNING", REVIEW: "REVIEW",
  DONE: "COMPLETED",
}

/** The founder's lane. FAILED, BLOCKED, WAITING_*, CANCELLED and anything unrecognised are BLOCKED (lib/task-story.ts
 *  groupOf: never filed as healthy). A stale or missing report is UNKNOWN unless delivery itself explains it. */
/** The API's freshness is a snapshot of the moment it answered; the browser re-derives it from the report's own
 *  generation time, so a page whose refreshes stopped never keeps calling an old report fresh. */
export const FRESH_MS = 180_000
export function liveFreshness(server: string | null | undefined, generatedAt: string | null | undefined,
                              now: number): "FRESH" | "UNKNOWN" {
  const at = generatedAt ? Date.parse(generatedAt) : NaN
  return server === "FRESH" && Number.isFinite(at) && now - at <= FRESH_MS ? "FRESH" : "UNKNOWN"
}
export const live = (h: Handoff, now: number): Handoff => {
  const freshness = liveFreshness(h.freshness, h.generated_at, now)
  // Production's VERIFIED rests on a fresh report: once the report ages out, so does the verification.
  return { ...h, freshness, verification: freshness === "FRESH" ? h.verification
    : { ...h.verification, state: "UNKNOWN", reason: "no fresh Stage C report" } }
}

export function laneOf(h: Handoff): Lane {
  if (h.cancellation === "ACKNOWLEDGED" || h.delivery === "SUSPENDED" || h.delivery === "REFUSED") return "BLOCKED"
  if (!h.task) return h.delivery === "PENDING" ? "BACKLOG" : "UNKNOWN"
  if (h.freshness !== "FRESH") return "UNKNOWN"
  return STATUS_LANE[h.task.status] ?? "BLOCKED"
}

const words = (c: string) => c.toLowerCase().replaceAll("_", " ")

/** Why the handoff is not moving, from recorded fields only; null while it is running or done. */
export function waitingReason(h: Handoff): string | null {
  if (h.cancellation === "ACKNOWLEDGED") return "Cancelled; Stage C acknowledged the stop"
  if (h.cancellation === "PENDING") return "Cancel requested; Stage C has not acknowledged the stop yet"
  if (h.delivery === "SUSPENDED" || h.delivery === "REFUSED")
    return `Delivery ${words(h.delivery)} (${h.last_error_code ?? "no error code recorded"}); waits for your retry`
  if (h.delivery === "PENDING")
    return h.last_error_code ? `Not delivered yet; last attempt: ${h.last_error_code}` : "Released; waiting for delivery to Stage C"
  if (!h.task) return "Delivered; Stage C has not reported this task yet"
  if (h.freshness !== "FRESH")
    return `No Stage C report in the last 3 minutes${h.last_error_code ? ` (last poll: ${h.last_error_code})` : ""}; state unknown`
  const t = h.task
  if (t.needs_founder) return "Stage C reports it needs you"
  switch (t.status) {
    case "BLOCKED": case "FAILED":
      return `${t.status === "FAILED" ? "Failed" : "Blocked"}: ${t.failure_category ? words(t.failure_category) : "no failure category reported"}`
    case "CANCELLED": return "Cancelled on Stage C"
    case "WAITING_APPROVAL": return "Waiting for an approval on Stage C"
    case "WAITING_EXTERNAL": return "Waiting for evidence from outside Stage C"
    case "IDEA": case "BACKLOG": return "In the Stage C backlog; not scheduled"
    case "READY": case "QUEUED":
      if (h.authorization !== "ACTIVE") return `Waiting for a Stage C authorization (grant ${h.authorization ? words(h.authorization) : "not reported"})`
      if (h.write_enabled === false) return "Waiting: Stage C code write is off"
      return "Queued on Stage C; not started yet"
    case "REVIEW": {
      const ci = ciView(t)
      if (ci.kind === "failed") return "CI failed on the PR"
      if (reviewView(t).badge === "FAILED") return "The independent reviewer rejected it"
      if (t.reviewer_verdict === null) return "Waiting for the independent review"
      if (t.test_verdict !== "PASSED") return `Tests: ${testView(t).label}`
      if (reviewView(t).badge !== "READY") return `Independent review: ${reviewView(t).label}`
      if (ci.kind === "running") return "Waiting for CI on the PR"
      if (ci.kind !== "passed") return `CI: ${ci.label}`
      if (h.merge.state !== "MERGED") return "Ready to merge; waiting for the founder's merge"
      if (h.merge.main_ci === "FAILURE") return "Merged; main CI failed on the merge commit"
      if (h.verification.state === "VERIFIED") return "Verified in production; waiting for your DONE"
      return `Merged; not verified in production${h.verification.reason ? `: ${h.verification.reason}` : ""}`
    }
    case "IN_PROGRESS": case "DONE": return null
    default: return `Unrecognised Stage C status ${t.status}`
  }
}

/** Handoffs grouped by lane, newest report first inside a lane. Empty lanes are kept so the board keeps its shape. */
export function board(hs: Handoff[]): { lane: Lane; items: Handoff[] }[] {
  const at = (h: Handoff) => Date.parse(h.generated_at ?? h.released_at ?? "") || 0
  return LANES.map(lane => ({ lane, items: hs.filter(h => laneOf(h) === lane).sort((a, b) => at(b) - at(a)) }))
}
