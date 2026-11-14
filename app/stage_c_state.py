"""Stage C read model for the Founder Desk (GET /stage-c/state). Read-only, zero model calls, no GitHub.

The one place that turns Stage C rows into lifecycle semantics, so the Desk never invents them. Every string it
returns is a fixed code or a validated identifier (40-hex SHA, 64-hex fingerprint, TASK key, SAFE_FILES path,
ISO timestamp re-emitted from a parsed datetime). Free text (task context, diffs, findings, model output, errors,
emails, tokens) never leaves this module. Missing evidence is null, never false, 0 or "passed".

What the API cannot see is reported as NOT_VERIFIED: the host facts behind the preflight (launcher, images, egress,
coder logins) are proven on the host and reach the database only as the watchdog's fresh readiness row; the
worker's control plane (jobs directory, mirror, GitHub token) only through its heartbeat (autonomy.stage == "C").
PR and CI state are the last recorded evidence, not a live GitHub read.
"""
import os
import re
from datetime import datetime, timezone

from sqlalchemy import select

from app import approval_identity, control, enums, models, stage_c, stage_c_ci, stage_c_worker, worker

VERSION = 1
MAX_TASKS = 20
# shortcut: metrics transfer histories for tasks behind at most 5000 runs; use SQL aggregates if this scan is routine.
MAX_RUNS = 5000
USD_NOTE = "excludes_subscription_cost"

STATES = ("ACTIVE", "READY", "BLOCKED", "FAILED", "EXPIRED", "UNKNOWN", "NOT_VERIFIED")
WORKER_STATUSES = {"HEALTHY", "STALE", "PAUSED", "DEGRADED", "NO_DATA"}
OUTCOMES = {"LEASE_BUSY", "CODER_FAILED", "REJECTED", "CHECKS_FAILED", "CHECKS_REPEATED", "LEASE_LOST",
            "READY_FOR_REVIEW", "REVIEW_FAILED", "NOT_INDEPENDENT", "TAMPERED", "APPROVED", "PR_OPEN", "PR_FAILED",
            "PUSH_FAILED", "DRIFT", "RUNTIME_ERROR", "LAUNCHER_REFUSED", "OWNER_CHANGED", "PUBLISHED_PR_REFUSED",
            "WAITING_FOR_INDEPENDENT_PROVIDER", "WAITING_BEFORE_PUBLISH", "FAILED",
            "PRIVACY_GATE_BLOCKED", "PRIVACY_GATE_ERROR"}
# task.error prefix -> fixed failure code (order matters: first match wins).
ERROR_PREFIXES = (
    (stage_c_worker.REPAIR_INTERRUPTED, "REPAIR_INTERRUPTED"), (stage_c_worker.JOB_UNFINISHED, "JOB_UNFINISHED"),
    (stage_c_ci.MERGE_NOT_RECORDED, "MERGE_NOT_RECORDED"),
    (stage_c_ci.REFRESH_CONTEXT_NOT_REVIEWED, "REFRESH_CONTEXT_NOT_REVIEWED"), (stage_c_worker.PR_CLOSED, "PR_CLOSED"),
    ("CHECKS_REPEATED", "CHECKS_REPEATED"), ("LAUNCHER_REFUSED", "LAUNCHER_REFUSED"),
    ("upstream_role_not_read_only", "UPSTREAM_ROLE_NOT_READ_ONLY"),
    ("upstream_role_unverified", "UPSTREAM_ROLE_UNVERIFIED"), ("PR_FAILED", "PR_FAILED"),
    ("CI failed after", "CI_REPAIRS_EXHAUSTED"), ("PR head moved", "PR_HEAD_MOVED"),
    ("Merge eligibility refused", "MERGE_ELIGIBILITY_REFUSED"), ("workflow approval required", "CI_APPROVAL_REQUIRED"),
    ("CI ended without a result", "CI_ENDED_WITHOUT_RESULT"), ("worker lost the Stage C run", "WORKER_INTERRUPTED"),
)
CI_STATES = {"passed": "SUCCESS", "failed": "FAILURE", "running": "PENDING", "observed after merge": "MERGED_OBSERVED"}
MAX_HISTORY = 12
REACHED = ("READY_TO_MERGE", "MERGED", "VERIFYING", "MEASURED")
STOPPED = (enums.TaskStatus.BLOCKED, enums.TaskStatus.FAILED)
CLOSED = (enums.TaskStatus.DONE, enums.TaskStatus.CANCELLED)
NEEDS_FOUNDER = (enums.TaskStatus.BLOCKED, enums.TaskStatus.FAILED, enums.TaskStatus.WAITING_APPROVAL)
GRANT_STATUS = {None: "ACTIVE", "stage_c_authorization_consumed": "CONSUMED", "stage_c_authorization_expired": "EXPIRED",
                "stage_c_standing_expired": "EXPIRED", "stage_c_standing_daily_limit": "DAILY_LIMIT"}

