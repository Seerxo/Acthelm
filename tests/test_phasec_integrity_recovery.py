"""Phase C gap 3: every status change stage_c_worker.recover_interrupted makes has its own audit row (actor
company-worker, before/after status, reason, run id). Nothing else about the recovery changes."""
import pytest

from app import audit, enums, models, stage_c, worker
from tests.test_stage_c_worker import interrupted, interrupted_repair
from tests.test_worker import _db

T = enums.TaskStatus


def _rows(db, task):
    return db.query(models.AuditLog).filter_by(task_id=task.id, action="task.stage-c.interrupted").all()


@pytest.mark.parametrize("case, status", [
    ("none", T.READY), ("review", T.READY), ("detected", T.CANCELLED), ("testing", T.CANCELLED),
    ("pr_open", T.REVIEW), ("repair", T.BLOCKED),
])
def test_recovery_audited(case, status):
    db = _db()
    if case == "repair":
        task, run = interrupted_repair(db, "IMPLEMENTING")
    else:
        stage = {"none": None, "review": "INDEPENDENT_REVIEW", "detected": "DETECTED", "testing": "TESTING",
                 "pr_open": "INDEPENDENT_REVIEW"}[case]
        task, run = interrupted(db, stage)
        if case == "pr_open":
            stage_c.advance(db, task, "PR_OPEN", "stage-c-runner", {"pr": "https://github.com/x/y/pull/1"})
            db.commit()
    stage_before = ((task.context or {}).get("improvement") or {}).get("stage")
    assert worker.recover_stale(db) == [task.key]
    db.refresh(task)
    assert task.status == status
    [row] = _rows(db, task)
    assert row.actor == "company-worker" and row.entity_type == "task" and row.entity_key == task.key
    assert row.before["status"] == "IN_PROGRESS" and row.before["stage"] == stage_before
    assert row.after["status"] == status.value and row.after["run_id"] == run.id
    assert row.after["stage"] == ((task.context or {}).get("improvement") or {}).get("stage")
    assert row.reason == task.error and row.after["error"] == task.error
    assert worker.recover_stale(db) == [] and len(_rows(db, task)) == 1   # idempotent: one change, one row


def test_a_run_that_no_longer_owns_its_task_writes_no_recovery_row():
    """A cancelled task is not the run's: recover_stale records it as superseded, recover_interrupted changes nothing."""
    db = _db()
    task, run = interrupted(db, "DETECTED")
    task.status = T.CANCELLED
    audit.record(db, actor="founder", action="task.status", entity_type="task", entity_key=task.key,
                 task_id=task.id, after={"status": "CANCELLED"})
    db.commit()
    assert worker.recover_stale(db) == [task.key]
    db.refresh(task)
    assert task.status == T.CANCELLED and _rows(db, task) == []
    assert db.query(models.AuditLog).filter_by(task_id=task.id, action="task.autonomous-run.superseded").count() == 1
