// The Work list's three founder lanes (plus not-scheduled), built on lib/task-story.ts groups so filters and routes
// (#/tasks/attention, ...) keep working: what needs you, what is running, what is done. Code tasks carry PR/CI chips
// from /desk/state improvements, recorded facts only.
import type { Improvement, Tone } from "./api.ts"
import { GROUPS, groupOf, type Group } from "./task-story.ts"

export type Lane = "needs" | "running" | "backlog" | "done"
export const LANES: { key: Lane; label: string; tone: Tone; groups: Group[] }[] = [
  { key: "needs", label: "Needs you", tone: "danger", groups: ["attention", "review"] },
  { key: "running", label: "Running", tone: "info", groups: ["running", "next", "waiting"] },
  { key: "backlog", label: "Not scheduled", tone: "neutral", groups: ["backlog"] },
  { key: "done", label: "Done", tone: "neutral", groups: ["closed"] },
]

export const laneOf = (status: string): Lane => LANES.find(l => l.groups.includes(groupOf(status)))!.key

/** Inside a lane: the more urgent group first (attention before review), then priority, then newest. */
export function laneSort<T extends { status: string; priority: string; updated_at: string }>(tasks: T[]): T[] {
  const rank = (s: string) => GROUPS.findIndex(g => g.key === groupOf(s))
  return [...tasks].sort((a, b) => rank(a.status) - rank(b.status) || a.priority.localeCompare(b.priority) || b.updated_at.localeCompare(a.updated_at))
}

/** PR and CI chips for a code task's row; nothing when the mission records neither. */
export function codeChips(i: Pick<Improvement, "pr_number" | "ci_state" | "failed_check" | "pr_verified"> | undefined): { label: string; tone: Tone }[] {
  if (!i) return []
  const ci = i.ci_state
  return [
    // A number only for a verified publication; otherwise the PR exists but its number is not vouched for.
    i.pr_number != null ? (i.pr_verified === true ? { label: `PR #${i.pr_number}`, tone: "neutral" as Tone }
      : { label: "PR unverified", tone: "warning" as Tone }) : null,
    ci ? { label: `CI ${ci.toLowerCase().replaceAll("_", " ")}`,
      tone: (i.failed_check || /fail/i.test(ci) ? "danger" : /success/i.test(ci) ? "success" : "info") as Tone } : null,
  ].filter((x): x is { label: string; tone: Tone } => !!x)
}