_SHA = re.compile(r"[a-f0-9]{40}")
_FP = re.compile(r"[a-f0-9]{64}")
_TASK_KEY = re.compile(r"[A-Z]+-[0-9]{1,6}")
PREFLIGHT_CHECKS = {
    "source_sha", "api_worker_sha_agree", "acceptance_recorded", "acceptance_source_sha", "launcher",
    "coder_image", "checks_image", "checks_command", "egress_proxy", "coder_logins", "codex_reviewer_volume",
    "claude_auth", "jobs_directory", "repository_mirror", "github_credential", "independent_providers",
    "guardian_policy", "allowed_repository", "allowed_branch", "project_profiles", "db_project_repository",
    "worker_identity", "github_commit_statuses", "github_actions",
    *(f"acceptance:{name}" for name in stage_c_worker.ACCEPTANCE_CHECKS),
}


def _sha(v):
    return v if isinstance(v, str) and _SHA.fullmatch(v) else None


def _fp(v):
    return v if isinstance(v, str) and _FP.fullmatch(v) else None


def _task_key(v):
    return v if isinstance(v, str) and len(v) <= 32 and _TASK_KEY.fullmatch(v) else None


def _int(v):
    return v if type(v) is int else None


def _ts(v):
    if isinstance(v, datetime):
        at = v
    elif isinstance(v, str):
        at = control._utc(v)
        if at is None:
            return None
    else:
        return None
    try:
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _role(issuer):
    if approval_identity.is_founder_id(issuer):
        return "founder"
    return "operator" if isinstance(issuer, str) and issuer.startswith("operator:") else "unknown"


def _revoked(db, key: str, issue_action: str, revoke_action: str) -> dict | None:
    """The revoked grant's last value, when the newest issue/revoke audit row for `key` is a revoke (revoke deletes
    the control_state row, so the audit trail is the only record of it)."""
    last = db.scalars(select(models.AuditLog).where(
        models.AuditLog.entity_key == key, models.AuditLog.action.in_((issue_action, revoke_action)))
        .order_by(models.AuditLog.id.desc()).limit(1)).first()
    if last is None or last.action != revoke_action:
        return None
    return last.before if isinstance(last.before, dict) else {}


# --------------------------------------------------------------------------- runtime
def _runtime(db, api_sha, now):
    try:
        view = worker.worker_state(db)
    except Exception:
        view = {}
    view = view if isinstance(view, dict) else {}
    rt = view.get("runtime") if isinstance(view.get("runtime"), dict) else {}
    autonomy = view.get("autonomy") if isinstance(view.get("autonomy"), dict) else {}
    worker_sha = _sha(rt.get("git_sha"))
    flag = autonomy.get("code_write_flag")
    flag = flag if isinstance(flag, bool) else None
    status = view.get("status") if isinstance(view.get("status"), str) and view["status"] in WORKER_STATUSES else None
    # The worker's own ready() (host half + control plane), as it last reported it; only meaningful with the flag on.
    worker_ready = (autonomy.get("stage") == "C") if flag is True and autonomy.get("stage") in ("B", "C") else None

    acc = db.get(models.ControlState, control.ACCEPTANCE)
    acc_value = acc.value if acc is not None and isinstance(acc.value, dict) else {}
    readiness = db.get(models.ControlState, stage_c_worker.RUNTIME_READINESS_KEY)
    r_value = readiness.value if readiness is not None and isinstance(readiness.value, dict) else {}
    if readiness is None:
        preflight = "NOT_VERIFIED"
    else:
        try:
            if stage_c_worker.runtime_ready(db, now):
                preflight = "PASS"
            elif r_value.get("ready") is False and readiness.updated_by == "watchdog":
                preflight = "FAIL"
            else:
                preflight = "STALE"  # too old, another SHA, or not written by the watchdog
        except Exception:
            preflight = "STALE"
    failed = r_value.get("failed")
    return {
        "api_sha": api_sha, "worker_sha": worker_sha,
        # Agreement across all three processes cannot be proven without the reasoner's SHA.
        "sha_agree": False if api_sha and worker_sha and api_sha != worker_sha else None,
        "worker_heartbeat": status, "write_enabled": flag, "worker_ready": worker_ready,
        "agents_paused": control.agents_paused(db),
        "acceptance": {"source_sha": _sha(acc_value.get("source_sha")), "accepted_at": _ts(acc_value.get("issued_at")),
                       "valid": control.acceptance_valid(db, api_sha or "") if acc is not None else None},
        "preflight": {"status": preflight, "checked_at": _ts(r_value.get("checked_at")),
                      "source_sha": _sha(r_value.get("source_sha")),
                      # The watchdog's failing check names (watchdog.record_stage_c_readiness): fixed codes only.
                      "failed": [f if isinstance(f, str) and f in PREFLIGHT_CHECKS else "other" for f in failed[:10]]
                      if isinstance(failed, list) else None},
        # No process records the reasoner's deployed source in the database: never guessed.
        "reasoner_sha": None,
        "host_facts": {"host_checks": "NOT_VERIFIED", "reasoner_sha": "NOT_VERIFIED",
                       "api_sha": "RECORDED" if api_sha else "NOT_VERIFIED",
                       "worker_sha": "RECORDED" if worker_sha else "NOT_VERIFIED"},
    }


