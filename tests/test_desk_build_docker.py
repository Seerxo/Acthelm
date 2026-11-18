"""Real-Docker integration test of scripts/release-gateway-desk-build (the sudo'd root wrapper), not a substitute.

Opt-in only: ACTHELM_DESK_BUILD_DOCKER_IT=1 on a disposable Linux host with passwordless sudo, Docker and setpriv
(the CI host). It installs the wrapper's fixed paths (/etc/release-gateway/desk-build-image,
/var/lib/release-gateway-desk), seeds the npm cache from this tree's desk/ lockfile as a real root session, runs the
wrapper through sudo exactly as the gateway does, and removes everything again. It refuses to run on a production
host (release ledger present) or over an existing desk-build setup. Standard library + pytest only (run with
--noconftest), so it needs no app dependencies."""
import ast
import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import tempfile
import time
import types
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


def _gateway_drain():
    """The committed _run_drained/_stop_group/_group_alive from app/release_gateway_service.py, loaded without the
    app's dependencies (this file runs with the standard library and pytest only)."""
    src = (ROOT / "app/release_gateway_service.py").read_text()
    names = {"_run_drained", "_stop_group", "_group_alive"}
    tree = ast.parse(src)
    consts = {"DESK_BUILD_STOP_GRACE", "DESK_BUILD_CLEANUP_FAILED", "LEAKED_CONTAINER_ACTION"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    nodes += [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DeskBuildCleanupFailed"]
    nodes += [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in consts for t in n.targets)]
    ns = {"os": os, "select": select, "subprocess": subprocess}
    exec(compile(ast.Module(sorted(nodes, key=lambda n: n.lineno), []), "release_gateway_service.py", "exec"), ns)
    return types.SimpleNamespace(**{k: ns[k] for k in names})


def _build_leftovers(marker: str):
    containers = sudo("docker", "ps", "-aq", "--filter", "name=release-gateway-desk-build").stdout.split()
    procs = [line for line in subprocess.run(["ps", "-eo", "pid,user,args"], capture_output=True,
                                             text=True).stdout.splitlines()
             if INSTALLED.name in line or "--name release-gateway-desk-build" in line or marker in line]
    return containers, procs


@pytest.mark.parametrize("case", ["output_cap", "timeout"])
def test_a_build_the_gateway_gives_up_on_is_stopped_with_its_root_wrapper_and_container(host, case):
    """Through the real sudo'd wrapper: a build that floods output past the cap, or hangs past the deadline, leaves
    no root process and no container behind once _run_drained returns (it SIGTERMs the group; sudo forwards it to
    the wrapper, whose trap removes the container)."""
    seed = subprocess.run(["sudo", "-n", "env", "-u", "SUDO_UID", "-u", "SUDO_USER", "-u", "SUDO_GID",
                           "-u", "SUDO_COMMAND", str(INSTALLED), "seed", str(host)], capture_output=True, text=True,
                          timeout=900)
    assert seed.returncode == 0, seed.stderr[-2000:]
    drain, marker = _gateway_drain(), f"XGW{case.upper()}"
    work = caller_dir(ROOT)
    try:
        pkg = json.loads((work / "desk/package.json").read_text())
        pkg["scripts"]["build"] = (
            f"node -e \"/*{marker}*/setInterval(()=>process.stderr.write('x'.repeat(65536)),1)\"" if case == "output_cap"
            else f"node -e \"/*{marker}*/setInterval(()=>{{}},1000)\"")
        (work / "desk/package.json").write_text(json.dumps(pkg))
        verdict = drain._run_drained(["sudo", "-n", str(INSTALLED), str(work)],
                                     timeout=600 if case == "output_cap" else 90, cap=1 << 20)
        assert verdict == ("output" if case == "output_cap" else "timeout")
        deadline = time.monotonic() + 60
        while (left := _build_leftovers(marker)) != ([], []) and time.monotonic() < deadline:
            time.sleep(2)
        assert left == ([], []), f"the build outlived _run_drained by 60 s: {left}"
    finally:
        for container in sudo("docker", "ps", "-aq", "--filter", "name=release-gateway-desk-build",
                              check=False).stdout.split():
            sudo("docker", "rm", "-f", container, check=False)
        shutil.rmtree(work, ignore_errors=True)


def test_a_tree_built_under_umask_077_still_builds(host):
    """The gateway may create its desk/ tree under a restrictive umask (0700 directories); the root-owned staged copy
    is normalized, so the container (running as the caller) can read it and the build reproduces the bundle."""
    seed = subprocess.run(["sudo", "-n", "env", "-u", "SUDO_UID", "-u", "SUDO_USER", "-u", "SUDO_GID",
                           "-u", "SUDO_COMMAND", str(INSTALLED), "seed", str(host)], capture_output=True, text=True,
                          timeout=900)
    assert seed.returncode == 0, seed.stderr[-2000:]
    old = os.umask(0o077)
    try:
        work = Path(tempfile.mkdtemp(prefix="gateway-desk-"))
        for src in sorted((ROOT / "desk").rglob("*")):     # like default_desk_build: mkdir + write, no copystat
            dst = work / "desk" / src.relative_to(ROOT / "desk")
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
        (work / "app/static").mkdir(parents=True)
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE((work / "desk").stat().st_mode) == 0o700
        out = sudo(str(INSTALLED), str(work), check=False, timeout=1800)
        assert out.returncode == 0, out.stderr[-2000:]
        built, committed = tree_files(work / "app/static"), tree_files(ROOT / "app/static")
        assert {k: v[0] for k, v in built.items()} == {k: v[0] for k, v in committed.items()}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_no_build_starts_beside_a_leaked_container(host):
    """A container left by an earlier build (its removal could not be proven) blocks every new build until an operator
    removes it: the wrapper answers 75 (infrastructure, retried, never a pass) and starts nothing."""
    leak = "release-gateway-desk-build-leaktest"
    sudo("docker", "run", "-d", "--rm", "--name", leak, "--network=none", IMAGE, "sleep", "300")
    work = caller_dir(ROOT)
    try:
        out = sudo(str(INSTALLED), str(work), check=False, timeout=300)
        assert out.returncode == 75 and "operator must remove it" in out.stderr, out.stderr[-2000:]
        assert list((work / "app/static").iterdir()) == []
        names = sudo("docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=release-gateway-desk-build").stdout
        assert names.split() == [leak]                       # nothing new started beside it
    finally:
        sudo("docker", "rm", "-f", leak, check=False)
        shutil.rmtree(work, ignore_errors=True)


def test_a_failed_npm_ci_never_yields_an_archive(host):
    """A dependency install that fails (lockfile out of sync, deterministic, offline) fails the build: no archive, no
    files in app/static, so a candidate that deleted its generated files cannot get a passing rebuild proof."""
    work = caller_dir(ROOT)
    try:
        pkg = json.loads((work / "desk/package.json").read_text())
        pkg.setdefault("dependencies", {})["left-pad-not-in-lockfile"] = "1.0.0"
        (work / "desk/package.json").write_text(json.dumps(pkg))
        out = sudo(str(INSTALLED), str(work), check=False, timeout=600)
        assert out.returncode != 0 and "build failed" in out.stderr, (out.returncode, out.stderr[-2000:])
        assert list((work / "app/static").iterdir()) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_executable_build_script_keeps_its_mode(host):
    """desk/ sources keep their executable bits in the staged copy (only group/world write is dropped), so a build
    that runs a committed 100755 script still builds."""
    work = caller_dir(ROOT)
    try:
        pkg = json.loads((work / "desk/package.json").read_text())
        (work / "desk/build.sh").write_text("#!/bin/sh\nset -e\n" + pkg["scripts"]["build"] + "\n")
        (work / "desk/build.sh").chmod(0o755)
        pkg["scripts"]["build"] = "./build.sh"
        (work / "desk/package.json").write_text(json.dumps(pkg))
        out = sudo(str(INSTALLED), str(work), check=False, timeout=600)
        assert out.returncode == 0, out.stderr[-2000:]
        assert (work / "app/static/desk.html").is_file()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_an_archive_written_to_the_containers_stdout_by_the_build_is_refused(host):
    """The build runs as the container's own uid and can open PID 1's file descriptors: during `npm run build >&2`
    the shell keeps the real stdout on a saved descriptor, so the build can write its own archive into the stream
    ahead of the real one. The stream must then hold exactly one archive, so it is refused, never read in part."""
    work = caller_dir(ROOT)
    try:
        pkg = json.loads((work / "desk/package.json").read_text())
        pkg["scripts"]["build"] += (   # every pipe of PID 1 other than its stderr is the real stdout
            " && for f in /proc/1/fd/*; do case $(readlink $f) in pipe:*) [ \"$(readlink $f)\" ="
            " \"$(readlink /proc/1/fd/2)\" ] || { tar -cf $f package.json && echo XGWINJECTED >&2; };; esac; done")
        (work / "desk/package.json").write_text(json.dumps(pkg))
        out = sudo(str(INSTALLED), str(work), check=False, timeout=600)
        assert "XGWINJECTED" in out.stderr, out.stderr[-2000:]           # the injection really happened
        assert out.returncode != 0 and "archive" in out.stderr, out.stderr[-2000:]
        assert list((work / "app/static").iterdir()) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)
