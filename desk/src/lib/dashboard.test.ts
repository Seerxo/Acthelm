// Home's "Needs you" order and its honesty about sources the API did not report.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState, Need } from "./api.ts"
import { agentRows, autonomy, costs, doingNow, mergedVerified, needsYou, recentlyFinished, runningVersion, stageCOnHost, upNext } from "./dashboard.ts"
import { parseStageC } from "./stage-c.ts"

const NOW = Date.parse("2026-11-09T12:00:00Z")
const task = (key: string, status: string, updated_at = "2026-11-09T10:00:00Z") => ({ key, title: key, status, priority: "P2", risk: "LOW",
  agent: null, project: null, business_unit: null, approval_required: false, created_at: updated_at, updated_at })
const need = (over: Partial<Need>): Need => ({ kind: "OTHER", rank: 8, level: "warning", title: "n", why: "w", affected: "", since: null,
  evidence: "", action: "", target: { page: "approvals" }, ...over })
const sc = (over: Record<string, unknown> = {}) => parseStageC({ version: 1, state: "BLOCKED", blockers: [], runtime: {}, metrics: {}, tasks: [],
  authorizations: { one_task: null, standing: null }, ...over })
const state = (over: Partial<DeskState> = {}) => ({
  control: { agents_paused: false }, worker: { status: "HEALTHY", heartbeat_at: "2026-11-09T11:59:00Z" }, needs_you: [],
  tasks: [], agents: [], ...over,
}) as unknown as DeskState

test("needs you: critical first, then the API rank, Desk items placed among them, oldest first", () => {
  const d = state({
    needs_you: [need({ kind: "APPROVAL", rank: 4, title: "approve" }), need({ kind: "SEV0", rank: 0, level: "critical", title: "sev0" })],
    tasks: [task("TASK-2", "REVIEW"), task("TASK-3", "BLOCKED", "2026-11-09T09:00:00Z"), task("TASK-4", "FAILED", "2026-11-08T09:00:00Z")],
  })
  const s = sc({
    blockers: ["write_disabled", "task_needs_founder"],
    tasks: [{ key: "TASK-1", status: "REVIEW", stage: "READY_TO_MERGE", pr: { number: 61 } }, { key: "TASK-5", status: "REVIEW", stage: "MERGED" }],
  })
  const v = needsYou(d, s)
  assert.deepEqual(v.items.map(i => i.id),
    ["need:SEV0:1", "need:APPROVAL:0", "sc:merge:TASK-1", "sc:accept:TASK-5", "task:TASK-4", "task:TASK-3", "sc:readiness", "task:TASK-2"])
  assert.deepEqual(v.items[2].target, { href: "https://github.com/Seerxo/Acthelm/pull/61" })
  assert.equal(v.items[6].title, "Stage C cannot run: Code-write flag is off")
  assert.deepEqual(v.unknown, [])
})

test("needs you: one item per task, the API's row first", () => {
  const d = state({ needs_you: [need({ kind: "BLOCKED_P0", rank: 5, target: { page: "tasks", key: "TASK-3" } })], tasks: [task("TASK-3", "BLOCKED")] })
  assert.deepEqual(needsYou(d, sc()).items.map(i => i.id), ["need:BLOCKED_P0:TASK-3"])
})

test("needs you: an unreported source is named unknown, never read as empty", () => {
  const v = needsYou(state({ needs_you: undefined }), null)
  assert.deepEqual(v.unknown, ["attention queue"])   // no Stage C on this host: hidden, not unknown
  assert.deepEqual(needsYou(state(), sc({ blockers: "x", tasks: null })).unknown, ["Stage C tasks", "Stage C readiness"])
  // An unrecognised level is never quieter than a warning.
  assert.equal(needsYou(state({ needs_you: [need({ level: "odd" })] }), sc()).items[0].level, "warning")
})

test("autonomy and costs: missing Stage C is unknown, not zero", () => {
  const a = autonomy(state(), null)
  assert.equal(a.counts, null)
  assert.equal(a.codeWrite, null)
  assert.equal(a.paused, false)
  assert.deepEqual(costs(state(), null).map(c => c.value), [null, null, null, null])
  const b = autonomy(state(), sc({ runtime: { write_enabled: true, agents_paused: false }, tasks: [{ key: "TASK-1", status: "IN_PROGRESS", stage: "IMPLEMENTING" }, { key: "TASK-2", status: "DONE", stage: "MERGED" }] }))
  assert.deepEqual(b.counts, { inProgress: 1, readyToMerge: 0, merged: 1, blocked: 0 })
  assert.equal(b.codeWrite, true)
  const c = costs(state({ agents: [{ cost_today_usd: 0.5 }, {}] as DeskState["agents"], costs: { new_autonomous_spend_usd: 0, items: [] } }),
    sc({ metrics: { usd_recorded: 1.25, quota_stops: 2 } }))
  assert.deepEqual(c.map(x => x.value), ["$0.00", "$0.50", "$1.25", "2"])
})

