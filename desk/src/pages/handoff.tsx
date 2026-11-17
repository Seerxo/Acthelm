// Handoff (B5): the founder records a code task, sees its approval state and releases it to the isolated Stage C host.
// Endpoints only (app/main.py): GET /desk/handoffs, GET /stage-c/state, POST /stage-c/tasks, GET /tasks/{key},
// POST /tasks/{key}/handoff. The API refuses anyone but the verified founder and any task it cannot release; the
// screen mirrors that (lib/handoff.ts) so an unauthorized task never even offers the button.
import { useCallback, useEffect, useState, type ComponentProps } from "react"
import { toast } from "sonner"
import { api, type TaskDetail } from "@/lib/api"
import { codeTaskBody } from "@/lib/founder-forms"
import { approvalState, releaseBlock, type HandoffList, type HandoffView } from "@/lib/handoff"
import { parseStageC } from "@/lib/stage-c"
import { when } from "@/lib/format"
import { useDesk } from "@/state"
import { Facts, Muted, PageHeader, Panel, PanelEmpty, PanelRow, Status, TaskKey } from "@/components/desk"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"

type Task = TaskDetail & { remote_stage_c?: HandoffView | null }
const BLANK = { title: "", objective: "", success: "", paths: [] as string[], context: "" }
const Code = ({ v }: { v: string }) => <code className="font-mono text-xs break-all select-all">{v}</code>

