"""Should the Stage C worker carry ENABLE_AUTONOMOUS_CODE_WRITE right now? (docs/CODER_RUNTIME.md)

The founder's Start/Stop Pilot on the Desk is the existing one-task authorization (control.authorize_pilot /
revoke_pilot_authorization). The root host timer scripts/stage-c-arm asks decide() every tick and arms the worker
only while that authority exists, so the flag is task-bound, time-limited and closes by itself.

Read-only: plain reads through the app's own validators, never a row lock, lease acquire or consume. Any error is
a DISARM. Output contract of main(): exactly one line, `ARM <task key>` or `DISARM <reason>`.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app import control, enums, models, stage_c, stage_c_runner, stage_c_worker

# execute() commits the consume with its STARTED run, then run() takes the repository lease within seconds.
CONSUME_GAP = timedelta(minutes=10)


def _in_flight(db, value: dict, now: datetime) -> bool:
    """The authorized task's own Stage C run is still going after it consumed the authorization.

    Only the exact run that consumed this authorization counts (value["consumed_by_run_id"]): it must belong to the
    authorized task, be STARTED, and the task IN_PROGRESS. Then either the live repository lease is THAT run's lease
    (the runner records the run id it acquired the lease for; a lease of an earlier run, of another task or without a
    run id never counts), or the run started within CONSUME_GAP (execute() commits the consume and its STARTED run
    together, before run() takes the lease)."""
    task_key = value.get("task_key")
    task = db.scalar(select(models.Task).where(models.Task.key == task_key))
    run_id = value.get("consumed_by_run_id")
    run = db.get(models.AgentRun, run_id) if type(run_id) is int else None
    if not (task is not None and task.status == enums.TaskStatus.IN_PROGRESS and run is not None
            and run.task_id == task.id and run.status == enums.AgentRunStatus.STARTED):
        return False
    lease = db.get(models.ControlState, stage_c.lease_key(stage_c_worker.REPOSITORY))
    lv = lease.value if lease is not None and isinstance(lease.value, dict) else {}
    try:
        live = datetime.fromisoformat(str(lv.get("expires_at"))) > now
    except ValueError:
        live = False
    if (live and lv.get("holder") == f"{stage_c_runner.HOLDER}:{task_key}" and type(lv.get("run_id")) is int
            and lv["run_id"] == run.id):
        return True
    # Only that short gap: a STARTED run older than CONSUME_GAP without its own live lease is not live work (an
    # interrupted run the database never recovered), so it cannot keep the worker armed after the grant is spent.
    started = run.started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started is not None and timedelta(0) <= now - started <= CONSUME_GAP


def decide(db, sha: str, now: datetime | None = None) -> tuple[str, str]:
    """("ARM", task_key) or ("DISARM", reason). Fail closed: any exception is ("DISARM", "read_error")."""
    now = now or datetime.now(timezone.utc)
    try:
        if os.environ.get("COMPANY_OS_GIT_SHA", "") != sha:   # runtime_ready() binds to this process's SHA
            return "DISARM", "api_sha_mismatch"
        if not control.acceptance_valid(db, sha):
            return "DISARM", "not_accepted"
        if control.paused_or_unreadable(db):
            return "DISARM", "paused"
        if not stage_c_worker.runtime_ready(db, now):
            return "DISARM", "not_ready"
        row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
        value = row.value if row is not None and isinstance(row.value, dict) else {}
        task_key = value.get("task_key") if isinstance(value.get("task_key"), str) else ""
        denial = control.pilot_authorization_denial(db, task_key, sha, now)
        if denial is None:
            return "ARM", task_key
        # Consumed implies every other field validated (founder, audit, repository, SHA); expiry is not re-checked
        # because the claimed run is bounded by its own lease and run lifecycle.
        if denial == "stage_c_authorization_consumed" and _in_flight(db, value, now):
            return "ARM", task_key
        return "DISARM", denial
    except Exception:  # noqa: BLE001 - any read failure means "no code write"
        return "DISARM", "read_error"


def main(sha: str) -> str:
    try:
        from app.db import SessionLocal
        with SessionLocal() as db:
            # The host passes PGOPTIONS default_transaction_read_only=on; prove it applied rather than assume it.
            if db.execute(text("SHOW default_transaction_read_only")).scalar() != "on":
                return "DISARM not_read_only"
            action, detail = decide(db, sha)
            db.rollback()
    except Exception:  # noqa: BLE001
        return "DISARM read_error"
    return f"{action} {detail}"


if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else ""))
