import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { stageCPresent } from "@/lib/stage-c-gate"
import { useDesk } from "@/state"

/** Whether this host proves Stage C exists (lib/stage-c-gate.ts). Reads GET /stage-c/state on load and every
 *  5 minutes while online. Until an answer arrives, or on any error, Stage C stays hidden. */
export function useStageCPresent(): boolean {
  const { data, online } = useDesk()
  const [raw, setRaw] = useState<unknown>(null)
  useEffect(() => {
    if (!online) return
    let live = true
    // ponytail: a fixed 5-minute re-read; Stage C presence changes only on a host deploy or acceptance.
    const read = () => api<unknown>("/stage-c/state").then(r => { if (live) setRaw(r) }, () => { if (live) setRaw(null) })
    void read()
    const id = setInterval(read, 5 * 60_000)
    return () => { live = false; clearInterval(id) }
  }, [online])
  return stageCPresent(raw, data)
}
