import { useEffect, useState } from "react"

/** A clock that re-renders every `ms`, so freshness expires on screen even when no request succeeds. */
export function useNow(ms = 10_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), ms); return () => clearInterval(t) }, [ms])
  return now
}
