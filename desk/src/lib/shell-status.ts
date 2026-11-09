// The three facts the shell header shows on every page: are agents allowed to run (and how many are), is the worker
// heard from, and how many decisions wait for the founder. Read only from /desk/state through the same helpers the
// pages use (operating.ts vitals, agent-state.ts, approvals.ts), so the header never disagrees with the page it opens.
// Missing data reads "unknown", never 0 or "running".
import type { DeskState, Tone } from "./api"
import { agentState } from "./agent-state.ts"
import { approvalSummary } from "./approvals.ts"
import { vitals } from "./operating.ts"

export interface ShellFact { key: "agents" | "worker" | "approvals"; label: string; value: string; short: string; tone: Tone; page: "agents" | "automation" | "approvals" }

export function shellStatus(d: DeskState, now: number): ShellFact[] {
  const auto = vitals(d, now).find(v => v.key === "automation")!
  const running = d.agents.filter(a => agentState(a, d, now).running).length
  const agents: ShellFact = d.control.agents_paused
    ? { key: "agents", label: "Agents", value: "Paused", short: "Paused", tone: "warning", page: "agents" }
    : { key: "agents", label: "Agents", value: running ? `${running} running` : "None running", short: String(running),
        tone: running ? "info" : "neutral", page: "agents" }
  const worker: ShellFact = {
    key: "worker", label: "Worker", value: auto.value, tone: auto.tone, page: "automation",
    short: auto.tone === "success" ? "OK" : auto.tone === "danger" ? "Down" : d.control.agents_paused ? "Paused" : "Check",
  }
  const s = approvalSummary(d)
  // Approval records and tasks stopped at WAITING_APPROVAL are listed separately on the Approvals page; this is their sum.
  const n = s.records + s.waitingTasks
  const approvals: ShellFact = { key: "approvals", label: "Approvals", value: n ? `${n} waiting` : "None waiting",
    short: String(n), tone: n ? "warning" : "neutral", page: "approvals" }
  return [agents, worker, approvals]
}
