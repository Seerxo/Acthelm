"""write_authorization_denial reports the blocker of the authorization form that actually applies.
Break caught (seen live): a stale consumed one-task row masked the standing daily limit, so the worker logged
stage_c_authorization_invalid instead of stage_c_standing_daily_limit. Only the reason changed; allow/deny did not.

Precedence: with a standing row present, its reason wins unless a one-task row was issued for THIS task and is
invalid. With no standing row (never issued or revoked), the one-task reason stays. Within the standing form:
missing > invalid > expired > scope > daily_limit (the order of control._standing_denial)."""
from datetime import datetime, timedelta, timezone

from app import control, models
from tests.test_stage_c_worker import FOUNDER_ID, SHA_A, SHA_B, armed

PATH = "tests/test_metrics.py"


def old_write_authorization_denial(db, task, sha, now):
    """Reference copy of the function before the fix (014d460), for the allow/deny invariant."""
    pilot = control.pilot_authorization_denial(db, task.key, sha, now)
    if pilot is None:
        return None
    row = db.get(models.ControlState, control.STANDING_AUTHORIZATION)
    standing = control._standing_denial(db, row, control._task_paths(task), sha, now)
    if standing is None:
        return None
    return pilot if row is None or db.get(models.ControlState, control.PILOT_AUTHORIZATION) is not None else standing


def _clear(db):
    for key in (control.PILOT_AUTHORIZATION, control.STANDING_AUTHORIZATION):
        if (row := db.get(models.ControlState, key)) is not None:
            db.delete(row)
    db.commit()


def set_pilot(db, task, state, now):
    if state == "none":
        return
    if state == "expired":
        control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID, now=now - timedelta(hours=2))
        return
    control.authorize_pilot(db, task.key, SHA_B if state == "invalid_this_task" else SHA_A,
                            founder=FOUNDER_ID, now=now)
    if state == "consumed":
        assert control.consume_pilot_authorization(db, task.key, SHA_A, 99, now) is None
        db.commit()
    elif state == "other_task":
        row = db.get(models.ControlState, control.PILOT_AUTHORIZATION)
        row.value = {**row.value, "task_key": "TASK-9999"}
        db.commit()


def set_standing(db, state, now):
    if state == "none":
        return
    if state in ("expired", "expired_and_limit"):
        control.issue_standing(db, [PATH], SHA_A, founder=FOUNDER_ID, days=1, max_per_day=1,
                               now=now - timedelta(days=3))
    else:
        control.issue_standing(db, ["tests/test_goals.py"] if state == "scope" else [PATH],
                               SHA_B if state == "invalid" else SHA_A, founder=FOUNDER_ID, max_per_day=1, now=now)
    if state in ("limit", "expired_and_limit"):
        row = db.get(models.ControlState, control.STANDING_AUTHORIZATION)
        row.value = {**row.value, "used": {"day": now.date().isoformat(), "count": 1}}
        db.commit()
    elif state == "revoked":
        assert control.revoke_standing(db, actor=FOUNDER_ID)


def reason(db, task, pilot, standing, now):
    _clear(db)
    set_pilot(db, task, pilot, now)
    set_standing(db, standing, now)
    return control.write_authorization_denial(db, task, SHA_A, now)


def test_reported_reasons(monkeypatch, tmp_path):
    db, task, _ = armed(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    cases = [
        ("consumed", "limit", "stage_c_standing_daily_limit"),      # the live case
        ("none", "limit", "stage_c_standing_daily_limit"),
        ("other_task", "limit", "stage_c_standing_daily_limit"),
        ("expired", "limit", "stage_c_standing_daily_limit"),
        ("consumed", "expired", "stage_c_standing_expired"),
        ("none", "expired", "stage_c_standing_expired"),
        ("consumed", "scope", "stage_c_standing_scope"),
        ("consumed", "invalid", "stage_c_standing_invalid"),
        ("consumed", "expired_and_limit", "stage_c_standing_expired"),  # expired outranks the daily limit
        ("none", "revoked", "stage_c_authorization_missing"),        # revoke deletes the row: pilot wording
        ("consumed", "revoked", "stage_c_authorization_consumed"),
        ("expired", "none", "stage_c_authorization_expired"),
        ("invalid_this_task", "limit", "stage_c_authorization_invalid"),  # a broken grant for this very task
    ]
    for pilot, standing, want in cases:
        assert reason(db, task, pilot, standing, now) == want, (pilot, standing)


def test_allow_and_deny_are_unchanged_from_the_old_function(monkeypatch, tmp_path):
    db, task, _ = armed(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc)
    pilots = ("none", "valid", "consumed", "expired", "other_task", "invalid_this_task")
    standings = ("none", "valid", "limit", "expired", "scope", "invalid", "revoked", "expired_and_limit")
    for pilot in pilots:
        for standing in standings:
            new = reason(db, task, pilot, standing, now)
            old = old_write_authorization_denial(db, task, SHA_A, now)
            assert (new is None) == (old is None), (pilot, standing, old, new)
            if new is not None:  # only ever one of the two forms' own reasons
                pilot_reason = control.pilot_authorization_denial(db, task.key, SHA_A, now)
                standing_reason = control._standing_denial(
                    db, db.get(models.ControlState, control.STANDING_AUTHORIZATION),
                    control._task_paths(task), SHA_A, now)
                assert new in (pilot_reason, standing_reason), (pilot, standing, new)
    assert reason(db, task, "valid", "none", now) is None and reason(db, task, "none", "valid", now) is None
