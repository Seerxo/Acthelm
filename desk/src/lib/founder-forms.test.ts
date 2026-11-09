import assert from "node:assert/strict"
import { test } from "node:test"
import { codeTaskBody, goalBody } from "./founder-forms.ts"

const ALLOWED = ["app/goals.py", "tests/test_goals.py"]
const ok = { title: " Fix rounding ", objective: "Round once", success: "Tests pass", paths: ["app/goals.py"], context: "" }

test("code task payload", () => {
  const r = codeTaskBody({ ...ok, paths: ["app/goals.py", "app/goals.py", "tests/test_goals.py"], context: "app/models.py\n app/models.py, tests/test_metrics.py" }, ALLOWED)
  assert.deepEqual(r, { error: null, body: { title: "Fix rounding", objective: "Round once", success_criteria: "Tests pass",
    paths: ["app/goals.py", "tests/test_goals.py"], context: ["app/models.py", "tests/test_metrics.py"] } })
  assert.deepEqual(codeTaskBody(ok, ALLOWED).body?.context, [])
})

test("code task refusals", () => {
  for (const f of [{ title: "x" }, { objective: "" }, { success: "  " }, { paths: [] }, { paths: ["app/main.py"] },
    { context: "README.md" }, { context: "app/../x.py" }, { context: "app/goals.py" },
    { context: Array.from({ length: 11 }, (_, i) => `app/m${i}.py`).join("\n") }])
    assert.ok(codeTaskBody({ ...ok, ...f }, ALLOWED).error, JSON.stringify(f))
})

test("goal payload", () => {
  assert.deepEqual(goalBody({ title: " Reach 10k ", metric: "views", target: "10000", baseline: "", project: "youtube-auto" }),
    { error: null, body: { title: "Reach 10k", metric: "views", target: "10000", project: "youtube-auto" } })
  assert.deepEqual(goalBody({ title: "Reach 10k", metric: "views", target: "10000", baseline: "4000", project: "" }).body,
    { title: "Reach 10k", metric: "views", target: "10000", baseline: "4000" })
  for (const f of [{ title: "ab" }, { metric: "v" }, { target: "" }, { baseline: "x".repeat(101) }])
    assert.ok(goalBody({ title: "Reach 10k", metric: "views", target: "1", baseline: "", project: "", ...f }).error)
})
