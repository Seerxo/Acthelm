// Stage C: the autonomous coding loop as GET /stage-c/state reports it (app/stage_c_state.py). The backend owns the
// lifecycle; this page parses and shows it (lib/stage-c.ts) and never upgrades missing evidence to success. Its one
// action is the emergency revoke of the standing grant. Green CI here is a PR check, not a production deploy.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { ExternalLink, RefreshCw } from "lucide-react"
import { api } from "@/lib/api"
import { ago, humanize, when } from "@/lib/format"
import { freshLabel } from "@/lib/freshness"
import {
  acceptanceView, BADGE_TONE, blockerLabel, canRevokeStanding, ciView, driftView, grantView, heartbeatView, isRevoked, lastReadyPr, loadError, na,
  nextAction, parseStageC, preflightView, privacyGateLabel, prHref, readFreshness, reviewView, short, steps, testView, workerReadyView,
  type LoadError, type StageCState, type StepState, type Task, type View,
} from "@/lib/stage-c"
import { cn } from "@/lib/utils"
import { useDesk, useNow } from "@/state"
import { EmptyState, Facts, FigureStrip, Muted, PageHeader, Panel, PanelEmpty, Section, Status, TaskKey } from "@/components/desk"
import { RevokeStanding } from "@/components/controls"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

const POLL_MS = 30_000

const Badge = ({ v, pill }: { v: View; pill?: boolean }) => <Status value={v.badge} label={v.label} tone={BADGE_TONE[v.badge]} pill={pill} />
const Sha = ({ s }: { s: string | null }) => s ? <span className="font-mono text-xs" title={s}>{short(s)}</span> : <Muted>Not reported</Muted>
const At = ({ t }: { t: string | null }) => t ? <span title={when(t)}>{when(t)} <Muted>({ago(t)} ago)</Muted></span> : <Muted>Not reported</Muted>
const Note = ({ children }: { children: ReactNode }) =>
  <p className="max-w-3xl rounded-lg border border-border/70 bg-surface/70 px-3.5 py-3 text-[13px] leading-5 text-muted-foreground">{children}</p>

const STEP: Record<StepState, [string, "success" | "info" | "danger" | "neutral" | "warning"]> = {
  completed: ["Completed", "success"], in_progress: ["In progress", "info"], failed: ["Failed", "danger"], pending: ["Pending", "neutral"], unknown: ["Unknown", "warning"],
  not_verified: ["Not verified", "warning"],
}

