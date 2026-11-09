// Stage C read model for the Founder Desk: GET /stage-c/state (app/stage_c_state.py, founder only). The backend owns
// every lifecycle decision (`state`, `blockers`, grant `status`, CI STALE, reviewer UNVERIFIED); this file only parses
// it defensively and turns it into display values. Anything missing, wrong-typed or unrecognised becomes UNKNOWN or
// NOT_VERIFIED, never success; a missing list is null ("not reported"), never an empty one. Pure functions, no
// imports beyond ApiError, so `npm test` runs them without a browser.
import { ApiError } from "./api.ts"
import type { Tone } from "./api.ts"

export const CONTRACT_VERSION = 1
export const BADGES = ["ACTIVE", "READY", "BLOCKED", "FAILED", "EXPIRED", "UNKNOWN", "NOT_VERIFIED"] as const
export type Badge = (typeof BADGES)[number]
export const PREFLIGHT = ["PASS", "FAIL", "STALE", "NOT_VERIFIED"] as const
export const GRANT = ["ACTIVE", "CONSUMED", "EXPIRED", "DAILY_LIMIT", "INVALID", "REVOKED", "SHA_MISMATCH"] as const
export const CI = ["SUCCESS", "FAILURE", "PENDING", "STALE", "MERGED_OBSERVED"] as const
export const TASK_STATUS = ["IDEA", "BACKLOG", "READY", "QUEUED", "IN_PROGRESS", "WAITING_EXTERNAL", "WAITING_APPROVAL", "BLOCKED", "REVIEW", "FAILED", "DONE", "CANCELLED"] as const
const NEXT_ACTION = ["check_deploy", "check_worker", "redeploy_one_sha", "resume_agents", "accept_host", "enable_code_write", "run_preflight", "review_task", "wait_daily_reset", "issue_authorization", "none"] as const
export const HEARTBEAT = ["HEALTHY", "STALE", "PAUSED", "DEGRADED", "NO_DATA"] as const
/** The stages the Desk draws (app/stage_c.py STAGES, from PLANNED through MERGED). */
export const PIPELINE = [
  ["PLANNED", "Planned"], ["IMPLEMENTING", "Implementing"], ["TESTING", "Testing"], ["INDEPENDENT_REVIEW", "Independent review"],
  ["PR_OPEN", "PR open"], ["CI", "CI"], ["READY_TO_MERGE", "Ready to merge"], ["MERGED", "Merged"],
] as const
const KNOWN_STAGES = ["DETECTED", ...PIPELINE.map(([k]) => k), "VERIFYING", "MEASURED", "ABANDONED"]
export const REPO_PR = "https://github.com/Seerxo/Acthelm/pull/"

type U = "UNKNOWN"
export interface Grant {
  status: (typeof GRANT)[number] | U; source_sha: string | null; expires_at: string | null; issuer: "founder" | "operator" | U
}
export interface OneTask extends Grant { task_key: string | null }
export interface Standing extends Grant {
  paths: string[] | null; max_per_day: number | null; used_today: number | null; remaining_today: number | null
  /** true / false as reported; null = not reported (never read as "not revoked"). */
  revoked: boolean | null
}
export interface Task {
  key: string | null; status: (typeof TASK_STATUS)[number] | U; stage: string | U | null
  history: { stage: string | U; at: string | null }[] | null
  attempts: number | null; retry_count: number | null; repairs: number | null
  started_at: string | null; updated_at: string | null
  test_verdict: "PASSED" | "FAILED" | null; reviewer_verdict: "APPROVED" | "UNVERIFIED" | "REJECTED" | U | null
  tested_fingerprint: string | null; reviewed_fingerprint: string | null; fingerprint_match: boolean | null
  pr: { number: number | null; head_sha: string | null } | null
  ci: { state: (typeof CI)[number] | U | null; sha: string | null } | null
  last_outcome: string | null; failure_category: string | null; needs_founder: boolean | null
}
export interface Metrics {
  tasks: { started: number | null; reached_ready_to_merge: number | null; stopped: number | null; in_progress: number | null; abandoned: number | null }
  attempts: { total: number | null; failed: number | null }
  model_calls: number | null; minutes: number | null; tokens: number | null; usd_recorded: number | null
  quota_stops: number | null; truncated: boolean | null
}
export interface StageCState {
  /** false when the version is not the one this Desk was written for: every field is still parsed, nothing trusted more. */
  version_ok: boolean
  generated_at: string | null
  state: Badge
  blockers: string[] | null
  next_action: (typeof NEXT_ACTION)[number] | U
  last_ready_pr: { task_key: string | null; number: number | null; head_sha: string | null; stage: string | U } | null
  runtime: {
    api_sha: string | null; worker_sha: string | null; reasoner_sha: string | null; sha_agree: boolean | null
    worker_heartbeat: (typeof HEARTBEAT)[number] | U; write_enabled: boolean | null; worker_ready: boolean | null
    agents_paused: boolean | null
    acceptance: { source_sha: string | null; accepted_at: string | null; valid: boolean | null }
    preflight: { status: (typeof PREFLIGHT)[number] | U; checked_at: string | null; source_sha: string | null; failed: string[] | null }
    host_facts: "NOT_VERIFIED"
  }
  /** null = explicitly absent; undefined = missing or malformed, existence unknown. */
  authorizations: { one_task: OneTask | null | undefined; standing: Standing | null | undefined }
  tasks: Task[] | null
  metrics: Metrics
  /** The files a new code task may change (POST /stage-c/tasks re-checks); null = not reported. */
  code_task_paths: string[] | null
}

