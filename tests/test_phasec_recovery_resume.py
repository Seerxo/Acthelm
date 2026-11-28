"""Phase C gap 2: opt-in BLOCKED -> READY only for a blocker a later run proves gone, bounded and audited."""
from datetime import timedelta

import pytest

from app import control, enums, models, stage_c_worker, worker
from tests.test_stage_c_worker import FOUNDER_ID, fully_accepted, runtime_report, write_task
from tests.test_worker import SPECS, _task

SHA = "a" * 40
CAPACITY = "CODER_CAPACITY: the coder was unavailable 3 attempts in a row; check ..., then set READY"
LAUNCHER = "LAUNCHER_REFUSED: the host launcher refused 3 attempts in a row; check ..., then set READY"


@pytest.fixture
def armed(monkeypatch, tmp_path):
    """Everything the founder's own READY needs: flag on (and in the heartbeat), host accepted, preflight fresh."""
    db = fully_accepted(monkeypatch, tmp_path, SHA)
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", True)
    monkeypatch.setattr(worker, "ENABLE_BLOCKED_AUTO_RESUME", True)
    runtime_report(db, SHA)
    worker._heartbeat(db, autonomy={"code_write_flag": True})
    return db


def _code_task(db, key, **over):
    """write_task, sharing its one project between tasks."""
    first = db.query(models.Task).filter(models.Task.project_id.isnot(None)).first()
    if first is None:
        return write_task(db, key=key, **over)
    return _task(db, key=key, project_id=first.project_id, assigned_agent_id=first.assigned_agent_id,
                 context={"autonomous": True, "write": True, "stage_c": {"paths": ["tests/test_metrics.py"]}}, **over)


def _blocked(db, error, *, key="TASK-0100", provider="claude", ended_ago=timedelta(hours=2), authorize=True):
    task = _code_task(db, key, status=enums.TaskStatus.BLOCKED, error=error)
    db.add(models.AgentRun(task_id=task.id, agent_id=task.assigned_agent_id, provider=provider,
                           status=enums.AgentRunStatus.FAILED, started_at=worker._now() - ended_ago - timedelta(minutes=5),
                           ended_at=worker._now() - ended_ago,
                           tool_calls={"stage_c": True, "model_calls": 0, "outcome": "LAUNCHER_REFUSED",
                                       "capacity_failure": True}))
    db.commit()
    if authorize:
        control.authorize_pilot(db, task.key, SHA, founder=FOUNDER_ID)
    return task


def _later_coder_run(db, *, provider="claude", outcome="PR_OPEN", calls=1, ago=timedelta(minutes=30), **extra):
    other = db.query(models.Task).filter_by(key="TASK-0900").one_or_none() or _code_task(
        db, "TASK-0900", status=enums.TaskStatus.REVIEW)
    run = models.AgentRun(task_id=other.id, agent_id=other.assigned_agent_id, provider=provider,
                          status=enums.AgentRunStatus.SUCCEEDED, started_at=worker._now() - ago,
                          ended_at=worker._now() - ago + timedelta(minutes=10),
                          tool_calls={"stage_c": True, "model_calls": calls, "outcome": outcome, **extra})
    db.add(run)
    db.commit()
    return run


@pytest.mark.parametrize("error", [CAPACITY, LAUNCHER], ids=["cap", "launch"])
def test_resume_audited(armed, error):
    db = armed
    task = _blocked(db, error)
    proof = _later_coder_run(db)
    assert worker.resume_resolved_blocks(db, SPECS) == [task.key]
    db.refresh(task)
    assert task.status == enums.TaskStatus.READY and task.retry_count == 0
    row = db.query(models.AuditLog).filter_by(action=worker.AUTO_RESUMED).one()
    assert row.before["status"] == "BLOCKED" and row.before["error"] == error
    assert row.after["status"] == "READY" and row.after["evidence"]["proof_run_id"] == proof.id
    assert db.query(models.AgentRun).count() == 2                       # no run started, no model called
    # Blocked again the same day: no second automatic resume in the window.
    task.status = enums.TaskStatus.BLOCKED
    db.commit()
    _later_coder_run(db, ago=timedelta(minutes=1))
    assert worker.resume_resolved_blocks(db, SPECS) == []


