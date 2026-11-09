import assert from "node:assert/strict"
import { test } from "node:test"
import type { Improvement } from "./api.ts"
import { releaseRows, runningVersion } from "./releases.ts"

const imp = (o: Partial<Improvement>): Improvement => ({ task: "T", title: "t", stage: "PR_OPEN", updated_at: null, repository: null, branch: null,
  pr_number: null, pr_state: null, ci_state: null, failed_check: null, repairs: null, review_verdict: null, risk: null, expected_effect: null,
  verified_at: null, ...o })

test("a PR number only for a verified publication; merged first, newest first; no PR, no row", () => {
  assert.equal(releaseRows(undefined), undefined)
  const rows = releaseRows([
    imp({ task: "A", pr_number: 5, pr_verified: false, stage: "MERGED", updated_at: "2026-11-01" }),
    imp({ task: "B", pr_number: 7, pr_verified: true, stage: "CI", ci_state: "SUCCESS", updated_at: "2026-11-03" }),
    imp({ task: "C", pr_number: 9, pr_verified: true, stage: "VERIFYING", updated_at: "2026-11-02" }),
    imp({ task: "D", stage: "IMPLEMENTING" }),
  ])!
  assert.deepEqual(rows.map(r => [r.task, r.pr, r.merged]), [["C", "#9", true], ["A", "unverified", true], ["B", "#7", false]])
  assert.ok(!rows.some(r => r.pr === "#5"))                   // an unverified number is never shown
  assert.deepEqual(rows[2].evidence, ["CI success"])           // the PR fact is the PR column, not repeated
})

test("the running version says unknown for what is missing", () => {
  assert.equal(runningVersion(undefined), null)
  const v = runningVersion({ api: { git_sha: "abcdef1234", started_at: "x" }, worker: { git_sha: null, image: null, started_at: null },
    migration: { database: null, code_head: "0042" }, stage_c_enabled: null, guardian_enabled: null, drift: [] })!
  assert.equal(v.api, "abcdef1"); assert.equal(v.worker, "unknown"); assert.equal(v.schema, "unknown"); assert.equal(v.schemaOk, false)
})
