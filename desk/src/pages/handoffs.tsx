// B5 Desk -> Stage C handoffs, two screens on one route: #/handoffs is live tracking (GET /desk/handoffs), and
// #/handoffs/TASK-KEY is one task's result and evidence (GET /tasks/{key}, its remote_stage_c and the production record).
// lib/handoffs.ts owns the lanes and waiting reasons; what Stage C said is labelled "Stage C reported" and a stale report
// is UNKNOWN. Read-only: no action is offered here.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { ArrowLeft, ExternalLink, RefreshCw } from "lucide-react"
import { api, type TaskDetail } from "@/lib/api"
import { ago, humanize, when } from "@/lib/format"
import { board, LANE_LABEL, LANE_TONE, laneOf, live, parseHandoff, parseHandoffs, waitingReason, type Handoff } from "@/lib/handoffs"
import { BADGE_TONE, ciView, loadError, prHref, reviewView, short, testView, type LoadError, type View } from "@/lib/stage-c"
import { prLink } from "@/lib/accept"
import { cn } from "@/lib/utils"
import { useDesk } from "@/state"
import { useNow } from "@/lib/use-now"
import { Dot, EmptyState, Facts, Muted, PageHeader, Panel, PanelEmpty, PanelRow, Section, Status, TaskKey } from "@/components/desk"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"

const POLL_MS = 30_000
const NOT_YET = <Muted>Not reported yet</Muted>

/** The Stage C page's read pattern (pages/stage-c.tsx): re-read with the Desk read model and every 30s while visible;
 *  a newer request wins, a failed refresh keeps the last read. */
function useRead<T>(path: string, parse: (raw: unknown) => T) {
  const { lastOk, reload } = useDesk()
  const [value, setValue] = useState<T | null>(null)
  const [fetchedAt, setFetchedAt] = useState(0)
  const [failure, setFailure] = useState<{ kind: LoadError; message: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const request = useRef(0)
  const read = useCallback(async () => {
    const id = ++request.current
    setLoading(true)
    try {
      const raw = await api<unknown>(path)
      if (id !== request.current) return
      setValue(parse(raw)); setFetchedAt(Date.now()); setFailure(null)
    } catch (e) {
      if (id !== request.current) return
      const kind = loadError(e)
      setFailure({ kind, message: (e as Error).message })
      if (kind === "signed_out") { setValue(null); void reload() }   // the shell's read ends the session (state.tsx)
    } finally { if (id === request.current) setLoading(false) }
  }, [path, parse, reload])
  useEffect(() => { void read() }, [read, lastOk])
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) void read() }, POLL_MS)
    return () => clearInterval(t)
  }, [read])
  return { value, fetchedAt, failure, loading, read }
}

function Header({ title, fetchedAt, loading, onRefresh, children }: {
  title: ReactNode; fetchedAt: number; loading: boolean; onRefresh: () => void; children?: ReactNode
}) {
  return <PageHeader title={title} meta={fetchedAt ? <span className="num" role="status">read {ago(fetchedAt)} ago</span> : null}>
    {children}
    <Button variant="outline" className="h-11" onClick={onRefresh} disabled={loading} aria-label="Refresh">
      <RefreshCw className={cn(loading && "animate-spin")} />Refresh</Button>
  </PageHeader>
}

function Failed({ failure, what, onRetry }: { failure: { kind: LoadError; message: string }; what: string; onRetry: () => void }) {
  return <div role="alert"><EmptyState title={failure.kind === "forbidden" ? "Founder sign-in required"
    : failure.kind === "unavailable" ? "Not available on this API" : failure.kind === "signed_out" ? "Signed out" : `${what} unavailable`}
    action={<Button variant="outline" className="h-11" onClick={onRetry}><RefreshCw />Try again</Button>}>
    {failure.kind === "forbidden" ? "This read is founder only; a Desk token cannot open it."
      : failure.kind === "unavailable" ? "The deployed API does not serve this read yet." : failure.message}</EmptyState></div>
}

const Badge = ({ v }: { v: View }) => <Status value={v.badge} label={v.label} tone={BADGE_TONE[v.badge]} />
const At = ({ t }: { t: string | null }) => t ? <span title={when(t)}>{ago(t)} ago</span> : NOT_YET
const Sha = ({ s }: { s: string | null }) => s ? <span className="font-mono text-xs" title={s}>{short(s)}</span> : NOT_YET
const LaneStatus = ({ h }: { h: Handoff }) => {
  const lane = laneOf(h)
  return <Status value={lane} label={lane === "UNKNOWN" ? "Unknown" : `${LANE_LABEL[lane]} · Stage C reported`} tone={LANE_TONE[lane]} pill />
}
const stage = (h: Handoff) => h.task?.stage && h.task.stage !== "UNKNOWN" ? humanize(h.task.stage) : null