// --------------------------------------------------------------------------- primitives
type Obj = Record<string, unknown>
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? v as Obj : {})
const isObj = (v: unknown): v is Obj => !!v && typeof v === "object" && !Array.isArray(v)
const oneOf = <T extends string>(v: unknown, allowed: readonly T[]): T | U =>
  typeof v === "string" && (allowed as readonly string[]).includes(v) ? v as T : "UNKNOWN"
export const sha = (v: unknown): string | null => (typeof v === "string" && /^[a-f0-9]{40}$/.test(v) ? v : null)
export const fingerprint = (v: unknown): string | null => (typeof v === "string" && /^[a-f0-9]{64}$/.test(v) ? v : null)
const taskKey = (v: unknown): string | null => (typeof v === "string" && /^[A-Z]+-\d{1,6}$/.test(v) ? v : null)
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null)
const count = (v: unknown): number | null => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : null)
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null)
const ts = (v: unknown): string | null => (typeof v === "string" && v.length <= 40 && !Number.isNaN(Date.parse(v)) ? v : null)
/** A fixed backend code (blocker, outcome, failure category): short, upper/lower snake case only. */
const code = (v: unknown): string | null => (typeof v === "string" && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(v) ? v : null)
/** A repository path from the SAFE_FILES list: plain characters only, no scheme, no traversal. */
const path = (v: unknown): string | null =>
  (typeof v === "string" && v.length <= 200 && /^[A-Za-z0-9_./-]+$/.test(v) && !v.includes("..") && !v.includes(":") ? v : null)
const issuer = (v: unknown) => oneOf(v, ["founder", "operator"] as const)

// --------------------------------------------------------------------------- parser
function grant(v: Obj): Grant {
  return { status: oneOf(v.status, GRANT), source_sha: sha(v.source_sha), expires_at: ts(v.expires_at), issuer: issuer(v.issuer) }
}

function task(raw: unknown): Task {
  const t = obj(raw)
  const pr = isObj(t.pr) ? { number: count(t.pr.number) || null, head_sha: sha(t.pr.head_sha) } : null
  const ci = isObj(t.ci) ? { state: t.ci.state === null ? null : oneOf(t.ci.state, CI), sha: sha(t.ci.sha) } : null
  const stage = t.stage === null || t.stage === undefined ? null : oneOf(t.stage, KNOWN_STAGES)
  const tv = t.test_verdict
  const rv = t.reviewer_verdict
  return {
    key: taskKey(t.key), status: oneOf(t.status, TASK_STATUS), stage,
    history: Array.isArray(t.history) ? t.history.map(h => ({ stage: oneOf(obj(h).stage, KNOWN_STAGES), at: ts(obj(h).at) })) : null,
    attempts: count(t.attempts), retry_count: count(t.retry_count), repairs: count(t.repairs),
    started_at: ts(t.started_at), updated_at: ts(t.updated_at),
    test_verdict: tv === "PASSED" || tv === "FAILED" ? tv : null,
    reviewer_verdict: rv === null || rv === undefined ? null : oneOf(rv, ["APPROVED", "UNVERIFIED", "REJECTED"] as const),
    tested_fingerprint: fingerprint(t.tested_fingerprint), reviewed_fingerprint: fingerprint(t.reviewed_fingerprint),
    fingerprint_match: bool(t.fingerprint_match), pr, ci,
    last_outcome: code(t.last_outcome), failure_category: code(t.failure_category), needs_founder: bool(t.needs_founder),
  }
}

