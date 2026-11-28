"""Read-only reconciliation of every open task (Phase C, R5). Zero model calls, no GitHub, no writes.

For each task in an open status it answers: where is it, for how long, what (if anything) blocks it and with which
evidence, would the worker admit it, and what is the one next action — who does it, the exact operation, and the
precondition that triggers it. It never changes a task, an audit row or a lock: the founder (or a flag the founder
turns on) acts on the answer through the existing routes, which keep every one of their checks.

Two layers, so the API and an offline dry run use the same rules:
    facts_for(db, task, ...) -> Facts      reads the database (and only reads it)
    classify(Facts, System) -> dict        pure rules, no I/O

Facts an adapter cannot know are UNKNOWN, and UNKNOWN is never treated as admissible or as proof. Every gate below is
the existing one (worker._admission_gate, worker.ready_refusal, stage_c_worker.done_refusal,
stage_c_ci.merge_recovery_block, worker.resolution_evidence, stage_c_state's error classes); nothing is loosened.

Output is fixed codes, validated identifiers, numbers and server-written refusal reasons. Free task text (error notes,
results, context) is reduced to a class here; the API never re-emits it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import enums, models

VERSION = 1
UNKNOWN = "UNKNOWN"
OPEN = ("BACKLOG", "READY", "WAITING_EXTERNAL", "WAITING_APPROVAL", "BLOCKED", "REVIEW", "FAILED", "IN_PROGRESS")
# Not touched by anyone for this long and not admissible: the founder decides whether it is still wanted.
ARCHIVE_AFTER_D = 28

# Recommended actions (fixed codes).
KEEP_WAITING = "keep-waiting"
AUTO_PROGRESS = "system-progresses"
AUTO_RESUMABLE = "auto-resumable-when-flag-on"
STALE_CLAIM = "stale-claim-recovery"
FOUNDER_REVIEW = "founder-review"
FOUNDER_APPROVAL = "founder-approval"
FOUNDER_DECISION = "founder-decision-needed"
FOUNDER_READY = "founder-set-ready"
RETRY_EXHAUSTED = "retry-exhausted"
ARCHIVE = "archive-candidate"
CANCEL_ONLY = "stuck-cancel-only"
STALE_LOCK = "stale-lock"

# Buckets for the founder's summary.
B_SYSTEM = "system"              # the system progresses it by itself, now or once a named switch is on
B_CLICK = "founder-click"        # one founder action, no new judgement beyond reading what is there
B_DECISION = "founder-decision"  # the founder must decide what this task should become
B_STUCK = "stuck"                # every forward path is refused by the code; only cancel remains
BUCKETS = (B_SYSTEM, B_CLICK, B_DECISION, B_STUCK)

# Admission reasons that are runtime switches, not task defects: the task itself would be admitted.
RUNTIME_GATES = {"code_write_disabled", "stage_c_runtime_not_ready", "stage_c_launcher_cooling"}
AUTO_RESUME_CLASSES = ("CODER_CAPACITY", "LAUNCHER_REFUSED")
# Block classes whose next step is the founder inspecting the job or PR, then READY (ready_refusal still applies).
INSPECT_THEN_READY = {
    "REPAIR_INTERRUPTED": "inspect and discard the Stage C job directory",
    "JOB_UNFINISHED": "inspect the unfinished launcher request in the job directory",
    "STALLED": "check provider capacity, the independent reviewer and the lease",
    "UPSTREAM_ROLE_NOT_READ_ONLY": "restore the agent GitHub account to Read on the upstream repository",
    "UPSTREAM_ROLE_UNVERIFIED": "renew the worker GitHub token on the Stage C host",
    "OWNER_CHANGED": "confirm the new owner and re-authorize the task",
    "WORKER_INTERRUPTED": "confirm the interrupted attempt left nothing behind",
}
FOUNDER_JUDGEMENT = {
    "RUNTIME_ERROR": "an unclassified exception stopped the attempt; inspect the worker log for its run",
    "PUSH_FAILED": "the branch push failed; inspect the branch on the fork",
    "DRIFT": "the base moved under the attempt; decide whether to retry on the new base",
    "LEASE_LOST": "the repository lease was lost mid-attempt; inspect the job directory",
    "PRIVACY_GATE_BLOCKED": "the privacy gate matched the diff; inspect the reported paths",
    "PRIVACY_GATE_ERROR": "the privacy gate could not run; inspect the gate",
    "CHECKS_REPEATED": "the same tests failed twice; rewrite the task or cancel it",
    "CI_REPAIRS_EXHAUSTED": "CI kept failing after the bounded repairs; decide on the PR",
    "PR_HEAD_MOVED": "the PR head moved after review; decide on the PR",
    "MERGE_ELIGIBILITY_REFUSED": "the merge gate refused the PR; read the refusal reasons",
    "CI_APPROVAL_REQUIRED": "GitHub waits for a workflow approval on the PR",
    "CI_ENDED_WITHOUT_RESULT": "CI ended without a result; re-run CI or decide on the PR",
    "PR_CLOSED": "the PR was closed without merge; cancel or create a new task",
    "RESULT_RECOVERY": "a saved result could not be finalized; review the run",
    "PR_FAILED": "publication failed; inspect the existing branch/PR",
}


def _error_class(error: str) -> str | None:
    """The fixed class of a task error: stage_c_state's classes first, then the worker's own block markers."""
    from app import stage_c_state, worker  # late: keep classify() importable without the Stage C stack
    if not error:
        return None
    code = stage_c_state._failure(SimpleNamespace(error=error))
    if code not in (None, "OTHER"):
        return code
    for prefix, name in (("CODER_CAPACITY:", "CODER_CAPACITY"), ("STALLED:", "STALLED"),
                         (worker.OWNER_CHANGED_BLOCK, "OWNER_CHANGED"),
                         ("Result recovery needs operator review", "RESULT_RECOVERY"), ("superseded:", "SUPERSEDED")):
        if error.startswith(prefix):
            return name
    return "FOUNDER_NOTE"   # free text written through the status route (e.g. a pilot deferral)


@dataclass(frozen=True)
class System:
    paused: bool | str = UNKNOWN              # control.agents_paused
    code_write_flag: bool | str = UNKNOWN     # the worker's ENABLE_AUTONOMOUS_CODE_WRITE (its heartbeat)
    auto_resume_flag: bool | str = UNKNOWN    # the worker's ENABLE_BLOCKED_AUTO_RESUME (its heartbeat)
    external_release_flag: bool | str = UNKNOWN
    worker_status: str = UNKNOWN


@dataclass(frozen=True)
class Facts:
    key: str
    status: str
    age_h: float | None = None
    in_status_h: float | None = None
    in_status_basis: str = UNKNOWN            # "audit" | "created_at" | UNKNOWN
    retry_count: int = 0
    max_attempts: int | str = UNKNOWN
    approval_required: bool = False
    risk: str = "LOW"
    agent: str | None | object = UNKNOWN
    autonomous: bool | None = None
    write: bool | None = None
    handoff: bool = False
    error_class: str | None = None
    error_known: bool = True                  # offline: the log truncates errors but always shows them
    result_present: bool | str = UNKNOWN
    last_run: str | None = UNKNOWN            # newest AgentRun status value
    open_run: bool | str = UNKNOWN
    stage: str | None = UNKNOWN               # Stage C improvement stage
    admission: str | None = UNKNOWN           # worker._admission_gate reason as if READY; None = admitted
    ready_refusal: str | None = UNKNOWN
    done_refusal: str | None = UNKNOWN
    pending_approval: str | None = UNKNOWN    # APR key of a REQUESTED approval
    held_before_publish: bool | str = UNKNOWN
    merge_recoverable: bool | str = UNKNOWN
    resolution_evidence: dict | None | str = UNKNOWN
    waits_on: str | None = UNKNOWN            # "handoff" | "external_event" | "dependencies" | None
    dependencies_open: tuple = ()
    stale_claim: bool | str = UNKNOWN
    notes: tuple = field(default_factory=tuple)


def _known(v) -> bool:
    return v is not UNKNOWN and v != UNKNOWN


def _admissible(f: Facts):
    if not _known(f.admission):
        return None
    return f.admission is None


def _ready_pre(f: Facts) -> str:
    """The precondition a founder READY must meet: worker.ready_refusal (the status route refuses otherwise)."""
    if not _known(f.ready_refusal):
        return "the status route's ready_refusal passes (not evaluated offline)"
    return "ready_refusal passes now" if f.ready_refusal is None else f"ready_refusal clears: {f.ready_refusal}"


def _system_pre(s: System, f: Facts) -> list[str]:
    """What must be on for the worker to claim admitted READY work."""
    pre = []
    if s.paused is not False:
        pre.append("founder unpauses agents (agents_paused)" if s.paused is True else "agents are not paused")
    if f.write and s.code_write_flag is not True:
        pre.append("worker runs with ENABLE_AUTONOMOUS_CODE_WRITE=true and a valid Stage C authorization")
    if f.admission in RUNTIME_GATES:
        pre.append(f"runtime gate clears: {f.admission}")
    return pre


def _stale_or(f: Facts, otherwise: dict) -> dict:
    """An old task that the worker would never admit is an archive candidate rather than a live decision."""
    if (otherwise["bucket"] == B_DECISION and _admissible(f) is False and f.admission not in RUNTIME_GATES
            and f.age_h is not None and f.age_h >= ARCHIVE_AFTER_D * 24):
        return {**otherwise, "recommended": ARCHIVE,
                "operation": "cancel via Desk if no longer wanted (it has waited "
                              f"{int(f.age_h // 24)}d and the worker would never admit it); otherwise: "
                              + otherwise["operation"]}
    return otherwise


def _r(recommended, actor, operation, precondition, bucket, blocker=None, evidence=()):
    return {"recommended": recommended, "actor": actor, "operation": operation, "precondition": precondition,
            "bucket": bucket, "blocker": blocker, "evidence": list(evidence)}


def _not_admitted(f: Facts) -> list[str]:
    ev = []
    if _known(f.admission) and f.admission is not None:
        ev.append(f"admission: {f.admission}")
    return ev


def _decide(f: Facts, s: System) -> dict:
    st = f.status
    ev = _not_admitted(f)
    if st == "IN_PROGRESS":
        if f.open_run is True:
            return _r(KEEP_WAITING, "system", "worker finishes the run; recover_stale recovers it if its worker died",
                      "run ends, or stops beating past the stale window", B_SYSTEM, None, ["agent run STARTED"])
        if f.stale_claim is True:
            return _r(STALE_CLAIM, "system", "recover_stale requeues the claim to READY (no retry spent)",
                      "next worker tick", B_SYSTEM, "CLAIM_WITHOUT_RUN", ["IN_PROGRESS with no open run past STALE_AFTER"])
        return _r(KEEP_WAITING, "system", "worker starts the claimed run", "next worker tick", B_SYSTEM)

    if st == "READY":
        if _admissible(f) is True or f.admission in RUNTIME_GATES:
            pre = _system_pre(s, f)
            return _r(AUTO_PROGRESS, "system", "worker claims and runs it",
                      "; ".join(pre) if pre else "next worker tick (budgets and backoff permitting)", B_SYSTEM,
                      "AGENTS_PAUSED" if s.paused is True else (f.admission if f.admission in RUNTIME_GATES else None),
                      ev + (["agents_paused is set"] if s.paused is True else []))
        if f.admission == "dependencies_incomplete":
            return _r(KEEP_WAITING, "founder", "close the dependencies; the worker then claims it",
                      "every dependency DONE", B_DECISION, "DEPENDENCIES_INCOMPLETE",
                      ev + [f"open dependency {k}" for k in f.dependencies_open])
        if _admissible(f) is None:
            pre = _system_pre(s, f)
            return _r(FOUNDER_DECISION, "founder",
                      "check its admission on GET /desk/reconciliation (project, owner, criteria, registry); if "
                      "admitted the worker claims it, otherwise fix its scope or cancel",
                      "; ".join(["admission confirmed"] + pre), B_DECISION, "ADMISSION_UNVERIFIED",
                      ev + ["opted in (autonomous, LOW, no approval); remaining admission checks not in this source"])
        return _stale_or(f, _r(FOUNDER_DECISION, "founder",
                               "READY but never claimable: opt it in (autonomous, LOW risk, no approval) or work it "
                               "manually, or cancel", "founder decision", B_DECISION, "NOT_ADMISSIBLE", ev))

    if st == "BACKLOG":
        if f.waits_on == "dependencies":
            return _r(KEEP_WAITING, "system", "release_dependency_waits moves it to READY",
                      "every dependency DONE and agents not paused", B_SYSTEM, "DEPENDENCIES_INCOMPLETE",
                      [f"open dependency {k}" for k in f.dependencies_open])
        if _admissible(f) is True or f.admission in RUNTIME_GATES:
            return _r(FOUNDER_READY, "founder", "set READY via Desk if it is still wanted (the worker then runs it)",
                      _ready_pre(f), B_CLICK, "NOT_PROMOTED", ["BACKLOG is never claimed"])
        return _stale_or(f, _r(FOUNDER_DECISION, "founder",
                               "decide whether it is still wanted; it is not admissible for autonomous work as written",
                               "founder decision", B_DECISION, "NOT_ADMISSIBLE", ev or ["admission not evaluated"]))

    if st == "WAITING_EXTERNAL":
        if f.handoff:
            return _r(KEEP_WAITING, "system", "Stage C reports through the handoff; the Desk shows it",
                      "Stage C delivery/report", B_SYSTEM, "HANDED_OFF", ["released to Stage C"])
        if f.waits_on == "external_event" and _admissible(f) is True:
            pre = "matching external event arrives; ENABLE_AUTOMATIC_EXTERNAL_RELEASE=true; agents not paused"
            return _r(KEEP_WAITING, "system", "release_external_waits moves it to READY", pre, B_SYSTEM,
                      "WAITING_EXTERNAL_EVENT", ["structured external_wait"])
        if f.waits_on == "dependencies":
            return _r(KEEP_WAITING, "system", "release_dependency_waits moves it to READY",
                      "every dependency DONE", B_SYSTEM, "DEPENDENCIES_INCOMPLETE",
                      [f"open dependency {k}" for k in f.dependencies_open])
        return _stale_or(f, _r(FOUNDER_DECISION, "founder",
                               "nothing releases it automatically: supply the awaited input and set READY, or cancel",
                               _ready_pre(f), B_DECISION, "NO_AUTOMATIC_RELEASE",
                               ev + ["no structured external_wait/handoff the system can act on"
                                     if _known(f.waits_on) else "wait condition not evaluated offline"]))

    if st == "WAITING_APPROVAL":
        if _known(f.pending_approval) and f.pending_approval:
            return _r(FOUNDER_APPROVAL, "founder", f"decide approval {f.pending_approval} (approve or reject)",
                      "founder decision on the approval", B_CLICK, "APPROVAL_PENDING", [f"{f.pending_approval} REQUESTED"])
        if f.held_before_publish is True:
            return _r(FOUNDER_DECISION, "founder", "decide publication of the reviewed Stage C change",
                      "publication flag and founder GO", B_DECISION, "HELD_BEFORE_PUBLISH",
                      ["Stage C review approved; nothing pushed"])
        if not _known(f.pending_approval):
            return _r(FOUNDER_APPROVAL, "founder", "open the task's approval on the Desk and decide it",
                      "founder decision; approval row not visible to this source", B_CLICK, "APPROVAL_PENDING",
                      ["approval_required" if f.approval_required else "WAITING_APPROVAL"])
        return _r(FOUNDER_DECISION, "founder", "no approval is pending: set READY or cancel", _ready_pre(f),
                  B_DECISION, "NO_PENDING_APPROVAL", ["WAITING_APPROVAL without a REQUESTED approval"])

    if st == "REVIEW":
        if f.stage in ("PR_OPEN", "CI"):
            return _r(KEEP_WAITING, "system", "stage-c reconcile tracks the PR's CI", "CI result on GitHub", B_SYSTEM,
                      "CI_PENDING", [f"stage {f.stage}"])
        if f.stage == "READY_TO_MERGE":
            return _r(FOUNDER_REVIEW, "founder", "merge the PR on GitHub; stage-c-ci records the merge", "CI green",
                      B_CLICK, "AWAITING_FOUNDER_MERGE", ["stage READY_TO_MERGE"])
        if _known(f.done_refusal) and f.done_refusal is not None:
            return _r(FOUNDER_DECISION, "founder", "DONE is refused for this Stage C task; decide on it or cancel",
                      f"done_refusal clears: {f.done_refusal}", B_DECISION, "DONE_REFUSED", [f.done_refusal])
        return _r(FOUNDER_REVIEW, "founder", "accept the output (DONE with evidence) or send it back (READY)",
                  "founder reads the result", B_CLICK, "AWAITING_FOUNDER_REVIEW",
                  ["result present" if f.result_present is True else "output in REVIEW"])

    if st == "FAILED":
        cls = f.error_class or "UNSPECIFIED"
        if _known(f.max_attempts) and f.retry_count >= f.max_attempts:
            return _r(RETRY_EXHAUSTED, "founder",
                      "cancel, or create a new task with a changed approach (READY would never be claimed: "
                      "attempts_exhausted)", "founder decision", B_DECISION, cls,
                      [f"retry {f.retry_count}/{f.max_attempts}", f"last outcome {cls}"])
        if _known(f.max_attempts):
            return _r(FOUNDER_DECISION, "founder", "re-queue with READY (spends a retry) or cancel", _ready_pre(f),
                      B_DECISION, cls, [f"retry {f.retry_count}/{f.max_attempts}", f"last outcome {cls}"])
        return _r(FOUNDER_DECISION, "founder",
                  "cancel, or re-queue with READY if retries remain (max_attempts not in this source; FAILED is "
                  "normally set only when retries are exhausted)", _ready_pre(f), B_DECISION, cls,
                  [f"retry {f.retry_count}/?", f"last outcome {cls}"])

    if st == "BLOCKED":
        return _blocked(f, s)
    return _r(KEEP_WAITING, "none", "not an open status", "-", B_SYSTEM)


def _blocked(f: Facts, s: System) -> dict:
    cls = f.error_class
    ev = _not_admitted(f)
    if cls in AUTO_RESUME_CLASSES:
        if isinstance(f.resolution_evidence, dict):
            proof = (f"proof run {f.resolution_evidence.get('proof_run_id')} "
                     f"({f.resolution_evidence.get('proof_provider')}) after blocking run "
                     f"{f.resolution_evidence.get('blocking_run_id')}")
            on = s.auto_resume_flag is True
            return _r(AUTO_RESUMABLE, "system",
                      "resume_resolved_blocks sets READY (audited task.blocked.auto_resumed)",
                      ("next tick" if on else "founder GO to set ENABLE_BLOCKED_AUTO_RESUME=true on the worker")
                      + "; ready_refusal and admission pass under the row lock; retries left; one resume a day",
                      B_SYSTEM, cls, [proof])
        return _r(KEEP_WAITING, "system",
                  "resume once another task's run proves the provider/launcher works again; or founder sets READY "
                  "after checking it", "resolution evidence appears (worker.resolution_evidence)", B_SYSTEM, cls,
                  ["no proof run yet" if _known(f.resolution_evidence) else "resolution evidence not evaluated"])
    if cls == "MERGE_NOT_RECORDED":
        if f.merge_recoverable is True:
            return _r(AUTO_PROGRESS, "system", "stage-c reconcile re-checks the recorded merge on GitHub",
                      "GitHub confirms the merge facts", B_SYSTEM, cls, ["exact system merge block"])
        why = ["READY refused: a published pull request or its recovery owns this task"]
        if _known(f.done_refusal) and f.done_refusal:
            why.append(f"DONE refused: {f.done_refusal}")
        elif not _known(f.done_refusal):
            why.append("DONE refused unless a verified founder merge is recorded (stage_c_worker.done_refusal)")
        why.append("merge recovery covers only PR_OPEN/CI/READY_TO_MERGE" if f.stage not in (UNKNOWN,)
                   else "merge recovery not evaluated offline")
        return _r(CANCEL_ONLY, "founder",
                  "cancel it with a reason (the change merged outside the Stage C record); a follow-up needs a new task",
                  "founder decision", B_STUCK, cls, why)
    if cls == "REFRESH_CONTEXT_NOT_REVIEWED":
        return _r(FOUNDER_REVIEW, "founder", "review the refreshed head against main, then accept DONE or cancel",
                  "founder review", B_CLICK, cls, ["merged refresh awaiting explicit acceptance"])
    if cls in INSPECT_THEN_READY:
        return _r(FOUNDER_DECISION, "founder", f"{INSPECT_THEN_READY[cls]}, then set READY via Desk", _ready_pre(f),
                  B_DECISION, cls, [f"error class {cls}"] + ev)
    if cls in FOUNDER_JUDGEMENT:
        return _r(FOUNDER_DECISION, "founder", f"{FOUNDER_JUDGEMENT[cls]}; then set READY or cancel", _ready_pre(f),
                  B_DECISION, cls, [f"error class {cls}"] + ev)
    if cls is None:
        if f.last_run == enums.AgentRunStatus.ESCALATED.value or (f.result_present is True and f.last_run is not None):
            return _r(FOUNDER_REVIEW, "founder",
                      "read the escalation in the task result, then set READY with guidance or cancel",
                      _ready_pre(f), B_DECISION, "ESCALATED",
                      ["no error text; newest run ESCALATED" if f.last_run == "ESCALATED" else "no error text; result present"])
        if not _known(f.last_run):
            return _r(FOUNDER_REVIEW, "founder",
                      "read the task result/last run (likely a supervisor ESCALATE, which blocks without error text), "
                      "then set READY with guidance or cancel", _ready_pre(f), B_DECISION, "NO_ERROR_TEXT",
                      ["no error text (hypothesis: ESCALATED; run not visible offline)"] + ev)
        return _stale_or(f, _r(FOUNDER_DECISION, "founder", "BLOCKED without a recorded reason: decide READY or cancel",
                               _ready_pre(f), B_DECISION, "NO_ERROR_TEXT", ["no error text, no escalation"] + ev))
    # FOUNDER_NOTE, SUPERSEDED, anything else: a human wrote why.
    return _stale_or(f, _r(FOUNDER_DECISION, "founder",
                           "the block is a founder note: re-scope the task (or create a new one) or cancel",
                           _ready_pre(f), B_DECISION, cls, [f"error class {cls}"] + ev))


def classify(f: Facts, s: System) -> dict:
    """Pure: one open task's reconciliation row."""
    d = _decide(f, s)
    return {"key": f.key, "status": f.status,
            "in_status_h": round(f.in_status_h, 1) if f.in_status_h is not None else None,
            "in_status_basis": f.in_status_basis, "age_h": round(f.age_h, 1) if f.age_h is not None else None,
            "blocker_class": d["blocker"], "blocker_evidence": d["evidence"],
            "admissible": _admissible(f), "admission_reason": f.admission if _known(f.admission) else UNKNOWN,
            "recommended": d["recommended"], "actor": d["actor"], "operation": d["operation"],
            "precondition": d["precondition"], "bucket": d["bucket"],
            "retry": f"{f.retry_count}/{f.max_attempts if _known(f.max_attempts) else '?'}",
            "notes": list(f.notes)}


