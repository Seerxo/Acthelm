// Home's whole-company view, derived only from what the Desk already reads: /desk/state (DeskState) and, when the
// founder's session may read it, /stage-c/state (StageCState). Every figure is the API's own; a source the API did not
// report is null and reads "unknown", never 0 or "none". Pure functions, so `npm test` runs them without a browser.
import type { Agent, DeskState, Event, Need, TaskRow, Tone } from "./api.ts"
import { agentState } from "./agent-state.ts"
import { blockerLabel, lastReadyPr, nextAction, prHref, type StageCState } from "./stage-c.ts"

export type NeedLevel = "critical" | "warning" | "info"
export type NeedTarget = { page: string; key?: string } | { href: string }
export interface NeedItem {
  id: string; level: NeedLevel; rank: number; title: string; detail: string; since: string | null
  target: NeedTarget
  /** The API's needs-you row this item came from, so Home can offer the same in-place decision as before. */
  need?: Need
}
export interface NeedsView { items: NeedItem[]; /** Sources the API did not report: the list may be incomplete. */ unknown: string[] }

const LEVEL: Record<NeedLevel, number> = { critical: 0, warning: 1, info: 2 }
const level = (v: string): NeedLevel => (v === "critical" || v === "info" ? v : "warning")   // unrecognised: never quieter than warning
// Ranks for items the Desk adds, placed among desk.py _RANK (APPROVAL 4, BLOCKED_P0 5, PROVIDER 7, OTHER 8).
const R = { readyToMerge: 4.1, awaitingApproval: 4.2, mergedAccept: 4.3, stopped: 5.5, review: 6, stageC: 7.5 }
const CLOSED = ["DONE", "CANCELLED"]

/** Everything that waits on the founder, most urgent first: critical before warning before info, then the API's own
 *  rank, then oldest first. One item per task: the API's row wins, then the more specific Stage C reason. */
/** A REVIEW task is "merged, awaiting acceptance" only with positive evidence: its improvement row is at MERGED or
 *  VERIFYING and `pr_verified === true` (added by another branch; absent or anything else = not verified). */
export function mergedVerified(improvements: DeskState["improvements"], key: string): boolean {
  const i = improvements?.find(x => x.task === key)
  return !!i && ["MERGED", "VERIFYING"].includes(i.stage) && (i as { pr_verified?: unknown }).pr_verified === true
}

/** Stage C exists on this host only when /stage-c/state carries host evidence: a recorded host acceptance or a
 *  watchdog preflight. Production serves the route too, with neither; there Home hides Stage C instead of showing it
 *  broken. Returns the state when proven, else null. */
export function stageCOnHost(sc: StageCState | null): StageCState | null {
  if (!sc) return null
  const { acceptance: a, preflight: p } = sc.runtime
  const proven = a.accepted_at !== null || a.source_sha !== null || p.checked_at !== null || ["PASS", "FAIL", "STALE"].includes(p.status)
  return proven ? sc : null
}

