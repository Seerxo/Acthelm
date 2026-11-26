"""R5: read-only reconciliation of every open task (app/reconciliation.py, GET /desk/reconciliation).

Synthetic fixtures (keys, ages, texts and names are made up) cover each class an open-task inventory can hold: old non-opted-in BACKLOG/READY work, an opted-in READY task behind the pause, WAITING_* without a
release path, a pending approval, BLOCKED RUNTIME_ERROR / merge-before-CI / founder note / escalation without error
text, a capacity block with proof, REVIEW outputs, FAILED with exhausted and remaining retries, a stale claim, and
repository locks of retired repository names. Break caught: a gate that writes, a classification that calls a refused
path recoverable, free text in the API response, or a non-founder reading it."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app import control, enums, models, reconciliation, registry, stage_c, stage_c_worker
from tests.test_founder_authorization import ADMIN, ASSERT, DESK, YT, _bearer, _c, _founder, env  # noqa: F401
from tests.test_stage_c_worker import write_task
from tests.test_worker import _db, _task

T = enums.TaskStatus
R = reconciliation
NOTE = "Deferred during the single-task pilot; its objective does not match its allowed paths"


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _agent(db, key):
    return db.query(models.Agent).filter_by(key=key).one().id


def _inventory(db):
    """One task per production class. Returns {label: task key}."""
    db.add(models.ControlState(key=control._PAUSE_KEY, value={"paused": True}, updated_by="founder"))
    k = {}
    k["backlog_old"] = _task(db, "TASK-9017", autonomous=None, status=T.BACKLOG, approval_required=True,
                             risk_level=enums.RiskLevel.MEDIUM, created_at=_ago(days=53),
                             assigned_agent_id=_agent(db, "developer")).key
    k["ready_not_opted"] = _task(db, "TASK-9042", autonomous=False, created_at=_ago(days=44)).key
    k["ready_opted"] = _task(db, "TASK-9074", assigned_agent_id=_agent(db, "ceo"), created_at=_ago(days=32)).key
    k["waiting_external"] = _task(db, "TASK-9033", autonomous=False, status=T.WAITING_EXTERNAL,
                                  created_at=_ago(days=36)).key
    approval_task = _task(db, "TASK-9014", autonomous=None, status=T.WAITING_APPROVAL, approval_required=True,
                          risk_level=enums.RiskLevel.MEDIUM, created_at=_ago(days=25))
    db.add(models.Approval(key="APR-0007", action="a", reason="r", expected_effect="e", rollback="r",
                           risk_level=enums.RiskLevel.MEDIUM, requested_by="supervisor", task_id=approval_task.id))
    k["approval"] = approval_task.key

    project = None
    runtime = write_task(db, key="TASK-9054", status=T.BLOCKED, error="RUNTIME_ERROR", retry_count=1,
                         created_at=_ago(days=42))
    project = runtime.project_id
    k["runtime_error"] = runtime.key

    def code(key, **over):
        agent = _agent(db, "developer")
        return _task(db, key, project_id=project, assigned_agent_id=agent,
                     context={"autonomous": True, "write": True, "stage_c": {"paths": ["tests/test_metrics.py"]}},
                     **over)
    merged = code("TASK-9055", status=T.BLOCKED, created_at=_ago(days=34),
                  error="Merge not recorded: synthetic example")
    for s in ("DETECTED", "PLANNED", "IMPLEMENTING"):
        stage_c.advance(db, merged, s, "stage-c-runner", {})
    k["merge_before_ci"] = merged.key
    k["founder_note"] = code("TASK-9060", status=T.BLOCKED, error=NOTE, retry_count=1, created_at=_ago(days=39)).key
    escalated = _task(db, "TASK-9049", status=T.BLOCKED, result="needs a product decision", created_at=_ago(days=44))
    db.add(models.AgentRun(task_id=escalated.id, provider="claude", status=enums.AgentRunStatus.ESCALATED,
                           started_at=_ago(days=40), ended_at=_ago(days=40)))
    k["escalated"] = escalated.key

    capacity = code("TASK-9090", status=T.BLOCKED, created_at=_ago(days=3),
                    error="CODER_CAPACITY: the coder was unavailable 3 attempts in a row; check ..., then set READY")
    db.add(models.AgentRun(task_id=capacity.id, provider="claude", status=enums.AgentRunStatus.FAILED,
                           started_at=_ago(days=2, minutes=5), ended_at=_ago(days=2),
                           tool_calls={"stage_c": True, "outcome": "CODER_FAILED", "capacity_failure": True}))
    other = code("TASK-9091", status=T.REVIEW, created_at=_ago(days=1))
    db.add(models.AgentRun(task_id=other.id, provider="claude", status=enums.AgentRunStatus.SUCCEEDED,
                           started_at=_ago(hours=5), ended_at=_ago(hours=4),
                           tool_calls={"stage_c": True, "model_calls": 2, "outcome": "PR_OPEN"}))
    k["capacity"] = capacity.key
    k["review_output"] = _task(db, "TASK-9027", status=T.REVIEW, result="research summary", created_at=_ago(days=52)).key
    k["failed_exhausted"] = code("TASK-9061", status=T.FAILED, error="CHECKS_FAILED", retry_count=1, max_attempts=1,
                                 created_at=_ago(days=39)).key
    k["failed_retries_left"] = code("TASK-9044", status=T.FAILED, error="REJECTED", retry_count=2, max_attempts=3,
                                    created_at=_ago(days=42)).key
    k["stale_claim"] = _task(db, "TASK-9095", status=T.IN_PROGRESS, created_at=_ago(days=1),
                             updated_at=_ago(hours=3)).key
    db.add(models.ControlState(key="lock:repo:example/retired-os", value={"token": 4}, updated_by="stage-c"))
    db.add(models.ControlState(key="lock:repo:example-owner/retired-app", updated_by="stage-c-runner:TASK-9010",
                               value={"token": 9, "holder": "stage-c-runner:TASK-9010",
                                      "expires_at": _ago(days=40).isoformat()}))
    db.add(models.ControlState(key=stage_c.lease_key(stage_c_worker.REPOSITORY), updated_by="stage-c-runner:TASK-9054",
                               value={"token": 2, "holder": "stage-c-runner:TASK-9054",
                                      "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()}))
    db.commit()
    return k


def _snapshot(db):
    tables = (models.Task, models.AuditLog, models.AgentRun, models.ControlState, models.Approval, models.Event)
    counts = tuple(db.scalar(select(func.count()).select_from(t)) for t in tables)
    rows = tuple(db.execute(select(models.Task.key, models.Task.status, models.Task.revision, models.Task.updated_at,
                                   models.Task.error, models.Task.retry_count).order_by(models.Task.id)).all())
    locks = tuple(db.execute(select(models.ControlState.key, models.ControlState.value)
                             .order_by(models.ControlState.key)).all())
    return counts, rows, locks


@pytest.fixture
def inventory():
    db = _db()
    keys = _inventory(db)
    writes = []

    def no_writes(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().split(" ", 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            writes.append(statement)
    engine = db.get_bind()
    before = _snapshot(db)
    event.listen(engine, "before_cursor_execute", no_writes)
    try:
        report = R.build(db, specs=registry.load())
    finally:
        event.remove(engine, "before_cursor_execute", no_writes)
    assert writes == [] and _snapshot(db) == before
    rows = {r["key"]: r for r in report["tasks"]}
    return report, {label: rows[key] for label, key in keys.items()}


def test_rows(inventory):
    report, rows = inventory
    assert report["read_only"] is True and report["system"]["agents_paused"] is True
    assert report["system"]["blocked_auto_resume_flag"] is False         # default off, never enabled here
    for row in report["tasks"]:
        assert row["status"] in R.OPEN and row["bucket"] in R.BUCKETS
        assert row["actor"] in ("system", "founder") and row["operation"] and row["precondition"]
        assert row["in_status_h"] is not None and row["in_status_basis"] in ("audit", "created_at")
    assert sum(v["count"] for v in report["summary"].values()) == len(report["tasks"])


def test_classes(inventory):
    _, r = inventory
    assert r["backlog_old"]["recommended"] == R.ARCHIVE and r["backlog_old"]["admission_reason"] == "not_opted_in"
    assert r["ready_not_opted"]["recommended"] == R.ARCHIVE and r["ready_not_opted"]["admissible"] is False
    opted = r["ready_opted"]
    assert opted["admissible"] is True and opted["bucket"] == R.B_SYSTEM and opted["blocker_class"] == "AGENTS_PAUSED"
    assert "unpauses" in opted["precondition"]
    assert r["waiting_external"]["blocker_class"] == "NO_AUTOMATIC_RELEASE"
    assert r["approval"]["recommended"] == R.FOUNDER_APPROVAL and "APR-0007" in r["approval"]["operation"]
    assert r["runtime_error"]["blocker_class"] == "RUNTIME_ERROR" and r["runtime_error"]["bucket"] == R.B_DECISION
    assert r["founder_note"]["blocker_class"] == "FOUNDER_NOTE"
    assert r["escalated"]["blocker_class"] == "ESCALATED" and r["escalated"]["recommended"] == R.FOUNDER_REVIEW
    assert r["review_output"]["recommended"] == R.FOUNDER_REVIEW and r["review_output"]["bucket"] == R.B_CLICK
    assert r["failed_exhausted"]["recommended"] == R.RETRY_EXHAUSTED and r["failed_exhausted"]["retry"] == "1/1"
    assert r["failed_retries_left"]["recommended"] == R.FOUNDER_DECISION
    assert r["stale_claim"]["recommended"] == R.STALE_CLAIM and r["stale_claim"]["actor"] == "system"


def test_merge_stuck(inventory):
    """TASK-9055's class: the real gates refuse READY (published PR/recovery owns it) and DONE (no recorded founder
    merge at IMPLEMENTING), and merge recovery only covers PR_OPEN/CI/READY_TO_MERGE. Only cancel remains."""
    _, r = inventory
    row = r["merge_before_ci"]
    assert row["blocker_class"] == "MERGE_NOT_RECORDED" and row["recommended"] == R.CANCEL_ONLY
    assert row["bucket"] == R.B_STUCK and any("DONE refused" in e for e in row["blocker_evidence"])


def test_capacity_flag(inventory):
    _, r = inventory
    row = r["capacity"]
    assert row["recommended"] == R.AUTO_RESUMABLE and row["bucket"] == R.B_SYSTEM
    assert "founder GO" in row["precondition"] and "ENABLE_BLOCKED_AUTO_RESUME" in row["precondition"]


def test_locks(inventory):
    report, _ = inventory
    locks = {lk["key"]: lk for lk in report["locks"]}
    for key in ("lock:repo:example/retired-os", "lock:repo:example-owner/retired-app"):
        assert locks[key]["recommended"] == R.STALE_LOCK and locks[key]["blocks"] == "nothing"
    current = locks[stage_c.lease_key(stage_c_worker.REPOSITORY)]
    assert current["recommended"] == R.STALE_LOCK and current["actor"] == "system"   # live holder, no open run


def test_dirty():
    db = _db()
    _task(db, "TASK-9001")
    db.get(models.Task, 1).title = "changed in the caller's session"
    with pytest.raises(RuntimeError):
        R.build(db, specs=registry.load())
    db.expire_all()
    assert db.get(models.Task, 1).title == "probe"      # rolled back, never committed


# ----------------------------------------------------------------------------------- pure rules (offline facts)
def test_unknown():
    f = R.Facts(key="TASK-9074", status="READY", autonomous=True)
    row = R.classify(f, R.System(paused=True))
    assert row["admissible"] is None and row["bucket"] == R.B_DECISION and row["blocker_class"] == "ADMISSION_UNVERIFIED"


def test_no_proof():
    f = R.Facts(key="TASK-9001", status="BLOCKED", error_class="LAUNCHER_REFUSED", resolution_evidence=None)
    row = R.classify(f, R.System(auto_resume_flag=True))
    assert row["recommended"] == R.KEEP_WAITING and row["blocker_evidence"] == ["no proof run yet"]


def test_flag_on():
    f = R.Facts(key="TASK-9001", status="BLOCKED", error_class="CODER_CAPACITY",
                resolution_evidence={"proof_run_id": 9, "proof_provider": "claude", "blocking_run_id": 3})
    row = R.classify(f, R.System(auto_resume_flag=True))
    assert row["precondition"].startswith("next tick") and "proof run 9" in row["blocker_evidence"][0]


def test_review_pr():
    assert R.classify(R.Facts(key="T-1", status="REVIEW", stage="CI"), R.System())["actor"] == "system"
    row = R.classify(R.Facts(key="T-1", status="REVIEW", stage="READY_TO_MERGE"), R.System())
    assert row["bucket"] == R.B_CLICK and "merge the PR" in row["operation"]


def test_merge_ok():
    f = R.Facts(key="T-1", status="BLOCKED", error_class="MERGE_NOT_RECORDED", merge_recoverable=True)
    assert R.classify(f, R.System())["bucket"] == R.B_SYSTEM


# ----------------------------------------------------------------------------------- the route
CALLERS = [("anonymous", {}, "172.18.0.5", 401), ("youtube", _bearer(YT), "172.18.0.5", 403),
           ("desk token", _bearer(DESK), "172.18.0.5", 403), ("admin", _bearer(ADMIN), "172.18.0.5", 403),
           ("local", {}, "127.0.0.1", 403), ("host", {}, "10.0.6.1", 403)]


def test_route_founder_only(env, monkeypatch):
    # One test, not a parametrized grid: the suite's PASSED-line byte budget (root conftest.py) is nearly spent.
    for mode in ("enforce", "log"):
        monkeypatch.setenv("API_AUTH_MODE", mode)
        for label, headers, host, code in CALLERS:
            r = _c(host).get("/desk/reconciliation", headers=headers)
            assert r.status_code == code, (label, mode, r.text)
            assert "tasks" not in r.text


def test_route_read(env):
    engine = env[0]
    with Session(engine) as s:
        t = s.scalar(select(models.Task).where(models.Task.key == "TASK-0045"))
        t.status, t.error, t.result = T.BLOCKED, NOTE, "private result text"
        s.commit()
        before = (s.scalar(select(func.count()).select_from(models.AuditLog)), t.revision)
    r = _founder().get("/desk/reconciliation", headers=ASSERT)
    assert r.status_code == 200, r.text
    body = r.json()
    [row] = [x for x in body["tasks"] if x["key"] == "TASK-0045"]
    assert row["blocker_class"] == "FOUNDER_NOTE"
    assert NOTE not in r.text and "private result text" not in r.text
    assert _founder().get("/desk/reconciliation", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 401
    with Session(engine) as s:
        t = s.scalar(select(models.Task).where(models.Task.key == "TASK-0045"))
        assert (s.scalar(select(func.count()).select_from(models.AuditLog)), t.revision) == before


# ----------------------------------------------------------------------------------- offline dry run
LOG = """# task inventory (read-only transaction) 2030-01-01T00:00:00Z
== open tasks (not DONE/CANCELLED)
  TASK-9017  BACKLOG          P1 MEDIUM retry=0 appr=T age=53d agent=developer auto=None write=None scoped=F handoff=F err=''
  TASK-9074  READY            P1 LOW    retry=0 appr=F age=32d agent=ceo auto=true write=None scoped=F handoff=F err=''
  TASK-9054  BLOCKED          P3 LOW    retry=1 appr=F age=42d agent=developer auto=true write=true scoped=T handoff=F err='RUNTIME_ERROR'
  TASK-9055  BLOCKED          P3 LOW    retry=0 appr=F age=34d agent=developer auto=true write=true scoped=T handoff=F err='Merge not recorded: synthetic example'
  TASK-9049  BLOCKED          P2 LOW    retry=0 appr=F age=44d agent=developer auto=true write=None scoped=F handoff=F err=''
  TASK-9027  REVIEW           P1 LOW    retry=0 appr=F age=52d agent=research auto=true write=false scoped=F handoff=F err=''
  TASK-9061  FAILED           P2 LOW    retry=1 appr=F age=39d agent=developer auto=true write=true scoped=T handoff=F err='CHECKS_FAILED'
  total open: 7