def classify_lock(key: str, value: dict | None, current_repository: str, now: datetime,
                  holder_running: bool | str = UNKNOWN) -> dict:
    """A repository lease row (stage_c.lease_key). Pure."""
    repository = key.removeprefix("lock:repo:")
    value = value if isinstance(value, dict) else {}
    holder, expires = value.get("holder"), value.get("expires_at")
    try:
        live = expires is not None and datetime.fromisoformat(str(expires)) > now
    except ValueError:
        live = UNKNOWN
    if repository != current_repository:
        return {"key": key, "recommended": STALE_LOCK, "actor": "founder",
                "operation": "optional cleanup: delete the row (a production DB write; needs its own founder GO)",
                "precondition": "none; no current code reads it",
                "evidence": [f"repository {repository} is not the Stage C repository {current_repository}"],
                "blocks": "nothing"}
    if not holder or live is False:
        return {"key": key, "recommended": KEEP_WAITING, "actor": "system", "operation": "none (free or expired)",
                "precondition": "-", "evidence": ["no live holder"], "blocks": "nothing"}
    if holder_running is False:
        return {"key": key, "recommended": STALE_LOCK, "actor": "system",
                "operation": "expires by itself at its expires_at; the next acquire takes it",
                "precondition": f"expires_at {expires}", "evidence": [f"holder {holder} has no open run"],
                "blocks": "Stage C attempts until it expires"}
    return {"key": key, "recommended": KEEP_WAITING, "actor": "system", "operation": "held by a running attempt",
            "precondition": "-", "evidence": [f"holder {holder}"], "blocks": "other Stage C attempts (by design)"}