# --------------------------------------------------------------------------- authorizations
def _one_task(db, api_sha, now):
    row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
    if row is None:
        before = _revoked(db, control.PILOT_AUTHORIZATION, "stage-c.authorization.issue", "stage-c.authorization.revoke")
        if before is None:
            return None
        value, status = before, "REVOKED"
    else:
        value = row.value if isinstance(row.value, dict) else {}
        key = value.get("task_key") if isinstance(value.get("task_key"), str) else ""
        denial = control._authorization_denial(db, row, key, api_sha or "", now)
        status = GRANT_STATUS.get(denial, "INVALID")
    key = value.get("task_key")
    return {"task_key": _task_key(key),
            "source_sha": _sha(value.get("source_sha")), "expires_at": _ts(value.get("expires_at")),
            "revoked": status == "REVOKED", "issuer": _role(value.get("issued_by")), "status": status}


def _standing(db, api_sha, now):
    row = db.get(models.ControlState, control.STANDING_AUTHORIZATION)
    if row is None:
        before = _revoked(db, control.STANDING_AUTHORIZATION, "stage-c.standing.issue", "stage-c.standing.revoke")
        if before is None:
            return None
        value, status = before, "REVOKED"
    else:
        value = row.value if isinstance(row.value, dict) else {}
        paths = value.get("paths") if isinstance(value.get("paths"), list) else []
        # Malformed paths must not reach the validator's set(wanted) conversion.
        denial = (control._standing_denial(db, row, paths, api_sha or "", now)
                  if all(isinstance(p, str) for p in paths) else "stage_c_standing_invalid")
        status = GRANT_STATUS.get(denial, "INVALID")
        if (status == "INVALID" and api_sha and _sha(value.get("source_sha"))
                and value["source_sha"] != api_sha and all(isinstance(p, str) for p in paths)
                and control._standing_denial(db, row, paths, value["source_sha"], now) is None):
            status = "SHA_MISMATCH"  # a deploy voids the grant; the founder issues a new one
    paths = value.get("paths") if isinstance(value.get("paths"), list) else []
    cap = value.get("max_per_day")
    cap = cap if type(cap) is int and 1 <= cap <= control.STANDING_MAX_PER_DAY else None
    used = value.get("used") if isinstance(value.get("used"), dict) else {}
    today = used.get("day") == now.date().isoformat()
    used_today = (used.get("count") if type(used.get("count")) is int and used["count"] >= 0 else None) if today else 0
    return {"source_sha": _sha(value.get("source_sha")),
            "paths": sorted({p for p in paths[:20] if isinstance(p, str) and p in stage_c_worker.SAFE_FILES}),
            "expires_at": _ts(value.get("expires_at")), "max_per_day": cap, "used_today": used_today,
            "remaining_today": 0 if status != "ACTIVE"
            else max(cap - used_today, 0) if cap is not None and used_today is not None else None,
            "revoked": status == "REVOKED", "issuer": _role(value.get("issued_by")), "status": status}


