// GET /stage-c/state as the Desk reads it (app/stage_c_state.py). Break caught: missing or wrong-typed evidence reads
// as success, CI on other bytes reads green, a mismatched fingerprint reads approved, null tokens read 0, an untrusted
// string becomes a link, or a sign-out is treated as an ordinary error. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { ApiError } from "./api.ts"
import { freshLabel, isStale } from "./freshness.ts"
import { reconcilerView } from "./reconciler.ts"
import { systemChecks } from "./operating.ts"
import { toneOf } from "./format.ts"
import type { DeskState } from "./api.ts"
import {
  canRevokeStanding, ciView, grantView, isRevoked, lastReadyPr, loadError, na, nextAction, offerRevokeOnFailure, parseStageC, prHref, readFreshness, reviewView,
  steps, standingRevokeMessage, testView, workerReadyView, preflightView, privacyGateLabel, acceptanceView, driftView, blockerLabel, PIPELINE, TASK_STATUS, CLOCK_SKEW_MS, STALE_MS,
} from "./stage-c.ts"

const SHA = "c".repeat(40), HEAD = "1".repeat(40), OLD = "2".repeat(40), FP = "a".repeat(64), FP2 = "b".repeat(64)
const NOW = "2026-11-04T10:00:00+00:00"

const taskRaw = (over: Record<string, unknown> = {}) => ({
  key: "TASK-0101", status: "REVIEW", stage: "READY_TO_MERGE", attempts: 1, retry_count: 0, repairs: 0,
  started_at: NOW, updated_at: NOW, test_verdict: "PASSED", reviewer_verdict: "APPROVED",
  tested_fingerprint: FP, reviewed_fingerprint: FP, fingerprint_match: true,
  history: ["DETECTED", ...PIPELINE.map(([k]) => k)].slice(0,
    ["DETECTED", ...PIPELINE.map(([k]) => k)].indexOf(String(over.stage ?? "READY_TO_MERGE")) + 1).map(stage => ({ stage, at: NOW })),
  pr: { number: 61, url: "https://github.com/Seerxo/Acthelm/pull/61", head_sha: HEAD }, ci: { state: "SUCCESS", sha: HEAD },
  last_outcome: "PR_OPEN", failure_category: null, needs_founder: true, ...over,
})
const raw = (over: Record<string, unknown> = {}) => ({
  version: 1, generated_at: NOW, state: "READY", blockers: ["task_needs_founder"],
  next_action: "review_task", last_ready_pr: { task_key: "TASK-0101", number: 61, head_sha: HEAD, stage: "READY_TO_MERGE" },
  runtime: { api_sha: SHA, worker_sha: SHA, reasoner_sha: null, sha_agree: null, worker_heartbeat: "HEALTHY", write_enabled: true, worker_ready: true,
    agents_paused: false, acceptance: { source_sha: SHA, accepted_at: NOW, valid: true },
    preflight: { status: "PASS", checked_at: NOW, source_sha: SHA },
    host_facts: { host_checks: "NOT_VERIFIED", reasoner_sha: "NOT_VERIFIED", api_sha: "RECORDED", worker_sha: "RECORDED" } },
  authorizations: { one_task: null, standing: { source_sha: SHA, paths: ["tests/test_metrics.py"], expires_at: NOW, max_per_day: 2,
    used_today: 0, remaining_today: 2, revoked: false, issuer: "founder", status: "ACTIVE" } },
  tasks: [taskRaw()],
  metrics: { tasks: { started: 1, reached_ready_to_merge: 1, stopped: 0, in_progress: 0, abandoned: 0 }, attempts: { total: 1, failed: 0 },
    model_calls: { total: 2 }, minutes: 10, tokens: null, usd_recorded: 0, usd_note: "excludes_subscription_cost",
    quota_stops: 0, truncated: false },
  ...over,
})

