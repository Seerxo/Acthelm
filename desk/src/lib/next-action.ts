// The one primary action of a task's detail view. Every other offered control stays, as a secondary button. The order
// follows the task's flow: accepting a finished result first, then a pending approval, then a code task's Stage C
// authorization (Mark READY waits on it), then READY, then assignment, then a follow-up for closed work.
export type NextAction = "accept" | "approve" | "authorize" | "ready" | "assign" | "follow-up"

export function primaryAction(o: { accept: boolean; approve: boolean; authorize: boolean; ready: boolean; readyBlocked: boolean
  assign: boolean; followUp: boolean }): NextAction | null {
  if (o.accept) return "accept"
  if (o.approve) return "approve"
  // An authorized code task is marked READY next; an unauthorized one is authorized first.
  if (o.ready && !o.readyBlocked) return "ready"
  if (o.authorize) return "authorize"
  if (o.assign) return "assign"
  if (o.followUp) return "follow-up"
  return null
}