# --------------------------------------------------------------------------- tasks
def _history(task):
    ctx = task.context if isinstance(task.context, dict) else {}
    imp = ctx.get("improvement") if isinstance(ctx.get("improvement"), dict) else {}
    hist = imp.get("history") if isinstance(imp.get("history"), list) else []
    rows = [h for h in hist if isinstance(h, dict) and isinstance(h.get("stage"), str)]
    rows = [{**h, "evidence": h["evidence"] if isinstance(h.get("evidence"), dict) else {}} for h in rows]
    return imp, rows, len(rows) == len(hist)


def _failure(task):
    error = task.error if isinstance(task.error, str) else ""
    if not error:
        return None
    if error in OUTCOMES:
        return error
    return next((code for prefix, code in ERROR_PREFIXES if error.startswith(prefix)), "OTHER")


def _task(db, task, runs):
    imp, hist, intact = _history(task)
    stage = imp.get("stage") if imp.get("stage") in stage_c.STAGES else None
    tested = next((h["evidence"] for h in reversed(hist) if "checks_passed" in h["evidence"]), {})
    passed = tested.get("checks_passed")
    test_verdict = "PASSED" if passed is True else "FAILED" if passed is False else None
    tested_fp = next((_fp(h["evidence"].get("fingerprint")) for h in reversed(hist)
                      if h["stage"] == "INDEPENDENT_REVIEW"), None)

    approval = imp.get("approval") if isinstance(imp.get("approval"), dict) else None
    reviewed_fp = _fp(approval.get("fingerprint")) if approval else None
    opened_at = max((i for i, h in enumerate(hist) if h["stage"] == "PR_OPEN"), default=None)
    pr_ev = hist[opened_at]["evidence"] if opened_at is not None else {}
    pr_head = _sha(pr_ev.get("commit"))
    published_fp = _fp(pr_ev.get("fingerprint"))
    if approval is not None:
        fp_match = bool(reviewed_fp and reviewed_fp == tested_fp and (published_fp is None or published_fp == reviewed_fp))
        audit_id = approval.get("audit_id")
        event = db.get(models.AuditLog, audit_id) if type(audit_id) is int else None
        recorded = (fp_match and event is not None and isinstance(event.after, dict)
                    and stage_c_ci._approval_recorded(db, task, task.context))
        reviewer_verdict = "APPROVED" if fp_match and recorded else "UNVERIFIED"
    else:
        fp_match = None
        last_review = next((h["evidence"].get("review") for h in reversed(hist) if "review" in h["evidence"]), None)
        reviewer_verdict = "REJECTED" if last_review == "REJECTED" else None

    number = stage_c_worker.published_pr_number(task) if opened_at is not None else None
    ci_state = ci_sha = None
    if opened_at is not None:
        ci_ev = next((h["evidence"] for h in reversed(hist[opened_at + 1:]) if "ci_state" in h["evidence"]), None)
        if ci_ev is not None:
            ci_sha = _sha(ci_ev.get("commit"))
            ci_state = CI_STATES.get(ci_ev.get("ci_state")) if isinstance(ci_ev.get("ci_state"), str) else None
            if ci_state is not None and (ci_sha is None or ci_sha != pr_head):
                ci_state = "STALE"  # CI about other bytes than the published head is never success

    repairs = stage_c_ci.repairs_used(task) if intact and hist else None
    last = runs[0] if runs else None
    outcome = (last.tool_calls or {}).get("outcome") if last is not None and isinstance(last.tool_calls, dict) else None
    started = task.started_at or (runs[-1].started_at if runs else None)
    return {
        "key": _task_key(task.key),
        "status": task.status.value if task.status else None, "stage": stage,
        "attempts": len(runs), "retry_count": _int(task.retry_count), "repairs": repairs,
        "started_at": _ts(started), "updated_at": _ts(task.updated_at),
        "test_verdict": test_verdict, "reviewer_verdict": reviewer_verdict,
        "tested_fingerprint": tested_fp, "reviewed_fingerprint": reviewed_fp, "fingerprint_match": fp_match,
        "pr": {"number": number, "head_sha": pr_head}
        if opened_at is not None else None,
        "ci": {"state": ci_state, "sha": ci_sha} if opened_at is not None else None,
        "last_outcome": (outcome if isinstance(outcome, str) and outcome in OUTCOMES else "OTHER") if outcome else None,
        "failure_category": _failure(task),
        "needs_founder": (task.status in NEEDS_FOUNDER or stage == "READY_TO_MERGE") if task.status else None,
        "history": [{"stage": h["stage"], "at": _ts(h.get("at"))} for h in hist if h["stage"] in stage_c.STAGES][-MAX_HISTORY:],
    }


