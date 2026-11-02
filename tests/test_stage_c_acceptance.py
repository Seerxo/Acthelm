"""Stage C host acceptance is the verified founder's (Cloudflare Access), from a fresh all-PASS candidate the host
recorded for the deployed SHA. Break caught: a row that only names "founder" (the old wizard wrote exactly that, and
anyone with database or host root access could), a founder id written without the endpoint's audit, a stale or
partial candidate, an acceptance for another SHA, or the route open to anyone but the verified founder."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app import control, models, stage_c_worker
from tests.test_founder_authorization import (ASSERT, CALLERS, FID, SHA, _c, _founder, env)  # noqa: F401 (fixture)
from tests.test_stage_c_worker import ACCEPTING_FOUNDER, _db, accept_host

ALL = dict.fromkeys(stage_c_worker.ACCEPTANCE_CHECKS, True)
S = "a" * 40


def test_the_real_path_is_accepted():
    db = _db()
    accept_host(db, S)
    assert control.acceptance_valid(db, S)
    row = db.get(models.ControlState, control.ACCEPTANCE)
    assert row.updated_by == ACCEPTING_FOUNDER and row.value["issued_by"] == ACCEPTING_FOUNDER


@pytest.mark.parametrize("updated_by", ["founder", "operator:host-root", "claude", ACCEPTING_FOUNDER])
def test_a_hand_written_row_is_never_acceptance(updated_by):
    """The old wizard wrote updated_by="founder"; a direct write with a real founder id has no endpoint audit."""
    db = _db()
    db.add(models.ControlState(key=control.ACCEPTANCE, updated_by=updated_by,
                               value={"source_sha": S, "checks": dict(ALL), "issued_by": updated_by}))
    db.commit()
    assert not control.acceptance_valid(db, S)


def test_acceptance_is_for_one_sha_and_tampering_breaks_it():
    db = _db()
    accept_host(db, S)
    assert not control.acceptance_valid(db, "b" * 40)
    row = db.get(models.ControlState, control.ACCEPTANCE)
    row.value = {**row.value, "checks": {**row.value["checks"], "github_blocked": True}, "source_sha": "b" * 40}
    db.commit()
    assert not control.acceptance_valid(db, "b" * 40)       # no endpoint audit for these values


@pytest.mark.parametrize("label", ["no candidate", "other sha", "a failed check", "stale", "not a founder"])
def test_the_founder_cannot_accept_a_bad_candidate(label):
    db = _db()
    now = datetime.now(timezone.utc)
    if label != "no candidate":
        checks = {**ALL, "egress": False} if label == "a failed check" else dict(ALL)
        recorded = now - timedelta(hours=3) if label == "stale" else now
        control.record_acceptance_candidate(db, "b" * 40 if label == "other sha" else S, checks, now=recorded)
    with pytest.raises(PermissionError if label == "not a founder" else ValueError):
        control.accept_host(db, S, founder="founder" if label == "not a founder" else ACCEPTING_FOUNDER, now=now)
    assert not control.acceptance_valid(db, S)


def test_only_the_operator_records_a_candidate_and_it_grants_nothing():
    db = _db()
    with pytest.raises(ValueError):
        control.record_acceptance_candidate(db, S, ALL, actor=ACCEPTING_FOUNDER)
    with pytest.raises(ValueError):
        control.record_acceptance_candidate(db, S, {**ALL, "extra": True})
    control.record_acceptance_candidate(db, S, ALL)
    assert not control.acceptance_valid(db, S)


# ------------------------------------------------------------------------------------------- the route
@pytest.mark.parametrize("label,headers,host", CALLERS)
def test_only_the_verified_founder_can_accept_over_http(env, label, headers, host):   # noqa: F811
    engine, _ = env
    with Session(engine) as s:
        control.record_acceptance_candidate(s, SHA, ALL)
    assert _c(host).post("/stage-c/acceptance", headers=headers).status_code in (401, 403), label
    with Session(engine) as s:
        assert not control.acceptance_valid(s, SHA)


def test_the_founder_accepts_the_candidate_for_the_deployed_sha(env):   # noqa: F811
    engine, _ = env
    assert _founder().post("/stage-c/acceptance", headers=ASSERT).status_code == 422      # no candidate yet
    with Session(engine) as s:
        control.record_acceptance_candidate(s, SHA, ALL)
    r = _founder().post("/stage-c/acceptance", headers=ASSERT)
    assert r.status_code == 201 and r.json()["source_sha"] == SHA and r.json()["issued_by"] == FID
    with Session(engine) as s:
        assert control.acceptance_valid(s, SHA)


def test_the_route_is_enforce_always_and_founder_scoped():
    from app import api_auth
    assert api_auth._match(api_auth.ENFORCE_ALWAYS, "POST", "/stage-c/acceptance")
    assert api_auth._match(api_auth.FOUNDER_SCOPE, "POST", "/stage-c/acceptance")