export function parseStageC(raw: unknown): StageCState {
  const r = obj(raw)
  const rt = obj(r.runtime), acc = obj(rt.acceptance), pre = obj(rt.preflight)
  const auth = obj(r.authorizations)
  const m = obj(r.metrics), mt = obj(m.tasks), ma = obj(m.attempts)
  const one = isObj(auth.one_task) ? { ...grant(auth.one_task), task_key: taskKey(auth.one_task.task_key) } : auth.one_task === null ? null : undefined
  const s = isObj(auth.standing) ? auth.standing : null
  const standing: Standing | null | undefined = s ? {
    ...grant(s),
    paths: Array.isArray(s.paths) ? s.paths.map(path).filter((p): p is string => p !== null) : null,
    max_per_day: count(s.max_per_day), used_today: count(s.used_today), remaining_today: count(s.remaining_today),
    revoked: bool(s.revoked),
  } : auth.standing === null ? null : undefined
  const calls = isObj(m.model_calls) ? m.model_calls.total : null
  const last = isObj(r.last_ready_pr) ? r.last_ready_pr : null
  return {
    version_ok: r.version === CONTRACT_VERSION,
    generated_at: ts(r.generated_at),
    state: oneOf(r.state, BADGES) as Badge,
    blockers: Array.isArray(r.blockers) ? r.blockers.map(b => code(b) ?? "unrecognised_blocker") : null,
    next_action: oneOf(r.next_action, NEXT_ACTION),
    last_ready_pr: last ? { task_key: taskKey(last.task_key), number: count(last.number) || null, head_sha: sha(last.head_sha),
      stage: oneOf(last.stage, ["READY_TO_MERGE", "MERGED"] as const) } : null,
    runtime: {
      api_sha: sha(rt.api_sha), worker_sha: sha(rt.worker_sha), reasoner_sha: sha(rt.reasoner_sha), sha_agree: bool(rt.sha_agree),
      worker_heartbeat: oneOf(rt.worker_heartbeat, HEARTBEAT), write_enabled: bool(rt.write_enabled),
      worker_ready: bool(rt.worker_ready), agents_paused: bool(rt.agents_paused),
      acceptance: { source_sha: sha(acc.source_sha), accepted_at: ts(acc.accepted_at), valid: bool(acc.valid) },
      preflight: { status: oneOf(pre.status, PREFLIGHT), checked_at: ts(pre.checked_at), source_sha: sha(pre.source_sha),
        failed: Array.isArray(pre.failed) ? pre.failed.map(code).filter((c): c is string => c !== null) : null },
      host_facts: "NOT_VERIFIED",   // the API cannot see host facts; whatever it sends, the Desk does not claim them
    },
    authorizations: { one_task: one, standing },
    tasks: Array.isArray(r.tasks) ? r.tasks.map(task) : null,
    metrics: {
      tasks: { started: count(mt.started), reached_ready_to_merge: count(mt.reached_ready_to_merge), stopped: count(mt.stopped),
        in_progress: count(mt.in_progress), abandoned: count(mt.abandoned) },
      attempts: { total: count(ma.total), failed: count(ma.failed) },
      model_calls: count(calls), minutes: num(m.minutes), tokens: count(m.tokens), usd_recorded: num(m.usd_recorded),
      quota_stops: count(m.quota_stops), truncated: bool(m.truncated),
    },
    code_task_paths: Array.isArray(r.code_task_paths) ? r.code_task_paths.map(path).filter((p): p is string => p !== null) : null,
  }
}

// --------------------------------------------------------------------------- display
export const BADGE_TONE: Record<Badge, Tone> = {
  ACTIVE: "info", READY: "success", BLOCKED: "danger", FAILED: "danger", EXPIRED: "warning", UNKNOWN: "warning", NOT_VERIFIED: "warning",
}
export const BADGE_LABEL: Record<Badge, string> = {
  ACTIVE: "Active", READY: "Ready", BLOCKED: "Blocked", FAILED: "Failed", EXPIRED: "Expired", UNKNOWN: "Unknown", NOT_VERIFIED: "Not verified",
}
export interface View { badge: Badge; label: string }
const v = (badge: Badge, label = BADGE_LABEL[badge]): View => ({ badge, label })

