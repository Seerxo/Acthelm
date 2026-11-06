# Contributing to Acthelm

Thank you for your interest. This document explains how to propose a change.

## Contributions and licensing (please read first)

No license has been chosen for this project yet (see [README.md](README.md#license)). Until a license
and a contributor agreement are published, **code and documentation pull requests from outside the
maintainer team cannot be accepted**: a contribution would remain under its author's copyright, and
the project would then need every contributor's permission to choose or change its license.

What is welcome now: bug reports, security reports, questions and design discussion in issues. Once
a license and contribution terms are published, this section will be updated.

## Before you start

- Read [README.md](README.md) for what the project does and how to run it.
- For a large change, open an issue first so we can agree on the approach before you write code.
- Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## Workflow

The repository is trunk-based ([docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md)):

1. Fork the repository and create a short-lived branch from the current `main`, named
   `<type>/<short-slug>` where type is one of `feat`, `fix`, `docs`, `test`, `ci`, `chore`, `refactor`,
   `perf`, `hardening` or `security` (for example `fix/worker-lease-renewal`).
2. Make the smallest change that solves one problem.
3. Add or update tests. A bug fix should come with a test that fails without it.
4. Run the tests locally (see [Running the tests](README.md#running-the-tests)).
5. Open a pull request against `main`. Its title is a Conventional Commit subject, for example
   `fix(worker): renew the lease before the provider call`.

## Pull request expectations

- Describe what changed, why, which tests you ran, and anything left open.
- Keep pull requests focused; prefer under about 400 changed lines.
- Do not commit secrets, real credentials, production addresses, personal data or database dumps.
  Use `.env.example` style placeholders and the synthetic fixture pattern in
  [SECURITY.md](SECURITY.md#synthetic-test-fixtures).

## Required checks

These workflows must pass on the **exact head commit** of the pull request. They are the list in
[`agents/repository_guardian.yaml`](agents/repository_guardian.yaml):

| Workflow | Check (job) name | What it runs |
|---|---|---|
| `.github/workflows/python-tests.yml` | `python-tests` | Compile, single Alembic head, the pytest suite and golden evaluations, and the PostgreSQL, concurrency, Stage C and crash-recovery tests |
| `.github/workflows/frontend.yml` | `frontend-tests` | Desk build, committed-bundle check, unit tests, lint |
| `.github/workflows/secret-scan.yml` | `trufflehog` | TruffleHog secret scan of the pull request's commits |

Also run on pull requests, but not required: `dependency-scan` (OSV, when dependency files change) and
`workflow-lint` (zizmor and actionlint, when workflows change). `prowler` runs on a schedule or by hand,
and `ci` is a manually started readiness check for a `main` commit.

GitHub branch protection on `main` requires these three checks (`python-tests`, `frontend-tests`,
`trufflehog`), a branch that is up to date with `main` and one approving review. On top of that, the merge tooling
(`scripts/merge-pr`) and the Stage C merge gate run their own exact-head check: they refuse a missing,
pending, failed, cancelled, skipped or other-commit run. These exact-head checks apply only when
`merge-pr` is used; a direct GitHub merge is limited by branch protection alone, and Stage C
reconciliation records or blocks it afterwards.

## Safe pull requests

The safe-PR and review sections apply to maintainers and agents now, and to outside contributors once
contributions open.

- One problem per pull request, from a short-lived branch, with tests that fail without the change.
- Never weaken a test, a scanner rule or a security check to make a change pass.
- Changes to `.github/workflows/` must keep the trust-boundary tests passing
  (`tests/test_trust_boundary.py`): GitHub-hosted runners only, read-only tokens, no secrets, no
  privileged triggers, actions pinned to full commit SHAs.
- Changes to `.github/workflows/`, the release gateway, the host verifier, the review signer, the
  agent policies in `agents/` or `tests/test_trust_boundary.py` receive an extra security review.
- Stage C's pre-push privacy gate checks outgoing history and public text for credentials and overlap
  with indexed founder task text (`tests/test_stage_c_privacy_gate.py`). It does not replace reviewing
  every commit for private material: encodings, homoglyphs, rewording and short fragments can pass.
  See [SECURITY.md](SECURITY.md#security-layers-are-separate) for its scope and thresholds.

## Review, merge and deploy

- **Independent review.** Project policy is that every pull request is reviewed by someone other than
  its author. Branch protection requires one approving review. Stage C additionally enforces a
  reviewer different from the coder and binds approval to the exact diff by a fingerprint
  (`app/stage_c_runner.py`). Repository settings are not verified by local tests. A model's answer
  alone is not an approval.
- **Agents cannot approve their own work.** An author reviewing its own change brings the same blind
  spots that produced a mistake to the check that should catch it. Agents also cannot merge, and the
  coding agent's account has only read access to this repository.
- **Exact-head CI.** A green run on an earlier commit does not count. Any new push, rebase or branch
  update invalidates earlier CI evidence; the required checks run again on the new head. For
  agent-written changes, a new diff also needs a new review, because the approval is bound to the
  reviewed diff.
- **Merge and deploy are separate permissions.** Merging is not releasing: a release needs a signed
  manifest; low-risk paths (currently docs only) are gateway-signed after merge, everything else needs
  the founder's signature. Production releases go through the signed release gateway
  ([docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md)), which is a separate authority
  from merging.

## Coding style

- Python 3.12, standard library first; add a dependency only when it removes real work.
- Deterministic code decides whether work exists; a model is called only for judgment.
- Bounded retries, explicit states, and evidence for every claim of success.
- Comments explain *why*, not *what*.

## AI-assisted contributions

Contributions written with AI assistance are welcome. You are responsible for the change: review it,
run the tests, and make sure it does not weaken a security check or a test to make it pass.

## Conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
