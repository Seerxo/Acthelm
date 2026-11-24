// B5 handoff tracking (GET /desk/handoffs, /tasks/{key}.remote_stage_c). Break caught: a stale report keeps its last
// lane, an unknown status files as healthy, CI on another SHA reads passed, or a missing report invents evidence.
// Synthetic data only. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { board, laneOf, liveFreshness, parseHandoff, parseHandoffs, waitingReason } from "./handoffs.ts"

const NOW = "2026-11-17T10:00:00+00:00", HEAD = "1".repeat(40), OTHER = "2".repeat(40), FP = "a".repeat(64)
const remote = (over: Record<string, unknown> = {}) => ({
  status: "REVIEW", stage: "CI", test_verdict: "PASSED", reviewer_verdict: "APPROVED",
  tested_fingerprint: FP, reviewed_fingerprint: FP, fingerprint_match: true,
  pr: { number: 12, head_sha: HEAD }, ci: { state: "SUCCESS", sha: HEAD }, failure_category: null, needs_founder: false,
  history: [{ stage: "PLANNED", at: NOW }, { stage: "CI", at: NOW }], ...over,
})
const view = (over: Record<string, unknown> = {}, task: unknown = remote()) => ({
  id: "00000000-0000-4000-8000-000000000001", payload_sha256: FP, delivery: "DELIVERED", remote_task_key: "TASK-9001",
  released_at: NOW, expires_at: NOW, last_error_code: null, cancellation: "NONE", last_success_at: NOW,
  freshness: "FRESH", source: "Stage C reported", seq: 3, observed_at: NOW,
  snapshot: { version: 1, task_key: "TASK-9001", seq: 3, generated_at: NOW, runtime: { write_enabled: true },
    authorization: { status: "ACTIVE" }, task, merge: { state: "NOT_OBSERVED", commit: null, at: null }, cancellation: "NONE" },
  ...over,
})

test("lanes follow the Stage C-reported status", () => {
  const lane = (status: string) => laneOf(parseHandoff(view({}, remote({ status })))!)
  assert.equal(lane("BACKLOG"), "BACKLOG")
  assert.equal(lane("QUEUED"), "READY")
  assert.equal(lane("IN_PROGRESS"), "RUNNING")
  assert.equal(lane("REVIEW"), "REVIEW")
  assert.equal(lane("DONE"), "COMPLETED")
  for (const s of ["FAILED", "BLOCKED", "WAITING_APPROVAL", "CANCELLED", "SOMETHING_NEW"]) assert.equal(lane(s), "BLOCKED", s)
})

test("a stale report is UNKNOWN, never its last lane, and says why", () => {
  const stale = parseHandoff(view({ freshness: "UNKNOWN", last_error_code: "TIMEOUT" }, remote({ status: "DONE" })))!
  assert.equal(laneOf(stale), "UNKNOWN")
  assert.match(waitingReason(stale)!, /No Stage C report.*TIMEOUT/)
  assert.equal(laneOf(parseHandoff(view({ freshness: "garbage" }))!), "UNKNOWN")
})

test("without a report: delivery explains the lane; nothing is invented", () => {
  const pending = parseHandoff(view({ delivery: "PENDING", snapshot: null }))!
  assert.equal(pending.task, null)
  assert.equal(pending.merge.state, "UNKNOWN")
  assert.equal(laneOf(pending), "BACKLOG")
  assert.equal(waitingReason(pending), "Released; waiting for delivery to Stage C")
  const refused = parseHandoff(view({ delivery: "REFUSED", last_error_code: "SCOPE_REFUSED", snapshot: null }))!
  assert.equal(laneOf(refused), "BLOCKED")
  assert.match(waitingReason(refused)!, /SCOPE_REFUSED/)
  const delivered = parseHandoff(view({ snapshot: null, freshness: "UNKNOWN" }))!
  assert.equal(laneOf(delivered), "UNKNOWN")
  assert.equal(waitingReason(delivered), "Delivered; Stage C has not reported this task yet")
  assert.equal(parseHandoff(null), null)
})

test("waiting reasons come from recorded fields", () => {
  assert.equal(waitingReason(parseHandoff(view({}, remote({ status: "IN_PROGRESS" })))!), null)
  assert.equal(waitingReason(parseHandoff(view({}, remote({ status: "BLOCKED", failure_category: "TESTS_FAILED" })))!), "Blocked: tests failed")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ needs_founder: true })))!), "Stage C reports it needs you")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ reviewer_verdict: null })))!), "Waiting for the independent review")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ ci: { state: "PENDING", sha: HEAD } })))!), "Waiting for CI on the PR")
  assert.equal(waitingReason(parseHandoff(view({}, remote()))!), "Ready to merge; waiting for the founder's merge")
  assert.equal(waitingReason(parseHandoff(view({ cancellation: "PENDING" }))!), "Cancel requested; Stage C has not acknowledged the stop yet")
  const queued = view({}, remote({ status: "READY" }))
  queued.snapshot.authorization = { status: "EXPIRED" }
  assert.equal(waitingReason(parseHandoff(queued)!), "Waiting for a Stage C authorization (grant expired)")
})

test("an unverified or fingerprint-mismatched review, or failed tests, is never ready to merge", () => {
  assert.equal(waitingReason(parseHandoff(view({}, remote({ reviewer_verdict: "UNVERIFIED" })))!), "Independent review: Not verified")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ fingerprint_match: false })))!), "Independent review: Approved, fingerprint not verified")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ reviewed_fingerprint: "b".repeat(64) })))!), "Independent review: Approved, fingerprint not verified")
  assert.equal(waitingReason(parseHandoff(view({}, remote({ test_verdict: "FAILED" })))!), "Tests: Failed")
})

test("CI SUCCESS on another SHA is not a pass", () => {
  const t = parseHandoff(view({}, remote({ ci: { state: "SUCCESS", sha: OTHER } })))!
  assert.equal(waitingReason(t), "CI: CI on outdated SHA")
})

test("the list parser keeps the production row and drops malformed entries", () => {
  const out = parseHandoffs({ enabled: false, handoffs: [{ key: "TASK-0042", title: "Synthetic task", status: "WAITING_EXTERNAL", handoff: view() }, { key: "TASK-1" }, 7] })
  assert.equal(out.enabled, false)
  assert.equal(out.handoffs.length, 1)
  assert.deepEqual([out.handoffs[0].key, out.handoffs[0].status, out.handoffs[0].task?.key], ["TASK-0042", "WAITING_EXTERNAL", "TASK-9001"])
  assert.deepEqual(parseHandoffs(null), { enabled: null, handoffs: [] })
  const b = board(out.handoffs)
  assert.equal(b.find(l => l.lane === "REVIEW")!.items.length, 1)
  assert.equal(b.length, 7)
})

test("freshness expires in the browser even when the server last said FRESH", () => {
  const at = "2026-11-17T12:00:00Z", t0 = Date.parse(at)
  assert.equal(liveFreshness("FRESH", at, t0 + 60_000), "FRESH")
  assert.equal(liveFreshness("FRESH", at, t0 + 181_000), "UNKNOWN")      // stalled refresh: not fresh any more
  assert.equal(liveFreshness("UNKNOWN", at, t0), "UNKNOWN")
  assert.equal(liveFreshness("FRESH", null, t0), "UNKNOWN")
  assert.equal(liveFreshness("FRESH", "not a date", t0), "UNKNOWN")
})
