// A Stage C task stays REVIEW after its PR is merged (app/stage_c_ci.py): the founder still accepts the result. Showing
// that as "Review" hides the merge, so the Desk names it from the lifecycle stage instead, and never as done.
import type { Tone } from "./api.ts"

const AFTER_MERGE = ["MERGED", "VERIFYING", "MEASURED"]

export const mergedAwaitingAcceptance = (status: string | null | undefined, stage: string | null | undefined) =>
  status === "REVIEW" && !!stage && AFTER_MERGE.includes(stage)

/** The status chip for a task with a known lifecycle stage. `label`/`tone` undefined = the status speaks for itself. */
export function taskStatusView(status: string | null | undefined, stage: string | null | undefined): { value: string; label?: string; tone?: Tone } {
  if (mergedAwaitingAcceptance(status, stage)) return { value: "MERGED", label: "Merged · awaiting acceptance", tone: "info" }
  return { value: status ?? "UNKNOWN" }
}

/** Does this host run Stage C? Only positive evidence from GET /stage-c/state counts: a host acceptance, a preflight,
 *  a Stage C task, or a grant record. Production has none of these, so its Stage C controls hide instead of breaking.
 *  An unreadable grant (undefined) counts as present: its existence is unknown, so the revoke must stay reachable. */
export function stageCOnHost(s: {
  runtime: { acceptance: { accepted_at: string | null; source_sha: string | null }; preflight: { checked_at: string | null } }
  authorizations: { one_task: unknown; standing: unknown }; tasks: unknown[] | null
}): boolean {
  const a = s.authorizations
  return !!s.runtime.acceptance.accepted_at || !!s.runtime.acceptance.source_sha || !!s.runtime.preflight.checked_at
    || (s.tasks?.length ?? 0) > 0 || a.one_task !== null || a.standing !== null
}
