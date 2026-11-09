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
const host = (over: { accepted_at?: string | null; checked_at?: string | null; tasks?: unknown[] | null; one_task?: unknown; standing?: unknown } = {}) => ({
  runtime: { acceptance: { accepted_at: over.accepted_at ?? null, source_sha: null }, preflight: { checked_at: over.checked_at ?? null } },
  authorizations: { one_task: "one_task" in over ? over.one_task : null, standing: "standing" in over ? over.standing : null },
  tasks: "tasks" in over ? over.tasks! : [],
})

test("a host with no Stage C evidence (production) is not a Stage C host", () => {
  assert.equal(stageCOnHost(host()), false)
  assert.equal(stageCOnHost(host({ tasks: null })), false)
})

test("any positive Stage C evidence, or an unreadable grant, makes it a Stage C host", () => {
  assert.equal(stageCOnHost(host({ accepted_at: "2026-11-09T00:00:00Z" })), true)
  assert.equal(stageCOnHost(host({ checked_at: "2026-11-09T00:00:00Z" })), true)
  assert.equal(stageCOnHost(host({ tasks: [{}] })), true)
  assert.equal(stageCOnHost(host({ standing: { status: "ACTIVE" } })), true)
  assert.equal(stageCOnHost(host({ standing: undefined })), true)
})
