"""Phase C gap 5: a native (non-handoff) task that went through Stage C is DONE only with a verified founder merge
recorded by Stage C itself (stage_c_ci._record_merge's MERGED row, then VERIFYING/MEASURED), never on an evidence
string or a client-written context. Tasks that never entered Stage C keep the general DONE rule. Service callers
still cannot move a Stage C task to DONE, REVIEW or READY."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, enums, main, models, stage_c, stage_c_ci
from tests.test_task_ready_guard import DESK_HOST, engine  # noqa: F401  (pytest fixture)

T = enums.TaskStatus
KEY = "TASK-0040"
HEAD, MERGE = "a" * 40, "c" * 40
FOUNDER_LOGIN = "founder-login"
DONE = {"status": "DONE", "actor": "founder", "evidence": "merged and verified", "reason": "accept"}


def _founder():
    return TestClient(main.app, base_url=f"https://{DESK_HOST}", client=("172.18.0.5", 50000)), \
        {"Cf-Access-Jwt-Assertion": "founder"}


def _service():
    return TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000)), \
        {"authorization": f"Bearer {'d' * 40}"}


def _post(who, key, body):
    client, headers = who()
    return client.post(f"/tasks/{key}/status", headers=headers, json=body)


def _stage_c_task(engine, upto="READY_TO_MERGE", status=T.REVIEW, key=KEY):
    """A write task advanced through the real lifecycle (stage_c.advance writes its audit rows) up to `upto`."""
    with Session(engine) as db:
        agent = db.scalar(select(models.Agent).where(models.Agent.key == "developer"))
        project = db.scalar(select(models.Project).where(models.Project.key == "company-os"))
        task = models.Task(key=key, title="t", success_criteria="evidence present", objective="o", status=status,
                           risk_level=enums.RiskLevel.LOW, assigned_agent_id=agent.id, project_id=project.id,
                           context={"autonomous": True, "write": True, "stage_c": {"paths": ["tests/test_metrics.py"]}})
        db.add(task)
        db.flush()
        for stage, actor, evidence in (
                ("DETECTED", "stage-c-runner", {}), ("PLANNED", "stage-c-runner", {}),
                ("IMPLEMENTING", "stage-c-runner", {}), ("TESTING", "stage-c-runner", {"coder": "claude"}),
                ("INDEPENDENT_REVIEW", "stage-c-runner", {"fingerprint": "f" * 64}),
                ("PR_OPEN", "stage-c-runner", {"commit": HEAD, "pr": "https://github.com/seerxo/acthelm/pull/81"}),
                ("CI", "stage-c-ci", {"commit": HEAD}), ("READY_TO_MERGE", "stage-c-ci", {"commit": HEAD})):
            stage_c.advance(db, task, stage, actor, evidence)
            if stage == upto:
                break
        db.commit()


def _record_merge(engine, key=KEY):
    """The founder's merge as Stage C records it: GitHub facts checked by stage_c_ci._record_merge."""
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        facts = {"merged_by": FOUNDER_LOGIN, "draft": False, "base_ref": "main", "head_sha": HEAD,
                 "merge_commit": MERGE}
        out = stage_c_ci._record_merge(db, task, "READY_TO_MERGE", facts, {"commit": HEAD, "pr_number": 81},
                                       FOUNDER_LOGIN, "main")
        assert out == {"outcome": "MERGED"}
        db.commit()


def _task(engine, key=KEY):
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == key))
        statuses = db.query(models.AuditLog).filter_by(entity_key=key, action="task.status").count()
        return task.status, (task.context or {}).get("improvement", {}).get("stage"), statuses


def _refused(r):
    assert r.status_code == 409, r.text
    assert "verified founder merge" in r.json()["detail"]


# ------------------------------------------------------------------------------------------- refused

@pytest.mark.parametrize("upto", ["PR_OPEN", "READY_TO_MERGE"])
def test_evidence_alone(engine, upto):
    _stage_c_task(engine, upto)
    _refused(_post(_founder, KEY, DONE))
    assert _task(engine) == (T.REVIEW, upto, 0)                         # nothing written


def test_abandoned_not_done(engine):
    _stage_c_task(engine, "TESTING", status=T.BLOCKED)
    with Session(engine) as db:
        stage_c.advance(db, db.scalar(select(models.Task).where(models.Task.key == KEY)), "ABANDONED",
                        "company-worker", {"reason": "x"})
        db.commit()
    _refused(_post(_founder, KEY, DONE))


