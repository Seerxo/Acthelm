// Home, the founder's command center: the whole company in one screen. The honest headline (lib/operating.ts) with one
// action and the kill switch, the one place to tell the CEO what to do, then compact cards in order of urgency: what
// needs you, the agents, autonomous (Stage C) work, goals and business numbers, recorded cost, and alerts. Each card
// reads only /desk/state and /stage-c/state through lib/dashboard.ts; anything not reported reads "unknown".
import { ArrowRight } from "lucide-react"
import { operating } from "@/lib/operating"
import { useData, useDesk, useNow, useStale, type Page } from "@/state"
import { Dot } from "@/components/desk"
import { Button } from "@/components/ui/button"
import { AgentsSwitch } from "@/components/controls"
import { CommandComposer } from "@/components/command-composer"
import { AlertsCard, AutonomyCard, BudgetCard, FinishedCard, GoalsCard, NeedsCard, NextCard, NowCard } from "@/components/dashboard/home"
import { needsYou, stageCOnHost } from "@/lib/dashboard"
import { useStageC } from "@/components/dashboard/use-stage-c"

function Header({ op }: { op: ReturnType<typeof operating> }) {
  const { go, reload } = useDesk()
  const act = () => {
    const t = op.primary.target
    if (t === "brief") document.getElementById("ceo-command")?.focus()   // the composer right below
    else if (t.page === "needs-you") document.getElementById("needs-you")?.scrollIntoView({ block: "start" })
    else if (t.page === "refresh") void reload()
    else go(t.page as Page, t.key)
  }
  // One sentence, one action, and the kill switch.
  return (
    <header aria-labelledby="operating-state" className="flex min-w-0 flex-wrap items-center justify-between gap-x-8 gap-y-3">
      <h1 id="operating-state" className="flex min-w-0 items-center gap-3 text-[26px] leading-9 font-semibold tracking-[-0.045em] sm:text-[30px]">
        <Dot tone={op.tone} className="size-2.5" />{op.headline}
      </h1>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="lg" className="h-10 px-4 max-sm:h-11" onClick={act}>{op.primary.label}<ArrowRight /></Button>
        <AgentsSwitch className="px-3.5 max-sm:size-11" iconOnPhone />
      </div>
    </header>
  )
}

export function Overview() {
  const d = useData()
  const stale = useStale()
  const now = useNow("minute")
  const read = useStageC()
  const sc = stageCOnHost(read.state)   // null on hosts without Stage C (production): its parts are hidden, not broken
  const base = operating(d, { stale, now })
  // operating() does not count PRs to merge, results to accept or Stage C blockers; never say "normal" above them.
  const n = needsYou(d, sc).items.length
  const op = base.level === "normal" && n ? { ...base, tone: "warning" as const, headline: `${n} ${n === 1 ? "item needs" : "items need"} you`,
    primary: { label: "See what needs you", target: { page: "needs-you" } } } : base
  // The founder's order: what runs now, what finished, what waits on you, what comes next; then supporting facts.
  // Mobile: one column in that order. Desktop: a two-column grid (three on very wide screens).
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:gap-8">
      <Header op={op} />
      <CommandComposer />
      <div className="grid min-w-0 grid-cols-1 items-start gap-6 lg:grid-cols-2 2xl:grid-cols-3">
        <NowCard />
        <FinishedCard sc={sc} />
        <div id="needs-you" className="min-w-0 scroll-mt-4"><NeedsCard sc={sc} /></div>
        <NextCard sc={sc} />
        {sc && <AutonomyCard sc={sc} read={read} />}
        <GoalsCard />
        <BudgetCard sc={sc} />
        <AlertsCard />
      </div>
    </div>
  )
}