test("a complete ready-to-merge task: every step up to merge done, approved, CI passed on the PR head, PR link built from the number", () => {
  const s = parseStageC(raw())
  assert.equal(s.state, "READY")
  const [t] = s.tasks!
  assert.deepEqual(steps(t).map(x => x.state), [...Array(7).fill("completed"), "pending"])   // merging is the founder's
  assert.ok(steps(parseStageC(raw({ tasks: [taskRaw({ stage: "MERGED", status: "DONE" })] })).tasks![0]).every(x => x.state === "completed"))
  assert.equal(reviewView(t).badge, "READY")
  assert.equal(testView(t).badge, "READY")
  assert.deepEqual(ciView(t), { kind: "passed", label: "CI passed on PR head", tone: "success", sha: HEAD })
  assert.equal(prHref(t.pr!.number), "https://github.com/Seerxo/Acthelm/pull/61")
  assert.equal(lastReadyPr(s)?.task_key, "TASK-0101")
  assert.equal(preflightView(s.runtime.preflight).badge, "READY")
  assert.deepEqual(driftView(s.runtime), { badge: "NOT_VERIFIED", label: "API = worker; reasoner not verified" })
})

test("matching API and worker commits do not verify the reasoner; known drift and missing commits stay distinct", () => {
  for (const [api_sha, worker_sha, sha_agree, badge, label] of [
    [SHA, SHA, null, "NOT_VERIFIED", "API = worker; reasoner not verified"],
    [SHA, OLD, false, "BLOCKED", "Drift: API and worker differ"],
    [SHA, OLD, null, "BLOCKED", "Drift: API and worker differ"],
    [SHA, SHA, false, "BLOCKED", "SHA disagreement reported"],
    [null, SHA, null, "UNKNOWN", "Unknown"],
    [SHA, null, null, "UNKNOWN", "Unknown"],
  ] as const) {
    const rt = parseStageC(raw({ runtime: { ...raw().runtime, api_sha, worker_sha, sha_agree } })).runtime
    assert.deepEqual(driftView(rt), { badge, label })
  }
})

test("a running task is in progress at its stage; later steps pending, nothing claimed done after it", () => {
  const s = parseStageC(raw({ state: "ACTIVE", blockers: [], next_action: "none", tasks: [taskRaw({ status: "IN_PROGRESS", stage: "IMPLEMENTING", pr: null, ci: null,
    reviewer_verdict: null, test_verdict: null, needs_founder: false })] }))
  assert.equal(s.state, "ACTIVE")
  const [t] = s.tasks!
  assert.deepEqual(steps(t).map(x => x.state), ["completed", "in_progress", "pending", "pending", "pending", "pending", "pending", "pending"])
  assert.equal(ciView(t).kind, "none")
  assert.equal(reviewView(t).badge, "UNKNOWN")
  assert.equal(nextAction(s), null)
})

test("blocked and failed tasks fail at their stage and the backend state is shown as given", () => {
  const blocked = parseStageC(raw({ state: "BLOCKED", tasks: [taskRaw({ status: "BLOCKED", stage: "CI", failure_category: "REPAIR_INTERRUPTED" })] }))
  assert.equal(blocked.state, "BLOCKED")
  assert.equal(steps(blocked.tasks![0]).find(x => x.key === "CI")!.state, "failed")
  assert.equal(blocked.tasks![0].failure_category, "REPAIR_INTERRUPTED")
  const failed = parseStageC(raw({ state: "FAILED", tasks: [taskRaw({ status: "FAILED", stage: "TESTING", test_verdict: "FAILED" })] }))
  assert.equal(failed.state, "FAILED")
  assert.equal(steps(failed.tasks![0])[2].state, "failed")
  assert.equal(testView(failed.tasks![0]).badge, "FAILED")
})

test("privacy gate codes explain a blocked publication without implying successful verification", () => {
  for (const [code, label] of [
    ["PRIVACY_GATE_BLOCKED", "Privacy check blocked publication"],
    ["PRIVACY_GATE_ERROR", "Privacy verification could not complete"],
  ]) {
    const [t] = parseStageC(raw({ state: "BLOCKED", tasks: [taskRaw({ status: "BLOCKED", stage: "INDEPENDENT_REVIEW",
      last_outcome: code, failure_category: code, test_verdict: null, reviewer_verdict: null })] })).tasks!
    assert.equal(t.failure_category, code)
    assert.equal(t.last_outcome, code)
    assert.equal(privacyGateLabel(code), label)
    assert.notEqual(steps(t).find(s => s.key === "INDEPENDENT_REVIEW")!.state, "completed")
    assert.equal(reviewView(t).badge, "UNKNOWN")
  }
  assert.equal(privacyGateLabel("OTHER"), null)
})

