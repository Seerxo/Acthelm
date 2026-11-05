# Stage C Desk read model, version 1

`GET /stage-c/state` belongs to DESK scope and additionally requires the verified
founder principal in the handler, including in log auth mode. It only reads the
database and the API source SHA environment variable. It calls no model, GitHub,
Git, launcher, or authorization writer. The reported write flag changes nothing.

The exact response shape follows. `?` means nullable; objects marked `?` are null
when absent. Arrays are bounded as shown. Timestamps are parsed and emitted in UTC.
SHA means 40 lowercase hexadecimal characters; fingerprint means 64. Task keys
match the existing Stage C key validator. Other string values are fixed codes or
paths from `stage_c_worker.SAFE_FILES`. There are no PR URLs or issuer identities.

```text
{
  version: 1, generated_at: timestamp,
  state: ACTIVE|READY|BLOCKED|FAILED|EXPIRED|UNKNOWN|NOT_VERIFIED,
  blockers: code[], next_action: code,
  last_ready_pr: {task_key: key?, number: integer?, head_sha: SHA?, stage: READY_TO_MERGE|MERGED}?,
  runtime: {
    api_sha: SHA?, worker_sha: SHA?, reasoner_sha: null, sha_agree: false|null,
    worker_heartbeat: HEALTHY|STALE|PAUSED|DEGRADED|NO_DATA|null,
    write_enabled: boolean?, worker_ready: boolean?, agents_paused: boolean,
    acceptance: {source_sha: SHA?, accepted_at: timestamp?, valid: boolean?},
    preflight: {status: PASS|FAIL|STALE|NOT_VERIFIED, checked_at: timestamp?, source_sha: SHA?},
    host_facts: {host_checks: NOT_VERIFIED, reasoner_sha: NOT_VERIFIED,
                 api_sha: RECORDED|NOT_VERIFIED, worker_sha: RECORDED|NOT_VERIFIED}
  },
  authorizations: {
    one_task: {task_key: key?, source_sha: SHA?, expires_at: timestamp?,
               revoked: boolean, issuer: founder|operator|unknown, status: grant_status}?,
    standing: {source_sha: SHA?, paths: safe_path[<=20], expires_at: timestamp?,
               max_per_day: integer?, used_today: integer?, remaining_today: integer?,
               revoked: boolean, issuer: founder|operator|unknown, status: grant_status}?
  },
  tasks: [{
    key: key?, status: TaskStatus?, stage: StageCStage?,
    attempts: integer, retry_count: integer?, repairs: integer?,
    started_at: timestamp?, updated_at: timestamp?,
    test_verdict: PASSED|FAILED|null, reviewer_verdict: APPROVED|REJECTED|UNVERIFIED|null,
    tested_fingerprint: fingerprint?, reviewed_fingerprint: fingerprint?, fingerprint_match: boolean?,
    pr: {number: integer?, head_sha: SHA?}?, ci: {state: SUCCESS|FAILURE|PENDING|STALE|MERGED_OBSERVED|null, sha: SHA?}?,
    last_outcome: outcome_code?, failure_category: failure_code?, needs_founder: boolean?,
    history: [{stage: StageCStage, at: timestamp?} <=12]
  } <=20],
  metrics: {
    tasks: {started: integer, reached_ready_to_merge: integer, stopped: integer,
            in_progress: integer, abandoned: integer},
    attempts: {total: integer, failed: integer}, model_calls: {total: integer?},
    minutes: number?, tokens: integer?, usd_recorded: number?,
    usd_note: excludes_subscription_cost, quota_stops: integer, truncated: boolean
  }
}
```

Privacy publication stops retain only `PRIVACY_GATE_BLOCKED` or `PRIVACY_GATE_ERROR`;
unknown outcomes become `OTHER`. No raw scanner detail is returned, and these
display codes do not change authorization or publication decisions.

Grant statuses are `ACTIVE`, `CONSUMED`, `EXPIRED`, `DAILY_LIMIT`, `INVALID`,
`SHA_MISMATCH`, or `REVOKED`, as applicable to the grant type. Each grant uses its
own existing validator. Standing usage comes from `value.used` for the UTC day;
an earlier day's count becomes zero. Malformed current-day counts become null.
A revoked standing grant retains its observed usage and has zero remaining.
Revocation is recovered from the latest issue/revoke audit when the live row is
absent; a later issue replaces that historical revocation.

