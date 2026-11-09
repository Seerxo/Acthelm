import assert from "node:assert/strict"
import { test } from "node:test"
import { mergedAwaitingAcceptance, taskStatusView } from "./qa-status.ts"

test("a REVIEW task whose PR is merged reads as merged, awaiting acceptance, never review or done", () => {
  for (const stage of ["MERGED", "VERIFYING", "MEASURED"]) {
    assert.equal(mergedAwaitingAcceptance("REVIEW", stage), true)
    assert.deepEqual(taskStatusView("REVIEW", stage), { value: "MERGED", label: "Merged · awaiting acceptance", tone: "info" })
  }
})

test("any other status or stage keeps its own status; missing reads unknown", () => {
  assert.deepEqual(taskStatusView("REVIEW", "READY_TO_MERGE"), { value: "REVIEW" })
  assert.deepEqual(taskStatusView("REVIEW", null), { value: "REVIEW" })
  assert.deepEqual(taskStatusView("DONE", "MERGED"), { value: "DONE" })
  assert.deepEqual(taskStatusView(null, "MERGED"), { value: "UNKNOWN" })
})

import { stageCOnHost } from "./qa-status.ts"
const host = (over: { source_sha?: string | null; api_sha?: string | null; status?: string; tasks?: unknown[] | null; one_task?: unknown; standing?: unknown } = {}) => ({
  runtime: { api_sha: over.api_sha ?? null, acceptance: { accepted_at: null, source_sha: over.source_sha ?? null },
    preflight: { status: over.status ?? "NOT_VERIFIED", checked_at: null } },
  authorizations: { one_task: "one_task" in over ? over.one_task : null, standing: "standing" in over ? over.standing : null },
  tasks: "tasks" in over ? over.tasks! : [],
}) as Parameters<typeof stageCOnHost>[0]

test("a host with no Stage C evidence (production) is not a Stage C host", () => {
  assert.equal(stageCOnHost(host()), false)
  assert.equal(stageCOnHost(host({ tasks: null })), false)
})

test("only current capability makes a Stage C host; history does not (founder rule 2026-11-09)", () => {
  const SHA = "c".repeat(40)
  assert.equal(stageCOnHost(host({ status: "PASS" })), true)
  assert.equal(stageCOnHost(host({ source_sha: SHA, api_sha: SHA })), true)
  assert.equal(stageCOnHost(host({ standing: { status: "ACTIVE" } })), true)
  assert.equal(stageCOnHost(host({ standing: undefined })), true)           // unreadable grant keeps Revoke reachable
  assert.equal(stageCOnHost(host({ source_sha: SHA, api_sha: "d".repeat(40) })), false)   // accepted for older code
  assert.equal(stageCOnHost(host({ tasks: [{}] })), false)                 // past tasks
  assert.equal(stageCOnHost(host({ status: "FAIL" })), false)              // recorded on every host
  assert.equal(stageCOnHost(host({ one_task: { status: "CONSUMED" } })), false)
})