test("expired and revoked standing grants never read active", () => {
  const exp = parseStageC(raw({ state: "EXPIRED", blockers: ["standing_expired"],
    authorizations: { one_task: null, standing: { ...raw().authorizations.standing, status: "EXPIRED" } } }))
  assert.equal(exp.state, "EXPIRED")
  assert.equal(grantView(exp.authorizations.standing).badge, "EXPIRED")
  assert.equal(blockerLabel("standing_expired"), "Standing authorization: expired")
  const rev = parseStageC(raw({ state: "BLOCKED", blockers: ["standing_revoked"], next_action: "issue_authorization", authorizations: { one_task: null, standing: {
    ...raw().authorizations.standing, status: "REVOKED", revoked: true, used_today: null, remaining_today: null } } }))
  const st = rev.authorizations.standing!
  assert.ok(isRevoked(st))
  assert.deepEqual(grantView(st), { badge: "BLOCKED", label: "Revoked" })
  assert.equal(na(st.remaining_today), "n/a")
  assert.match(nextAction(rev)!, /authorization/)
})

test("missing, partial or wrong-typed fields become UNKNOWN / NOT VERIFIED / not reported, never success", () => {
  for (const input of [null, undefined, "x", [], 42, {}]) {
    const s = parseStageC(input)
    assert.equal(s.state, "UNKNOWN")
    assert.equal(s.version_ok, false)
    assert.equal(s.blockers, null)                       // not reported, not "no blockers"
    assert.equal(s.tasks, null)
    assert.equal(s.runtime.preflight.status, "UNKNOWN")
    assert.equal(preflightView(s.runtime.preflight).badge, "UNKNOWN")
    assert.equal(acceptanceView(s.runtime.acceptance.valid).badge, "NOT_VERIFIED")
    assert.equal(workerReadyView(s.runtime.worker_ready).badge, "NOT_VERIFIED")
    assert.equal(driftView(s.runtime).badge, "UNKNOWN")
    assert.equal(s.runtime.host_facts, "NOT_VERIFIED")
    assert.equal(s.metrics.tokens, null)
    assert.equal(s.authorizations.standing, undefined)
    assert.ok(nextAction(s))
  }
  const s = parseStageC(raw({ state: "GREEN", version: 2, runtime: { api_sha: "abc", worker_sha: 7, sha_agree: "yes", write_enabled: "true",
    worker_ready: 1, preflight: { status: "OK" }, host_facts: "VERIFIED" }, tasks: [{ stage: "SHIPPED", status: 3, test_verdict: "OK",
    reviewer_verdict: "LGTM", fingerprint_match: "true", pr: { number: "61", head_sha: "HEAD" }, ci: { state: "passed", sha: HEAD } }] }))
  assert.equal(s.state, "UNKNOWN")
  assert.equal(s.version_ok, false)
  assert.equal(s.runtime.write_enabled, null)          // a string "true" is not the flag
  assert.equal(s.runtime.host_facts, "NOT_VERIFIED")
  const [t] = s.tasks!
  assert.equal(t.stage, "UNKNOWN")
  assert.ok(steps(t).every(x => x.state === "unknown"))
  assert.equal(t.test_verdict, null)
  assert.equal(reviewView(t).badge, "UNKNOWN")
  assert.equal(t.pr!.number, null)
  assert.equal(prHref(t.pr!.number), null)
  assert.equal(ciView(t).kind, "unknown")
})

test("the write flag on is never readiness", () => {
  const s = parseStageC(raw({ runtime: { ...raw().runtime, write_enabled: true, worker_ready: null, preflight: { status: "NOT_VERIFIED" } } }))
  assert.equal(workerReadyView(s.runtime.worker_ready).badge, "NOT_VERIFIED")
  assert.equal(preflightView(s.runtime.preflight).badge, "NOT_VERIFIED")
})

test("CI on an outdated SHA is never success, whether the backend says STALE or the SHAs disagree", () => {
  const stale = parseStageC(raw({ tasks: [taskRaw({ ci: { state: "STALE", sha: OLD } })] })).tasks![0]
  assert.equal(ciView(stale).label, "CI on outdated SHA")
  const contradicted = parseStageC(raw({ tasks: [taskRaw({ ci: { state: "SUCCESS", sha: OLD } })] })).tasks![0]
  assert.equal(ciView(contradicted).kind, "outdated")
  const noSha = parseStageC(raw({ tasks: [taskRaw({ ci: { state: "SUCCESS", sha: null } })] })).tasks![0]
  assert.equal(ciView(noSha).kind, "outdated")
  assert.equal(ciView(parseStageC(raw({ tasks: [taskRaw({ ci: { state: null, sha: null } })] })).tasks![0]).kind, "none")
})