export function HandoffsPage({ taskKey }: { taskKey?: string }) {
  return taskKey ? <HandoffResult taskKey={taskKey} /> : <HandoffBoard />
}

// --------------------------------------------------------------------------- live tracking
function HandoffBoard() {
  const { data, go } = useDesk()
  const { value, fetchedAt, failure, loading, read } = useRead("/desk/handoffs", parseHandoffs)
  const now = useNow()
  const agentOf = (key: string | null) => data?.tasks.find(t => t.key === key)?.agent ?? null
  const header = <Header title="Handoffs" fetchedAt={fetchedAt} loading={loading} onRefresh={() => void read()}>
    {value?.enabled === false && <Status value="OFF" label="Sender off on this host" tone="warning" />}
  </Header>
  if (!value) return <>{header}{failure ? <Failed failure={failure} what="Handoffs" onRetry={() => void read()} />
    : <div className="grid gap-3" aria-busy="true" aria-label="Loading handoffs"><Skeleton className="h-28" /><Skeleton className="h-48" /></div>}</>
  return <>
    {header}
    {failure && <p role="alert" className="text-[13px] text-warning">Refresh failed ({failure.message}); showing the last read.</p>}
    {!value.handoffs.length ? <EmptyState title="No task handed to Stage C">Released tasks appear here with what Stage C reports for them.</EmptyState>
      : board(value.handoffs.map(h => live(h, now))).filter(l => l.items.length || l.lane !== "UNKNOWN").map(({ lane, items }) => (
        <Panel key={lane} title={LANE_LABEL[lane]} count={items.length}>
          {!items.length ? <PanelEmpty title="None" /> : items.map(h => {
            const reason = waitingReason(h), agent = agentOf(h.key), s = stage(h)
            return <PanelRow key={h.id ?? h.key} label={`Open result and evidence for ${h.key ?? "task"}`}
              onClick={h.key ? () => go("handoffs", h.key!) : undefined}
              lead={<Dot tone={LANE_TONE[lane]} live={lane === "RUNNING"} />}
              title={<>{h.key && <TaskKey k={h.key} className="mr-2" />}{h.title ?? "Untitled"}</>}
              meta={<>
                {reason ? `Waiting: ${reason}` : s ? `Stage C step: ${s}` : "Stage C reports no waiting reason"}
                {" · "}{agent ? `owner ${agent}` : "owner not recorded"}{h.remote_task_key ? ` · Stage C ${h.remote_task_key}` : ""}
              </>}
              tone={reason && (lane === "BLOCKED" || lane === "UNKNOWN") ? LANE_TONE[lane] : undefined}
              trail={<><LaneStatus h={h} /><span className="num">updated <At t={h.generated_at ?? h.released_at} /></span></>} />
          })}
        </Panel>))}
    <p className="text-xs text-muted-foreground">Lanes follow the last authenticated Stage C report; without one in the last 3 minutes a task shows as Unknown. Stage C does not report which of its agents is working; "owner" is the production assignee.</p>
  </>
}

// --------------------------------------------------------------------------- result & evidence
const parseDetail = (raw: unknown) => raw as TaskDetail & { remote_stage_c?: unknown }