export const short = (s: string | null, n = 7) => (s ? s.slice(0, n) : null)
/** A PR link built only from a positive integer, on the one repository Stage C publishes to. Never from a URL. */
export const prHref = (n: unknown): string | null => (typeof n === "number" && Number.isSafeInteger(n) && n > 0 ? REPO_PR + n : null)
/** null → "n/a" (not recorded), never 0. */
export const na = (n: number | null, fmt: (n: number) => string = String) => (n === null ? "n/a" : fmt(n))

export function preflightView(p: StageCState["runtime"]["preflight"]): View {
  return p.status === "PASS" ? v("READY", "Passed") : p.status === "FAIL" ? v("FAILED")
    : p.status === "STALE" ? v("NOT_VERIFIED", "Stale") : v(p.status === "NOT_VERIFIED" ? "NOT_VERIFIED" : "UNKNOWN")
}
export const acceptanceView = (valid: boolean | null): View =>
  valid === true ? v("READY", "Accepted") : valid === false ? v("BLOCKED", "Not valid") : v("NOT_VERIFIED", "Not recorded")
/** The worker's own readiness. The write flag is a permission, never readiness: it is shown beside this, not instead. */
export const workerReadyView = (ready: boolean | null): View =>
  ready === true ? v("READY") : ready === false ? v("BLOCKED", "Not ready") : v("NOT_VERIFIED")
export const driftView = (rt: Pick<StageCState["runtime"], "api_sha" | "worker_sha" | "sha_agree">): View =>
  !rt.api_sha || !rt.worker_sha ? v("UNKNOWN")
    : rt.api_sha !== rt.worker_sha ? v("BLOCKED", "Drift: API and worker differ")
    : rt.sha_agree === false ? v("BLOCKED", "SHA disagreement reported") : v("NOT_VERIFIED", "API = worker; reasoner not verified")
export const heartbeatView = (h: StageCState["runtime"]["worker_heartbeat"]): View =>
  h === "HEALTHY" ? v("ACTIVE", "Healthy") : h === "PAUSED" ? v("BLOCKED", "Paused") : h === "DEGRADED" ? v("FAILED", "Degraded")
    : h === "STALE" ? v("UNKNOWN", "Stale heartbeat") : h === "NO_DATA" ? v("UNKNOWN", "No heartbeat") : v("UNKNOWN")

export function grantView(g: Grant | null | undefined): View {
  if (g === null) return v("UNKNOWN", "None issued")
  if (g === undefined) return v("UNKNOWN")
  return ({
    ACTIVE: v("ACTIVE"), EXPIRED: v("EXPIRED"), REVOKED: v("BLOCKED", "Revoked"), CONSUMED: v("BLOCKED", "Consumed"),
    DAILY_LIMIT: v("BLOCKED", "Daily limit reached"), SHA_MISMATCH: v("BLOCKED", "Other deploy (SHA mismatch)"),
    INVALID: v("BLOCKED", "Invalid"), UNKNOWN: v("UNKNOWN"),
  } as const)[g.status]
}
/** Hide the emergency action only when absence or revocation is explicit and consistent. */
export const isRevoked = (s: Standing | null | undefined) => !!s && s.status === "REVOKED" && s.revoked === true
export const canRevokeStanding = (s: Standing | null | undefined) => s !== null && !isRevoked(s)
export function standingRevokeMessage(result: unknown): string {
  const revoked = obj(result).revoked
  if (typeof revoked !== "boolean") throw new Error("Invalid standing revoke response")
  return revoked ? "Standing authorization revoked" : "No live standing grant — nothing was revoked"
}

