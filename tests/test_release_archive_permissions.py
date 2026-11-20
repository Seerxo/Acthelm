"""The release archive the deploy ships must not make production files group- or world-writable.

These tests run the deploy script's OWN packing lines (extracted verbatim from
scripts/deploy-company-os.sh) against real git archives, so a change to that command is what is tested.
Nothing touches a host path.
"""
import hashlib
import io
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "scripts" / "deploy-company-os.sh"
SOURCE = DEPLOY.read_text()


def packing_block() -> str:
    """From `PATHS=` through the line that writes main.sha256: the exact commands the deploy runs."""
    start = SOURCE.index("\nPATHS=") + 1
    end = SOURCE.index('> "$TMP/main.sha256"', start) + len('> "$TMP/main.sha256"')
    block = SOURCE[start:end]
    # Only the deploy's temp-dir setup is replaced (the test supplies TMP); every packing line runs as is.
    plumbing = ["TMP=$(mktemp -d)\n", "trap 'rm -rf \"$TMP\"' EXIT\n"]
    for line in plumbing:
        assert line in block, line
        block = block.replace(line, "", 1)
    return block


def pack(repo: Path, out: Path, sha: str = "HEAD", env: dict | None = None) -> tuple[tarfile.TarFile, list[str]]:
    script = f'set -euo pipefail\ncd "$1"\nSHA=$(git rev-parse "{sha}")\nTMP="$2"\n{packing_block()}\n'
    subprocess.run(["bash", "-c", script, "_", str(repo), str(out)], check=True, capture_output=True,
                   env=os.environ | (env or {}))
    data = (out / "src.tgz").read_bytes()
    sums = (out / "main.sha256").read_text().splitlines()
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"), sums


def writable_by_others(mode: int) -> bool:
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def test_the_real_release_archive_has_no_group_or_world_writable_entry(tmp_path):
    archive, _ = pack(ROOT, tmp_path)
    members = archive.getmembers()
    assert members
    for m in members:
        if m.issym():
            continue                                              # a link's own mode is meaningless
        if m.isdir():
            assert m.mode == 0o755, (m.name, oct(m.mode))
        elif m.mode & stat.S_IXUSR:
            assert m.mode == 0o755, (m.name, oct(m.mode))
        else:
            assert m.mode == 0o644, (m.name, oct(m.mode))
        assert not writable_by_others(m.mode), m.name
    executables = {m.name for m in members if m.isfile() and m.mode & stat.S_IXUSR}
    assert "scripts/companyctl" in executables and "scripts/deploy-company-os.sh" in executables
    assert "app/main.py" not in executables


def test_a_local_git_config_cannot_loosen_the_archive_modes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[tar]\n\tumask = 0000\n")       # an operator's loose global setting
    out = tmp_path / "out"
    out.mkdir()
    archive, _ = pack(ROOT, out, env={"HOME": str(home), "XDG_CONFIG_HOME": str(home)})
    assert not any(writable_by_others(m.mode) for m in archive.getmembers() if not m.issym())


def test_release_scope_and_checksums_are_unchanged_by_the_mode_fix(tmp_path):
    """Same files, same bytes: the archive list equals git's tree for PATHS and the checksum list."""
    archive, sums = pack(ROOT, tmp_path)
    paths = re.search(r'^PATHS="(.*)"$', SOURCE, re.M).group(1).split()
    tree = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only", "HEAD", "--", *paths],
                          check=True, capture_output=True, text=True).stdout.split()
    files = sorted(m.name for m in archive.getmembers() if m.isfile() or m.issym())
    listed = sorted(line[66:] for line in sums)
    assert files == sorted(tree) == listed
    for line in sums:
        want, rel = line[:64], line[66:]
        assert hashlib.sha256(archive.extractfile(rel).read()).hexdigest() == want, rel


def _toy_repo(tmp_path: Path) -> Path:
    """A repository with every PATHS entry, an executable, a nested directory and a symlink."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for p in re.search(r'^PATHS="(.*)"$', SOURCE, re.M).group(1).split():
        target = repo / p
        if "." in Path(p).name or p in ("Dockerfile",):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {p}\n")
        else:
            (target / "nested").mkdir(parents=True)
            (target / "nested" / "file.txt").write_text("x\n")
    run = repo / "scripts" / "run.sh"
    run.write_text("#!/bin/sh\necho ok\n")
    run.chmod(0o775)
    (repo / "app" / "link.txt").symlink_to("nested/file.txt")
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "toy"], check=True)
    return repo


def test_symlinks_stay_symlinks_and_everything_else_is_tightened(tmp_path):
    repo = _toy_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    archive, sums = pack(repo, out)
    by_name = {m.name: m for m in archive.getmembers()}
    link = by_name["app/link.txt"]
    assert link.issym() and link.linkname == "nested/file.txt"
    assert by_name["scripts/run.sh"].mode == 0o755
    assert by_name["app/nested/file.txt"].mode == 0o644
    assert by_name["app/nested"].isdir() and by_name["app/nested"].mode == 0o755
    assert "app/link.txt" in {line[66:] for line in sums}


def test_the_previous_command_was_vulnerable(tmp_path):
    """Pins the root cause: without the explicit umask, git's default packs group-writable modes."""
    loose = packing_block().replace("git -c tar.umask=0022 archive", "git archive")
    assert loose != packing_block()
    script = f'set -euo pipefail\ncd "$1"\nSHA=$(git rev-parse HEAD)\nTMP="$2"\n{loose}\n'
    subprocess.run(["bash", "-c", script, "_", str(ROOT), str(tmp_path)], check=True, capture_output=True,
                   env=os.environ | {"HOME": str(tmp_path)})                # no user config: git's default
    archive = tarfile.open(tmp_path / "src.tgz", "r:gz")
    assert any(writable_by_others(m.mode) for m in archive.getmembers() if not m.issym())


