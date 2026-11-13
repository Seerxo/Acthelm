// Founder-selected execution priorities. Facts always come from the Acthelm catalog;
// this file never creates a project or treats a missing integration as running.
import type { DeskState, TaskRow } from "./api"

export const FOCUS_PROJECTS = [
  { key: "seerxo", label: "Seerxo", purpose: "Develop the existing SaaS and grow paid usage." },
  { key: "youtube", label: "YouTube Studio", purpose: "Prepare and review recurring video production." },
  { key: "etsy", label: "Etsy Commerce", purpose: "Research and prepare original Etsy products and drafts." },
] as const

type Catalog = DeskState["catalog"]
type Project = NonNullable<Catalog>["projects"][number]

function matches(key: string, p: Project): boolean {
  const id = p.key.toLowerCase(), name = p.name.toLowerCase()
  const unit = (p.business_unit ?? "").toLowerCase()
  if (key === "seerxo") return unit === "seerxo" || id === "seerxo" || id.startsWith("seerxo-")
  if (key === "youtube") return unit === "youtube" || id === "youtube" || id.startsWith("youtube-")
  // product-factory is not synonymous with Etsy: it may contain other product lines.
  return unit === "etsy" || /(^|[-_ ])etsy($|[-_ ])/.test(id) || /\betsy\b/.test(name)
}

const CLOSED = new Set(["DONE", "CANCELLED", "IDEA", "BACKLOG"])
const ATTENTION = new Set(["BLOCKED", "FAILED", "WAITING_APPROVAL"])

export function focusPortfolio(catalog: Catalog, tasks: TaskRow[]) {
  return FOCUS_PROJECTS.map(focus => {
    const projects = (catalog?.projects ?? []).filter(p => matches(focus.key, p))
    const keys = new Set(projects.map(p => p.key))
    const scoped = tasks.filter(t => t.project != null && keys.has(t.project))
    const openTasks = scoped.filter(t => !CLOSED.has(t.status))
    const attention = scoped.filter(t => ATTENTION.has(t.status))
    const goals = catalog ? catalog.goals.filter(g => g.project != null && keys.has(g.project)) : []
    return { ...focus, projects, openTasks, attention, goals }
  })
}
