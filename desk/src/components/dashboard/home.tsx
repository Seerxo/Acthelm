// Home's sections, one compact card each, in the founder's reading order: now, finished, decisions, next, then the
// supporting facts. Every card reads only /desk/state and /stage-c/state (lib/dashboard.ts); a source that was not
// reported says "unknown", never 0 or "none". Stage C parts render only where stageCOnHost proves it runs.
import { useState, type ReactNode } from "react"
import { ArrowRight, ChevronRight, ExternalLink } from "lucide-react"
import type { Metric, Tone } from "@/lib/api"
import { ago, fmtNum, humanize, metricName } from "@/lib/format"
import { freshOf, staleSuffix, dataSummary } from "@/lib/freshness"
import { healthSummary, systemChecks, vitals } from "@/lib/operating"
import { useOpenNeed } from "@/lib/needs"
import { retryEventId } from "@/lib/needs-pure"
import { nextAction, readFreshness, type StageCState } from "@/lib/stage-c"
import type { StageCRead } from "./use-stage-c"
import { agentRows, autonomy, BUCKET_TONE, costs, doingNow, needsYou, recentAlerts, recentlyFinished, runningVersion, upNext, type NeedItem } from "@/lib/dashboard"
import { cn } from "@/lib/utils"
import { useData, useDesk, useNow, type Page } from "@/state"
import { Dot, Panel, PanelEmpty, PanelRow, Status } from "@/components/desk"
import { ApprovalDecision, RetryDelivery } from "@/components/controls"
import { RunsChart } from "@/components/charts"

const TONE: Record<NeedItem["level"], Tone> = { critical: "danger", warning: "warning", info: "info" }
const UNKNOWN = "unknown"
const LEVEL_LABEL: Record<NeedItem["level"], string> = { critical: "Critical", warning: "Needs action", info: "To review" }

// --------------------------------------------------------------------------- shared bits
const fresh = (m?: { freshness?: string | null } | null): Tone => (freshOf(m) === "VERIFIED" ? "success" : "warning")
const More = ({ children, onClick }: { children: ReactNode; onClick: () => void }) => (
  <button type="button" onClick={onClick} className="inline-flex min-h-11 items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
    {children}<ArrowRight className="size-3" aria-hidden />
  </button>
)

/** A labelled figure. `null` reads "unknown" in a warning colour: absent is never shown as zero. */
function Fig({ label, value, tone, sub }: { label: string; value: ReactNode | null; tone?: Tone; sub?: ReactNode }) {
  const missing = value === null
  return (
    <div className="grid min-w-0 content-start gap-0.5 px-4 py-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("num truncate text-lg leading-7 font-semibold tracking-[-0.02em]", missing && "text-sm font-medium text-warning",
        !missing && tone === "danger" && "text-destructive", !missing && tone === "warning" && "text-warning")}>{missing ? UNKNOWN : value}</dd>
      {sub && <dd className="truncate text-xs text-muted-foreground">{sub}</dd>}
    </div>
  )
}
const Figs = ({ children, cols = 2 }: { children: ReactNode; cols?: 2 | 4 }) =>
  <dl className={cn("grid grid-cols-2 divide-border/70", cols === 4 && "sm:grid-cols-4")}>{children}</dl>

// --------------------------------------------------------------------------- 1. Now
/** What the system is doing: tasks executing, then each agent's state and current task. The shell header already
 *  shows the agent, worker and approval counts, so this card lists who does what instead of counting again. */