test("a mismatched fingerprint is not verified, even if the backend's match flag or verdict says otherwise", () => {
  const unverified = parseStageC(raw({ tasks: [taskRaw({ reviewer_verdict: "UNVERIFIED", fingerprint_match: false, reviewed_fingerprint: FP2 })] })).tasks![0]
  assert.equal(reviewView(unverified).badge, "NOT_VERIFIED")
  const lying = parseStageC(raw({ tasks: [taskRaw({ reviewer_verdict: "APPROVED", fingerprint_match: true, reviewed_fingerprint: FP2 })] })).tasks![0]
  assert.equal(reviewView(lying).badge, "NOT_VERIFIED")
  const short = parseStageC(raw({ tasks: [taskRaw({ tested_fingerprint: "a".repeat(10) })] })).tasks![0]
  assert.equal(short.tested_fingerprint, null)
  assert.equal(reviewView(short).badge, "NOT_VERIFIED")
})

test("a rejecting reviewer is a failure", () => {
  const t = parseStageC(raw({ tasks: [taskRaw({ reviewer_verdict: "REJECTED", fingerprint_match: null, stage: "IMPLEMENTING", status: "READY" })] })).tasks![0]
  assert.deepEqual(reviewView(t), { badge: "FAILED", label: "Rejected" })
})

test("null tokens and USD read n/a, never 0; a recorded 0 stays 0", () => {
  const m = parseStageC(raw()).metrics
  assert.equal(na(m.tokens), "n/a")
  assert.equal(m.usd_recorded, 0)
  const empty = parseStageC(raw({ metrics: { tokens: "120", usd_recorded: null, attempts: {}, tasks: {} } })).metrics
  assert.equal(na(empty.tokens), "n/a")
  assert.equal(na(empty.usd_recorded), "n/a")
  assert.equal(na(empty.tasks.started), "n/a")
  assert.equal(empty.model_calls, null)
})

test("untrusted strings never become links or markup-bearing codes", () => {
  const evil = parseStageC(raw({
    last_ready_pr: { task_key: "<script>", number: "javascript:alert(1)", stage: "MERGED" },
    blockers: ["<script>alert(1)</script>", "javascript:alert(1)", "sha_mismatch"],
    tasks: [taskRaw({ key: "<script>", status: "<b>x</b>", last_outcome: "javascript:alert(1)", failure_category: "<img onerror=x>",
      pr: { number: "javascript:alert(1)", url: "javascript:alert(1)", head_sha: "<script>" } })],
    authorizations: { one_task: { task_key: "javascript:x", status: "ACTIVE", issuer: "<script>" }, standing: {
      ...raw().authorizations.standing, paths: ["tests/test_metrics.py", "javascript:alert(1)", "<script>x</script>", "../etc/passwd"] } },
  }))
  assert.deepEqual(evil.blockers, ["unrecognised_blocker", "unrecognised_blocker", "sha_mismatch"])
  const [t] = evil.tasks!
  assert.equal(t.key, null)
  assert.equal(t.status, "UNKNOWN")
  assert.equal(t.last_outcome, null)
  assert.equal(t.failure_category, null)
  assert.equal(t.pr!.number, null)
  assert.equal(t.pr!.head_sha, null)
  assert.equal(prHref(t.pr!.number), null)
  assert.equal(prHref("javascript:alert(1)"), null)
  assert.equal(prHref(-1), null)
  assert.equal(prHref(1.5), null)
  assert.equal(lastReadyPr(evil), null)
  assert.equal(evil.authorizations.one_task!.task_key, null)
  assert.equal(evil.authorizations.one_task!.issuer, "UNKNOWN")
  assert.deepEqual(evil.authorizations.standing!.paths, ["tests/test_metrics.py"])
  // The backend's url is never used: a foreign repository cannot be linked even with a valid number.
  const foreign = parseStageC(raw({ tasks: [taskRaw({ pr: { number: 7, url: "https://github.com/attacker/x/pull/7", head_sha: HEAD } })] }))
  assert.equal(prHref(foreign.tasks![0].pr!.number), "https://github.com/Seerxo/Acthelm/pull/7")
})

