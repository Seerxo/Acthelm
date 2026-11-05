"""Stage C pre-push privacy gate (prompt-injection audit PI-e): the coder sees the private task text and the fork is
public, so publish() scans every commit new to the push target, and the PR text, before any push or PR create.
Synthetic data only; anything name- or email-shaped is assembled at runtime."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import enums, models, stage_c_runner, worker
from tests.test_stage_c_hardening import FORK, _checks, _coder, _providers
from tests.test_stage_c_worker import pipeline, write_task
from tests.test_worker import SPECS, _db

NAME = " ".join(["Ayla", "Kaya"])
EMAIL = "ayla.kaya" + chr(64) + "example.invalid"
RECORD = f"Customer {NAME}, email {EMAIL}, account 4711, renewal due before the quarterly invoice run"
OPS_NOTE = " ".join(["Ops note: the backup host rotates its keys every Tuesday at three",
                     "and the restore drill still fails on the staging replica"])
PASSED = {"passed": True, "summary": "1 passed in 0.1s", "passed_files": ["tests/test_metrics.py"]}
IDENT = ["-c", "user.name=test", "-c", "user.email=test@localhost"]


def _setup(monkeypatch, tmp_path, text, hooks=None):
    hooks = hooks if hooks is not None else {"opened": []}
    remote = pipeline(monkeypatch, tmp_path, hooks, publish_repository=FORK)
    def offline(*a, **k):
        raise AssertionError("privacy fixtures never call external services")
    monkeypatch.setattr(stage_c_runner.repo_guardian_github, "api", offline)
    _coder(monkeypatch, text)
    _checks(monkeypatch, PASSED)
    pushes, git = [], stage_c_runner._git
    def spy(repo, *args, **kwargs):
        if "push" in args:
            pushes.append(args)
        return git(repo, *args, **kwargs)
    monkeypatch.setattr(stage_c_runner, "_git", spy)
    return remote, hooks, pushes


def _remote_branches(remote):
    return subprocess.run(["git", "--git-dir", str(remote), "for-each-ref", "refs/heads"],
                          capture_output=True, text=True, check=True).stdout


def _assert_not_leaked(db, task, out, *values):
    run = db.query(models.AgentRun).filter_by(task_id=task.id).order_by(models.AgentRun.id.desc()).first()
    reasons = [a.reason or "" for a in db.query(models.AuditLog).filter_by(task_id=task.id)]
    surfaces = [task.error or "", run.error or "", json.dumps(run.tool_calls), json.dumps(out, default=str), *reasons]
    for value in values:
        for piece in (value, value.casefold()):
            assert not any(piece in s for s in surfaces), piece


def _assert_blocked(db, task, out, remote, hooks, pushes, code=stage_c_runner.PRIVACY_BLOCKED):
    assert out["verdict"] == code, out
    assert task.status == enums.TaskStatus.BLOCKED and task.error == code
    assert pushes == [] and hooks["opened"] == [] and _remote_branches(remote) == ""


def test_customer_record_copied_from_task_context_is_blocked(monkeypatch, tmp_path):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path,
                                   f'RECORD = "{RECORD}"\n\n\ndef test_metric():\n    assert RECORD\n')
    db = _db()
    task = write_task(db)
    task.context = {**task.context, "customer": {"record": RECORD}}
    db.commit()
    out = worker.run_once(db, _providers(), SPECS)
    _assert_blocked(db, task, out, remote, hooks, pushes)
    run = db.query(models.AgentRun).one()
    assert run.tool_calls["privacy"] == [{"category": "private_task_text", "path": "tests/test_metrics.py", "line": 1}]
    _assert_not_leaked(db, task, out, NAME, EMAIL, RECORD)


def test_private_operations_note_copied_verbatim_is_blocked(monkeypatch, tmp_path):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path,
                                   f"# {OPS_NOTE}\ndef test_metric():\n    assert 1 == 1\n")
    db = _db()
    task = write_task(db, description=OPS_NOTE)
    out = worker.run_once(db, _providers(), SPECS)
    _assert_blocked(db, task, out, remote, hooks, pushes)
    _assert_not_leaked(db, task, out, OPS_NOTE, "staging replica")


def test_private_context_copied_with_case_and_whitespace_changes_is_blocked(monkeypatch, tmp_path):
    words = OPS_NOTE.upper().split()
    wrapped = "\n".join("#   " + "    ".join(words[i:i + 3]) for i in range(0, len(words), 3))
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, f"{wrapped}\ndef test_metric():\n    assert 1 == 1\n")
    db = _db()
    task = write_task(db)
    task.context = {**task.context, "notes": [OPS_NOTE]}
    db.commit()
    out = worker.run_once(db, _providers(), SPECS)
    _assert_blocked(db, task, out, remote, hooks, pushes)
    _assert_not_leaked(db, task, out, OPS_NOTE, "STAGING")


SIGNATURE = "progress_ratio(goal_value, target_value, lower_bound, upper_bound, round_digits, clamp_mode, scale_factor)"


def test_legitimate_test_naming_the_objective_function_is_published(monkeypatch, tmp_path):
    test = f"def test_progress_ratio_handles_a_zero_target():\n    assert {SIGNATURE} == 0\n"
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, test)
    mirror = tmp_path / "jobs/.control/sez-company.git"
    (mirror / "tests/conftest.py").write_text(f"goal_value = target_value = lower_bound = upper_bound = 0\n"
                                              f"round_digits = clamp_mode = scale_factor = 0\n\n\n"
                                              f"def {SIGNATURE}:\n    return 0\n")
    subprocess.run(["git", "-C", str(mirror), "add", "."], check=True)
    subprocess.run(["git", "-C", str(mirror), *IDENT, "commit", "-qm", "helper"], check=True)
    db = _db()
    task = write_task(db, objective=f"Exercise {SIGNATURE}")
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == "PR_OPEN", out
    assert len(pushes) == 1 and len(hooks["opened"]) == 1
    # It passes because the overlap is public at base, not because nothing overlapped.
    private = stage_c_runner._private_index([task.objective])
    nothing_public = SimpleNamespace(exempt_shingles=lambda shingles: set(), exempt_window=lambda window: False)
    lines = list(enumerate(test.split("\n"), 1))
    assert stage_c_runner._overlaps("t.py", lines, private, nothing_public)
    assert stage_c_runner._overlaps("t.py", lines, private,
                                    stage_c_runner._Public(mirror, subprocess.run(
                                        ["git", "-C", str(mirror), "rev-parse", "main"], capture_output=True,
                                        text=True, check=True).stdout.strip())) == []


def test_scanner_exception_blocks_without_push(monkeypatch, tmp_path):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    def broken(task):
        raise RuntimeError(f"scanner crashed on {RECORD}")
    monkeypatch.setattr(stage_c_runner, "_private_texts", broken)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(), SPECS)
    _assert_blocked(db, task, out, remote, hooks, pushes, stage_c_runner.PRIVACY_ERROR)
    _assert_not_leaked(db, task, out, RECORD, NAME)


def test_recovery_publication_goes_through_the_same_gate(monkeypatch, tmp_path):
    note = " ".join(["rotate the staging replica keys", "before the quarterly restore drill on tuesday"])
    failing = {"on": True}
    def during_open_pr(db, task):
        if failing["on"]:
            raise RuntimeError("fork-only token cannot open the upstream PR")
    hooks = {"opened": [], "during_open_pr": during_open_pr}
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, f"# {note}\ndef test_metric():\n    assert 1 == 1\n", hooks)
    monkeypatch.setattr(stage_c_runner, "gh_pr_lookup",
                        lambda *a, **k: (lambda branch, commit: (_ for _ in ()).throw(RuntimeError("no PR"))))
    monkeypatch.setattr(stage_c_runner, "gh_pr_absent", lambda *a, **k: (lambda branch: True))
    db = _db()
    task = write_task(db)
    assert worker.run_once(db, _providers(), SPECS)["verdict"] == "PR_FAILED"
    assert len(pushes) == 1 and len(hooks["opened"]) == 1
    # The founder's later private note matches what the pushed branch holds: the recovery must not open its PR.
    failing["on"] = False
    task.status, task.error, task.description = enums.TaskStatus.READY, None, note.capitalize() + "."
    db.commit()
    gate = stage_c_runner._privacy_stop
    calls = []
    monkeypatch.setattr(stage_c_runner, "_privacy_stop", lambda *a, **k: calls.append(k) or gate(*a, **k))
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == stage_c_runner.PRIVACY_BLOCKED, out
    assert task.status == enums.TaskStatus.BLOCKED and task.error == stage_c_runner.PRIVACY_BLOCKED
    assert calls == [{}]   # the recovery call: no push target boundary, base..head is scanned whole
    assert len(pushes) == 1 and len(hooks["opened"]) == 1
    _assert_not_leaked(db, task, out, note)


@pytest.mark.parametrize("move", ["commit", "workspace", "index", "branch", "remote", "error"])
def test_commit_changed_after_the_scan_is_not_pushed(monkeypatch, tmp_path, move):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    gate = stage_c_runner._privacy_stop
    def scan_then_move(db, worktree, *args, **kwargs):
        result = gate(db, worktree, *args, **kwargs)
        assert result is None   # X scanned clean
        if move in ("commit", "workspace", "index"):
            Path(worktree, "tests/test_metrics.py").write_text(f"# {OPS_NOTE}\n")
        if move == "commit":
            _git(worktree, "commit", "-qa", "--amend", "-m", "Y")
        elif move == "index":
            _git(worktree, "add", "-A")
        elif move == "branch":
            _git(worktree, "update-ref", "refs/heads/task/TASK-0100", args[0])
        elif move == "remote":
            sha = _git(worktree, "rev-parse", "HEAD")
            _git(remote, "fetch", str(worktree), sha)
            _git(remote, "update-ref", "refs/heads/task/TASK-0100", sha)
        elif move == "error":
            monkeypatch.setattr(stage_c_runner, "_privacy_unchanged",
                                lambda *a: (_ for _ in ()).throw(RuntimeError(RECORD)))
        return result
    monkeypatch.setattr(stage_c_runner, "_privacy_stop", scan_then_move)
    db = _db()
    task = write_task(db, description=OPS_NOTE)
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == stage_c_runner.PRIVACY_ERROR, out
    assert task.status == enums.TaskStatus.BLOCKED and task.error == stage_c_runner.PRIVACY_ERROR
    assert pushes == [] and hooks["opened"] == []
    if move != "remote":
        assert _remote_branches(remote) == ""
    worktree = tmp_path / "jobs" / task.key / "worktrees" / task.key
    base = next(h["evidence"]["base"] for h in task.context["improvement"]["history"]
                if h["stage"] == "IMPLEMENTING")
    assert _git(worktree, "rev-parse", "HEAD") == base
    assert _git(worktree, "diff", "--cached", "--stat")
    _assert_not_leaked(db, task, out, OPS_NOTE, RECORD)


# --------------------------------------------------------------------------- scan boundary and unreviewable content

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *IDENT, *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(repo, files, message="c"):
    for path, data in files.items():
        target = Path(repo, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            target.unlink()
        else:
            target.write_bytes(data if isinstance(data, bytes) else data.encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    base = _commit(path, {"tests/test_metrics.py": "def test_metric():\n    assert 1 == 1\n"})
    return path, base


TASK = SimpleNamespace(key="TASK-0100", title="t", description=OPS_NOTE, objective="o", success_criteria="s",
                       context={"customer": RECORD})


def test_scan_covers_only_commits_new_to_the_push_target_even_when_the_tip_is_clean(repo, tmp_path):
    path, base = repo
    remote = tmp_path / "fork.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    a = _commit(path, {"a.py": f"# {OPS_NOTE}\n"}, "A")
    _git(path, "push", "-q", str(remote), f"{a}:refs/heads/task/TASK-0100")
    b = _commit(path, {"b.py": f"# {OPS_NOTE}\n"}, "B")
    c = _commit(path, {"b.py": "# clean\n", "a.py": "# clean\n"}, "C")   # the tip is clean
    findings, scanned = stage_c_runner._privacy_gate(path, base, c, TASK, {}, remote=str(remote), protocol="file")
    assert scanned == [b, c]
    assert findings == [{"category": "private_task_text", "path": "b.py", "line": 1}]
    # A PR (no push boundary) shows base..tip: A is scanned too.
    findings, scanned = stage_c_runner._privacy_gate(path, base, c, TASK, {})
    assert scanned == [a, b, c] and {f["path"] for f in findings} == {"a.py", "b.py"}


def test_unreadable_push_target_fails_closed(repo, tmp_path):
    path, base = repo
    head = _commit(path, {"x.py": "x = 1\n"})
    with pytest.raises(stage_c_runner.PrivacyGateError) as error:
        stage_c_runner._privacy_gate(path, base, head, TASK, {}, remote=str(tmp_path / "missing.git"), protocol="file")
    assert str(error.value) == "unreadable"
    out = stage_c_runner._privacy_stop(_db(), path, base, head, TASK, {},
                                       str(tmp_path / "missing.git"), "file", expected_remote="")
    assert out["outcome"] == stage_c_runner.PRIVACY_ERROR


def test_shallow_history_is_a_privacy_error_even_with_a_remote_conflict(repo):
    path, base = repo
    head = _commit(path, {"x.py": "x = 1\n"})
    _git(path, "update-ref", "refs/heads/task/TASK-0100", base)
    (path / ".git/shallow").write_text(base + "\n")
    out = stage_c_runner._privacy_stop(_db(), path, base, head, TASK, {}, str(path), "file", expected_remote="")
    assert out["outcome"] == stage_c_runner.PRIVACY_ERROR


def _submodule(path):
    _git(path, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/sub")
    _git(path, "commit", "-qm", "sub")


@pytest.mark.parametrize("change, category", [
    (lambda p: _commit(p, {"data.bin": b"\x00\x01\x02binary"}), "binary"),
    (lambda p: _commit(p, {"latin.py": b"name = 'caf\xe9'\n"}), "undecodable"),
    (lambda p: (os.symlink("tests/test_metrics.py", p / "link.py"), _commit(p, {})), "symlink"),
    (lambda p: (os.chmod(p / "tests/test_metrics.py", 0o755), _commit(p, {})), "mode_only"),
    (lambda p: _commit(p, {"tests/test_metrics.py": None}), "unreadable"),
    (_submodule, "submodule"),
    (lambda p: _commit(p, {"big.py": "x = 1\n" * (stage_c_runner.coder_diff.MAX_FILE_BYTES // 6 + 1)}), "oversize"),
])
def test_unreviewable_content_is_blocked(repo, change, category):
    path, base = repo
    change(path)
    findings, _ = stage_c_runner._privacy_gate(path, base, _git(path, "rev-parse", "HEAD"), TASK, {})
    assert category in {f["category"] for f in findings}, findings


def test_pr_text_is_scanned(repo):
    path, base = repo
    head = _commit(path, {"x.py": "x = 1\n"})
    findings, _ = stage_c_runner._privacy_gate(path, base, head, TASK, {"<pr body>": f"see {RECORD}"})
    assert findings == [{"category": "private_task_text", "path": "<pr body>", "line": 1}]
    assert stage_c_runner._privacy_gate(path, base, head, TASK, {"<pr title>": "TASK-0100: Stage C change"})[0] == []


@pytest.mark.parametrize("field", ["description", "objective", "success_criteria", "context"])
def test_all_private_task_fields_are_indexed(repo, field):
    path, base = repo
    value = {field: OPS_NOTE} if field != "context" else {field: {"private_note": OPS_NOTE}}
    task = SimpleNamespace(**{**vars(TASK), **value})
    head = _commit(path, {"note.py": f"# {OPS_NOTE}\n"})
    findings, _ = stage_c_runner._privacy_gate(path, base, head, task, {})
    assert {f["category"] for f in findings} == {"private_task_text"}


def test_exact_64_character_overlap_with_fewer_than_eight_words_is_blocked(repo):
    path, base = repo
    value = "-".join(["SyntheticCustomerReference", "QuarterlyRenewalReference", "InternalRecordReference"])
    assert len(value) >= 64 and len(stage_c_runner._WORD.findall(value)) < 8
    task = SimpleNamespace(**{**vars(TASK), "description": value})
    head = _commit(path, {"note.py": f"# {value[:64]}\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == [
        {"category": "private_task_text", "path": "note.py", "line": 1}]


def test_public_prose_is_not_an_identifier_exemption(repo):
    path, _ = repo
    base = _commit(path, {"README.md": OPS_NOTE})
    head = _commit(path, {"note.py": f"# {OPS_NOTE}\n"})
    assert stage_c_runner._privacy_gate(path, base, head, TASK, {})[0]


def test_private_edge_words_cannot_hide_around_a_public_identifier(repo):
    path, _ = repo
    base = _commit(path, {"helper.py": "public_identifier = 0\n"})
    value = " ".join(["PrivateCustomerReference" * 2, "public_identifier", "PrivateAccountReference" * 2])
    task = SimpleNamespace(**{**vars(TASK), "description": value})
    head = _commit(path, {"note.py": f"# {value}\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == [
        {"category": "private_task_text", "path": "note.py", "line": 1}]


def test_context_files_given_text_is_compared_without_exempting_its_prose(repo):
    path, _ = repo
    base = _commit(path, {"app/context.py": f"# {OPS_NOTE}\n"})
    task = SimpleNamespace(**{**vars(TASK), "description": "", "context": {
        "stage_c": {"context": ["app/context.py"]}}})
    head = _commit(path, {"note.py": f"# {OPS_NOTE}\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == []
    # The base file is public, but a founder supplying the same prose still makes it private task text.
    task.context["notes"] = OPS_NOTE
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == [
        {"category": "private_task_text", "path": "note.py", "line": 1}]


def test_plain_python_names_in_the_objective_are_allowed(repo):
    path, _ = repo
    signature = "calculate(rate, amount, duration, principal, divisor, precision, result)"
    base = _commit(path, {"helper.py": f"def {signature}:\n    return 0\n"})
    task = SimpleNamespace(**{**vars(TASK), "objective": "Exercise " + signature})
    head = _commit(path, {"test_helper.py": f"def test_calculate():\n    assert {signature} == 0\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == []


@pytest.mark.parametrize("sentence", [
    "Founder approved refund for pilot user after release gateway outage on production host",
    "secret release key is stored on backup host in root home directory with old password",
    "pilot user email is admin at company domain and password reset token expires tomorrow",
    "Founder note: customer account status is blocked until the payment provider confirms the refund for the pilot task",
])
def test_individual_base_names_never_exempt_private_sentences(repo, sentence):
    path, _ = repo
    words = stage_c_runner._WORD.findall(sentence.casefold())
    # Every word is a NAME in valid code, but their sequence occurs only in comments and a literal.
    code = "\n".join(f"class Holder{i}:\n    {word} = 0" for i, word in enumerate(words)
                     if word not in ("for", "is", "in", "and", "with", "not"))
    code += "\nif False and None is not None:\n    pass\nfor item in []:\n    pass\nwith open(__file__):\n    pass\n"
    base = _commit(path, {"names.py": f'{code}\n# {sentence}\nnote = "{sentence}"\nf_note = f"{sentence}"\n'})
    public = stage_c_runner._Public(path, base)
    public._load()
    assert set(words) <= public._identifiers
    task = SimpleNamespace(**{**vars(TASK), "description": sentence})
    head = _commit(path, {"note.py": f"# {sentence}\n"})
    findings, _ = stage_c_runner._privacy_gate(path, base, head, task, {})
    assert findings == [{"category": "private_task_text", "path": "note.py", "line": 1}]
    assert sentence not in json.dumps(findings)


def test_existing_base_function_signature_verbatim_is_allowed(repo):
    path, _ = repo
    signature = f"def {SIGNATURE}:"
    base = _commit(path, {"helper.py": signature + "\n    return 0\n"})
    task = SimpleNamespace(**{**vars(TASK), "objective": signature})
    head = _commit(path, {"copy.py": signature + "\n    return 1\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == []


@pytest.mark.parametrize("transform", [
    lambda text: "\u200b".join(text),
    lambda text: text.replace("e", "e\u00ad"),
    lambda text: "".join(chr(ord(c) + 0xFEE0) if "a" <= c <= "z" else c for c in text),
    lambda text: text.replace(" ", "_"),
    lambda text: "".join(word.capitalize() for word in text.split()),
    lambda text: "\n".join(f'x{i} = "{word}"' for i, word in enumerate(text.split())),
    lambda text: "\n".join(f'x{i} = f"{word}"' for i, word in enumerate(text.split())),
    lambda text: "\n".join(f'x{i} = b"{word}"' for i, word in enumerate(text.split())),
], ids=["zero-width", "soft-hyphen", "fullwidth", "underscores", "camel-case", "one-word-per-literal",
        "one-word-per-f-literal", "one-word-per-bytes-literal"])
def test_normalized_and_split_literal_private_text_is_blocked(repo, transform):
    path, base = repo
    note = "the founder needs a manual refund before the customer invoice is closed"
    task = SimpleNamespace(**{**vars(TASK), "description": note})
    head = _commit(path, {"note.py": transform(note) + "\n"})
    findings, _ = stage_c_runner._privacy_gate(path, base, head, task, {})
    assert findings and {f["category"] for f in findings} == {"private_task_text"}
    assert note not in json.dumps(findings)


def test_bytes_prose_is_decoded_as_utf8_with_replacement():
    assert stage_c_runner._prose_lines([(7, r'x = b"caf\xc3\xa9\xff"')]) == [(7, "caf\u00e9\ufffd")]


def test_normalization_is_applied_to_private_text_too(repo):
    path, base = repo
    task = SimpleNamespace(**{**vars(TASK), "description": "\u200b".join(OPS_NOTE)})
    head = _commit(path, {"note.py": "# " + OPS_NOTE + "\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0]


@pytest.mark.parametrize("generated", [
    {"title": OPS_NOTE},
    {"context": {"improvement": {"approval": {"findings": [OPS_NOTE]}}}},
    {"context": {"improvement": {"history": [{"evidence": {"failures": [OPS_NOTE]}}]}}},
])
def test_generated_fields_are_not_private_task_text(repo, generated):
    path, base = repo
    task = SimpleNamespace(**{**vars(TASK), "description": "", "context": {}, **generated})
    head = _commit(path, {"note.py": "# " + OPS_NOTE + "\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == []


def test_exact_character_windows_cannot_use_per_word_exemptions(repo):
    path, _ = repo
    value = "syntheticcustomerreference quarterlyrenewalreference internalrecordreference"
    base = _commit(path, {"names.py": "\n".join(f"{word} = 0" for word in value.split())})
    task = SimpleNamespace(**{**vars(TASK), "description": value})
    head = _commit(path, {"note.py": "# " + value[:64] + "\n"})
    assert stage_c_runner._privacy_gate(path, base, head, task, {})[0] == [
        {"category": "private_task_text", "path": "note.py", "line": 1}]


def test_submodule_is_blocked_even_when_local_git_config_hides_it(repo):
    path, base = repo
    _git(path, "config", "diff.ignoreSubmodules", "all")
    _submodule(path)
    findings, _ = stage_c_runner._privacy_gate(path, base, _git(path, "rev-parse", "HEAD"), TASK, {})
    assert {f["category"] for f in findings} == {"submodule"}


def test_copy_spanning_unchanged_and_added_lines_is_blocked(repo):
    path, _ = repo
    words = OPS_NOTE.split()
    base = _commit(path, {"note.py": "# " + " ".join(words[:7]) + "\n"})
    head = _commit(path, {"note.py": "# " + " ".join(words[:7]) + "\n# " + words[7] + "\n"})
    assert stage_c_runner._privacy_gate(path, base, head, TASK, {})[0] == [
        {"category": "private_task_text", "path": "note.py", "line": 1}]


@pytest.mark.parametrize("surface", ["blob", "message", "path"])
def test_credential_in_earlier_commit_is_blocked_even_after_deletion(repo, surface):
    path, base = repo
    canary = "".join(["github_", "pat_", "A1b2C3" * 6])
    filename = f"{canary}.py" if surface == "path" else "note.py"
    _commit(path, {filename: f"# {canary}\n" if surface == "blob" else "# clean\n"},
            canary if surface == "message" else "c")
    head = _commit(path, {filename: None})
    findings, commits = stage_c_runner._privacy_gate(path, base, head, TASK, {})
    assert len(commits) == 2 and "credential" in {f["category"] for f in findings}
    assert canary not in json.dumps(findings)


def test_private_path_is_blocked_without_echoing_it(repo):
    path, base = repo
    filename = OPS_NOTE + ".py"
    head = _commit(path, {filename: "# clean\n"})
    findings, _ = stage_c_runner._privacy_gate(path, base, head, TASK, {})
    assert findings == [{"category": "private_task_text", "path": "<private path>", "line": 0}]
    assert OPS_NOTE not in json.dumps(findings)


def test_unknown_remote_history_fails_closed(repo, tmp_path):
    path, base = repo
    head = _commit(path, {"note.py": "# clean\n"})
    remote = tmp_path / "fork"
    remote.mkdir()
    _git(remote, "init", "-q")
    other = _commit(remote, {"other.py": "# remote-only history\n"})
    _git(remote, "update-ref", "refs/heads/task/TASK-0100", other)
    with pytest.raises(stage_c_runner.PrivacyGateError, match="^unreadable$"):
        stage_c_runner._privacy_gate(path, base, head, TASK, {}, str(remote), "file")


@pytest.mark.parametrize("listing", ["garbage", "a" * 40 + "\trefs/heads/wrong", "a" * 40 + "\tx\na\ty"])
def test_malformed_remote_state_fails_closed(repo, monkeypatch, listing):
    path, base = repo
    head = _commit(path, {"note.py": "# clean\n"})
    monkeypatch.setattr(stage_c_runner, "_git", lambda *a, **k: listing)
    with pytest.raises(stage_c_runner.PrivacyGateError, match="^unreadable$"):
        stage_c_runner._privacy_gate(path, base, head, TASK, {}, "remote", "file")
    out = stage_c_runner._privacy_stop(_db(), path, base, head, TASK, {}, "remote", "file", expected_remote="")
    assert out["outcome"] == stage_c_runner.PRIVACY_ERROR


@pytest.mark.parametrize("failure", [RuntimeError, stage_c_runner.PrivacyGateError])
def test_gate_boundary_suppresses_arbitrary_scanner_errors(monkeypatch, tmp_path, failure):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    def broken(*a, **k):
        raise failure(RECORD)
    monkeypatch.setattr(stage_c_runner, "_privacy_gate", broken)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(), SPECS)
    _assert_blocked(db, task, out, remote, hooks, pushes, stage_c_runner.PRIVACY_ERROR)
    _assert_not_leaked(db, task, out, RECORD)


def test_unreadable_blob_blocks_with_fixed_error(repo, monkeypatch):
    path, base = repo
    head = _commit(path, {"note.py": "# clean\n"})
    git = stage_c_runner._git_bytes
    def unreadable(repo, *args, **kwargs):
        if args[:2] == ("cat-file", "blob"):
            raise OSError(RECORD)
        return git(repo, *args, **kwargs)
    monkeypatch.setattr(stage_c_runner, "_git_bytes", unreadable)
    out = stage_c_runner._privacy_stop(_db(), path, base, head, TASK, {})
    assert out["outcome"] == stage_c_runner.PRIVACY_ERROR
    assert out["privacy"] == [{"category": "unreadable", "path": "<gate>", "line": 0}]
    assert RECORD not in json.dumps(out)


@pytest.mark.parametrize("result", [(None, []), (False, []), ({}, []), ([], None),
                                   ([{"category": "unexpected", "path": "note.py", "line": 0}], [])])
def test_malformed_scanner_result_is_an_error(repo, monkeypatch, result):
    path, base = repo
    monkeypatch.setattr(stage_c_runner, "_privacy_gate", lambda *a, **k: result)
    out = stage_c_runner._privacy_stop(_db(), path, base, base, TASK, {})
    assert out["outcome"] == stage_c_runner.PRIVACY_ERROR


def test_publish_creates_one_commit_directly_on_base_and_pushes_its_sha(monkeypatch, tmp_path):
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    git = stage_c_runner._git
    def tag_before_push(repo, *args, **kwargs):
        if "push" in args:
            _git(repo, "config", "push.followTags", "true")
            _git(repo, "tag", "-a", "private-review-tag", "-m", "local only")
        return git(repo, *args, **kwargs)
    monkeypatch.setattr(stage_c_runner, "_git", tag_before_push)
    db = _db()
    task = write_task(db)
    out = worker.run_once(db, _providers(), SPECS)
    assert out["verdict"] == "PR_OPEN", out
    worktree = tmp_path / "jobs" / task.key / "worktrees" / task.key
    base = next(h["evidence"]["base"] for h in task.context["improvement"]["history"]
                if h["stage"] == "IMPLEMENTING")
    sha = task.artifacts["commit"]
    assert _git(worktree, "rev-list", "--parents", "-1", sha).split() == [sha, base]
    assert _git(worktree, "rev-list", "--count", f"{base}..{sha}") == "1"
    assert len(pushes) == 1 and pushes[0][-1] == f"{sha}:refs/heads/task/{task.key}"
    assert pushes[0][:4] == ("-c", "push.followTags=false", "push", "--no-verify")
    assert _git(worktree, "tag", "--list") == "private-review-tag"
    assert _git(remote, "for-each-ref", "refs/tags") == ""


def test_repair_findings_quoting_new_code_do_not_make_it_private(monkeypatch, tmp_path):
    from app import stage_c_ci
    from tests.test_stage_c_worker import Reviewer, _published_then_repair
    code = "def test_metric():\n    assert sum([1, 2, 3, 4, 5, 6, 7, 8]) == 36\n"
    class QuotingReviewer(Reviewer):
        def _run(self, prompt, schema, **kwargs):
            result = super()._run(prompt, schema, **kwargs)
            if schema == stage_c_runner.REVIEW_SCHEMA:
                result.output = {**result.output, "findings": [code]}
            return result
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    db = _db()
    task = write_task(db)
    previous = _published_then_repair(db, task, _providers(), monkeypatch)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *a, **k: {"state": "open", "merged": False})
    _coder(monkeypatch, code)
    out = worker.run_once(db, _providers(QuotingReviewer), SPECS)
    assert out["verdict"] == "PR_OPEN", out
    assert task.context["improvement"]["approval"]["findings"] == [code]
    assert len(pushes) == 2 and len(hooks["opened"]) == 1
    assert _git(remote, "rev-parse", f"refs/heads/task/{task.key}") != previous


def test_republish_repair_is_gated_before_replacing_the_remote(monkeypatch, tmp_path):
    from tests.test_stage_c_worker import _published_then_repair
    from app import stage_c_ci
    remote, hooks, pushes = _setup(monkeypatch, tmp_path, "def test_metric():\n    assert 1 == 1\n")
    db = _db()
    task = write_task(db)
    previous = _published_then_repair(db, task, _providers(), monkeypatch)
    monkeypatch.setattr(stage_c_ci, "gh_facts", lambda *a, **k: {"state": "open", "merged": False})
    task.description = OPS_NOTE
    db.commit()
    _coder(monkeypatch, f"# {OPS_NOTE}\ndef test_metric():\n    assert 1 == 2\n")
    out = worker.run_once(db, _providers(), SPECS)
    assert out.get("verdict") == stage_c_runner.PRIVACY_BLOCKED, out
    assert task.status == enums.TaskStatus.BLOCKED and task.error == stage_c_runner.PRIVACY_BLOCKED
    assert len(pushes) == 1 and len(hooks["opened"]) == 1
    assert _git(remote, "rev-parse", f"refs/heads/task/{task.key}") == previous
    _assert_not_leaked(db, task, out, OPS_NOTE)
