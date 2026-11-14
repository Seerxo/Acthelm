"""Real-Docker integration test of scripts/release-gateway-desk-build (the sudo'd root wrapper), not a substitute.

Opt-in only: ACTHELM_DESK_BUILD_DOCKER_IT=1 on a disposable Linux host with passwordless sudo, Docker and setpriv
(the CI host). It installs the wrapper's fixed paths (/etc/release-gateway/desk-build-image,
/var/lib/release-gateway-desk), seeds the npm cache from this tree's desk/ lockfile as a real root session, runs the
wrapper through sudo exactly as the gateway does, and removes everything again. It refuses to run on a production
host (release ledger present) or over an existing desk-build setup. Standard library + pytest only (run with
--noconftest), so it needs no app dependencies."""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-gateway-desk-build"
IMAGE = "node:26.13.0@sha256:438603bbb5cda404b9416f0b5c8026f61501ab911760d861ec28c36b5d675725"
PIN_FILE, STATE = Path("/etc/release-gateway/desk-build-image"), Path("/var/lib/release-gateway-desk")
INSTALLED = Path("/usr/local/sbin/release-gateway-desk-build-it")
PRODUCTION_LEDGER = Path("/var/lib/release-host/ledger/ledger.json")

pytestmark = pytest.mark.skipif(os.environ.get("ACTHELM_DESK_BUILD_DOCKER_IT") != "1",
                                reason="real-Docker integration test: set ACTHELM_DESK_BUILD_DOCKER_IT=1 on the CI host")


def sudo(*args, check=True, **kw):
    return subprocess.run(["sudo", "-n", *args], check=check, capture_output=True, text=True, **kw)


def tree_files(base: Path) -> dict:
    return {p.relative_to(base).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(base.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def host():
    if PRODUCTION_LEDGER.exists():
        pytest.fail("refusing to run on a production host (release ledger present)")
    for tool in ("sudo", "docker", "setpriv"):
        if shutil.which(tool) is None:
            pytest.fail(f"opted in but {tool} is missing")
    if sudo("true", check=False).returncode != 0:
        pytest.fail("opted in but passwordless sudo is unavailable")
    # Checked as root (a 0700 root dir would hide its contents from us). Any existing /etc/release-gateway is a real
    # gateway's config: the test neither writes into it nor removes it, so it refuses to run at all.
    for path in (PIN_FILE.parent, STATE, INSTALLED):
        if sudo("test", "-e", str(path), check=False).returncode == 0 or path.is_symlink():
            pytest.fail(f"{path} already exists on this host: not touching it")
    created = []   # only what this fixture made, removed in reverse order
    pulled_id = None   # the image is removed only if this fixture pulled it; a pre-existing image is never touched
    try:
        if sudo("docker", "image", "inspect", IMAGE, check=False).returncode != 0:
            # The reference may be missing while its image is present (untagged): snapshot every local image id
            # first (check=True: fail closed) and own the image only if the pull created a new id.
            before = set(sudo("docker", "images", "-aq", "--no-trunc").stdout.split())
            sudo("docker", "pull", "-q", IMAGE, timeout=600)
            new_id = sudo("docker", "image", "inspect", "-f", "{{.Id}}", IMAGE).stdout.strip()
            pulled_id = None if new_id in before else new_id
        created.append(Path(sudo("mktemp", "-d", "/root/desk-it-seed.XXXXXX").stdout.strip()))
        seed = created[-1]
        sudo("install", "-m", "0755", "-o", "root", "-g", "root", str(SCRIPT), str(INSTALLED))
        created.append(INSTALLED)
        sudo("mkdir", "-m", "0755", str(PIN_FILE.parent))   # plain mkdir: fails rather than adopt an existing dir
        created.append(PIN_FILE.parent)
        sudo("mkdir", "-m", "0755", str(STATE))
        created.append(STATE)
        created.append(PIN_FILE)   # registered before writing: a half-written pin is still removed
        subprocess.run(["sudo", "-n", "tee", str(PIN_FILE)], input=IMAGE + "\n", check=True, capture_output=True,
                       text=True)
        sudo("cp", "-R", str(ROOT / "desk"), f"{seed}/desk")
        sudo("chown", "-R", "root:root", str(seed))
        yield seed
    finally:
        for path in reversed(created):
            if path == PIN_FILE.parent:
                sudo("rmdir", str(path), check=False)   # only if still empty: never deletes files it did not create
            elif path == PIN_FILE:
                sudo("rm", "-f", str(path), check=False)
            else:   # the seed dir, the installed wrapper and STATE (its npm cache and stage) are all ours
                sudo("rm", "-rf", "--", str(path), check=False)
        if pulled_id:   # no -f: docker refuses while any container uses it or another tag still references it
            sudo("docker", "image", "rm", pulled_id, check=False)


def caller_dir(src: Path) -> Path:
    work = Path(tempfile.mkdtemp(prefix="gateway-desk-"))
    shutil.copytree(src / "desk", work / "desk", symlinks=True)
    (work / "app/static").mkdir(parents=True)
    return work


def test_seed_is_refused_under_sudo(host):
    out = sudo(str(INSTALLED), "seed", str(host), check=False)
    assert out.returncode == 2 and "refused under sudo" in out.stderr


def test_real_build_reproduces_the_committed_bundle_and_cleans_up(host):
    seed = subprocess.run(["sudo", "-n", "env", "-u", "SUDO_UID", "-u", "SUDO_USER", "-u", "SUDO_GID",
                           "-u", "SUDO_COMMAND", str(INSTALLED), "seed", str(host)], capture_output=True, text=True,
                          timeout=900)
    assert seed.returncode == 0, seed.stderr[-2000:]
    work = caller_dir(ROOT)
    try:
        out = sudo(str(INSTALLED), str(work), check=False, timeout=1800)
        assert out.returncode == 0, out.stderr[-2000:]
        built, committed = tree_files(work / "app/static"), tree_files(ROOT / "app/static")
        assert {k: v[0] for k, v in built.items()} == {k: v[0] for k, v in committed.items()}   # byte-identical set
        assert {mode for _, mode in built.values()} == {0o644}
        assert all(p.owner() == os.environ.get("USER", p.owner()) for p in (work / "app/static").rglob("*"))
        assert sudo("docker", "ps", "-aq", "--filter", "name=release-gateway-desk-build").stdout.strip() == ""
        assert sudo("ls", "-A", str(STATE / "stage")).stdout.strip() == ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_symlink_in_the_callers_tree_is_refused(host):
    work = caller_dir(ROOT)
    try:
        (work / "desk/evil").symlink_to("/etc")
        out = sudo(str(INSTALLED), str(work), check=False, timeout=300)
        assert out.returncode == 2 and "symlink" in out.stderr
        assert list((work / "app/static").iterdir()) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)
