import assert from "node:assert/strict"
import { test } from "node:test"
import { acceptBody, acceptEvidence, acceptOffer, prLink, rowMerged } from "./accept.ts"

const URL = "https://github.com/seerxo/acthelm/pull/77"
const HEAD = "b11b0aa" + "1".repeat(33), MERGE = "e".repeat(40)
const pr = (over = {}) => ({ verified: true, merged: true, number: 77, url: URL, commit: HEAD, merge_commit: MERGE, ...over })
const t = (over = {}) => ({ key: "TASK-0008", status: "REVIEW", context: { write: true }, result: "r", pull_request: pr(), ...over })

test("evidence: merged PR URL and merge commit, else the head commit", () => {
  assert.equal(acceptEvidence(t()), `${URL} merged as ${MERGE}`)
  assert.equal(acceptEvidence(t({ pull_request: pr({ merge_commit: null }) })), `${URL} at head ${HEAD}`)
  assert.deepEqual(acceptBody("e"), { status: "DONE", actor: "founder", evidence: "e" })   // no reason: it would hide the evidence
})

test("offered for a merged verified PR in REVIEW only", () => {
  assert.equal(acceptOffer(t())!.merged, true)
  for (const over of [{ status: "DONE" }, { status: "IN_PROGRESS" }, { pull_request: pr({ merged: false }) },
    { pull_request: pr({ verified: false, url: undefined, number: undefined }) }, { pull_request: null }])
    assert.equal(acceptOffer(t(over)), null, JSON.stringify(over))
})

test("a REVIEW task without a PR is accepted explicitly, with its result", () => {
  const plain = t({ context: null, pull_request: undefined })
  assert.equal(acceptOffer(plain)!.merged, false)
  assert.match(acceptOffer(plain)!.evidence, /TASK-0008/)
  assert.equal(acceptOffer({ ...plain, result: null }), null)
})

test("the link is only the backend-verified one", () => {
  assert.equal(prLink(t())!.state, "verified")
  assert.equal(prLink(t({ pull_request: pr({ verified: false }) }))!.state, "unverified")
  assert.equal(prLink(t({ pull_request: pr({ url: "javascript:alert(1)" }) }))!.state, "unverified")
  assert.equal(prLink(t({ pull_request: null })), null)
})

test("a list row reads merged only for a recorded merge on a verified PR in REVIEW", () => {
  assert.equal(rowMerged("REVIEW", { stage: "MERGED", pr_verified: true }), true)
  assert.equal(rowMerged("REVIEW", { stage: "VERIFYING", pr_verified: true }), true)
  for (const [status, i] of [["REVIEW", { stage: "MERGED", pr_verified: false }], ["REVIEW", { stage: "MERGED" }],
    ["REVIEW", { stage: "READY_TO_MERGE", pr_verified: true }], ["DONE", { stage: "MERGED", pr_verified: true }], ["REVIEW", undefined]] as const)
    assert.equal(rowMerged(status, i), false, JSON.stringify([status, i]))
})