export function StageCPage() {
  const { lastOk, reload } = useDesk()
  const now = useNow("second")
  const [state, setState] = useState<StageCState | null>(null)
  const [fetchedAt, setFetchedAt] = useState(0)
  const [failure, setFailure] = useState<{ kind: LoadError; message: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const request = useRef(0)

  const read = useCallback(async () => {
    const id = ++request.current
    setLoading(true)
    try {
      const raw = await api<unknown>("/stage-c/state")
      if (id !== request.current) return
      setState(parseStageC(raw)); setFetchedAt(Date.now()); setFailure(null)
    } catch (e) {
      if (id !== request.current) return
      const kind = loadError(e)
      setFailure({ kind, message: (e as Error).message })
      // A sign-out: drop what is shown and let the shell's own read meet the 401 and end the session (state.tsx).
      if (kind === "signed_out") { setState(null); void reload() }
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
  const header = <PageHeader title="Stage C" description="The autonomous coding loop, as the backend reports it. Missing evidence reads unknown, never done."
    meta={state && <span className={cn("num", marker && "text-warning")} role="status">
      {marker ? `${humanize(marker)} · ` : ""}{fetchedAt ? `read ${ago(fetchedAt)} ago` : ""}</span>}>
    <Button variant="outline" className="h-10" onClick={() => void read()} disabled={loading} aria-label="Refresh Stage C state">
      <RefreshCw className={cn(loading && "animate-spin")} />Refresh</Button>
  </PageHeader>

  if (!state) return <>
    {header}
    {!failure ? <div className="grid gap-3" aria-busy="true" aria-label="Loading Stage C state"><Skeleton className="h-28" /><Skeleton className="h-48" /></div>
      : <div role="alert"><EmptyState title={failure.kind === "forbidden" ? "Founder sign-in required"
          : failure.kind === "unavailable" ? "Not available on this API" : failure.kind === "signed_out" ? "Signed out" : "Stage C state unavailable"}
          action={<Button variant="outline" onClick={() => void read()}><RefreshCw />Try again</Button>}>
          {failure.kind === "forbidden" ? "This read is founder only; a Desk token cannot open it."
            : failure.kind === "unavailable" ? "The deployed API does not serve /stage-c/state yet."
            : `${failure.message}. Nothing is shown until the API answers; no value is guessed.`}</EmptyState></div>}
  </>

  return <>
    {header}
    {failure && <p role="alert" className="text-[13px] text-warning">Refresh failed ({failure.message}); showing the last read, marked stale.</p>}
    {!state.version_ok && <Note>This API reports a contract version this Desk does not know. Fields it could not read show as unknown.</Note>}
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
    : rt.write_enabled === true ? "Agents running · code-write flag on" : rt.write_enabled === false ? "Agents running · code-write flag off" : "Agents running · code-write flag not reported"
  const grant = (g: Parameters<typeof grantView>[0]) => <Badge v={grantView(g)} />
  return (
    <Section title="Autonomy overview" description="Every value as reported; the code-write flag is a permission, not readiness."
      action={<Badge v={{ badge: s.state, label: humanize(s.state) }} pill />}>
      <div className="grid gap-4 lg:grid-cols-2">
        <Facts items={[
          ["State", <Badge key="s" v={{ badge: s.state, label: humanize(s.state) }} pill />],
          ["Operating mode", mode],
          ["Worker heartbeat", <Badge key="h" v={heartbeatView(rt.worker_heartbeat)} />],
          ["Worker readiness", <Badge key="r" v={workerReadyView(rt.worker_ready)} />],
          ["API commit", <Sha key="a" s={rt.api_sha} />],
          ["Worker commit", <Sha key="w" s={rt.worker_sha} />],
          ["Reasoner commit", <Sha key="rs" s={rt.reasoner_sha} />],
          ["Drift", <Badge key="d" v={driftView(rt)} />],
          ["Host acceptance", <span key="ac" className="flex flex-wrap items-center gap-2"><Badge v={acceptanceView(rt.acceptance.valid)} /><Sha s={rt.acceptance.source_sha} /></span>],
          ["Preflight", <span key="p" className="flex flex-wrap items-center gap-2"><Badge v={preflightView(rt.preflight)} />
            {rt.preflight.checked_at && <Muted>checked {ago(rt.preflight.checked_at)} ago</Muted>}<Sha s={rt.preflight.source_sha} /></span>],
          ["Host facts", <Badge key="hf" v={{ badge: "NOT_VERIFIED", label: "Not verified by the API" }} />],
        ]} />
        <Facts items={[
          ["One-task grant", <span key="o" className="flex flex-wrap items-center gap-2">{grant(s.authorizations.one_task)}
            {s.authorizations.one_task?.task_key && <TaskKey k={s.authorizations.one_task.task_key} />}</span>],
          ["Standing grant", grant(s.authorizations.standing)],
          ["Running work", running === null ? <Badge key="rw" v={{ badge: "UNKNOWN", label: "Not reported" }} />
            : running.length ? <span key="rw" className="flex flex-wrap gap-2">{running.map((t, i) => <TaskKey key={t.key ?? i} k={t.key ?? "unknown key"} />)}</span>
            : <Muted key="rw">None</Muted>],
          ["Blockers", s.blockers === null ? <Badge key="b" v={{ badge: "UNKNOWN", label: "Not reported" }} />
            : s.blockers.length ? <ul key="b" className="grid gap-0.5">{s.blockers.map((b, i) => <li key={`${b}-${i}`}>{blockerLabel(b)}</li>)}</ul>
            : <Muted key="b">None reported</Muted>],
          ["Last ready / merged PR", last ? <PrLink key="l" number={last.number} /> : <Muted key="l">Not reported</Muted>],
          ["Next founder action", next ? <span key="n" className="font-medium">{next}</span> : <Muted key="n">No action reported</Muted>],
        ]} />
      </div>
    </Section>
  )
}

function PrLink({ number }: { number: number | null }) {
  const href = prHref(number)
  if (!href) return <Muted>No PR number recorded</Muted>
  return <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-medium underline-offset-2 hover:underline focus-visible:underline">
    PR #{number}<ExternalLink className="size-3" aria-hidden /><span className="sr-only">(opens GitHub)</span></a>
}

function StandingPanel({ s, onRevoked }: { s: StageCState; onRevoked: () => void }) {
  const g = s.authorizations.standing
  const revoked = isRevoked(g)
  return (
    <Section title="Standing authorization" description="Issued and renewed on the host by the founder; the Desk can only withdraw it."
      action={canRevokeStanding(g) ? <RevokeStanding onDone={onRevoked} /> : undefined}>
      {g === null ? <PanelEmpty title="No standing grant">The API explicitly reports no standing authorization.</PanelEmpty>
        : g === undefined ? <PanelEmpty title="UNKNOWN">The API did not report a readable standing grant. Its existence is unknown; Revoke remains available.</PanelEmpty>
        : <Facts items={[
          ["Status", <Badge key="s" v={grantView(g)} pill />],
          ["Revoked", g.revoked === null ? <Muted key="r">Not reported</Muted> : revoked ? "Yes" : "No"],
          ["Source commit", <Sha key="sha" s={g.source_sha} />],
          ["Paths", g.paths === null ? <Muted key="p">Not reported</Muted> : g.paths.length
            ? <ul key="p" className="grid gap-0.5 font-mono text-xs">{g.paths.map(p => <li key={p}>{p}</li>)}</ul> : <Muted key="p">None</Muted>],
          ["Expires", <At key="e" t={g.expires_at} />],
          ["Per-day cap", na(g.max_per_day)],
          ["Used today", na(g.used_today)],
          ["Remaining today", na(g.remaining_today)],
          ["Issuer role", humanize(g.issuer)],
        ]} />}
    </Section>
  )
}

function Pipeline({ tasks }: { tasks: Task[] | null }) {
  return (
    <Section title="Pipeline" count={tasks?.length} description="Newest first. CI passing on a PR is not a production deploy; merging and releasing stay with the founder and the release gateway.">
      {tasks === null ? <PanelEmpty title="Not reported">This API did not report Stage C tasks.</PanelEmpty>
        : !tasks.length ? <PanelEmpty title="No Stage C tasks yet">Nothing has run under Stage C.</PanelEmpty>
        : <div className="grid gap-3">{tasks.map((t, i) => <TaskCard key={t.key ?? `unknown-${i}`} t={t} />)}</div>}
    </Section>
  )
}

function TaskCard({ t }: { t: Task }) {
  const ci = ciView(t)
  return (
    <Panel title={<span className="flex flex-wrap items-center gap-2">{t.key ? <TaskKey k={t.key} /> : <Muted>Unknown key</Muted>}
      <Status value={t.status} />{t.needs_founder && <Status value="NEEDS_FOUNDER" label="Needs you" tone="warning" />}</span>}
      description={t.stage === "ABANDONED" ? "Abandoned" : t.stage === null ? "No lifecycle recorded" : undefined}>
      <div className="grid gap-4 p-4">
        <ol className="flex flex-wrap gap-x-4 gap-y-2" aria-label={`Pipeline of ${t.key ?? "task"}`}>
          {steps(t).map(x => { const [label, tone] = STEP[x.state]
            return <li key={x.key} className="grid gap-0.5 text-[13px]"><span className="font-medium">{x.label}</span>
              <Status value={x.state} label={label} tone={tone} live={x.state === "in_progress"} /></li> })}
        </ol>
        <Facts items={[
          ["Attempts", na(t.attempts)], ["Repairs", na(t.repairs)],
          ["Tests", <Badge key="t" v={testView(t)} />],
          ["Independent review", <Badge key="r" v={reviewView(t)} />],
          ["Fingerprint", <span key="f" className="font-mono text-xs" title={[t.tested_fingerprint, t.reviewed_fingerprint].filter(Boolean).join(" / ") || undefined}>
            tested {short(t.tested_fingerprint, 12) ?? "n/a"} · reviewed {short(t.reviewed_fingerprint, 12) ?? "n/a"}</span>],
          ["Pull request", t.pr ? <span key="pr" className="flex flex-wrap items-center gap-2"><PrLink number={t.pr.number} /><Muted>head</Muted><Sha s={t.pr.head_sha} /></span> : <Muted key="pr">Not opened</Muted>],
          ["CI", <span key="ci" className="flex flex-wrap items-center gap-2"><Status value={ci.kind} label={ci.label} tone={ci.tone} />
            {ci.sha && <><Muted>on</Muted><Sha s={ci.sha} /></>}</span>],
          ["Last outcome", t.last_outcome ? privacyGateLabel(t.last_outcome) ?? humanize(t.last_outcome) : <Muted key="o">None recorded</Muted>],
          ["Failure", t.failure_category ? <span key="fc" className="text-destructive">{privacyGateLabel(t.failure_category) ?? humanize(t.failure_category)}</span> : <Muted key="fc">None recorded</Muted>],
          ["Updated", <At key="u" t={t.updated_at} />],
        ]} />
      </div>
    </Panel>
  )
}

function MetricsPanel({ s }: { s: StageCState }) {
  const m = s.metrics
  return (
    <Section title="Metrics" description="Stage C tasks and attempts are separate counts. n/a means not recorded, never zero.">
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
