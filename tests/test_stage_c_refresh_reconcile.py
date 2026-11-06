"""Founder-requested Stage C refresh reconciliation, using local upstream/fork Git history."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import (approval_identity, audit, coder_diff, enums, main, models, schemas, stage_c,
                 stage_c_ci, stage_c_runner, stage_c_worker)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def scene(tmp_path):
    upstream, fork, mirror = (tmp_path / name for name in ("upstream", "fork", "mirror"))
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "config", "user.name", "Founder")
    git(upstream, "config", "user.email", "founder@example.invalid")
    (upstream / "app").mkdir()
    (upstream / "app" / "a.py").write_text("x = 1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "base")
    base = git(upstream, "rev-parse", "HEAD")
    git(upstream, "clone", "-q", str(upstream), str(fork))
    git(fork, "config", "user.name", "Maintainer")
    git(fork, "config", "user.email", "maintainer@example.invalid")
    git(fork, "checkout", "-qb", "task/TASK-0001")
    (fork / "app" / "a.py").write_text("x = 2\n")
    git(fork, "add", ".")
    git(fork, "commit", "-qm", "feature")
    first = git(fork, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    (upstream / "app" / "b.py").write_text("b = 1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "main moved")
    git(fork, "fetch", "-q", "origin", "main")
    git(fork, "merge", "-qm", "refresh", "origin/main")
    final = git(fork, "rev-parse", "HEAD")
    git(fork, "push", "-q", "origin", "HEAD:refs/pull/1/head")
    git(upstream, "merge", "--squash", final)
    git(upstream, "commit", "-qm", "founder squash")
    merged = git(upstream, "rev-parse", "HEAD")
    git(upstream, "clone", "--bare", "-q", str(upstream), str(mirror))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    db = Session(engine)
    task = models.Task(key="TASK-0001", title="fix", success_criteria="green", priority=enums.Priority.P1)
    db.add(task)
    db.flush()
    fingerprint = stage_c.diff_fingerprint(coder_diff.render_range(fork, base, first))
    for stage, evidence in (("DETECTED", {}), ("PLANNED", {}), ("IMPLEMENTING", {}),
                            ("TESTING", {"coder": "claude"}),
                            ("INDEPENDENT_REVIEW", {"fingerprint": fingerprint})):
        stage_c.advance(db, task, stage, "stage-c-runner", evidence)
    approval = {"fingerprint": fingerprint, "review_stage_at": task.context["improvement"]["history"][-1]["at"],
                "reviewer": "codex", "coder": "claude"}
    review = audit.record(db, actor="codex", action="improvement.review_approved", entity_type="task",
                          entity_key=task.key, task_id=task.id, after=approval)
    db.flush()
    approval["audit_id"] = review.id
    task.context = {**task.context, "improvement": {**task.context["improvement"], "approval": approval}}
    stage_c.advance(db, task, "PR_OPEN", "stage-c-runner", {"branch": "task/TASK-0001", "commit": first,
        "fingerprint": fingerprint, "pr": "https://github.com/seerxo/acthelm/pull/1"})
    stage_c.advance(db, task, "CI", "stage-c-ci")
    stage_c.advance(db, task, "READY_TO_MERGE", "stage-c-ci", {"review_audit_id": review.id})
    task.status = enums.TaskStatus.BLOCKED
    task.error = "PR head moved after READY_TO_MERGE; the reviewed commit is no longer the head"
    block = audit.record(db, actor="stage-c-ci", action="improvement.blocked", entity_type="task",
                         entity_key=task.key, task_id=task.id, after={"head": final})
    db.flush()
    body = {"task_key": task.key, "pr_number": 1, "final_head": final, "merge_commit": merged}
    request = audit.record(db, actor="founder:access:founder@example.invalid",
                           action="stage-c.refresh_reconcile.requested", entity_type="task",
                           entity_key=task.key, task_id=task.id, after=body)
    db.commit()
    now = datetime.now(timezone.utc)
    facts = {"state": "closed", "merged": True, "head_sha": final, "head_ref": "task/TASK-0001",
             "head_repo": "seerxo/acthelm", "base_ref": "main", "draft": False,
             "merged_by": "semihbugrasezer", "merge_commit": merged,
             "merged_at": (now + timedelta(seconds=10)).isoformat(),
             "ci_completed_at": now.isoformat(), "checks_complete": True,
             "checks": [{"name": "python-tests", "status": "completed", "conclusion": "success",
                         "run_id": 1, "attempt": 1, "completed_at": now.isoformat()}],
             "statuses": {}, "statuses_complete": True, "status_problems": []}
    chain = stage_c_runner.refresh_chain(mirror, 1, first, final, merged,
                                          remote=str(upstream), pr_commits=[first, final])
    yield db, task, request, block, facts, chain, mirror, upstream, fork
    db.close()
    engine.dispose()


def judge(scene, *, apply=True):
    db, task, request, _, facts, chain, mirror, *_ = scene
    return stage_c_ci.reconcile_refreshed(db, task, request, facts, chain, mirror,
        repository="seerxo/acthelm", head_repository="seerxo/acthelm", required_statuses=(), apply=apply)


@pytest.fixture(params=["head", "merge"], ids=["head", "merge"])
def blocked_scene(scene, request):
    db, task, _, block, facts, *_ = scene
    if request.param == "merge":
        task.error = stage_c_ci.MERGE_NOT_RECORDED + "eligibility was not proven"
        block.after = {"merge_eligibility": "refused", "stage": "READY_TO_MERGE",
                       "pr_number": 1, "merge_commit": facts["merge_commit"],
                       "merged_by": facts["merged_by"]}
        db.commit()
    return scene


def assert_refused(scene, code):
    out = judge(scene, apply=False)
    assert out["outcome"] == "BLOCKED"
    assert any(reason.startswith(code) for reason in out["reasons"])


@pytest.mark.parametrize("case", ["actor", "before", "expired", "head", "number"],
                         ids=["actor", "before", "expired", "head", "number"])
def test_request_identity_refusals(blocked_scene, case, monkeypatch):
    scene = blocked_scene
    db, task, request, block, facts, *_ = scene
    if case == "actor":
        request.actor = "company-worker"
    elif case == "before":
        audit.record(db, actor="stage-c-ci", action="improvement.blocked", entity_type="task",
                     entity_key=task.key, task_id=task.id, after=block.after)
    elif case == "expired":
        request.occurred_at = datetime.now(timezone.utc) - timedelta(hours=25)
    elif case == "head":
        request.after = {**request.after, "final_head": "f" * 40}
    else:
        request.after = {**request.after, "pr_number": 2}
        monkeypatch.setattr(stage_c_ci, "pr_number", lambda _: 1)
        # Keep the URL cross-check independent of the request-number comparison.
        monkeypatch.setattr(stage_c_ci, "_evidence", lambda t, s: {
            **stage_c_ci.publication(t), "pr": "https://github.com/seerxo/acthelm/pull/2"}
            if s == "PR_OPEN" else {})
    db.flush()
    assert_refused(scene, "REQUEST_MISMATCH")


def test_review_must_predate_merge(blocked_scene):
    db, task, *_ = blocked_scene
    approval_id = task.context["improvement"]["approval"]["audit_id"]
    db.get(models.AuditLog, approval_id).occurred_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    db.flush()
    assert_refused(blocked_scene, "BINDING:review approval does not predate")


def test_body_head_matches_github(blocked_scene, monkeypatch):
    _, _, _, _, facts, *_ = blocked_scene
    facts["head_sha"] = "f" * 40
    monkeypatch.setattr(stage_c_ci, "_merge_problems", lambda *a: [])
    monkeypatch.setattr(stage_c_ci.merge_authorization, "binding_reasons", lambda **kw: [])
    assert_refused(blocked_scene, "REQUEST_MISMATCH")


def test_chain_identity_matches_request(blocked_scene, monkeypatch):
    _, _, _, _, _, chain, *_ = blocked_scene
    chain["published_head"] = "f" * 40
    monkeypatch.setattr(stage_c_ci.merge_authorization, "binding_reasons", lambda **kw: [])
    assert_refused(blocked_scene, "REQUEST_MISMATCH")


@pytest.mark.parametrize("case,code", [("tree", "NOT_UP_TO_DATE"), ("list", "EXTRA_COMMIT"),
                                      ("notmain", "MERGE_FACTS")], ids=["tree", "list", "notmain"])
def test_refresh_git_refusals(blocked_scene, case, code):
    _, _, _, _, facts, chain, mirror, upstream, _ = blocked_scene
    merged = facts["merge_commit"]
    commits = [chain["published_head"], facts["head_sha"]]
    if case == "tree":
        (upstream / "app" / "altered.py").write_text("different = True\n")
        git(upstream, "add", ".")
        altered = git(upstream, "write-tree")
        merged = git(upstream, "commit-tree", altered, "-p", f"{merged}^", "-m", "wrong squash tree")
        git(upstream, "update-ref", "refs/heads/main", merged)
    elif case == "list":
        commits = [facts["head_sha"], chain["published_head"]]
    else:
        merged = git(upstream, "commit-tree", git(upstream, "rev-parse", f"{merged}^{{tree}}"),
                     "-p", f"{merged}^", "-m", "unlanded squash")
    with pytest.raises(stage_c_runner.RefreshRefused, match=code):
        stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], facts["head_sha"], merged,
                                     remote=str(upstream), pr_commits=commits)


@pytest.mark.parametrize("rc,error", [(1, stage_c_runner.RefreshRefused), (129, RuntimeError)],
                         ids=["conflict", "git-error"])
def test_merge_tree_return_codes(blocked_scene, monkeypatch, rc, error):
    _, _, _, _, facts, chain, mirror, upstream, _ = blocked_scene
    original = stage_c_runner.subprocess.run

    def fail_merge_tree(args, **kwargs):
        if "merge-tree" in args:
            return SimpleNamespace(returncode=rc, stdout=b"", stderr=b"unsupported")
        return original(args, **kwargs)

    monkeypatch.setattr(stage_c_runner.subprocess, "run", fail_merge_tree)
    with pytest.raises(error, match="CONFLICT_RESOLVED" if rc == 1 else "merge-tree unavailable"):
        stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], facts["head_sha"],
                                     facts["merge_commit"], remote=str(upstream),
                                     pr_commits=[chain["published_head"], facts["head_sha"]])


def test_refresh_second_parent_must_be_on_main(blocked_scene, monkeypatch):
    _, _, _, _, facts, chain, mirror, upstream, fork = blocked_scene
    published = chain["published_head"]
    main_move = git(upstream, "rev-parse", f"{facts['merge_commit']}^")
    base = git(upstream, "rev-parse", f"{main_move}^")
    off_main = git(upstream, "commit-tree", git(upstream, "rev-parse", f"{main_move}^{{tree}}"),
                   "-p", base, "-m", "same tree off main")
    git(fork, "fetch", "-q", "origin", off_main)
    first_tree = git(fork, "merge-tree", "--write-tree", published, off_main)
    first_refresh = git(fork, "commit-tree", first_tree, "-p", published, "-p", off_main,
                        "-m", "off-main refresh")
    git(upstream, "update-ref", "refs/heads/main", main_move)
    git(upstream, "checkout", "-f", "main")
    (upstream / "app" / "later.py").write_text("later = 1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "later main")
    latest_main = git(upstream, "rev-parse", "HEAD")
    git(fork, "fetch", "-q", "origin", "main")
    final_tree = git(fork, "merge-tree", "--write-tree", first_refresh, latest_main)
    final = git(fork, "commit-tree", final_tree, "-p", first_refresh, "-p", latest_main,
                "-m", "good final refresh")
    git(fork, "push", "-q", "origin", f"{final}:refs/pull/1/head", "--force")
    merged = git(upstream, "commit-tree", final_tree, "-p", latest_main, "-m", "founder squash")
    git(upstream, "update-ref", "refs/heads/main", merged)
    original = stage_c_runner._git_bytes

    def branch_only(repo, *args, **kwargs):
        if args == ("rev-list", final, f"^{published}", f"^{latest_main}"):
            return f"{first_refresh}\n{final}\n".encode()
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(stage_c_runner, "_git_bytes", branch_only)
    with pytest.raises(stage_c_runner.RefreshRefused, match="EXTRA_COMMIT"):
        stage_c_runner.refresh_chain(mirror, 1, published, final, merged, remote=str(upstream),
                                     pr_commits=[published, first_refresh, final])


def test_clean_refresh_records_append_only_merge(scene):
    db, task, _, block, facts, chain, *_ = scene
    old = deepcopy(task.context["improvement"]["history"])
    assert judge(scene)["outcome"] == "MERGED"
    db.commit()
    assert task.status == enums.TaskStatus.REVIEW and task.error is None
    assert task.context["improvement"]["history"][:-1] == old
    row = db.scalar(select(models.AuditLog).where(models.AuditLog.action == "improvement.refresh_reconciled"))
    assert row.before["blocked_audit_id"] == block.id
    assert row.after["refreshed_heads"] == [facts["head_sha"]]
    assert row.after["fingerprint"] == task.context["improvement"]["approval"]["fingerprint"]


def test_merge_not_recorded_shape_is_reconciled(scene):
    db, task, request, _, facts, chain, mirror, *_ = scene
    task.error = stage_c_ci.MERGE_NOT_RECORDED + "eligibility was not proven"
    audit.record(db, actor="stage-c-ci", action="improvement.blocked", entity_type="task",
                 entity_key=task.key, task_id=task.id, after={"merge_eligibility": "refused"})
    db.flush()
    newer = audit.record(db, actor=request.actor, action=request.action, entity_type="task",
                         entity_key=task.key, task_id=task.id, after=request.after)
    db.flush()
    assert stage_c_ci.reconcile_refreshed(db, task, newer, facts, chain, mirror,
        repository="seerxo/acthelm", head_repository="seerxo/acthelm",
        required_statuses=(), apply=True)["outcome"] == "MERGED"


def test_two_refresh_merges_keep_all_heads(scene):
    db, task, request, _, facts, chain, mirror, upstream, fork = scene
    first_update = facts["head_sha"]
    base = git(upstream, "rev-parse", f"{facts['merge_commit']}^")
    git(upstream, "checkout", "-B", "main", base)
    (upstream / "app" / "later.py").write_text("later = 1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "main moved twice")
    git(fork, "fetch", "-q", "origin", "main")
    git(fork, "merge", "-qm", "second refresh", "origin/main")
    final = git(fork, "rev-parse", "HEAD")
    git(fork, "push", "-q", "origin", "HEAD:refs/pull/1/head")
    git(upstream, "merge", "--squash", final)
    git(upstream, "commit", "-qm", "second squash")
    merged = git(upstream, "rev-parse", "HEAD")
    request.after = {**request.after, "final_head": final, "merge_commit": merged}
    facts.update(head_sha=final, merge_commit=merged)
    chain = stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], final, merged,
        remote=str(upstream), pr_commits=[chain["published_head"], first_update, final])
    assert chain["refreshed_heads"] == [first_update, final]
    assert stage_c_ci.reconcile_refreshed(db, task, request, facts, chain, mirror,
        repository="seerxo/acthelm", head_repository="seerxo/acthelm",
        required_statuses=(), apply=True)["outcome"] == "MERGED"


def test_worker_request_is_idempotent(scene, monkeypatch):
    db, task, request, _, facts, chain, mirror, upstream, *_ = scene
    from app import repo_guardian_github
    monkeypatch.setenv("STAGE_C_REFRESH_RECONCILE", "apply")
    monkeypatch.setattr(repo_guardian_github, "load_policy", lambda path: {"workflows": ["ci"]})
    monkeypatch.setattr(repo_guardian_github, "merge_statuses", lambda policy: ({}, ()))
    monkeypatch.setattr(repo_guardian_github, "publish_repository", lambda policy, repo: repo)
    monkeypatch.setattr(stage_c_worker, "mirror_path", lambda: mirror)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *args, **kwargs: facts)
    monkeypatch.setattr(stage_c_runner, "refresh_chain", lambda *args, **kwargs: chain)
    assert stage_c_worker.reconcile(db) == [{"task": task.key, "outcome": "MERGED"}]
    assert db.scalar(select(models.AuditLog).where(models.AuditLog.action == "improvement.refresh_reconciled"))
    assert stage_c_worker.reconcile(db) != [{"task": task.key, "outcome": "MERGED"}]
    assert len(db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "improvement.refresh_reconciled")).all()) == 1


def test_no_founder_request_does_not_reconcile(scene):
    _, source, *_ = scene
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    with Session(engine) as db:
        task = models.Task(key=source.key, title=source.title, success_criteria=source.success_criteria,
                           priority=source.priority, status=enums.TaskStatus.BLOCKED,
                           error=source.error, context=source.context)
        db.add(task)
        db.commit()
        assert stage_c_worker.reconcile(db) == []
        assert task.status == enums.TaskStatus.BLOCKED
        assert task.context["improvement"]["stage"] == "READY_TO_MERGE"
    engine.dispose()


@pytest.mark.parametrize("mode,expected", [("apply", "BLOCKED"), ("dry-run", "DRY_RUN")],
                         ids=["refused", "dry-run"])
def test_worker_refusal_or_dry_run_never_changes_block(scene, monkeypatch, mode, expected):
    db, task, request, _, facts, chain, mirror, *_ = scene
    from app import repo_guardian_github
    monkeypatch.setenv("STAGE_C_REFRESH_RECONCILE", mode)
    monkeypatch.setattr(repo_guardian_github, "load_policy", lambda path: {"workflows": ["ci"]})
    monkeypatch.setattr(repo_guardian_github, "merge_statuses", lambda policy: ({}, ()))
    monkeypatch.setattr(repo_guardian_github, "publish_repository", lambda policy, repo: repo)
    monkeypatch.setattr(stage_c_worker, "mirror_path", lambda: mirror)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *args, **kwargs: facts)
    if mode == "apply":
        monkeypatch.setattr(stage_c_runner, "refresh_chain", lambda *args, **kwargs:
                            (_ for _ in ()).throw(stage_c_runner.RefreshRefused("EXTRA_COMMIT")))
    else:
        monkeypatch.setattr(stage_c_runner, "refresh_chain", lambda *args, **kwargs: chain)
    before = db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc()))
    assert stage_c_worker.reconcile(db)[0]["outcome"] == expected
    assert task.status == enums.TaskStatus.BLOCKED
    assert task.context["improvement"]["stage"] == "READY_TO_MERGE"
    after = db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc()))
    if mode == "apply":
        row = db.get(models.AuditLog, after)
        assert row.after == {"request_audit_id": request.id, "reasons": ["EXTRA_COMMIT"]}
    else:
        row = db.get(models.AuditLog, after)
        assert after > before
        assert row.action == "stage-c.refresh_reconcile.dry_run"
        assert row.after == {"request_audit_id": request.id, "code": "PASS"}


def worker_facts(monkeypatch, scene, *, mode, chain=None):
    from app import repo_guardian_github
    _, _, _, _, facts, original_chain, mirror, *_ = scene
    monkeypatch.setenv("STAGE_C_REFRESH_RECONCILE", mode)
    monkeypatch.setattr(repo_guardian_github, "load_policy", lambda path: {"workflows": ["ci"]})
    monkeypatch.setattr(repo_guardian_github, "merge_statuses", lambda policy: ({}, ()))
    monkeypatch.setattr(repo_guardian_github, "publish_repository", lambda policy, repo: repo)
    monkeypatch.setattr(stage_c_worker, "mirror_path", lambda: mirror)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *args, **kwargs: facts)
    monkeypatch.setattr(stage_c_runner, "refresh_chain", chain or (lambda *args, **kwargs: original_chain))


@pytest.mark.parametrize("mode", ["off", "expired"], ids=["off", "expired"])
def test_pending_request_falls_to_normal_merge_recovery(blocked_scene, monkeypatch, mode):
    db, task, request, *_ = blocked_scene
    if mode == "expired":
        request.occurred_at = datetime.now(timezone.utc) - timedelta(hours=25)
    worker_facts(monkeypatch, blocked_scene, mode="off" if mode == "off" else "dry-run",
                 chain=lambda *args, **kwargs: pytest.fail("refresh must not run"))
    monkeypatch.setattr(stage_c_worker, "admission", lambda *args: None)
    calls = []
    monkeypatch.setattr(stage_c_ci, "ingest", lambda *args, **kwargs: calls.append(1) or {"outcome": "WAITING"})
    out = stage_c_worker.reconcile(db)
    if task.error.startswith(stage_c_ci.MERGE_NOT_RECORDED):
        assert calls == [1]
        assert out == [{"task": task.key, "outcome": "WAITING"}]
    else:
        assert calls == [] and out == []


def test_dry_run_is_audited_once_per_request_and_recovers_normally(blocked_scene, monkeypatch):
    db, task, request, *_ = blocked_scene
    calls = []
    worker_facts(monkeypatch, blocked_scene, mode="dry-run",
                 chain=lambda *args, **kwargs: calls.append(1) or blocked_scene[5])
    monkeypatch.setattr(stage_c_worker, "admission", lambda *args: None)
    normal = []
    monkeypatch.setattr(stage_c_ci, "ingest", lambda *args, **kwargs: normal.append(1) or {"outcome": "WAITING"})
    first = stage_c_worker.reconcile(db)
    second = stage_c_worker.reconcile(db)
    assert calls == [1]
    rows = db.scalars(select(models.AuditLog).where(
        models.AuditLog.action == "stage-c.refresh_reconcile.dry_run")).all()
    assert len(rows) == 1 and rows[0].after == {"request_audit_id": request.id, "code": "PASS"}
    assert not db.scalar(select(models.AuditLog).where(
        models.AuditLog.action.in_(("improvement.refresh_reconciled", "improvement.refresh_reconcile_refused"))))
    assert first[0]["outcome"] == "DRY_RUN"
    assert all(item["outcome"] != "DRY_RUN" for item in second)
    if task.error.startswith(stage_c_ci.MERGE_NOT_RECORDED):
        assert normal == [1, 1]


@pytest.mark.parametrize("error,code", [(stage_c_runner.RefreshRefused("EXTRA_COMMIT"), "EXTRA_COMMIT"),
                                         (RuntimeError("git failed"), "EVIDENCE_UNAVAILABLE")],
                         ids=["refuse", "git-error"])
def test_dry_run_records_refusal_code(blocked_scene, monkeypatch, error, code):
    db, task, request, *_ = blocked_scene
    worker_facts(monkeypatch, blocked_scene, mode="dry-run",
                 chain=lambda *args, **kwargs: (_ for _ in ()).throw(error))
    assert stage_c_worker.reconcile(db)[0]["reasons"] == [code]
    row = db.scalar(select(models.AuditLog).where(
        models.AuditLog.action == "stage-c.refresh_reconcile.dry_run"))
    assert row.after == {"request_audit_id": request.id, "code": code}
    assert task.status == enums.TaskStatus.BLOCKED


def test_old_git_refuses_without_cloning(blocked_scene, monkeypatch):
    db, task, *_ = blocked_scene
    worker_facts(monkeypatch, blocked_scene, mode="apply",
                 chain=lambda *args, **kwargs: pytest.fail("refresh must not run"))
    original = stage_c_worker.subprocess.run
    monkeypatch.setattr(stage_c_worker.subprocess, "run", lambda args, **kw:
                        SimpleNamespace(stdout="git version 2.37.9", returncode=0)
                        if args == ["git", "--version"] else original(args, **kw))
    assert stage_c_worker.reconcile(db)[0]["reasons"] == ["EVIDENCE_UNAVAILABLE"]
    assert task.status == enums.TaskStatus.BLOCKED


@pytest.mark.parametrize("case,code", [
    ("extra", "EXTRA_COMMIT"), ("conflict", "CONFLICT_RESOLVED"),
    ("stale", "NOT_UP_TO_DATE"), ("ci", "CI_NOT_GREEN"),
    ("late", "CI_AFTER_MERGE"), ("merger", "NOT_FOUNDER_MERGE"),
    ("fingerprint", "FINGERPRINT_MISMATCH"), ("approval", "BINDING:"),
], ids=["extra", "conflict", "stale", "ci", "late", "merger", "fingerprint", "approval"])
def test_refusals_keep_blocked(scene, case, code):
    db, task, request, _, facts, chain, mirror, upstream, fork = scene
    if case in ("extra", "conflict", "stale"):
        if case == "extra":
            (fork / "app" / "extra.py").write_text("extra = 1\n")
            git(fork, "add", ".")
            git(fork, "commit", "-qm", "extra")
            new = git(fork, "rev-parse", "HEAD")
            git(fork, "push", "-q", "origin", "HEAD:refs/pull/1/head")
            with pytest.raises(stage_c_runner.RefreshRefused, match=code):
                stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], new,
                    facts["merge_commit"], remote=str(upstream), pr_commits=[chain["published_head"], facts["head_sha"], new])
        elif case == "stale":
            (upstream / "app" / "later.py").write_text("later = 1\n")
            git(upstream, "add", ".")
            git(upstream, "commit", "-qm", "later")
            new_base = git(upstream, "rev-parse", "HEAD")
            new_merge = git(upstream, "commit-tree", git(fork, "rev-parse", f"{facts['head_sha']}^{{tree}}"),
                            "-p", new_base, "-m", "later squash")
            git(upstream, "update-ref", "refs/heads/main", new_merge)
            with pytest.raises(stage_c_runner.RefreshRefused, match=code):
                stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], facts["head_sha"],
                    new_merge, remote=str(upstream),
                    pr_commits=[chain["published_head"], facts["head_sha"]])
        else:
            # A hand-resolved tree cannot be reconstructed by merge-tree.
            (fork / "app" / "a.py").write_text("x = 3\n")
            git(fork, "add", ".")
            git(fork, "commit", "-qm", "altered tree")
            new = git(fork, "commit-tree", git(fork, "rev-parse", "HEAD^{tree}"),
                      "-p", chain["published_head"], "-p", chain["fingerprint_base"], "-m", "resolved by hand")
            git(fork, "push", "-q", "origin", f"{new}:refs/pull/1/head", "--force")
            with pytest.raises(stage_c_runner.RefreshRefused, match=code):
                stage_c_runner.refresh_chain(mirror, 1, chain["published_head"], new,
                    facts["merge_commit"], remote=str(upstream), pr_commits=[chain["published_head"], new])
        return
    if case == "ci":
        facts["checks"][0]["conclusion"] = "failure"
    elif case == "late":
        facts["ci_completed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    elif case == "merger":
        facts["merged_by"] = "seerxo-agent"
    elif case == "fingerprint":
        imp = deepcopy(task.context["improvement"])
        imp["approval"]["fingerprint"] = "0" * 64
        for item in imp["history"]:
            if item["stage"] in ("INDEPENDENT_REVIEW", "PR_OPEN"):
                item["evidence"]["fingerprint"] = "0" * 64
        db.get(models.AuditLog, imp["approval"]["audit_id"]).after = {
            **db.get(models.AuditLog, imp["approval"]["audit_id"]).after, "fingerprint": "0" * 64}
        task.context = {**task.context, "improvement": imp}
    else:
        task.context = {**task.context, "improvement": {**task.context["improvement"],
            "approval": {**task.context["improvement"]["approval"], "reviewer": "claude"}}}
    old = deepcopy(task.context["improvement"]["history"])
    out = judge(scene)
    assert any(reason.startswith(code) for reason in out["reasons"])
    assert task.status == enums.TaskStatus.BLOCKED and task.context["improvement"]["history"] == old
    assert db.scalar(select(models.AuditLog).where(models.AuditLog.action == "improvement.refresh_reconcile_refused"))


def test_dry_run_writes_nothing(scene):
    db, task, *_ = scene
    before = db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc()))
    assert judge(scene, apply=False)["outcome"] == "DRY_RUN"
    db.flush()
    assert db.scalar(select(models.AuditLog.id).order_by(models.AuditLog.id.desc())) == before
    assert task.status == enums.TaskStatus.BLOCKED


def test_renderer_is_identical_to_import(scene, tmp_path):
    _, _, _, _, facts, chain, mirror, _, fork = scene
    base = tmp_path / "base"
    work = tmp_path / "work"
    from app import coder_source
    manifest = coder_source.export_source(fork, chain["fingerprint_base"], ["app/a.py"], base)
    coder_source.export_source(fork, chain["fingerprint_base"], ["app/a.py"], work)
    (work / "app" / "a.py").write_text("x = 2\n")
    assert coder_diff.render_range(mirror, chain["fingerprint_base"], facts["head_sha"]) == coder_diff.import_changes(base, work, manifest)["diff"]


def test_non_founder_request_is_403(scene, monkeypatch):
    db, task, request, *_ = scene
    monkeypatch.setenv("COMPANY_API_TOKEN_ADMIN", "a" * 40)
    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        client = TestClient(main.app)
        response = client.post("/stage-c/reconcile-refresh", json=request.after,
                               headers={"Authorization": "Bearer " + "a" * 40})
        assert response.status_code == 403
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


def test_founder_endpoint_audits_without_changing_task(scene):
    db, task, request, *_ = scene
    who = approval_identity.founder("founder@example.invalid")
    body = schemas.StageCRefreshReconcile(**request.after)
    result = main.stage_c_reconcile_refresh(body, SimpleNamespace(state=SimpleNamespace(principal=who)), db)
    assert result["outcome"] == "REQUESTED"
    row = db.get(models.AuditLog, result["audit_id"])
    assert row.actor == who.id and row.action == "stage-c.refresh_reconcile.requested"
    assert task.status == enums.TaskStatus.BLOCKED
