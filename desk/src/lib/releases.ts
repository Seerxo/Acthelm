// The Releases page's rows (pages/releases.tsx), from data the Desk already has: the API's runtime facts and the code
// missions in /desk/state improvements (lib/delivery.ts). The release gateway's ledger is root-only and not in this
// read model, so nothing here claims a signature, a candidate or a deploy. A PR number is shown only when the backend
// verified the publication (pr_verified === true); otherwise the row says "unverified" without a number.
import type { Improvement, Runtime, Tone } from "./api.ts"
import { deliveryView } from "./delivery.ts"

const MERGED = ["MERGED", "VERIFYING", "MEASURED"]

export interface ReleaseRow { task: string; title: string; pr: string; verified: boolean; merged: boolean; stage: string; tone: Tone
  evidence: string[]; at: string | null }

/** Missions that reached a pull request, merged first, newest first. `undefined` (not reported) stays undefined. */
export function releaseRows(items: Improvement[] | undefined): ReleaseRow[] | undefined {
  if (items === undefined) return undefined
  return items.filter(i => i.pr_number != null).map(i => {
    const v = deliveryView(i), verified = i.pr_verified === true
    return { task: i.task, title: i.title, pr: verified ? `#${i.pr_number}` : "unverified", verified, merged: MERGED.includes(i.stage),
      stage: v.label, tone: v.tone, evidence: v.evidence.filter(e => !e.startsWith("PR ")), at: i.updated_at }
  }).sort((a, b) => Number(b.merged) - Number(a.merged) || (b.at ?? "").localeCompare(a.at ?? ""))
}

export const sha7 = (v: string | null | undefined) => (v && /^[0-9a-f]{7,40}$/.test(v) ? v.slice(0, 7) : null)

/** The running version as reported; every missing fact reads "unknown". */
export function runningVersion(r: Runtime | undefined) {
  if (!r) return null
  const api = sha7(r.api.git_sha), worker = sha7(r.worker.git_sha)
  return { api: api ?? "unknown", worker: worker ?? "unknown", sameCommit: !!api && api === worker,
    schema: r.migration.database ?? "unknown", codeHead: r.migration.code_head ?? "unknown",
    schemaOk: !!r.migration.database && r.migration.database === r.migration.code_head, drift: r.drift, startedAt: r.api.started_at,
    tone: (r.drift.length ? "danger" : api ? "success" : "warning") as Tone }
}
