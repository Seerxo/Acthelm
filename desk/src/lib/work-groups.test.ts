import assert from "node:assert/strict"
import { test } from "node:test"
import { arrived, closedScope, isOpen, pinned, searchScope, signals } from "./work-groups.ts"

const t = (priority = "P2", risk = "LOW", key = priority + risk) => ({ key, priority, risk })
const overview = { overview: true, searching: false }

test("Needs attention can never be closed, not even by a manual choice", () => {
  assert.equal(isOpen("attention", [], { ...overview, manual: false }), true)
})

test("other groups start closed, even with P0 work; their P0 tasks stay visible as pinned rows", () => {
  assert.equal(isOpen("review", [t("P1"), t("P0")], overview), false)
  assert.deepEqual(pinned([t("P1", "LOW", "a"), t("P0", "LOW", "b")]).map(x => x.key), ["b"])
})

test("a search or a single-group filter opens every group shown; otherwise the founder's choice wins", () => {
  assert.equal(isOpen("next", [t()], { overview: true, searching: true, manual: false }), true)
  assert.equal(isOpen("next", [t()], { overview: false, searching: false, manual: false }), true)
  assert.equal(isOpen("next", [t()], { ...overview, manual: true }), true)
  assert.equal(isOpen("next", [t("P0")], { ...overview, manual: false }), false)   // P0 rows still show: see pinned
})

test("a closed header names priority and risk; new arrivals are counted against the first render", () => {
  assert.deepEqual(signals([t("P0", "HIGH", "a"), t("P1", "LOW", "b"), t("P1", "LOW", "c"), t("P3", "LOW", "d")]), ["1 P0", "2 P1", "1 high risk"])
  assert.deepEqual(signals([t()]), [])
  assert.equal(arrived([{ key: "A" }, { key: "B" }], new Set(["A"])), 1)
})

test("a capped closed history says how much is loaded, and an empty search says what it did not search", () => {
  assert.equal(closedScope(100, 250), "showing newest 100 of 350")
  assert.equal(closedScope(40), null)
  // An active-only match (closed group not rendered) still shows the scope notice when history is truncated.
  assert.equal(searchScope(true, true, 100, 250), "Search covers the newest 100 closed tasks; 250 older are not included.")
  assert.equal(searchScope(false, true, 100, 250), null)
  assert.equal(searchScope(true, false, 100, 250), null)   // Active filter: closed tasks are not in scope
  assert.equal(searchScope(true, true, 40, 0), null)
})
