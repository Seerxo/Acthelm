// When the Desk offers "Mark READY" on a task (POST /tasks/{key}/status, verified founder only). READY hands the task to
// the worker, so it is never offered while work may still be running or once it is finished: not for IN_PROGRESS,
// REVIEW, DONE, CANCELLED, not for READY itself, and not while any agent run is still STARTED. The API checks the same
// and, for code-writing tasks, every Stage C condition (worker.ready_refusal); its refusal is what the founder sees.
import type { TaskDetail } from "./api"

const FROM = ["BACKLOG", "IDEA", "BLOCKED", "FAILED", "WAITING_EXTERNAL"]

export function canOfferReady(t: Pick<TaskDetail, "status" | "runs">): boolean {
  return FROM.includes(t.status) && !t.runs.some(r => r.status === "STARTED")
}

/** What READY will not change, said before the founder confirms: the worker skips a task whose attempts are used up
 *  or that is not opted in to unattended execution, even once it is READY. */
export function readyNote(t: Pick<TaskDetail, "context"> & { retry_count?: number; max_attempts?: number }): string | null {
  const ctx = (t.context as { autonomous?: unknown } | null) ?? {}
  if (typeof t.retry_count === "number" && typeof t.max_attempts === "number" && t.retry_count >= t.max_attempts)
    return `All ${t.max_attempts} attempts are used; the worker will not retry it on its own.`
  if (ctx.autonomous !== true) return "Not opted in to unattended work: READY makes it available to an attended run, not the worker."
  return null
}

export const isCodeWrite = (t: Pick<TaskDetail, "context">) => (t.context as { write?: unknown } | null)?.write === true

/** Why Mark READY waits on a code task: Authorize Stage C comes first. `stage_c_authorization` is what the API's READY
 *  check will say (null = an authorization covers it); absent on an older API, where the API's refusal still applies. */
export function readyBlock(t: Pick<TaskDetail, "context" | "stage_c_authorization">): string | null {
  const why = t.stage_c_authorization
  return isCodeWrite(t) && typeof why === "string"
    ? `Authorize Stage C first: no valid authorization covers this task yet (${why}).` : null
}

/** The agent sheet's "Check attended task plans" (POST /dispatch/run): "paused", or this agent's next commands.
 *  A response without that shape throws, so the caller shows an error instead of an empty plan list. */
export function attendedPlans(out: unknown, agent: string): string[] | "paused" {
  const o = out as { paused?: unknown; work?: unknown } | null
  if (!o || typeof o.paused !== "boolean" || !Array.isArray(o.work)) throw new Error("Unexpected /dispatch/run response")
  if (o.paused) return "paused"
  return (o.work as { agent?: unknown; next_command?: unknown }[])
    .filter(w => w && w.agent === agent && typeof w.next_command === "string").map(w => w.next_command as string)
}