export type CiKind = "passed" | "failed" | "running" | "outdated" | "observed" | "none" | "unknown"
/** CI is success only when it ran on the PR's published head. Any disagreement reads "CI on outdated SHA". */
export function ciView(t: Pick<Task, "pr" | "ci">): { kind: CiKind; label: string; tone: Tone; sha: string | null } {
  const ci = t.ci, head = t.pr?.head_sha ?? null
  const out = (kind: CiKind, label: string, tone: Tone) => ({ kind, label, tone, sha: ci?.sha ?? null })
  if (!t.pr) return out("none", "No PR yet", "neutral")
  if (!ci || ci.state === null) return out("none", "No CI result recorded", "neutral")
  if (ci.state === "MERGED_OBSERVED") return out("observed", "Observed after merge", "neutral")
  if (ci.state === "STALE" || (ci.sha !== null && head !== null && ci.sha !== head)) return out("outdated", "CI on outdated SHA", "warning")
  if (ci.state === "FAILURE") return out("failed", "CI failed", "danger")
  if (ci.state === "PENDING") return out("running", "CI running", "info")
  if (ci.state === "SUCCESS") return ci.sha && head && ci.sha === head ? out("passed", "CI passed on PR head", "success")
    : out("outdated", "CI on outdated SHA", "warning")
  return out("unknown", "CI state unknown", "warning")
}

/** Independent review: approved only with a matching tested/reviewed fingerprint; anything else is not verified. */
export function reviewView(t: Pick<Task, "reviewer_verdict" | "fingerprint_match" | "tested_fingerprint" | "reviewed_fingerprint">): View {
  if (t.reviewer_verdict === "REJECTED") return v("FAILED", "Rejected")
  if (t.reviewer_verdict === "APPROVED")
    return fingerprintOk(t) ? v("READY", "Approved") : v("NOT_VERIFIED", "Approved, fingerprint not verified")
  if (t.reviewer_verdict === "UNVERIFIED") return v("NOT_VERIFIED")
  return t.reviewer_verdict === null ? v("UNKNOWN", "No verdict") : v("UNKNOWN")
}
export const fingerprintOk = (t: Pick<Task, "fingerprint_match" | "tested_fingerprint" | "reviewed_fingerprint">) =>
  t.fingerprint_match === true && !!t.tested_fingerprint && t.tested_fingerprint === t.reviewed_fingerprint
export const testView = (t: Pick<Task, "test_verdict">): View =>
  t.test_verdict === "PASSED" ? v("READY", "Passed") : t.test_verdict === "FAILED" ? v("FAILED") : v("UNKNOWN", "Not recorded")
export const privacyGateLabel = (code: string): string | null =>
  code === "PRIVACY_GATE_BLOCKED" ? "Privacy check blocked publication"
    : code === "PRIVACY_GATE_ERROR" ? "Privacy verification could not complete" : null

export type StepState = "completed" | "in_progress" | "failed" | "pending" | "unknown" | "not_verified" | "observed"
/** History records entry, not completion. A later entry and the relevant evidence must both confirm completion.
 *  Use the last visit to each stage so an earlier attempt cannot complete a repair still in progress. */
export function steps(t: Task): { key: string; label: string; state: StepState }[] {
  const current = KNOWN_STAGES.indexOf(t.stage ?? "UNKNOWN") - 1
  const history = t.history ?? []
  const stages = history.map(h => h.stage)
  const intact = history.length > 0 && stages.at(-1) === t.stage && history.every((h, i) =>
    h.stage !== "UNKNOWN" && h.at !== null && (i === 0 || Date.parse(h.at) >= Date.parse(history[i - 1].at ?? "")))
  const tested = t.test_verdict === "PASSED"
  const reviewed = tested && reviewView(t).badge === "READY"
  const published = reviewed && prHref(t.pr?.number) !== null && !!t.pr?.head_sha
  const checked = published && ciView(t).kind === "passed"
  const evidence = [true, true, tested, reviewed, published, checked, checked, checked]
  return PIPELINE.map(([key, label], j) => {
    let state: StepState = "unknown"
    const at = stages.lastIndexOf(key)
    if (t.ci?.state === "MERGED_OBSERVED" && j >= 5) state = "observed"
    else if (t.status !== "UNKNOWN" && intact && current >= -1 && t.stage !== "ABANDONED") {
      if (j > current) state = "pending"
      else if (j === current && (t.status === "FAILED" || t.status === "BLOCKED")) state = "failed"
      else if (at >= 0) {
        if (j === current && key !== "READY_TO_MERGE" && key !== "MERGED")
          state = t.status === "IN_PROGRESS" ? "in_progress" : "unknown"
        else if (j === current || stages.lastIndexOf(KNOWN_STAGES[j + 2]) > at)
          state = evidence[j] && !(j === current && t.status === "CANCELLED") ? "completed" : "not_verified"
      }
    }
    return { key, label, state }
  })
}

