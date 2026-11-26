"""R5: the watchdog's time-in-status clock reads the status entries that recovery and Stage C reconcilers record.

recover_interrupted writes "task.stage-c.interrupted" with before/after status (phasec/integrity); stage_c_ci and
stage_c_verify block with "improvement.blocked", which carries no status. Both must start the clock, and a re-block of
an already BLOCKED task must not restart it."""
from datetime import datetime, timedelta, timezone

from app import audit, enums, models, worker
from app.watchdog import check_stale_work, status_entered_at
from tests.test_stage_c_worker import interrupted_repair
from tests.test_worker import _db

T = enums.TaskStatus


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _aware(ts):
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _row(db, task, action, at, before=None, after=None, actor="stage-c-ci"):
    db.add(models.AuditLog(actor=actor, action=action, entity_type="task", entity_key=task.key, task_id=task.id,
                           before=before, after=after, occurred_at=at))
    db.commit()


def _stale(db, status):
    return any(f.dedupe == f"stale:{status.value}" for f in check_stale_work(db))


def test_interrupted():
    """A task created 10 days ago whose CI repair was just interrupted is BLOCKED for minutes, not 10 days."""
    db = _db()
    task, _run = interrupted_repair(db, "IMPLEMENTING")
    task.created_at = _ago(days=10)
    db.commit()
    assert worker.recover_stale(db) == [task.key]
    db.refresh(task)
    assert task.status == T.BLOCKED
    assert not _stale(db, T.BLOCKED)
    row = db.query(models.AuditLog).filter_by(task_id=task.id, action="task.stage-c.interrupted").one()
    assert _aware(status_entered_at(db, task)) == _aware(row.occurred_at)
    row.occurred_at = _ago(days=4)    # the same recovery, four days on: now it is stale, by that row's time
    db.commit()
    assert _stale(db, T.BLOCKED)


def _review_task(db):
    task = models.Task(key="TASK-0055", title="t", success_criteria="reviewed by the founder", status=T.BLOCKED,
                       priority=enums.Priority.P3, risk_level=enums.RiskLevel.LOW, created_at=_ago(days=20))
    db.add(task)
    db.commit()
    _row(db, task, "task.claim", _ago(days=12), {"status": "READY"}, {"status": "IN_PROGRESS"}, "company-worker")
    _row(db, task, "task.autonomous-run", _ago(days=10), {"status": "IN_PROGRESS"}, {"status": "REVIEW"},
         "company-worker")
    return task


def test_ci_block():
    db = _db()
    task = _review_task(db)
    _row(db, task, "improvement.blocked", _ago(hours=1), after={"merge_problems": ["merged before CI passed"]})
    assert _aware(status_entered_at(db, task)) > _ago(hours=2)
    assert not _stale(db, T.BLOCKED)


def test_re_block():
    db = _db()
    task = _review_task(db)
    _row(db, task, "improvement.blocked", _ago(days=4), after={"merge_problems": ["x"]})
    _row(db, task, "improvement.blocked", _ago(hours=1), after={"merge_eligibility": "refused"})
    assert _ago(days=4, minutes=-1) > _aware(status_entered_at(db, task)) > _ago(days=4, minutes=1)
    assert _stale(db, T.BLOCKED)


def test_founder_block():
    db = _db()
    task = _review_task(db)
    audit.record(db, actor="founder", action="task.status", entity_type="task", entity_key=task.key,
                 before={"status": "REVIEW"}, after={"status": "BLOCKED"}, task_id=task.id)
    db.commit()
    founder_row = db.query(models.AuditLog).filter_by(task_id=task.id, action="task.status").one()
    founder_row.occurred_at = _ago(days=5)
    db.commit()
    _row(db, task, "improvement.blocked", _ago(hours=1), after={"merge_problems": ["x"]})
    assert _aware(status_entered_at(db, task)) == _aware(founder_row.occurred_at)


def test_keep_row():
    db = _db()
    task = _review_task(db)
    _row(db, task, "task.status", _ago(days=6), {"status": "REVIEW"}, {"status": "BLOCKED"}, "founder")
    _row(db, task, "task.assign", _ago(days=5), {"status": "BLOCKED"}, {"status": "BLOCKED"}, "founder")
    _row(db, task, "improvement.blocked", _ago(hours=1), after={"merge_problems": ["x"]})
    assert _aware(status_entered_at(db, task)) < _ago(days=5, hours=23)


def test_other_status():
    db = _db()
    task = _review_task(db)
    task.status = T.REVIEW
    db.commit()
    _row(db, task, "improvement.blocked", _ago(hours=1), after={"merge_problems": ["x"]})
    assert _ago(days=10, minutes=-1) > _aware(status_entered_at(db, task)) > _ago(days=10, minutes=1)
