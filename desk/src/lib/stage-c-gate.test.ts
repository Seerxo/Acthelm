// Stage C navigation is shown only where the backend proves Stage C exists. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import type { DeskState } from "./api.ts"
import { stageCPresent } from "./stage-c-gate.ts"
import { stageCOnHost as homeStageC } from "./dashboard.ts"
import { stageCOnHost as pageStageC } from "./qa-status.ts"
import { parseStageC } from "./stage-c.ts"

const SHA = "a".repeat(40)
// What production answers: the watchdog's failing preflight (recorded on every host), no acceptance, no tasks, no grants.
const prod = { version: 1, tasks: [], authorizations: { one_task: null, standing: null },
  runtime: { acceptance: { source_sha: null, accepted_at: null, valid: null },
    preflight: { status: "FAIL", checked_at: "2026-11-09T10:00:00Z", failed: ["preflight_unavailable"] } } }
const withRt = (rt: object) => ({ ...prod, runtime: { ...prod.runtime, ...rt } })

test("absent: no answer, or production's recorded-but-failing preflight, hides Stage C", () => {
  assert.equal(stageCPresent(undefined), false)
  assert.equal(stageCPresent(null), false)
  assert.equal(stageCPresent(prod), false)
  assert.equal(stageCPresent(withRt({ preflight: { status: "STALE", checked_at: "2026-11-09T10:00:00Z" } })), false)
  assert.equal(stageCPresent(prod, { runtime: { stage_c_enabled: false } } as DeskState), false)
})

test("history alone hides Stage C: an old acceptance, finished tasks, settled grants (founder rule 2026-11-09)", () => {
  const old = withRt({ api_sha: "b".repeat(40), acceptance: { source_sha: SHA, accepted_at: "2026-10-23T09:00:00Z", valid: false } })
  assert.equal(stageCPresent(old), false)                                              // accepted for another commit
  assert.equal(stageCPresent({ ...prod, tasks: [{ key: "TASK-0077" }] }), false)       // past pilot task
  for (const status of ["CONSUMED", "EXPIRED", "REVOKED"])
    assert.equal(stageCPresent({ ...prod, authorizations: { one_task: { status }, standing: null } }), false)
})

test("current capability shows Stage C: running-code acceptance, passing preflight, live grant, or the flag", () => {
  assert.equal(stageCPresent(withRt({ api_sha: SHA, acceptance: { source_sha: SHA, accepted_at: "2026-11-09T09:00:00Z", valid: true } })), true)
  assert.equal(stageCPresent(withRt({ preflight: { status: "PASS" } })), true)
  assert.equal(stageCPresent({ ...prod, authorizations: { one_task: { status: "ACTIVE" }, standing: null } }), true)
  assert.equal(stageCPresent({ ...prod, authorizations: { one_task: null, standing: "garbled" } }), true)  // unreadable: keep Revoke
  assert.equal(stageCPresent(undefined, { runtime: { stage_c_enabled: true } } as DeskState), true)
})

test("error or junk: hidden, never a crash", () => {
  for (const junk of ["<html>", 42, [], true]) assert.equal(stageCPresent(junk), false)
  const hostile = new Proxy({}, { get() { throw new Error("boom") } })
  assert.equal(stageCPresent(hostile), false)
})

test("write flag alone agrees in nav, Home and Stage C despite failed preflight", () => {
  for (const flag of [true, false, null, undefined, "true"]) {
    const raw = withRt({ write_enabled: flag })
    const parsed = parseStageC(raw)
    const expected = flag === true
    assert.equal(stageCPresent(raw), expected)
    assert.equal(homeStageC(parsed), expected ? parsed : null)
    assert.equal(pageStageC(parsed), expected)
  }
})
