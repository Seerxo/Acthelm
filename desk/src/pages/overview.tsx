// Home, the founder's operations view. Five-second test: system status, active agents, work in progress and the
// decisions expected from the founder. Composition: the honest headline (lib/operating.ts) with the one primary action
// and the kill switch, the one-line CEO command field, a status strip, then two columns: operations on the left
// (executing, up next, finished, autonomous work), decisions and alerts on the right. Cards with nothing to show are
// not drawn: they become one quiet line at the end, beside the sources the API did not report ("Not reported: …").
// Every value reads only /desk/state and /stage-c/state through lib/dashboard.ts; nothing missing is shown as 0.
import type { ReactNode } from "react"
import { ArrowRight } from "lucide-react"
import { operating, healthSummary, systemChecks } from "@/lib/operating"
import { shellStatus } from "@/lib/shell-status"
import { useData, useDesk, useNow, useStale, type Page } from "@/state"
import type { Tone } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Dot, TEXT } from "@/components/desk"
import { Button } from "@/components/ui/button"
import { AgentsSwitch } from "@/components/controls"
import { CommandComposer } from "@/components/command-composer"
import { AlertsCard, AutonomyCard, BudgetCard, FinishedCard, GoalsCard, NeedsCard, NextCard, NowCard } from "@/components/dashboard/home"
import { agentRows, autonomy, costs, doingNow, needsYou, recentlyFinished, runningVersion, stageCOnHost, upNext } from "@/lib/dashboard"
import { nextAction } from "@/lib/stage-c"
import { useStageC } from "@/components/dashboard/use-stage-c"

const toNeeds = () => document.getElementById("needs-you")?.scrollIntoView({ block: "start" })

function Header({ op }: { op: ReturnType<typeof operating> }) {
  const { go, reload } = useDesk()
  const act = () => {
    const t = op.primary.target
    if (t === "brief") document.getElementById("ceo-command")?.focus()   // the command field below
    else if (t.page === "needs-you") toNeeds()
    else if (t.page === "refresh") void reload()
    else go(t.page as Page, t.key)
  }
  // One sentence, one primary action, and the kill switch as a secondary button.
  return (
    <header aria-labelledby="operating-state" className="flex min-w-0 flex-wrap items-center justify-between gap-x-8 gap-y-4">
      <h1 id="operating-state" className="t-page flex min-w-0 items-center gap-3">
        <Dot tone={op.tone} className="size-2.5" />{op.headline}
      </h1>
      <div className="flex flex-wrap items-center gap-3">
        <AgentsSwitch className="px-3.5 max-sm:size-11" iconOnPhone />
        <Button size="lg" className="max-sm:h-11" onClick={act}>{op.primary.label}<ArrowRight /></Button>
      </div>
    </header>
  )
}

interface Vital { label: string; value: string | null; sub: string; tone?: Tone; onClick: () => void }

/** The four 5-second facts in one strip: label, one big number, one short line. null reads "unknown", never 0. */
function Vitals({ items }: { items: Vital[] }) {
  return (
    <section aria-label="Company at a glance" className="panel grid grid-cols-2 overflow-hidden lg:grid-cols-4">
      {items.map((v, i) => (
        <button key={v.label} type="button" onClick={v.onClick}
          className={cn("grid min-w-0 content-start gap-1 px-4 py-4 text-left transition-colors duration-150 hover:bg-surface focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring sm:px-6 sm:py-5",
            i % 2 === 1 && "border-l", i > 1 && "max-lg:border-t", i === 2 && "lg:border-l")}>
          <span className="t-meta font-medium">{v.label}</span>
          {/* The number stays ink; its state is a small dot + text on the line below (colour never carries it alone). */}
          <span className={cn(v.value === null ? "text-base leading-8 font-medium" : "metric")}>{v.value ?? "unknown"}</span>
          <span className={cn("t-meta flex min-w-0 items-center gap-1.5", v.tone && v.tone !== "neutral" && TEXT[v.tone])}>
            {(v.value === null || (v.tone && v.tone !== "neutral")) && <Dot tone={v.value === null ? "warning" : v.tone!} className="size-1.5" />}
            <span className="truncate">{v.sub}</span></span>
        </button>
      ))}
    </section>
  )
}

/** Empty and unreported sources, grouped into one quiet line instead of a card each. */
function Quiet({ calm, missing }: { calm: string[]; missing: string[] }) {
  if (!calm.length && !missing.length) return null
  return (
    <p role="status" className="t-meta flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-4">
      {calm.map(c => <span key={c} className="inline-flex items-center gap-1.5"><Dot tone="neutral" className="size-1.5" />{c}</span>)}
      {!!missing.length && <span className="inline-flex min-w-0 items-center gap-1.5"><Dot tone="warning" className="size-1.5" />
        <span className="min-w-0 [overflow-wrap:anywhere]"><span className="font-medium text-foreground/80">Not reported:</span> {missing.join(", ")}</span></span>}
    </p>
  )
}