def test_the_pre_deploy_backup_keeps_and_restores_the_old_modes(tmp_path):
    """Rollback restores files-before.tgz, which the deploy creates from the host's CURRENT files: their
    old 0775/0664 modes must come back exactly. Runs the script's own backup and restore lines; umask 0
    stands in for root, whose tar extraction does not apply a umask."""
    backup_line = next(l for l in SOURCE.splitlines() if l.startswith('tar -czf "$B/files-before.tgz"'))
    restore_line = 'restore_source "$B"'
    assert SOURCE.count(restore_line) >= 3                                   # the rollback paths use exactly this
    fn = SOURCE[SOURCE.index("restore_source() {"):SOURCE.index("\n}\n", SOURCE.index("restore_source() {")) + 3]
    host = tmp_path / "opt"
    (host / "scripts").mkdir(parents=True)
    (host / "app").mkdir()
    old = {"scripts/companyctl": 0o775, "scripts/deploy-health-gate.sh": 0o775, "app/main.py": 0o664}
    for rel, mode in old.items():
        (host / rel).write_text(f"old {rel}\n")
        (host / rel).chmod(mode)
    listing = tmp_path / "main.sha256"
    listing.write_text("".join(f"{'0' * 64}  {rel}\n" for rel in old))
    script = f'''set -euo pipefail
umask 000
cd "$1"
B="$2"
{backup_line.replace('"$REL/main.sha256"', '"$3"')}
for f in {" ".join(old)}; do printf "new\\n" > "$f"; chmod 0755 "$f"; done   # the new release lands
{fn.replace("/opt/company-os", '"$HOSTROOT"')}
HOSTROOT="$1"
restore_source "$B"
'''
    backups = tmp_path / "b"
    backups.mkdir()
    subprocess.run(["bash", "-c", script, "_", str(host), str(backups), str(listing)], check=True,
                   capture_output=True)
    for rel, mode in old.items():
        assert stat.S_IMODE((host / rel).stat().st_mode) == mode, rel
        assert (host / rel).read_text() == f"old {rel}\n", rel


# _toy_repo (above) creates every entry of the driver's PATHS: desk/ as a directory, .dockerignore as a file.
def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm",
                    message], check=True)


def test_the_driver_and_the_gateway_pack_the_same_files(tmp_path):
    """One release payload: the deploy driver's archive and the gateway/host release_archive hold the same members,
    desk/ and .dockerignore included."""
    from app import release_gateway as rg
    repo = _toy_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    archive, _ = pack(repo, out)
    driver = sorted(m.name for m in archive.getmembers() if not m.isdir())
    gateway = tarfile.open(fileobj=io.BytesIO(rg.release_archive(repo, "HEAD")))
    assert driver == sorted(m.name for m in gateway.getmembers() if not m.isdir())
    assert ".dockerignore" in driver and "desk/nested/file.txt" in driver


def test_only_desk_and_dockerignore_may_be_absent(tmp_path):
    """An older commit without the two new paths still packs; a commit missing any other release path does not."""
    repo = _toy_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "rm", "-rq", "desk", ".dockerignore"], check=True)
    _commit(repo, "older: no desk")
    out = tmp_path / "old"
    out.mkdir()
    archive, _ = pack(repo, out)
    assert not any(m.name.startswith("desk") or m.name == ".dockerignore" for m in archive.getmembers())
    subprocess.run(["git", "-C", str(repo), "rm", "-rq", "migrations"], check=True)
    _commit(repo, "broken: no migrations")
    out = tmp_path / "broken"
    out.mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        pack(repo, out)


