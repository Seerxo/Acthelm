"""/desk/state stays bounded as history grows: exact counts from SQL, capped closed tasks, aggregate cost."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.orm import Session

from app import desk, enums, models, registry
from tests.test_desk_stream_postgres import engine as postgres_engine

NOW = datetime.now(timezone.utc)


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return Session(engine)


def _task(i, status, agent, **kw):
    return models.Task(key=f"TASK-{i:05d}", title=f"t{i}", success_criteria="s", status=status,
                       priority=enums.Priority.P2, risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id,
                       created_at=NOW - timedelta(hours=i), updated_at=NOW - timedelta(hours=i), **kw)


@pytest.fixture(params=("sqlite", "postgresql"))
def db(request):
    if request.param == "sqlite":
        session = _db()
    else:
        session = Session(request.getfixturevalue("postgres_engine"))
    try:
        yield session
    finally:
        session.close()


def test_closed_tasks_are_capped_while_counts_open_and_failed_tasks_stay_exact(db):
    agent = models.Agent(key="research", role="Research", enabled=True)
    db.add(agent)
    db.flush()
    n = desk.CLOSED_TASKS_SHOWN
    done = [_task(i, enums.TaskStatus.DONE, agent, completed_at=NOW - timedelta(hours=i))
            for i in range(n + 50)]
    cancelled = [_task(1000 + i, enums.TaskStatus.CANCELLED, agent) for i in range(10)]
    failed = [_task(2000 + i, enums.TaskStatus.FAILED, agent) for i in range(150)]
    open_ = [_task(3000 + i, enums.TaskStatus.READY, agent) for i in range(5)]
    # Stage C work still in flight on an old closed task must stay visible.
    old_merged = _task(4000, enums.TaskStatus.DONE, agent, context={"improvement": {
        "stage": "VERIFYING", "history": [{"stage": "VERIFYING", "at": "2026-10-01T00:00:00Z"}]}})
    measured = _task(4001, enums.TaskStatus.DONE, agent, context={"improvement": {"stage": "MEASURED"}})
    db.add_all(done + cancelled + failed + open_ + [old_merged, measured])
    db.flush()
    # Activity naming an old closed task still names it.
    db.add(models.Event(event_type="TASK_DONE", source="worker", task_id=old_merged.id))
    db.commit()

    state = desk.build_state(db, registry.load(), False)

    assert state["task_counts"] == {"DONE": n + 52, "CANCELLED": 10, "FAILED": 150, "READY": 5}
    assert state["tasks_total"] == n + 217
    statuses = [t["status"] for t in state["tasks"]]
    assert statuses.count("DONE") + statuses.count("CANCELLED") == n
    assert statuses.count("FAILED") == 150 and statuses.count("READY") == 5
    assert state["tasks_truncated"] == state["tasks_total"] - len(state["tasks"])
    assert len({t["key"] for t in state["tasks"]}) == len(state["tasks"])
    assert next(a for a in state["agents"] if a["key"] == "research")["tasks_done"] == n + 52
    keys = {t["key"] for t in state["tasks"]}
    assert "TASK-00000" in keys and f"TASK-{n + 49:05d}" not in keys  # newest closed kept
    assert state["overview"]["open_tasks"] == 5
    # Lead time uses every DONE task in the window, not the capped list.
    assert state["task_lead_time"]["sample_size"] == n + 50 == sum(
        1 for t in done if t.completed_at.replace(tzinfo=timezone.utc) >= NOW - timedelta(days=30))
    assert [i["task"] for i in state["improvements"]] == ["TASK-04000"]
    assert state["activity"][0]["task"] == "TASK-04000"
    # Old order: priority, then newest first.
    p2 = [t["created_at"] for t in state["tasks"] if t["status"] == "READY"]
    assert p2 == sorted(p2, reverse=True)


def test_cost_total_matches_the_old_python_sum(db):
    agent = models.Agent(key="research", role="Research", enabled=True)
    db.add(agent)
    db.flush()
    calls = [None, {}, {"incremental_cost_usd": None}, {"incremental_cost_usd": 0},
             {"incremental_cost_usd": 1.25}, {"incremental_cost_usd": "0.5"},
             {"incremental_cost_usd": ""}, {"incremental_cost_usd": False},
             {"incremental_cost_usd": True}, {"other": 9}]
    db.add_all(models.AgentRun(agent_id=agent.id, provider="claude", status=enums.AgentRunStatus.SUCCEEDED,
                               tool_calls=tc) for tc in calls)
    db.commit()
    old = sum(float((tc or {}).get("incremental_cost_usd") or 0) for tc in calls)
    assert desk.cost_view(db)["new_autonomous_spend_usd"] == old == 2.75
    assert desk.cost_view(_db())["new_autonomous_spend_usd"] == 0.0


@pytest.mark.parametrize("value", ["garbage", "1.25oops", {"unexpected": 1}])
def test_malformed_cost_is_never_silently_counted_as_zero(db, value):
    agent = models.Agent(key="research", role="Research", enabled=True)
    db.add(agent)
    db.flush()
    db.add(models.AgentRun(agent_id=agent.id, provider="claude", status=enums.AgentRunStatus.SUCCEEDED,
                           tool_calls={"incremental_cost_usd": value}))
    db.commit()
    with pytest.raises((ValueError, TypeError)):
        desk.cost_view(db)


def test_task_closing_during_state_read_uses_one_snapshot(postgres_engine):
    with Session(postgres_engine) as db:
        agent = models.Agent(key="research", role="Research", enabled=True)
        db.add(agent)
        db.flush()
        task = _task(0, enums.TaskStatus.READY, agent)
        db.add(task)
        db.commit()
        task_id = task.id
        db.expunge_all()
        changed = False

        def close_task(_conn, _cursor, statement, _params, _ctx, _many):
            nonlocal changed
            if not changed and "FROM tasks" in statement:
                changed = True
                with postgres_engine.begin() as writer:
                    writer.execute(update(models.Task).where(models.Task.id == task_id).values(
                        status=enums.TaskStatus.DONE, completed_at=NOW))

        event.listen(postgres_engine, "after_cursor_execute", close_task)
        try:
            state = desk.build_state(db, registry.load(), False)
        finally:
            event.remove(postgres_engine, "after_cursor_execute", close_task)
        assert changed
        assert len(state["tasks"]) == state["tasks_total"] == 1
        assert state["tasks_truncated"] == 0
        assert state["task_counts"] == {state["tasks"][0]["status"]: 1}