/** On phones the two columns collapse into one list in the founder's order (operations, decisions, alerts, then the
 *  rest); `order` does that without rendering anything twice. */
const Slot = ({ order, children }: { order: string; children: ReactNode }) => <div className={cn("min-w-0", order, "xl:order-none")}>{children}</div>

export function Overview() {
  const d = useData()
  const { go } = useDesk()
  const stale = useStale()
  const now = useNow("minute")
  const read = useStageC()
  const sc = stageCOnHost(read.state)   // null on hosts without Stage C (production): its parts are hidden, not broken
  // One list for the header, the "Decisions for you" vital and the panel below, so their counts always agree.
  const needs = needsYou(d, sc)
  const n = needs.items.length
  const op = operating(d, { stale, now, decisions: needs.items })

  // The status strip: the same sources as System, Agents and the cards below, so nothing disagrees.
  const checks = systemChecks(d, now), health = healthSummary(systemChecks(d, now))
  const agents = agentRows(d, now)
  const worker = shellStatus(d, now).find(f => f.key === "worker")!
  const working = doingNow(d.tasks).length
  const critical = needs.items.some(i => i.level === "critical")

  // What has content is drawn; the rest is one quiet line.
  const pr = sc ? autonomy(d, sc).lastPr : null
  const runs = (d.run_series ?? []).reduce((t, x) => t + x.SUCCEEDED + x.FAILED + x.ESCALATED + x.STARTED, 0)
  const hasFinished = recentlyFinished(d.tasks).length > 0 || runs > 0 || !!pr
  const version = runningVersion(d)
  const hasNext = upNext(d.tasks).length > 0 || !!d.current_priority || !!(sc && nextAction(sc))
  const r = d.revenue
  const hasGoals = (r != null && (r.mrr ?? r.month) != null) || (d.portfolio ?? []).some(u => u.metrics.length)
  const cost = costs(d, sc)
  const hasCost = cost.some(c => c.value !== null)
  const calm = [!hasFinished && "Nothing finished recently, no agent runs in 14 days", !hasNext && "Nothing queued",
    !hasGoals && r === null && "No verified revenue source"].filter(Boolean) as string[]
  const missing = [...needs.unknown, r === undefined && "revenue", d.portfolio === undefined && "business metrics",
    d.current_priority === undefined && "current priority", (!version || (!version.api && !version.worker)) && "running version",
    ...cost.filter(c => c.value === null).map(c => c.label.toLowerCase())].filter(Boolean) as string[]

  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:gap-8">
      <Header op={op} />
      <CommandComposer />
      <Vitals items={[
        { label: "System status", tone: health.tone === "success" ? "success" : health.tone === "danger" ? "danger" : "warning",
          value: !checks.length ? null : health.open.length ? `${health.open.length}/${checks.length}` : "OK",
          sub: !checks.length ? "No checks reported" : health.open.length ? "checks not verified" : `All ${checks.length} checks verified`, onClick: () => go("health") },
        { label: "Active agents", value: String(agents.counts.running), tone: d.control.agents_paused ? "warning" : undefined,
          sub: d.control.agents_paused ? "Agents paused" : `Worker: ${worker.value}`, onClick: () => go("agents") },
        { label: "In progress", value: String(working), sub: working === 1 ? "task executing" : "tasks executing", onClick: () => go("tasks") },
        // An unreported queue with nothing else listed is "unknown", never 0; a partial list says so.
        { label: "Decisions for you", value: !n && needs.unknown.length ? null : String(n), tone: critical ? "danger" : n ? "warning" : undefined,
          sub: needs.unknown.length ? "May be incomplete" : n ? (critical ? "Critical first" : "Waiting on you") : "Nothing waits on you", onClick: toNeeds },
      ]} />
      <div className="grid min-w-0 grid-cols-1 items-start gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(360px,440px)]">
        <div className="contents xl:grid xl:min-w-0 xl:content-start xl:gap-6">
          <Slot order="order-1"><NowCard /></Slot>
          {hasNext && <Slot order="order-4"><NextCard sc={sc} /></Slot>}
          {hasFinished && <Slot order="order-5"><FinishedCard sc={sc} /></Slot>}
          {sc && <Slot order="order-6"><AutonomyCard sc={sc} read={read} /></Slot>}
        </div>
        <div className="contents xl:grid xl:min-w-0 xl:content-start xl:gap-6">
          <Slot order="order-2"><div id="needs-you" className="scroll-mt-4"><NeedsCard sc={sc} /></div></Slot>
          <Slot order="order-3"><AlertsCard /></Slot>
          {hasGoals && <Slot order="order-7"><GoalsCard /></Slot>}
          {hasCost && <Slot order="order-8"><BudgetCard sc={sc} /></Slot>}
        </div>
      </div>
      <Quiet calm={calm} missing={missing} />
    </div>
  )
}