== agent runs still STARTED
== control state keys
  agents_paused                              2030-01-01 00:00
  lock:repo:example/retired-os                        2029-12-01 00:00
"""


def test_dryrun(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "dryrun", Path(__file__).resolve().parents[1] / "scripts" / "reconciliation_dryrun.py")
    dryrun = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dryrun)
    src, out = tmp_path / "inv.log", tmp_path / "r5" / "DRY.md"
    src.write_text(LOG)
    assert dryrun.main(str(src), str(out)) == 0
    md = out.read_text()
    row = {line.split("|")[1].strip(): line for line in md.splitlines() if line.startswith("| TASK-")}
    assert set(row) == {"TASK-9017", "TASK-9074", "TASK-9054", "TASK-9055", "TASK-9049", "TASK-9027", "TASK-9061"}
    assert "`archive-candidate`" in row["TASK-9017"] and "not_opted_in" in row["TASK-9017"]
    assert "ADMISSION_UNVERIFIED" in row["TASK-9074"] and "| None |" in row["TASK-9074"]
    assert "`stuck-cancel-only`" in row["TASK-9055"] and "agents are paused" in row["TASK-9054"]
    assert "hypothesis: ESCALATED" in row["TASK-9049"] and "`founder-review`" in row["TASK-9027"]
    assert "| Truly stuck (every forward path refused by code; only cancel) | 1 | TASK-9055 |" in md
    assert "`lock:repo:example/retired-os`" in md and "| nothing |" in md


def test_gate_flag(monkeypatch):
    """code_write_flag only replaces the flag for a read-only report; the worker's own gate keeps its environment."""
    from app import worker
    monkeypatch.setattr(worker, "ENABLE_AUTONOMOUS_CODE_WRITE", False)
    db = _db()
    task = write_task(db, key="TASK-0201")
    specs = registry.load()
    assert worker._admission_gate(db, task, specs)[0] == "code_write_disabled"
    assert worker._admission_gate(db, task, specs, code_write_flag=False)[0] == "code_write_disabled"
    assert worker._admission_gate(db, task, specs, code_write_flag=True)[0] != "code_write_disabled"
    assert worker._eligible(db, task, specs)[0] == "code_write_disabled"
