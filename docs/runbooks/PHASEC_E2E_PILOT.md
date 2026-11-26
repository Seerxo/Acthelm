# Phase C real-system pilot: Desk → Stage C → GitHub → Desk DONE

Run this only after R3 is in production and the founder gives GO. It covers one harmless task, end to end.
The offline rehearsal is `tests/test_phasec_e2e_flow.py`, which drives the same transitions in process.
**[GO]** marks a founder-only step. Every other step is read-only evidence capture.
Store all evidence under `artifacts/e2e-<date>/NN-*.log`. Never store a token, a cookie or a packet body.

## 0. Preconditions (all read-only; any FAIL = stop)
| Check | Command / evidence | Pass |
|---|---|---|
| Prod SHA | prod `GET /stage-c/state` (API SHA) + `companyctl stage-c status` (worker heartbeat SHA) | equals the R3 release SHA |
| Stage C SHA | Stage C `GET /stage-c/state`; `cat /opt/company-os/.pilot-source-sha` | the API, the worker and the pilot file agree |
| Sender only on prod | prod env: `ENABLE_STAGE_C_HANDOFF=true`, no `…_RECEIVER`, `ENABLE_AUTONOMOUS_CODE_WRITE` unset/false | as stated |
| Receiver only on Stage C | Stage C env: `ENABLE_STAGE_C_HANDOFF_RECEIVER=true`, no sender flag; token length ≥ 32 (print the length only) | as stated |
| Host acceptance + live preflight | `sudo /opt/company-os/scripts/companyctl stage-c preflight --json` (Stage C) | `READY`, `source_sha` = the Stage C SHA |
| Worker disarmed | `sudo /opt/company-os/scripts/stage-c-arm --dry-run` | `DISARM …`, `ENABLE_AUTONOMOUS_CODE_WRITE=false` |
| No competing work | Stage C: no task READY/IN_PROGRESS with `write: true`; no live `stage-c-pilot-authorization`; no repo lease | none |
| Agents not paused on Stage C | Stage C `GET /desk/state` agents_paused | false |
| Production pause | prod `GET /desk/state` agents_paused. While paused, `sync` sends no delivery or activation (`app/worker.py:1284`, `app/stage_c_handoff.py:480-483`) | recorded (see step 2b) |
| Fork + token | the agent token has Read upstream (`publication_role` PASS) and can push to `seerxo-agent/acthelm` | PASS |
| Baseline | the event/audit max id on both DBs; `git -C <mirror> rev-parse main`; the count of `test_*` in `tests/test_metrics.py` | recorded |

Stop if anything is unknown. Do not "fix forward" during the pilot.

## 1. Pilot task (harmless, allow-listed)
- Write scope: exactly `tests/test_metrics.py` (in `SAFE_FILES`, `app/stage_c_worker.py:51`). No context files.
- Objective: add a single new test, `test_pilot_phasec_marker`, that asserts `metrics_store` imports cleanly. Change nothing else.
- Success criteria: a one-file diff with one added test; the full offline suite passes; an independent reviewer approves the fingerprint.

## 2. Transitions (each row: actor, action, evidence, then the stop condition)
1. **[GO] Desk create (prod).** The founder uses the Founder Desk → New code task (`POST /stage-c/tasks`) with the body from §1.
   Evidence: the prod task key P, status BACKLOG, and one `task.create` audit row whose actor is the founder principal.
   Stop if any other path is listed or the status is not BACKLOG.
2. **[GO] Release (prod).** On the Desk Handoff page, the founder clicks Release (`POST /tasks/P/handoff`).
   Evidence: the handoff `id` H, the `payload_sha256` D, P in WAITING_EXTERNAL, and one `handoff.release` row.
   Stop if a repeated click gives a new id.
2b. **[GO] Production unpause, only if it is paused.** First capture evidence that no other production task is admissible.
   That means every READY or BACKLOG task listed with its admission reason, because unpausing lets the production worker claim READY work.
   The founder then unpauses. Re-pause right after step 3 if wanted: polls and DONE continue while paused.
3. **Delivery + activation (prod worker tick, ≤ 2 min).** Evidence: `GET /desk/handoffs` shows `delivery=DELIVERED` and `remote_task_key=S`.
   Prod audit has `handoff.delivery` then `handoff.activation`. Stage C audit has exactly one `handoff.import` and one `handoff.activate` for `handoff:H`.
   Stage C S is BACKLOG with `created_by=service:handoff`. Stop on any `last_error_code` (`HTTP_4xx`, `BAD_MAC`, `RELEASE_*`).
