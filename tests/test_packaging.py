"""TASK-0023: every new YouTube package is reviewed before upload; repeats never reach a model."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app import enums, models, packaging, registry, worker
from tests.test_worker import FakeProvider, _good

HISTORY = [{"title": t, "thumbnailText": th} for t, th in [
    ("Essex Shipwreck: A Whale Sinks Your Ship", "90 DAYS"),
    ("Lizzie Borden Finds Her Father Dead", "INSIDE JOB?"),
    ("Stone Age Winter: The Reindeer Turn Away", "NO HARVEST"),
    ("Rome's Ninth Legion Marched Into Britain", "NO GRAVES"),
    ("You Are Aboard Flight 93", "NO ESCAPE"),
    ("You Are Inside the Towers", "NO WAY DOWN"),
]]


def _db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    db.add_all([models.Agent(key="research", role="r", enabled=True),
                models.BusinessUnit(key="youtube", name="YT", status=enums.BusinessUnitStatus.ACTIVE,
                                    priority=enums.Priority.P0)])
    db.commit()
    return db


def _tasks(db):
    return db.execute(select(func.count()).select_from(models.Task)).scalar_one()


def test_repeated_first_word_is_rejected_by_rule_without_a_model():
    db = _db()
    out = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "NO TIME LEFT", HISTORY)
    assert out["status"] == "REVISE" and "NO" in out["reasons"][0] and out["task"] is None
    # Break caught: an obvious repeat spending a subscription call.
    assert _tasks(db) == 0


def test_exact_repeat_and_title_template_are_caught():
    assert any("already used" in r for r in packaging.findings("Anything New", "90 DAYS", HISTORY))
    assert any("you are" in r for r in packaging.findings("You Are a Roman Soldier", "AMBUSH", HISTORY))


def test_new_package_opens_one_autonomous_review_task_per_version():
    db = _db()
    a = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "ICE AHEAD", HISTORY)
    b = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "ICE AHEAD", HISTORY)
    assert a["status"] == "PENDING" and a["task"] == b["task"] and _tasks(db) == 1
    task = db.execute(select(models.Task)).scalar_one()
    assert task.context["autonomous"] is True and task.risk_level == enums.RiskLevel.LOW
    assert "NO GRAVES" in task.description  # reviewer sees the history
    c = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "SIX WARNINGS", HISTORY)
    assert c["task"] != a["task"]  # a regenerated package gets its own review


def test_a_cancelled_review_is_final_not_pending_forever():
    # PENDING makes youtube-auto's gate wait (exit 75) and poll again in 15 min without spending an attempt; a
    # cancelled review used to report PENDING on every gate poll, so the job never ended.
    db = _db()
    a = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "ICE AHEAD", HISTORY)
    task = db.execute(select(models.Task)).scalar_one()
    task.status = enums.TaskStatus.CANCELLED
    db.commit()
    assert packaging.status(db, a["job"], a["hash"])["status"] == "FAILED"
    again = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "ICE AHEAD", HISTORY)
    assert again["status"] == "FAILED" and again["reasons"] == ["review cancelled"] and _tasks(db) == 1


def test_worker_review_reaches_the_publish_gate():
    db = _db()
    out = packaging.submit(db, "2026-10-01-titanic", "Titanic's Last Night", "ICE AHEAD", HISTORY)
    key = out["task"]
    approve = _good(key, summary="Distinct from recent packages.",
                    evidence=[packaging.APPROVE, "no first-word or template repeat in history"])
    worker.run_once(db, {"claude": FakeProvider("claude", [approve])}, registry.load())
    assert packaging.status(db, "2026-10-01-titanic", out["hash"])["status"] == "APPROVED"


def test_revise_verdict_carries_reasons_and_an_ambiguous_verdict_is_not_accepted():
    db = _db()
    out = packaging.submit(db, "job-a", "The Mine That Exploded", "CAUSE UNKNOWN", HISTORY)
    both = _good(out["task"], evidence=[packaging.APPROVE, packaging.REVISE])
    worker.run_once(db, {"claude": FakeProvider("claude", [both])}, registry.load())
    assert packaging.status(db, "job-a", out["hash"])["status"] == "PENDING"  # not accepted
    out2 = packaging.submit(db, "job-b", "A Different Story", "COLD FRONT", HISTORY)
    revise = _good(out2["task"], evidence=[packaging.REVISE, "too close to NO ESCAPE in tone",
                                           "alternatives: ICE WALL / LAST SIGNAL / SIX WARNINGS"])
    worker.run_once(db, {"claude": FakeProvider("claude", [revise])}, registry.load())
    st = packaging.status(db, "job-b", out2["hash"])
    assert st["status"] == "REVISE" and any("alternatives" in r for r in st["reasons"])


def test_package_hash_is_deterministic_and_sensitive_to_title_and_thumbnail():
    base = packaging.package_hash("A Title", "ICE AHEAD")
    assert base == packaging.package_hash("A Title", "ICE AHEAD")
    assert base != packaging.package_hash("Another Title", "ICE AHEAD")
    assert base != packaging.package_hash("A Title", "ICE BEHIND")


def test_findings_is_empty_for_empty_history():
    assert packaging.findings("A Fresh Title", "ICE AHEAD", []) == []


def test_first_word_seen_once_is_not_flagged():
    history = [{"title": "Something Else Entirely", "thumbnailText": "NO ESCAPE"},
               {"title": "Quiet Harbor Mystery", "thumbnailText": "LAST SIGNAL"}]
    assert packaging.findings("A Fresh Title", "NO TIME LEFT", history) == []


def test_only_the_history_window_counts():
    filler = [{"title": f"Filler{i} Story", "thumbnailText": f"WORD{i} HERE"}
              for i in range(packaging.HISTORY_WINDOW)]
    old_repeat = {"title": "Old Unique Title", "thumbnailText": "ICE AHEAD"}
    assert packaging.findings("A Fresh Title", "ICE AHEAD", filler + [old_repeat]) == []
    # Control: the same repeat inside the window is flagged.
    flagged = packaging.findings("A Fresh Title", "ICE AHEAD", filler[:-1] + [old_repeat])
    assert any("already used" in r for r in flagged)


def _title_history(*titles):
    return [{"title": t, "thumbnailText": f"UNIQUE{i} THUMB"} for i, t in enumerate(titles)]


def test_title_head_seen_in_one_recent_title_is_not_flagged():
    history = _title_history("Dark Harbor Mystery", "Something Else Entirely")
    assert packaging.findings("Dark Harbor Returns", "ICE AHEAD", history) == []


def test_title_head_seen_in_two_recent_titles_is_flagged():
    history = _title_history("Dark Harbor Mystery", "Something Else Entirely", "Dark Harbor Secrets")
    out = packaging.findings("Dark Harbor Returns", "ICE AHEAD", history)
    assert len(out) == 1 and "dark harbor" in out[0] and "2 recent titles" in out[0]


def test_title_head_repeat_outside_history_window_does_not_count():
    # History is newest first: the second match sits beyond the window, so only one counts.
    filler = _title_history(*[f"Filler{i} Story" for i in range(packaging.HISTORY_WINDOW - 1)])
    old = [{"title": "Dark Harbor Old", "thumbnailText": "OLD ONE"}]
    recent = [{"title": "Dark Harbor Mystery", "thumbnailText": "RECENT ONE"}]
    assert packaging.findings("Dark Harbor Returns", "ICE AHEAD", recent + filler + old) == []
    # Control: with the older match moved inside the window it is flagged.
    assert any("dark harbor" in r for r in packaging.findings(
        "Dark Harbor Returns", "ICE AHEAD", recent + old + filler))


def test_one_word_title_never_triggers_the_title_rule():
    history = _title_history("Titanic Story", "Titanic Secrets", "Titanic")
    assert packaging.findings("Titanic", "ICE AHEAD", history) == []


def test_unknown_package_is_unknown_not_approved():
    db = _db()
    assert packaging.status(db, "nope", "deadbeef")["status"] == "UNKNOWN"


def test_submit_endpoint_works_without_a_registered_service():
    from app import main, schemas
    db = _db()
    out = main.youtube_packaging_submit(schemas.PackagingIn(
        job="2026-10-01-x", title="A Fresh Title", thumbnailText="ICE AHEAD", history=HISTORY), db)
    assert out["status"] == "PENDING" and out["task"]


def test_empty_title_is_422_over_http_with_authenticated_remote_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    token = "y" * 40  # auth rejects configured tokens shorter than 32 characters
    monkeypatch.setenv("COMPANY_API_TOKEN_YOUTUBE", token)
    monkeypatch.setenv("API_AUTH_MODE", "enforce")
    engine = create_engine(f"sqlite:///{tmp_path / 'packaging.db'}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)

    def override_db():
        with Session(engine) as session:
            yield session

    previous = main.app.dependency_overrides.get(main.get_db)
    main.app.dependency_overrides[main.get_db] = override_db
    try:
        client = TestClient(main.app, client=("203.0.113.7", 50000))
        resp = client.post(
            "/webhooks/youtube-packaging",
            headers={"Authorization": f"Bearer {token}"},
            json={"job": "2026-10-01-x", "title": "", "thumbnailText": "ICE AHEAD", "history": HISTORY})
        assert resp.status_code == 422
        with Session(engine) as db:
            assert _tasks(db) == 0
    finally:
        if previous is None:
            main.app.dependency_overrides.pop(main.get_db, None)
        else:
            main.app.dependency_overrides[main.get_db] = previous
