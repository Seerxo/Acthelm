"""The Founder Desk the API serves is built in the image from the reviewed desk/ sources, never taken from git.

app/static in git is frozen at an old build and excluded from the build context; the api stage copies app/static
only from the `desk` stage, which installs exactly the lockfile (no install scripts) on a digest-pinned node
image. The real-Docker test proves it on the release archive itself: it alters the git copy of desk.html before
building, and the image must still hold the desk stage's bytes, file for file.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
SERVED = {"desk.html", "manifest.webmanifest", "sw.js"}   # app/main.py serves these plus icons/


def _stage(name: str) -> str:
    match = re.search(rf"(?ms)^FROM [^\n]+ AS {name}\n(.*?)(?=^FROM |\Z)", DOCKERFILE)
    assert match, f"no {name} stage"
    return match[0]


def test_app_static_never_enters_the_build_context():
    ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    assert "app/static" in ignored


def test_the_desk_stage_installs_the_lockfile_on_a_pinned_image_without_scripts():
    desk = _stage("desk")
    assert re.match(r"FROM node:[\w.-]+@sha256:[0-9a-f]{64} AS desk\n", desk)
    assert "COPY desk/package.json desk/package-lock.json ./" in desk
    assert re.search(r"(?m)^RUN npm ci --ignore-scripts\b", desk)
    assert not re.search(r"(?m)^RUN npm (install|i)\b", desk)


def test_the_api_serves_app_static_only_from_the_desk_stage():
    api = _stage("api")
    lines = [line.strip() for line in api.splitlines() if not line.lstrip().startswith("#")]
    copy_app = lines.index("COPY app ./app")
    from_desk = lines.index("COPY --from=desk /w/app/static ./app/static")
    assert from_desk > copy_app                     # the built bundle replaces whatever COPY app brought
    assert sum("app/static" in line for line in lines) == 1


def _require_hosted_docker():
    if os.environ.get("SEERXO_REAL_DESK_IMAGE") != "1":
        pytest.skip("real image build: GitHub-hosted CI only (SEERXO_REAL_DESK_IMAGE=1)")
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted", "never on a persistent runner"
    assert not os.environ.get("DOCKER_CONTEXT")


def _files(image: str, path: str) -> dict[str, str]:
    cid = subprocess.run(["docker", "create", image], check=True, capture_output=True, text=True).stdout.strip()
    try:
        tar = subprocess.run(["docker", "cp", f"{cid}:{path}", "-"], check=True, capture_output=True).stdout
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        for m in t.getmembers():
            if m.isfile():
                out[m.name.split("/", 1)[1]] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
    return out


def test_the_image_serves_the_desk_stage_build_not_the_git_copy(tmp_path):
    _require_hosted_docker()
    from app import release_gateway as rg
    tarfile.open(fileobj=io.BytesIO(rg.release_archive(ROOT, "HEAD"))).extractall(tmp_path / "ctx", filter="data")
    ctx = tmp_path / "ctx"
    assert (ctx / ".dockerignore").is_file(), "the release archive must ship .dockerignore"
    tampered = ctx / "app/static/desk.html"
    tampered.write_bytes(tampered.read_bytes() + b"<!-- not reviewed -->")
    bad = hashlib.sha256(tampered.read_bytes()).hexdigest()
    for target in ("desk", "api"):
        subprocess.run(["docker", "build", "-q", "--target", target, "-t", f"desk-image-test:{target}", str(ctx)],
                       check=True, capture_output=True)
    built = _files("desk-image-test:desk", "/w/app/static")
    served = _files("desk-image-test:api", "/app/app/static")
    assert SERVED <= set(built) and any(name.startswith("icons/") for name in built), json.dumps(sorted(built))
    assert served == built                           # every served file is the desk stage's, nothing else
    assert served["desk.html"] != bad                # the altered git copy never reached the image