def test_desk_and_dockerignore_are_image_inputs(tmp_path):
    """The image build consumes desk/ and .dockerignore, so a change to either is a new inputs_digest (a new build
    record), never a reuse of images built from other sources."""
    from app import release_gateway as rg
    repo = _toy_repo(tmp_path)
    digest = lambda: rg.inputs_digest(rg.release_archive(repo, "HEAD"))
    before = digest()
    (repo / "desk" / "nested" / "file.txt").write_text("changed\n")
    _commit(repo, "desk source")
    after_desk = digest()
    (repo / ".dockerignore").write_text("app/static\n")
    _commit(repo, "build context")
    assert len({before, after_desk, digest()}) == 3


RUNNING = "caab3d38851665bcb674e08be78acfc375ac4533"   # the release production runs: desk/ in git, no .dockerignore


def _members(tar: tarfile.TarFile) -> dict:
    return {m.name: (m.mode, m.type, m.linkname, tar.extractfile(m).read() if m.isfile() else b"")
            for m in tar.getmembers() if not m.isdir()}


def _older_commits(tmp_path: Path):
    repo = _toy_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "rm", "-rq", "desk", ".dockerignore"], check=True)
    _commit(repo, "older: no desk")
    yield repo, "HEAD"
    if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{RUNNING}^{{commit}}"]).returncode == 0:
        yield ROOT, RUNNING                                # skipped in a shallow clone without that history


def test_an_older_commit_packs_the_same_payload_in_the_driver_and_the_gateway(tmp_path):
    """An older commit (from before desk/ shipped, including the running release caab3d3) still packs: the deploy
    driver and release_archive (what the gateway reviews and the host builds) produce the same members with the same
    modes and bytes. Declared rollback is disabled (release_gateway._check_rollback); this is about packing only."""
    from app import release_gateway as rg
    for n, (repo, sha) in enumerate(_older_commits(tmp_path)):
        out = tmp_path / f"out{n}"
        out.mkdir()
        driver, _ = pack(repo, out, sha)
        gateway = tarfile.open(fileobj=io.BytesIO(rg.release_archive(repo, sha)))
        got, want = _members(driver), _members(gateway)
        assert got == want, sorted(set(got) ^ set(want))
        assert ".dockerignore" not in got                  # absent from these commits; nothing invents it


def test_the_previous_path_list_leaves_out_exactly_desk_and_dockerignore(tmp_path):
    """What a rebase recomputes for a release recorded before desk/ shipped: same files minus the two new paths."""
    from app import release_gateway as rg
    repo = _toy_repo(tmp_path)
    now, before = (rg.payload_listing(repo, "HEAD", paths) for paths in (rg.DEPLOY_PATHS, rg.PREVIOUS_DEPLOY_PATHS))
    dropped = {line[66:] for line in now.splitlines()} - {line[66:] for line in before.splitlines()}
    assert dropped == {".dockerignore", "desk/nested/file.txt"}
    assert set(before.splitlines()) < set(now.splitlines())


@pytest.mark.parametrize("name", ["app/Türkçe ğüşıöç.py", "app/with space.txt", "app/tab\tname.txt", "desk/ünicode/é.ts"])
def test_the_driver_manifest_names_files_exactly_as_the_gateway(tmp_path, name):
    """main.sha256 from the driver equals payload_listing for names git would C-quote (non-ASCII) or that hold a space
    or a tab: both read NUL-separated paths with core.quotePath=false, so the host's manifest check and the gateway's
    digest agree. Non-ASCII names and spaces stay raw; git still quotes control characters such as a tab in
    %(path) on both sides, so such a file never matches its build-context name and the build refuses it."""
    from app import release_gateway as rg
    repo = _toy_repo(tmp_path)
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text("x\n")
    _commit(repo, "odd name")
    out = tmp_path / "out"
    out.mkdir()
    _, sums = pack(repo, out)
    if "\t" not in name:
        assert any(line.endswith("  " + name) for line in sums)
    assert rg.payload_digest("\n".join(sums) + "\n") == rg.payload_digest(rg.payload_listing(repo, "HEAD"))


def test_the_gateway_refuses_a_missing_required_path_as_the_driver_does(tmp_path):
    """One rule on both sides: only desk/ and .dockerignore may be absent. release_archive and payload_listing (what
    the gateway reviews and the host builds) refuse a commit that lacks any other release path."""
    from app import release_gateway as rg
    repo = _toy_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "rm", "-rq", "desk", ".dockerignore"], check=True)
    _commit(repo, "older: no desk")
    rg.release_archive(repo, "HEAD")                                   # the two optional paths may be absent
    rg.payload_listing(repo, "HEAD")
    subprocess.run(["git", "-C", str(repo), "rm", "-rq", "migrations"], check=True)
    _commit(repo, "broken: no migrations")
    for build in (rg.release_archive, rg.payload_listing):
        with pytest.raises(rg.Refused, match="lacks required paths: migrations"):
            build(repo, "HEAD")
    assert rg.REQUIRED_DEPLOY_PATHS == tuple(p for p in rg.DEPLOY_PATHS if p not in ("desk", ".dockerignore"))