State precedence is deterministic:

1. `UNKNOWN`: API/worker SHA missing, or heartbeat missing, NO_DATA, or STALE.
2. `BLOCKED`: API/worker SHA mismatch, agents paused, writes off/unknown,
   acceptance invalid/missing, unhealthy worker, or watchdog preflight FAIL.
3. `ACTIVE`: any listed task is IN_PROGRESS, including an already authorized
   attempt whose grant was subsequently revoked.
4. `FAILED` or `BLOCKED`: the newest listed task has that status.
5. `NOT_VERIFIED`: preflight missing/stale or worker readiness not confirmed.
6. With no active grant: `EXPIRED` when a grant expired and none is revoked;
   otherwise `BLOCKED`.
7. Otherwise `READY`. A ready-to-merge task can still need founder review.

Blockers are accumulated independently of state. Codes are `api_sha_unknown`,
`worker_sha_unknown`, `sha_mismatch`, `worker_not_healthy`, `agents_paused`,
`write_disabled`, `worker_not_ready`, `host_not_accepted`,
`preflight_not_verified`, `preflight_stale`, `preflight_fail`, `no_authorization`,
`task_needs_founder`, or a grant type (`one_task`/`standing`) plus its fixed
lowercase status. A standing limit therefore reports `standing_daily_limit`
even with a consumed pilot present. Neither denial behavior nor admission rules
are changed. `next_action` uses the ordered mapping in `stage_c_state.NEXT_ACTIONS`:
deploy/worker/SHA/pause/acceptance/flag/preflight/task review/daily reset/issue grant,
then `none` when no blocker matches. These codes are advice, never actions.

CI evidence is read only after the latest PR_OPEN. A missing or different SHA
becomes STALE, never SUCCESS. Post-merge observation has its own MERGED_OBSERVED
code. Reviewer approval needs matching tested/reviewed/published fingerprints
and the existing matching review audit; otherwise it is UNVERIFIED. History
exposes only the latest 12 recognized stages and normalized times.

The newest 5,000 recorded Stage C runs bound the task/attempt metrics and task
list; `truncated` is true when more runs exist. Task counts deduplicate attempts.
`last_ready_pr` independently queries all tasks currently at READY_TO_MERGE or
MERGED, even without a run or outside either window. It reports recorded stage,
not fresh GitHub verification. Token totals include only runs with both token
counts recorded; no recorded pair means null. Costs are recorded amounts only
and exclude subscription cost. Only a combined model-call count exists today;
coder/reviewer splits are omitted.

The reasoner SHA is not recorded in this API's database. Consequently full
three-process SHA agreement is null (false if API/worker already disagree),
never guessed true. API/worker SHAs are recorded evidence, and host facts remain
NOT_VERIFIED here. Watchdog readiness is a timestamped database observation,
not a fresh execution of host checks.

Revoke regression coverage pins both grant types: an in-scope task is allowed,
the server-side revoke deletes and audits the grant, a new task receives the
grant-specific validator denial, another DB session still sees revocation, and
replaying the old value without its issue audit is refused. Current admission
still returns the legacy `stage_c_authorization_missing` when no live grant row
exists, including after a standing revoke. The read model reports standing
revocation from its audit instead of forwarding that generic pilot reason.

Current running-attempt behavior is deliberately unchanged: authorization is
consumed at attempt start, but the final admission/publication gates do not
recheck grants. Revoking either grant during review therefore permits that
already running attempt to reach publication; the next claim is denied. Tests
exercise the real worker with model/Git/publication boundaries doubled. The
agents pause kill switch is the existing pre-publication stop mechanism.
Restoring an old value *with* its matching issue audit is outside the specified
no-audit replay guarantee; this PR does not harden or alter that decision.

Only targeted local tests are authorized for this change. Full CI, PostgreSQL
concurrency/integration, independent review, and live/runtime acceptance remain
unverified. No commit, push, deployment, grant issuance on a live database, or
network request is part of this work.
