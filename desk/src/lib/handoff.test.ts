import assert from "node:assert/strict"
import { test } from "node:test"
import { approvalState, releaseBlock, type HandoffView } from "./handoff.ts"

const H: HandoffView = { id: "00000000-0000-4000-8000-000000000001", payload_sha256: "a".repeat(64), delivery: "PENDING",
  remote_task_key: null, released_at: "2026-11-17T10:00:00+00:00", expires_at: "2026-11-18T10:00:00+00:00",
  last_error_code: null, cancellation: "NONE", freshness: "UNKNOWN", snapshot: null }
const ok = { founder: true, enabled: true, status: "BACKLOG", handoff: null, bodyError: null }

test("only the verified founder, on an enabled sender, releases an unreleased releasable task", () => {
  assert.equal(releaseBlock(ok), null)
  assert.equal(releaseBlock({ ...ok, risk: "LOW" }), null)
  for (const s of ["READY", "BLOCKED"]) assert.equal(releaseBlock({ ...ok, status: s }), null)
  for (const f of [{ founder: null }, { founder: false }, { enabled: false }, { status: null }, { status: "IN_PROGRESS" },
    { status: "DONE" }, { handoff: H }, { risk: "MEDIUM" }, { risk: "HIGH" }, { bodyError: "Choose at least one file the task may change." }])
    assert.ok(releaseBlock({ ...ok, ...f }), JSON.stringify(f))
})

test("a suspended or refused delivery may be retried with valid text; a cancelled one never", () => {
  for (const delivery of ["SUSPENDED", "REFUSED"]) {
    assert.equal(releaseBlock({ ...ok, status: "WAITING_EXTERNAL", handoff: { ...H, delivery } }), null)
    assert.ok(releaseBlock({ ...ok, status: "WAITING_EXTERNAL", handoff: { ...H, delivery, cancellation: "PENDING" } }))
    assert.ok(releaseBlock({ ...ok, founder: false, handoff: { ...H, delivery } }))
  }
  assert.ok(releaseBlock({ ...ok, status: "WAITING_EXTERNAL", handoff: { ...H, delivery: "DELIVERED" } }))
})

test("coding approval is what a fresh Stage C report says, otherwise unknown", () => {
  assert.deepEqual(approvalState(null).release, ["Not released", "neutral"])
  assert.equal(approvalState(H).release[1], "info")
  assert.equal(approvalState({ ...H, delivery: "REFUSED", last_error_code: "HTTP_409" }).release[0], "Released · delivery refused (HTTP_409)")
  const snap = (status: string) => ({ authorization: { status } })
  assert.equal(approvalState({ ...H, snapshot: snap("ACTIVE") }).coding[0], "Unknown: no fresh Stage C report")   // stale
  assert.deepEqual(approvalState({ ...H, freshness: "FRESH", snapshot: snap("ACTIVE") }).coding, ["Approved on Stage C", "success"])
  assert.equal(approvalState({ ...H, freshness: "FRESH", snapshot: snap("NONE") }).coding[1], "warning")
  assert.equal(approvalState({ ...H, freshness: "FRESH", snapshot: snap("EXPIRED") }).coding[1], "danger")
  assert.equal(approvalState({ ...H, freshness: "FRESH", snapshot: null }).coding[1], "warning")
})