export function HandoffPage() {
  const { act, open } = useDesk()
  const [list, setList] = useState<HandoffList | null>(null)
  const [founder, setFounder] = useState<boolean | null>(null)
  const [allowed, setAllowed] = useState<string[] | null>(null)
  const [f, setF] = useState(BLANK)
  const [key, setKey] = useState<string | null>(null)      // the task this screen releases
  const [task, setTask] = useState<Task | null>(null)
  const [lookup, setLookup] = useState("")
  const [busy, setBusy] = useState(false)

  const readList = useCallback(() => api<HandoffList>("/desk/handoffs")
    .then(r => { setList(r); setFounder(true) })
    .catch(e => setFounder((e as { status?: number }).status === 403 ? false : null)), [])
  const readTask = useCallback((k: string) => api<Task>(`/tasks/${encodeURIComponent(k)}`).then(setTask), [])
  useEffect(() => {
    void readList()
    api<unknown>("/stage-c/state").then(r => setAllowed(parseStageC(r).code_task_paths ?? [])).catch(() => setAllowed([]))
  }, [readList])

  const built = codeTaskBody(f, allowed ?? [])
  const handoff = task?.remote_stage_c ?? null
  const block = releaseBlock({ founder, enabled: list?.enabled === true, status: task?.status ?? null, risk: task?.risk, handoff, bodyError: null })   // the API builds the packet from the stored task
  const state = approvalState(handoff)
  const retryable = handoff?.delivery === "SUSPENDED" || handoff?.delivery === "REFUSED"
  const set = (k: "title" | "objective" | "success" | "context") => (v: string) => setF(x => ({ ...x, [k]: v }))
  const toggle = (p: string) => setF(x => ({ ...x, paths: x.paths.includes(p) ? x.paths.filter(y => y !== p) : [...x.paths, p] }))
  // Recorded here, the text is exactly what the task holds, so it is frozen. Opened later, the Desk's copy may be
  // redacted, so the founder may correct it to the text first released (a retry with other text is RELEASE_CONFLICT).
  const [textLocked, setTextLocked] = useState(false)

  const record: ComponentProps<"form">["onSubmit"] = async e => {
    e.preventDefault()
    if (!built.body || busy) return
    setBusy(true)
    let made: string | null = null
    await act("/stage-c/tasks", built.body, r => { made = (r as { key?: string }).key ?? null; return `${made ?? "Task"} recorded as BACKLOG: not sent anywhere yet` })
    if (made) { setKey(made); setTextLocked(true); await readTask(made).catch(() => setTask(null)) }
    setBusy(false)
  }
  const load = async () => {
    const k = lookup.trim().toUpperCase()
    if (!k || busy) return
    setBusy(true)
    try {
      const t = await api<Task>(`/tasks/${encodeURIComponent(k)}`)
      // ponytail: GET /tasks/{key} does not return a task's Stage C paths, so the founder picks them again below.
      setF({ ...BLANK, title: t.title, objective: t.objective ?? "", success: t.success_criteria ?? "" })
      setKey(t.key); setTask(t)
    } catch (err) { setTask(null); setKey(null); setLookup(""); toast.error((err as Error).message) }
    setBusy(false)
  }
  const release = async () => {
    if (!key || block || busy) return
    setBusy(true)
    await act(`/tasks/${encodeURIComponent(key)}/handoff`, {}, r => `${key} released to Stage C · ${(r as { id?: string }).id ?? ""}`)
    await Promise.all([readTask(key).catch(() => undefined), readList()])
    setBusy(false)
  }
  const reset = () => { setF(BLANK); setKey(null); setTask(null); setLookup(""); setTextLocked(false) }

  return <>
    <PageHeader title="Send to Stage C" description="Record a code task, check its approvals, then release it to the isolated Stage C host. Nothing runs here." />
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <section aria-label="Task" className="panel grid min-w-0 content-start gap-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-[15px] font-semibold">{key ? <>Task <TaskKey k={key} /></> : "New code task"}</h2>
          {key && <Button size="sm" variant="ghost" onClick={reset} disabled={busy}>Start another</Button>}
        </div>
        {!key && <div className="flex flex-wrap items-end gap-2 text-[13px]">
          <label className="grid min-w-0 flex-1 gap-1.5 font-medium">Or open a recorded task
            <Input value={lookup} onChange={e => setLookup(e.target.value)} placeholder="TASK-0000" className="font-mono font-normal" /></label>
          <Button size="sm" variant="outline" onClick={() => void load()} disabled={busy || !lookup.trim()}>Open</Button>
        </div>}
        <form onSubmit={record} className="grid gap-4">
          <fieldset disabled={busy || (!!handoff && !retryable)} className="grid min-w-0 gap-4">
            <label className="grid gap-1.5 text-[13px] font-medium">Title
              <Input value={f.title} onChange={e => set("title")(e.target.value)} readOnly={textLocked} required minLength={3} maxLength={200} className="font-normal" /></label>
            <label className="grid gap-1.5 text-[13px] font-medium">Objective
              <Textarea value={f.objective} onChange={e => set("objective")(e.target.value)} readOnly={textLocked} required maxLength={2000} rows={2} className="font-normal" /></label>
            <label className="grid gap-1.5 text-[13px] font-medium">Success criteria
              <Textarea value={f.success} onChange={e => set("success")(e.target.value)} readOnly={textLocked} required maxLength={2000} rows={2} className="font-normal" /></label>
            <fieldset className="grid gap-1.5 text-[13px]"><legend className="mb-1.5 font-medium">Files it may change</legend>
              {allowed === null ? <Muted>Loading the allowed Stage C files…</Muted>
                : allowed.length ? allowed.map(p => <label key={p} className="flex min-h-11 items-center gap-2 font-mono text-xs break-all">
                    <input type="checkbox" checked={f.paths.includes(p)} onChange={() => toggle(p)} className="size-4" />{p}</label>)
                : <span className="text-warning">No allowed Stage C files are reported, so no code task can be sent.</span>}
            </fieldset>
            <label className="grid gap-1.5 text-[13px] font-medium">Read-only context files (optional, one per line)
              <Textarea value={f.context} onChange={e => set("context")(e.target.value)} rows={2} placeholder="app/models.py" className="font-mono text-xs" /></label>
          </fieldset>
          {built.error && (f.title || f.paths.length) ? <p role="status" className="text-xs text-muted-foreground">{built.error}</p> : null}
          {!key && <div><Button type="submit" disabled={busy || !built.body || founder === false} className="h-10 px-4">{busy ? "Recording…" : "Record task"}</Button></div>}
        </form>
      </section>

      <section aria-label="Approvals" className="panel grid min-w-0 content-start gap-4 p-5">
        <h2 className="text-[15px] font-semibold">Approvals</h2>
        {task ? <Facts items={[
          ["Status", <Status key="s" value={task.status} />],
          ["Risk", task.risk], ["Agent", task.assigned_agent ?? "none"],
          ["Founder release", <Status key="r" value={state.release[0]} tone={state.release[1]} label={state.release[0]} />],
          ["Coding approval", <Status key="c" value={state.coding[0]} tone={state.coding[1]} label={state.coding[0]} />],
        ]} /> : <Muted className="text-sm">Record or open a task to see its approvals.</Muted>}
        {/* ponytail: shows the API's refusal reason instead of hiding the button, so the founder always sees why. */}
        <div className="grid gap-2">
          <Button onClick={() => void release()} disabled={busy || block !== null} className="h-10">{busy ? "Working…" : handoff ? "Retry release" : "Release to Stage C"}</Button>
          {block && <p role="status" className="text-xs text-muted-foreground">{block}</p>}
          {!block && <p className="text-xs text-muted-foreground">Releases the task exactly as recorded; the server builds the packet from the stored task. Founder only; the API checks it again.</p>}
        </div>
      </section>
    </div>

    {handoff && <Panel title="Released: approve coding on Stage C">
      <div className="grid gap-3 px-4 py-4 text-[13px]">
        <Facts items={[["Handoff id", <Code key="i" v={handoff.id} />], ["Payload digest", <Code key="d" v={handoff.payload_sha256} />],
          ["Stage C task", handoff.remote_task_key ? <TaskKey key="k" k={handoff.remote_task_key} /> : "not delivered yet"],
          ["Released", handoff.released_at ? when(handoff.released_at) : null],
          ["First approval by", handoff.expires_at ? when(handoff.expires_at) : null]]} />
        <ol className="grid list-decimal gap-1.5 pl-5">
          <li>This host delivers the task to Stage C by itself; the Stage C task key appears above once it lands.</li>
          <li>In this browser, open the Stage C Desk. It signs you in with Stage C's own Access, never with this site's session.</li>
          <li>Open that task and approve coding once, naming this handoff id and payload digest. Stage C refuses an approval without both, and one that arrives after the time above.</li>
          <li>Mark it READY on Stage C. Progress and the pull request come back here; only you accept it as done.</li>
        </ol>
      </div>
    </Panel>}

    <Panel title="Released tasks" count={list?.handoffs.length}>
      {list === null ? <PanelEmpty title={founder === false ? "Founder only" : "Not reported"} />
        : !list.handoffs.length ? <PanelEmpty title="Nothing released to Stage C yet" />
        : list.handoffs.map(h => {
          const s = approvalState(h.handoff)
          return <PanelRow key={h.key} onClick={() => open({ kind: "task", key: h.key })} title={h.title}
            meta={<><TaskKey k={h.key} /> · {s.release[0]}</>} trail={<Status value={s.coding[0]} tone={s.coding[1]} label={s.coding[0]} />} />
        })}
    </Panel>
  </>
}
