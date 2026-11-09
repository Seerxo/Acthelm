// Stage C: the autonomous coding loop as GET /stage-c/state reports it (app/stage_c_state.py). The backend owns the
// lifecycle; this page parses and shows it (lib/stage-c.ts) and never upgrades missing evidence to success. Its one
// action is the emergency revoke of the standing grant. Green CI here is a PR check, not a production deploy.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { ChevronRight, ExternalLink, RefreshCw } from "lucide-react"
import { PageTabs } from "@/components/page-tabs"
import { api } from "@/lib/api"
import { ago, humanize, when } from "@/lib/format"
import { freshLabel } from "@/lib/freshness"
import {
  acceptanceView, BADGE_TONE, blockerLabel, canRevokeStanding, ciView, driftView, grantView, heartbeatView, isRevoked, lastReadyPr, loadError, na,
  nextAction, offerRevokeOnFailure, parseStageC, preflightView, privacyGateLabel, prHref, readFreshness, reviewView, short, steps, testView, workerReadyView,
  type LoadError, type StageCState, type StepState, type Task, type View,
} from "@/lib/stage-c"
import { cn } from "@/lib/utils"
import { stageCOnHost, taskStatusView } from "@/lib/qa-status"
import { useDesk, useNow } from "@/state"
import { EmptyState, Facts, FigureStrip, Muted, PageHeader, Panel, PanelEmpty, Section, Status, TaskKey } from "@/components/desk"
import { RevokeStanding } from "@/components/controls"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

const POLL_MS = 30_000

const Badge = ({ v, pill }: { v: View; pill?: boolean }) => <Status value={v.badge} label={v.label} tone={BADGE_TONE[v.badge]} pill={pill} />
const Sha = ({ s }: { s: string | null }) => s ? <span className="font-mono text-xs" title={s}>{short(s)}</span> : <Muted>Not reported</Muted>
const At = ({ t }: { t: string | null }) => t ? <span title={when(t)}>{when(t)} <Muted>({ago(t)} ago)</Muted></span> : <Muted>Not reported</Muted>
const Note = ({ children }: { children: ReactNode }) => <p className="text-[13px] text-warning">{children}</p>
/** SHAs, fingerprints and check names: one click away, closed by default. */
const More = ({ children }: { children: ReactNode }) => (
  <details className="group text-[13px]">
    <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring sm:min-h-8 [&::-webkit-details-marker]:hidden">
      <ChevronRight aria-hidden className="size-4 transition-transform group-open:rotate-90" />Details</summary>
    <div className="pt-2 pl-5">{children}</div>
  </details>
)

const STEP: Record<StepState, [string, "success" | "info" | "danger" | "neutral" | "warning"]> = {
  completed: ["Completed", "success"], in_progress: ["In progress", "info"], failed: ["Failed", "danger"], pending: ["Pending", "neutral"], unknown: ["Unknown", "warning"],
  not_verified: ["Not verified", "warning"], observed: ["Observed", "neutral"],
}