def test_off_by_default(armed, monkeypatch):
    monkeypatch.setattr(worker, "ENABLE_BLOCKED_AUTO_RESUME", False)
    task = _blocked(armed, LAUNCHER)
    _later_coder_run(armed)
    assert worker.resume_resolved_blocks(armed, SPECS) == []
    armed.refresh(task)
    assert task.status == enums.TaskStatus.BLOCKED


@pytest.mark.parametrize("proof", [
    None,                                                   # nothing ran since the block
    {"ago": timedelta(hours=3)},                            # ran before the block ended
    {"calls": 0},                                           # the launcher never started a coder
    {"outcome": "LAUNCHER_REFUSED"},                        # checks launch refused
    {"outcome": "CODER_FAILED"},
    {"capacity_failure": True},
    {"provider": "codex"},                                  # capacity of a different provider proves nothing
])
def test_no_proof(armed, proof):
    task = _blocked(armed, CAPACITY)
    if proof is not None:
        _later_coder_run(armed, **proof)
    assert worker.resume_resolved_blocks(armed, SPECS) == []
    armed.refresh(task)
    assert task.status == enums.TaskStatus.BLOCKED


@pytest.mark.parametrize("error", [
    "STALLED: 6 attempts in a row made no progress", f"{stage_c_worker.JOB_UNFINISHED} inspect it",
    f"{stage_c_worker.REPAIR_INTERRUPTED} inspect it", "upstream_role_not_read_only: restore Read",
    "upstream_role_unverified: renew", "CHECKS_REPEATED: same tests", "RUNTIME_ERROR", worker.OWNER_CHANGED_BLOCK,
    None,
], ids=["stall", "job", "repair", "ro", "unver", "checks", "rt", "owner", "none"])
def test_no_resume_other(armed, error):
    task = _blocked(armed, error)
    _later_coder_run(armed)
    assert worker.resume_resolved_blocks(armed, SPECS) == []
    armed.refresh(task)
    assert task.status == enums.TaskStatus.BLOCKED


@pytest.mark.parametrize("gate", ["paused", "no_authorization", "approval_required", "retries_spent",
                                  "preflight_stale", "launcher_cooling", "not_a_write_task"])
def test_gates(armed, monkeypatch, gate):
    db = armed
    task = _blocked(db, LAUNCHER, authorize=gate != "no_authorization")
    _later_coder_run(db)
    if gate == "paused":
        db.add(models.ControlState(key="agents_paused", value={"paused": True}, updated_by="founder"))
    elif gate == "approval_required":
        task.approval_required = True
    elif gate == "retries_spent":
        task.retry_count = task.max_attempts
    elif gate == "preflight_stale":
        runtime_report(db, SHA, age=timedelta(hours=1))
    elif gate == "launcher_cooling":
        worker._heartbeat(db, cooldown={stage_c_worker.LAUNCHER_COOLDOWN_KEY: {
            "until": (worker._now() + timedelta(minutes=10)).isoformat(), "health": "LAUNCHER_UNAVAILABLE"}})
    elif gate == "not_a_write_task":
        task.context = {**task.context, "write": False}
    db.commit()
    assert worker.resume_resolved_blocks(db, SPECS) == []
    db.refresh(task)
    assert task.status == enums.TaskStatus.BLOCKED


def test_at_most_one_resume_per_tick(armed):
    first = _blocked(armed, LAUNCHER, key="TASK-0101", authorize=False)
    second = _blocked(armed, LAUNCHER, key="TASK-0102", authorize=False)
    control.issue_standing(armed, ["tests/test_metrics.py"], SHA, founder=FOUNDER_ID, max_per_day=3)
    _later_coder_run(armed)
    assert worker.resume_resolved_blocks(armed, SPECS) == [first.key]
    assert worker.resume_resolved_blocks(armed, SPECS) == [second.key]
