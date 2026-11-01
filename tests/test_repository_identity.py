"""The upstream is Seerxo/Acthelm (renamed from Seerxo/seerxo-os on 2026-11-01, same repository id); Stage C still
publishes from the fork seerxo-agent/seerxo-os. Break caught: one module keeps the old name (GitHub answers with the
canonical name, so that check silently stops matching), the fork is renamed along with the upstream, or a grant or
role answer that names the old repository is still accepted."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from app import (ci_usage, control, main_guard, models, project_profiles, stage_c_credential_boundary as cb,
                 stage_c_merge_gate, stage_c_worker)
from tests.test_founder_authorization import (ASSERT, CALLERS, FID, SHA, _c, _founder, env)  # noqa: F401 (fixture)
from tests.test_stage_c_founder_boundary import role_run, status
from tests.test_stage_c_worker import FOUNDER_ID, SHA_A, armed

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM, FORK, LEGACY = "seerxo/acthelm", "seerxo-agent/seerxo-os", "seerxo/seerxo-os"


def test_every_upstream_identity_names_acthelm():
    assert stage_c_worker.REPOSITORY == stage_c_merge_gate.REPOSITORY == cb.UPSTREAM == control.AUTH_REPOSITORY \
        == UPSTREAM
    assert main_guard.REPOSITORY == ci_usage.REPOSITORY == "Seerxo/Acthelm"     # GitHub API spelling


def test_the_policies_track_acthelm_and_keep_the_fork():
    guardian = yaml.safe_load((ROOT / "agents/repository_guardian.yaml").read_text())
    assert guardian["repositories"] == [UPSTREAM] and set(guardian["branches"]) == {UPSTREAM}
    assert guardian["publish_forks"] == {UPSTREAM: FORK}                         # the fork is not renamed
    repos = {r for p in yaml.safe_load(project_profiles.PROFILE_PATH.read_text())["projects"].values()
             for r in p.get("repositories", {})}
    assert UPSTREAM in repos and LEGACY not in repos


def test_no_deployed_file_still_authorizes_the_old_upstream_name():
    paths = [p for d in ("app", "agents", "scripts") for p in (ROOT / d).rglob("*") if p.is_file()]
    # The one allowed mention: merge-pr's list of names it refuses.
    refusal = "    seerxo/acthelm|seerxo/seerxo-os|seerxo/os)"
    hits = [f"{p.relative_to(ROOT)}:{line}" for p in paths + [ROOT / "docker-compose.yml"]
            for line in p.read_text(errors="ignore").splitlines()
            if re.search(r"(?<![-\w])seerxo/seerxo-os", line, re.I) and line != refusal]
    assert hits == []


def test_merge_pr_recognises_the_company_repository_by_id_and_name():
    script = (ROOT / "scripts/merge-pr").read_text()
    assert "COMPANY_REPO_ID=R_kgDOVXWcxg" in script and f'[ "$repo" = "{UPSTREAM}" ]' in script
    assert script.count('if [ -n "$company" ]; then') == 2       # both Stage C gates hang on the id check


def _published(url):
    return SimpleNamespace(context={"improvement": {"stage": "PR_OPEN", "history": [
        {"stage": "PR_OPEN", "evidence": {"commit": "a" * 40, "pr": url}}]}})


def test_only_a_pr_on_the_acthelm_upstream_is_polled_or_merged():
    assert stage_c_worker.published_pr_number(_published("https://github.com/seerxo/acthelm/pull/81")) == 81
    for url in ("https://github.com/seerxo/seerxo-os/pull/81", "https://github.com/seerxo-agent/seerxo-os/pull/81"):
        assert stage_c_worker.published_pr_number(_published(url)) is None, url


def test_a_role_answer_for_the_old_name_fails_and_the_canonical_casing_passes():
    perms = {"admin": False, "maintain": False, "push": False, "triage": False, "pull": True}
    old = cb.evaluate({"GH_TOKEN": "x"}, role_run(role_stdout=json.dumps({"full_name": "Seerxo/seerxo-os",
                                                                         "permissions": perms})))
    assert status(old, "worker_upstream_role") == "FAIL"
    new = cb.evaluate({"GH_TOKEN": "x"}, role_run(role_stdout=json.dumps({"full_name": "Seerxo/Acthelm",
                                                                         "permissions": perms})))
    assert status(new, "worker_upstream_role") == "PASS"


def _rename(db, key, repository):
    row = db.get(models.ControlState, key)
    row.value = {**row.value, "repository": repository}
    db.commit()


def test_a_standing_grant_for_the_old_name_is_denied(monkeypatch, tmp_path):
    db, task, _ = armed(monkeypatch, tmp_path)
    control.issue_standing(db, ["tests/test_metrics.py"], SHA_A, founder=FOUNDER_ID)
    assert control.write_authorization_denial(db, task, SHA_A) is None
    _rename(db, control.STANDING_AUTHORIZATION, LEGACY)
    assert control.write_authorization_denial(db, task, SHA_A) == "stage_c_standing_invalid"


def test_a_one_task_grant_for_the_old_name_is_denied(monkeypatch, tmp_path):
    db, task, _ = armed(monkeypatch, tmp_path)
    control.authorize_pilot(db, task.key, SHA_A, founder=FOUNDER_ID)
    now = datetime.now(timezone.utc)
    assert control.pilot_authorization_denial(db, task.key, SHA_A, now) is None
    _rename(db, control.PILOT_AUTHORIZATION, LEGACY)
    assert control.pilot_authorization_denial(db, task.key, SHA_A, now) == "stage_c_authorization_invalid"
