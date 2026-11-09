// In-page tabs between the pages of one section (Tasks | Stage C | Activity, Agents | Automation, Metrics | Finance,
// System | Incidents | Memory). Each tab is its own route, so deep links and the browser history keep working.
// The section model is lib/nav.ts; gated pages (Stage C) show only where the host proves them (lib/stage-c-gate.ts).
import { PAGE_LABEL, sectionOf, visibleTabs, type Page } from "@/lib/nav"
import { useStageCPresent } from "@/hooks/use-stage-c"
import { useDesk } from "@/state"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"

export function PageTabs({ page }: { page: Page }) {
  const { go } = useDesk()
  const stageC = useStageCPresent()
  const tabs = visibleTabs(sectionOf(page), stageC)
  if (tabs.length < 2 || !tabs.includes(page)) return null
  return (
    // ponytail: desktop only while App's SectionTabs (lg:hidden) carries the same tabs on phones; one row per width.
    <Tabs value={page} onValueChange={p => go(p as Page)} className="-mt-2 min-w-0 max-lg:hidden">
      <TabsList variant="line" aria-label="Section pages" className="h-11 max-w-full justify-start gap-4 overflow-x-auto border-b px-0 [scrollbar-width:none] w-full">
        {tabs.map(p => <TabsTrigger key={p} value={p} className="h-11 flex-none px-0.5 text-[13px] text-muted-foreground aria-selected:font-semibold aria-selected:text-foreground">{PAGE_LABEL[p]}</TabsTrigger>)}
      </TabsList>
    </Tabs>
  )
}
