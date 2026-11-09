// The shell header's three facts: honest about a silent worker, a pause, and decisions waiting. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState } from "./api.ts"
import { shellStatus } from "./shell-status.ts"

const NOW = Date.parse("2026-11-09T12:00:00Z")
const heard = { status: "HEALTHY", heartbeat_at: "2026-11-09T11:59:30Z" }
const base = (over: Partial<DeskState> = {}) => ({
  control: { agents_paused: false }, worker: heard, agents: [], tasks: [], approvals: [], incidents: [], needs_you: [],
  health: { api: "HEALTHY", postgresql: "HEALTHY", resources: {}, services: [], backup: null }, ...over,
} as unknown as DeskState)
const get = (d: DeskState) => Object.fromEntries(shellStatus(d, NOW).map(f => [f.key, f]))

test("running agents count only while the worker is heard from", () => {
  const agents = [{ key: "a", status: "RUNNING" }, { key: "b", status: "IDLE" }] as DeskState["agents"]
  assert.equal(get(base({ agents })).agents.value, "1 running")
  assert.equal(get(base({ agents })).worker.tone, "success")
  const silent = get(base({ agents, worker: { status: "HEALTHY", heartbeat_at: "2026-11-09T11:00:00Z" } }))
  assert.equal(silent.agents.value, "None running")
  assert.equal(silent.worker.tone, "danger")
  assert.equal(silent.worker.short, "Down")
})

test("a missing worker is not reported as running; a pause is shown as paused", () => {
  const none = get(base({ worker: undefined }))
  assert.equal(none.worker.value, "Not reported")
  assert.notEqual(none.worker.tone, "success")
  const paused = get(base({ control: { agents_paused: true } }))
  assert.equal(paused.agents.value, "Paused")
  assert.equal(paused.worker.short, "Paused")
})

test("approvals count records plus tasks stopped for a decision", () => {
  assert.equal(get(base()).approvals.value, "None waiting")
  const d = base({ approvals: [{ key: "x" }] as DeskState["approvals"],
    tasks: [{ key: "T1", status: "WAITING_APPROVAL" }, { key: "T2", status: "READY" }] as DeskState["tasks"] })
  assert.equal(get(d).approvals.short, "2")
  assert.equal(get(d).approvals.tone, "warning")
})
