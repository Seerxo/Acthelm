// GET /stage-c/state for Home: the same founder-only read the Stage C page makes (lib/stage-c.ts parses it).
import { useEffect, useState } from "react"
import { api } from "@/lib/api"
import { loadError, parseStageC, type LoadError, type StageCState } from "@/lib/stage-c"
import { useDesk } from "@/state"

export interface StageCRead { state: StageCState | null; error: LoadError | null; fetchedAt: number }

/** GET /stage-c/state, re-read with the Desk read model (live stream or its poll). Founder only: a Desk token gets 403,
 *  which Home shows as unknown. A sign-out is left to the shell's own read (state.tsx). */
export function useStageC(): StageCRead {
  const { lastOk } = useDesk()
  const [read, setRead] = useState<StageCRead>({ state: null, error: null, fetchedAt: 0 })
  // Heartbeat, preflight and grant age change without a commit the stream would announce: re-read every 30s while visible.
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) setTick(n => n + 1) }, 30_000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    let live = true   // a newer read (or unmount) wins; an older answer never overwrites it
    api<unknown>("/stage-c/state").then(
      raw => { if (live) setRead({ state: parseStageC(raw), error: null, fetchedAt: Date.now() }) },
      e => {
        if (!live) return
        const kind = loadError(e)
        // Keep the last read (shown stale) unless the session is gone or the scope was refused.
        setRead(r => ({ ...r, error: kind, state: kind === "signed_out" || kind === "forbidden" ? null : r.state }))
      })
    return () => { live = false }
  }, [lastOk, tick])
  return read
}
