"""POST /stage-c/tasks and the Desk's Stage C readiness fields. Break caught: a non-founder creates a code-writing
task, a write path outside the allowed Stage C files is accepted, the task is created READY or without the scope that
admission needs, or the failing preflight checks / Mark READY authorization are not served.
Few, short test ids on purpose: the root conftest caps the `PASSED <id>` bytes of the whole suite."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import control, enums, models, stage_c_worker
from tests.test_founder_authorization import (ADMIN, ASSERT, DESK, FID, SHA, YT, _bearer, _c, _founder,  # noqa: F401
                                              env)

GOAL_PY, TEST_PY = "app/goals.py", "tests/test_goals.py"
BODY = {"title": "Fix goal rounding", "objective": "Round progress once", "success_criteria": "Tests pass",
        "paths": [GOAL_PY, TEST_PY], "context": ["app/models.py"]}


@pytest.fixture
def seeded(env):
    with Session(env[0]) as s:
        s.add_all([models.Project(key="company-os", name="Company OS"), models.Agent(key="developer", role="dev")])
        s.commit()
    return env[0]


def _tasks(engine):
    with Session(engine) as s:
        return list(s.scalars(select(models.Task).where(models.Task.key != "TASK-0045")))


def test_create(seeded):
    assert GOAL_PY in stage_c_worker.task_paths() and set(stage_c_worker.task_paths()) <= stage_c_worker.SAFE_FILES
    r = _founder().post("/stage-c/tasks", json=BODY, headers=ASSERT)
    assert r.status_code == 201, r.text
    [t] = _tasks(seeded)
    assert r.json() == {"key": t.key, "status": "BACKLOG"}
    assert t.status == enums.TaskStatus.BACKLOG and t.risk_level == enums.RiskLevel.LOW and not t.approval_required
    assert t.context == {"autonomous": True, "write": True,
                         "stage_c": {"paths": [GOAL_PY, TEST_PY], "context": ["app/models.py"]}}
    assert t.assigned_agent_id and t.project_id
    # Exactly the scope admission accepts.
    assert not stage_c_worker.scope_refused(t.context["stage_c"], set(stage_c_worker.task_paths()))
    with Session(seeded) as s:
        assert s.scalar(select(models.AuditLog.actor).where(models.AuditLog.action == "task.create")) == FID
    # Task detail: Mark READY waits until an authorization covers the task; other tasks carry no code.
    detail = lambda key: _founder().get(f"/tasks/{key}", headers=ASSERT).json()["stage_c_authorization"]  # noqa: E731
    assert detail(t.key) == "stage_c_authorization_missing"
    with Session(seeded) as s:
        control.authorize_pilot(s, t.key, SHA, founder=FID)
    assert detail(t.key) is None and detail("TASK-0045") is None


def test_refused(seeded):
    callers = [("172.18.0.5", {}, 401), ("172.18.0.5", _bearer(DESK), 403), ("172.18.0.5", _bearer(YT), 403),
               ("172.18.0.5", _bearer(ADMIN), 403), ("127.0.0.1", {}, 403), ("10.0.6.1", {}, 403)]
    for host, headers, code in callers:
        assert _c(host).post("/stage-c/tasks", json=BODY, headers=headers).status_code == code, (host, headers)
    # The Desk bearer on the Desk host, without an Access assertion.
    assert _founder().post("/stage-c/tasks", json=BODY, headers=_bearer(DESK)).status_code == 401
    assert _tasks(seeded) == []


def test_bad_422(seeded):
    for change in ({"paths": ["app/main.py"]}, {"paths": [GOAL_PY, GOAL_PY]}, {"paths": ["../app/goals.py"]},
                   {"context": [GOAL_PY]}, {"context": ["README.md"]}, {"paths": []}):
        assert _founder().post("/stage-c/tasks", json={**BODY, **change}, headers=ASSERT).status_code == 422, change
    assert _tasks(seeded) == []


def test_preflight(env):
    with Session(env[0]) as s:
        s.add(models.ControlState(key=stage_c_worker.RUNTIME_READINESS_KEY, updated_by="watchdog", value={
            "ready": False, "source_sha": SHA, "checked_at": "2026-11-09T10:00:00+00:00",
            "failed": ["launcher", "Bad Name!", 3, "egress_proxy"]}))
        s.commit()
    out = _founder().get("/stage-c/state", headers=ASSERT).json()
    pre = out["runtime"]["preflight"]
    assert pre["failed"] == ["launcher", "other", "other", "egress_proxy"] and pre["checked_at"].startswith("2026-11-09T10:00:00")
    assert out["code_task_paths"] == stage_c_worker.task_paths()