export function needsYou(d: Pick<DeskState, "needs_you" | "tasks" | "improvements">, sc: StageCState | null): NeedsView {
  const items: NeedItem[] = []
  const unknown: string[] = []
  const seen = new Set<string>()
  const add = (it: NeedItem, taskKey?: string | null) => {
    if (taskKey) { if (seen.has(taskKey)) return; seen.add(taskKey) }
    items.push(it)
  }

  if (Array.isArray(d.needs_you)) d.needs_you.forEach((n, i) => {
    const key = n.target.key ?? undefined
    add({ id: `need:${n.kind}:${key ?? i}`, level: level(n.level), rank: Number.isFinite(n.rank) ? n.rank : 8, title: n.title,
      detail: n.why, since: n.since, target: { page: n.target.page, key }, need: n }, n.target.page === "tasks" ? key : null)
  })
  else unknown.push("attention queue")

  // sc null: Stage C is not proven on this host (stageCOnHost), so it adds nothing and is not "unknown" either.
  if (sc && !sc.tasks) unknown.push("Stage C tasks")
  else if (sc?.tasks) for (const t of sc.tasks) {
    if (!t.key || CLOSED.includes(t.status)) continue
    if (t.stage === "READY_TO_MERGE") {
      const href = prHref(t.pr?.number)
      add({ id: `sc:merge:${t.key}`, level: "warning", rank: R.readyToMerge, since: t.updated_at,
        title: href ? `PR #${t.pr!.number} is ready to merge` : `${t.key} is ready to merge`,
        detail: `${t.key} · merging is yours`, target: href ? { href } : { page: "tasks", key: t.key } }, t.key)
    } else if (t.stage === "MERGED" && t.status === "REVIEW") {
      add({ id: `sc:accept:${t.key}`, level: "warning", rank: R.mergedAccept, since: t.updated_at, title: `${t.key} merged; accept the result`,
        detail: "Merged on GitHub. Not done until you accept it", target: { page: "tasks", key: t.key } }, t.key)
    }
  }

  for (const t of d.tasks) {
    const stop = t.status === "BLOCKED" || t.status === "FAILED"
    if (stop) add({ id: `task:${t.key}`, level: "warning", rank: R.stopped, since: t.updated_at, title: t.title,
      detail: `${t.key} · ${t.status === "FAILED" ? "failed" : "blocked"}`, target: { page: "tasks", key: t.key } }, t.key)
    else if (t.status === "WAITING_APPROVAL") add({ id: `task:${t.key}`, level: "warning", rank: R.awaitingApproval, since: t.updated_at,
      title: t.title, detail: `${t.key} · waiting for your approval`, target: { page: "tasks", key: t.key } }, t.key)
    else if (t.status === "REVIEW" && mergedVerified(d.improvements, t.key)) add({ id: `task:${t.key}`, level: "warning", rank: R.mergedAccept,
      since: t.updated_at, title: t.title, detail: `${t.key} · Merged · awaiting acceptance`, target: { page: "tasks", key: t.key } }, t.key)
    else if (t.status === "REVIEW") add({ id: `task:${t.key}`, level: "info", rank: R.review, since: t.updated_at, title: t.title,
      detail: `${t.key} · finished by the agent; review and accept`, target: { page: "tasks", key: t.key } }, t.key)
  }

  if (sc) {
    // task_needs_founder is the task rows above; listing it again would count one decision twice.
    const blockers = sc.blockers?.filter(b => b !== "task_needs_founder") ?? null
    const failed = sc.runtime.preflight.failed ?? []
    if (blockers === null) unknown.push("Stage C readiness")
    else if (blockers.length || failed.length) {
      const first = blockers.length ? blockerLabel(blockers[0]) : `Preflight check failing: ${failed[0]}`
      const more = blockers.length + failed.length - 1
      add({ id: "sc:readiness", level: "warning", rank: R.stageC, since: sc.runtime.preflight.checked_at,
        title: `Stage C cannot run: ${first}${more > 0 ? ` (+${more} more)` : ""}`,
        detail: nextAction(sc) ?? "No next action reported", target: { page: "stage-c" } })
    }
  }

  const order = (a: NeedItem, b: NeedItem) => LEVEL[a.level] - LEVEL[b.level] || a.rank - b.rank
    || (a.since ?? "￿").localeCompare(b.since ?? "￿")
  return { items: items.sort(order), unknown }
}

// --------------------------------------------------------------------------- agents
export type Bucket = "running" | "waiting" | "sleeping" | "blocked" | "paused" | "unknown"
const BUCKET: Record<string, Bucket> = {
  RUNNING: "running", WAITING: "waiting", NEEDS_APPROVAL: "waiting", SLEEPING: "sleeping",
  BLOCKED: "blocked", FAILED: "blocked", STALLED: "blocked", PAUSED: "paused", DISABLED: "paused",
}
export const BUCKET_TONE: Record<Bucket, Tone> = { running: "info", waiting: "warning", sleeping: "neutral", blocked: "danger", paused: "warning", unknown: "warning" }

export interface AgentRow { key: string; name: string; bucket: Bucket; label: string; tone: Tone; task: Agent["current_task"]; lastRun: string | null; live: boolean }

/** Each agent's state as the Agents page shows it (lib/agent-state.ts): RUNNING without a heard worker is "not
 *  confirmed", which is unknown here, never running. Most urgent first: blocked, waiting, running, then the rest. */
export function agentRows(d: DeskState, now: number): { rows: AgentRow[]; counts: Record<Bucket, number> } {
  const rank: Bucket[] = ["blocked", "waiting", "running", "unknown", "paused", "sleeping"]
  const rows = d.agents.map(a => {
    const s = agentState(a, d, now)
    const bucket = BUCKET[s.value] ?? "unknown"
    return { key: a.key, name: a.name, bucket, label: s.label ?? s.value.toLowerCase().replaceAll("_", " "), tone: s.tone ?? BUCKET_TONE[bucket],
      task: a.current_task, lastRun: a.last_run, live: s.running }
  }).sort((a, b) => rank.indexOf(a.bucket) - rank.indexOf(b.bucket) || a.name.localeCompare(b.name))
  const counts = Object.fromEntries(rank.map(b => [b, rows.filter(r => r.bucket === b).length])) as Record<Bucket, number>
  return { rows, counts }
}

