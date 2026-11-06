# GIT WORKFLOW — Acthelm

This is the canonical Git and pull request policy for routine development in the public
[`Seerxo/Acthelm`](https://github.com/Seerxo/Acthelm) repository (renamed from `Seerxo/seerxo-os`). The private
[`Seerxo/os`](https://github.com/Seerxo/os) repository is archived and read-only; since the
signed production cutover (2026-10-31) it has no release or migration role. A source migration or
merged PR does not prove production migration. See [permanent facts](PERMANENT_FACTS.md)
and [founder decisions](operations/DECISIONS.md).

Routine development and CI stay on the public repository. Do not restore the previous
private self-hosted CI as the normal engineering path.

## 1. Branches

- **`main` is the only permanent branch.** Do not create `develop`, `integration/*`,
  `release/*` or `hotfix/*` branches. Protected names are rejected by `scripts/check_branch_policy.py`.
- Every change starts on a **short-lived task branch created from current `origin/main`**:
  `<prefix>/<lowercase-slug>`, with prefix `feat`, `fix`, `docs`, `test`, `ci`, `chore`,
  `refactor`, `perf`, `hardening` or `security`. Example: `fix/deploy-follow-reconnect`.
- Keep the branch alive for one task. Rebase on `origin/main` before the PR is opened; do not merge `main` into it.

## 2. Commits

- Conventional Commits: `type(scope): imperative description`. Subject ≤ 100 characters.
- Commit messages explain intent (§78–80). Include the attribution line the session requires.

## 3. Pull requests

- Every code change goes through a PR. Direct pushes to `main` are prohibited.
- **PR title** is a Conventional Commit subject. It is checked by `check_branch_policy.py title`.
- **PR size:** prefer under ~400 changed lines. Split larger work into a sequence of PRs.
- **Batch related small work:** finish related implementation and local checks, then open
  one focused, independently reviewable PR. Related documentation corrections, fixes in
  one component, test corrections, and supporting refactors belong together. One small fix
  does not automatically justify one PR. Separate urgent security fixes, confirmed
  production blockers, independent risk, or work too large for safe review. Do not combine
  unrelated high-risk changes to reduce PR count.
- **Review:** an independent reviewer that is not the author. Agents never approve their own work (§45).
  The reviewer result is recorded; a model result alone is not an approval.
- A PR states what changed, the exact tests run, and what remains blocked.
- Refresh checks on the exact current head. A prior successful run does not validate a
  changed head.

## 4A. Branch updates

- Do not use GitHub's **Update branch** just because a branch is behind `main`.
- If the branch is mergeable and repository rules allow it, leave it unchanged.
- Branch protection (a repository setting) requires a branch that is up to date with `main`, so a
  behind branch must be updated by a maintainer before it can merge (there is no automated refresh tool). The update
  changes the head: it invalidates earlier exact-head CI evidence, so rerun required checks, and
  preserve independent review. For Stage C PRs, see the behind-`main` limit in
  [README.md](../README.md#stage-c-from-task-to-pull-request).

## 4. CI gates

- The required checks `python-tests`, `frontend-tests` and `trufflehog` must pass on the exact head
  commit of the PR. `python-tests` covers compile, Alembic single head, the pytest suite, and the
  deterministic docs and branch-policy checks; the manual `ci` workflow is not required.
- The Postgres-only tests run in their own CI step.
- **Enforcement:** GitHub branch protection on `main` requires those three checks, an up-to-date
  branch and one approving review. It is a repository setting, not code, so nothing here can detect a change to it.
  `scripts/merge-pr` is the audited command path and also refuses a merge unless CI is green on the
  checked head commit. The founder retains direct GitHub merge authority; reconciliation verifies the
  exact head, CI, independent review and founder actor. It records the verified GitHub merge and links
  any matching control-plane authorization, but does not claim which client initiated the merge. GitHub
  cannot prevent another authorized maintainer from bypassing this process.

## 5. Merge process

1. Open the PR. Wait for CI and independent review.
2. The founder normally merges with `scripts/merge-pr <number>`. It pins the merge to the checked head SHA
   and refuses pending, failed or missing CI. Stage C task PRs additionally require the audited control-plane
  authorization and completion receipts. The worker's later reconciliation cannot distinguish that command
  from a direct founder GitHub merge, so the audit records verified GitHub facts and any matching authorization,
  not an unproven invocation path. While a new PR is being recorded, the commit's Stage C task subject also
  makes an unregistered task PR fail closed. This command cannot enforce behavior through GitHub's web UI or API.
3. After a successful merge, `merge-pr` deletes the temporary branch, but only when:
   - the branch is not protected (`main`, `master`, `develop`, `release/*`, `hotfix/*`),
   - the remote tip still equals the merged head (a moved tip is kept),
   - no local worktree has the branch checked out,
   - the merge itself succeeded (a failed or refused merge never reaches cleanup).
   Cleanup failures are warnings. The merge is already complete.
4. Set `MERGE_PR_DELETE_BRANCH=0` to keep a branch on purpose.

## 6. Deployment separation

- Merging to `main` is not releasing. Production releases go through the signed release gateway
  ([security/RELEASE_GATEWAY.md](security/RELEASE_GATEWAY.md)): LOW-risk releases are gateway-signed,
  HIGH-risk releases are founder-signed. Deploy evidence is the deployed API and worker SHA.
- Do not enable auto-merge or automatic deployment unless the founder approves a specific,
  tightly scoped policy. A passing CI run or merged PR is not a production deployment.

## 7. Model-driven and Stage C work

- Claude, Codex and Stage C code-generation runs follow this same workflow. A model subprocess
  never pushes to `main`, never calls the merge controller, and never gets GitHub admin credentials.
- Generated work opens a PR with a task branch name and a Conventional Commit title. Stage C jobs
  that stop before the PR leave no merged PR, so cleanup never touches them.
- Before a Stage C push, the privacy gate scans every commit new to the fork's target branch relative
  to upstream main, plus public PR/commit text, for credentials and overlap with indexed founder task
  text. It fails closed and binds publication to the scanned SHA and remote lease; repair pushes and
  missing-PR recovery also use it (`app/stage_c_runner.py`, `tests/test_stage_c_privacy_gate.py`).
  Its thresholds and residual limits still require human privacy review; see
  [the security policy](../SECURITY.md#security-layers-are-separate).

## 8. Branch retention and recovery

- A merged task branch is deleted. Its history stays in `main` through the merge commit, and the PR
  record keeps the head SHA and review.
- An unmerged branch with unique commits is never deleted by automation. A human decides.
- Historical branches are removed only from a reviewed manifest approved by the founder. The read-only
  inventory and dry run come first (`companyctl` and the manifest).
- Recovery: a deleted branch can be recreated from its recorded head SHA, which the PR and the merge
  log keep.

## 9. Checks

| Check | Command | Blocks |
|---|---|---|
| Branch name and protected names | `python3 scripts/check_branch_policy.py branch NAME` | creating a non-conforming branch |
| PR title | `python3 scripts/check_branch_policy.py title "TITLE"` | a non-conforming PR title |
| Docs references and handoff fields | `python3 scripts/check_docs.py` | broken docs |
| Merge gate and cleanup | `tests/test_merge_pr.py` | regressions in merge and cleanup |