def summarize(rows: list[dict]) -> dict:
    out = {b: [] for b in BUCKETS}
    for r in rows:
        out[r["bucket"]].append(r["key"])
    return {b: {"count": len(keys), "tasks": keys} for b, keys in out.items()}


# ----------------------------------------------------------------------------- database adapter (reads only)

def _aware(ts):
    return ts.replace(tzinfo=timezone.utc) if ts is not None and ts.tzinfo is None else ts


def system_facts(db: Session) -> System:
    from app import control, worker
    st = worker.worker_state(db)
    autonomy = st.get("autonomy") or {}
    return System(paused=control.agents_paused(db),
                  code_write_flag=autonomy.get("code_write_flag") is True,
                  auto_resume_flag=autonomy.get("blocked_auto_resume") is True,
                  external_release_flag=autonomy.get("external_release") is True,
                  worker_status=st.get("status") or UNKNOWN)


def facts_for(db: Session, task: models.Task, specs: dict, system: System, now: datetime) -> Facts:
    from app import stage_c_ci, stage_c_handoff, stage_c_worker, watchdog, worker
    ctx = task.context if isinstance(task.context, dict) else {}
    status = task.status
    entered = _aware(watchdog.status_entered_at(db, task))
    basis = "created_at" if entered == _aware(task.created_at) else "audit"
    runs = db.execute(select(models.AgentRun.status).where(models.AgentRun.task_id == task.id)
                      .order_by(models.AgentRun.id.desc()).limit(1)).scalars().all()
    open_run = db.scalar(select(models.AgentRun.id).where(
        models.AgentRun.task_id == task.id, models.AgentRun.status == enums.AgentRunStatus.STARTED).limit(1)) is not None
    imp = ctx.get("improvement")
    stage = imp.get("stage") if isinstance(imp, dict) else None
    agent = db.get(models.Agent, task.assigned_agent_id) if task.assigned_agent_id else None
    admission, _ = worker._admission_gate(db, task, specs, allowed_statuses=(status,),
                                          code_write_flag=system.code_write_flag is True)
    deps = db.execute(select(models.Task.key, models.Task.status)
                      .join(models.TaskDependency, models.Task.id == models.TaskDependency.depends_on_id)
                      .where(models.TaskDependency.task_id == task.id).order_by(models.Task.key)).all()
    open_deps = tuple(k for k, st in deps if st != enums.TaskStatus.DONE)
    if stage_c_handoff.released(task):
        waits_on = "handoff"
    elif ctx.get("release_on_dependencies") is True and deps:
        waits_on = "dependencies"
    elif worker._external_wait(ctx) is not None:
        waits_on = "external_event"
    else:
        waits_on = None
    approval = db.scalar(select(models.Approval.key).where(
        models.Approval.task_id == task.id, models.Approval.status == enums.ApprovalStatus.REQUESTED).limit(1))
    error_class = _error_class(task.error or "")
    blocked_or_review = status in (enums.TaskStatus.BLOCKED, enums.TaskStatus.REVIEW)
    resolution = (worker.resolution_evidence(db, task)
                  if status == enums.TaskStatus.BLOCKED and error_class in AUTO_RESUME_CLASSES else None)
    stale_claim = (status == enums.TaskStatus.IN_PROGRESS and not open_run
                   and _aware(task.updated_at) < now - worker.STALE_AFTER)
    return Facts(
        key=task.key, status=status.value, age_h=(now - _aware(task.created_at)).total_seconds() / 3600,
        in_status_h=(now - entered).total_seconds() / 3600, in_status_basis=basis,
        retry_count=task.retry_count, max_attempts=task.max_attempts, approval_required=bool(task.approval_required),
        risk=task.risk_level.value, agent=agent.key if agent else None,
        autonomous=ctx.get("autonomous") if isinstance(ctx.get("autonomous"), bool) else None,
        write=ctx.get("write") if isinstance(ctx.get("write"), bool) else None,
        handoff=stage_c_handoff.released(task), error_class=error_class, result_present=bool(task.result),
        last_run=runs[0].value if runs else None, open_run=open_run, stage=stage,
        admission=admission,
        ready_refusal=(worker.ready_refusal(db, task)
                       if status not in (enums.TaskStatus.READY, enums.TaskStatus.IN_PROGRESS) else None),
        done_refusal=stage_c_worker.done_refusal(db, task) if blocked_or_review else None,
        pending_approval=approval,
        held_before_publish=isinstance((task.artifacts or {}).get("held"), dict),
        merge_recoverable=(stage_c_ci.merge_recovery_block(db, task) is not None
                           if status == enums.TaskStatus.BLOCKED else False),
        resolution_evidence=resolution, waits_on=waits_on, dependencies_open=open_deps, stale_claim=stale_claim)


