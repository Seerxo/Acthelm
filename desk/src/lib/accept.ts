// The task's PR link and the founder's "Accept as done" (POST /tasks/{key}/status, verified founder only; DONE needs
// evidence). The link is only the backend's verified publication (app/desk.py _pull_request); the Desk never builds one.
import type { TaskDetail } from "./api"

type PR = NonNullable<TaskDetail["pull_request"]>
const SHA = /^[0-9a-f]{7,40}$/
const GITHUB = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+$/

/** "verified" (a link may be shown), "unverified" (the task and its publication disagree: no link), or none. */
export function prLink(t: Pick<TaskDetail, "pull_request">): { state: "verified"; pr: PR & { url: string; number: number } } | { state: "unverified" } | null {
  const pr = t.pull_request
  if (!pr || typeof pr !== "object") return null
  return pr.verified === true && typeof pr.url === "string" && GITHUB.test(pr.url) && typeof pr.number === "number"
    ? { state: "verified", pr: pr as PR & { url: string; number: number } } : { state: "unverified" }
}

/** A REVIEW task whose verified PR is merged: waiting only for the founder to accept it as done. */
export const mergedForReview = (t: Pick<TaskDetail, "status" | "pull_request">) =>
  t.status === "REVIEW" && prLink(t)?.state === "verified" && t.pull_request!.merged === true

/** The DONE evidence: the merged PR URL plus the merge commit, else the PR URL plus its head commit. */
export function acceptEvidence(t: Pick<TaskDetail, "status" | "pull_request">): string | null {
  if (!mergedForReview(t)) return null
  const { url, merge_commit, commit } = t.pull_request!
  return typeof merge_commit === "string" && SHA.test(merge_commit) ? `${url} merged as ${merge_commit}`
    : typeof commit === "string" && SHA.test(commit) ? `${url} at head ${commit}` : null
}

/** What the founder may accept: a merged PR, or (explicitly) a REVIEW task with a recorded result and no PR. Never a code task
 *  whose PR is unmerged or unverified: that waits for the merge. */
export function acceptOffer(t: Pick<TaskDetail, "status" | "pull_request" | "context" | "key" | "result">): { evidence: string; merged: boolean } | null {
  if (t.status !== "REVIEW") return null
  const evidence = acceptEvidence(t)
  if (evidence) return { evidence, merged: true }
  if (t.pull_request || (t.context as { write?: unknown } | null)?.write === true || !t.result?.trim()) return null
  return { evidence: `${t.key}: founder reviewed the recorded result in the Desk and accepted it`, merged: false }
}

/** The exact body sent. No `reason`: the API audits `reason or evidence`, so a reason would hide the evidence. */
export const acceptBody = (evidence: string) => ({ status: "DONE", actor: "founder", evidence })
