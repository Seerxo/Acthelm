import { ShieldCheck } from "lucide-react"
import { ago, humanize, when } from "@/lib/format"
import { cn } from "@/lib/utils"
import { useData, useNow } from "@/state"
import { ClampText, EmptyState, Facts, PageHeader, FigureStrip } from "@/components/desk"
import { PageTabs } from "@/components/page-tabs"

export function IncidentsPage() {
  const d = useData()
  const unit = (k: string | null) => d.portfolio?.find(u => u.key === k)?.name ?? k
  const sev = (s: string) => d.incidents.filter(i => i.severity === s).length
  // The watchdog opens incidents; its evidence is the host reading (every 10 min). An empty list proves nothing unless
  // that reading is fresh (the same 25-minute slack as Automation's Watchdog loop).
  const at = d.health.resources.at, now = useNow("minute")
  const watched = !!at && now - new Date(at).getTime() <= 25 * 60_000
  return <>
    <PageHeader title="Incidents" />
    <PageTabs page="incidents" />
    {d.incidents.length > 0 && <FigureStrip label="Incidents at a glance" items={[
      { label: "Open", value: String(d.incidents.length), tone: d.incidents.length ? "warning" : undefined, sub: d.incidents.length ? "recorded and unresolved" : "none recorded" },
      { label: "SEV0–1", value: String(sev("SEV0") + sev("SEV1")), tone: sev("SEV0") + sev("SEV1") ? "danger" : undefined, sub: "highest severity" },
      { label: "Lower severity", value: String(d.incidents.length - sev("SEV0") - sev("SEV1")), sub: "SEV2 and below" },
    ]} />}
    {d.incidents.length ? <div className={cn("grid grid-cols-1 gap-4", d.incidents.length > 1 ? "xl:grid-cols-2" : "max-w-3xl")}>{d.incidents.map(i => (
      <article key={i.key} className="grid min-w-0 content-start gap-4 rounded-xl border bg-card p-5 shadow-xs [overflow-wrap:anywhere]">
        <header className="grid gap-1">
          <div className="flex items-center justify-between gap-3">
            <span className={cn("text-[11px] font-semibold tracking-[0.06em] uppercase", ["SEV0", "SEV1"].includes(i.severity) ? "text-destructive" : "text-warning")}>{i.severity}</span>
          </div>
          <h2 className="text-[15px] leading-snug font-semibold">{i.title}</h2>
          <p className="text-sm text-muted-foreground">{unit(i.business_unit) ?? "No business unit linked"}{i.service && ` · ${i.service}`} · open {ago(i.started_at)}</p>
        </header>
        {i.description && <div className="grid gap-1"><span className="text-xs text-muted-foreground">Impact</span><p className="text-sm"><ClampText>{i.description}</ClampText></p></div>}
        <Facts items={[["Status", humanize(i.status)], ["Root cause", i.root_cause ? <ClampText key="root-cause">{i.root_cause}</ClampText> : "Not yet identified"], ["Founder notified", i.founder_notified ? "Yes" : "No"]]} />
        <details className="group text-[13px]"><summary className="min-h-11 cursor-pointer list-none py-2 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden sm:min-h-0">Details</summary>
          <Facts items={[["Key", <span key="k" className="font-mono">{i.key}</span>], ["Detected by", i.detected_by], ["Started", when(i.started_at)]]} /></details>
      </article>))}</div>
      : !watched
        ? <EmptyState icon={<ShieldCheck />} title="No incidents recorded, not verified">{at ? `Watchdog last reported ${ago(at)} ago` : "The watchdog has not reported"}: an empty list is not proof of health.</EmptyState>
        : <EmptyState icon={<ShieldCheck />} title="No active incidents" />}
  </>
}
