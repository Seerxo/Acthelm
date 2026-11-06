# Security policy

## Reporting a vulnerability

Please **do not** open a public issue, discussion or pull request for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. This creates a private advisory that only the maintainers can see.

Include, where you can:

- the affected component and file (for example `app/release_host.py`),
- the commit you tested,
- steps to reproduce or a proof of concept,
- the impact you expect (what an attacker gains).

We will acknowledge the report, keep you informed while we investigate, and credit you in the advisory
if you want. Please give us reasonable time to fix the problem before you disclose it.

## Supported versions

Only the latest commit on `main` is supported. There are no maintained release branches.

## Scope

In scope:

- the API, worker, reconciler and Founder Desk (`app/`, `desk/`),
- the Stage C coding pipeline, the review signer and the release gateway and host verifier,
- the backup, restore, deploy and operator scripts (`scripts/`),
- the GitHub Actions workflows (`.github/workflows/`).

Out of scope:

- vulnerabilities in third-party dependencies that are already public (please still tell us if a
  dependency is exploitable through this project),
- reports that need physical access to a machine or a compromised maintainer account,
- findings from automated scanners without a demonstrated impact.

## What must never be committed

This repository is public. Never commit, in code, tests, fixtures, documentation, commit messages or
pull request text:

- credentials of any kind: API keys, tokens, passwords, OAuth or session tokens, private keys,
  certificates, `.env` files (only [`.env.example`](.env.example) with placeholders is tracked);
- production data: database dumps, backups, logs, customer or personal data;
- private infrastructure details: host names, IP addresses, host accounts, internal URLs;
- live operational state, such as current grants and authorizations.

Refer to a secret by its variable name only. [`.gitignore`](.gitignore) excludes the common key, `.env`,
dump and backup file types, but it is a convenience, not a control.

## Synthetic test fixtures

Tests that exercise secret detection and redaction need values that look like credentials. Those
values are synthetic, and they are **built at runtime by concatenation** so that no complete
credential-shaped literal appears in the source and secret scanners do not fire on the repository.
For example, `tests/test_stage_c_hardening.py` builds a fake token as `"github_pat_" + "A1b2C3" * 6`
and a fake private-key marker from separate string pieces. Follow the same pattern for any new fixture,
use `example.invalid` or `example.com` for addresses, and never paste a real value, even a revoked one.

## Running the security tests

These tests need only the Python dependencies (`pip install -r requirements.txt`), not a database:

```bash
python -m pytest -q tests/test_trust_boundary.py tests/test_secret_scan_workflow.py \
  tests/test_security_scan_workflows.py tests/test_stage_c_hardening.py \
  tests/test_stage_c_privacy_gate.py tests/test_desk_data_exposure.py tests/test_api_auth.py
```

- `tests/test_trust_boundary.py`: what a repository change can reach in CI (runners, triggers, tokens,
  secrets, action pinning).
- `tests/test_secret_scan_workflow.py`, `tests/test_security_scan_workflows.py`: the scanner workflows
  stay pinned, read-only, secret-free and, for the secret scan, required on every pull request.
- `tests/test_stage_c_hardening.py`: Stage C redaction and the outgoing-diff credential check.
- `tests/test_stage_c_privacy_gate.py`: historical leaks, task-text overlap, credentials, fail-closed
  scans, scan/push binding, repair pushes and missing-PR recovery.
- `tests/test_desk_data_exposure.py`: write/read redaction, allowlisted task detail, fail-closed
  redaction, security headers and the script-hash CSP (script discovery uses `html.parser`).
- `tests/test_api_auth.py`: browser cross-site write checks and the Host allowlist, including DNS
  rebinding refusals. These are local request tests, not a live browser or tunnel assessment.

The release gateway and host verifier have their own suites (`tests/test_release_gateway.py`,
`tests/test_release_host.py`); the full backend suite is described in
[README.md](README.md#running-the-tests). These tests are tripwires: anyone who can change the
repository can change them.

## Security layers are separate

Each layer below catches a different failure. None of them replaces another, and passing one says
nothing about the others.

| Layer | What it checks | Where |
|---|---|---|
| Pre-push privacy gate | Stage C scans every commit new to the fork's target branch relative to upstream main: commit objects, full changed blobs, paths and public PR/commit text. It checks credentials and overlap with indexed founder task text, fails closed on unscannable content or errors, and binds the scan to the pushed SHA and remote lease. Repair pushes and missing-PR recovery are gated too | `app/stage_c_runner.py`, `tests/test_stage_c_privacy_gate.py` |
| Commit history scan | TruffleHog scans the commits of every pull request and every push to `main`; it fails on verified or unknown-verification findings. It does not rescan the whole history on each run | `.github/workflows/secret-scan.yml` |
| Runtime and Desk filtering | Model/provider output, errors and audit snapshots are redacted at write boundaries and again on Desk reads of stored text; task detail exposes allowlisted context/artifact projections. `redact()` returns fixed placeholders on errors or oversized input; notifications also strip the bot token | `app/redact.py`, `app/worker.py`, `app/desk.py`, `app/notify.py`, `tests/test_desk_data_exposure.py` |
| Browser response controls | Security headers and a hash-based Desk CSP restrict scripts to the served inline bundle; `html.parser` discovers the script bodies | `app/main.py`, `tests/test_desk_data_exposure.py` |
| Browser write boundary | Writes carrying Origin or Sec-Fetch-Site must use the configured Desk Host or a fixed loopback endpoint, and pass the same-origin checks. The Host allowlist also refuses matching attacker Host/Origin pairs used in DNS rebinding | `app/api_auth.py`, `tests/test_api_auth.py`, `tests/test_desk_data_exposure.py` |
| Release verification | Production applies only a release the host-side verifier accepts against a signed policy and the release ledger | `app/release_host.py`, [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md) |

The scanners and filters are pattern-based and can miss a secret. If one reaches a commit, treat it as
compromised and rotate it; removing it from the tree does not remove it from Git history.

The privacy gate indexes the task's description, objective, success criteria and context, excluding
generated `improvement` evidence and the task title. Overlap requires 8 normalized words or 64
normalized characters; a contiguous match in base Python code (excluding comments and literals) is
exempt. Homoglyphs, arbitrary encodings, rewording, translation, inserted junk words and fragments below
both thresholds are not caught by this overlap rule. Unrelated private facts absent from those task
fields are outside its coverage, and the gate cannot retract earlier publications.

Redaction is not a universal data-loss prevention system. The CSP still allows inline styles; its
parser does not model every browser script-data escape state, and a test pins the built bundle's
absence of those constructs. Writes with neither browser header bypass the browser-specific checks
and still need caller authentication. These controls do not stop a compromised same-origin bundle or
replace correct tunnel, proxy and identity configuration.

## How this repository is protected

- No secrets are stored in the repository and no workflow uses a repository secret.
- Pull request code runs only on fresh GitHub-hosted runners with a read-only token.
- GitHub branch protection on `main` requires the `python-tests`, `frontend-tests` and `trufflehog`
  checks, an up-to-date branch and one approving review; `scripts/merge-pr` and the Stage C merge gate add their own
  exact-head checks on top. Those apply only when `merge-pr` is used; a direct GitHub merge is limited
  by branch protection alone, and Stage C reconciliation records or blocks it afterwards.
- Coding agents publish only to a fork and cannot merge, including under a Stage C standing
  authorization; production applies only releases that a host-side verifier checks against a signed
  policy. See [docs/security/RELEASE_GATEWAY.md](docs/security/RELEASE_GATEWAY.md) and
  [docs/security/THREAT_MODEL.md](docs/security/THREAT_MODEL.md).
