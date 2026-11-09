// Releases: what runs in production and the delivery evidence behind it, from data the Desk already has (lib/releases.ts).
// The release gateway's ledger is root-only and not in this read model, so it reads "Not reported": a merged pull
// request or a green CI is never shown as a deployment, and a PR number appears only for a verified publication.
import { ago, when } from "@/lib/format"
import { releaseRows, runningVersion } from "@/lib/releases"
import { useData, useDesk } from "@/state"
import { Disclosure, Dot, Facts, Muted, PageHeader, Panel, PanelEmpty, Status, TaskKey } from "@/components/desk"
import { DataRows } from "@/components/data-rows"

export function ReleasesPage() {
  const d = useData()
  const { open } = useDesk()
  const v = runningVersion(d.runtime)
  const rows = releaseRows(d.improvements)
  const merged = rows?.filter(r => r.merged).length ?? 0
  return <>
    <PageHeader title="Releases" meta={v ? <span className="num">Running <span className="font-mono text-foreground">{v.api}</span>{v.startedAt && ` · started ${ago(v.startedAt)} ago`}</span> : undefined} />
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
      <section aria-label="Running in production" className="panel grid min-w-0 content-start gap-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-[15px] font-semibold">Production</h2>
          {v ? <Status value={v.drift.length ? "DRIFT" : "RUNNING"} tone={v.tone} label={v.drift.length ? "Drift" : v.api === "unknown" ? "Version unknown" : "Running"} />
            : <Status value="UNKNOWN" tone="warning" label="Not reported" />}
        </div>
        {v ? <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          {([["API", v.api], ["Worker", v.sameCommit ? "same commit" : v.worker], ["Schema", v.schemaOk ? `${v.schema} · current` : v.schema]] as const).map(([k, x]) => (
            <div key={k} className="grid min-w-0 gap-1"><dt className="text-xs text-muted-foreground">{k}</dt>
              <dd className="truncate font-mono text-[15px] font-semibold">{x}</dd></div>))}
        </dl> : <p className="text-sm text-muted-foreground">Runtime facts are not reported by this API.</p>}
        {v && v.drift.length > 0 && <p className="text-sm text-destructive [overflow-wrap:anywhere]">{v.drift.join(" · ")}</p>}
      </section>
      <section aria-label="Release gateway" className="panel grid min-w-0 content-start gap-2 p-5">
        <div className="flex items-center justify-between gap-2"><h2 className="text-[15px] font-semibold">Release gateway</h2><Status value="UNKNOWN" tone="neutral" label="Not reported" /></div>
        <p className="text-[13px] text-muted-foreground">Candidate, signature and ledger are not visible to the Desk.</p>
      </section>
    </div>
    <Panel title="Delivered code" count={rows?.length} action={rows && rows.length > 0 ? <span className="num">{merged} merged</span> : undefined}>
      {rows === undefined ? <PanelEmpty title="Not reported" />
        : !rows.length ? <PanelEmpty title="No pull request in flight or merged" />
        : <div className="p-2 sm:p-3"><DataRows label="Delivered code" rows={rows} rowKey={r => r.task} titleHead="Task" onRowClick={r => open({ kind: "task", key: r.task })}
            title={r => r.title} desc={r => <TaskKey k={r.task} />}
            cols={[
              { head: "PR", cell: r => r.verified ? <span className="font-mono">{r.pr}</span> : <Status value="UNVERIFIED" tone="warning" label="unverified" /> },
              { head: "Updated", className: "num text-right text-muted-foreground", cell: r => r.at ? <span title={when(r.at)}>{ago(r.at)} ago</span> : <Muted>unknown</Muted> },
            ]}
            aside={{ head: "Stage", cell: r => <Status value={r.stage} tone={r.tone} label={r.stage} /> }} /></div>}
    </Panel>
    <Disclosure title="Details" hint="commits, schema head, evidence">
      {v ? <Facts items={[["API commit", <span key="a" className="font-mono">{d.runtime?.api.git_sha ?? "unknown"}</span>],
        ["Worker commit", <span key="w" className="font-mono">{d.runtime?.worker.git_sha ?? "unknown"}</span>],
        ["Worker image", d.runtime?.worker.image ?? "unknown"], ["API started", v.startedAt ? when(v.startedAt) : "unknown"],
        ["Database schema", v.schema], ["Code head", v.codeHead], ["Drift", v.drift.length ? v.drift.join(" · ") : "none"]]} />
        : <Muted className="text-sm">Runtime facts not reported.</Muted>}
      {rows && rows.some(r => r.evidence.length > 0) && <ul className="grid gap-2 text-[13px]">
        {rows.filter(r => r.evidence.length).map(r => <li key={r.task} className="flex flex-wrap gap-x-2"><Dot tone={r.tone} className="mt-1.5" />
          <TaskKey k={r.task} /><span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{r.evidence.join(" · ")}</span></li>)}
      </ul>}
    </Disclosure>
  </>
}
