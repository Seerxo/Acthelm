import assert from "node:assert/strict"
import { test } from "node:test"
import { focusPortfolio } from "./focus-portfolio"
import type { DeskState, TaskRow } from "./api"

const catalog: NonNullable<DeskState["catalog"]> = {
  projects: [
    { key: "seerxo-app", name: "Seerxo SaaS", business_unit: "seerxo" },
    { key: "youtube-auto", name: "YouTube Auto", business_unit: "youtube" },
    { key: "etsy-commerce", name: "Etsy Commerce", business_unit: "product-factory" },
    { key: "kdp", name: "KDP Books", business_unit: "product-factory" },
  ],
  goals: [{ id: 1, title: "Grow subscribers", status: "PROPOSED", project: "seerxo-app" }],
}
const task = (project: string, status: string): TaskRow => ({
  key: `TASK-${project}-${status}`, title: status, status, priority: "P2", risk: "LOW",
  agent: null, project, business_unit: null, approval_required: false,
  created_at: "2026-11-13T00:00:00Z", updated_at: "2026-11-13T00:00:00Z",
})

test("shows three ordered focus areas and only their registered project work", () => {
  const groups = focusPortfolio(catalog, [
    task("seerxo-app", "READY"), task("seerxo-app", "DONE"),
    task("youtube-auto", "WAITING_APPROVAL"), task("etsy-commerce", "BACKLOG"),
    task("kdp", "BLOCKED"),
  ])
  assert.deepEqual(groups.map(g => g.key), ["seerxo", "youtube", "etsy"])
  assert.deepEqual(groups.map(g => g.projects.map(p => p.key)),
    [["seerxo-app"], ["youtube-auto"], ["etsy-commerce"]])
  assert.deepEqual(groups.map(g => g.openTasks.length), [1, 1, 0])
  assert.deepEqual(groups.map(g => g.attention.length), [0, 1, 0])
  assert.deepEqual(groups.map(g => g.goals.length), [1, 0, 0])
})

test("missing catalog never invents projects or goals", () => {
  const groups = focusPortfolio(undefined, [task("seerxo-app", "READY")])
  for (const group of groups) {
    assert.equal(group.projects.length, 0)
    assert.equal(group.openTasks.length, 0)
    assert.equal(group.goals.length, 0)
  }
})

test("other product factory lines are not mislabeled as Etsy", () => {
  const groups = focusPortfolio({ projects: [catalog.projects[3]], goals: [] }, [task("kdp", "READY")])
  assert.equal(groups[2].projects.length, 0)
})
