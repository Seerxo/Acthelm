import assert from "node:assert/strict"
import { test } from "node:test"
import { codeChips, LANES, laneOf, laneSort } from "./task-lanes.ts"

const ALL = ["IDEA", "BACKLOG", "READY", "QUEUED", "IN_PROGRESS", "WAITING_EXTERNAL", "WAITING_APPROVAL", "BLOCKED", "REVIEW", "FAILED", "DONE", "CANCELLED"]

test("every status lands in one lane; review and stopped work need the founder", () => {
  for (const s of ALL) assert.ok(LANES.some(l => l.key === laneOf(s)), s)
  for (const s of ["REVIEW", "BLOCKED", "FAILED", "WAITING_APPROVAL", "SOMETHING_NEW"]) assert.equal(laneOf(s), "needs", s)
  for (const s of ["IN_PROGRESS", "READY", "WAITING_EXTERNAL"]) assert.equal(laneOf(s), "running", s)
  assert.equal(laneOf("DONE"), "done"); assert.equal(laneOf("BACKLOG"), "backlog")
})

test("stopped work sorts before review inside Needs you", () => {
  const rows = [{ key: "a", status: "REVIEW", priority: "P0", updated_at: "2" }, { key: "b", status: "FAILED", priority: "P3", updated_at: "1" }]
  assert.deepEqual(laneSort(rows).map(r => r.key), ["b", "a"])
})

test("PR and CI chips come only from recorded facts", () => {
  assert.deepEqual(codeChips(undefined), [])
  assert.deepEqual(codeChips({ pr_number: null, ci_state: null, failed_check: null }), [])
  assert.deepEqual(codeChips({ pr_number: 77, ci_state: "SUCCESS", failed_check: null, pr_verified: true }), [{ label: "PR #77", tone: "neutral" }, { label: "CI success", tone: "success" }])
  assert.equal(codeChips({ pr_number: 7, ci_state: "FAILURE", failed_check: "python", pr_verified: true })[1].tone, "danger")
  for (const pr_verified of [false, undefined])   // unverified or an older API: never a number
    assert.deepEqual(codeChips({ pr_number: 7, ci_state: null, failed_check: null, pr_verified }), [{ label: "PR unverified", tone: "warning" }])
})

test("PR id", () => {
  assert.deepEqual(codeChips({ pr_number: null, pr_verified: true, ci_state: null, failed_check: null }), [])
  // A later evidence conflict makes the API row unverified, even with a known publication number.
  assert.deepEqual(codeChips({ pr_number: 77, pr_verified: false, ci_state: null, failed_check: null }),
    [{ label: "PR unverified", tone: "warning" }])
})
