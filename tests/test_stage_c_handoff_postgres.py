"""B5 handoff on real PostgreSQL: the report revision is serialized with the change it labels, and cancel and
activation of one import serialize without a deadlock. Breaks caught: a change committed after an unrelated one
keeps a revision an older projection already used (production then keeps the old state forever), two writers with
stale rows reuse a revision, or a cancel racing an activation leaves a runnable import.

Same disposable-database guard as the other PostgreSQL tests: CI fails rather than skips when its URL is missing,
and only the disposable test database is allowed."""
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import audit, control, enums, models, stage_c, stage_c_handoff as h

BODY = {"title": "Fix goal rounding", "objective": "Round progress once", "success_criteria": "Tests pass",
        "paths": ["app/goals.py", "tests/test_goals.py"], "context": []}


@pytest.fixture
def engine(monkeypatch):
    url = os.environ.get("EXTERNAL_RELEASE_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("CI requires the disposable PostgreSQL URL; skipping is forbidden")
        pytest.skip("disposable PostgreSQL URL not supplied")
    if make_url(url).database != "company_external_release_test":
        pytest.fail("Only the disposable external-release test database is allowed")
    from app import worker
    monkeypatch.setattr(worker, "worker_state", lambda db: {})
    engine = create_engine(url)
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([models.Project(key="company-os", name="Company OS", status=enums.ProjectStatus.ACTIVE),
                   models.Agent(key="developer", role="dev")])
        s.commit()
    yield engine
    models.Base.metadata.drop_all(engine)
    engine.dispose()


def _imported(engine, activate=True):
    now = datetime.now(timezone.utc)
    packet = {"version": 1, "id": str(uuid.uuid4()), "origin": "production", "origin_task_key": "TASK-0001",
              "repository": "seerxo/acthelm", "issued_at": now.isoformat(),
              "expires_at": (now + timedelta(hours=1)).isoformat(), "risk": "LOW", "kind": "code_change",
              "task": BODY}
    packet["payload_sha256"] = h.digest(packet)
    with Session(engine) as s:
        _, receipt = h.import_packet(s, packet["id"], packet)
        if activate:
            h.activate_import(s, packet["id"], packet["payload_sha256"])
    return packet["id"], packet["payload_sha256"], receipt["task_key"]


def _task(s, key, lock=False):
    stmt = select(models.Task).where(models.Task.key == key)
    return s.scalar(stmt.with_for_update() if lock else stmt)


def test_pg_seq(engine):
    """Codex finding: an uncommitted task change (audit id N) and an unrelated committed change (audit id N+1). The
    poll in between must not label the old state with a sequence the later state reuses."""
    hid, _, key = _imported(engine)
    with Session(engine) as a, Session(engine) as b:
        task = _task(a, key, lock=True)
        stage_c.advance(a, task, "DETECTED", "company-worker")
        a.flush()                                                     # in flight, not committed
        other = models.Task(key="TASK-0999", title="other", success_criteria="s")
        b.add(other)
        b.flush()
        # The review's reproduction: another task's pilot grant, audited (and committed) after the in-flight change.
        audit.record(b, actor="x", action="stage-c.authorization.issue", entity_type="control_state",
                     entity_key=control.PILOT_AUTHORIZATION, task_id=other.id)
        b.commit()
        with Session(engine) as c:
            old = h.snapshot(c, hid)
        a.commit()
    with Session(engine) as c:
        new = h.snapshot(c, hid)
    assert old["task"]["stage"] is None and new["task"]["stage"] == "DETECTED"
    assert new["seq"] > old["seq"]                                    # never UNCHANGED for a newer committed state


def test_pg_no_reuse(engine):
    """Two writers holding stale copies of the row: the database increments, so no revision is reused."""
    _, _, key = _imported(engine)
    with Session(engine) as x, Session(engine) as y:
        tx, ty = _task(x, key), _task(y, key)
        start = tx.revision
        ty.title = "changed by y"
        y.commit()
        tx.objective = "changed by x"                                 # x still holds the old revision in memory
        x.commit()
    with Session(engine) as s:
        assert _task(s, key).revision == start + 2


@pytest.mark.parametrize("first", ["cancel", "activate"])
def test_pg_cancel_activate(engine, first):
    """Cancel and activation of one import, released at once from behind a held task lock: no deadlock, and the
    result is never a runnable import (a cancel always wins, before or after activation)."""
    hid, sha, key = _imported(engine, activate=False)
    errors, start = [], threading.Barrier(3)

    def run(fn):
        with Session(engine) as s:
            start.wait()
            try:
                fn(s, hid, sha)
            except h.HandoffError as e:
                errors.append(e.code)
    with Session(engine) as holder:
        _task(holder, key, lock=True)
        order = [h.cancel_import, h.activate_import]
        threads = [threading.Thread(target=run, args=(fn,)) for fn in (order if first == "cancel" else order[::-1])]
        for t in threads:
            t.start()
        start.wait()
        holder.commit()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "cancel/activation deadlocked"
    assert set(errors) <= {"CANCELLED"}
    with Session(engine) as s:
        task = _task(s, key)
        assert task.status == enums.TaskStatus.CANCELLED
        assert h.grant_denial(s, task, {}) is not None
        assert s.get(models.ControlState, h.KEY_PREFIX + hid).value["cancelled_at"]


def _packet(issued):
    p = {"version": 1, "id": str(uuid.uuid4()), "origin": "production", "origin_task_key": "TASK-0001",
         "repository": "seerxo/acthelm", "issued_at": issued.isoformat(),
         "expires_at": (issued + timedelta(hours=1)).isoformat(), "risk": "LOW", "kind": "code_change",
         "task": BODY}
    p["payload_sha256"] = h.digest(p)
    return p


def test_pg_origin(engine, monkeypatch):
    """R4a-2 finding 4: two first imports of one production task with different ids, interleaved inside
    _retire_superseded (nothing committed yet to lock), both committed. One open import per production task."""
    now = datetime.now(timezone.utc)
    p1, p2 = _packet(now - timedelta(minutes=1)), _packet(now)
    a_in, b_done, out = threading.Event(), threading.Event(), {}
    real = h._retire_superseded

    def retire(db, origin, hid, at):
        real(db, origin, hid, at)
        if hid == p1["id"]:
            a_in.set()
            b_done.wait(2)                  # B would finish here unless it waits for A
    monkeypatch.setattr(h, "_retire_superseded", retire)

    def run(p, after=None):
        if after:
            after.wait(10)
        with Session(engine) as s:
            try:
                out[p["id"]] = h.import_packet(s, p["id"], p)[0]
            except h.HandoffError as e:
                s.rollback()
                out[p["id"]] = e.code
        if after:
            b_done.set()
    threads = [threading.Thread(target=run, args=(p1,)), threading.Thread(target=run, args=(p2, a_in))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    with Session(engine) as s:
        live = [t for t in s.scalars(select(models.Task)) if t.status != enums.TaskStatus.CANCELLED]
    assert out == {p1["id"]: 201, p2["id"]: 201}
    assert [t.context["remote_stage_c"]["id"] for t in live] == [p2["id"]]


def test_pg_grant_lock(engine):
    """R4a-2 finding 3: replacing a finished handoff grant locks the handoff task (as a claim does) before the grant.
    A native authorization waits for a held lock on that task, never deadlocks, and bumps its revision."""
    hid, sha, key = _imported(engine)
    fid, past = "founder:access:founder@example.invalid", datetime.now(timezone.utc) - timedelta(minutes=10)
    with Session(engine) as s:   # issued earlier, expired now: replaceable
        control.authorize_pilot(s, key, "c" * 40, founder=fid, handoff_id=hid, payload_sha256=sha,
                                ttl=timedelta(minutes=1), now=past)
        s.add(models.Task(key="TASK-0900", title="native", success_criteria="s"))
        s.commit()
        rev = _task(s, key).revision

    def native():
        with Session(engine) as s:
            control.authorize_pilot(s, "TASK-0900", "c" * 40, founder=fid)
    with Session(engine) as holder:
        _task(holder, key, lock=True)
        t = threading.Thread(target=native)
        t.start()
        t.join(1)
        assert t.is_alive()                  # waits on the handoff task's lock
        holder.commit()
    t.join(30)
    assert not t.is_alive()
    with Session(engine) as s:
        assert _task(s, key).revision == rev + 1
        assert s.get(models.ControlState, control.PILOT_AUTHORIZATION).value["task_key"] == "TASK-0900"