test("agents: running without a heard worker is unknown, blocked sorts first", () => {
  const agents = [{ key: "a", name: "A", status: "RUNNING" }, { key: "b", name: "B", status: "STALLED" }, { key: "c", name: "C", status: "SLEEPING" }]
  const live = agentRows(state({ agents } as Partial<DeskState>), NOW)
  assert.deepEqual(live.rows.map(r => [r.key, r.bucket]), [["b", "blocked"], ["a", "running"], ["c", "sleeping"]])
  const silent = agentRows(state({ agents, worker: { status: "STALE" } } as Partial<DeskState>), NOW)
  assert.equal(silent.rows.find(r => r.key === "a")!.bucket, "unknown")
  assert.equal(silent.counts.running, 0)
})

test("Stage C shows only on the host's current capability, never on history or a failing preflight", () => {
  assert.equal(stageCOnHost(null), null)
  assert.equal(stageCOnHost(sc()), null)   // production: the route answers, with no host evidence
  assert.equal(stageCOnHost(sc({ runtime: { preflight: { status: "FAIL", checked_at: "2026-11-09T11:00:00Z" } } })), null)
  assert.equal(stageCOnHost(sc({ runtime: { acceptance: { accepted_at: "2026-10-23T11:00:00Z", source_sha: "a".repeat(40) } } })), null)
  assert.ok(stageCOnHost(sc({ runtime: { preflight: { status: "PASS", checked_at: "2026-11-09T11:00:00Z" } } })))
  assert.ok(stageCOnHost(sc({ runtime: { api_sha: "b".repeat(40), acceptance: { accepted_at: "2026-11-09T11:00:00Z", source_sha: "b".repeat(40) } } })))
})

test("now, finished and next come from task status, in a fixed order", () => {
  const tasks = [task("T-1", "DONE", "2026-11-09T08:00:00Z"), task("T-2", "DONE", "2026-11-09T09:00:00Z"), task("T-3", "IN_PROGRESS"),
    { ...task("T-4", "READY", "2026-11-09T07:00:00Z"), priority: "P1" }, { ...task("T-5", "QUEUED", "2026-11-09T06:00:00Z"), priority: "P3" },
    { ...task("T-6", "READY", "2026-11-09T05:00:00Z"), priority: "P1" }, task("T-7", "REVIEW")]
  assert.deepEqual(doingNow(tasks).map(t => t.key), ["T-3"])
  assert.deepEqual(recentlyFinished(tasks).map(t => t.key), ["T-2", "T-1"])
  assert.deepEqual(upNext(tasks).map(t => t.key), ["T-6", "T-4", "T-5"])
})

test("running version: short SHAs from runtime only; absent runtime is unknown", () => {
  assert.equal(runningVersion({}), null)
  const sha = "a".repeat(40)
  const rt = (worker: string | null, drift: string[] = []) => ({ runtime: { api: { git_sha: sha, started_at: "" }, worker: { git_sha: worker, image: null, started_at: null },
    migration: { database: null, code_head: null }, stage_c_enabled: null, guardian_enabled: null, drift } })
  assert.deepEqual(runningVersion(rt(sha)), { api: "aaaaaaa", worker: "aaaaaaa", agree: true, drift: [] })
  assert.equal(runningVersion(rt(null))!.agree, null)
  assert.equal(runningVersion(rt("b".repeat(40)))!.agree, false)
})

test("merged and awaiting acceptance needs pr_verified === true at MERGED or VERIFYING; anything else is plain review", () => {
  const imp = (stage: string, pr_verified?: unknown) => [{ task: "T-1", stage, pr_verified }] as unknown as DeskState["improvements"]
  assert.equal(mergedVerified(imp("MERGED", true), "T-1"), true)
  assert.equal(mergedVerified(imp("VERIFYING", true), "T-1"), true)
  assert.equal(mergedVerified(imp("MERGED"), "T-1"), false)          // field absent (older API): not verified
  assert.equal(mergedVerified(imp("MERGED", "true"), "T-1"), false)
  assert.equal(mergedVerified(imp("PR_OPEN", true), "T-1"), false)
  assert.equal(mergedVerified(undefined, "T-1"), false)
  const v = needsYou(state({ tasks: [task("T-1", "REVIEW")], improvements: imp("MERGED", true) }), null)
  assert.equal(v.items[0].detail, "T-1 · Merged · awaiting acceptance")
  assert.equal(v.items[0].level, "warning")
  assert.equal(needsYou(state({ tasks: [task("T-1", "REVIEW")], improvements: imp("MERGED") }), null).items[0].level, "info")
})
