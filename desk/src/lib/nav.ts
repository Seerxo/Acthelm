// Navigation model: the founder's information architecture (2026-11-09): Dashboard, Goals, Tasks, Agents, Approvals,
// Metrics, System. Each place holds existing pages; nothing was removed. Releases has no page of its own yet: the
// running version and the "not reported" release gateway live on System status, so #/releases resolves there.
// Every #/page route stays a deep link; retired and IA names resolve through ALIASES (nav.test.ts).

export const PAGES = ["overview", "approvals", "tasks", "agents", "projects", "project", "finance", "growth",
  "health", "incidents", "automation", "stage-c", "activity", "memory"] as const
export type Page = (typeof PAGES)[number]

/** Routes from earlier Desk versions (bookmarks, notification links) and where they live now. */
export const ALIASES: Record<string, Page> = {
  needs: "approvals", home: "overview", system: "health", work: "tasks",
  // IA names (founder 2026-11-09) that are not page ids.
  dashboard: "overview", goals: "projects", metrics: "growth", settings: "health", releases: "health",
}

export function resolvePage(raw: string): Page | null {
  if ((PAGES as readonly string[]).includes(raw)) return raw as Page
  return ALIASES[raw] ?? null
}

export type SectionKey = "dashboard" | "goals" | "tasks" | "agents" | "approvals" | "metrics" | "system"
/** `shortcut`: the second key of the "g <key>" go-to shortcut. `tabs` always starts with `home`. */
export interface NavSection { key: SectionKey; label: string; home: Page; tabs: Page[]; shortcut: string }

export const SECTIONS: NavSection[] = [
  { key: "dashboard", label: "Dashboard", home: "overview", tabs: ["overview"], shortcut: "d" },
  { key: "goals", label: "Goals", home: "projects", tabs: ["projects"], shortcut: "g" },
  { key: "tasks", label: "Tasks", home: "tasks", tabs: ["tasks", "stage-c", "activity"], shortcut: "t" },
  { key: "agents", label: "Agents", home: "agents", tabs: ["agents", "automation"], shortcut: "a" },
  { key: "approvals", label: "Approvals", home: "approvals", tabs: ["approvals"], shortcut: "p" },
  { key: "metrics", label: "Metrics", home: "growth", tabs: ["growth", "finance"], shortcut: "m" },
  { key: "system", label: "System", home: "health", tabs: ["health", "incidents", "memory"], shortcut: "s" },
]

/** Pages that exist only where the backend proves it (lib/stage-c-gate.ts). Hidden from navigation otherwise; the
 *  route itself still opens, and its page reports what the API says. */
export const GATED: Partial<Record<Page, "stage-c">> = { "stage-c": "stage-c" }

/** The pages a section shows, without gated pages whose environment is not proven. */
export const visibleTabs = (s: NavSection, stageC: boolean): Page[] => s.tabs.filter(p => !GATED[p] || stageC)

export const PAGE_LABEL: Record<Page, string> = {
  overview: "Overview", approvals: "Approvals", tasks: "Tasks", agents: "Agents", projects: "Portfolio", project: "Project",
  finance: "Finance", growth: "Growth", health: "Status", incidents: "Incidents", automation: "Automation", "stage-c": "Stage C",
  activity: "Activity", memory: "Memory",
}

/** A page's name where its section is not around it (palette, page header). It matches the sidebar label, so the
 *  page a link opens carries the name that was clicked; only Status reads longer once out of its Trust section. */
export const pageTitle = (p: Page): string =>
  ({ health: "System status" } as Partial<Record<Page, string>>)[p] ?? PAGE_LABEL[p]

/** The section a route belongs to; a project detail page lives under Goals, beside Portfolio. */
export function sectionOf(page: Page): NavSection {
  if (page === "project") return SECTIONS.find(s => s.key === "goals")!
  return SECTIONS.find(s => s.home === page || s.tabs.includes(page)) ?? SECTIONS[0]
}

/** Every page a person can navigate to from the sidebar / section tabs (the project page is reached per unit). */
export const reachable = (): Page[] => [...new Set(SECTIONS.flatMap(s => [s.home, ...s.tabs]))]