@pytest.mark.parametrize("stage", ["MERGED", "MEASURED"])
def test_hand_edited_ctx(engine, stage):
    _stage_c_task(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        imp = task.context["improvement"]
        task.context = {**task.context, "improvement": {**imp, "stage": stage, "history": [*imp["history"], {
            "stage": "MERGED", "actor": f"founder (github:{FOUNDER_LOGIN})",
            "evidence": {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}}]}}
        db.commit()
    _refused(_post(_founder, KEY, DONE))
    assert _task(engine)[0] == T.REVIEW


def test_fabricated_ctx(engine, monkeypatch):
    """A MEASURED improvement in a client body (POST /tasks, admin credential) proves nothing: since R5 the server-owned
    context keys and non-initial states are refused at creation, and even a task already holding such a context (an
    older row) is refused DONE, because done_refusal reads only server-written audit rows."""
    monkeypatch.setenv("COMPANY_API_TOKEN_ADMIN", "a" * 40)
    client = TestClient(main.app, base_url="http://testserver", client=("172.18.0.5", 50000))
    headers = {"authorization": f"Bearer {'a' * 40}"}
    fake = {"stage": "MEASURED", "history": [{"stage": "MERGED", "actor": f"founder (github:{FOUNDER_LOGIN})",
                                              "evidence": {"merged_by": FOUNDER_LOGIN, "merge_commit": MERGE}}]}
    r = client.post("/tasks", headers=headers, json={
        "title": "fabricated", "success_criteria": "evidence present", "status": "REVIEW",
        "context": {"autonomous": True, "write": True, "improvement": fake}})
    assert r.status_code == 422, r.text
    r = client.post("/tasks", headers=headers, json={
        "title": "fabricated", "success_criteria": "evidence present", "context": {"improvement": fake}})
    assert r.status_code == 422, r.text
    with Session(engine) as db:
        assert db.query(models.Task).count() == 0
        db.add(models.Task(key=KEY, title="t", success_criteria="evidence present", status=T.REVIEW,
                           risk_level=enums.RiskLevel.LOW, context={"autonomous": True, "improvement": fake}))
        db.commit()
    _refused(_post(_founder, KEY, DONE))


def test_merged_no_evidence(engine):
    """stage_c.advance refuses a bare "founder" actor for MERGED (no GitHub merge proof), so the task stays open."""
    _stage_c_task(engine)
    with Session(engine) as db:
        with pytest.raises(PermissionError):
            stage_c.advance(db, db.scalar(select(models.Task).where(models.Task.key == KEY)), "MERGED", "founder")
        db.rollback()
    _refused(_post(_founder, KEY, DONE))


def test_refresh_no_row(engine):
    _stage_c_task(engine, status=T.BLOCKED)
    with Session(engine) as db:
        db.scalar(select(models.Task).where(models.Task.key == KEY)).error = \
            stage_c_ci.REFRESH_CONTEXT_NOT_REVIEWED + "x"
        db.commit()
    _refused(_post(_founder, KEY, DONE))


# ------------------------------------------------------------------------------------------- allowed

@pytest.mark.parametrize("then", [None, "VERIFYING", "MEASURED"])
def test_recorded_merge_closes(engine, then):
    _stage_c_task(engine)
    _record_merge(engine)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        for stage in ("VERIFYING", "MEASURED")[:("VERIFYING", "MEASURED").index(then) + 1] if then else ():
            stage_c.advance(db, task, stage, "stage-c-verify", {"merge_commit": MERGE})
        db.commit()
    r = _post(_founder, KEY, DONE)
    assert r.status_code == 200, r.text
    assert _task(engine) == (T.DONE, then or "MERGED", 1)


def test_founder_accepts_refresh(engine):
    """stage_c_ci.reconcile_refreshed leaves a verified founder merge BLOCKED for explicit acceptance."""
    _stage_c_task(engine, status=T.BLOCKED)
    with Session(engine) as db:
        task = db.scalar(select(models.Task).where(models.Task.key == KEY))
        task.error = stage_c_ci.REFRESH_CONTEXT_NOT_REVIEWED + "not reviewed against the updated main"
        audit.record(db, actor=stage_c_ci.ACTOR, action="improvement.refresh_reconciled", entity_type="task",
                     entity_key=KEY, task_id=task.id,
                     after={"outcome": "REFRESH_CONTEXT_NOT_REVIEWED", "merge_commit": MERGE,
                            "merged_by": FOUNDER_LOGIN, "status": "BLOCKED", "stage": "READY_TO_MERGE"})
        db.commit()
    assert _post(_founder, KEY, DONE).status_code == 200


@pytest.mark.parametrize("context", [None, {"autonomous": True}, {"autonomous": True, "write": True,
                                                                  "stage_c": {"paths": ["tests/test_metrics.py"]}}])
def test_non_stage_c_done_rule(engine, context):
    """Non-code tasks keep the general rule (evidence). A code task (context.stage_c) that never ran has no merge to
    show (R5): DONE is refused and it is cancelled instead."""
    with Session(engine) as db:
        db.add(models.Task(key=KEY, title="t", success_criteria="evidence present", status=T.REVIEW,
                           risk_level=enums.RiskLevel.LOW, context=context))
        db.commit()
    assert _post(_founder, KEY, {**DONE, "evidence": None, "result": None}).status_code == 422
    if context and "stage_c" in context:
        _refused(_post(_founder, KEY, DONE))
        assert _post(_founder, KEY, {"status": "CANCELLED", "actor": "founder", "reason": "x"}).status_code == 200
    else:
        assert _post(_founder, KEY, DONE).status_code == 200


# ------------------------------------------------------------------------------------------- callers

@pytest.mark.parametrize("status", ["DONE", "REVIEW", "READY", "CANCELLED"])
def test_fake_founder_merged(engine, status):
    _stage_c_task(engine)
    _record_merge(engine)
    r = _post(_service, KEY, {**DONE, "status": status})
    assert r.status_code == 403, r.text
    assert _task(engine) == (T.REVIEW, "MERGED", 0)


@pytest.mark.parametrize("status", ["DONE", "REVIEW", "READY"])
def test_svc_unmerged(engine, status):
    _stage_c_task(engine, status=T.BLOCKED)
    r = _post(_service, KEY, {**DONE, "status": status})
    assert r.status_code == 403, r.text
    assert _task(engine) == (T.BLOCKED, "READY_TO_MERGE", 0)
