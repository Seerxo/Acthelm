"""Generated Desk bundle (docs/security/DESK_BUNDLE_RELEASE.md): the reviewer reads desk/ sources, never the
minified app/static bundle, and every verifier refuses a payload whose app/static differs from the gateway's
rebuild of desk/ at that exact commit, for every signer. Real git repositories and the real host step."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import release_gateway as rg
from tests.test_release_host import hw, step, tmp_path  # noqa: F401  (fixtures)
from tests.test_release_gateway_service import World, sh

# Same guard as test_release_gateway_service (whose World this uses): the gateway checks image has no ssh-keygen.
pytestmark = pytest.mark.skipif(not (shutil.which("ssh-keygen") and shutil.which("git")), reason="needs git, ssh-keygen")


def capture(w):
    packets, ask = [], w.review
    w.review = lambda packet: packets.append(packet) or ask(packet)
    return packets


def desk_commit(w, src="v2\n", bundle=None, **extra):
    sha = w.commit("desk", **{"desk/src.txt": src, "app/static/desk.html": bundle or f"bundle:{src}", **extra})
    w.push()
    return sha


def founder_sign(w, draft):
    raw = rg.canonical(draft)
    inbox = w.tmp / "founder-inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "000007.json").write_bytes(raw)
    (inbox / "000007.sig").write_text(rg.sign(raw, w.keys["founder"][0], rg.NS_RELEASE))


def draft(w):
    return json.loads(next(w.cfg.founder_queue.iterdir()).read_text())


def test_desk_ok(hw):
    packets = capture(hw)
    sha = desk_commit(hw)
    assert hw.tick()["outcome"] == "FOUNDER_REQUIRED"
    diff = packets[0]["diff"]
    assert "bundle:" not in diff and "+v2" in diff and "desk/src.txt" in diff
    old, new = (hashlib.sha256(b).hexdigest() for b in (b"bundle:v1\n", b"bundle:v2\n"))
    assert f"# generated app/static/desk.html {old} -> {new}" in diff
    d = draft(hw)
    assert d["evidence"]["desk_build"]["files"] == {"app/static/desk.html": new}
    founder_sign(hw, d)
    out = step(hw)
    assert out["outcome"] == "APPLIED" and out["sha"] == sha


@pytest.mark.parametrize("signer", ["founder", "gateway"])
@pytest.mark.parametrize("bad", ["drop", "files", "tree", "sha", "fail", "nosig", "fsig", "edit"])
def test_bad_proof(hw, signer, bad):
    """Field cases are re-signed with the gateway key (its content checks still hold); nosig/fsig/edit keep the right
    content but no gateway signature over it: a founder signature on the manifest does not repair a forged proof."""
    desk_commit(hw)
    hw.tick()
    d = draft(hw)
    proof = d["evidence"]["desk_build"]
    assert rg.verify(rg.desk_proof_bytes(proof), proof["signature"], hw.keys["gateway"][1], rg.NS_DESK_BUILD)
    if bad == "drop":
        del d["evidence"]["desk_build"]
    elif bad == "nosig":
        del proof["signature"]
    elif bad == "fsig":
        proof["signature"] = rg.sign(rg.desk_proof_bytes(proof), hw.keys["founder"][0], rg.NS_DESK_BUILD)
    elif bad == "edit":
        proof["files"] = {"app/static/desk.html": "0" * 64}
    else:
        proof.update({"files": {"files": {"app/static/desk.html": "0" * 64}}, "tree": {"desk_tree": "5" * 40},
                      "sha": {"sha": "6" * 40}, "fail": {"passed": False}}[bad])
        proof["signature"] = rg.sign(rg.desk_proof_bytes(proof), hw.keys["gateway"][0], rg.NS_DESK_BUILD)
    d["approver"] = signer
    raw = rg.canonical(d)
    hw.cfg.outbox.mkdir(parents=True, exist_ok=True)
    (hw.cfg.outbox / "000007-x.json").write_bytes(raw)
    (hw.cfg.outbox / "000007-x.sig").write_text(rg.sign(raw, hw.keys[signer][0], rg.NS_RELEASE))
    out = step(hw)
    assert out["outcome"] == "REJECTED" and "desk/" in out["reason"] and hw.runs == []


def test_gateway_approve_checks_proof_signature(hw):
    """gateway_approve (the other verifier) refuses an unsigned proof too."""
    desk_commit(hw)
    hw.tick()
    d = draft(hw)
    del d["evidence"]["desk_build"]["signature"]
    policy = json.loads((hw.tmp / "policy.json").read_text())
    with pytest.raises(rg.Refused, match="desk/"):
        rg._check_desk_build(d, {"required": True, "files": {}, "desk_tree": "x"}, policy)


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/release-gateway-desk-build"


def run_script(*args, **env):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], **env})


@pytest.mark.parametrize("env", [{"SUDO_UID": "1000"}, {"SUDO_USER": "gateway"}], ids=["uid", "user"])
def test_script_seed_refused_under_sudo(tmp_path, env):
    (tmp_path / "desk").mkdir()
    (tmp_path / "desk/package-lock.json").write_text("{}")
    r = run_script("seed", str(tmp_path), **env)
    assert r.returncode == 2 and "refused under sudo" in r.stderr


def test_script_refuses_symlink(tmp_path):
    work = tmp_path / "gateway-desk-x"
    (work / "desk").mkdir(parents=True)
    (work / "app/static").mkdir(parents=True)
    (work / "desk/package-lock.json").write_text("{}")
    os.symlink("/etc/passwd", work / "desk/leak")
    r = run_script(str(work), SUDO_UID=str(os.getuid()))
    assert r.returncode == 2 and "symlink" in r.stderr


@pytest.mark.parametrize("case", ["bundle", "stale", "extra"])
def test_mismatch(hw, case):
    packets = capture(hw)
    if case == "bundle":
        desk_commit(hw, bundle="bundle:evil\n")
    elif case == "stale":                       # sources changed, bundle not rebuilt
        hw.commit("desk", **{"desk/src.txt": "v2\n"})
        hw.push()
    else:                                       # a file in app/static the build does not make
        desk_commit(hw, **{"app/static/x.js": "alert(1)\n"})
    out = hw.tick()
    assert out["outcome"] == "DESK_BUILD_MISMATCH" and packets == []


@pytest.mark.parametrize("src,outcome", [("broken\n", "CHECKS_FAILED"), ("link\n", "DESK_BUILD_MISMATCH")],
                         ids=["fail", "link"])
def test_build_bad(hw, src, outcome):
    packets = capture(hw)
    desk_commit(hw, src=src)
    out = hw.tick()
    assert out["outcome"] == outcome and "desk build" in out["detail"] and packets == []


def test_no_command(hw):
    hw.cfg.desk_build_command = None
    desk_commit(hw)
    out = hw.tick()
    assert out["outcome"] == "WAITING" and "desk_build_command" in out["reason"] and hw.check_calls == []


def test_unchanged(tmp_path):
    w = World(tmp_path)
    sha = w.commit("docs", **{"docs/HANDOFF.md": "new\n"})
    g = rg.generated_facts(w.work, w.base, sha)
    assert g["required"] is False
    assert rg.review_diff(w.work, w.base, sha, {}, g) == rg.git_release_diff(w.work, w.base, sha)


def test_symlink(tmp_path):
    w = World(tmp_path)
    os.symlink("../app/static/desk.html", w.work / "desk/link")
    sh(w.work, "git", "add", "-A")
    sh(w.work, "git", "commit", "-qm", "link")
    w.push()
    out = w.tick()
    assert out["outcome"] == "DESK_BUILD_MISMATCH" and "regular file" in out["detail"]


def test_rebase_scope():
    g = {"required": True, "files": {"app/static/a"}, "parent_files": {"app/static/b"}}
    scope = rg.review_scope({"chain_base": "1" * 40}, g)
    assert scope[:2] == ("--", rg.DEPLOY_PATHS[0]) and rg.DESK_DIR in scope
    assert scope[-2:] == (":(top,literal,exclude)app/static/a", ":(top,literal,exclude)app/static/b")
    assert rg.review_scope({}, g)[:2] == ("--", ":(top)")