export function NowCard() {
  const d = useData()
  const { go, open } = useDesk()
  const now = useNow("minute")
  const tasks = doingNow(d.tasks)
  const { rows } = agentRows(d, now)
  const busy = new Set(tasks.map(t => t.key))
  const agents = rows.filter(r => !(r.task && busy.has(r.task.key)))   // an agent shown with its task above is not repeated
  return (
    <Panel title="Now" description="What the system is doing" action={<More onClick={() => go("agents")}>Agents</More>}>
      {tasks.slice(0, 4).map(t => (
        <PanelRow key={t.key} onClick={() => open({ kind: "task", key: t.key })} label={`${t.title}, in progress`} className="min-h-11 py-2.5"
          lead={<Dot tone="info" live />} title={t.title}
          meta={<>{t.agent ?? "Unassigned"} · updated {ago(t.updated_at)} ago</>} trail={<Status value="IN_PROGRESS" tone="info" />} />
      ))}
      {agents.slice(0, 5).map(r => (
        <PanelRow key={r.key} onClick={() => open({ kind: "agent", key: r.key })} label={`${r.name}, ${r.label}`} className="min-h-11 py-2"
          lead={<Dot tone={r.tone} live={r.live} />} title={r.name}
          meta={<>{r.task ? r.task.title : "No current task"} · {r.lastRun ? `last run ${ago(r.lastRun)} ago` : "no run recorded"}</>}
          trail={<Status value={r.label} label={humanize(r.label)} tone={r.tone ?? BUCKET_TONE[r.bucket]} />} />
      ))}
      {!tasks.length && !rows.length && <PanelEmpty title="Nothing executing">No task is in progress and the API reported no agents.</PanelEmpty>}
    </Panel>
  )
}

// --------------------------------------------------------------------------- 2. Finished
export function FinishedCard({ sc }: { sc: StageCState | null }) {
  const d = useData()
  const { go, open } = useDesk()
  const done = recentlyFinished(d.tasks)
  const pr = sc ? autonomy(d, sc).lastPr : null
  return (
    <Panel title="Recently finished" action={<More onClick={() => go("activity")}>Activity</More>}>
      {pr && <PrRow pr={pr} />}
      {done.map(t => (
        <PanelRow key={t.key} onClick={() => open({ kind: "task", key: t.key })} className="min-h-11 py-2.5" lead={<Dot tone="success" />}
          title={t.title} meta={<>{t.agent ?? "Unassigned"} · done {ago(t.updated_at)} ago</>} />
      ))}
      {!done.length && !pr && <PanelEmpty title="Nothing finished recently">No task in this read is done yet.</PanelEmpty>}
      {d.run_series?.length ? <div className="px-4 py-3"><RunsChart series={d.run_series} height="h-28" /></div> : null}
    </Panel>
  )
}

function PrRow({ pr }: { pr: NonNullable<ReturnType<typeof autonomy>["lastPr"]> }) {
  return <a href={pr.href} target="_blank" rel="noopener noreferrer"
    className="flex min-h-11 w-full items-center gap-x-3 px-4 py-2.5 transition-colors hover:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring">
    <span className="flex w-4 shrink-0 justify-center"><Dot tone={pr.stage === "MERGED" ? "success" : "warning"} /></span>
    <span className="min-w-0 flex-1 text-[14px] leading-5 font-medium">PR #{pr.number} · {humanize(pr.stage)}{pr.taskKey && <span className="font-normal text-muted-foreground"> · {pr.taskKey}</span>}</span>
    <ExternalLink className="size-3 shrink-0" aria-hidden /><span className="sr-only">(opens GitHub)</span>
  </a>
}

// --------------------------------------------------------------------------- 3. Decisions: needs you
const SHOW = 6