export function StageCPage() {
  const { lastOk, reload } = useDesk()
  const now = useNow("second")
  const [state, setState] = useState<StageCState | null>(null)
  const [fetchedAt, setFetchedAt] = useState(0)
  const [failure, setFailure] = useState<{ kind: LoadError; message: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const request = useRef(0)
  const signedOutReloaded = useRef(false)

  const read = useCallback(async () => {
    const id = ++request.current
    setLoading(true)
    try {
      const raw = await api<unknown>("/stage-c/state")
      if (id !== request.current) return
      setState(parseStageC(raw)); setFetchedAt(Date.now()); setFailure(null)
      signedOutReloaded.current = false
    } catch (e) {
      if (id !== request.current) return
      const kind = loadError(e)
      setFailure({ kind, message: (e as Error).message })
      // A sign-out: drop what is shown and let the shell's own read meet the 401 and end the session (state.tsx).
      if (kind === "signed_out") {
        setState(null)
        // The shell may answer successfully and trigger another read; reload only once until this episode ends.
        if (!signedOutReloaded.current) { signedOutReloaded.current = true; void reload() }
      } else signedOutReloaded.current = false
    } finally { if (id === request.current) setLoading(false) }
  }, [reload])

  // Re-read whenever the Desk read model refreshed (live stream or its fallback poll), and every 30s while visible:
  // grant expiry, heartbeat and preflight age change without any commit the stream would announce.
  useEffect(() => { void read() }, [read, lastOk])
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void read() }, POLL_MS)
    return () => clearInterval(t)
  }, [read])

  const fresh = state ? readFreshness(state, fetchedAt, now, !!failure) : null
  const marker = fresh ? freshLabel(fresh) : ""
  const header = <><PageHeader title="Stage C"
    meta={state && <span className={cn("num", marker && "text-warning")} role="status">
      {marker ? `${humanize(marker)} · ` : ""}{fetchedAt ? `read ${ago(fetchedAt)} ago` : ""}</span>}>
    <Button variant="outline" className="h-11" onClick={() => void read()} disabled={loading} aria-label="Refresh Stage C state">
      <RefreshCw className={cn(loading && "animate-spin")} />Refresh</Button>
  </PageHeader><PageTabs page="stage-c" /></>

  if (!state) return <>
    {header}
    {!failure ? <div className="grid gap-3" aria-busy="true" aria-label="Loading Stage C state"><Skeleton className="h-28" /><Skeleton className="h-48" /></div>
      : <div role="alert"><EmptyState title={failure.kind === "forbidden" ? "Founder sign-in required"
          : failure.kind === "unavailable" ? "Not available on this API" : failure.kind === "signed_out" ? "Signed out" : "Stage C state unavailable"}
          action={<div className="flex flex-wrap justify-center gap-2">
            <Button variant="outline" className="h-11" onClick={() => void read()}><RefreshCw />Try again</Button>
            {offerRevokeOnFailure(failure.kind) && <RevokeStanding onDone={() => void read()} />}
          </div>}>
          {failure.kind === "forbidden" ? "This read is founder only; a Desk token cannot open it."
            : failure.kind === "unavailable" ? "The deployed API does not serve /stage-c/state yet."
            : failure.message}</EmptyState></div>}
  </>

  // Production runs the same Desk with no Stage C: say so once instead of a page of unknowns and controls.
  if (!stageCOnHost(state)) return <>
    {header}
    <EmptyState title="Stage C is not on this host">No host acceptance, preflight, task or grant is reported here.</EmptyState>
  </>

  return <>
    {header}
    {failure && <p role="alert" className="text-[13px] text-warning">Refresh failed ({failure.message}); showing the last read, marked stale.</p>}
    {!state.version_ok && <Note>Unknown contract version: unreadable fields show as unknown.</Note>}
    <Overview s={state} />
    <StandingPanel s={state} onRevoked={() => void read()} />
    <Pipeline tasks={state.tasks} />
    <MetricsPanel s={state} />
  </>
}