4. **Publication is part of the arm (no `.env` change).** `docker-compose.yml` pins `STAGE_C_PUBLISH_ENABLED: "false"` on the worker;
   only the `stage-c-arm` override sets it to `true`, together with `ENABLE_AUTONOMOUS_CODE_WRITE=true` and `STAGE_C_ARMED_TASK=S`,
   and disarm recreates the worker without it. The worker publishes only when the flag is `true` AND it is armed for exactly this task
   (`stage_c_runner.publication_enabled`). Evidence: before Start Pilot, `stage-c-arm --dry-run` shows `STAGE_C_PUBLISH_ENABLED=false`.
5. **[GO] Start Pilot (Stage C Desk).** The founder authorizes S and names H, D and the shown SHA
   (`POST /stage-c/authorization`, 30 min). Evidence: one `stage-c.authorization.issue` row whose value carries `handoff_id=H` and `payload_sha256=D`.
   Within 20 s, `journalctl -t stage-c-arm` shows `action=ARM … task=S`. `stage-c-arm --dry-run` shows `STAGE_C_ARMED_TASK=S STAGE_C_PUBLISH_ENABLED=true`.
   Steps 5 and 6 must fit inside the grant TTL: READY needs an unexpired grant.
6. **[GO] Mark READY (Stage C Desk).** `POST /tasks/S/status READY`. A 409 names the failing gate: stop and record it.
7. **Code → checks → review → PR (worker, ≤ 40 min).** Evidence: run R STARTED → SUCCEEDED with `tool_calls.outcome=PR_OPEN` and `model_calls=2`.
   The grant has `consumed_by_run_id=R`. History runs DETECTED → … → INDEPENDENT_REVIEW (`checks_passed=true`, coder ≠ reviewer) → PR_OPEN.
   The PR on `seerxo/acthelm` has head repo `seerxo-agent/acthelm`, branch `task/S`, and a one-file diff (`gh pr view N --json headRepositoryOwner,files`).
   Afterwards `stage-c-arm` logs `DISARM stage_c_authorization_consumed`, and `--dry-run` shows `STAGE_C_PUBLISH_ENABLED=false` again.
   Stop on BLOCKED/FAILED (record `task.error` and `run.tool_calls`), a second run, or any file other than `tests/test_metrics.py`.
8. **CI (GitHub, then worker reconcile).** Evidence: `gh pr checks N`, all required workflows green on the PR head; S stage READY_TO_MERGE.
   On prod, the Desk snapshot shows `ci.state=SUCCESS` and `merge=NOT_OBSERVED`. A DONE attempt on P must return **409**; capture it.