export function NeedsCard({ sc }: { sc: StageCState | null }) {
  const d = useData()
  const { go, open } = useDesk()
  const openNeed = useOpenNeed()
  const { items, unknown } = needsYou(d, sc)
  const [all, setAll] = useState(false)
  const shown = all ? items : items.slice(0, SHOW)
  const openItem = (it: NeedItem) => {
    if (it.need) return openNeed(it.need)
    if ("href" in it.target) return
    if (it.target.page === "tasks" && it.target.key) open({ kind: "task", key: it.target.key })
    else go(it.target.page as Page)
  }
  return (
    <Panel title="Needs you" count={items.length}
      description={unknown.length ? <span className="text-warning">May be incomplete: {unknown.join(", ")} {UNKNOWN}</span> : undefined}
      action={<More onClick={() => go("approvals")}>Approvals</More>}>
      {shown.length ? shown.map(it => {
        const a = it.need?.kind === "APPROVAL" && it.need.target.page === "approvals" ? d.approvals.find(x => x.key === it.need!.target.key) : undefined
        const retry = it.need ? retryEventId(it.need) : null
        const lead = <Dot tone={TONE[it.level]} />
        // The level in words too, never by colour alone.
        const meta = <><span className="font-medium text-foreground/80">{LEVEL_LABEL[it.level]}</span> · {it.detail}{it.since && <> · {ago(it.since)} ago</>}</>
        if ("href" in it.target) return (
          <a key={it.id} href={it.target.href} target="_blank" rel="noopener noreferrer"
            className="flex min-h-11 w-full items-start gap-x-3 px-4 py-2.5 transition-colors hover:bg-muted/50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring">
            <span className="mt-[3px] flex w-4 shrink-0 justify-center">{lead}</span>
            <span className="grid min-w-0 flex-1 gap-0.5"><span className="text-[14px] leading-5 font-medium">{it.title}</span>
              <span className="text-[13px] leading-5 text-muted-foreground">{meta}</span></span>
            <span className="flex shrink-0 items-center gap-1 self-center text-xs font-medium">Open PR<ExternalLink className="size-3" aria-hidden /><span className="sr-only">(opens GitHub)</span></span>
          </a>)
        const row = <PanelRow key={it.id} onClick={() => openItem(it)} lead={lead} title={it.title} meta={meta} className="min-h-11 py-2.5"
          trail={a || retry !== null ? undefined : <ChevronRight className="size-4 text-muted-foreground/50 max-sm:hidden" aria-hidden />} />
        if (!a && retry === null) return row
        // Decided right here with the same confirmation as Approvals / Activity.
        return <div key={it.id} className="flex items-center gap-x-2 pr-3 max-sm:flex-wrap max-sm:pb-2.5 max-sm:pl-7">
          <div className="min-w-0 flex-1 max-sm:-ml-7 max-sm:basis-[calc(100%+1.75rem)]">{row}</div>
          {a ? <ApprovalDecision a={a} compact /> : <RetryDelivery eventId={retry!} compact />}
        </div>
      }) : <PanelEmpty title={unknown.length ? "Nothing listed, but not every source answered" : "Nothing waits on you"}>
        {unknown.length ? `Not reported: ${unknown.join(", ")}.` : sc ? "No approvals, stopped tasks, PRs to merge or Stage C blockers." : "No approvals, stopped tasks or results to accept."}</PanelEmpty>}
      {items.length > SHOW && <button type="button" onClick={() => setAll(v => !v)}
        className="min-h-11 w-full px-4 text-left text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring">
        {all ? "Show fewer" : `Show all ${items.length}`}</button>}
    </Panel>
  )
}

