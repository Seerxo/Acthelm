// The founder's IA (2026-11-09): eight places (Releases added 2026-11-09); no destination is lost and old links still resolve. `npm test`.
import assert from "node:assert/strict"
import { test } from "node:test"
import { ALIASES, LEGACY_SHORTCUTS, PAGES, SECTIONS, pageTitle, reachable, resolvePage, sectionOf, visibleTabs } from "./nav.ts"

test("the IA places in the founder's order, each page reachable from exactly one", () => {
  assert.deepEqual(SECTIONS.map(s => s.label), ["Dashboard", "Goals", "Tasks", "Agents", "Approvals", "Releases", "Metrics", "System"])
  for (const s of SECTIONS) assert.equal(s.tabs[0], s.home, s.key)
  for (const p of PAGES.filter(p => p !== "project"))
    assert.equal(SECTIONS.filter(s => s.home === p || s.tabs.includes(p)).length, 1, p)
  assert.equal(reachable().length + 1, PAGES.length)   // all but the per-unit project page
})

test("retired routes resolve to their new home; unknown routes do not", () => {
  assert.equal(resolvePage("needs"), "approvals")
  assert.equal(resolvePage("tasks"), "tasks")
  assert.equal(resolvePage("nope"), null)
  assert.equal(resolvePage("home"), "overview")
  assert.equal(resolvePage("system"), "health")
  assert.equal(resolvePage("work"), "tasks")
  // IA names that are not page ids
  assert.equal(resolvePage("dashboard"), "overview")
  assert.equal(resolvePage("goals"), "projects")
  assert.equal(resolvePage("metrics"), "growth")
  assert.equal(resolvePage("releases"), "releases")   // a page of its own since 2026-11-09, no longer an alias to System status
  assert.ok(!("releases" in ALIASES))
  for (const target of Object.values(ALIASES)) assert.ok(PAGES.includes(target))
})

test("go-to shortcuts are unique letters and titles stand alone", () => {
  assert.equal(new Set(SECTIONS.map(s => s.shortcut)).size, SECTIONS.length)
  for (const s of SECTIONS) assert.match(s.shortcut, /^[a-z]$/)
  for (const p of PAGES) assert.ok(pageTitle(p))
})

test("every page that used to be a sidebar link is still a tab of exactly one place", () => {
  for (const p of ["overview", "approvals", "tasks", "agents", "automation", "activity", "projects", "finance", "growth", "health", "incidents", "memory", "releases"] as const)
    assert.equal(SECTIONS.filter(s => s.tabs.includes(p)).length, 1, p)
  assert.equal(sectionOf("project").key, "goals")     // a unit's page lives beside Portfolio
  assert.equal(sectionOf("approvals").key, "approvals")
})

test("Stage C is listed only where the environment is proven; its route still resolves everywhere", () => {
  const tasks = SECTIONS.find(s => s.key === "tasks")!
  assert.deepEqual(visibleTabs(tasks, false), ["tasks", "activity"])
  assert.deepEqual(visibleTabs(tasks, true), ["tasks", "stage-c", "activity"])
  assert.equal(resolvePage("stage-c"), "stage-c")
})

test("legacy go-to keys G H and G W still land on Dashboard and Tasks, and never shadow a section key", () => {
  assert.equal(LEGACY_SHORTCUTS.h, "overview")
  assert.equal(LEGACY_SHORTCUTS.w, "tasks")
  for (const k of Object.keys(LEGACY_SHORTCUTS)) assert.ok(!SECTIONS.some(s => s.shortcut === k), k)
})