def build(db: Session, specs: dict | None = None, now: datetime | None = None) -> dict:
    """The reconciliation of every open task, the repository leases and the switches that gate them. Read-only:
    nothing is added, changed or flushed, and the session is rolled back before returning."""
    from app import registry, stage_c_worker
    now = now or datetime.now(timezone.utc)
    specs = registry.load() if specs is None else specs
    def refuse(*_args, **_kw):   # a gate that flushed or committed would be a bug here: stop before the database
        raise RuntimeError("reconciliation must not change the session")
    event.listen(db, "before_flush", refuse)
    event.listen(db, "before_commit", refuse)
    try:
        system = system_facts(db)
        tasks = db.scalars(select(models.Task).where(models.Task.status.in_([enums.TaskStatus(s) for s in OPEN]))
                           .order_by(models.Task.id)).all()
        rows = [classify(facts_for(db, t, specs, system, now), system) for t in tasks]
        running = {f"stage-c-runner:{k}" for k in db.scalars(
            select(models.Task.key).join(models.AgentRun, models.AgentRun.task_id == models.Task.id)
            .where(models.AgentRun.status == enums.AgentRunStatus.STARTED))}
        locks = [classify_lock(row.key, row.value, stage_c_worker.REPOSITORY, now,
                               holder_running=(row.value or {}).get("holder") in running)
                 for row in db.scalars(select(models.ControlState).where(models.ControlState.key.like("lock:repo:%"))
                                       .order_by(models.ControlState.key))]
        if db.new or db.dirty or db.deleted:
            refuse()
    finally:
        event.remove(db, "before_flush", refuse)
        event.remove(db, "before_commit", refuse)
        db.rollback()
    return {"version": VERSION, "generated_at": now.isoformat(), "read_only": True,
            "system": {"agents_paused": system.paused, "code_write_flag": system.code_write_flag,
                       "blocked_auto_resume_flag": system.auto_resume_flag,
                       "external_release_flag": system.external_release_flag, "worker": system.worker_status},
            "summary": summarize(rows), "tasks": rows, "locks": locks}