// --------------------------------------------------------------------------- 4. Next
/** What comes next: the current priority, queued work by priority, and Stage C's own next step where it runs. */
export function NextCard({ sc }: { sc: StageCState | null }) {
  const d = useData()
  const { go, open } = useDesk()
  const p = d.current_priority
  const queued = upNext(d.tasks)
  const step = sc ? nextAction(sc) : null
  return (
    <Panel title="Up next" action={<More onClick={() => go("tasks")}>Tasks</More>}>
      <PanelRow onClick={() => (p?.kind === "task" && p.key ? open({ kind: "task", key: p.key }) : go("projects"))} className="min-h-11 py-2.5"
        lead={<Dot tone={p?.measure?.latest ? fresh(p.measure) : p ? "neutral" : "warning"} />}
        title={p === undefined ? `Current priority ${UNKNOWN}` : p === null ? "No current priority set" : p.title}
        meta={p?.measure?.latest ? <>Priority goal · {fmtNum(p.measure.latest.value)}{p.measure.target_value != null && <> of {fmtNum(p.measure.target_value)}</>}
          {p.measure.progress != null && <> · {Math.round(p.measure.progress * 100)}%</>} · {ago(p.measure.latest.observed_at)} ago{staleSuffix(p.measure)}</>
          : p ? `Priority ${p.kind} · no measured progress` : undefined} />
      {queued.map(t => (
        <PanelRow key={t.key} onClick={() => open({ kind: "task", key: t.key })} className="min-h-11 py-2.5" lead={<Dot tone="neutral" />}
          title={t.title} meta={<>{t.priority} · {d.control.agents_paused ? "queued, agents paused" : t.agent ? `queued for ${t.agent}` : "queued, no agent"}</>} />
      ))}
      {step && <PanelRow onClick={() => go("stage-c")} className="min-h-11 py-2.5" lead={<Dot tone="warning" />} title="Stage C" meta={step} />}
      {!queued.length && !step && <PanelEmpty title="Nothing queued">No task is ready or queued.</PanelEmpty>}
    </Panel>
  )
}

// --------------------------------------------------------------------------- 5. Autonomous work (Stage C hosts only)
export function AutonomyCard({ sc, read }: { sc: StageCState; read: StageCRead }) {
  const d = useData()
  const { go } = useDesk()
  const now = useNow("second")
  const a = autonomy(d, sc)
  const c = a.counts
  const flag = a.codeWrite === null ? null : a.codeWrite ? "On" : "Off"
  const f = readFreshness(sc, read.fetchedAt, now, !!read.error).freshness
  return (
    <Panel title="Autonomous work" action={<More onClick={() => go("stage-c")}>Stage C</More>}
      description={<span className={cn(f !== "VERIFIED" && "text-warning")}>Stage C recent tasks{a.partial ? " (partial list)" : ""} · read {ago(read.fetchedAt)} ago
        {f === "STALE" ? " · stale" : f === "UNKNOWN" ? " · freshness unknown" : ""}</span>}>
      <Figs cols={4}>
        <Fig label="In progress" value={c && c.inProgress} />
        <Fig label="Ready to merge" value={c && c.readyToMerge} tone={c?.readyToMerge ? "warning" : undefined} />
        <Fig label="Merged" value={c && c.merged} />
        <Fig label="Blocked or failed" value={c && c.blocked} tone={c?.blocked ? "danger" : undefined} />
      </Figs>
      <Figs>
        <Fig label="Code-write" value={flag} tone={flag === "Off" ? "warning" : undefined} sub="A permission, not readiness" />
        <Fig label="Agents" value={a.paused === null ? null : a.paused ? "Paused" : "Not paused"} tone={a.paused ? "warning" : undefined} />
      </Figs>
      {a.lastPr ? <PrRow pr={a.lastPr} /> : <p className="px-4 py-3 text-[13px] text-muted-foreground">No ready or merged PR reported</p>}
    </Panel>
  )
}

// --------------------------------------------------------------------------- 6. Metrics