function Overview({ s }: { s: StageCState }) {
  const rt = s.runtime
  const running = s.tasks?.filter(t => t.status === "IN_PROGRESS") ?? null
  const last = lastReadyPr(s)
  const next = nextAction(s)
  const mode = rt.agents_paused === true ? "Agents paused" : rt.agents_paused === null ? "Not reported"
    : rt.write_enabled === true ? "Agents not paused · code-write flag on" : rt.write_enabled === false ? "Agents not paused · code-write flag off" : "Agents not paused · code-write flag not reported"
  const grant = (g: Parameters<typeof grantView>[0]) => <Badge v={grantView(g)} />
  return (
    <Section title="Overview"
      action={<Badge v={{ badge: s.state, label: humanize(s.state) }} pill />}>
      <div className="grid gap-4 lg:grid-cols-2">
        <Facts items={[
          ["Next founder action", next ? <span key="n" className="font-medium">{next}</span> : <Muted key="n">No action reported</Muted>],
          ["Blockers", s.blockers === null ? <Badge key="b" v={{ badge: "UNKNOWN", label: "Not reported" }} />
            : s.blockers.length ? <ul key="b" className="grid gap-0.5">{s.blockers.map((b, i) => <li key={`${b}-${i}`}>{blockerLabel(b)}</li>)}</ul>
            : <Muted key="b">None reported</Muted>],
          ["One-task grant", <span key="o" className="flex flex-wrap items-center gap-2">{grant(s.authorizations.one_task)}
            {s.authorizations.one_task?.task_key && <TaskKey k={s.authorizations.one_task.task_key} />}</span>],
          ["Standing grant", grant(s.authorizations.standing)],
          ["Running work", running === null ? <Badge key="rw" v={{ badge: "UNKNOWN", label: "Not reported" }} />
            : running.length ? <span key="rw" className="flex flex-wrap gap-2">{running.map((t, i) => <TaskKey key={t.key ?? i} k={t.key ?? "unknown key"} />)}</span>
            : <Muted key="rw">None</Muted>],
          ["Last ready / merged PR", last ? <PrLink key="l" number={last.number} /> : <Muted key="l">Not reported</Muted>],
        ]} />
        <Facts items={[
          ["State", <Badge key="s" v={{ badge: s.state, label: humanize(s.state) }} pill />],
          ["Operating mode", mode],
          ["Worker heartbeat", <Badge key="h" v={heartbeatView(rt.worker_heartbeat)} />],
          ["Worker readiness", <Badge key="r" v={workerReadyView(rt.worker_ready)} />],
          ["Drift", <Badge key="d" v={driftView(rt)} />],
          ["Host acceptance", <Badge key="ac" v={acceptanceView(rt.acceptance.valid)} />],
          ["Preflight", <span key="p" className="flex flex-wrap items-center gap-2"><Badge v={preflightView(rt.preflight)} />
            {rt.preflight.checked_at && <Muted>checked {ago(rt.preflight.checked_at)} ago</Muted>}</span>],
          ["Host facts", <Badge key="hf" v={{ badge: "NOT_VERIFIED", label: "Not verified by the API" }} />],
        ]} />
      </div>
      <More><Facts items={[
        ["API commit", <Sha key="a" s={rt.api_sha} />],
        ["Worker commit", <Sha key="w" s={rt.worker_sha} />],
        ["Reasoner commit", <Sha key="rs" s={rt.reasoner_sha} />],
        ["Acceptance commit", <Sha key="ac" s={rt.acceptance.source_sha} />],
        ["Preflight commit", <Sha key="ps" s={rt.preflight.source_sha} />],
        // Why the worker is not ready: the watchdog's failing checks, as recorded (fixed check names only).
        ["Failing checks", rt.preflight.failed?.length ? <span key="f" className="flex flex-wrap gap-1.5">
          {rt.preflight.failed.map(c => <code key={c} className="rounded bg-muted px-1.5 font-mono text-xs break-all">{c}</code>)}</span> : <Muted key="f">None reported</Muted>],
      ]} /></More>
    </Section>
  )
}

function PrLink({ number }: { number: number | null }) {
  const href = prHref(number)
  if (!href) return <Muted>No PR number recorded</Muted>
  return <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center gap-1 px-2 font-medium underline-offset-2 hover:underline focus-visible:underline">
    PR #{number}<ExternalLink className="size-3" aria-hidden /><span className="sr-only">(opens GitHub)</span></a>
}

function StandingPanel({ s, onRevoked }: { s: StageCState; onRevoked: () => void }) {
  const g = s.authorizations.standing
  const revoked = isRevoked(g)
  return (
    <Section title="Standing authorization" description="Issued on the host; the Desk can only withdraw it."
      action={canRevokeStanding(g) ? <RevokeStanding onDone={onRevoked} /> : undefined}>
      {g === null ? <PanelEmpty title="No standing grant" />
        : g === undefined ? <PanelEmpty title="UNKNOWN">No readable standing grant was reported; Revoke stays available.</PanelEmpty>
        : <Facts items={[
          ["Status", <Badge key="s" v={grantView(g)} pill />],
          ["Revoked", g.revoked === null ? <Muted key="r">Not reported</Muted> : revoked ? "Yes" : "No"],
          ["Paths", g.paths === null ? <Muted key="p">Not reported</Muted> : g.paths.length
            ? <ul key="p" className="grid gap-0.5 font-mono text-xs break-all">{g.paths.map(p => <li key={p}>{p}</li>)}</ul> : <Muted key="p">None</Muted>],
          ["Expires", <At key="e" t={g.expires_at} />],
          ["Per-day cap", na(g.max_per_day)],
          ["Used today", na(g.used_today)],
          ["Remaining today", na(g.remaining_today)],
          ["Issuer role", humanize(g.issuer)],
        ]} />}
      {g && <More><Facts items={[["Source commit", <Sha key="sha" s={g.source_sha} />]]} /></More>}
    </Section>
  )
}

function Pipeline({ tasks }: { tasks: Task[] | null }) {
  return (
    <Section title="Pipeline" count={tasks?.length} description="Green CI on a PR is not a deploy; merge and release stay with the founder.">
      {tasks === null ? <PanelEmpty title="Not reported" />
        : !tasks.length ? <PanelEmpty title="No Stage C tasks yet" />
        : <div className="grid gap-3">{tasks.map((t, i) => <TaskCard key={t.key ?? `unknown-${i}`} t={t} />)}</div>}
    </Section>
  )
}