test("a stale or failed read carries the lib/freshness stale marker; no server time is unknown", () => {
  const s = parseStageC(raw())
  const at = Date.parse(NOW)
  assert.equal(freshLabel(readFreshness(s, at, at + 10_000, false)), "")
  assert.ok(isStale(readFreshness(s, at, at + 91_000, false)))
  assert.ok(isStale(readFreshness(s, at, at + 1_000, true)))
  assert.equal(freshLabel(readFreshness(parseStageC({}), at, at, false)), "unverified")
})

test("401 and the Access sign-in page go to the shell's sign-out handling; 403 and 404 do not", () => {
  assert.equal(loadError(new ApiError(401, "founder authentication required")), "signed_out")
  assert.equal(loadError(new ApiError(0, "Sign in again, then reload", true)), "signed_out")
  assert.equal(loadError(new ApiError(403, "forbidden")), "forbidden")
  assert.equal(loadError(new ApiError(404, "Not Found")), "unavailable")
  assert.equal(loadError(new ApiError(0, "Invalid API response", false)), "error")
  assert.equal(loadError(new TypeError("Failed to fetch")), "error")
})

test("a failed initial state read offers emergency Revoke only for an error or unavailable endpoint", () => {
  for (const [kind, offered] of [["error", true], ["unavailable", true], ["signed_out", false], ["forbidden", false]] as const)
    assert.equal(offerRevokeOnFailure(kind), offered, kind)
})

test("the standing revoke toast reports whether a live grant was actually revoked", () => {
  assert.equal(standingRevokeMessage({ revoked: true }), "Standing authorization revoked")
  assert.equal(standingRevokeMessage({ revoked: false }), "No live standing grant — nothing was revoked")
  for (const malformed of [undefined, null, {}, { revoked: null }, { revoked: "false" }, { revoked: 1 }])
    assert.throws(() => standingRevokeMessage(malformed), /Invalid standing revoke response/)
})

test("pipeline completion requires recorded history, task status and evidence, not just READY_TO_MERGE", () => {
  const parse = (over: Record<string, unknown>) => parseStageC(raw({ tasks: [taskRaw(over)] })).tasks![0]
  const bare = parseStageC({ tasks: [{ stage: "READY_TO_MERGE" }] }).tasks![0]
  assert.ok(steps(bare).every(s => s.state === "unknown"))
  for (const history of [undefined, null, [], "bad", [{ stage: "READY_TO_MERGE" }],
    [{ stage: "UNKNOWN", at: NOW }], [{ stage: "IMPLEMENTING", at: NOW }],
    [{ stage: "PLANNED", at: NOW }, { stage: "READY_TO_MERGE", at: "2020-01-01T00:00:00Z" }]]) {
    assert.ok(steps(parse({ history })).every(s => s.state === "unknown"), JSON.stringify(history))
  }
  const truncated = steps(parse({ history: [{ stage: "READY_TO_MERGE", at: NOW }] }))
  assert.ok(truncated.slice(0, 6).every(s => s.state === "unknown"))
  for (const over of [{ test_verdict: null }, { test_verdict: "FAILED" }, { reviewer_verdict: null },
    { reviewer_verdict: "UNVERIFIED" }, { reviewer_verdict: "REJECTED" }, { fingerprint_match: false },
    { reviewed_fingerprint: FP2 }, { tested_fingerprint: null }, { pr: null }, { pr: { number: 0, head_sha: HEAD } },
    { pr: { number: 61, head_sha: null } }, { ci: null }, { ci: { state: "SUCCESS", sha: OLD } },
    { ci: { state: "SUCCESS", sha: null } }, { ci: { state: "STALE", sha: HEAD } },
    { ci: { state: "FAILURE", sha: HEAD } }, { ci: { state: "PENDING", sha: HEAD } }]) {
    const pipeline = steps(parse(over))
    assert.equal(pipeline[6].state, "not_verified", JSON.stringify(over))
  }
  assert.equal(steps(parse({ test_verdict: "FAILED" }))[2].state, "not_verified")
  assert.equal(steps(parse({ reviewed_fingerprint: FP2 }))[3].state, "not_verified")
  assert.equal(steps(parse({ pr: null }))[4].state, "not_verified")
  assert.equal(steps(parse({ ci: { state: "SUCCESS", sha: OLD } }))[5].state, "not_verified")
  // A repair revisits implementation: the earlier test/review/CI history cannot complete the new attempt.
  const repair = parse({ stage: "IMPLEMENTING", status: "IN_PROGRESS", history: [...taskRaw().history, { stage: "IMPLEMENTING", at: NOW }] })
  assert.equal(steps(repair)[1].state, "in_progress")
  assert.ok(steps(repair).slice(2).every(s => s.state === "pending"))
})