export function GoalsCard() {
  const d = useData()
  const { go } = useDesk()
  const r = d.revenue
  const mrr = r ? (r.mrr ?? r.month) : null
  const units = (d.portfolio ?? []).filter(u => u.metrics.length)
  const metric = (m: Metric) => `${fmtNum(m.value)}${m.unit ? ` ${m.unit}` : ""}`
  return (
    <Panel title="Business metrics" action={<More onClick={() => go("growth")}>Metrics</More>}>
      <PanelRow onClick={() => go("finance")} lead={<Dot tone={r ? fresh(r) : "warning"} />} className="min-h-11 py-2.5"
        title={r?.mrr != null ? "MRR" : "Revenue this month"}
        meta={mrr != null ? <><b className="num font-semibold text-foreground">${fmtNum(mrr)}</b> · {r!.source} · {ago(r!.observed_at)} ago{staleSuffix(r)}</>
          : r === undefined ? `${UNKNOWN}: not reported by this API` : "No verified revenue source"} />
      {d.portfolio === undefined ? <PanelEmpty title={`Business metrics ${UNKNOWN}`}>This API does not report the portfolio.</PanelEmpty>
        : units.slice(0, 3).map(u => (
          <PanelRow key={u.key} onClick={() => go("project", u.key)} className="min-h-11 py-2.5"
            lead={<Dot tone={u.metrics.every(m => freshOf(m) === "VERIFIED") ? "success" : "warning"} />} title={u.name}
            meta={<>{u.metrics.slice(0, 3).map(m => `${metricName(m)} ${metric(m)}${staleSuffix(m)}`).join(" · ")} · {dataSummary(u)}</>} />
        ))}
    </Panel>
  )
}

// --------------------------------------------------------------------------- 7. Budget
export function BudgetCard({ sc }: { sc: StageCState | null }) {
  const d = useData()
  const { go } = useDesk()
  return (
    <Panel title="Budget and cost" description="Only what runs recorded" action={<More onClick={() => go("finance")}>Finance</More>}>
      <Figs>{costs(d, sc).map(c => <Fig key={c.label} label={c.label} value={c.value} sub={c.note}
        tone={c.label === "Quota stops" && c.value !== null && c.value !== "0" ? "warning" : undefined} />)}</Figs>
    </Panel>
  )
}

// --------------------------------------------------------------------------- 8. Alerts, incidents and running version
export function AlertsCard() {
  const d = useData()
  const { go } = useDesk()
  const now = useNow("minute")
  const health = healthSummary(systemChecks(d, now))
  const events = recentAlerts(d.activity)
  return (
    <Panel title="Alerts and incidents" count={d.incidents.length || undefined} action={<More onClick={() => go("incidents")}>Incidents</More>}>
      <PanelRow onClick={() => go("health")} lead={<Dot tone={health.tone} />} title="Company health" meta={health.text} className="min-h-11 py-2.5" />
      {d.incidents.slice(0, 3).map(i => (
        <PanelRow key={i.key} onClick={() => go("incidents")} className="min-h-11 py-2.5"
          lead={<Dot tone={["SEV0", "SEV1"].includes(i.severity) ? "danger" : "warning"} />} title={i.title}
          meta={<>{i.severity} · {humanize(i.status)} · opened {ago(i.started_at)} ago</>} />
      ))}
      {events.map(e => (
        <PanelRow key={e.id} onClick={() => go("activity")} className="min-h-11 py-2.5"
          lead={<Dot tone={(e.latest_observation?.severity ?? e.severity) === "CRITICAL" ? "danger" : "warning"} />}
          title={e.latest_observation?.title ?? e.title} meta={<>{humanize(e.type)} · {ago(e.latest_observation?.observed_at ?? e.at)} ago</>} />
      ))}
      {!d.incidents.length && !events.length && <PanelEmpty title="No open incidents or recent high alerts">
        {vitals(d, now)[1].value}</PanelEmpty>}
      <VersionRow />
    </Panel>
  )
}

/** The running version from /desk/state runtime only. The release ledger is root-only, so releases are not claimed. */
function VersionRow() {
  const { go } = useDesk()
  const v = runningVersion(useData())
  return <PanelRow onClick={() => go("health")} className="min-h-11 py-2.5"
    lead={<Dot tone={!v || v.agree === null ? "warning" : v.agree ? "success" : "warning"} />} title="Running version"
    meta={!v ? `${UNKNOWN}: not reported by this API`
      : <><span className="font-mono">API {v.api ?? UNKNOWN} · worker {v.worker ?? UNKNOWN}</span>
        {v.agree === false ? ` · ${v.drift[0] ?? "API and worker differ"}` : v.agree ? " · same commit" : ""}</>} />
}
