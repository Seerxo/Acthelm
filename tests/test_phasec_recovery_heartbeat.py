"""Phase C gap 4: stale Stage C runs are judged by their heartbeat, bounded by a hard maximum age."""
from datetime import timedelta

import pytest

from app import enums, models, worker
from tests.test_stage_c_worker import interrupted
from tests.test_worker import _db


def _beat(db, run, ago):
    db.get(models.Agent, run.agent_id).last_heartbeat_at = worker._now() - ago
    db.commit()


def test_beating_run_kept():
    """A second worker's run of 61 minutes whose wait() beat a minute ago is alive: it keeps its task."""
    db = _db()
    task, run = interrupted(db, "IMPLEMENTING")
    _beat(db, run, timedelta(minutes=1))
    assert worker.recover_stale(db) == []
    db.refresh(task), db.refresh(run)
    assert run.status == enums.AgentRunStatus.STARTED and task.status == enums.TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("beat_ago", [None, worker.HEARTBEAT_STALE + timedelta(minutes=1),
                                      timedelta(hours=1, minutes=5)])   # stale, or older than the run itself
def test_silent_run_recovered(beat_ago):
    db = _db()
    task, run = interrupted(db, "IMPLEMENTING")
    if beat_ago is not None:
        _beat(db, run, beat_ago)
    assert worker.recover_stale(db) == [task.key]
    db.refresh(run)
    assert run.status == enums.AgentRunStatus.FAILED


def test_heartbeat_hard_max():
    db = _db()
    task, run = interrupted(db, "IMPLEMENTING")
    run.started_at = worker._now() - worker.RUN_MAX_AGE - timedelta(minutes=1)
    db.commit()
    _beat(db, run, timedelta(seconds=10))
    assert worker.recover_stale(db) == [task.key]
    db.refresh(run)
    assert run.status == enums.AgentRunStatus.FAILED


def test_hard_maximum_is_the_hang_exit():
    """No tick, so no run, outlives the liveness hang exit; past it a beat proves nothing."""
    assert worker.RUN_MAX_AGE == timedelta(seconds=worker.HANG_EXIT_S)