9. **[GO] Founder merges on GitHub** (the founder's own login, merge into `main`). Evidence: `gh pr view N --json mergedBy,mergeCommit`.
   S has one MERGED history entry, actor `founder (github:<login>, via founder_github)`, with `merge_commit=M`.
10. **MERGED (Stage C reported).** Stage C stays at stage MERGED for an imported task (its own host SHAs are never this task's deploy)
    and reads main CI on M from GitHub. Evidence: `GET /desk/handoffs` row has `freshness=FRESH`,
    `snapshot.merge={state: MERGED, commit: M, main_ci: {state: SUCCESS, sha: M}}` once main is green, `snapshot.task_key=S`,
    `authorization.status=CONSUMED`, and `verification.state=NOT_VERIFIED`. P is still WAITING_EXTERNAL; DONE returns **409**: capture it.
    Stop if main CI on M is `FAILURE` (Stage C S goes BLOCKED: a regression on main).
11. **[GO] Production release to M, then VERIFIED (production decides).** Production can only verify what it runs: release production to
    exactly M through the normal release path (founder GO, separate from this pilot). Production has no git history, so a later commit on
    top of M does NOT verify it (fail closed; see open decision in section 5). On the next poll the production worker checks its own
    watchdog report (API + worker SHA = M, fresh) and its own `COMPANY_OS_GIT_SHA`; the API rechecks its own SHA at DONE.
    Evidence: one prod audit `handoff.production_verification` with reason `VERIFIED M`; `/desk/handoffs` shows `verification.state=VERIFIED`,
    `production_api_sha=M`; the Desk shows "Verified · production".
12. **[GO] DONE (prod Desk).** `POST /tasks/P/status DONE` with evidence `PR N merged as M, production runs M`. Expect 200 only after step 11.
    Afterwards `handoff.*` traffic for H stops (no new prod audit rows on later ticks).

## 2a. Scripted read-only checklist (run after every step)
`scripts/phasec-e2e-check.py` judges all transitions from captured evidence. It makes no network call, starts no process and writes
no file (`tests/test_pilot_check.py` enforces that); the offline rehearsal drives it over real API reads
(`tests/test_phasec_e2e_flow.py`). Capture with GET only (founder Access session or SSH tunnel; never a POST):

| File (in `artifacts/e2e-<date>/check/`) | Capture |
|---|---|
| `prod-task.json` | `GET <prod>/tasks/P` |
| `prod-audit.json` | on the prod host (root): `docker exec company-api curl -fsS 'http://localhost:8000/audit?limit=5000'` (a loopback GET, caller `local`; `/audit` is not a Desk read). The limit is the newest rows across all entities: raise it if CREATE waits although the task exists |
| `sc-task.json` | `GET <stage-c>/tasks/S` |
| `sc-audit.json` | on the Stage C host: the same `docker exec company-api curl` GET |
| `pr.json` | `gh pr view N --repo seerxo/acthelm --json number,state,headRefName,headRepositoryOwner,files,mergedBy,mergeCommit` |
| `arm.txt` (optional) | `sudo /opt/company-os/scripts/stage-c-arm --dry-run` |

Run: `python3 scripts/phasec-e2e-check.py artifacts/e2e-<date>/check --founder-login <login>`. It prints
`CREATE → RELEASE → IMPORT → ACTIVATION → AUTHORIZATION → CODER → PR → CI → MERGED → VERIFIED → DONE`, each PASS/WAIT/STOP with its
evidence. Exit 0 = all PASS, 1 = in progress (first WAIT names what is missing), 3 = a STOP condition (section 3), 2 = unreadable evidence.
STOP conditions it checks: a second release/delivery/activation/import/authorization/run, a delivery or poll error in the hard set
(`BAD_MAC`, `BINDING_MISMATCH`, `HTTP_4xx`, `RELEASE_*`, `TASK_CHANGED`), a PR head outside the fork or any file but the pilot file,
CI SUCCESS on another SHA, red PR or main CI, a merge by anyone but the founder, a Stage C `VERIFIED` claim, DONE without an earlier
production `VERIFIED` audit row, and a worker still armed after DONE.

## 3. Stop conditions (any one: Stop Pilot, then investigate; never retry blindly)
- An unexpected 4xx/5xx, `BAD_MAC`, `BINDING_MISMATCH`, `STALE` repeating for more than 3 min, or a SHA mismatch between the API and the worker.
- A second AgentRun, a second import or grant for H, or any claim or run on a task other than S while armed.
- A push to the upstream repo, a PR head outside the fork, a diff touching anything but the one file, or a red required check.
- A merge by anyone but the founder (S goes BLOCKED with `MERGE_NOT_RECORDED`, which is correct). The worker stays armed after its run.
- Production DONE accepted before step 10, or any production task other than P changing.

Stop Pilot: Stage C Desk → Stop (`POST /stage-c/authorization/revoke`). Within 20 s, `stage-c-arm` logs DISARM.
Prod cancel, if needed: `POST /tasks/P/status CANCELLED` → `handoff.cancel_acknowledged`.

## 4. Cleanup (always)
1. Confirm `stage-c-arm --dry-run` shows `ENABLE_AUTONOMOUS_CODE_WRITE=false STAGE_C_PUBLISH_ENABLED=false` (no `.env` to restore).
2. Re-pause production if it was paused before 2b. Confirm `stage-c-arm --dry-run` shows DISARM and that no `stage-c-pilot-authorization` row exists. Revoke it if one does.
3. **[GO]** Close S on Stage C (`POST /tasks/S/status DONE`, evidence `prod P DONE, M`). Nothing closes it automatically (gap G3).
4. Remove the `jobs/S` directory only after recording `tool_calls` and history. The remote branch `task/S` on the fork may be deleted after merge.
5. Revert the pilot test only through a normal reviewed PR, if the founder wants it gone. Never by force-push.
6. Archive the evidence index (`ls artifacts/e2e-<date>`) and the before/after audit max ids for both DBs.

## 5. Known gaps and open decisions
- **G1 (fixed, R5):** MERGED (Stage C report), VERIFIED (production) and DONE (founder) are distinct; DONE needs production VERIFIED.
- **G4 (fixed, R5):** verification uses production's own runtime SHAs, never Stage C's. Open decision: production checks equality
  with M (no git history in the container). Deploying a later main on top of M leaves the task NOT_VERIFIED; options are a
  GitHub compare read on production, or a release-ledger field naming the commits a release contains.
- **G5 (fixed, R5):** publication is armed and disarmed with the code write; no `.env` flag.
- **G2 (open, `app/control.py` authorize_pilot):** a Start Pilot for a native task replaces a live handoff-bound grant: the
  "another task's grant is still live" guard runs only when the new grant has a handoff binding. Do not Start Pilot any other task
  while S is authorized or running.
- **G3 (open):** after production DONE, the Stage C import stays at REVIEW/MERGED. Sync stops sending (`sync` skips closed tasks);
  closing it needs a new production → Stage C close message. Cleanup step 3 closes it by hand.
