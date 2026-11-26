"""Phase C gap 1: the watchdog measures how long a task has been in its CURRENT status, alerts only."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import audit, enums, models
from app.watchdog import check_stale_work


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def _task(db, status, created_ago, key="TASK-0001"):
    task = models.Task(key=key, title="t", success_criteria="reviewed by the founder", status=status,
                       priority=enums.Priority.P2, risk_level=enums.RiskLevel.LOW,
                       created_at=datetime.now(timezone.utc) - created_ago)
    db.add(task)
    db.commit()
    return task


def _entered(db, task, status, ago, before="READY"):
    db.add(models.AuditLog(actor="company-worker", action="task.status", entity_type="task", entity_key=task.key,
                           task_id=task.id, before={"status": before}, after={"status": status.value},
                           occurred_at=datetime.now(timezone.utc) - ago))
    db.commit()


def _labels(db):
    return {f.dedupe: f for f in check_stale_work(db)}


@pytest.mark.parametrize("status", [enums.TaskStatus.IN_PROGRESS, enums.TaskStatus.REVIEW,
                                    enums.TaskStatus.WAITING_APPROVAL, enums.TaskStatus.WAITING_EXTERNAL,
                                    enums.TaskStatus.BLOCKED])
def test_long_time_in_status_alerts(status):
    db = _db()
    task = _task(db, status, timedelta(days=10))
    _entered(db, task, status, timedelta(days=4))
    finding = _labels(db)[f"stale:{status.value}"]
    assert task.key in finding.detail and finding.event_type == "TASK_FAILED_REPEATEDLY"


@pytest.mark.parametrize("status", [enums.TaskStatus.IN_PROGRESS, enums.TaskStatus.REVIEW,
                                    enums.TaskStatus.WAITING_APPROVAL, enums.TaskStatus.WAITING_EXTERNAL,
                                    enums.TaskStatus.BLOCKED])
def test_new_status_not_stale(status):
    """Age since creation is not time in status: a 10-day-old task that entered this status an hour ago is fine."""
    db = _db()
    task = _task(db, status, timedelta(days=10))
    _entered(db, task, status, timedelta(days=5))            # an earlier episode in the same status
    _entered(db, task, enums.TaskStatus.READY, timedelta(days=2), before=status.value)
    _entered(db, task, status, timedelta(minutes=30))
    assert f"stale:{status.value}" not in _labels(db)


def test_same_status_audit_row_does_not_reset_the_clock():
    """A row that keeps the status (context write, assignment) is not a transition into it."""
    db = _db()
    task = _task(db, enums.TaskStatus.REVIEW, timedelta(days=10))
    _entered(db, task, enums.TaskStatus.REVIEW, timedelta(days=4))
    audit.record(db, actor="founder", action="task.assign", entity_type="task", entity_key=task.key,
                 before={"status": "REVIEW"}, after={"status": "REVIEW"}, task_id=task.id)
    db.commit()
    assert "stale:REVIEW" in _labels(db)


def test_without_audit_falls_back_to_created_at_and_never_mutates():
    db = _db()
    task = _task(db, enums.TaskStatus.IN_PROGRESS, timedelta(hours=4))
    revision = task.revision
    assert "stale:IN_PROGRESS" in _labels(db)
    db.refresh(task)
    assert task.status == enums.TaskStatus.IN_PROGRESS and task.revision == revision


def test_in_progress_inside_the_recovery_window_is_quiet():
    db = _db()
    _task(db, enums.TaskStatus.IN_PROGRESS, timedelta(hours=2))
    assert "stale:IN_PROGRESS" not in _labels(db)
