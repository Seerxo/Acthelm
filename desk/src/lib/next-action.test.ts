import assert from "node:assert/strict"
import { test } from "node:test"
import { primaryAction } from "./next-action.ts"

const none = { accept: false, approve: false, authorize: false, ready: false, readyBlocked: false, assign: false, followUp: false }

test("exactly one primary action, in flow order", () => {
  assert.equal(primaryAction(none), null)
  assert.equal(primaryAction({ ...none, accept: true, assign: true, followUp: true }), "accept")
  assert.equal(primaryAction({ ...none, approve: true, assign: true }), "approve")
  // A code task without authorization: Authorize first; Mark READY is offered but blocked.
  assert.equal(primaryAction({ ...none, authorize: true, ready: true, readyBlocked: true, assign: true }), "authorize")
  // Once authorized, READY is the next step.
  assert.equal(primaryAction({ ...none, authorize: true, ready: true, assign: true }), "ready")
  assert.equal(primaryAction({ ...none, assign: true }), "assign")
  assert.equal(primaryAction({ ...none, followUp: true }), "follow-up")
})