test("explicit null standing grant means absent; missing or malformed grants are UNKNOWN with Revoke available", () => {
  const absent = parseStageC(raw({ authorizations: { standing: null } })).authorizations.standing
  assert.equal(absent, null)
  assert.equal(grantView(absent).label, "None issued")
  assert.equal(canRevokeStanding(absent), false)
  for (const authorizations of [undefined, null, [], {}, { standing: undefined }, { standing: 42 }, { standing: "none" }, { standing: [] }, { standing: {} }]) {
    const g = parseStageC(raw({ authorizations })).authorizations.standing
    assert.notEqual(g, null)
    assert.deepEqual(grantView(g), { badge: "UNKNOWN", label: "Unknown" })
    assert.equal(canRevokeStanding(g), true)
  }
  for (const [status, revoked, available] of [["REVOKED", true, false], ["REVOKED", null, true], ["ACTIVE", true, true]] as const) {
    const g = parseStageC(raw({ authorizations: { standing: { ...raw().authorizations.standing, status, revoked } } })).authorizations.standing
    assert.equal(canRevokeStanding(g), available)
  }
})

test("only the backend task-status enum is accepted; unsupported success-like statuses are UNKNOWN", () => {
  for (const status of TASK_STATUS) {
    const t = parseStageC(raw({ tasks: [taskRaw({ status })] })).tasks![0]
    assert.equal(t.status, status)
  }
  for (const status of ["APPROVED", "SUCCESS", "COMPLETED", "HEALTHY", "NEW_STATUS", "review", null, undefined, 7]) {
    const t = parseStageC(raw({ tasks: [taskRaw({ status })] })).tasks![0]
    assert.equal(t.status, "UNKNOWN")
    assert.notEqual(toneOf(t.status), "success")
    assert.ok(steps(t).every(s => s.state !== "completed"))
  }
})

test("guidance uses the backend next_action allowlist without inferring or reordering blockers", () => {
  const blockers = ["standing_daily_limit", "task_needs_founder", "sha_mismatch"]
  const s = parseStageC(raw({ blockers, next_action: "wait_daily_reset" }))
  assert.deepEqual(s.blockers, blockers)
  assert.equal(nextAction(s), "Wait for the standing authorization's daily limit to reset")
  for (const next_action of ["check_deploy", "check_worker", "redeploy_one_sha", "resume_agents", "accept_host", "enable_code_write",
    "run_preflight", "review_task", "issue_authorization"]) {
    const parsed = parseStageC(raw({ blockers: [], next_action }))
    assert.equal(parsed.next_action, next_action)
    assert.ok(nextAction(parsed))
  }
  assert.equal(nextAction(parseStageC(raw({ blockers, next_action: "none" }))), null)
  for (const next_action of [undefined, null, 7, "issue_another_grant", "<script>"]) {
    const parsed = parseStageC(raw({ next_action }))
    assert.equal(parsed.next_action, "UNKNOWN")
    assert.equal(nextAction(parsed), "Next action unknown: not reported or unrecognised")
  }
})

test("MERGED_OBSERVED renders CI, ready-to-merge and merged as observed, never verified completion", () => {
  for (const sha of [HEAD, OLD, null]) {
    for (const history of [taskRaw({ stage: "MERGED" }).history, [{ stage: "MERGED", at: NOW }], null]) {
      const t = parseStageC(raw({ tasks: [taskRaw({ stage: "MERGED", status: "DONE", history, ci: { state: "MERGED_OBSERVED", sha } })] })).tasks![0]
      assert.equal(t.ci!.state, "MERGED_OBSERVED")
      assert.deepEqual(ciView(t), { kind: "observed", label: "Observed after merge", tone: "neutral", sha })
      assert.deepEqual(steps(t).slice(5).map(s => [s.key, s.state]), [["CI", "observed"], ["READY_TO_MERGE", "observed"], ["MERGED", "observed"]])
    }
  }
})

