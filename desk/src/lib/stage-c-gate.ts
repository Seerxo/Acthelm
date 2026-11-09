// Is Stage C present on this host? The same Desk runs on production and on the Stage C host, and GET /stage-c/state
// answers on both (same code). Only positive evidence counts (same rule as QA's stageCOnHost): a recorded host
// acceptance, a Stage C task, a grant record (an unreadable grant counts: its existence is unknown, so the revoke must
// stay reachable), or the code-write flag on in the runtime. One deliberate difference: a preflight counts only when it
// PASSes. The watchdog records a preflight on every host on every tick, flag on or off (app/watchdog.py
// record_stage_c_readiness), so a merely recorded preflight is present on production too and proves nothing.
// No answer, an error or a non-object hides Stage C navigation; nothing here throws.
import type { DeskState } from "./api"
import { parseStageC } from "./stage-c.ts"

export function stageCPresent(raw: unknown, desk?: Pick<DeskState, "runtime"> | null): boolean {
  if (desk?.runtime?.stage_c_enabled === true) return true
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return false
  try {
    const s = parseStageC(raw), rt = s.runtime, a = s.authorizations
    return !!rt.acceptance.accepted_at || !!rt.acceptance.source_sha || rt.preflight.status === "PASS"
      || (s.tasks?.length ?? 0) > 0 || a.one_task !== null || a.standing !== null
  } catch { return false }
}
