"""R5 task integrity. Break caught:
- POST /tasks (admin credential) creates a task already DONE/REVIEW/CANCELLED/..., skipping the founder-only status
  route, or injects context keys the server writes and later trusts (improvement, remote_stage_c, stage_c, ...);
- a Stage C code task that never ran is closed DONE on an evidence string instead of a recorded founder merge;
- a bare, unverified "founder" actor (or a mismatched login / non-SHA merge commit) records MERGED, and a snapshot or
  done_refusal counts such a row as the founder's merge.
Short test ids on purpose (root conftest byte budget)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, enums, main, models, stage_c
from app import stage_c_handoff as h
from tests.test_phasec_integrity_done import (DONE, FOUNDER_LOGIN, KEY, MERGE, _founder, _post, _refused,
                                              _stage_c_task)
from tests.test_task_ready_guard import engine  # noqa: F401  (pytest fixture)

T = enums.TaskStatus
ADMIN = "a" * 40


def _admin(monkeypatch):
    monkeypatch.setenv("COMPANY_API_TOKEN_ADMIN", ADMIN)
    return TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000)), \
        {"authorization": f"Bearer {ADMIN}"}


def _rows(engine):
    with Session(engine) as db:
        return (db.scalar(select(func.count()).select_from(models.Task)),
                db.scalar(select(func.count()).select_from(models.AuditLog)))


def _create(monkeypatch, **body):
    client, headers = _admin(monkeypatch)
    return client.post("/tasks", headers=headers, json={"title": "t", "success_criteria": "evidence present", **body})


# ------------------------------------------------------------------------------------------- POST /tasks

def test_create_refuses_state(engine, monkeypatch):
    before = _rows(engine)
    for status in (s.value for s in T if s not in (T.IDEA, T.BACKLOG, T.READY)):
        r = _create(monkeypatch, status=status)
        assert r.status_code == 422, (status, r.text)
    assert _rows(engine) == before                                       # nothing written


def test_create_initial_states(engine, monkeypatch):
    for status in ("IDEA", "BACKLOG", "READY"):
        r = _create(monkeypatch, status=status, dedupe_key=status,
                    context={"autonomous": True, "source": "x", "verify": {"min_actions": 1}})
        assert r.status_code == 201, r.text
        assert r.json()["status"] == status


def test_create_refuses_server_context(engine, monkeypatch):
    before = _rows(engine)
    for key in ("improvement", "remote_stage_c", "stage_c", "write", "external_release", "guardian", "incident"):
        r = _create(monkeypatch, context={"autonomous": True, key: {"stage": "MEASURED", "id": "x", "paths": ["a"]}})
        assert r.status_code == 422, (key, r.text)
        assert key in r.json()["detail"]
    assert _rows(engine) == before


def _never_run(engine):
    """A code task as the founder's Desk creates it (POST /stage-c/tasks): BACKLOG, write scope, never run."""
    client, headers = _founder()
    r = client.post("/stage-c/tasks", headers=headers, json={
        "title": "Fix metric", "objective": "Make it pass", "success_criteria": "Tests pass",
        "paths": ["tests/test_metrics.py"]})
    assert r.status_code == 201, r.text
    return r.json()["key"]


def test_code_task_route_kept(engine):
    key = _never_run(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == T.BACKLOG and task.context["write"] is True and "stage_c" in task.context


# ------------------------------------------------------------------------------------------- never-run Stage C task


def test_never_run_not_done(engine):
    key = _never_run(engine)
    _refused(_post(_founder, key, DONE))
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        assert task.status == T.BACKLOG and task.completed_at is None
        assert db.query(models.AuditLog).filter_by(entity_key=key, action="task.status").count() == 0


def test_never_run_cancel(engine):
    key = _never_run(engine)
    r = _post(_founder, key, {"status": "CANCELLED", "actor": "founder", "reason": "not needed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED"


# ------------------------------------------------------------------------------------------- verified merge actor

def test_merged_needs_verified_actor(engine):
    _stage_c_task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        for actor, evidence in (
                ("founder", {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}),            # bare, unverified
                ("founder", {}),
                ("founder (github:other, via founder_github)", {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}),
                (f"founder (github:{FOUNDER_LOGIN}, via founder_github)", {"merged_by": FOUNDER_LOGIN,
                                                                          "merge_commit": "abc"}),
                (f"founder (github:{FOUNDER_LOGIN}x", {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE})):
            with pytest.raises(PermissionError):
                stage_c.advance(db, task, "MERGED", actor, evidence)
        assert task.context["improvement"]["stage"] == "READY_TO_MERGE"


def test_verified_merge_ok(engine):
    _stage_c_task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        stage_c.advance(db, task, "MERGED", f"founder (github:{FOUNDER_LOGIN}, via founder_github)",
                        {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE})
        assert task.context["improvement"]["stage"] == "MERGED"


def test_done_ignores_bare_row(engine):
    """A MERGED row written outside advance() (direct DB/audit write) with a bare founder actor proves nothing."""
    _stage_c_task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        imp = task.context["improvement"]
        task.context = {**task.context, "improvement": {**imp, "stage": "MERGED"}}
        audit.record(db, actor="founder", action="improvement.advance", entity_type="task", entity_key=KEY,
                     task_id=task.id, before={"stage": "READY_TO_MERGE"},
                     after={"stage": "MERGED", "evidence": {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}})
        db.commit()
    _refused(_post(_founder, KEY, DONE))


def test_snapshot_ignores_bare(engine):
    """The Stage C snapshot (production's DONE input for a handoff) reports the merge only for a verified actor. Stage C
    reports MERGED, never VERIFIED: production decides VERIFIED itself (G1)."""
    _stage_c_task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        imp = task.context["improvement"]
        task.context = {**task.context, "improvement": {**imp, "stage": "MERGED", "history": [*imp["history"], {
            "stage": "MERGED", "actor": "founder", "at": "2026-11-24T00:00:00+00:00",
            "evidence": {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}}]}}
        db.add(models.ControlState(key=h.KEY_PREFIX + "hid1", value={"task_key": KEY}, updated_by="t"))
        db.commit()
        assert h.snapshot(db, "hid1")["merge"]["state"] == "NOT_OBSERVED"
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        hist = list(task.context["improvement"]["history"])   # a new list: an in-place edit is not detected
        hist[-1] = {**hist[-1], "actor": f"founder (github:{FOUNDER_LOGIN}, via founder_github)"}
        task.context = {**task.context, "improvement": {**task.context["improvement"], "history": hist}}
        db.commit()
        assert h.snapshot(db, "hid1")["merge"]["state"] == "MERGED"