// --------------------------------------------------------------------------- autonomous work
export interface Autonomy {
  /** null = Stage C did not report its task list. Counts cover the recent tasks the API returns. */
  counts: { inProgress: number; readyToMerge: number; merged: number; blocked: number } | null
  /** true when the API says its task list is cut short: the counts are a lower bound. */
  partial: boolean
  lastPr: { number: number; href: string; taskKey: string | null; stage: string } | null
  codeWrite: boolean | null
  paused: boolean | null
}

export function autonomy(d: Pick<DeskState, "control" | "worker">, sc: StageCState | null): Autonomy {
  const tasks = sc?.tasks ?? null
  const last = sc ? lastReadyPr(sc) : null
  const href = last ? prHref(last.number) : null
  return {
    counts: tasks && {
      inProgress: tasks.filter(t => t.status === "IN_PROGRESS").length,
      readyToMerge: tasks.filter(t => t.stage === "READY_TO_MERGE" && !CLOSED.includes(t.status)).length,
      merged: tasks.filter(t => t.stage === "MERGED").length,
      blocked: tasks.filter(t => t.status === "BLOCKED" || t.status === "FAILED").length,
    },
    partial: sc?.metrics.truncated === true,
    lastPr: last && href ? { number: last.number!, href, taskKey: last.task_key, stage: last.stage } : null,
    // Stage C's own runtime first; the worker's flag in /desk/state only when Stage C is not readable.
    codeWrite: sc ? sc.runtime.write_enabled : d.worker?.autonomy?.code_write_flag ?? null,
    paused: sc?.runtime.agents_paused ?? d.control.agents_paused,
  }
}

// --------------------------------------------------------------------------- budget
export interface Cost { label: string; value: string | null; note: string }
const money = (n: number | null | undefined) => (typeof n === "number" && Number.isFinite(n) ? `$${n.toFixed(2)}` : null)

/** Only what the API recorded. A value that is not reported is null (shown "unknown"), never $0. */
export function costs(d: Pick<DeskState, "costs" | "agents">, sc: StageCState | null): Cost[] {
  const reported = d.agents.filter(a => typeof a.cost_today_usd === "number")
  return [
    { label: "New autonomous spend", value: money(d.costs?.new_autonomous_spend_usd), note: "Policy: no new paid spend" },
    { label: "Agent runs today", value: reported.length ? money(reported.reduce((n, a) => n + a.cost_today_usd!, 0)) : null, note: "Estimated, as recorded per run" },
    { label: "Stage C recorded", value: money(sc?.metrics.usd_recorded), note: "USD recorded by Stage C runs" },
    { label: "Quota stops", value: sc && sc.metrics.quota_stops !== null ? String(sc.metrics.quota_stops) : null, note: "Provider quota or limit stops" },
  ]
}

// --------------------------------------------------------------------------- alerts
const LOUD = ["CRITICAL", "HIGH"]
/** Recent HIGH/CRITICAL events, newest first. Incidents are listed separately (they stay open until resolved). */
export const recentAlerts = (activity: Event[], n = 4): Event[] =>
  activity.filter(e => LOUD.includes(e.latest_observation?.severity ?? e.severity)).sort((a, b) => b.at.localeCompare(a.at)).slice(0, n)

// --------------------------------------------------------------------------- now, finished, next
const PRIORITY = (p: string) => (/^P\d$/.test(p) ? Number(p[1]) : 9)
const newest = (a: TaskRow, b: TaskRow) => b.updated_at.localeCompare(a.updated_at)

/** What the system is executing right now, newest activity first. */
export const doingNow = (tasks: TaskRow[]) => tasks.filter(t => t.status === "IN_PROGRESS").sort(newest)

/** Recently finished: DONE tasks, newest first. `tasks` holds every open task but only the newest closed ones, so this
 *  is the latest few, never a total. */
export const recentlyFinished = (tasks: TaskRow[], n = 5) => tasks.filter(t => t.status === "DONE").sort(newest).slice(0, n)

/** Up next: queued work by priority (P0 first), then the oldest waiting longest. */
export const upNext = (tasks: TaskRow[], n = 5) => tasks.filter(t => t.status === "READY" || t.status === "QUEUED")
  .sort((a, b) => PRIORITY(a.priority) - PRIORITY(b.priority) || a.updated_at.localeCompare(b.updated_at)).slice(0, n)

/** The running version, only from /desk/state runtime. null = not reported; the release ledger is never guessed. */
export function runningVersion(d: Pick<DeskState, "runtime">): { api: string | null; worker: string | null; agree: boolean | null; drift: string[] } | null {
  const r = d.runtime
  if (!r) return null
  const api = r.api.git_sha?.slice(0, 7) ?? null, worker = r.worker.git_sha?.slice(0, 7) ?? null
  return { api, worker, agree: api && worker ? r.api.git_sha === r.worker.git_sha && !r.drift.length : null, drift: r.drift }
}