test("fresh retrieval of an old generated_at is stale; future clock skew has a bounded allowance", () => {
  const at = Date.parse(NOW)
  const old = parseStageC(raw({ generated_at: "2020-01-01T00:00:00Z" }))
  assert.equal(readFreshness(old, at, at, false).freshness, "STALE")
  const dated = (ms: number) => parseStageC(raw({ generated_at: new Date(ms).toISOString() }))
  assert.equal(readFreshness(dated(at - STALE_MS), at, at, false).freshness, "VERIFIED")
  assert.equal(readFreshness(dated(at - STALE_MS - 1), at, at, false).freshness, "STALE")
  assert.equal(readFreshness(dated(at + CLOCK_SKEW_MS), at, at, false).freshness, "VERIFIED")
  assert.equal(readFreshness(dated(at + CLOCK_SKEW_MS + 1), at, at, false).freshness, "UNKNOWN")
  assert.equal(readFreshness(dated(at + CLOCK_SKEW_MS + 1), at, at + CLOCK_SKEW_MS + 1, false).freshness, "UNKNOWN")
  assert.equal(readFreshness(parseStageC(raw()), at, at - 1, false).freshness, "UNKNOWN")
  assert.equal(readFreshness(parseStageC(raw({ generated_at: "invalid" })), at, at, false).freshness, "UNKNOWN")
})

test("business reconciler / scheduler pause overrides a completed heartbeat on Automation and Health", () => {
  const at = Date.parse(NOW)
  const completed = { outcome: "COMPLETED", last_attempt_at: NOW, last_completed_at: NOW, freshness: "VERIFIED", expected_cadence_s: 600 }
  assert.equal(reconcilerView(completed, at).label, "COMPLETED")
  for (const e of [completed, null]) {
    const view = reconcilerView(e, at, true)
    assert.equal(view.label, "PAUSED")
    assert.notEqual(view.tone, "success")
  }
  for (const [agents_paused, value] of [[true, "PAUSED"], [false, "Running"]] as const) {
    const state = { control: { agents_paused }, generated_at: NOW, incidents: [], health: { resources: {}, services: [] },
      reconciler_execution: completed } as unknown as DeskState
    const check = systemChecks(state, at).find(c => c.key === "reconciler")!
    assert.equal(check.value, value)
    assert.equal(check.tone, agents_paused ? "warning" : "success")
  }
})

test("runtime reasoner SHA and last ready PR use the emitted contract, including tasks outside the recent list", () => {
  const s = parseStageC(raw({ tasks: [], runtime: { ...raw().runtime, reasoner_sha: SHA } }))
  assert.equal(s.runtime.reasoner_sha, SHA)
  assert.equal(lastReadyPr(s)?.number, 61)
  assert.equal(lastReadyPr(parseStageC(raw({ last_ready_pr: null }))), null)
  assert.equal(lastReadyPr(parseStageC(raw({ last_ready_pr: { number: 61, stage: "UNTRUSTED" } }))), null)
  assert.equal(parseStageC(raw({ runtime: { reasoner_sha: "short" } })).runtime.reasoner_sha, null)
})

test("preflight failures and code task paths are codes and plain paths only", () => {
  const s = parseStageC(raw({ code_task_paths: ["app/goals.py", "../etc", "https://x"],
    runtime: { ...raw().runtime, preflight: { status: "FAIL", checked_at: NOW, source_sha: SHA, failed: ["launcher", "bad name", 3] } } }))
  assert.deepEqual(s.runtime.preflight.failed, ["launcher"])
  assert.deepEqual(s.code_task_paths, ["app/goals.py"])
  assert.equal(parseStageC(raw()).runtime.preflight.failed, null)
  assert.equal(parseStageC(raw()).code_task_paths, null)
})

test("revoke toasts follow the API's answer; minutes clamp only when sent", async () => {
  const { pilotRevokeMessage, standingRevokeMessage, clampMinutes } = await import("./stage-c.ts")
  assert.match(pilotRevokeMessage({ revoked: true }), /revoked$/)
  assert.match(pilotRevokeMessage({ revoked: false }), /nothing was revoked/)
  assert.match(standingRevokeMessage({ revoked: false }), /nothing was revoked/)
  for (const bad of [{}, { revoked: "yes" }, null]) assert.throws(() => pilotRevokeMessage(bad))
  assert.deepEqual(["", " ", "abc", "0", "5", "5.6", "90"].map(clampMinutes), [null, null, null, 1, 5, 6, 60])
})
