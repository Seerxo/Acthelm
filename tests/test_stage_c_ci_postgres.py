"""A PR merged after its CI failed is blocked, not restarted, against real PostgreSQL.

Same disposable-database guard as the other PostgreSQL tests: only the CI test database is
allowed, CI fails rather than skips when its URL is missing, and local runs without a URL skip.
"""
import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import audit, enums, models, stage_c, stage_c_ci
from tests.test_stage_c_refresh_reconcile import scene  # synthetic local Git history; no network

REQUIRED_CONTEXTS = ("ci/circleci: python-tests", "ci/circleci: frontend-tests")
GREEN_STATUSES = {context: "success" for context in REQUIRED_CONTEXTS}


def ingest_ci(*args, **kwargs):
    """ingest with the policy's required commit statuses (the production contract)."""
    return stage_c_ci.ingest(*args, required_statuses=REQUIRED_CONTEXTS, **kwargs)


SHA = "a" * 40
MERGE = "c" * 40
FOUNDER = "semihbugrasezer"
BAD = {"name": "test", "status": "completed", "conclusion": "failure"}


@pytest.fixture
def db():
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _repaired(db):
    t = models.Task(key="TASK-0080", title="fix", success_criteria="ci green", priority=enums.Priority.P1)
    db.add(t)
    db.flush()
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING", "TESTING", "INDEPENDENT_REVIEW"):
        stage_c.advance(db, t, s, "stage-c-runner")
    stage_c.advance(db, t, "PR_OPEN", "stage-c-runner",
                    {"branch": "task/TASK-0080", "commit": SHA, "fingerprint": "f" * 64,
                     "pr": "https://github.com/o/r/pull/90"})
    ingest_ci(db, t, {"state": "open", "merged": False, "head_sha": SHA, "checks": [BAD],
                              "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": []})
    db.commit()
    assert t.context["improvement"]["stage"] == "IMPLEMENTING"
    return t


def test_postgres_merge_after_failed_ci_is_blocked_with_history_and_audit(db):
    t = _repaired(db)
    history = list(t.context["improvement"]["history"])
    facts = {"state": "closed", "merged": True, "head_sha": SHA, "checks": [], "checks_complete": True, "statuses": dict(GREEN_STATUSES), "statuses_complete": True, "status_problems": [],
             "merged_by": FOUNDER, "draft": False, "base_ref": "main", "merge_commit": MERGE}
    out = ingest_ci(db, t, facts, founder_login=FOUNDER, tracked_branch="main")
    db.commit()

    assert out["outcome"] == "MERGE_UNVERIFIED"
    fresh = db.scalar(select(models.Task).where(models.Task.key == "TASK-0080"))
    assert fresh.status == enums.TaskStatus.BLOCKED
    assert fresh.context["improvement"]["stage"] == "IMPLEMENTING"  # no restart of coding
    assert fresh.context["improvement"]["history"] == history
    rows = db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0080",
                                                    models.AuditLog.action == "improvement.blocked")).all()
    assert len(rows) == 1 and rows[0].after["merge_commit"] == MERGE

    # A repeated reconciliation of the same merge must not write a second block or restart anything.
    again = ingest_ci(db, fresh, facts, founder_login=FOUNDER, tracked_branch="main")
    db.commit()
    assert again["outcome"] == "SKIPPED"
    assert len(db.scalars(select(models.AuditLog).where(models.AuditLog.entity_key == "TASK-0080",
                                                        models.AuditLog.action == "improvement.blocked")).all()) == 1


def test_postgres_refreshed_merge_is_append_only(db, scene):
    _, source, _, _, facts, chain, mirror, *_ = scene
    task = models.Task(key=source.key, title=source.title, success_criteria=source.success_criteria,
                       priority=source.priority, status=enums.TaskStatus.BLOCKED, error=source.error,
                       context=source.context)
    db.add(task)
    db.flush()
    approval = dict(task.context["improvement"]["approval"])
    review = audit.record(db, actor="codex", action="improvement.review_approved", entity_type="task",
                          entity_key=task.key, task_id=task.id, after=approval)
    db.flush()
    approval["audit_id"] = review.id
    task.context = {**task.context, "improvement": {**task.context["improvement"], "approval": approval}}
    block = audit.record(db, actor="stage-c-ci", action="improvement.blocked", entity_type="task",
                         entity_key=task.key, task_id=task.id, after={"head": facts["head_sha"]})
    db.flush()
    request = audit.record(db, actor="founder:access:founder@example.invalid",
                           action="stage-c.refresh_reconcile.requested", entity_type="task",
                           entity_key=task.key, task_id=task.id,
                           after={"task_key": task.key, "pr_number": 1, "final_head": facts["head_sha"],
                                  "merge_commit": facts["merge_commit"]})
    db.commit()
    old = list(task.context["improvement"]["history"])
    assert stage_c_ci.reconcile_refreshed(db, task, request, facts, chain, mirror,
        repository="seerxo/acthelm", head_repository="seerxo/acthelm",
        required_statuses=(), apply=True)["outcome"] == "MERGED"
    db.commit()
    db.expire_all()
    assert task.status == enums.TaskStatus.REVIEW and task.context["improvement"]["history"][:-1] == old
    row = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "improvement.refresh_reconciled"))
    assert row.before["blocked_audit_id"] == block.id and row.after["request_audit_id"] == request.id