function HandoffResult({ taskKey }: { taskKey: string }) {
  const { data, go } = useDesk()
  const { value: t, fetchedAt, failure, loading, read } = useRead(`/tasks/${encodeURIComponent(taskKey)}`, parseDetail)
  const now = useNow()
  const back = <Button variant="ghost" className="h-11" onClick={() => go("handoffs")}><ArrowLeft />All handoffs</Button>
  const header = <Header title={<><TaskKey k={taskKey} className="mr-2 text-base" />{t?.title ?? "Result and evidence"}</>}
    fetchedAt={fetchedAt} loading={loading} onRefresh={() => void read()}>{back}</Header>
  if (!t) return <>{header}{failure ? <Failed failure={failure} what="Task" onRetry={() => void read()} />
    : <div className="grid gap-3" aria-busy="true" aria-label="Loading task"><Skeleton className="h-28" /><Skeleton className="h-48" /></div>}</>
  const parsed = parseHandoff(t.remote_stage_c)
  const h = parsed && live(parsed, now)
  const rt = h?.task ?? null
  const pr = prLink(t)
  const remotePr = prHref(rt?.pr?.number)
  const events = (data?.activity ?? []).filter(e => e.task === taskKey)
  return <>
    {header}
    {failure && <p role="alert" className="text-[13px] text-warning">Refresh failed ({failure.message}); showing the last read.</p>}
    {!h && <p className="text-[13px] text-muted-foreground">This task was not handed off to Stage C; only the production record is shown.</p>}

    {h && <Section title="Where it stands" action={<LaneStatus h={h} />}>
      <Facts items={[
        ["Waiting on", waitingReason(h) ?? <Muted key="w">Nothing reported</Muted>],
        ["Stage C step", stage(h) ?? NOT_YET],
        ["Stage C status", rt ? `${humanize(rt.status)}${h.freshness !== "FRESH" ? " (last report, not current)" : ""}` : NOT_YET],
        ["Report freshness", h.freshness === "FRESH" ? "Fresh" : <Status key="f" value="UNKNOWN" label="Unknown: no report in the last 3 minutes" tone="warning" />],
        ["Last report", <span key="r">Stage C reported <At t={h.generated_at} />{h.seq !== null && <Muted> · seq {h.seq}</Muted>}</span>],
        ["Delivery", `${humanize(h.delivery)}${h.last_error_code ? ` · last error ${h.last_error_code}` : ""}`],
        ["Production status", humanize(t.status)],
        ["Owner (production)", t.assigned_agent ?? NOT_YET],
        ["Stage C task", h.remote_task_key ?? NOT_YET],
        ["Released", <At key="rl" t={h.released_at} />],
      ]} />
    </Section>}

    <Section title="Changed files">
      {/* No endpoint returns the file list; the PR is where it is. */}
      <p className="text-[13px]">{NOT_YET}{remotePr || pr?.state === "verified" ? <Muted> · see the PR for the diff</Muted> : null}</p>
    </Section>

    <Section title="Tests, review and CI" description="Stage C reported, bound to the tested and reviewed fingerprint.">
      {!rt ? <p className="text-[13px]">{NOT_YET}</p> : <Facts items={[
        ["Tests", <Badge key="t" v={testView(rt)} />],
        ["Independent review", <Badge key="r" v={reviewView(rt)} />],
        ["Pull request", remotePr ? <a key="p" href={remotePr} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-medium underline-offset-2 hover:underline">
          PR #{rt.pr!.number}<ExternalLink className="size-3" aria-hidden /><span className="sr-only">(opens GitHub)</span></a> : NOT_YET],
        ["PR head", <Sha key="h" s={rt.pr?.head_sha ?? null} />],
        ["CI", <Status key="c" value={ciView(rt).kind} label={ciView(rt).label} tone={ciView(rt).tone} />],
        ["Merge", h!.merge.state === "VERIFIED" ? <span key="m">Verified · <Sha s={h!.merge.commit} /> · <At t={h!.merge.at} /></span> : h!.merge.state === "NOT_OBSERVED" ? "Not observed" : NOT_YET],
      ]} />}
    </Section>

    <Section title="Production record">
      <Facts items={[
        ["Verified PR", pr?.state === "verified" ? <a key="p" href={pr.pr.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-medium underline-offset-2 hover:underline">
          PR #{pr.pr.number}{pr.pr.merged ? " · merged" : ""}<ExternalLink className="size-3" aria-hidden /></a>
          : pr?.state === "unverified" ? "Unverified (no link)" : NOT_YET],
        ["Commit", <Sha key="c" s={t.pull_request?.commit ?? null} />],
        ["Supervisor verdict", t.supervisor ? `${humanize(t.supervisor.verdict)}${t.supervisor.notes ? `: ${t.supervisor.notes}` : ""}` : NOT_YET],
        ["Result", t.result || NOT_YET],
        ["Error", t.error || <Muted key="e">None recorded</Muted>],
      ]} />
    </Section>

    <Section title="History">
      <Panel>
        {[
          ...(rt?.history ?? []).map((s, i) => ({ id: `s${i}`, at: s.at, title: `Stage C: ${humanize(s.stage)}`, meta: "Stage C reported" })),
          ...t.runs.map(r => ({ id: `r${r.id}`, at: r.started_at, title: `Run ${humanize(r.status)}${r.provider ? ` · ${r.provider}` : ""}`,
            meta: [r.supervisor_verdict && `verdict ${humanize(r.supervisor_verdict)}`, r.error].filter(Boolean).join(" · ") || "Agent run" })),
          ...events.map(e => ({ id: `e${e.id}`, at: e.at, title: e.title, meta: humanize(e.type) })),
        ].sort((a, b) => (Date.parse(b.at ?? "") || 0) - (Date.parse(a.at ?? "") || 0))
          .map(x => <PanelRow key={x.id} title={x.title} meta={x.meta} trail={<At t={x.at} />} />)}
        {!rt?.history?.length && !t.runs.length && !events.length && <PanelEmpty title="No history reported yet" />}
      </Panel>
    </Section>
  </>
}
