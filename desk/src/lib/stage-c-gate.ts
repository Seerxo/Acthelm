// Is Stage C present on this host? The same Desk runs on production and on the Stage C host, and GET /stage-c/state
// answers on both (same code). Only the host's CURRENT capability counts (stageCEvidence): a passing watchdog
// preflight, a host acceptance for the running commit, a live or unreadable grant, or the code-write flag on.
// History (old acceptances, finished tasks, settled grants) never shows Stage C. No answer, an error or a non-object
// hides Stage C; nothing here throws.
import type { DeskState } from "./api"
import { parseStageC } from "./stage-c.ts"

export function stageCPresent(raw: unknown, desk?: Pick<DeskState, "runtime"> | null): boolean {
  if (desk?.runtime?.stage_c_enabled === true) return true
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return false
  try { return stageCEvidence(parseStageC(raw)) } catch { return false }
}

/** The one evidence rule, on an already parsed /stage-c/state. Every page gates on this (stage-c, Home, nav). */
// Founder rule (2026-11-09): only the host's CURRENT capability counts, never history. Old rows (a past acceptance
// for another commit, finished tasks, consumed/expired/revoked grants) stay in a database after Stage C is gone.
export function stageCEvidence(s: {
  runtime: { api_sha: string | null; write_enabled: boolean | null; acceptance: { source_sha: string | null }; preflight: { status: string } }
  authorizations: { one_task?: { status: string } | null; standing?: { status: string } | null }
}): boolean {
  const rt = s.runtime, a = s.authorizations
  // A live grant (or one that could not be read) keeps Revoke reachable; any settled grant is history.
  const live = (g: { status: string } | null | undefined) =>
    g === undefined || (g !== null && (g.status === "ACTIVE" || g.status === "UNKNOWN"))
  return rt.write_enabled === true || rt.preflight.status === "PASS"      // current flag or passing watchdog check
    || (!!rt.acceptance.source_sha && rt.acceptance.source_sha === rt.api_sha)   // accepted for the running code
    || live(a.one_task) || live(a.standing)
}