function TaskCard({ t }: { t: Task }) {
  const ci = ciView(t)
  return (
    <Panel title={<span className="flex flex-wrap items-center gap-2">{t.key ? <TaskKey k={t.key} /> : <Muted>Unknown key</Muted>}
      <Status {...taskStatusView(t.status, t.stage)} />{t.needs_founder && <Status value="NEEDS_FOUNDER" label="Needs you" tone="warning" />}</span>}
      description={t.stage === "ABANDONED" ? "Abandoned" : t.stage === null ? "No lifecycle recorded" : undefined}>
      <div className="grid gap-4 p-4">
        <ol className="flex flex-wrap gap-x-4 gap-y-2" aria-label={`Pipeline of ${t.key ?? "task"}`}>
          {steps(t).map(x => { const [label, tone] = STEP[x.state]
            return <li key={x.key} className="grid gap-0.5 text-[13px]"><span className="font-medium">{x.label}</span>
              <Status value={x.state} label={label} tone={tone} live={x.state === "in_progress"} /></li> })}
        </ol>
        <Facts items={[
          ["Tests", <Badge key="t" v={testView(t)} />],
          ["Independent review", <Badge key="r" v={reviewView(t)} />],
          ["Pull request", t.pr ? <PrLink key="pr" number={t.pr.number} /> : <Muted key="pr">Not opened</Muted>],
          ["CI", <Status key="ci" value={ci.kind} label={ci.label} tone={ci.tone} />],
          ...(t.failure_category ? [["Failure", <span key="fc" className="text-destructive">{privacyGateLabel(t.failure_category) ?? humanize(t.failure_category)}</span>] as [string, ReactNode]] : []),
          ["Updated", <At key="u" t={t.updated_at} />],
        ]} />
        <More><Facts items={[
          ["Attempts", na(t.attempts)], ["Repairs", na(t.repairs)],
          ["Tested fingerprint", <span key="tf" className="font-mono text-xs [overflow-wrap:anywhere]">{t.tested_fingerprint ?? "n/a"}</span>],
          ["Reviewed fingerprint", <span key="rf" className="font-mono text-xs [overflow-wrap:anywhere]">{t.reviewed_fingerprint ?? "n/a"}</span>],
          ["PR head", <Sha key="ph" s={t.pr?.head_sha ?? null} />],
          ["CI commit", <Sha key="cs" s={ci.sha ?? null} />],
          ["Last outcome", t.last_outcome ? privacyGateLabel(t.last_outcome) ?? humanize(t.last_outcome) : <Muted key="o">None recorded</Muted>],
          ["Failure", t.failure_category ? privacyGateLabel(t.failure_category) ?? humanize(t.failure_category) : <Muted key="fc">None recorded</Muted>],
        ]} /></More>
      </div>
    </Panel>
  )
}

function MetricsPanel({ s }: { s: StageCState }) {
  const m = s.metrics
  return (
    <Section title="Metrics" description="n/a means not recorded, never zero.">
      <div className="grid gap-4">
        <FigureStrip label="Stage C tasks" items={[
          { label: "Tasks started", value: na(m.tasks.started) },
          { label: "Reached ready to merge", value: na(m.tasks.reached_ready_to_merge) },
          { label: "Stopped", value: na(m.tasks.stopped), tone: m.tasks.stopped ? "danger" : undefined, sub: "blocked or failed" },
          { label: "In progress", value: na(m.tasks.in_progress) },
        ]} />
        <FigureStrip label="Stage C attempts and cost" items={[
          { label: "Attempts", value: na(m.attempts.total), sub: `${na(m.attempts.failed)} failed` },
          { label: "Model calls", value: na(m.model_calls), sub: m.minutes === null ? "minutes n/a" : `${m.minutes} min of runs` },
          { label: "Tokens", value: na(m.tokens) },
          { label: "USD recorded", value: na(m.usd_recorded, n => `$${n.toFixed(2)}`), sub: "excludes subscription cost" },
        ]} />
        <p className="text-[13px] text-muted-foreground">
          Quota stops: {na(m.quota_stops)} · Abandoned: {na(m.tasks.abandoned)}
          {m.truncated === true && <span className="text-warning"> · counts cover only the newest runs (truncated)</span>}
        </p>
      </div>
    </Section>
  )
}