def _last_ready_pr(db):
    """The newest task at READY_TO_MERGE or MERGED among all Stage C tasks, not only the 20 listed."""
    task = db.execute(select(models.Task.key, models.Task.context).where(
        models.Task.context["improvement"]["stage"].as_string().in_(("READY_TO_MERGE", "MERGED")))
        .order_by(models.Task.updated_at.desc(), models.Task.id.desc()).limit(1)).first()
    if task is not None:
        imp, hist, _ = _history(task)
        opened = next((h["evidence"] for h in reversed(hist) if h["stage"] == "PR_OPEN"), {})
        key = _task_key(task.key)
        return {"task_key": key, "number": stage_c_worker.published_pr_number(task),
                "head_sha": _sha(opened.get("commit")), "stage": imp["stage"]}
    return None


# --------------------------------------------------------------------------- metrics
def _metrics(tasks, runs, truncated):
    stages = {t.id: t.stage for t in tasks}
    reached = {t.id for t in tasks if t.stage in REACHED or any(
        isinstance(h, dict) and h.get("stage") == "READY_TO_MERGE"
        for h in (t.history if isinstance(t.history, list) else []))}
    stopped = {t.id for t in tasks if t.status in STOPPED}
    done = reached | stopped
    tokens = [r.input_tokens + r.output_tokens for r in runs
              if type(r.input_tokens) is int and type(r.output_tokens) is int]
    usd = [float(r.estimated_cost_usd) for r in runs if r.estimated_cost_usd is not None]
    calls = [r.tool_calls.get("model_calls") for r in runs if isinstance(r.tool_calls, dict)]
    calls = [c for c in calls if type(c) is int]
    minutes = [(r.ended_at - r.started_at).total_seconds() / 60 for r in runs if r.ended_at and r.started_at]

    def quota(r):
        attempts = r.tool_calls.get("attempts") if isinstance(r.tool_calls, dict) else None
        return isinstance(attempts, list) and any(
            isinstance(a, dict) and a.get("health") == "QUOTA_EXHAUSTED" for a in attempts)
    return {
        "tasks": {"started": len(tasks), "reached_ready_to_merge": len(reached), "stopped": len(stopped),
                  "in_progress": sum(1 for t in tasks if t.id not in done and t.status not in CLOSED),
                  "abandoned": sum(1 for t in tasks if stages[t.id] == "ABANDONED" or t.status == enums.TaskStatus.CANCELLED)},
        "attempts": {"total": len(runs), "failed": sum(1 for r in runs if r.status == enums.AgentRunStatus.FAILED)},
        # The worker records one combined count per run (coder + reviewer); the split is not recorded, so not shown.
        "model_calls": {"total": sum(calls) if calls else None},
        "minutes": round(sum(minutes), 1) if minutes else None,
        "tokens": sum(tokens) if tokens else None,
        "usd_recorded": round(sum(usd), 4) if usd else None, "usd_note": USD_NOTE,
        "quota_stops": sum(1 for r in runs if quota(r)),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------- state
def _derive(runtime, one_task, standing, tasks) -> tuple[str, list[str]]:
    b = []
    if runtime["api_sha"] is None:
        b.append("api_sha_unknown")
    if runtime["worker_sha"] is None:
        b.append("worker_sha_unknown")
    if runtime["api_sha"] and runtime["worker_sha"] and runtime["api_sha"] != runtime["worker_sha"]:
        b.append("sha_mismatch")
    if runtime["worker_heartbeat"] != "HEALTHY":
        b.append("worker_not_healthy")
    if runtime["agents_paused"]:
        b.append("agents_paused")
    if runtime["write_enabled"] is not True:
        b.append("write_disabled")
    elif runtime["worker_ready"] is not True:
        b.append("worker_not_ready")
    if runtime["acceptance"]["valid"] is not True:
        b.append("host_not_accepted")
    if runtime["preflight"]["status"] != "PASS":
        b.append("preflight_" + runtime["preflight"]["status"].lower())
    grants = [g for g in (one_task, standing) if g]
    active = any(g["status"] == "ACTIVE" for g in grants)
    if not active:
        # Per grant, from its own validator (never write_authorization_denial, which reports the pilot's reason
        # whenever a pilot row exists): e.g. one_task_consumed + standing_daily_limit.
        b.extend([f"{name}_{g['status'].lower()}" for name, g in (("one_task", one_task), ("standing", standing)) if g]
                 or ["no_authorization"])
    latest = tasks[0] if tasks else None
    if any(t["needs_founder"] for t in tasks):
        b.append("task_needs_founder")

    if runtime["api_sha"] is None or runtime["worker_sha"] is None or runtime["worker_heartbeat"] in (None, "NO_DATA", "STALE"):
        return "UNKNOWN", b
    if {"sha_mismatch", "agents_paused", "write_disabled", "host_not_accepted", "worker_not_healthy", "preflight_fail"} & set(b):
        return "BLOCKED", b
    if any(t["status"] == "IN_PROGRESS" for t in tasks):
        return "ACTIVE", b
    if latest and latest["status"] == "FAILED":
        return "FAILED", b
    if latest and latest["status"] == "BLOCKED":
        return "BLOCKED", b
    if runtime["preflight"]["status"] != "PASS" or runtime["worker_ready"] is not True:
        return "NOT_VERIFIED", b
    if not active:
        statuses = {g["status"] for g in grants}
        expired = "EXPIRED" in statuses and "REVOKED" not in statuses
        return ("EXPIRED" if expired else "BLOCKED"), b
    return "READY", b


# blocker -> what unblocks it, first match wins; a fixed code, derived from the blockers only.
NEXT_ACTIONS = (("api_sha_unknown", "check_deploy"), ("worker_sha_unknown", "check_worker"),
                ("worker_not_healthy", "check_worker"), ("sha_mismatch", "redeploy_one_sha"),
                ("agents_paused", "resume_agents"), ("host_not_accepted", "accept_host"),
                ("write_disabled", "enable_code_write"), ("preflight_", "run_preflight"),
                ("worker_not_ready", "run_preflight"), ("task_needs_founder", "review_task"),
                ("standing_daily_limit", "wait_daily_reset"), ("no_authorization", "issue_authorization"),
                ("one_task_", "issue_authorization"), ("standing_", "issue_authorization"))


def _next_action(blockers):
    return next((action for prefix, action in NEXT_ACTIONS if any(b.startswith(prefix) for b in blockers)), "none")


def build(db, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    api_sha = _sha(os.environ.get("COMPANY_OS_GIT_SHA"))
    runtime = _runtime(db, api_sha, now)
    one_task, standing = _one_task(db, api_sha, now), _standing(db, api_sha, now)

    runs = list(db.scalars(select(models.AgentRun).where(models.AgentRun.tool_calls["stage_c"].as_boolean().is_(True))
                           .order_by(models.AgentRun.id.desc()).limit(MAX_RUNS + 1)))
    truncated = len(runs) > MAX_RUNS
    runs = runs[:MAX_RUNS]
    by_task: dict[int, list] = {}
    for r in runs:
        by_task.setdefault(r.task_id, []).append(r)
    all_tasks = list(db.execute(select(models.Task.id, models.Task.status,
                                models.Task.context["improvement"]["stage"].as_string().label("stage"),
                                models.Task.context["improvement"]["history"].label("history"))
                               .where(models.Task.id.in_(by_task))))
    listed_tasks = db.scalars(select(models.Task).where(models.Task.id.in_(by_task))
                             .order_by(models.Task.updated_at.desc(), models.Task.id.desc()).limit(MAX_TASKS))
    tasks = [_task(db, t, by_task[t.id]) for t in listed_tasks]
    state, blockers = _derive(runtime, one_task, standing, tasks)
    return {"version": VERSION, "generated_at": now.isoformat(), "state": state, "blockers": blockers,
            "next_action": _next_action(blockers), "last_ready_pr": _last_ready_pr(db), "runtime": runtime,
            "authorizations": {"one_task": one_task, "standing": standing},
            "tasks": tasks, "metrics": _metrics(all_tasks, runs, truncated),
            # What the Desk's code-task form may offer as write scope (POST /stage-c/tasks re-checks it).
            "code_task_paths": stage_c_worker.task_paths()}