const BLOCKER: Record<string, string> = {
  api_sha_unknown: "API commit not reported", worker_sha_unknown: "Worker commit not reported",
  sha_mismatch: "API and worker run different commits", worker_not_healthy: "Worker heartbeat not healthy",
  agents_paused: "Agents are paused", write_disabled: "Code-write flag is off", worker_not_ready: "Worker reports not ready",
  host_not_accepted: "Host acceptance missing or not valid for this commit", no_authorization: "No Stage C authorization issued",
  task_needs_founder: "A task needs you", unrecognised_blocker: "Unrecognised blocker (not shown)",
}
const FAMILY: [string, string][] = [["one_task_", "One-task authorization"], ["standing_", "Standing authorization"], ["preflight_", "Preflight"]]
/** Words for a backend blocker code. Unknown codes are shown as they are, as text. */
export function blockerLabel(c: string): string {
  if (BLOCKER[c]) return BLOCKER[c]
  const f = FAMILY.find(([p]) => c.startsWith(p))
  return f ? `${f[1]}: ${c.slice(f[0].length).replaceAll("_", " ")}` : c
}

const ACTION: Record<StageCState["next_action"], string | null> = {
  check_deploy: "Check the deployed API commit",
  check_worker: "Check the worker and its heartbeat",
  redeploy_one_sha: "Redeploy so the API and worker run the same commit",
  resume_agents: "Resume agents when you want autonomous work to continue",
  accept_host: "Run the host acceptance for the deployed commit",
  enable_code_write: "Enable code writes on the host only if you intend Stage C to run",
  run_preflight: "Wait for or rerun the watchdog preflight on the host",
  review_task: "Review the task that needs you (merge a READY_TO_MERGE PR, or inspect a blocked or failed one)",
  wait_daily_reset: "Wait for the standing authorization's daily limit to reset",
  issue_authorization: "Issue a new Stage C authorization from the host if you want work to continue",
  none: null,
  UNKNOWN: "Next action unknown: not reported or unrecognised",
}
/** Guidance comes only from the backend's allowlisted decision; never derive another blocker priority. */
export const nextAction = (s: Pick<StageCState, "next_action">): string | null => ACTION[s.next_action]

/** The backend searches all tasks, including ones outside this snapshot's recent-task window. */
export const lastReadyPr = (s: Pick<StageCState, "last_ready_pr">) =>
  s.last_ready_pr && s.last_ready_pr.stage !== "UNKNOWN" && prHref(s.last_ready_pr.number) ? s.last_ready_pr : null

export const STALE_MS = 90_000
export const CLOCK_SKEW_MS = 30_000
/** Both retrieval and snapshot age must be within 90s. Allow up to 30s of future server skew (age floored at zero);
 *  larger future skew, a reversed browser clock or invalid times are UNKNOWN, never verified. */
export function readFreshness(s: Pick<StageCState, "generated_at">, fetchedAt: number, now: number, failed: boolean): { freshness: "VERIFIED" | "STALE" | "UNKNOWN" } {
  const generated = Date.parse(s.generated_at ?? "")
  if (![generated, fetchedAt, now].every(Number.isFinite) || now < fetchedAt || generated - fetchedAt > CLOCK_SKEW_MS)
    return { freshness: "UNKNOWN" }
  return { freshness: failed || now - fetchedAt > STALE_MS || Math.max(0, now - generated) > STALE_MS ? "STALE" : "VERIFIED" }
}

export type LoadError = "signed_out" | "forbidden" | "unavailable" | "error"
export const offerRevokeOnFailure = (kind: LoadError) => kind === "error" || kind === "unavailable"
/** How a failed read is handled: a sign-out goes to the shell's session handling; 403 is a scope refusal (a Desk token
 *  on a founder-only route), not a sign-out; 404 means the deployed API does not serve this read yet. */
export function loadError(e: unknown): LoadError {
  if (!(e instanceof ApiError)) return "error"
  if (e.signIn || e.status === 401) return "signed_out"
  return e.status === 403 ? "forbidden" : e.status === 404 ? "unavailable" : "error"
}
